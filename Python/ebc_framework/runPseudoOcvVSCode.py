#!/usr/bin/env python3
"""Edit the settings below, then click VS Code's Run Python File button."""

import os

from ecm_characterization.pseudo_ocv import characterize_pseudo_ocv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(SCRIPT_DIR, "results")

# --------------------------- EDIT THESE SETTINGS ---------------------------

# Completed low-rate (e.g. C/20) full-discharge pseudo-OCV logger CSV.
# Leave None to pick it with a file dialog instead of typing a path.
DISCHARGE_CSV = None

# Optional companion charge-direction pseudo-OCV CSV. Stored for later use
# once redone properly (see the README caveat); not yet combined into the
# curve. Leave None if you don't have one, or don't want to reference it yet.
CHARGE_CSV = None

# Derived JSON/CSV/SVG files are written here. None places them in a
# pseudo_ocv folder next to the discharge CSV's own protocol folder.
OUTPUT_FOLDER = None

# ---------------------------------------------------------------------------


def pick_file(initial_dir: str, title: str) -> str | None:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askopenfilename(
        title=title,
        initialdir=initial_dir if os.path.isdir(initial_dir) else SCRIPT_DIR,
        filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
    root.destroy()
    return path or None


def resolve_discharge_csv() -> str:
    if DISCHARGE_CSV:
        return DISCHARGE_CSV
    picked = pick_file(RESULTS_DIR, "Select a completed pseudo-OCV discharge CSV")
    if not picked:
        raise SystemExit("No discharge CSV selected.")
    return picked


def resolve_output_folder(discharge_csv: str) -> str:
    if OUTPUT_FOLDER:
        return OUTPUT_FOLDER
    # .../results/<protocol>/run.csv -> .../results/<protocol>/pseudo_ocv
    protocol_dir = os.path.dirname(discharge_csv)
    return os.path.join(protocol_dir, "pseudo_ocv")


def main() -> None:
    discharge_csv = resolve_discharge_csv()
    output_folder = resolve_output_folder(discharge_csv)
    result = characterize_pseudo_ocv(discharge_csv, output_folder, CHARGE_CSV)
    print("Pseudo-OCV characterization complete")
    print(f"  discharge input: {discharge_csv}")
    print(f"  measured capacity: {result['capacity_ah']:.4f} Ah")
    print(f"  output folder: {os.path.abspath(output_folder)}")
    print("  point runCharacterizationVSCode.py's PSEUDO_OCV_JSON at "
         f"{os.path.join(os.path.abspath(output_folder), 'pseudo_ocv.json')} "
         "(or leave it None to auto-discover the newest one)")


if __name__ == "__main__":
    main()
