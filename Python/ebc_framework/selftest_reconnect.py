"""Offline regression for launchTest.py's dropped-connection auto-reconnect.

Simulates exactly one connection drop (an OSError from the transport, the
same exception class a real pyserial disconnect raises) partway through a
run, and checks that run_with_reconnect backs off, reopens the device, and
resumes the interrupted test to completion using the checkpoint file --
without needing real hardware.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import tempfile
import time

from dataclasses import replace

from ebc.celllib import CellLibrary
from ebc.device import EBCA20
from ebc.experiment import DataLogger
from ebc.runner import TestDef
from ebc.transport import SimulatorTransport
from launchTest import run_with_reconnect, write_json_atomic, last_csv_time

ROOT = os.path.dirname(os.path.abspath(__file__))
cell = CellLibrary(os.path.join(ROOT, "config", "cells")).get("LP604765")


class _FailAfterNTransport:
    """Wraps a real transport; raises OSError on the Nth read_frames() call
    (simulating a drop partway through, not instantly), then is never used
    again -- a fresh device is built for the reconnect.

    Once broken, write() also raises -- a real dead USB-serial connection
    doesn't suddenly accept writes again just because disconnect() tries one
    (see EBCA20.disconnect()'s STOP write during cleanup). Tracks whether
    close() was actually called, to catch a handle leak on reconnect.
    """

    def __init__(self, inner, fail_on_call: int):
        self._inner = inner
        self._fail_on_call = fail_on_call
        self._calls = 0
        self._broken = False
        self.closed = False

    def write(self, frame: bytes) -> None:
        if self._broken:
            raise OSError("simulated USB-serial disconnect (write)")
        self._inner.write(frame)

    def read_frames(self):
        self._calls += 1
        if self._calls == self._fail_on_call:
            self._broken = True
            raise OSError("simulated USB-serial disconnect")
        return self._inner.read_frames()

    def close(self) -> None:
        self.closed = True


def run_reconnect_case(folder: str) -> None:
    path = os.path.join(folder, "reconnect_test.csv")
    checkpoint_path = os.path.join(folder, "reconnect_test_checkpoint.json")
    test = TestDef({
        "name": "reconnect_test", "type": "rest", "cell": cell.id,
        "sample_period_s": 1.0, "initial_rest_s": 0, "max_duration_s": 20,
    })

    # One shared simulated cell so its state (SOC, etc.) survives the
    # simulated disconnect exactly as a real cell's state would. accel=200
    # keeps the test fast without changing the number of ticks (device.py
    # scales its real-time sleep-per-tick by the same factor).
    shared_transport = SimulatorTransport(
        capacity_ah=cell.nominal_capacity_ah, soc0=.8,
        emit_period_s=0.0, time_accel=200)
    build_calls = {"n": 0}
    first_transport = {}

    def build_device():
        build_calls["n"] += 1
        if build_calls["n"] == 1:
            transport = _FailAfterNTransport(shared_transport, fail_on_call=8)
            first_transport["t"] = transport
        else:
            transport = shared_transport
        return EBCA20(transport, limits=cell.safety_limits())

    logger = DataLogger(path)
    checkpoint = {
        "version": 1, "complete": False, "test_path": "n/a",
        "cell_id": cell.id, "csv_path": os.path.abspath(path),
        "runner_state": {"stage": "not_started", "csv_elapsed_s": 0.0},
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    write_json_atomic(checkpoint_path, checkpoint)

    def save_progress(runner_state: dict) -> None:
        checkpoint["runner_state"] = runner_state
        checkpoint["saved_at_epoch"] = time.time()
        checkpoint["complete"] = False
        write_json_atomic(checkpoint_path, checkpoint)

    dev = build_device()
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        result = run_with_reconnect(
            dev, build_device, test, cell, logger, checkpoint, checkpoint_path,
            resume_state=None, save_progress=save_progress,
            auto_reconnect=True, backoff_s=0.01, max_attempts=0)
    logger.close()
    output = captured.getvalue()
    print(output, end="")

    assert build_calls["n"] == 2, "expected exactly one reconnect"
    # The dead transport from the first attempt must be closed before the
    # second EBCA20.on_serial-equivalent opens the same port again -- else
    # two live handles compete for incoming bytes and every future read
    # silently starves, even though the device keeps working and writes
    # still go through (see EBCA20.disconnect()).
    assert first_transport["t"].closed, "old transport was never closed -- handle leak"
    assert "Reconnected. Resuming: main" in output
    # The second "Passive observation" line must show less than the full
    # 20s -- proof this resumed mid-run rather than restarting from zero.
    second_rest_line = output.split("Passive observation:")[2]
    remaining_s = float(second_rest_line.split("zero applied current for")[1].split("s")[0])
    assert 5 <= remaining_s <= 15, (
        f"expected a mid-run remaining duration, got {remaining_s}s", output)
    assert result is not None and result.reason == "passive observation complete", result
    # Receive polling is intentionally more frequent than CSV logging. A
    # simulated failure can therefore land between scheduled log ticks and
    # omit several samples around teardown/reconnect; the properties under
    # test are a sufficiently populated, monotonic timeline that reaches the
    # intended end and a resume from roughly where it left off (~7s in).
    assert 14 <= result.samples <= 22, result
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert 14 <= len(rows) <= 22, f"expected a near-complete 20s run, got {len(rows)} rows"
    times = [float(r["t_s"]) for r in rows]
    assert times == sorted(times), "timestamps must stay monotonic across the drop"
    assert times[-1] >= 19.0


def run_give_up_case(folder: str) -> None:
    """A connection that never recovers must eventually raise, not hang."""
    path = os.path.join(folder, "give_up_test.csv")
    checkpoint_path = os.path.join(folder, "give_up_test_checkpoint.json")
    test = TestDef({
        "name": "give_up_test", "type": "rest", "cell": cell.id,
        "sample_period_s": 1.0, "initial_rest_s": 0, "max_duration_s": 20,
    })

    class _AlwaysFailTransport:
        def write(self, frame: bytes) -> None: pass
        def read_frames(self): raise OSError("permanently gone")
        def close(self) -> None: pass

    def build_device():
        return EBCA20(_AlwaysFailTransport(), limits=cell.safety_limits())

    logger = DataLogger(path)
    checkpoint = {
        "version": 1, "complete": False, "test_path": "n/a",
        "cell_id": cell.id, "csv_path": os.path.abspath(path),
        "runner_state": {"stage": "not_started", "csv_elapsed_s": 0.0},
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    write_json_atomic(checkpoint_path, checkpoint)

    def save_progress(runner_state: dict) -> None:
        checkpoint["runner_state"] = runner_state
        write_json_atomic(checkpoint_path, checkpoint)

    dev = build_device()
    try:
        run_with_reconnect(
            dev, build_device, test, cell, logger, checkpoint, checkpoint_path,
            resume_state=None, save_progress=save_progress,
            auto_reconnect=True, backoff_s=0.01, max_attempts=3)
        raised = False
    except OSError:
        raised = True
    logger.close()
    assert raised, "a permanently broken connection must eventually raise, not retry forever"


def run_hard_safety_stop_case(folder: str) -> None:
    """A genuine safety-limit stop must NEVER be treated as a reconnectable
    drop -- it's a normal (non-exception) RunResult, so run_with_reconnect
    must return it immediately with zero reconnect attempts."""
    path = os.path.join(folder, "safety_stop_test.csv")
    checkpoint_path = os.path.join(folder, "safety_stop_test_checkpoint.json")
    unsafe_cell = replace(cell, hard_max_v=3.0)  # guarantees an immediate v_max trip
    test = TestDef({
        "name": "safety_stop_test", "type": "rest", "cell": unsafe_cell.id,
        "sample_period_s": 1.0, "initial_rest_s": 0, "max_duration_s": 20,
        "voltage_cutoff_rest_s": 10,
    })
    build_calls = {"n": 0}

    def build_device():
        build_calls["n"] += 1
        transport = SimulatorTransport(
            capacity_ah=unsafe_cell.nominal_capacity_ah, soc0=.8,
            emit_period_s=0.0, time_accel=200)
        return EBCA20(transport, limits=unsafe_cell.safety_limits())

    logger = DataLogger(path)
    checkpoint = {
        "version": 1, "complete": False, "test_path": "n/a",
        "cell_id": unsafe_cell.id, "csv_path": os.path.abspath(path),
        "runner_state": {"stage": "not_started", "csv_elapsed_s": 0.0},
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    write_json_atomic(checkpoint_path, checkpoint)

    def save_progress(runner_state: dict) -> None:
        checkpoint["runner_state"] = runner_state
        write_json_atomic(checkpoint_path, checkpoint)

    dev = build_device()
    result = run_with_reconnect(
        dev, build_device, test, unsafe_cell, logger, checkpoint,
        checkpoint_path, resume_state=None, save_progress=save_progress,
        auto_reconnect=True, backoff_s=0.01, max_attempts=0)
    logger.close()

    assert build_calls["n"] == 1, (
        "a safety stop must never trigger a reconnect attempt", build_calls["n"])
    assert result is not None and result.reason.startswith("safety stop: v_max"), result
    assert "recovery rest" not in result.reason, (
        "a hard fault must not be treated as a recoverable end-of-discharge cutoff")


class _GoesSilentTransport:
    """Wraps a real transport; after `go_silent_after` write() calls (i.e.
    once a command has been sent, matching "device applies current but PC
    hears nothing back"), read_frames() returns nothing at all -- no data,
    no exception -- forever. This is the actual failure mode reported: not a
    thrown error, just a link that goes quiet without ever raising, which
    only a watchdog (not exception handling) can detect."""

    def __init__(self, inner, go_silent_after: int = 1):
        self._inner = inner
        self._go_silent_after = go_silent_after
        self._writes = 0

    def write(self, frame: bytes) -> None:
        self._writes += 1
        if self._writes <= self._go_silent_after:
            self._inner.write(frame)
        # else: device is imagined to still be receiving/acting on this in
        # reality, but our simulated one-way link no longer forwards it.

    def read_frames(self):
        if self._writes > self._go_silent_after:
            return iter(())
        return self._inner.read_frames()

    def close(self) -> None:
        pass


def run_watchdog_case() -> None:
    """A link that goes silent without ever raising must still be caught --
    by the no_response_timeout_s watchdog in EBCA20.run(), not exception
    handling, since there is no exception to catch."""
    transport = SimulatorTransport(
        capacity_ah=cell.nominal_capacity_ah, soc0=.8,
        emit_period_s=0.0, time_accel=1.0)
    dev = EBCA20(_GoesSilentTransport(transport, go_silent_after=0),
                limits=cell.safety_limits())
    raised = None
    with dev:
        try:
            dev.run(lambda m, t: None, period_s=0.05, max_duration_s=5.0,
                   no_response_timeout_s=0.3)
        except OSError as exc:
            raised = exc
    assert raised is not None, (
        "a silently dead link (no data, no exception) must still raise "
        "via the watchdog -- otherwise nothing can ever catch it")
    assert "no response from device" in str(raised)


def main() -> None:
    run_watchdog_case()
    with tempfile.TemporaryDirectory(prefix="ebc_reconnect_test_") as folder:
        run_reconnect_case(folder)
        run_give_up_case(folder)
        run_hard_safety_stop_case(folder)
    print("RECONNECT/AUTO-RESUME CHECKS PASSED")
    print("  silent link (no exception) -> watchdog raises -> catchable")
    print("  one simulated drop -> automatic reconnect -> run completes")
    print("  permanent failure -> gives up after max_attempts, does not hang")
    print("  hard safety stop -> zero reconnect attempts, stop is permanent")


if __name__ == "__main__":
    main()
