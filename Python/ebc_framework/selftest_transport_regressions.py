"""Offline regressions for framing, polling, and reconnect state handling."""

from __future__ import annotations

import csv
import json
import os
import tempfile
from types import SimpleNamespace

import launchTest
from ebc.device import EBCA20
from ebc.protocol import EOF, SOF, crc
from ebc.transport import FrameReassembler, SimulatorTransport


def check_crc_equal_to_eof_is_not_a_delimiter() -> None:
    # All payload bytes are below the reserved framing values, but their XOR
    # is exactly EOF. The old delimiter search emitted an 18-byte frame.
    payload = bytes([0xEF, 0x17] + [0x00] * 14)
    assert crc(payload) == EOF
    frame = bytes([SOF]) + payload + bytes([crc(payload), EOF])
    assert len(frame) == 19

    reassembler = FrameReassembler()
    assert list(reassembler.feed(frame[:18])) == []
    assert list(reassembler.feed(frame[18:])) == [frame]

    # Short framed connection/firmware traffic must be consumed separately;
    # otherwise it can be joined to and destroy the following status frame.
    short_frame = bytes([SOF, 0x05, 0x05, EOF])
    assert list(reassembler.feed(short_frame + frame)) == [short_frame, frame]

    # It must also resynchronize after noise/a malformed candidate and retain
    # two concatenated valid responses.
    malformed = bytes([SOF]) + bytes(18)
    emitted = list(reassembler.feed(malformed + frame + frame))
    assert emitted[-2:] == [frame, frame]


def check_long_logging_period_does_not_false_timeout() -> None:
    transport = SimulatorTransport(
        capacity_ah=1.9, soc0=0.8, emit_period_s=0.0, time_accel=1.0)
    dev = EBCA20(transport)
    samples = []
    with dev:
        reason = dev.run(
            lambda measurement, elapsed: None,
            period_s=0.40,
            max_duration_s=0.55,
            on_sample=lambda measurement, elapsed: samples.append(elapsed),
            no_response_timeout_s=0.12,
        )
    assert reason == "duration limit reached"
    assert samples, "controller/logging must still receive its scheduled sample"


def check_reconnect_uses_live_not_disk_state() -> None:
    class FakeDevice:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stop(self):
            pass

    with tempfile.TemporaryDirectory(prefix="ebc_live_state_") as folder:
        csv_path = os.path.join(folder, "run.csv")
        checkpoint_path = os.path.join(folder, "run_checkpoint.json")
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(
                ["t_s", "voltage_v", "current_a", "charge_ah", "mode"])

        stale = {
            "stage": "main",
            "phase_elapsed_s": 10.0,
            "controller_state": {"step_index": 0, "step_elapsed_s": 1.0},
        }
        latest = {
            "stage": "main",
            "phase_elapsed_s": 19.0,
            "controller_state": {"step_index": 2, "step_elapsed_s": 7.0},
        }
        checkpoint = {
            "csv_path": csv_path,
            "runner_state": dict(stale),
            "saved_at_epoch": 1.0,
        }
        with open(checkpoint_path, "w", encoding="utf-8") as fh:
            json.dump(checkpoint, fh)

        calls = {"count": 0, "resumed": None}
        original_run_test = launchTest.run_test

        def fake_run_test(test, cell, dev, logger, resume_state, progress):
            calls["count"] += 1
            if calls["count"] == 1:
                progress(latest)
                raise OSError("simulated drop before disk flush")
            calls["resumed"] = resume_state
            return SimpleNamespace(reason="complete")

        def save_progress(state):
            # Match launchTest.main: update the live object but deliberately
            # leave the disk checkpoint stale.
            checkpoint["runner_state"] = dict(state)
            checkpoint["saved_at_epoch"] = 2.0

        launchTest.run_test = fake_run_test
        try:
            result = launchTest.run_with_reconnect(
                FakeDevice(), FakeDevice, object(), object(), object(),
                checkpoint, checkpoint_path, None, save_progress,
                auto_reconnect=True, backoff_s=0.0, max_attempts=1,
            )
        finally:
            launchTest.run_test = original_run_test

        assert result.reason == "complete"
        resumed = calls["resumed"]
        assert resumed["phase_elapsed_s"] == latest["phase_elapsed_s"]
        assert resumed["controller_state"] == latest["controller_state"]


def main() -> None:
    check_crc_equal_to_eof_is_not_a_delimiter()
    check_long_logging_period_does_not_false_timeout()
    check_reconnect_uses_live_not_disk_state()
    print("TRANSPORT/RECONNECT REGRESSIONS PASSED")
    print("  CRC 0xF8 -> complete fixed-length response")
    print("  logging period > watchdog -> no false timeout")
    print("  reconnect -> latest in-memory controller state")


if __name__ == "__main__":
    main()
