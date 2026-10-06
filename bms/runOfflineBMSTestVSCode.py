#!/usr/bin/env python3
"""Replay a completed tester CSV through one EKF or UKF configuration."""

from __future__ import annotations

import json
import os
import time

from realtime_bms.model import TheveninEcmModel
from realtime_bms.observer import SocObserver
from realtime_bms.offline import (load_measurements, reference_array,
                                  replay_samples, write_diagnostics)

BMS_DIR = os.path.dirname(os.path.abspath(__file__))
FRAMEWORK_DIR = os.path.join(os.path.dirname(BMS_DIR), "Python", "ebc_framework")

# --------------------------- EDIT THESE SETTINGS ---------------------------

INPUT_CSV = os.path.join(
    FRAMEWORK_DIR, "results", "cc_cv_charge", "cc_cv_charge_20260722_162401_meta.json")

MODEL_JSON = os.path.join(BMS_DIR, "realtime_bms", "example_2rc_model.json")
MODEL_MAT = None
MAT_CAPACITY_AH = 1.9

# Optional best_kf_config.json from runKFTuningVSCode.py. When set, it
# overrides the filter/tuning variables below but not INITIAL_SOC.
TUNED_KF_CONFIG_JSON = None

FILTER_TYPE = "ekf"                 # "ekf" or "ukf"
INITIAL_SOC = 1.0
INITIAL_SOC_STD = 0.10
INITIAL_RC_STD_V = 0.03

Q_SOC_PER_S = 1e-10
# One entry per RC branch, fastest branch first, matching the model JSON.
Q_RC_PER_S = (1e-7, 1e-8)
VOLTAGE_STD_V = 0.010
NIS_GATE = 9.0
LARGE_GAP_WARNING_S = 30.0

# "column" uses a ground-truth/simulator SOC column. "coulomb_count" builds a
# provisional reference using INITIAL_SOC and REFERENCE_CAPACITY_AH.
REFERENCE_SOURCE = "coulomb_count"
REFERENCE_SOC_COLUMN = "soc_true"
REFERENCE_SOC_IS_PERCENT = False
REFERENCE_CAPACITY_AH = None          # None uses ECM capacity

# Ignore this initial fraction when computing metrics (filter convergence).
SCORE_START_FRACTION = 0.20

OUTPUT_ROOT = os.path.join(BMS_DIR, "results", "offline")

# ---------------------------------------------------------------------------


def load_model() -> TheveninEcmModel:
    if MODEL_JSON:
        with open(MODEL_JSON, encoding="utf-8") as fh:
            metadata = json.load(fh)
        if metadata.get("calibration_status", "").startswith("EXAMPLE"):
            print("WARNING: using example, non-calibrated ECM parameters.")
        return TheveninEcmModel.from_json(MODEL_JSON)
    if MODEL_MAT:
        return TheveninEcmModel.from_mat(MODEL_MAT, MAT_CAPACITY_AH)
    raise ValueError("Set MODEL_JSON or MODEL_MAT")


def main() -> None:
    model = load_model()
    reference_column = REFERENCE_SOC_COLUMN if REFERENCE_SOURCE == "column" else None
    samples = load_measurements(INPUT_CSV, reference_column,
                                REFERENCE_SOC_IS_PERCENT)
    reference, reference_kind = reference_array(
        samples, REFERENCE_SOURCE, INITIAL_SOC,
        REFERENCE_CAPACITY_AH or model.capacity_ah,
        model.charge_efficiency)
    settings = {
        "filter_type": FILTER_TYPE, "initial_soc": INITIAL_SOC,
        "initial_soc_std": INITIAL_SOC_STD,
        "initial_rc_std_v": INITIAL_RC_STD_V,
        "q_soc_per_s": Q_SOC_PER_S, "q_rc_per_s": Q_RC_PER_S,
        "voltage_std_v": VOLTAGE_STD_V,
        "nis_gate": NIS_GATE, "max_dt_s": LARGE_GAP_WARNING_S,
    }
    if TUNED_KF_CONFIG_JSON:
        with open(TUNED_KF_CONFIG_JSON, encoding="utf-8") as fh:
            tuned = json.load(fh)["observer_settings"]
        allowed = set(settings)-{"initial_soc"}
        settings.update({key: value for key, value in tuned.items()
                         if key in allowed})
        print(f"Loaded tuned KF settings: {TUNED_KF_CONFIG_JSON}")
    replay = replay_samples(samples, reference, SocObserver(model, **settings),
                            SCORE_START_FRACTION)
    stem = os.path.splitext(os.path.basename(INPUT_CSV))[0]
    output = os.path.join(
        OUTPUT_ROOT, f"{stem}_{FILTER_TYPE}_{time.strftime('%Y%m%d_%H%M%S')}")
    write_diagnostics(output, replay, {
        "source_csv": os.path.abspath(INPUT_CSV),
        "model": os.path.abspath(MODEL_JSON or MODEL_MAT),
        "reference_kind": reference_kind,
        "observer_settings": settings}, model.num_branches)
    print("Offline replay complete")
    print(f"  reference: {reference_kind}")
    for key, value in replay.metrics.items(): print(f"  {key}: {value}")
    print(f"  output: {output}")


if __name__ == "__main__":
    main()
