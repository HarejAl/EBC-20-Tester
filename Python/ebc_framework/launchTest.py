#!/usr/bin/env python3
"""
launchTest.py — one entry point to run any JSON-defined battery test.

Loads a test definition, resolves its cell from the cell library, applies the
cell's voltage/current safety bounds, and executes it on the EBC-A20.

Examples
--------
  # Windows:
  python launchTest.py config/tests/hppc.json --cell LP604765 --port COM5

  # Linux:
  python launchTest.py hppc --cell LP604765 --port /dev/ttyUSB0

  # Watch the run without writing anything to disk:
  python launchTest.py connection_test --cell LP604765 --port COM5 --no-save

  # List available cells:
  python launchTest.py --list-cells

Output (unless --no-save): a timestamped CSV (t, V, I, charge, mode) under
results/<protocol_name>/, plus a sibling <name>_meta.json capturing the exact
test definition and cell parameters used, and a _checkpoint.json for resume.
"""

import argparse
import copy
import csv
import dataclasses
import json
import os
import re
import subprocess
import sys
import time

# allow running from repo root without install
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ebc.celllib import CellLibrary
from ebc.device import EBCA20
from ebc.experiment import DataLogger
from ebc.protocol import EBC_MAX_CHARGE_A, EBC_MAX_DISCHARGE_A
from ebc.runner import TestDef, run_test

CELLS_DIR = os.path.join(os.path.dirname(__file__), "config", "cells")
TESTS_DIR = os.path.join(os.path.dirname(__file__), "config", "tests")
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")
CHECKPOINT_INTERVAL_S = 10.0


def resolve_test_path(value: str) -> str:
    """Accept a path, a config/tests file name, or a file stem."""
    candidates = [value]
    if not value.lower().endswith(".json"):
        candidates.append(value + ".json")
    candidates += [os.path.join(TESTS_DIR, candidate)
                   for candidate in list(candidates)]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return value


def resolve_checkpoint_path(value: str) -> str:
    """Accept a checkpoint path or the CSV/meta file belonging to it."""
    path = os.path.abspath(value)
    stem, ext = os.path.splitext(path)
    if ext.lower() == ".csv":
        return stem + "_checkpoint.json"
    if path.endswith("_meta.json"):
        return path[:-10] + "_checkpoint.json"
    return path


def result_folder_name(test_name: str) -> str:
    """Return a Windows-safe folder name derived from the protocol name."""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(test_name))
    safe = safe.strip(" .")
    return safe or "unnamed_test"


def write_json_atomic(path: str, data: dict) -> bool:
    """Write a checkpoint without allowing a sync lock to stop the test.

    OneDrive and antivirus scanners can briefly lock an existing file on
    Windows, causing ``os.replace`` to fail with WinError 5/32. Retry the
    atomic replace first, then fall back to rewriting the destination in
    place. Checkpoints are recovery aids, so even a persistent write failure
    is reported as a warning instead of aborting an active battery test.
    """
    payload = json.dumps(data, indent=2)
    temporary = f"{path}.{os.getpid()}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())

        replace_error = None
        for delay_s in (0.0, 0.05, 0.10, 0.20, 0.40):
            if delay_s:
                time.sleep(delay_s)
            try:
                os.replace(temporary, path)
                return True
            except PermissionError as exc:
                replace_error = exc

        # Replacing an existing OneDrive reparse point may remain forbidden
        # even though opening that same file for writing is allowed.
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            return True
        except OSError as exc:
            print(f"Warning: checkpoint could not be updated: {exc} "
                  f"(atomic replace also failed: {replace_error})",
                  file=sys.stderr)
            return False
    except OSError as exc:
        print(f"Warning: checkpoint could not be written: {exc}",
              file=sys.stderr)
        return False
    finally:
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass


def last_csv_time(path: str) -> float:
    """Return the last valid logged timestamp, or zero for an empty CSV."""
    last = 0.0
    if not path or not os.path.isfile(path):
        return last
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                last = float(row["t_s"])
            except (KeyError, TypeError, ValueError):
                continue
    return last


# A run segment that survived this long counts as a recovered link, so a
# later unrelated drop restarts its backoff from the beginning.
HEALTHY_SEGMENT_S = 180.0


def run_with_reconnect(dev, build_device, test, cell, logger, checkpoint,
                       checkpoint_path, resume_state, save_progress, *,
                       auto_reconnect: bool, backoff_s: float,
                       max_attempts: int):
    """Run the test; on a dropped hardware connection, reconnect and resume.

    A dropped USB-serial connection (cable hiccup, driver reset, Windows
    power management) surfaces as an OSError (pyserial's SerialException is
    a subclass of it) from somewhere inside the polling loop. Rather than
    letting that crash the whole script -- requiring the run to be noticed
    and manually restarted with --resume -- reopen the port and resume from
    the checkpoint the crashed attempt left behind, exactly as --resume
    would, just triggered automatically instead of by hand. KeyboardInterrupt
    is deliberately not treated as a connection drop.
    """
    attempt = 0
    while True:
        segment_started = time.monotonic()
        try:
            with dev:
                try:
                    return run_test(test, cell, dev, logger,
                                    resume_state=resume_state,
                                    progress=save_progress)
                except KeyboardInterrupt:
                    dev.stop()
                    if checkpoint_path:
                        print("\nInterrupted -- device stopped. Resume later "
                              f"with {checkpoint_path}")
                    else:
                        print("\nInterrupted -- device stopped. Data was not "
                              "being saved, so this run cannot be resumed.")
                    return None
        except OSError as exc:
            if not auto_reconnect:
                raise
            # Exponential backoff is meant to stop hammering a link that is
            # persistently down -- not to punish a healthy run for a drop
            # that happened hours ago. Without this reset the counter only
            # ever grows, so by the sixth drop every later one waits the full
            # 120 s cap even after hours of clean operation in between.
            # Confirmed on hardware 2026-09-25: four drops in twenty minutes
            # escalated 5s -> 10s -> 20s -> 40s with no recovery.
            # Note this makes max_attempts count *consecutive* failures,
            # which is the more useful reading of "give up".
            if time.monotonic() - segment_started >= HEALTHY_SEGMENT_S:
                attempt = 0
            attempt += 1
            if max_attempts and attempt > max_attempts:
                resume_hint = (f"Resume later with {checkpoint_path}"
                               if checkpoint_path else "Data was not being "
                               "saved, so this run cannot be resumed.")
                print(f"\nConnection lost ({exc}); giving up after "
                      f"{attempt - 1} reconnect attempt(s). {resume_hint}",
                      file=sys.stderr)
                raise
            wait_s = min(backoff_s * (2 ** (attempt - 1)), 120.0)
            print(f"\nConnection lost ({exc}). Reconnect attempt {attempt} "
                  f"in {wait_s:g}s...", file=sys.stderr)
            time.sleep(wait_s)
            # The live snapshot is newer than the throttled disk checkpoint.
            # Reloading the file here could repeat already-applied profile
            # time (or extend a pulse) by up to one checkpoint interval.
            resume_state = copy.deepcopy(checkpoint.get("runner_state", {}))
            resume_state["csv_elapsed_s"] = max(
                float(resume_state.get("csv_elapsed_s", 0.0)),
                last_csv_time(checkpoint["csv_path"]))
            resume_state["saved_at_epoch"] = checkpoint.get(
                "saved_at_epoch", time.time())
            dev = build_device()
            print(f"Reconnected. Resuming: "
                  f"{resume_state.get('stage', 'unknown stage')}")


class NullLogger:
    """Stands in for DataLogger when the user chose not to save data."""

    def __init__(self) -> None:
        self.rows = 0

    def log(self, m, t_elapsed, **extra) -> None:
        self.rows += 1

    def close(self) -> None:
        pass


class PrintingLogger:
    """Wraps a DataLogger to also print each sample to the terminal."""

    def __init__(self, real_logger: DataLogger):
        self._logger = real_logger

    def log(self, m, t_elapsed, **extra) -> None:
        self._logger.log(m, t_elapsed, **extra)
        clock_time = time.strftime("%H:%M:%S")
        print(f"{clock_time}  t_exp = {t_elapsed:7.1f}s  "
              f"V = {m.voltage:6.3f} V  "
              f"I = {m.signed_current:+6.3f} A  "
              f"Q = {m.charge:6.3f} Ah  [{m.mode}]")

    @property
    def rows(self) -> int:
        return self._logger.rows

    def close(self) -> None:
        self._logger.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run a JSON-defined EBC-A20 test.")
    ap.add_argument("test", nargs="?", help="path to a test JSON file")
    ap.add_argument("--resume", metavar="CHECKPOINT_OR_CSV",
                    help="continue an interrupted run from its checkpoint")
    ap.add_argument("--cell",
                    help="cell ID to use; overrides the test JSON's cell")
    ap.add_argument("--port", help="serial port of the EBC-A20 "
                    "(e.g. COM5 or /dev/ttyUSB0)")
    ap.add_argument("--no-save", dest="save", action="store_false",
                    default=True,
                    help="only print samples to the terminal; write no CSV, "
                    "metadata or checkpoint (the run cannot be resumed)")
    ap.add_argument("--cells-dir", default=CELLS_DIR,
                    help="directory of cell JSON files")
    ap.add_argument("--out", help="output CSV path "
                    "(default: results/<protocol>/<protocol>_<ts>.csv)")
    ap.add_argument("--live-plot", action="store_true",
                    help="open a plot that updates while the test runs")
    ap.add_argument("--list-cells", action="store_true",
                    help="list cells in the library and exit")
    ap.add_argument("--no-auto-reconnect", dest="auto_reconnect",
                    action="store_false", default=True,
                    help="disable automatic reconnect/resume after a dropped "
                    "hardware connection (default: on for hardware runs)")
    ap.add_argument("--reconnect-backoff-s", type=float, default=5.0,
                    help="initial delay before the first reconnect attempt; "
                    "doubles each attempt up to 120s (default: 5)")
    ap.add_argument("--reconnect-max-attempts", type=int, default=0,
                    help="give up after this many consecutive reconnect "
                    "attempts (0 = retry indefinitely, default)")
    args = ap.parse_args(argv)

    library = CellLibrary(args.cells_dir)

    if args.list_cells:
        print("Cells in library:")
        for cid in library.ids():
            c = library.get(cid)
            print(f"  {cid:24s} {c.nominal_capacity_ah:5.2f} Ah  "
                  f"{c.min_v:.2f}-{c.max_v:.2f} V  {c.chemistry}")
        return

    if not args.test and not args.resume:
        ap.error("a test JSON path or --resume file is required")
    if args.test and args.resume:
        ap.error("specify either a test or --resume, not both")
    if not args.port:
        ap.error("specify --port, the tester's serial port (e.g. COM5)")
    if not args.save:
        if args.resume:
            ap.error("--no-save cannot be combined with --resume")
        if args.out:
            ap.error("--no-save cannot be combined with --out")
        if args.live_plot:
            ap.error("--live-plot reads the saved CSV; drop --no-save to use it")

    checkpoint = None
    checkpoint_path = None
    resume_state = None
    if args.resume:
        checkpoint_path = resolve_checkpoint_path(args.resume)
        if not os.path.isfile(checkpoint_path):
            ap.error(f"checkpoint not found: {checkpoint_path}")
        with open(checkpoint_path) as fh:
            checkpoint = json.load(fh)
        if checkpoint.get("complete"):
            ap.error("this checkpoint is already marked complete")
        test_path = checkpoint["test_path"]
        resume_state = dict(checkpoint.get("runner_state", {}))
        # Older/pre-phase checkpoints may not yet contain the samples written
        # during the initial rest. Recover the append offset from the CSV.
        resume_state["csv_elapsed_s"] = max(
            float(resume_state.get("csv_elapsed_s", 0.0)),
            last_csv_time(checkpoint["csv_path"]))
        resume_state["saved_at_epoch"] = checkpoint.get(
            "saved_at_epoch", time.time())
        cell_id = checkpoint["cell_id"]
        if args.cell and args.cell.strip() != cell_id:
            ap.error("--cell cannot change the cell of a resumed run")
    else:
        test_path = resolve_test_path(args.test)
        cell_id = args.cell.strip() if args.cell else None

    test = TestDef.load(test_path)
    cell_id = cell_id or test.cell_id
    if not cell_id:
        ap.error("the test does not define a cell; select one with --cell CELL_ID")
    try:
        cell = library.get(cell_id)
    except KeyError as exc:
        ap.error(str(exc))

    print(f"Test:  {test.name}  (type={test.type})")
    print(f"Cell:  {cell.id}  {cell.nominal_capacity_ah} Ah  "
          f"window {cell.min_v}-{cell.max_v} V")
    print(f"Voltage hard stops: {cell.hard_min_v}-{cell.hard_max_v} V")
    print("Effective current limits: "
          f"charge <= {min(cell.max_charge_a, EBC_MAX_CHARGE_A):.3f} A, "
          f"discharge <= "
          f"{min(cell.max_discharge_a, EBC_MAX_DISCHARGE_A):.3f} A")

    # build device
    def build_device():
        return EBCA20.on_serial(args.port, limits=cell.safety_limits())

    try:
        dev = build_device()
    except OSError as exc:
        ap.error(f"could not open serial port {args.port}: {exc}. Check that "
                 "the tester is plugged in and powered, and the port number "
                 "(Windows: Device Manager > Ports (COM & LPT)).")
    print(f"Port:  {args.port}")

    if not args.save:
        out = ""
    elif checkpoint is not None:
        out = checkpoint["csv_path"]
        if args.out and os.path.abspath(args.out) != os.path.abspath(out):
            ap.error("--out cannot change the CSV of a resumed run")
    elif args.out:
        out = args.out
    else:
        protocol_results = os.path.join(
            RESULTS_DIR, result_folder_name(test.name))
        os.makedirs(protocol_results, exist_ok=True)
        out = os.path.join(
            protocol_results,
            f"{test.name}_{time.strftime('%Y%m%d_%H%M%S')}.csv")
    # Anything beyond a few sample periods is a dropout hole, not flowing
    # current, so cumulative_ah must not integrate across it. Scaled off the
    # test's own cadence because sample_period_s ranges from 1 s to 30 s.
    logger = PrintingLogger(DataLogger(
        out, append=checkpoint is not None,
        max_gap_s=max(3.0 * test.sample_period_s, 5.0))
        if args.save else NullLogger())

    if args.live_plot:
        plot_script = os.path.join(os.path.dirname(__file__),
                                   "examples", "plot_log.py")
        try:
            subprocess.Popen([sys.executable, plot_script, out, "--watch"])
            print("Live plot: opened (close its window to stop plotting)")
        except OSError as exc:
            print(f"Warning: could not open live plot: {exc}", file=sys.stderr)

    # Write metadata now, before the run starts -- not after it finishes.
    # An early-terminated run (killed terminal, power loss, crash) still
    # leaves you knowing exactly what config produced the partial CSV.
    meta_path = os.path.splitext(out)[0] + "_meta.json" if args.save else None
    if not args.save:
        # Same in-memory checkpoint as a saved run, so an automatic reconnect
        # still resumes correctly -- it is just never written to disk.
        checkpoint = {
            "version": 1, "complete": False, "csv_path": "",
            "runner_state": {"stage": "not_started", "csv_elapsed_s": 0.0},
        }
    elif checkpoint is None:
        with open(meta_path, "w") as fh:
            json.dump({
            "test": test.raw,
            "cell": dataclasses.asdict(cell),
            "run": {
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "backend": f"serial {args.port}",
                "selected_cell_id": cell.id,
                "csv_file": os.path.basename(out),
            },
            }, fh, indent=2)

        checkpoint_path = os.path.splitext(out)[0] + "_checkpoint.json"
        checkpoint = {
            "version": 1,
            "complete": False,
            "test_path": os.path.abspath(test_path),
            "cell_id": cell.id,
            "csv_path": os.path.abspath(out),
            "runner_state": {"stage": "not_started", "csv_elapsed_s": 0.0},
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        write_json_atomic(checkpoint_path, checkpoint)

    last_checkpoint_write = {"wall": 0.0, "stage": None}

    def save_progress(runner_state: dict) -> None:
        now = time.time()
        stage = runner_state.get("stage")
        # Always retain the newest state for an in-process reconnect. Throttle
        # only the comparatively expensive recovery-file write.
        checkpoint["runner_state"] = copy.deepcopy(runner_state)
        checkpoint["saved_at_epoch"] = now
        checkpoint["complete"] = False
        if not args.save:
            return
        if (stage == last_checkpoint_write["stage"] and
                now - last_checkpoint_write["wall"] < CHECKPOINT_INTERVAL_S):
            return
        write_json_atomic(checkpoint_path, checkpoint)
        last_checkpoint_write.update({"wall": now, "stage": stage})

    if args.save:
        print(f"Output: {out}")
        print(f"Metadata: {meta_path}")
        print(f"Checkpoint: {checkpoint_path}")
    else:
        print("Output: NOT SAVED (--no-save) -- samples are only printed")
    if resume_state is not None:
        print(f"Resuming: {resume_state.get('stage', 'unknown stage')}")
    print("Running... (Ctrl-C to stop safely)\n")
    try:
        result = run_with_reconnect(
            dev, build_device, test, cell, logger, checkpoint,
            checkpoint_path, resume_state, save_progress,
            auto_reconnect=args.auto_reconnect,
            backoff_s=args.reconnect_backoff_s,
            max_attempts=args.reconnect_max_attempts)
    finally:
        logger.close()

    if result:
        print(f"\nDone: {result.reason}")
        if args.save:
            checkpoint["complete"] = True
            checkpoint["completed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            checkpoint["result_reason"] = result.reason
            write_json_atomic(checkpoint_path, checkpoint)
            print(f"Samples: {result.samples}  ->  {out}")
        else:
            print(f"Samples: {result.samples} (not saved)")
        if result.capacity_ah is not None:
            print(f"Measured capacity (SOH): {result.capacity_ah:.3f} Ah "
                  f"({100*result.capacity_ah/cell.nominal_capacity_ah:.1f}% "
                  f"of nominal)")
        if result.cycle_capacities:
            print("Per-cycle discharge capacity (Ah): " +
                  ", ".join(f"{c:.3f}" for c in result.cycle_capacities))

    # Batch launchers can inspect the reason and stop a sequence after an
    # interruption or safety event. Existing command-line use is unchanged.
    return result


if __name__ == "__main__":
    main()
