#!/usr/bin/env python3
"""
launchTest.py — one entry point to run any JSON-defined battery test.

Loads a test definition, resolves its cell from the cell library, applies the
cell's voltage/current safety bounds, and executes it on either the real
EBC-A20 or the built-in simulator.

Examples
--------
  # Run the DST validation test on the real device (Linux):
  python launchTest.py config/tests/dst_validation.json --port /dev/ttyUSB0

  # Same on Windows:
  python launchTest.py config/tests/dst_validation.json --port COM3

  # Dry-run on the simulator (no hardware), fast:
  python launchTest.py config/tests/capacity_discharge.json --sim --time-accel 500

  # List available cells:
  python launchTest.py --list-cells

Output: a timestamped CSV (t, V, I, charge, mode) under results/, plus a
sibling <name>_meta.json capturing the exact test definition and cell
parameters used, for reproducibility.
"""

import argparse
import dataclasses
import json
import os
import subprocess
import sys
import time

# allow running from repo root without install
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ebc.celllib import CellLibrary
from ebc.device import EBCA20
from ebc.experiment import DataLogger
from ebc.runner import TestDef, run_test

CELLS_DIR = os.path.join(os.path.dirname(__file__), "config", "cells")
TESTS_DIR = os.path.join(os.path.dirname(__file__), "config", "tests")
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")


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


class PrintingLogger:
    """Wraps a DataLogger to also print each sample to the terminal."""

    def __init__(self, real_logger: DataLogger):
        self._logger = real_logger

    def log(self, m, t_elapsed, **extra) -> None:
        self._logger.log(m, t_elapsed, **extra)
        print(f"t={t_elapsed:7.1f}s  V={m.voltage:6.3f}  "
              f"I={m.signed_current:+6.3f}  Ah={m.charge:6.3f}  [{m.mode}]")

    @property
    def rows(self) -> int:
        return self._logger.rows

    def close(self) -> None:
        self._logger.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run a JSON-defined EBC-A20 test.")
    ap.add_argument("test", nargs="?", help="path to a test JSON file")
    ap.add_argument("--cell",
                    help="cell ID to use; overrides the test JSON's cell")
    ap.add_argument("--port", help="serial port of the real EBC-A20")
    ap.add_argument("--sim", action="store_true",
                    help="use the simulator instead of hardware")
    ap.add_argument("--time-accel", type=float, default=1.0,
                    help="simulator time acceleration (sim only)")
    ap.add_argument("--sim-soc0", type=float, default=0.8,
                    help="simulator initial SOC (sim only)")
    ap.add_argument("--cells-dir", default=CELLS_DIR,
                    help="directory of cell JSON files")
    ap.add_argument("--out", help="output CSV path "
                    "(default: results/<test>_<ts>.csv)")
    ap.add_argument("--live-plot", action="store_true",
                    help="open a plot that updates while the test runs")
    ap.add_argument("--list-cells", action="store_true",
                    help="list cells in the library and exit")
    args = ap.parse_args(argv)

    library = CellLibrary(args.cells_dir)

    if args.list_cells:
        print("Cells in library:")
        for cid in library.ids():
            c = library.get(cid)
            print(f"  {cid:24s} {c.nominal_capacity_ah:5.2f} Ah  "
                  f"{c.min_v:.2f}-{c.max_v:.2f} V  {c.chemistry}")
        return

    if not args.test:
        ap.error("a test JSON path is required (or use --list-cells)")
    if not args.sim and not args.port:
        ap.error("specify --port for hardware, or --sim for the simulator")

    test_path = resolve_test_path(args.test)
    test = TestDef.load(test_path)
    cell_id = args.cell.strip() if args.cell else test.cell_id
    if not cell_id:
        ap.error("the test does not define a cell; select one with --cell CELL_ID")
    try:
        cell = library.get(cell_id)
    except KeyError as exc:
        ap.error(str(exc))

    print(f"Test:  {test.name}  (type={test.type})")
    print(f"Cell:  {cell.id}  {cell.nominal_capacity_ah} Ah  "
          f"window {cell.min_v}-{cell.max_v} V")
    print(f"Bounds: hard stop {cell.hard_min_v}-{cell.hard_max_v} V, "
          f"i_max {max(cell.max_charge_a, cell.max_discharge_a)} A")

    # build device
    if args.sim:
        dev = EBCA20.on_simulator(
            limits=cell.safety_limits(),
            capacity_ah=cell.nominal_capacity_ah,
            soc0=args.sim_soc0, emit_period_s=0.0,
            time_accel=args.time_accel)
        print(f"Backend: SIMULATOR (time_accel={args.time_accel})")
    else:
        dev = EBCA20.on_serial(args.port, limits=cell.safety_limits())
        print(f"Backend: SERIAL {args.port}")

    if args.out:
        out = args.out
    else:
        os.makedirs(RESULTS_DIR, exist_ok=True)
        out = os.path.join(
            RESULTS_DIR, f"{test.name}_{time.strftime('%Y%m%d_%H%M%S')}.csv")
    logger = PrintingLogger(DataLogger(out))

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
    meta_path = os.path.splitext(out)[0] + "_meta.json"
    with open(meta_path, "w") as fh:
        json.dump({
            "test": test.raw,
            "cell": dataclasses.asdict(cell),
            "run": {
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "backend": (f"simulator (time_accel={args.time_accel})"
                           if args.sim else f"serial {args.port}"),
                "selected_cell_id": cell.id,
                "csv_file": os.path.basename(out),
            },
        }, fh, indent=2)

    print(f"Output: {out}")
    print(f"Metadata: {meta_path}")
    print("Running... (Ctrl-C to stop safely)\n")
    try:
        with dev:
            result = run_test(test, cell, dev, logger)
    except KeyboardInterrupt:
        dev.stop()
        result = None
        print("\nInterrupted — device stopped.")
    finally:
        logger.close()

    if result:
        print(f"\nDone: {result.reason}")
        print(f"Samples: {result.samples}  ->  {out}")
        if result.capacity_ah is not None:
            print(f"Measured capacity (SOH): {result.capacity_ah:.3f} Ah "
                  f"({100*result.capacity_ah/cell.nominal_capacity_ah:.1f}% "
                  f"of nominal)")
        if result.cycle_capacities:
            print("Per-cycle discharge capacity (Ah): " +
                  ", ".join(f"{c:.3f}" for c in result.cycle_capacities))


if __name__ == "__main__":
    main()
