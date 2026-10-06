"""Reproducible grid search for EKF/UKF process and measurement weights."""

from __future__ import annotations

import csv
import itertools
import json
import math
from dataclasses import dataclass
from pathlib import Path

from .model import TheveninEcmModel
from .observer import SocObserver
from .offline import (MeasurementSample, _json_safe, replay_samples,
                      write_diagnostics)


@dataclass
class TuningSpace:
    filter_types: tuple[str, ...] = ("ekf", "ukf")
    q_soc_per_s: tuple[float, ...] = (1e-11, 1e-10, 1e-9)
    # Process noise for the fastest RC branch; each slower branch gets
    # q_rc_fast_per_s * q_rc_decay_ratio**i (i = 0 for the fastest branch),
    # so the grid's size stays fixed regardless of the model's branch count.
    q_rc_fast_per_s: tuple[float, ...] = (1e-8, 1e-7, 1e-6)
    q_rc_decay_ratio: tuple[float, ...] = (.05, .1, .2)
    voltage_std_v: tuple[float, ...] = (.005, .010, .020)

    def candidates(self):
        for values in itertools.product(
                self.filter_types, self.q_soc_per_s, self.q_rc_fast_per_s,
                self.q_rc_decay_ratio, self.voltage_std_v):
            yield dict(zip(("filter_type", "q_soc_per_s", "q_rc_fast_per_s",
                            "q_rc_decay_ratio", "voltage_std_v"), values))


@dataclass
class TuningWeights:
    soc_rmse: float = 1.0
    nis_log_error: float = .002
    rejection_fraction: float = .05
    coverage_error: float = .01


def tuning_score(metrics: dict[str, object], weights: TuningWeights) -> float:
    """SOC dominates; NIS/rejection/coverage break near-ties robustly."""
    nis = max(float(metrics["nis_median"]), 1e-12)
    coverage = float(metrics["soc_2sigma_coverage"])
    if not math.isfinite(coverage): coverage = 0.0
    soc_rmse = float(metrics["soc_rmse"])
    if not math.isfinite(soc_rmse): return math.inf
    return (weights.soc_rmse*soc_rmse
            + weights.nis_log_error*abs(math.log(nis))
            + weights.rejection_fraction*float(metrics["voltage_rejection_fraction"])
            + weights.coverage_error*abs(coverage-.95))


def expand_q_rc_per_s(num_branches: int, q_rc_fast_per_s: float,
                      q_rc_decay_ratio: float) -> tuple[float, ...]:
    """Fastest branch first, each subsequent branch scaled by the ratio."""
    return tuple(q_rc_fast_per_s * q_rc_decay_ratio**i
                for i in range(num_branches))


def make_candidate_observer(model: TheveninEcmModel, candidate: dict,
                            fixed: dict) -> SocObserver:
    params = dict(candidate)
    q_rc_fast_per_s = params.pop("q_rc_fast_per_s")
    q_rc_decay_ratio = params.pop("q_rc_decay_ratio")
    params["q_rc_per_s"] = expand_q_rc_per_s(
        model.num_branches, q_rc_fast_per_s, q_rc_decay_ratio)
    return SocObserver(model=model, **fixed, **params)


def tune_filters(model: TheveninEcmModel,
                 samples: list[MeasurementSample], reference_soc,
                 space: TuningSpace, fixed_settings: dict,
                 score_start_fraction: float = .2,
                 weights: TuningWeights | None = None,
                 progress_every: int = 10):
    weights = weights or TuningWeights()
    ranked = []
    candidates = list(space.candidates())
    for number, candidate in enumerate(candidates, 1):
        record = dict(candidate)
        try:
            observer = make_candidate_observer(model, candidate, fixed_settings)
            replay = replay_samples(samples, reference_soc, observer,
                                    score_start_fraction)
            record.update(replay.metrics)
            record["score"] = tuning_score(replay.metrics, weights)
            record["status"] = "ok"
        except Exception as exc:
            record.update({"score": math.inf, "status": f"failed: {exc}"})
        ranked.append(record)
        if progress_every and (number % progress_every == 0 or number == len(candidates)):
            best = min(ranked, key=lambda row: float(row["score"]))
            print(f"tuning {number}/{len(candidates)}; best score={best['score']:.6g}")
    ranked.sort(key=lambda row: float(row["score"]))
    if not ranked or not math.isfinite(float(ranked[0]["score"])):
        raise RuntimeError("all tuning candidates failed")
    return ranked


def write_tuning_outputs(folder: str | Path, ranked: list[dict[str, object]],
                         model: TheveninEcmModel,
                         samples: list[MeasurementSample], reference_soc,
                         fixed_settings: dict, reference_kind: str,
                         score_start_fraction: float,
                         weights: TuningWeights,
                         space: TuningSpace) -> dict[str, object]:
    output = Path(folder); output.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in ranked for key in row))
    with (output/"tuning_results.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader(); writer.writerows(ranked)
    best_row = ranked[0]
    candidate_keys = tuple(next(space.candidates()).keys())
    best_candidate = {key: best_row[key] for key in candidate_keys}
    q_rc_fast_per_s = best_candidate.pop("q_rc_fast_per_s")
    q_rc_decay_ratio = best_candidate.pop("q_rc_decay_ratio")
    # Stored as a list, not a tuple: this dict is round-tripped through JSON
    # (best_kf_config.json), where tuples come back as lists anyway.
    q_rc_per_s = list(expand_q_rc_per_s(model.num_branches, q_rc_fast_per_s,
                                        q_rc_decay_ratio))
    best_settings = {**fixed_settings, **best_candidate,
                     "q_rc_per_s": q_rc_per_s}
    export = {
        "description": "Best offline BMS tuning candidate",
        "reference_kind": reference_kind,
        "warning": ("Coulomb-count reference is provisional and is not an "
                    "independent validation of SOC accuracy."
                    if reference_kind.startswith("provisional") else None),
        "score_start_fraction": score_start_fraction,
        "score_weights": vars(weights),
        "observer_settings": best_settings,
        "best_metrics": {key: value for key, value in best_row.items()
                         if key not in candidate_keys and key != "status"},
    }
    (output/"best_kf_config.json").write_text(
        json.dumps(_json_safe(export), indent=2, allow_nan=False),
        encoding="utf-8")
    best_observer = SocObserver(model=model, **best_settings)
    best_replay = replay_samples(samples, reference_soc, best_observer,
                                 score_start_fraction)
    write_diagnostics(output/"best_replay", best_replay, {
        "reference_kind": reference_kind,
        "observer_settings": best_settings}, model.num_branches)
    return export
