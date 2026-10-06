#!/usr/bin/env python3
"""Build a measured OCV(SOC) reference curve from a low-rate pseudo-OCV run.

Turns a single low-current (e.g. C/20) full discharge CSV into a reusable
pseudo_ocv.json: a measured capacity (the device's own charge_ah counter at
the end of the active discharge, not a datasheet figure) and an invertible
SOC->OCV curve, both derived from the same run so there is no capacity
mismatch between them. characterize_hppc.py consumes this JSON to correct
its coulomb-counting capacity and anchor per-pulse SOC to real OCV.

Deliberately discharge-only for now: a companion charge-direction run would
let this curve cancel IR-drop/hysteresis by averaging both directions, but
the charge-side run on hand was stopped mid-taper after an under-length
pre-rest, so its capacity is a biased lower bound, not fit to combine with a
clean discharge trace. `charge_csv` is accepted and stored for later use once
that run is redone properly, but is not combined into the curve yet.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Iterable, Optional

import numpy as np


def _finite(value: str | None) -> float:
    try:
        result = float(value) if value not in (None, "") else math.nan
    except (TypeError, ValueError):
        return math.nan
    return result if math.isfinite(result) else math.nan


def load_run(path: str) -> dict[str, np.ndarray]:
    """Load t_s, voltage_v, charge_ah, mode from a pseudo-OCV CSV."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        headers = set(reader.fieldnames or [])
        missing = {"t_s", "voltage_v", "charge_ah", "mode"} - headers
        if missing:
            raise ValueError(f"CSV is missing columns: {', '.join(sorted(missing))}")
        rows = list(reader)

    t, voltage, charge_ah, mode = [], [], [], []
    for row in rows:
        t_val = _finite(row.get("t_s"))
        v_val = _finite(row.get("voltage_v"))
        q_val = _finite(row.get("charge_ah"))
        if not all(math.isfinite(v) for v in (t_val, v_val, q_val)):
            continue
        t.append(t_val)
        voltage.append(v_val)
        charge_ah.append(q_val)
        mode.append((row.get("mode") or "").strip().lower())
    if len(t) < 3:
        raise ValueError("need at least three valid samples")
    return {"t": np.asarray(t), "voltage": np.asarray(voltage),
            "charge_ah": np.asarray(charge_ah), "mode": np.asarray(mode)}


def measured_capacity_ah(run: dict[str, np.ndarray], direction: str = "discharge") -> float:
    """Peak Ah the device's own counter reached during `direction` mode."""
    active = run["charge_ah"][run["mode"] == direction]
    if len(active) == 0:
        raise ValueError(f"no rows with mode == {direction!r} in this run")
    return float(np.max(active))


def soc_voltage_curve(run: dict[str, np.ndarray], capacity_ah: float,
                      direction: str = "discharge") -> tuple[np.ndarray, np.ndarray]:
    """Return (soc, ocv_v) sorted ascending by SOC, monotonic in voltage.

    Only rows in `direction` mode are used (excludes any idle padding before/
    after the sweep). A light isotonic pass (cumulative max, since voltage
    must increase with SOC on a discharge trace) removes small sweep-noise
    non-monotonicity so the curve is safely invertible later -- real sweep
    noise, not a genuine multi-valued OCV(SOC), is what this is correcting.
    """
    active = run["mode"] == direction
    charge_ah = run["charge_ah"][active]
    voltage = run["voltage"][active]
    soc = 1.0 - charge_ah / capacity_ah
    order = np.argsort(soc)
    soc_sorted = soc[order]
    voltage_sorted = np.maximum.accumulate(voltage[order])
    return soc_sorted, voltage_sorted


def invert_ocv_to_soc(voltage: float, soc_curve: np.ndarray, ocv_curve: np.ndarray,
                      min_slope_v_per_soc: float = 0.05) -> float:
    """Invert a monotonic OCV(SOC) curve at one voltage; NaN if untrustworthy.

    Rejects (returns NaN) rather than silently extrapolating/clamping when
    `voltage` falls outside the curve's own measured range, and rejects when
    the local slope is too flat to trust an inversion (ordinary rest-voltage
    noise would translate into a large SOC swing there).
    """
    if not math.isfinite(voltage):
        return math.nan
    if voltage < ocv_curve[0] or voltage > ocv_curve[-1]:
        return math.nan
    index = int(np.searchsorted(ocv_curve, voltage))
    lo = max(index - 1, 0)
    hi = min(index, len(ocv_curve) - 1)
    if hi == lo:
        hi = min(lo + 1, len(ocv_curve) - 1)
    d_voltage = ocv_curve[hi] - ocv_curve[lo]
    d_soc = soc_curve[hi] - soc_curve[lo]
    if d_soc <= 0 or d_voltage / d_soc < min_slope_v_per_soc:
        return math.nan
    return float(np.interp(voltage, ocv_curve, soc_curve))


def _esc(text: object) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _plot_curve(path: Path, soc: np.ndarray, ocv_v: np.ndarray) -> None:
    width, height = 900, 520
    left, right, top, bottom = 85, width - 25, 50, height - 65
    xmin, xmax = float(np.min(soc)), float(np.max(soc))
    ymin, ymax = float(np.min(ocv_v)), float(np.max(ocv_v))
    xpad, ypad = .03 * (xmax - xmin or 1), .08 * (ymax - ymin or 1)
    xmin, xmax, ymin, ymax = xmin - xpad, xmax + xpad, ymin - ypad, ymax + ypad
    sx = lambda x: left + (x - xmin) / (xmax - xmin) * (right - left)
    sy = lambda y: bottom - (y - ymin) / (ymax - ymin) * (bottom - top)
    points = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in zip(soc, ocv_v))
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
             '<rect width="100%" height="100%" fill="white"/>',
             f'<text x="{width/2}" y="28" text-anchor="middle" font-family="sans-serif" font-size="18">Pseudo-OCV versus SOC (discharge-only)</text>']
    for fraction in np.linspace(0, 1, 6):
        x = left + fraction * (right - left); y = bottom - fraction * (bottom - top)
        xv = xmin + fraction * (xmax - xmin); yv = ymin + fraction * (ymax - ymin)
        parts += [f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{bottom}" stroke="#ddd"/>',
                  f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="#ddd"/>',
                  f'<text x="{x:.1f}" y="{bottom+20}" text-anchor="middle" font-family="sans-serif" font-size="11">{xv:.3g}</text>',
                  f'<text x="{left-8}" y="{y+4:.1f}" text-anchor="end" font-family="sans-serif" font-size="11">{yv:.4g}</text>']
    parts += [f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" stroke="black"/>',
              f'<line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" stroke="black"/>',
              f'<polyline points="{points}" fill="none" stroke="#1f77b4" stroke-width="2"/>',
              f'<text x="{(left+right)/2}" y="{height-18}" text-anchor="middle" font-family="sans-serif" font-size="13">SOC (fraction)</text>',
              f'<text x="18" y="{(top+bottom)/2}" text-anchor="middle" transform="rotate(-90 18 {(top+bottom)/2})" font-family="sans-serif" font-size="13">OCV (V)</text>', '</svg>']
    path.write_text("\n".join(parts), encoding="utf-8")


def characterize_pseudo_ocv(discharge_csv: str, output_dir: str,
                            charge_csv: Optional[str] = None) -> dict[str, object]:
    input_path = Path(discharge_csv).resolve()
    output = Path(output_dir).resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f"discharge CSV not found: {input_path}")
    output.mkdir(parents=True, exist_ok=True)

    run = load_run(str(input_path))
    capacity_ah = measured_capacity_ah(run, "discharge")
    soc, ocv_v = soc_voltage_curve(run, capacity_ah, "discharge")

    export = {
        "method": "discharge_only",
        "capacity_ah": capacity_ah,
        "source_discharge_csv": str(input_path),
        "source_charge_csv": str(Path(charge_csv).resolve()) if charge_csv else None,
        "note": ("Built from the discharge run alone. A charge-direction run "
                 "exists but was stopped mid-taper after an under-length "
                 "pre-rest, so its capacity/curve are not combined in here."),
        "soc": soc.tolist(),
        "ocv_v": ocv_v.tolist(),
    }
    with (output / "pseudo_ocv.json").open("w", encoding="utf-8") as fh:
        json.dump(export, fh, indent=2, allow_nan=False)

    with (output / "pseudo_ocv_lookup.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["soc", "ocv_v"])
        for s, v in zip(soc, ocv_v):
            writer.writerow([f"{s:.6f}", f"{v:.4f}"])

    _plot_curve(output / "pseudo_ocv_curve.svg", soc, ocv_v)
    return export


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("discharge_csv")
    parser.add_argument("--charge-csv", default=None)
    parser.add_argument("--output-dir", required=True)
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> None:
    args = parse_args(argv)
    result = characterize_pseudo_ocv(args.discharge_csv, args.output_dir, args.charge_csv)
    print(f"Measured capacity: {result['capacity_ah']:.4f} Ah "
          f"(from {result['source_discharge_csv']})")
    print(f"Outputs: {Path(args.output_dir).resolve()}")


if __name__ == "__main__":
    main()
