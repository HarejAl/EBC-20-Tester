#!/usr/bin/env python3
"""Edit the variables below and click VS Code's Run Python File button."""

from __future__ import annotations

import json
import os
from pathlib import Path

from realtime_bms.model import TheveninEcmModel
from realtime_bms.monitor import make_observer, run_monitor

BMS_DIR = os.path.dirname(os.path.abspath(__file__))
FRAMEWORK_DIR = os.path.join(os.path.dirname(BMS_DIR), "Python", "ebc_framework")

# --------------------------- EDIT THESE SETTINGS ---------------------------

# Exact EBC logger CSV. Set to None to use the newest raw CSV under results/.
# Start the cell test first when using automatic discovery.
INPUT_CSV = None

# JSON lookup is easiest. Alternatively set MODEL_JSON=None, provide MODEL_MAT,
# and set MAT_CAPACITY_AH; your MATLAB SOC/Em/R0/Rx/taux schema is supported.
MODEL_JSON = os.path.join(BMS_DIR, "realtime_bms", "example_2rc_model.json")
MODEL_MAT = None
MAT_CAPACITY_AH = 1.9

# "ekf" or "ukf". Both estimate x=[SOC, V_1, ..., V_N] (N = model branches).
FILTER_TYPE = "ekf"
INITIAL_SOC = 1.0
INITIAL_SOC_STD = 0.10

# If True, the first near-zero-current voltage initializes SOC from OCV.
# Use only when the cell has rested enough that V approximately equals OCV.
INITIALIZE_FROM_REST_VOLTAGE = False
REST_CURRENT_THRESHOLD_A = 0.05

# Filter tuning. Variances are internally formed from the voltage standard
# deviation. Process values are continuous per-second covariance additions.
# One entry per RC branch, fastest branch first, matching the model JSON.
VOLTAGE_STD_V = 0.010
Q_SOC_PER_S = 1e-10
Q_RC_PER_S = (1e-7, 1e-8)
NIS_GATE = 9.0
MAX_DT_S = 30.0

# True follows the file while the tester appends. Stop with Ctrl+C.
FOLLOW_LIVE = True
POLL_INTERVAL_S = 0.5
IDLE_TIMEOUT_S = None
PRINT_EVERY_SAMPLES = 5

OUTPUT_FOLDER = os.path.join(BMS_DIR, "results")

# ---------------------------------------------------------------------------


def find_latest_measurement_csv() -> str:
    root = os.path.join(FRAMEWORK_DIR, "results")
    candidates = []
    for folder, _, files in os.walk(root):
        if any(part.casefold() in {"resampled", "ecm_characterization"}
               for part in Path(folder).parts):
            continue
        for name in files:
            lower = name.casefold()
            if lower.endswith(".csv") and "_bms" not in lower:
                path = os.path.join(folder, name)
                candidates.append((os.path.getmtime(path), path))
    if not candidates:
        raise FileNotFoundError(f"No measurement CSV found under {root}")
    return max(candidates)[1]


def load_model() -> TheveninEcmModel:
    if MODEL_JSON:
        with open(MODEL_JSON, encoding="utf-8") as fh:
            metadata = json.load(fh)
        if metadata.get("calibration_status", "").startswith("EXAMPLE"):
            print("WARNING: using example ECM parameters, not a calibrated cell model.")
        return TheveninEcmModel.from_json(MODEL_JSON)
    if MODEL_MAT:
        return TheveninEcmModel.from_mat(MODEL_MAT, MAT_CAPACITY_AH)
    raise ValueError("Set MODEL_JSON or MODEL_MAT")


def main() -> None:
    source = os.path.abspath(INPUT_CSV or find_latest_measurement_csv())
    os.makedirs(OUTPUT_FOLDER, exist_ok=True)
    stem = os.path.splitext(os.path.basename(source))[0]
    output = os.path.join(OUTPUT_FOLDER, f"{stem}_bms.csv")
    checkpoint = os.path.join(OUTPUT_FOLDER, f"{stem}_bms_checkpoint.json")
    observer = make_observer(load_model(), {
        "filter_type": FILTER_TYPE,
        "initial_soc": INITIAL_SOC,
        "initial_soc_std": INITIAL_SOC_STD,
        "q_soc_per_s": Q_SOC_PER_S,
        "q_rc_per_s": Q_RC_PER_S,
        "voltage_std_v": VOLTAGE_STD_V,
        "nis_gate": NIS_GATE,
        "max_dt_s": MAX_DT_S,
        "initialize_from_rest_voltage": INITIALIZE_FROM_REST_VOLTAGE,
        "rest_current_threshold_a": REST_CURRENT_THRESHOLD_A,
    })
    print("Read-only real-time BMS")
    print(f"  source: {source}")
    print(f"  model:  {MODEL_JSON or MODEL_MAT}")
    state_labels = ", ".join(f"V{i}" for i in range(1, observer.model.num_branches+1))
    print(f"  filter: {FILTER_TYPE.upper()}  state=[SOC, {state_labels}]")
    print(f"  output: {output}")
    print("  hardware access: none")
    run_monitor(
        source, output, observer, follow=FOLLOW_LIVE,
        poll_interval_s=POLL_INTERVAL_S, idle_timeout_s=IDLE_TIMEOUT_S,
        checkpoint_path=checkpoint, print_every=PRINT_EVERY_SAMPLES)


if __name__ == "__main__":
    main()
