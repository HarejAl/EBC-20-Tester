#!/usr/bin/env python3
"""Grid-search EKF/UKF Q/R weights on a completed tester CSV."""

from __future__ import annotations

import json
import os
import time

from realtime_bms.model import TheveninEcmModel
from realtime_bms.offline import load_measurements, reference_array
from realtime_bms.tuning import (TuningSpace, TuningWeights, tune_filters,
                                 write_tuning_outputs)

BMS_DIR = os.path.dirname(os.path.abspath(__file__))
FRAMEWORK_DIR = os.path.join(os.path.dirname(BMS_DIR), "Python", "ebc_framework")

# --------------------------- EDIT THESE SETTINGS ---------------------------

INPUT_CSV = os.path.join(
    FRAMEWORK_DIR, "results", "hppc", "hppc_YYYYMMDD_HHMMSS.csv")

MODEL_JSON = os.path.join(BMS_DIR, "realtime_bms", "example_2rc_model.json")
MODEL_MAT = None
MAT_CAPACITY_AH = 1.9

INITIAL_SOC = 1.0
INITIAL_SOC_STD = 0.10
INITIAL_RC_STD_V = 0.03
NIS_GATE = 9.0
LARGE_GAP_WARNING_S = 30.0

# Prefer an independent reference column when available. For a real HPPC with
# known starting SOC/capacity, coulomb_count is useful for provisional tuning.
REFERENCE_SOURCE = "coulomb_count"   # "column" or "coulomb_count"
REFERENCE_SOC_COLUMN = "soc_true"
REFERENCE_SOC_IS_PERCENT = False
REFERENCE_CAPACITY_AH = None          # None uses ECM capacity

# Candidate grid. Start compact; expand around the best region afterward.
# Process noise for the fastest RC branch; each slower branch is scaled down
# by Q_RC_DECAY_RATIO_CANDIDATES (e.g. 0.1 means each slower branch gets 10x
# less process noise than the one before it). This keeps the grid's size
# fixed regardless of how many RC branches the model has.
FILTER_TYPES = ("ekf", "ukf")
Q_SOC_CANDIDATES = (1e-11, 1e-10, 1e-9)
Q_RC_FAST_CANDIDATES = (1e-8, 1e-7)
Q_RC_DECAY_RATIO_CANDIDATES = (0.05, 0.1)
VOLTAGE_STD_CANDIDATES_V = (0.005, 0.010, 0.020)

SCORE_START_FRACTION = 0.20

# Composite score: SOC RMSE dominates; the other terms discourage statistically
# inconsistent innovations, excessive rejection, and poor uncertainty coverage.
WEIGHT_SOC_RMSE = 1.0
WEIGHT_NIS_LOG_ERROR = 0.002
WEIGHT_REJECTION_FRACTION = 0.05
WEIGHT_COVERAGE_ERROR = 0.01

OUTPUT_ROOT = os.path.join(BMS_DIR, "results", "tuning")

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
    space = TuningSpace(
        filter_types=tuple(FILTER_TYPES), q_soc_per_s=tuple(Q_SOC_CANDIDATES),
        q_rc_fast_per_s=tuple(Q_RC_FAST_CANDIDATES),
        q_rc_decay_ratio=tuple(Q_RC_DECAY_RATIO_CANDIDATES),
        voltage_std_v=tuple(VOLTAGE_STD_CANDIDATES_V))
    fixed = {
        "initial_soc": INITIAL_SOC, "initial_soc_std": INITIAL_SOC_STD,
        "initial_rc_std_v": INITIAL_RC_STD_V, "nis_gate": NIS_GATE,
        "max_dt_s": LARGE_GAP_WARNING_S,
    }
    weights = TuningWeights(
        soc_rmse=WEIGHT_SOC_RMSE, nis_log_error=WEIGHT_NIS_LOG_ERROR,
        rejection_fraction=WEIGHT_REJECTION_FRACTION,
        coverage_error=WEIGHT_COVERAGE_ERROR)
    ranked = tune_filters(model, samples, reference, space, fixed,
                          SCORE_START_FRACTION, weights)
    stem = os.path.splitext(os.path.basename(INPUT_CSV))[0]
    output = os.path.join(
        OUTPUT_ROOT, f"{stem}_{time.strftime('%Y%m%d_%H%M%S')}")
    export = write_tuning_outputs(
        output, ranked, model, samples, reference, fixed, reference_kind,
        SCORE_START_FRACTION, weights, space)
    print("KF tuning complete")
    print(f"  candidates: {len(ranked)}")
    print(f"  best settings: {export['observer_settings']}")
    print(f"  best metrics: {export['best_metrics']}")
    print(f"  output: {output}")


if __name__ == "__main__":
    main()
