#!/usr/bin/env python3
"""Resample irregular EBC CSV measurements onto a fixed time grid.

Edit the settings below, then click VS Code's Run Python File button. Raw CSVs
are never modified. Processed files and a reproducibility metadata JSON are
written under a sibling ``resampled`` folder.
"""

import bisect
import csv
import json
import math
import os
import time
from dataclasses import dataclass


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(SCRIPT_DIR, "results")


# --------------------------- EDIT THESE SETTINGS ---------------------------

# A CSV file or a directory. A directory processes its raw CSV files; when
# RECURSIVE is True, protocol subfolders are included. Keep None for a picker.
INPUT_PATH = None
RECURSIVE = True

# Fixed output interval. Examples: 1.0 = 1 Hz, 0.5 = 2 Hz, 10.0 = 0.1 Hz.
TARGET_PERIOD_S = 1.0

# Default for numeric measurement columns: "linear", "previous", or "nearest".
DEFAULT_NUMERIC_METHOD = "linear"

# Per-column overrides. Current and mode are step-like, and accumulated charge
# can reset between phases, so previous-value sampling avoids invented ramps.
COLUMN_METHODS = {
    "current_a": "previous",
    "signed_current_a": "previous",
    "charge_ah": "previous",
    "mode": "previous",
}

OUTPUT_SUBFOLDER = "resampled"
OVERWRITE = False

# ---------------------------------------------------------------------------


METHODS = {"linear", "previous", "nearest"}


@dataclass
class SourceData:
    headers: list[str]
    times: list[float]
    rows: list[dict[str, str]]
    numeric_columns: set[str]
    precision: dict[str, int]
    input_rows: int
    invalid_rows: int
    duplicate_rows: int


def pick_input_file() -> str | None:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askopenfilename(
        title="Select an EBC result CSV to resample",
        initialdir=RESULTS_DIR,
        filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
    )
    root.destroy()
    return path or None


def discover_csv_files(path: str) -> list[str]:
    path = os.path.abspath(path)
    if os.path.isfile(path):
        if not path.lower().endswith(".csv"):
            raise ValueError(f"input file is not CSV: {path}")
        return [path]
    if not os.path.isdir(path):
        raise FileNotFoundError(f"input path not found: {path}")

    found = []
    if RECURSIVE:
        iterator = os.walk(path)
    else:
        iterator = [(path, [], os.listdir(path))]
    for folder, subfolders, filenames in iterator:
        subfolders[:] = [name for name in subfolders
                         if name.casefold() != OUTPUT_SUBFOLDER.casefold()]
        if os.path.basename(folder).casefold() == OUTPUT_SUBFOLDER.casefold():
            continue
        for filename in filenames:
            lower = filename.lower()
            if lower.endswith(".csv") and "_resampled_" not in lower:
                found.append(os.path.join(folder, filename))
    return sorted(found, key=str.casefold)


def decimal_places(value: str) -> int:
    value = value.strip().lower()
    if "e" in value:
        value = value.split("e", 1)[0]
    return len(value.split(".", 1)[1]) if "." in value else 0


def load_source(path: str) -> SourceData:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        if "t_s" not in headers:
            raise ValueError(f"CSV has no t_s column: {path}")
        raw_rows = list(reader)

    valid: list[tuple[float, dict[str, str]]] = []
    invalid_rows = 0
    duplicate_rows = 0
    previous_t = -math.inf
    for row in raw_rows:
        try:
            timestamp = float(row["t_s"])
        except (TypeError, ValueError):
            invalid_rows += 1
            continue
        if not math.isfinite(timestamp):
            invalid_rows += 1
            continue
        if timestamp < previous_t - 1e-9:
            raise ValueError(
                f"timestamps decrease at t={timestamp:g}s in {path}")
        previous_t = timestamp
        if valid and abs(timestamp - valid[-1][0]) <= 1e-9:
            # Keep the later status at an identical timestamp (for example,
            # the idle report immediately following a charge report).
            valid[-1] = (timestamp, row)
            duplicate_rows += 1
        else:
            valid.append((timestamp, row))

    if len(valid) < 2:
        raise ValueError(f"need at least two valid timestamps: {path}")

    numeric_columns = set()
    precision = {}
    for header in headers:
        if header == "t_s":
            continue
        values = [row.get(header, "").strip() for _, row in valid]
        nonblank = [value for value in values if value != ""]
        try:
            parsed = [float(value) for value in nonblank]
        except ValueError:
            continue
        if nonblank and all(math.isfinite(value) for value in parsed):
            numeric_columns.add(header)
            precision[header] = min(max(decimal_places(value)
                                        for value in nonblank), 9)

    return SourceData(
        headers=headers,
        times=[timestamp for timestamp, _ in valid],
        rows=[row for _, row in valid],
        numeric_columns=numeric_columns,
        precision=precision,
        input_rows=len(raw_rows),
        invalid_rows=invalid_rows,
        duplicate_rows=duplicate_rows,
    )


def format_time(value: float, period_s: float) -> str:
    period_text = f"{period_s:.9f}".rstrip("0")
    period_precision = (len(period_text.split(".", 1)[1])
                        if "." in period_text else 0)
    return f"{value:.{max(period_precision, 3)}f}"


def numeric_value(row: dict[str, str], column: str) -> float:
    value = row.get(column, "").strip()
    return float(value) if value else math.nan


def sample_column(data: SourceData, column: str, target_t: float,
                  lower: int, upper: int) -> str:
    method = COLUMN_METHODS.get(
        column,
        DEFAULT_NUMERIC_METHOD if column in data.numeric_columns else "previous")
    if method not in METHODS:
        raise ValueError(f"unknown method for {column}: {method}")

    if method == "previous" or upper == lower:
        return data.rows[lower].get(column, "")
    if method == "nearest":
        nearest = (lower if target_t - data.times[lower]
                   <= data.times[upper] - target_t else upper)
        return data.rows[nearest].get(column, "")
    if column not in data.numeric_columns:
        raise ValueError(f"linear interpolation requires numeric column: {column}")

    y0 = numeric_value(data.rows[lower], column)
    y1 = numeric_value(data.rows[upper], column)
    if not math.isfinite(y0) or not math.isfinite(y1):
        return data.rows[lower].get(column, "")
    t0, t1 = data.times[lower], data.times[upper]
    fraction = 0.0 if t1 == t0 else (target_t - t0) / (t1 - t0)
    value = y0 + fraction * (y1 - y0)
    return f"{value:.{data.precision.get(column, 6)}f}"


def resample_rows(data: SourceData, period_s: float) -> list[dict[str, str]]:
    if not math.isfinite(period_s) or period_s <= 0:
        raise ValueError("TARGET_PERIOD_S must be a positive finite number")

    first_index = math.ceil((data.times[0] - 1e-9) / period_s)
    last_index = math.floor((data.times[-1] + 1e-9) / period_s)
    output = []
    for grid_index in range(first_index, last_index + 1):
        target_t = grid_index * period_s
        lower = max(bisect.bisect_right(data.times, target_t) - 1, 0)
        upper = min(lower + 1, len(data.times) - 1)
        if abs(data.times[lower] - target_t) <= 1e-9:
            upper = lower
        row = {"t_s": format_time(target_t, period_s)}
        for column in data.headers:
            if column != "t_s":
                row[column] = sample_column(
                    data, column, target_t, lower, upper)
        output.append(row)
    if not output:
        raise ValueError("source time range contains no target-grid samples")
    return output


def interval_label(period_s: float) -> str:
    return f"{period_s:g}".replace(".", "p") + "s"


def output_path_for(source: str, period_s: float) -> str:
    folder = os.path.join(os.path.dirname(source), OUTPUT_SUBFOLDER)
    os.makedirs(folder, exist_ok=True)
    stem = os.path.splitext(os.path.basename(source))[0]
    return os.path.join(
        folder, f"{stem}_resampled_{interval_label(period_s)}.csv")


def resample_file(source: str) -> str:
    data = load_source(source)
    rows = resample_rows(data, TARGET_PERIOD_S)
    output = output_path_for(source, TARGET_PERIOD_S)
    if os.path.exists(output) and not OVERWRITE:
        raise FileExistsError(
            f"output exists (set OVERWRITE=True to replace): {output}")

    with open(output, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=data.headers)
        writer.writeheader()
        writer.writerows(rows)

    methods = {
        column: COLUMN_METHODS.get(
            column,
            DEFAULT_NUMERIC_METHOD if column in data.numeric_columns
            else "previous")
        for column in data.headers if column != "t_s"
    }
    metadata = {
        "source_csv": os.path.abspath(source),
        "output_csv": os.path.abspath(output),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "target_period_s": TARGET_PERIOD_S,
        "target_frequency_hz": 1.0 / TARGET_PERIOD_S,
        "column_methods": methods,
        "source_rows": data.input_rows,
        "invalid_rows_skipped": data.invalid_rows,
        "duplicate_timestamps_replaced_by_later_row": data.duplicate_rows,
        "output_rows": len(rows),
        "source_time_range_s": [data.times[0], data.times[-1]],
    }
    meta_path = os.path.splitext(output)[0] + "_meta.json"
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(metadata, fh, indent=2)

    print(f"Resampled: {source}")
    print(f"        -> {output}")
    print(f"  rows: {data.input_rows} raw -> {len(rows)} fixed-grid; "
          f"invalid={data.invalid_rows}, duplicates={data.duplicate_rows}")
    return output


def main() -> None:
    if DEFAULT_NUMERIC_METHOD not in METHODS:
        raise ValueError(
            f"DEFAULT_NUMERIC_METHOD must be one of {sorted(METHODS)}")
    source = INPUT_PATH or pick_input_file()
    if not source:
        raise SystemExit("No CSV selected.")
    files = discover_csv_files(source)
    if not files:
        raise SystemExit("No raw CSV files found.")

    failures = []
    for path in files:
        try:
            resample_file(path)
        except Exception as exc:
            failures.append((path, exc))
            print(f"ERROR: {path}: {exc}")
    if failures:
        raise SystemExit(f"Resampling failed for {len(failures)} file(s).")
    print(f"\nCompleted {len(files)} file(s).")


if __name__ == "__main__":
    main()
