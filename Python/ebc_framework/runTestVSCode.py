#!/usr/bin/env python3
"""Edit the settings below, then click VS Code's Run Python File button."""

import os

from launchTest import main

# --------------------------- EDIT THESE SETTINGS ---------------------------
# File name/stem from config/tests, or a full path to another test JSON.
# Subfolders work too, e.g. "paper/02c_cc_discharge_2C".
TEST = "connection_test"

# Cell ID from config/cells. Set to None to use the cell written in the test.
# Setting an ID here always overrides the value in the test JSON.
CELL = "LP604765"

# Serial port of the tester (Windows: "COM5"; Linux: "/dev/ttyUSB0").
PORT = "COM5"

# True: write CSV + metadata + checkpoint to results/<test name>/.
# False: only print samples in the terminal (nothing saved, no resume).
SAVE_DATA = True

# Open a graph that updates during the test (requires SAVE_DATA = True).
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
    args += ["--port", PORT]
    if not SAVE_DATA:
        args.append("--no-save")
    if LIVE_PLOT:
        args.append("--live-plot")
    if OUTPUT_CSV and not resume_from:
        args += ["--out", OUTPUT_CSV]
    return args


if __name__ == "__main__":
    main(build_arguments())
