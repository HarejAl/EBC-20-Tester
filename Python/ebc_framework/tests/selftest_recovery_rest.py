"""Offline regression for voltage-cutoff recovery without real hardware."""

from __future__ import annotations

import csv
import os
import tempfile
import time
from dataclasses import replace

from _paths import ROOT
from fake_tester import fake_device
from ebc import DataLogger, EBCA20
from ebc.celllib import CellLibrary
from ebc.runner import TestDef, run_test


cell = CellLibrary(os.path.join(ROOT, "config", "cells")).get("LP604765")


def run_hppc_cutoff(folder: str) -> None:
    path = os.path.join(folder, "hppc_cutoff.csv")
    test = TestDef({
        "name": "hppc_cutoff_recovery_test", "type": "hppc",
        "cell": cell.id, "sample_period_s": 1.0, "initial_rest_s": 0,
        "current_unit": "A", "pulse_current": 1.0,
        "pulse_s": 60, "rest_s": 600,
        # Above simulator OCV so the first pulse terminates immediately.
        "v_min": 4.05, "max_duration_s": 1000,
        "voltage_cutoff_rest_s": 10,
    })
    dev = fake_device(
        limits=cell.safety_limits(), capacity_ah=cell.nominal_capacity_ah,
        soc0=.8, emit_period_s=0.0, time_accel=200)
    logger = DataLogger(path)
    with dev:
        result = run_test(test, cell, dev, logger)
    logger.close()
    assert result.reason == "voltage cutoff reached; recovery rest complete (10 s)"
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) >= 8
    assert any(row["mode"] == "idle" for row in rows[1:])
    assert float(rows[-1]["t_s"]) >= 9.0


def run_hard_overvoltage(folder: str) -> None:
    path = os.path.join(folder, "hard_fault.csv")
    unsafe_cell = replace(cell, hard_max_v=3.0)
    test = TestDef({
        "name": "hard_fault_test", "type": "rest", "cell": cell.id,
        "sample_period_s": 1.0, "initial_rest_s": 0,
        "max_duration_s": 5, "voltage_cutoff_rest_s": 10,
    })
    dev = fake_device(
        limits=unsafe_cell.safety_limits(), capacity_ah=cell.nominal_capacity_ah,
        soc0=.8, emit_period_s=0.0, time_accel=200)
    logger = DataLogger(path)
    with dev:
        result = run_test(test, unsafe_cell, dev, logger)
    logger.close()
    assert result.reason.startswith("safety stop: v_max")
    assert "recovery rest" not in result.reason


def resume_recovery_only(folder: str) -> None:
    path = os.path.join(folder, "resumed_recovery.csv")
    test = TestDef({
        "name": "resume_recovery_test", "type": "hppc", "cell": cell.id,
        "sample_period_s": 1.0, "initial_rest_s": 0,
        "current_unit": "A", "pulse_current": 1.0,
        "pulse_s": 60, "rest_s": 600, "v_min": 2.75,
        "voltage_cutoff_rest_s": 10,
    })
    dev = fake_device(
        limits=cell.safety_limits(), capacity_ah=cell.nominal_capacity_ah,
        soc0=.8, emit_period_s=0.0, time_accel=200)
    logger = DataLogger(path)
    saved = {
        "stage": "voltage_cutoff_recovery_rest", "test_type": "hppc",
        "phase_elapsed_s": 8.0, "csv_elapsed_s": 100.0,
        "saved_at_epoch": time.time(),
        "cutoff_reason": "controller requested stop",
    }
    with dev:
        result = run_test(test, cell, dev, logger, resume_state=saved)
    logger.close()
    assert result.reason == "voltage cutoff reached; recovery rest complete (10 s)"
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows and float(rows[0]["t_s"]) >= 101.0
    assert float(rows[-1]["t_s"]) < 105.0  # only remaining rest, no new HPPC


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ebc_recovery_test_") as folder:
        run_hppc_cutoff(folder)
        run_hard_overvoltage(folder)
        resume_recovery_only(folder)
    print("VOLTAGE-CUTOFF RECOVERY CHECKS PASSED")
    print("  low-voltage cutoff -> STOP + logged recovery rest")
    print("  hard overvoltage -> immediate abort, no recovery continuation")
    print("  interrupted recovery -> resumes remaining rest only")


if __name__ == "__main__":
    main()
