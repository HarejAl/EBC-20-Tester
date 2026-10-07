"""Offline regression: every config/tests/examples/*.json runs end to end.

These files are the templates the README tells users to copy, so they must
always load, resolve their limits, and finish for the documented reason.
"""

from __future__ import annotations

import glob
import os
import tempfile

from _paths import ROOT
from fake_tester import fake_device
from ebc.celllib import CellLibrary
from ebc.experiment import DataLogger
from ebc.runner import TestDef, run_test

cell = CellLibrary(os.path.join(ROOT, "config", "cells")).get("LP604765")
EXAMPLES = os.path.join(ROOT, "config", "tests", "examples")

# Starting SOC per example: low for the charge, high for discharges.
SOC0 = {"charge_to_3v8": 0.15, "discharge_to_3v0": 0.7,
        "custom_profile": 0.6, "discharge_rest_charge": 0.5}


def run_example(path: str):
    test = TestDef.load(path)
    assert not test.cell_id, f"{test.name}: examples must leave cell unset"
    dev = fake_device(limits=cell.safety_limits(),
                      capacity_ah=cell.nominal_capacity_ah,
                      soc0=SOC0[test.name], emit_period_s=0.0,
                      time_accel=400.0)
    with tempfile.TemporaryDirectory() as folder:
        log = DataLogger(os.path.join(folder, "run.csv"))
        try:
            with dev:
                result = run_test(test, cell, dev, log)
        finally:
            log.close()
    return test, result, dev


def main() -> None:
    paths = sorted(glob.glob(os.path.join(EXAMPLES, "*.json")))
    assert paths, "no example tests found"
    for path in paths:
        test, result, dev = run_example(path)
        reason = result.reason
        assert not reason.startswith("safety stop"), (test.name, reason)
        if test.name == "charge_to_3v8":
            assert dev.limits.charge_cv == 3.8, dev.limits.charge_cv
        if test.name == "discharge_to_3v0":
            assert reason.startswith("voltage cutoff reached"), reason
            assert dev.limits.discharge_cutoff_v == 3.0
        print(f"  {test.name:24s} -> {reason} ({result.samples} samples)")
    print("EXAMPLE PROTOCOL CHECKS PASSED")


if __name__ == "__main__":
    main()
