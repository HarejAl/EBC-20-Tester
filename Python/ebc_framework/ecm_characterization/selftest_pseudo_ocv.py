#!/usr/bin/env python3
"""Offline regression for pseudo_ocv.py against synthetic data with a known
capacity and a known linear OCV(SOC) formula, so every derived quantity has
a ground truth to check against."""

from __future__ import annotations

import csv
import math
import os
import tempfile

import numpy as np

from pseudo_ocv import (characterize_pseudo_ocv, invert_ocv_to_soc,
                        load_run, measured_capacity_ah, soc_voltage_curve)

TRUE_CAPACITY_AH = 2.4
OCV_A, OCV_B = 3.0, 1.2  # ocv = OCV_A + OCV_B * soc


def write_synthetic_discharge(path: str) -> None:
    """A clean low-rate discharge from soc=1 to soc=0: idle pad, discharge to
    empty (charge_ah rising 0 -> TRUE_CAPACITY_AH), idle pad, matching the
    real device's charge_ah-resets-on-PT_START_DISCHARGE behavior."""
    rows = [["t_s", "voltage_v", "current_a", "signed_current_a", "charge_ah", "mode"]]
    rows.append(["0.000", f"{OCV_A + OCV_B:.4f}", "0.0000", "0.0000", "2.07000", "idle"])
    n_steps = 400
    current_a = TRUE_CAPACITY_AH / (n_steps * 30.0 / 3600.0)  # finishes at n_steps*30s
    for i in range(1, n_steps + 1):
        t = i * 30.0
        charge_ah = TRUE_CAPACITY_AH * i / n_steps
        soc = 1.0 - charge_ah / TRUE_CAPACITY_AH
        voltage = OCV_A + OCV_B * soc
        rows.append([f"{t:.3f}", f"{voltage:.4f}", f"{current_a:.4f}",
                    f"{-current_a:.4f}", f"{charge_ah:.5f}", "discharge"])
    tail_t = n_steps * 30.0
    for i in range(1, 4):
        rows.append([f"{tail_t + i*30:.3f}", f"{OCV_A:.4f}", "0.0000", "0.0000",
                    f"{TRUE_CAPACITY_AH:.5f}", "idle"])
    with open(path, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)


def check_measured_capacity() -> None:
    with tempfile.TemporaryDirectory(prefix="ebc_pseudo_ocv_test_") as folder:
        csv_path = os.path.join(folder, "discharge.csv")
        write_synthetic_discharge(csv_path)
        run = load_run(csv_path)
        capacity = measured_capacity_ah(run, "discharge")
        assert abs(capacity - TRUE_CAPACITY_AH) < 1e-6, (
            f"expected capacity {TRUE_CAPACITY_AH}, got {capacity}")


def check_curve_inversion_recovers_soc() -> None:
    with tempfile.TemporaryDirectory(prefix="ebc_pseudo_ocv_test_") as folder:
        csv_path = os.path.join(folder, "discharge.csv")
        write_synthetic_discharge(csv_path)
        run = load_run(csv_path)
        capacity = measured_capacity_ah(run, "discharge")
        soc_curve, ocv_curve = soc_voltage_curve(run, capacity, "discharge")
        assert np.all(np.diff(soc_curve) >= 0), "soc_curve must be sorted ascending"
        assert np.all(np.diff(ocv_curve) >= -1e-12), "ocv_curve must be non-decreasing"
        for true_soc in (0.1, 0.35, 0.6, 0.85):
            true_voltage = OCV_A + OCV_B * true_soc
            recovered = invert_ocv_to_soc(true_voltage, soc_curve, ocv_curve)
            assert math.isfinite(recovered), f"expected a finite SOC at soc={true_soc}"
            assert abs(recovered - true_soc) < 0.01, (
                f"expected soc~={true_soc}, got {recovered}")


def check_out_of_range_voltage_rejected() -> None:
    with tempfile.TemporaryDirectory(prefix="ebc_pseudo_ocv_test_") as folder:
        csv_path = os.path.join(folder, "discharge.csv")
        write_synthetic_discharge(csv_path)
        run = load_run(csv_path)
        capacity = measured_capacity_ah(run, "discharge")
        soc_curve, ocv_curve = soc_voltage_curve(run, capacity, "discharge")
        too_high = float(ocv_curve[-1]) + 1.0
        too_low = float(ocv_curve[0]) - 1.0
        assert math.isnan(invert_ocv_to_soc(too_high, soc_curve, ocv_curve))
        assert math.isnan(invert_ocv_to_soc(too_low, soc_curve, ocv_curve))


def check_flat_plateau_rejected() -> None:
    # A voltage plateau (near-zero local dV/dSOC) must be rejected even
    # though it's technically within [min, max] and inversion is possible --
    # the slope there is too weak to trust the result.
    soc_curve = np.array([0.0, 0.2, 0.4, 0.6, 0.8, 1.0])
    ocv_curve = np.array([3.0, 3.3, 3.30001, 3.30002, 3.6, 3.9])
    recovered = invert_ocv_to_soc(3.300015, soc_curve, ocv_curve,
                                  min_slope_v_per_soc=0.05)
    assert math.isnan(recovered), (
        "a near-flat plateau region must be rejected as ill-conditioned, "
        f"got {recovered}")
    # A well-sloped region must still be accepted with the same threshold.
    recovered_good = invert_ocv_to_soc(3.75, soc_curve, ocv_curve,
                                       min_slope_v_per_soc=0.05)
    assert math.isfinite(recovered_good)


def check_end_to_end_json_output() -> None:
    with tempfile.TemporaryDirectory(prefix="ebc_pseudo_ocv_test_") as folder:
        csv_path = os.path.join(folder, "discharge.csv")
        write_synthetic_discharge(csv_path)
        output_dir = os.path.join(folder, "out")
        result = characterize_pseudo_ocv(csv_path, output_dir)
        assert abs(result["capacity_ah"] - TRUE_CAPACITY_AH) < 1e-6
        assert result["method"] == "discharge_only"
        assert os.path.isfile(os.path.join(output_dir, "pseudo_ocv.json"))
        assert os.path.isfile(os.path.join(output_dir, "pseudo_ocv_lookup.csv"))
        assert os.path.isfile(os.path.join(output_dir, "pseudo_ocv_curve.svg"))


def main() -> None:
    check_measured_capacity()
    check_curve_inversion_recovers_soc()
    check_out_of_range_voltage_rejected()
    check_flat_plateau_rejected()
    check_end_to_end_json_output()
    print("PSEUDO-OCV CHECKS PASSED")
    print("  measured capacity recovered from synthetic charge_ah counter")
    print("  curve inversion recovers known synthetic SOC within tolerance")
    print("  out-of-range voltage -> rejected (NaN), not clamped")
    print("  near-flat plateau -> rejected (NaN), not trusted")
    print("  end-to-end json/csv/svg outputs written")


if __name__ == "__main__":
    main()
