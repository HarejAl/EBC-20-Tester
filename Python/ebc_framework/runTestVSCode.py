#!/usr/bin/env python3
"""Edit the settings below, then click VS Code's Run Python File button."""

import os

from launchTest import main

# --------------------------- EDIT THESE SETTINGS ---------------------------
# File name/stem from config/tests, or a full path to another test JSON.
# Paper sequence, run one at a time instead of via runFolderVSCode.py:
#   "paper/02c_cc_discharge_2C", "paper/02b_cc_discharge_1C",
#   "paper/03a_pulse_train_precharge", "paper/03b_pulse_train"
TEST = "cc_cv_charge"

# Cell ID from config/cells. Set to None to use the cell written in the test.
# Setting an ID here always overrides the value in the test JSON.
CELL = "LP604765"

# Real tester settings.
PORT = "COM5"

# Keep True for an offline dry-run. Change to False only when the real tester
# and the selected cell are connected; PORT is ignored in simulator mode.
SIMULATOR = False
TIME_ACCEL = 100.0
SIMULATOR_INITIAL_SOC = 0.8

# Open a graph that updates during the test.
LIVE_PLOT = False

# Optional exact CSV path. Keep None for
# results/<protocol>/<protocol>_<timestamp>.csv.
OUTPUT_CSV = None

# To continue an interrupted run, put its CSV or *_checkpoint.json path here.
# While resuming, TEST, CELL, and OUTPUT_CSV are read from the checkpoint.
RESUME_FROM = None

# Set True to select an interrupted CSV/checkpoint with a file picker.
SELECT_RESUME_FILE = False

# ---------------------------------------------------------------------------
def pick_resume_file() -> str | None:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askopenfilename(
        title="Select an interrupted EBC test",
        initialdir=os.path.join(os.path.dirname(__file__), "results"),
        filetypes=[("EBC run", "*.csv *.json"), ("All files", "*.*")],
    )
    root.destroy()
    return path or None


def build_arguments() -> list[str]:
    resume_from = pick_resume_file() if SELECT_RESUME_FILE else RESUME_FROM
    if SELECT_RESUME_FILE and not resume_from:
        raise SystemExit("No interrupted test selected.")
    args = ["--resume", resume_from] if resume_from else [TEST]
    if CELL and not resume_from:
        args += ["--cell", CELL]
    if SIMULATOR:
        args += [
            "--sim",
            "--time-accel", str(TIME_ACCEL),
            "--sim-soc0", str(SIMULATOR_INITIAL_SOC),
        ]
    else:
        args += ["--port", PORT]
    if LIVE_PLOT:
        args.append("--live-plot")
    if OUTPUT_CSV and not resume_from:
        args += ["--out", OUTPUT_CSV]
    return args


if __name__ == "__main__":
    main(build_arguments())
