"""Offline replay, reference construction, metrics, and diagnostics."""

from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from .monitor import TimeStitcher
from .observer import ObserverResult, SocObserver


@dataclass(frozen=True)
class MeasurementSample:
    time_s: float
    voltage_v: float
    current_a: float       # positive discharge
    reference_soc: float | None = None


@dataclass
class ReplayResult:
    rows: list[dict[str, object]]
    metrics: dict[str, object]


def load_measurements(path: str, reference_soc_column: str | None = None,
                      reference_soc_percent: bool = False
                      ) -> list[MeasurementSample]:
    """Load the EBC schema and stitch resumed timestamp resets."""
    stitch = TimeStitcher()
    samples = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        headers = set(reader.fieldnames or [])
        if not {"t_s", "voltage_v"} <= headers:
            raise ValueError("CSV needs t_s and voltage_v")
        if "signed_current_a" not in headers and "current_a" not in headers:
            raise ValueError("CSV needs signed_current_a or current_a")
        if reference_soc_column and reference_soc_column not in headers:
            raise ValueError(f"reference SOC column not found: {reference_soc_column}")
        for row in reader:
            try:
                raw_t, voltage = float(row["t_s"]), float(row["voltage_v"])
                if "signed_current_a" in headers and row.get("signed_current_a", "").strip():
                    current = -float(row["signed_current_a"])
                else:
                    magnitude = abs(float(row["current_a"]))
                    mode = row.get("mode", "").strip().lower()
                    current = (magnitude if mode == "discharge" else
                               -magnitude if mode == "charge" else 0.0)
                reference = None
                if reference_soc_column and row.get(reference_soc_column, "").strip():
                    reference = float(row[reference_soc_column])
                    if reference_soc_percent: reference /= 100.0
                values = [raw_t, voltage, current]
                if reference is not None: values.append(reference)
                if not all(math.isfinite(value) for value in values): continue
            except (KeyError, TypeError, ValueError):
                continue
            samples.append(MeasurementSample(
                stitch(raw_t), voltage, current, reference))
    if len(samples) < 3:
        raise ValueError("need at least three valid measurement rows")
    return samples


def coulomb_reference(samples: list[MeasurementSample], initial_soc: float,
                      capacity_ah: float,
                      charge_efficiency: float = 1.0) -> np.ndarray:
    """Provisional reference; not independent SOC ground truth."""
    if capacity_ah <= 0: raise ValueError("capacity_ah must be positive")
    reference = np.empty(len(samples)); reference[0] = initial_soc
    for index in range(1, len(samples)):
        dt = max(samples[index].time_s-samples[index-1].time_s, 0.0)
        mean_i = .5*(samples[index-1].current_a+samples[index].current_a)
        efficiency = charge_efficiency if mean_i < 0 else 1.0
        reference[index] = reference[index-1]-efficiency*mean_i*dt/(3600*capacity_ah)
    return np.clip(reference, 0, 1)


def reference_array(samples: list[MeasurementSample], source: str,
                    initial_soc: float, capacity_ah: float,
                    charge_efficiency: float = 1.0) -> tuple[np.ndarray, str]:
    key = source.strip().lower()
    if key == "column":
        values = np.asarray([s.reference_soc if s.reference_soc is not None
                             else math.nan for s in samples])
        if np.sum(np.isfinite(values)) < 3:
            raise ValueError("reference column contains fewer than three values")
        return values, "measured_or_simulated_column"
    if key == "coulomb_count":
        return (coulomb_reference(samples, initial_soc, capacity_ah,
                                  charge_efficiency),
                "provisional_coulomb_count_not_independent_ground_truth")
    raise ValueError("reference source must be 'column' or 'coulomb_count'")


def replay_samples(samples: list[MeasurementSample], reference_soc: np.ndarray,
                   observer: SocObserver, score_start_fraction: float = .2
                   ) -> ReplayResult:
    if len(reference_soc) != len(samples):
        raise ValueError("reference SOC length does not match samples")
    rows = []
    for sample, reference in zip(samples, reference_soc):
        result = observer.process(sample.time_s, sample.voltage_v,
                                  sample.current_a)
        row = asdict(result)
        rc_voltages = row.pop("rc_voltages_v")
        for i, v in enumerate(rc_voltages, start=1):
            row[f"v{i}_v"] = v
        row["reference_soc"] = float(reference) if math.isfinite(reference) else math.nan
        row["soc_error"] = (result.soc-float(reference)
                            if math.isfinite(reference) else math.nan)
        rows.append(row)
    start = min(max(int(len(rows)*score_start_fraction), 0), len(rows)-1)
    scored = rows[start:]
    soc_error = np.asarray([r["soc_error"] for r in scored], dtype=float)
    soc_error = soc_error[np.isfinite(soc_error)]
    innovations = np.asarray([r["innovation_v"] for r in scored], dtype=float)
    innovations = innovations[np.isfinite(innovations)]
    nis = np.asarray([r["nis"] for r in scored], dtype=float)
    finite_nis = nis[np.isfinite(nis)]
    accepted = np.asarray([r["voltage_update_accepted"] for r in scored], dtype=bool)
    coverage = np.asarray([
        abs(r["soc_error"]) <= 2*r["soc_std"]
        for r in scored if math.isfinite(r["soc_error"])], dtype=bool)
    metrics = {
        "samples": len(rows), "scored_samples": len(scored),
        "score_start_fraction": score_start_fraction,
        "soc_rmse": float(np.sqrt(np.mean(soc_error**2))),
        "soc_mae": float(np.mean(np.abs(soc_error))),
        "soc_max_abs_error": float(np.max(np.abs(soc_error))),
        "soc_final_error": float(rows[-1]["soc_error"]),
        "soc_2sigma_coverage": float(np.mean(coverage)) if len(coverage) else math.nan,
        "voltage_innovation_rmse_v": float(np.sqrt(np.mean(innovations**2))),
        "voltage_innovation_mean_v": float(np.mean(innovations)),
        "nis_mean": float(np.mean(finite_nis)) if len(finite_nis) else math.nan,
        "nis_median": float(np.median(finite_nis)) if len(finite_nis) else math.nan,
        "voltage_rejection_fraction": float(1-np.mean(accepted)),
    }
    return ReplayResult(rows, metrics)


def replay_fields(num_branches: int) -> list[str]:
    return [
        "time_s", "dt_s", "current_a", "voltage_v", "reference_soc", "soc",
        "soc_error", "soc_std", *[f"v{i}_v" for i in range(1, num_branches+1)],
        "predicted_voltage_v", "innovation_v", "nis",
        "voltage_update_accepted", "status",
    ]


def write_replay_csv(path: str | Path, rows: list[dict[str, object]],
                     num_branches: int) -> None:
    fields = replay_fields(num_branches)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fields})


def _svg_lines(path: str | Path, title: str, ylabel: str,
               series: list[tuple[str, str, list[float], list[float]]]) -> None:
    width, height = 1000, 520
    left, right, top, bottom = 85, width-25, 45, height-65
    xs = [x for _, _, xx, _ in series for x in xx]
    ys = [y for _, _, _, yy in series for y in yy if math.isfinite(y)]
    xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
    if xmax == xmin: xmax += 1
    if ymax == ymin: ymax += 1
    ymin -= .06*(ymax-ymin); ymax += .06*(ymax-ymin)
    sx = lambda x: left+(x-xmin)/(xmax-xmin)*(right-left)
    sy = lambda y: bottom-(y-ymin)/(ymax-ymin)*(bottom-top)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
           '<rect width="100%" height="100%" fill="white"/>',
           f'<text x="{width/2}" y="25" text-anchor="middle" font-family="sans-serif" font-size="18">{title}</text>']
    for f in np.linspace(0, 1, 6):
        x, y = left+f*(right-left), bottom-f*(bottom-top)
        out += [f'<line x1="{x}" y1="{top}" x2="{x}" y2="{bottom}" stroke="#ddd"/>',
                f'<line x1="{left}" y1="{y}" x2="{right}" y2="{y}" stroke="#ddd"/>']
    for index, (name, color, xvals, yvals) in enumerate(series):
        points = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in zip(xvals, yvals)
                          if math.isfinite(y))
        out += [f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="1.7"/>',
                f'<text x="{right-150}" y="{top+18+index*18}" fill="{color}" font-family="sans-serif" font-size="12">{name}</text>']
    out += [f'<text x="{(left+right)/2}" y="{height-18}" text-anchor="middle" font-family="sans-serif">Time (s)</text>',
            f'<text x="18" y="{(top+bottom)/2}" text-anchor="middle" transform="rotate(-90 18 {(top+bottom)/2})" font-family="sans-serif">{ylabel}</text>', '</svg>']
    Path(path).write_text("\n".join(out), encoding="utf-8")


def write_diagnostics(folder: str | Path, replay: ReplayResult,
                      metadata: dict[str, object], num_branches: int) -> None:
    output = Path(folder); output.mkdir(parents=True, exist_ok=True)
    write_replay_csv(output/"offline_replay.csv", replay.rows, num_branches)
    payload = {**metadata, "metrics": replay.metrics}
    (output/"offline_metrics.json").write_text(
        json.dumps(_json_safe(payload), indent=2, allow_nan=False),
        encoding="utf-8")
    t = [float(r["time_s"]) for r in replay.rows]
    _svg_lines(output/"soc_replay.svg", "Offline SOC replay", "SOC (%)", [
        ("reference", "#111111", t, [100*float(r["reference_soc"]) for r in replay.rows]),
        ("estimate", "#1f77b4", t, [100*float(r["soc"]) for r in replay.rows])])
    _svg_lines(output/"voltage_replay.svg", "Measured and predicted voltage", "Voltage (V)", [
        ("measured", "#111111", t, [float(r["voltage_v"]) for r in replay.rows]),
        ("predicted", "#ff7f0e", t, [float(r["predicted_voltage_v"]) for r in replay.rows])])


def _json_safe(value):
    if isinstance(value, dict): return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)): return [_json_safe(item) for item in value]
    if isinstance(value, (float, np.floating)) and not math.isfinite(float(value)):
        return None
    if isinstance(value, np.generic): return value.item()
    return value
