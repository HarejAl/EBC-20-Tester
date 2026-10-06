#!/usr/bin/env python3
"""Generate synthetic EBC-format HPPC data and validate the offline pipeline."""

from __future__ import annotations

import csv
import json
import math
import os
import tempfile

import numpy as np

from characterize_hppc import CharacterizationConfig, characterize, fit_relaxation


def write_synthetic(path: str) -> None:
    # Includes both directions, jittered intervals, a missing sample, a
    # duplicate timestamp, one resume time reset, and a 660-second bad pulse.
    schedule = [
        (60, 0.0), (60, 2.0), (180, 0.0),
        (60, -1.5), (180, 0.0), (60, 2.0), (180, 0.0),
        (60, -1.5), (180, 0.0), (660, 2.0), (180, 0.0),
    ]
    capacity_ah, soc, v_rc = 20.0, .9, 0.0
    r0, r1, tau = .025, .018, 25.0
    physical_t = logged_t = 0.0
    rows = []
    sample_number = 0
    reset_done = False
    for duration, current_ecm in schedule:
        phase_end = physical_t + duration
        while physical_t < phase_end - 1e-9:
            dt = min((.8, 1.0, 1.2)[sample_number % 3], phase_end - physical_t)
            physical_t += dt
            logged_t += dt
            soc -= current_ecm * dt / 3600 / capacity_ah
            v_rc += (-v_rc + current_ecm * r1) * dt / tau
            ocv = 3.25 + .95 * soc
            voltage = ocv - current_ecm * r0 - v_rc
            signed_ebc = -current_ecm
            mode = ("discharge" if current_ecm > 0 else
                    "charge" if current_ecm < 0 else "idle")
            sample_number += 1
            if sample_number == 450:  # create a missing-sample gap
                continue
            if not reset_done and physical_t > 740:
                logged_t = 0.0
                reset_done = True
            row = [f"{logged_t:.3f}", f"{voltage:.6f}",
                   f"{abs(current_ecm):.5f}", f"{signed_ebc:.5f}",
                   "0.0", mode]
            rows.append(row)
            if sample_number == 250:
                rows.append(list(row))  # later duplicate should replace earlier
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["t_s", "voltage_v", "current_a",
                         "signed_current_a", "charge_ah", "mode"])
        writer.writerows(rows)


def write_synthetic_two_branch(path: str) -> None:
    """Two known, well-separated time constants (fast ~15s, slow ~200s) plus
    small measurement noise, repeated over several pulses so the SOC-binned
    lookup table's medians average the noise down."""
    rng = np.random.default_rng(7)
    capacity_ah = 20.0
    soc = .9
    r0, r_fast, tau_fast, r_slow, tau_slow = .025, .012, 15.0, .020, 200.0
    v_fast = v_slow = 0.0
    physical_t = logged_t = 0.0
    rows = []
    pulse_s, rest_s, pulse_a = 60.0, 700.0, 2.0
    schedule = [(pulse_s, pulse_a), (rest_s, 0.0)] * 4
    for duration, current_ecm in schedule:
        phase_end = physical_t + duration
        while physical_t < phase_end - 1e-9:
            dt = min(1.0, phase_end - physical_t)
            physical_t += dt; logged_t += dt
            soc -= current_ecm * dt / 3600 / capacity_ah
            v_fast += (-v_fast + current_ecm * r_fast) * dt / tau_fast
            v_slow += (-v_slow + current_ecm * r_slow) * dt / tau_slow
            ocv = 3.25 + .95 * soc
            voltage = ocv - current_ecm * r0 - v_fast - v_slow + rng.normal(0, .0008)
            signed_ebc = -current_ecm
            mode = ("discharge" if current_ecm > 0 else
                    "charge" if current_ecm < 0 else "idle")
            rows.append([f"{logged_t:.3f}", f"{voltage:.6f}",
                        f"{abs(current_ecm):.5f}", f"{signed_ebc:.5f}",
                        "0.0", mode])
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["t_s", "voltage_v", "current_a",
                         "signed_current_a", "charge_ah", "mode"])
        writer.writerows(rows)
    return r_fast, tau_fast, r_slow, tau_slow


def check_single_branch(folder: str) -> None:
    source = os.path.join(folder, "synthetic_hppc.csv")
    output = os.path.join(folder, "output")
    write_synthetic(source)
    with open(os.path.join(folder, "synthetic_hppc_checkpoint.json"),
              "w", encoding="utf-8") as fh:
        json.dump({"complete": True, "result_reason":
                   "voltage cutoff reached; recovery rest complete (600 s)"}, fh)
    result = characterize(CharacterizationConfig(
        input_csv=source, output_dir=output, capacity_ah=20.0,
        initial_soc=.9, expected_pulse_s=60,
        current_threshold_a=.2, num_rc_branches=1,
        relaxation_window_s=(1, 150), soc_bin_width=.05))
    quality = result["input_quality"]
    assert quality["duplicate_rows_replaced"] >= 1
    assert quality["resumed_timestamp_segments"] >= 1
    assert result["pulse_quality_counts"]["rejected"] >= 1
    directions = {row["direction"] for row in result["lookup"]}
    assert directions == {"charge", "discharge"}, directions
    required = {
        "pulse_characterization.csv", "ecm_lookup_table.csv",
        "ecm_parameters.json", "ocv_vs_soc.svg",
        "ecm_parameters_vs_soc.svg", "detected_pulses.svg"}
    assert required <= set(os.listdir(output))
    with open(os.path.join(output, "pulse_characterization.csv"),
              newline="", encoding="utf-8") as fh:
        pulse_rows = list(csv.DictReader(fh))
    long_rows = [r for r in pulse_rows if float(r["duration_s"]) > 600]
    assert len(long_rows) == 1
    assert long_rows[0]["quality_status"] == "rejected"
    assert "pulse_duration_abnormal" in long_rows[0]["quality_reasons"]
    assert "terminal_pulse_interrupted_by_voltage_cutoff" in \
        long_rows[0]["quality_reasons"]
    with open(os.path.join(output, "ecm_parameters.json"),
              encoding="utf-8") as fh:
        json.load(fh)  # strict JSON export is readable
    print(f"  single-branch pulse quality: {result['pulse_quality_counts']}")
    print(f"  single-branch timestamp quality: {quality}")


def check_two_branch(folder: str) -> None:
    source = os.path.join(folder, "synthetic_two_branch.csv")
    output = os.path.join(folder, "output_two_branch")
    r_fast, tau_fast, r_slow, tau_slow = write_synthetic_two_branch(source)
    result = characterize(CharacterizationConfig(
        input_csv=source, output_dir=output, capacity_ah=20.0,
        initial_soc=.9, expected_pulse_s=60,
        current_threshold_a=.2, num_rc_branches=2,
        relaxation_window_s=(1, 650), soc_bin_width=.1))
    lookup = [row for row in result["lookup"] if row["direction"] == "discharge"]
    assert lookup, "no accepted discharge pulses in the two-branch lookup"
    branches = lookup[0]["rc_branches"]
    assert len(branches) == 2
    fitted_fast, fitted_slow = branches[0], branches[1]
    assert fitted_fast["tau_s"] < fitted_slow["tau_s"]
    assert abs(fitted_fast["tau_s"]-tau_fast)/tau_fast < .5, fitted_fast
    assert abs(fitted_slow["tau_s"]-tau_slow)/tau_slow < .5, fitted_slow
    assert abs(fitted_fast["r_ohm"]-r_fast)/r_fast < .5, fitted_fast
    assert abs(fitted_slow["r_ohm"]-r_slow)/r_slow < .5, fitted_slow
    print(f"  two-branch fitted: fast={fitted_fast}, slow={fitted_slow}")
    print(f"  two-branch true:   fast=(r={r_fast}, tau={tau_fast}), "
          f"slow=(r={r_slow}, tau={tau_slow})")


def write_synthetic_pseudo_ocv_json(path: str, capacity_ah: float) -> None:
    """A noise-free reference curve matching the same ocv = 3.25 + .95*soc
    formula the synthetic HPPC generators use, so wiring can be checked
    against a curve with a known-correct answer."""
    soc = np.linspace(0.0, 1.0, 2001)
    ocv_v = 3.25 + .95 * soc
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"method": "discharge_only", "capacity_ah": capacity_ah,
                   "soc": soc.tolist(), "ocv_v": ocv_v.tolist()}, fh)


def check_pseudo_ocv_anchoring(folder: str) -> None:
    """End-to-end: a deliberately-wrong initial capacity_ah must be
    overridden by pseudo_ocv.json's measured value, pulses must get
    OCV-anchored soc_parameter/fixed-OCV fits instead of coulomb-only, and
    the fitted R/tau must stay as accurate as the coulomb-only baseline
    (check_two_branch) since the supplied curve is correct."""
    source = os.path.join(folder, "synthetic_pseudo_ocv_anchor.csv")
    pseudo_ocv_json = os.path.join(folder, "pseudo_ocv.json")
    output = os.path.join(folder, "output_pseudo_ocv_anchor")
    r_fast, tau_fast, r_slow, tau_slow = write_synthetic_two_branch(source)
    true_capacity_ah = 20.0
    write_synthetic_pseudo_ocv_json(pseudo_ocv_json, true_capacity_ah)

    wrong_capacity_ah = 17.0  # simulates a wrong datasheet figure
    result = characterize(CharacterizationConfig(
        input_csv=source, output_dir=output, capacity_ah=wrong_capacity_ah,
        initial_soc=.9, expected_pulse_s=60,
        current_threshold_a=.2, num_rc_branches=2,
        relaxation_window_s=(1, 650), soc_bin_width=.1,
        pseudo_ocv_json=pseudo_ocv_json))

    assert result["configuration"]["capacity_ah"] == true_capacity_ah, (
        "pseudo_ocv_json must override a wrong initial capacity_ah")
    assert result["pseudo_ocv_source"]["capacity_ah"] == true_capacity_ah

    lookup = [row for row in result["lookup"] if row["direction"] == "discharge"]
    assert lookup, "no accepted discharge pulses in the anchored lookup"
    fitted_fast, fitted_slow = lookup[0]["rc_branches"]
    assert abs(fitted_fast["tau_s"] - tau_fast) / tau_fast < .5, fitted_fast
    assert abs(fitted_slow["tau_s"] - tau_slow) / tau_slow < .5, fitted_slow
    assert abs(fitted_fast["r_ohm"] - r_fast) / r_fast < .5, fitted_fast
    assert abs(fitted_slow["r_ohm"] - r_slow) / r_slow < .5, fitted_slow

    with open(os.path.join(output, "pulse_characterization.csv"),
              newline="", encoding="utf-8") as fh:
        pulse_rows = list(csv.DictReader(fh))
    accepted = [r for r in pulse_rows if r["quality_status"] == "accepted"]
    assert any(r["soc_parameter_source"] == "ocv" for r in accepted), (
        "expected at least one pulse to be OCV-anchored with a correct curve")
    assert any(r["ocv_anchor_adopted"] == "True" for r in accepted), (
        "expected at least one fixed-OCV fit to be adopted with a correct curve")
    print(f"  pseudo-OCV anchored fitted: fast={fitted_fast}, slow={fitted_slow}")


def check_fixed_ocv_mismatch_safeguard() -> None:
    """Direct regression on fit_relaxation's fixed_ocv adoption decision
    (the exact arithmetic analyze_pulse uses): a correct anchor must produce
    a small mismatch/rmse-increase (adoptable), a deliberately-wrong anchor
    (~50 mV off, well past the 10 mV default tolerance) must not be -- the
    fixed basis can't represent a constant residual, so a bad anchor pushes
    tau back toward the search-grid boundary instead of just biasing the
    amplitude, exactly the degeneracy this safeguard exists to catch."""
    t = np.arange(0, 300, 1.0)
    true_ocv, true_amplitude, true_tau = 3.5, -0.05, 40.0
    rng = np.random.default_rng(3)
    voltage = (true_ocv + true_amplitude * np.exp(-t / true_tau)
              + rng.normal(0, 0.0005, len(t)))

    free = fit_relaxation(t, voltage, num_branches=1)
    good = fit_relaxation(t, voltage, num_branches=1, fixed_ocv=true_ocv)
    bad = fit_relaxation(t, voltage, num_branches=1, fixed_ocv=true_ocv + 0.05)

    max_mismatch_v, max_rmse_increase_frac = 0.010, 0.5

    good_mismatch = abs(free["ocv"] - true_ocv)
    good_rmse_increase = (good["rmse"] - free["rmse"]) / free["rmse"]
    assert good_mismatch <= max_mismatch_v, good_mismatch
    assert good_rmse_increase <= max_rmse_increase_frac, good_rmse_increase

    bad_mismatch = abs(free["ocv"] - (true_ocv + 0.05))
    bad_rmse_increase = (bad["rmse"] - free["rmse"]) / free["rmse"]
    assert bad_mismatch > max_mismatch_v or bad_rmse_increase > max_rmse_increase_frac, (
        "a ~50 mV wrong anchor must fail at least one safeguard check, else "
        "analyze_pulse would silently adopt a biased fit", bad_mismatch, bad_rmse_increase)
    # The bad anchor's own tau must have visibly degraded relative to truth --
    # confirming this is the same tau-toward-boundary degeneracy, not just a
    # harmless amplitude shift the safeguard is being overly cautious about.
    assert abs(bad["taus"][0] - true_tau) / true_tau > abs(good["taus"][0] - true_tau) / true_tau
    print(f"  fixed-ocv safeguard: good anchor mismatch={good_mismatch:.4f}V "
          f"rmse_increase={good_rmse_increase:.2f} (adoptable); "
          f"bad anchor mismatch={bad_mismatch:.4f}V "
          f"rmse_increase={bad_rmse_increase:.2f} (rejected)")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ebc_ecm_test_") as folder:
        check_single_branch(folder)
        check_two_branch(folder)
        check_pseudo_ocv_anchoring(folder)
    check_fixed_ocv_mismatch_safeguard()
    print("SYNTHETIC CHARACTERIZATION CHECKS PASSED")


if __name__ == "__main__":
    main()
