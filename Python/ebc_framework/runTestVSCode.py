#!/usr/bin/env python3
"""Edit the settings below, then click VS Code's Run Python File button."""

from launchTest import main


# --------------------------- EDIT THESE SETTINGS ---------------------------

# File name/stem from config/tests, or a full path to another test JSON.
TEST = "hppc"

# Cell ID from config/cells. Set to None to use the cell written in the test.
# Setting an ID here always overrides the value in the test JSON.
CELL = "LP604765"

# Real tester settings.
PORT = "COM5"

# Keep True for an offline dry-run. Change to False only when the real tester
# and the selected cell are connected; PORT is ignored in simulator mode.
SIMULATOR = True
TIME_ACCEL = 100.0
SIMULATOR_INITIAL_SOC = 0.8

# Open a graph that updates during the test.
LIVE_PLOT = True

# Optional exact CSV path. Keep None for results/<test>_<timestamp>.csv.
OUTPUT_CSV = None

# ---------------------------------------------------------------------------


def build_arguments() -> list[str]:
    args = [TEST]
    if CELL:
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
    if OUTPUT_CSV:
        args += ["--out", OUTPUT_CSV]
    return args


if __name__ == "__main__":
    main(build_arguments())
