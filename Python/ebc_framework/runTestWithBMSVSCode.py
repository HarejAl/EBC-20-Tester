#!/usr/bin/env python3
"""Launch one EBC test and its read-only SOC observer together.

Edit the settings below, then click VS Code's Run Python File button. The EBC
test remains the only owner of the serial port; the BMS runs in a background
thread and follows the test's flushed CSV rows.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback

from launchTest import (RESULTS_DIR, TestDef, main as run_test_main,
                        resolve_checkpoint_path, resolve_test_path,
                        result_folder_name)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TESTER_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
BMS_DIR = os.path.join(TESTER_DIR, "bms")
if BMS_DIR not in sys.path:
    sys.path.insert(0, BMS_DIR)

from realtime_bms.model import TheveninEcmModel
from realtime_bms.monitor import make_observer, run_monitor


# ------------------------- TEST SETTINGS -----------------------------------

TEST = "hppc"
CELL = "LP604765"
PORT = "COM5"

# Keep True for an offline dry run. Set False only with the intended cell and
# tester connected. In simulator mode PORT is ignored.
SIMULATOR = True
TIME_ACCEL = 100.0
SIMULATOR_INITIAL_SOC = 0.8

LIVE_TEST_PLOT = False

# Optional exact measurement CSV. None creates the normal timestamped result.
OUTPUT_CSV = None

# Set to an interrupted CSV or checkpoint to resume it; TEST/CELL/OUTPUT_CSV
# are then obtained from the checkpoint.
RESUME_FROM = None


# -------------------------- BMS SETTINGS -----------------------------------

MODEL_JSON = os.path.join(BMS_DIR, "realtime_bms", "example_2rc_model.json")
MODEL_MAT = None
MAT_CAPACITY_AH = 1.9

# Optional best_kf_config.json produced by runKFTuningVSCode.py. It overrides
# filter/tuning values below, while BMS_INITIAL_SOC remains specific to this run.
TUNED_KF_CONFIG_JSON = None

FILTER_TYPE = "ekf"                 # "ekf" or "ukf"
BMS_INITIAL_SOC = 0.8
BMS_INITIAL_SOC_STD = 0.10
INITIALIZE_FROM_REST_VOLTAGE = False
REST_CURRENT_THRESHOLD_A = 0.05

VOLTAGE_STD_V = 0.010
Q_SOC_PER_S = 1e-10
# One entry per RC branch, fastest branch first, matching the model JSON.
Q_RC_PER_S = (1e-7, 1e-8)
NIS_GATE = 9.0
LARGE_GAP_WARNING_S = 30.0

BMS_POLL_INTERVAL_S = 0.25
BMS_PRINT_EVERY_SAMPLES = 5
BMS_OUTPUT_FOLDER = os.path.join(BMS_DIR, "results")

# ---------------------------------------------------------------------------


def load_bms_model() -> TheveninEcmModel:
    if MODEL_JSON:
        with open(MODEL_JSON, encoding="utf-8") as fh:
            metadata = json.load(fh)
        if metadata.get("calibration_status", "").startswith("EXAMPLE"):
            print("WARNING: BMS is using example, non-calibrated ECM parameters.")
        return TheveninEcmModel.from_json(MODEL_JSON)
    if MODEL_MAT:
        return TheveninEcmModel.from_mat(MODEL_MAT, MAT_CAPACITY_AH)
    raise ValueError("Set MODEL_JSON or MODEL_MAT")


def measurement_csv_path() -> str:
    """Choose the path before either the logger or BMS starts."""
    if RESUME_FROM:
        checkpoint_path = resolve_checkpoint_path(RESUME_FROM)
        with open(checkpoint_path, encoding="utf-8") as fh:
            checkpoint = json.load(fh)
        if checkpoint.get("complete"):
            raise ValueError("the selected checkpoint is already complete")
        return os.path.abspath(checkpoint["csv_path"])
    if OUTPUT_CSV:
        return os.path.abspath(OUTPUT_CSV)
    test = TestDef.load(resolve_test_path(TEST))
    folder = os.path.join(RESULTS_DIR, result_folder_name(test.name))
    os.makedirs(folder, exist_ok=True)
    return os.path.join(
        folder, f"{test.name}_{time.strftime('%Y%m%d_%H%M%S')}.csv")


def test_arguments(measurement_csv: str) -> list[str]:
    if RESUME_FROM:
        args = ["--resume", RESUME_FROM]
    else:
        args = [TEST, "--out", measurement_csv]
        if CELL: args += ["--cell", CELL]
    if SIMULATOR:
        args += ["--sim", "--time-accel", str(TIME_ACCEL),
                 "--sim-soc0", str(SIMULATOR_INITIAL_SOC)]
    else:
        args += ["--port", PORT]
    if LIVE_TEST_PLOT: args.append("--live-plot")
    return args


def build_observer():
    settings = {
        "filter_type": FILTER_TYPE,
        "initial_soc": BMS_INITIAL_SOC,
        "initial_soc_std": BMS_INITIAL_SOC_STD,
        "initialize_from_rest_voltage": INITIALIZE_FROM_REST_VOLTAGE,
        "rest_current_threshold_a": REST_CURRENT_THRESHOLD_A,
        "voltage_std_v": VOLTAGE_STD_V,
        "q_soc_per_s": Q_SOC_PER_S,
        "q_rc_per_s": Q_RC_PER_S,
        "nis_gate": NIS_GATE,
        "max_dt_s": LARGE_GAP_WARNING_S,
    }
    if TUNED_KF_CONFIG_JSON:
        with open(TUNED_KF_CONFIG_JSON, encoding="utf-8") as fh:
            tuned = json.load(fh)["observer_settings"]
        allowed = set(settings)-{"initial_soc"}
        settings.update({key: value for key, value in tuned.items()
                         if key in allowed})
        print(f"Loaded tuned KF settings: {TUNED_KF_CONFIG_JSON}")
    return make_observer(load_bms_model(), settings)


def main() -> None:
    # Validate the model and observer before a hardware connection is opened.
    observer = build_observer()
    measurement_csv = measurement_csv_path()
    os.makedirs(BMS_OUTPUT_FOLDER, exist_ok=True)
    stem = os.path.splitext(os.path.basename(measurement_csv))[0]
    bms_csv = os.path.join(BMS_OUTPUT_FOLDER, f"{stem}_bms.csv")
    bms_checkpoint = os.path.join(
        BMS_OUTPUT_FOLDER, f"{stem}_bms_checkpoint.json")
    stop_event = threading.Event()
    bms_failure = []

    def bms_worker() -> None:
        try:
            run_monitor(
                measurement_csv, bms_csv, observer, follow=True,
                poll_interval_s=BMS_POLL_INTERVAL_S,
                checkpoint_path=bms_checkpoint,
                print_every=BMS_PRINT_EVERY_SAMPLES,
                stop_event=stop_event)
        except Exception:
            bms_failure.append(traceback.format_exc())
            print("\nBMS monitor failed; the tester controller remains independent.",
                  file=sys.stderr)
            print(bms_failure[-1], file=sys.stderr)

    print("Combined test + BMS run")
    print(f"  measurement CSV: {measurement_csv}")
    print(f"  BMS output:      {bms_csv}")
    state_labels = ", ".join(f"V{i}" for i in range(1, observer.model.num_branches+1))
    print(f"  BMS filter:      {observer.filter_type.upper()} [SOC, {state_labels}]")
    print("  serial ownership: EBC test only; BMS reads CSV only\n")
    worker = threading.Thread(target=bms_worker, name="read-only-bms",
                              daemon=True)
    worker.start()
    try:
        run_test_main(test_arguments(measurement_csv))
    finally:
        # The logger flushes each row. Signal completion, then let the follower
        # drain all rows already present before it exits on an empty read.
        stop_event.set()
        worker.join(timeout=15.0)
        if worker.is_alive():
            print("Warning: BMS follower did not exit within 15 seconds.",
                  file=sys.stderr)
    if bms_failure:
        raise RuntimeError("test finished, but the BMS monitor failed")
    print("Combined run complete.")


if __name__ == "__main__":
    main()
