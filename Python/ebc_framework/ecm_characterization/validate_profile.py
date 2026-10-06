#!/usr/bin/env python3
"""Play a fitted ECM open-loop against a recorded voltage/current profile.

Reads an `ecm_parameters.json` produced by `characterize_hppc` and a logger
CSV from a different run, then simulates
`V = OCV(SOC) - I*R0(SOC) - V1` with `V1` driven by the recorded current
(no voltage feedback) and compares the result to the measured voltage. This
is deliberately offline and never imports device code.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import numpy as np

from .characterize_hppc import (Samples, _svg_chart, coulomb_count_soc,
                                load_samples)


@dataclass
class ValidationConfig:
    profile_csv: str
    ecm_json: str
    output_dir: str
    initial_soc: float
    direction: str = "auto"
    capacity_ah: float | None = None
    current_threshold_a: float = 0.05

    def validate(self) -> None:
        if not 0 <= self.initial_soc <= 1:
            raise ValueError("initial_soc must be between 0 and 1")
        if self.capacity_ah is not None and self.capacity_ah <= 0:
            raise ValueError("capacity_ah must be positive")
        if self.direction not in ("auto", "charge", "discharge"):
            raise ValueError("direction must be auto, charge, or discharge")


@dataclass
class EcmLookup:
    direction: str
    soc: np.ndarray
    ocv_v: np.ndarray
    r0_ohm: np.ndarray
    r_ohm: list[np.ndarray]    # one SOC-aligned array per branch, fastest first
    tau_s: list[np.ndarray]

    def _interp(self, table: np.ndarray, soc: float) -> float:
        return float(np.interp(soc, self.soc, table))

    def ocv(self, soc: float) -> float: return self._interp(self.ocv_v, soc)
    def r0(self, soc: float) -> float: return self._interp(self.r0_ohm, soc)
    def r(self, branch: int, soc: float) -> float:
        return self._interp(self.r_ohm[branch], soc)
    def tau(self, branch: int, soc: float) -> float:
        return self._interp(self.tau_s[branch], soc)

    @property
    def num_branches(self) -> int: return len(self.r_ohm)


def load_ecm(path: str, direction: str) -> tuple[dict[str, EcmLookup], float]:
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    by_direction: dict[str, list[dict[str, object]]] = {}
    for row in raw["lookup"]:
        by_direction.setdefault(str(row["direction"]), []).append(row)
    if direction != "auto" and direction not in by_direction:
        raise ValueError(
            f"direction {direction!r} not in ECM lookup; available: "
            f"{sorted(by_direction)}")
    num_branches = int(raw["num_rc_branches"])
    tables = {}
    for name, rows in by_direction.items():
        rows = sorted(rows, key=lambda r: float(r["soc"]))
        r_ohm = [np.asarray([float(r["rc_branches"][i]["r_ohm"]) for r in rows])
                for i in range(num_branches)]
        tau_s = [np.asarray([float(r["rc_branches"][i]["tau_s"]) for r in rows])
                for i in range(num_branches)]
        tables[name] = EcmLookup(
            name,
            np.asarray([float(r["soc"]) for r in rows]),
            np.asarray([float(r["ocv_relaxed_v"]) for r in rows]),
            np.asarray([float(r["r0_ohm"]) for r in rows]),
            r_ohm, tau_s)
    capacity_ah = float(raw["configuration"]["capacity_ah"])
    return tables, capacity_ah


def pick_direction(tables: dict[str, EcmLookup], requested: str,
                   data: Samples) -> tuple[EcmLookup, str | None]:
    """Resolve which direction table drives the whole profile.

    Returns the chosen table and a note describing any fallback, so a
    discharge-only ECM validated against a charge profile (or vice versa)
    is never silently misrepresented as a matched-direction check.
    """
    if requested != "auto":
        return tables[requested], None
    mean_current = float(np.mean(data.current))
    wanted = "discharge" if mean_current >= 0 else "charge"
    if wanted in tables:
        return tables[wanted], None
    (fallback_name, fallback), = tables.items()
    return fallback, (
        f"profile is predominantly {wanted}, but the ECM has no "
        f"{wanted}-direction lookup; using {fallback_name} parameters "
        f"for the whole profile")


def simulate_voltage(data: Samples, soc: np.ndarray,
                     lookup: EcmLookup) -> np.ndarray:
    """Open-loop playback: current drives SOC and every RC branch voltage,
    never corrected against the measured voltage."""
    predicted = np.empty(len(data.t))
    v = [0.0] * lookup.num_branches
    clamped_soc = np.clip(soc, 0.0, 1.0)
    predicted[0] = (lookup.ocv(clamped_soc[0])
                    - lookup.r0(clamped_soc[0]) * data.current[0] - sum(v))
    for i in range(1, len(data.t)):
        dt = data.t[i] - data.t[i - 1]
        s0 = clamped_soc[i - 1]
        for b in range(lookup.num_branches):
            tau = lookup.tau(b, s0)
            a = math.exp(-dt / tau) if tau > 0 else 0.0
            v[b] = a * v[b] + lookup.r(b, s0) * (1 - a) * data.current[i]
        predicted[i] = (lookup.ocv(clamped_soc[i])
                        - lookup.r0(clamped_soc[i]) * data.current[i] - sum(v))
    return predicted


COMPARISON_FIELDS = ["t_s", "current_a", "soc", "voltage_measured_v",
                     "voltage_predicted_v", "error_v"]


def write_comparison_csv(path: Path, data: Samples, soc: np.ndarray,
                         predicted: np.ndarray) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COMPARISON_FIELDS)
        writer.writeheader()
        for i in range(len(data.t)):
            writer.writerow({
                "t_s": f"{data.t[i]:.3f}", "current_a": f"{data.current[i]:.6g}",
                "soc": f"{soc[i]:.6g}", "voltage_measured_v": f"{data.voltage[i]:.6g}",
                "voltage_predicted_v": f"{predicted[i]:.6g}",
                "error_v": f"{predicted[i] - data.voltage[i]:.6g}"})


def compute_metrics(data: Samples, predicted: np.ndarray) -> dict[str, float]:
    error = predicted - data.voltage
    worst = int(np.argmax(np.abs(error)))
    return {
        "rmse_v": float(np.sqrt(np.mean(error ** 2))),
        "mae_v": float(np.mean(np.abs(error))),
        "bias_v": float(np.mean(error)),
        "max_abs_error_v": float(np.abs(error[worst])),
        "max_abs_error_t_s": float(data.t[worst]),
    }


def validate(cfg: ValidationConfig) -> dict[str, object]:
    cfg.validate()
    profile_path = Path(cfg.profile_csv).resolve()
    output = Path(cfg.output_dir).resolve()
    if not profile_path.is_file():
        raise FileNotFoundError(f"profile CSV not found: {profile_path}")
    output.mkdir(parents=True, exist_ok=True)

    tables, ecm_capacity_ah = load_ecm(cfg.ecm_json, cfg.direction)
    capacity_ah = cfg.capacity_ah or ecm_capacity_ah
    data = load_samples(str(profile_path), cfg.current_threshold_a)
    soc = coulomb_count_soc(data, capacity_ah, cfg.initial_soc)
    lookup, direction_note = pick_direction(tables, cfg.direction, data)
    predicted = simulate_voltage(data, soc, lookup)

    write_comparison_csv(output / "voltage_comparison.csv", data, soc, predicted)
    _svg_chart(
        output / "voltage_comparison.svg", "Measured vs ECM-predicted voltage",
        "Voltage (V)",
        [("measured", "#111111", data.t.tolist(), data.voltage.tolist()),
         ("predicted", "#ff7f0e", data.t.tolist(), predicted.tolist())],
        xlabel="Time (s)", width=1100)
    _svg_chart(
        output / "voltage_error.svg", "Prediction error (predicted - measured)",
        "Error (V)",
        [("error", "#d62728", data.t.tolist(), (predicted - data.voltage).tolist())],
        xlabel="Time (s)", width=1100)

    out_of_range = int(np.sum((soc < 0) | (soc > 1)))
    metrics = compute_metrics(data, predicted)
    branch_terms = " - ".join(f"[R{i}(SOC) || C{i}(SOC)]"
                             for i in range(1, lookup.num_branches + 1))
    export = {
        "model": f"OCV(SOC) - R0(SOC) - {branch_terms}, open-loop",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "profile_csv": str(profile_path),
        "ecm_json": str(Path(cfg.ecm_json).resolve()),
        "direction_used": lookup.direction,
        "direction_note": direction_note,
        "configuration": asdict(cfg),
        "capacity_ah": capacity_ah,
        "input_quality": {
            "raw_rows": data.raw_rows, "valid_unique_rows": len(data.t),
            "invalid_rows": data.invalid_rows,
            "duplicate_rows_replaced": data.duplicate_rows,
            "resumed_timestamp_segments": data.resumed_segments,
            "median_sample_interval_s": data.median_dt_s,
            "soc_samples_out_of_0_1_range": out_of_range},
        "metrics": metrics,
    }
    with (output / "validation_metrics.json").open("w", encoding="utf-8") as fh:
        json.dump(export, fh, indent=2, allow_nan=False)
    return export


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile_csv")
    parser.add_argument("--ecm-json", required=True)
    parser.add_argument("--initial-soc", type=float, required=True)
    parser.add_argument("--direction", choices=("auto", "charge", "discharge"),
                        default="auto")
    parser.add_argument("--capacity-ah", type=float, default=None)
    parser.add_argument("--current-threshold-a", type=float, default=.05)
    parser.add_argument("--output-dir", required=True)
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> None:
    args = parse_args(argv)
    result = validate(ValidationConfig(
        profile_csv=args.profile_csv, ecm_json=args.ecm_json,
        output_dir=args.output_dir, initial_soc=args.initial_soc,
        direction=args.direction, capacity_ah=args.capacity_ah,
        current_threshold_a=args.current_threshold_a))
    if result["direction_note"]:
        print(f"NOTE: {result['direction_note']}")
    print(f"Validation complete: {result['metrics']}")
    print(f"Outputs: {Path(args.output_dir).resolve()}")


if __name__ == "__main__":
    main()
