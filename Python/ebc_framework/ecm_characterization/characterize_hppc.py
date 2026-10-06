#!/usr/bin/env python3
"""Estimate an OCV-R0-(N RC branches) model from an EBC HPPC CSV.

This module is deliberately offline: it only reads a logger CSV and writes
derived files to a separate output directory. It does not import device code.
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

try:
    # Flat sibling import: works when this file runs standalone (as a
    # script, or via selftest_characterization.py's own flat import) --
    # matches every other cross-file reference in this package.
    from pseudo_ocv import invert_ocv_to_soc
except ImportError:
    # Package-relative fallback: needed when imported as
    # ecm_characterization.characterize_hppc (e.g. runCharacterizationVSCode.py),
    # where the package's own directory isn't on sys.path.
    from .pseudo_ocv import invert_ocv_to_soc


@dataclass
class CharacterizationConfig:
    input_csv: str
    output_dir: str
    capacity_ah: float
    initial_soc: float = 1.0
    expected_pulse_s: float = 60.0
    current_threshold_a: float = 0.1
    relaxation_window_s: tuple[float, float] = (1.0, 300.0)
    soc_bin_width: float = 0.05
    pre_rest_window_s: float = 30.0
    duration_warn_fraction: float = 0.20
    duration_reject_fraction: float = 0.50
    max_current_cv: float = 0.10
    max_fit_rmse_v: float = 0.020
    max_gap_factor: float = 5.0
    min_pulse_samples: int = 4
    min_relaxation_samples: int = 8
    reject_terminal_cutoff_pulse: bool = True
    num_rc_branches: int = 2
    min_adjacent_tau_ratio: float = 2.5
    # A fitted tau within this fraction of the search grid's own upper bound
    # (~10x the relaxation window) isn't a real estimate -- the data couldn't
    # bound it, so the search just grabbed the largest candidate available.
    tau_boundary_reject_fraction: float = 0.95
    # Optional pseudo_ocv.json (see pseudo_ocv.py). When set: overrides
    # capacity_ah with the measured value, anchors each pulse's soc_parameter
    # to the pseudo-OCV curve instead of coulomb-counting, and attempts a
    # fixed-OCV relaxation fit -- adopted only if it agrees with the free fit
    # within the tolerances below (see analyze_pulse).
    pseudo_ocv_json: str | None = None
    ocv_anchor_max_mismatch_v: float = 0.010
    ocv_anchor_max_rmse_increase_frac: float = 0.5
    min_ocv_slope_v_per_soc: float = 0.05

    def validate(self) -> None:
        if not math.isfinite(self.capacity_ah) or self.capacity_ah <= 0:
            raise ValueError("capacity_ah must be positive")
        if not 0 <= self.initial_soc <= 1:
            raise ValueError("initial_soc must be between 0 and 1")
        if self.expected_pulse_s <= 0 or self.current_threshold_a <= 0:
            raise ValueError("pulse duration and current threshold must be positive")
        start, end = self.relaxation_window_s
        if start < 0 or end <= start:
            raise ValueError("relaxation_window_s must be (start, end), end > start")
        if not 0 < self.soc_bin_width <= 1:
            raise ValueError("soc_bin_width must be in (0, 1]")
        if not 1 <= self.num_rc_branches <= 4:
            raise ValueError("num_rc_branches must be between 1 and 4")
        if self.min_adjacent_tau_ratio <= 1:
            raise ValueError("min_adjacent_tau_ratio must be greater than 1")
        if not 0 < self.tau_boundary_reject_fraction <= 1:
            raise ValueError("tau_boundary_reject_fraction must be in (0, 1]")
        if self.ocv_anchor_max_mismatch_v <= 0:
            raise ValueError("ocv_anchor_max_mismatch_v must be positive")
        if self.ocv_anchor_max_rmse_increase_frac <= 0:
            raise ValueError("ocv_anchor_max_rmse_increase_frac must be positive")
        if self.min_ocv_slope_v_per_soc <= 0:
            raise ValueError("min_ocv_slope_v_per_soc must be positive")
        if self.num_rc_branches > 2:
            print(f"WARNING: fitting {self.num_rc_branches} RC branches from "
                  f"~{end:g}s of real relaxation data at typical HPPC noise "
                  "levels is fragile -- recovering more than 2 well-separated "
                  "time constants needs strong data, not just this setting.")

    @property
    def effective_min_adjacent_tau_ratio(self) -> float:
        return (max(self.min_adjacent_tau_ratio, 3.0)
                if self.num_rc_branches >= 3 else self.min_adjacent_tau_ratio)


@dataclass
class Samples:
    t: np.ndarray
    voltage: np.ndarray
    current: np.ndarray  # exported ECM convention: discharge positive
    mode: list[str]
    raw_rows: int
    invalid_rows: int
    duplicate_rows: int
    resumed_segments: int
    median_dt_s: float


@dataclass
class Pulse:
    pulse_id: int
    start: int
    end: int
    rest_end: int
    direction: str


def pulse_fields(num_branches: int) -> list[str]:
    fields = [
        "pulse_id", "direction", "quality_status", "quality_reasons",
        "start_time_s", "end_time_s", "duration_s", "duration_ratio",
        "samples", "rest_samples", "soc_start", "soc_end", "soc_parameter",
        "soc_from_ocv", "soc_parameter_source",
        "current_mean_a", "current_std_a", "current_cv", "ocv_pre_v",
        "voltage_initial_v", "voltage_end_v", "ocv_relaxed_v",
        "ocv_relaxed_fixed_v", "ocv_anchor_adopted", "r0_ohm",
    ]
    for i in range(1, num_branches + 1):
        fields += [f"r{i}_ohm", f"tau{i}_s", f"c{i}_f"]
    fields += ["relaxation_rmse_v", "relaxation_rmse_fixed_v", "relaxation_r2",
              "max_sample_gap_s", "missing_gap_count", "fit_samples"]
    return fields


def _finite(value: str | None) -> float:
    try:
        result = float(value) if value not in (None, "") else math.nan
    except (TypeError, ValueError):
        return math.nan
    return result if math.isfinite(result) else math.nan


def load_samples(path: str, threshold_a: float) -> Samples:
    """Load, de-duplicate, and stitch timestamp resets caused by resumes."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        headers = set(reader.fieldnames or [])
        missing = {"t_s", "voltage_v"} - headers
        if missing:
            raise ValueError(f"CSV is missing columns: {', '.join(sorted(missing))}")
        signed_column = ("signed_current_a" if "signed_current_a" in headers
                         else "current_a" if "current_a" in headers else None)
        if signed_column is None:
            raise ValueError("CSV needs signed_current_a or current_a")
        raw = list(reader)

    cleaned: list[tuple[float, float, float, str]] = []
    invalid = duplicate = resumed = 0
    offset = 0.0
    previous_raw: float | None = None
    positive_dts: list[float] = []
    for row in raw:
        raw_t = _finite(row.get("t_s"))
        voltage = _finite(row.get("voltage_v"))
        logged_i = _finite(row.get(signed_column))
        if not all(math.isfinite(v) for v in (raw_t, voltage, logged_i)):
            invalid += 1
            continue
        mode = (row.get("mode") or "").strip().lower()
        if signed_column == "current_a":
            if mode == "discharge":
                logged_i = -abs(logged_i)
            elif mode == "charge":
                logged_i = abs(logged_i)
            elif abs(logged_i) <= threshold_a:
                logged_i = 0.0
        if previous_raw is not None:
            delta = raw_t - previous_raw
            if delta > 1e-9:
                positive_dts.append(delta)
            elif delta < -1e-9:
                nominal = float(np.median(positive_dts[-100:])) if positive_dts else 1.0
                offset = cleaned[-1][0] + nominal - raw_t
                resumed += 1
        t = raw_t + offset
        previous_raw = raw_t
        item = (t, voltage, -logged_i, mode)  # EBC sign -> ECM sign
        if cleaned and abs(t - cleaned[-1][0]) <= 1e-9:
            cleaned[-1] = item  # later status at identical time wins
            duplicate += 1
        else:
            cleaned.append(item)

    if len(cleaned) < 3:
        raise ValueError("need at least three valid, unique samples")
    times = np.asarray([row[0] for row in cleaned], dtype=float)
    dts = np.diff(times)
    if np.any(dts <= 0):
        raise ValueError("timestamps could not be made strictly increasing")
    return Samples(
        times, np.asarray([r[1] for r in cleaned]),
        np.asarray([r[2] for r in cleaned]), [r[3] for r in cleaned],
        len(raw), invalid, duplicate, resumed, float(np.median(dts)))


def coulomb_count_soc(data: Samples, capacity_ah: float,
                      initial_soc: float) -> np.ndarray:
    """Integrate variable intervals; positive ECM current decreases SOC."""
    soc = np.empty(len(data.t), dtype=float)
    soc[0] = initial_soc
    for index in range(1, len(soc)):
        dt = data.t[index] - data.t[index - 1]
        delta_ah = .5 * (data.current[index - 1] + data.current[index]) * dt / 3600
        soc[index] = soc[index - 1] - delta_ah / capacity_ah
    return soc


def detect_pulses(data: Samples, threshold_a: float) -> list[Pulse]:
    active = np.abs(data.current) >= threshold_a
    pulses: list[Pulse] = []
    index = 0
    while index < len(active):
        if not active[index]:
            index += 1
            continue
        start = index
        sign = 1 if data.current[index] > 0 else -1
        while (index + 1 < len(active) and active[index + 1]
               and (1 if data.current[index + 1] > 0 else -1) == sign):
            index += 1
        end = index
        rest_end = end
        while rest_end + 1 < len(active) and not active[rest_end + 1]:
            rest_end += 1
        pulses.append(Pulse(len(pulses) + 1, start, end, rest_end,
                            "discharge" if sign > 0 else "charge"))
        index += 1
    return pulses


def _solve_fixed_taus(x: np.ndarray, voltage: np.ndarray, taus: list[float],
                      fixed_ocv: float | None = None):
    """Solve the linear sub-problem for fixed taus.

    Free-OCV mode (default, unchanged): solves for [ocv, amplitudes...] on
    basis [1, exp(-x/tau_1), ...]. Fixed-OCV mode (`fixed_ocv` given): drops
    the constant column and solves only for the amplitudes against
    `voltage - fixed_ocv` -- one fewer degree of freedom, since the caller
    already knows the true asymptote from an independent measurement.
    """
    if fixed_ocv is None:
        basis = np.column_stack([np.ones(len(x))] + [np.exp(-x / tau) for tau in taus])
        target = voltage
    else:
        basis = np.column_stack([np.exp(-x / tau) for tau in taus])
        target = voltage - fixed_ocv
    coef, _, rank, _ = np.linalg.lstsq(basis, target, rcond=None)
    prediction = basis @ coef + (0.0 if fixed_ocv is None else fixed_ocv)
    sse = float(np.sum((voltage - prediction) ** 2))
    return sse, coef, rank, basis.shape[1]


def _grid_search_one_tau(x: np.ndarray, voltage: np.ndarray, taus: list[float],
                         index: int, lo: float, hi: float, min_ratio: float,
                         fixed_ocv: float | None = None, grid_size: int = 400):
    """Grid-search taus[index], holding the other taus fixed.

    Candidates that would put taus[index] within min_ratio of any other
    branch are skipped first; if every candidate is excluded this way (can
    happen mid coordinate-descent, before branches have separated), the
    search is retried without that constraint so the round can still
    proceed -- the final result is flagged as collapsed by the caller.
    """
    others = [taus[j] for j in range(len(taus)) if j != index]

    def search(respect_ratio: bool):
        best = None
        for candidate in np.geomspace(lo, hi, grid_size):
            if respect_ratio and any(
                    max(candidate, o) / min(candidate, o) < min_ratio
                    for o in others):
                continue
            trial = list(taus); trial[index] = candidate
            sse, coef, rank, ncols = _solve_fixed_taus(x, voltage, trial, fixed_ocv)
            if rank < ncols:
                continue  # rank-deficient basis: two taus are ill-conditioned
            if best is None or sse < best[0]:
                best = (sse, candidate)
        return best

    return search(True) or search(False)


def fit_relaxation(t: np.ndarray, voltage: np.ndarray, num_branches: int = 1,
                   min_adjacent_tau_ratio: float = 2.5,
                   fixed_ocv: float | None = None) -> dict[str, object]:
    """Fit V = ocv + sum_i A_i*exp(-t/tau_i) via coordinate-descent VARPRO.

    Grid-searches one tau at a time, holding the rest fixed, solving the
    linear (ocv + all amplitudes) sub-problem by lstsq at every candidate --
    a direct generalization of the single-branch grid search this replaces.
    Multi-start (several differently-biased initial tau spacings) guards
    against coordinate descent settling into a local optimum. Branches are
    returned fastest-first (ascending tau).

    `fixed_ocv`, when given, pins the asymptote to a known value (e.g. from
    an independent pseudo-OCV curve) instead of fitting it freely -- see
    analyze_pulse's dual free/fixed fit for why the caller must not adopt
    this result unconditionally.
    """
    nan_result = {"ocv": math.nan, "amplitudes": [math.nan] * num_branches,
                 "taus": [math.nan] * num_branches, "rmse": math.nan,
                 "r2": math.nan, "branch_collapse": True, "grid_hi": math.nan}
    if len(t) < 2 * num_branches + 1 or t[-1] <= t[0]:
        return nan_result
    x = t - t[0]
    lo = max(float(np.median(np.diff(t))) * .25, .05)
    hi = max(float(x[-1]) * 10, lo * 10)

    if num_branches == 1:
        starts = [[math.sqrt(lo * hi)]]
    else:
        starts = [
            np.geomspace(lo, hi, num_branches).tolist(),          # even spread
            np.geomspace(lo, hi * .3, num_branches).tolist(),     # early-biased
            np.geomspace(lo * 3, hi, num_branches).tolist(),      # late-biased
        ]

    best_overall = None
    for init_taus in starts:
        taus = list(init_taus)
        for _round in range(8):
            max_rel_change = 0.0
            for i in sorted(range(num_branches), key=lambda k: taus[k]):
                result = _grid_search_one_tau(
                    x, voltage, taus, i, lo, hi, min_adjacent_tau_ratio, fixed_ocv)
                if result is None:
                    continue
                _, candidate = result
                max_rel_change = max(
                    max_rel_change, abs(candidate - taus[i]) / max(taus[i], 1e-9))
                taus[i] = candidate
            if max_rel_change < .01:
                break
        sorted_taus = sorted(taus)
        sse, coef, rank, ncols = _solve_fixed_taus(x, voltage, sorted_taus, fixed_ocv)
        collapsed = rank < ncols or (num_branches > 1 and any(
            sorted_taus[k + 1] / sorted_taus[k] < min_adjacent_tau_ratio
            for k in range(num_branches - 1)))
        candidate_result = (sse, sorted_taus, coef, collapsed)
        if best_overall is None:
            best_overall = candidate_result
        else:
            _, _, _, best_collapsed = best_overall
            if (best_collapsed and not collapsed) or (
                    collapsed == best_collapsed and sse < best_overall[0]):
                best_overall = candidate_result

    sse, sorted_taus, coef, collapsed = best_overall
    total = float(np.sum((voltage - np.mean(voltage)) ** 2))
    ocv = fixed_ocv if fixed_ocv is not None else float(coef[0])
    amplitudes = [float(c) for c in coef] if fixed_ocv is not None else [float(c) for c in coef[1:]]
    return {"ocv": ocv, "amplitudes": amplitudes,
            "taus": sorted_taus, "rmse": math.sqrt(sse / len(voltage)),
            "r2": 1 - sse / total if total > 1e-15 else math.nan,
            "branch_collapse": collapsed, "grid_hi": hi}


def analyze_pulse(data: Samples, soc: np.ndarray, pulse: Pulse,
                  cfg: CharacterizationConfig,
                  ocv_curve: tuple[np.ndarray, np.ndarray] | None = None
                  ) -> dict[str, object]:
    start, end = pulse.start, pulse.end
    duration = data.t[end] - data.t[start] + data.median_dt_s
    duration_ratio = duration / cfg.expected_pulse_s
    pulse_i = data.current[start:end + 1]
    mean_i, std_i = float(np.mean(pulse_i)), float(np.std(pulse_i))
    current_cv = std_i / abs(mean_i) if mean_i else math.inf

    pre_idx = np.where((data.t < data.t[start]) &
                       (data.t >= data.t[start] - cfg.pre_rest_window_s) &
                       (np.abs(data.current) < cfg.current_threshold_a))[0]
    ocv_pre = float(np.median(data.voltage[pre_idx])) if len(pre_idx) else math.nan
    initial_span = max(2 * data.median_dt_s, 1.0)
    initial_idx = np.where((data.t >= data.t[start]) &
                           (data.t <= data.t[start] + initial_span))[0]
    initial_idx = initial_idx[initial_idx <= end]
    voltage_initial = float(np.median(data.voltage[initial_idx])) if len(initial_idx) else math.nan
    voltage_end = float(data.voltage[end])
    r0 = ((ocv_pre - voltage_initial) / mean_i
          if mean_i and math.isfinite(ocv_pre) else math.nan)

    # Pseudo-OCV anchoring (Tiers 2-3, see pseudo_ocv.py): invert the real
    # pre-pulse rest voltage through the measured OCV(SOC) curve to get an
    # independent SOC estimate that sidesteps coulomb-count drift entirely.
    soc_from_ocv = math.nan
    fixed_ocv_value: float | None = None
    if ocv_curve is not None:
        soc_curve_arr, ocv_curve_arr = ocv_curve
        soc_from_ocv = invert_ocv_to_soc(
            ocv_pre, soc_curve_arr, ocv_curve_arr, cfg.min_ocv_slope_v_per_soc)
        if math.isfinite(soc_from_ocv):
            # The relaxation fit's asymptote corresponds to the pulse's
            # ENDING soc (post-pulse), not the pre-pulse anchor above -- shift
            # by the coulomb-counted delta across the pulse itself, which is
            # trustworthy over a single ~60s pulse even though coulomb-count
            # drift over the whole multi-hour test is not.
            soc_end_anchored = soc_from_ocv + (soc[end] - soc[start])
            if soc_curve_arr[0] <= soc_end_anchored <= soc_curve_arr[-1]:
                fixed_ocv_value = float(
                    np.interp(soc_end_anchored, soc_curve_arr, ocv_curve_arr))
    soc_parameter_source = "ocv" if math.isfinite(soc_from_ocv) else "coulomb"
    soc_parameter = (soc_from_ocv if math.isfinite(soc_from_ocv)
                     else .5 * (soc[start] + soc[end]))

    rest_first = end + 1
    if rest_first <= pulse.rest_end:
        relative = data.t[rest_first:pulse.rest_end + 1] - data.t[rest_first]
        mask = ((relative >= cfg.relaxation_window_s[0]) &
                (relative <= cfg.relaxation_window_s[1]))
        fit_idx = np.arange(rest_first, pulse.rest_end + 1)[mask]
    else:
        fit_idx = np.asarray([], dtype=int)
    fit = fit_relaxation(data.t[fit_idx], data.voltage[fit_idx],
                        cfg.num_rc_branches, cfg.effective_min_adjacent_tau_ratio)

    ocv_relaxed_fixed_v = math.nan
    relaxation_rmse_fixed_v = math.nan
    ocv_anchor_adopted = False
    ocv_anchor_mismatch = False
    if fixed_ocv_value is not None and len(fit_idx) > 0:
        fit_fixed = fit_relaxation(
            data.t[fit_idx], data.voltage[fit_idx], cfg.num_rc_branches,
            cfg.effective_min_adjacent_tau_ratio, fixed_ocv=fixed_ocv_value)
        ocv_relaxed_fixed_v = fixed_ocv_value
        relaxation_rmse_fixed_v = fit_fixed["rmse"]
        mismatch_v = (abs(fit["ocv"] - fixed_ocv_value)
                     if math.isfinite(fit["ocv"]) else math.inf)
        rmse_increase = (
            (fit_fixed["rmse"] - fit["rmse"]) / fit["rmse"]
            if math.isfinite(fit["rmse"]) and fit["rmse"] > 0 else math.inf)
        # A fixed asymptote the exponential terms can't represent (wrong by
        # even a little) tends to push tau back toward the search-grid
        # boundary -- the same degeneracy this is meant to fix, just silent.
        # Only adopt it when it agrees with the independently-fit free
        # asymptote AND doesn't make the fit itself meaningfully worse.
        if (mismatch_v <= cfg.ocv_anchor_max_mismatch_v
                and rmse_increase <= cfg.ocv_anchor_max_rmse_increase_frac):
            fit = fit_fixed
            ocv_anchor_adopted = True
        else:
            ocv_anchor_mismatch = True

    branches = []
    for tau_i, amplitude_i in zip(fit["taus"], fit["amplitudes"]):
        denominator = (mean_i * (1 - math.exp(-duration / tau_i))
                       if mean_i and math.isfinite(tau_i) and tau_i > 0
                       else math.nan)
        r_i = (-amplitude_i / denominator
              if denominator and math.isfinite(denominator) else math.nan)
        c_i = tau_i / r_i if math.isfinite(r_i) and r_i > 0 else math.nan
        branches.append({"r_ohm": r_i, "tau_s": tau_i, "c_f": c_i})

    local_dts = np.diff(data.t[start:pulse.rest_end + 1])
    max_gap = float(np.max(local_dts)) if len(local_dts) else 0.0
    missing_gaps = int(np.sum(local_dts > cfg.max_gap_factor * data.median_dt_s))
    reject, warn = [], []
    deviation = abs(duration_ratio - 1)
    if deviation > cfg.duration_reject_fraction:
        reject.append("pulse_duration_abnormal")
    elif deviation > cfg.duration_warn_fraction:
        warn.append("pulse_duration_deviation")
    if end - start + 1 < cfg.min_pulse_samples:
        reject.append("too_few_pulse_samples")
    if len(pre_idx) == 0:
        reject.append("no_pre_pulse_rest")
    if len(fit_idx) < cfg.min_relaxation_samples:
        reject.append("too_few_relaxation_samples")
    if current_cv > cfg.max_current_cv:
        warn.append("unstable_pulse_current")
    if missing_gaps:
        warn.append("missing_samples_or_large_gaps")
    if math.isfinite(fit["rmse"]) and fit["rmse"] > cfg.max_fit_rmse_v:
        warn.append("high_relaxation_fit_error")
    if fit["branch_collapse"]:
        warn.append("rc_branch_tau_ratio_below_minimum")
    if ocv_anchor_mismatch:
        warn.append("ocv_anchor_mismatch")
    # A tau sitting at (or within a hair of) the search grid's own upper
    # bound isn't a real estimate: the relaxation window couldn't bound it,
    # so the search just grabbed the largest candidate on offer, and the
    # matching amplitude/R can come out arbitrarily (and implausibly) large.
    grid_hi = fit["grid_hi"]
    if math.isfinite(grid_hi) and any(
            math.isfinite(tau_i) and tau_i >= cfg.tau_boundary_reject_fraction * grid_hi
            for tau_i in fit["taus"]):
        reject.append("tau_at_search_grid_boundary")
    branch_values = [v for branch in branches for v in
                     (branch["r_ohm"], branch["tau_s"], branch["c_f"])]
    if not all(math.isfinite(v) and v > 0 for v in (r0, *branch_values)):
        reject.append("nonphysical_or_unavailable_parameters")
    status = "rejected" if reject else ("flagged" if warn else "accepted")
    row = {
        "pulse_id": pulse.pulse_id, "direction": pulse.direction,
        "quality_status": status, "quality_reasons": ";".join(reject + warn),
        "start_time_s": data.t[start], "end_time_s": data.t[end],
        "duration_s": duration, "duration_ratio": duration_ratio,
        "samples": end - start + 1, "rest_samples": pulse.rest_end - end,
        "soc_start": soc[start], "soc_end": soc[end],
        "soc_parameter": soc_parameter, "soc_from_ocv": soc_from_ocv,
        "soc_parameter_source": soc_parameter_source,
        "current_mean_a": mean_i, "current_std_a": std_i,
        "current_cv": current_cv, "ocv_pre_v": ocv_pre,
        "voltage_initial_v": voltage_initial, "voltage_end_v": voltage_end,
        "ocv_relaxed_v": fit["ocv"], "ocv_relaxed_fixed_v": ocv_relaxed_fixed_v,
        "ocv_anchor_adopted": ocv_anchor_adopted, "r0_ohm": r0,
        "relaxation_rmse_v": fit["rmse"],
        "relaxation_rmse_fixed_v": relaxation_rmse_fixed_v,
        "relaxation_r2": fit["r2"],
        "max_sample_gap_s": max_gap, "missing_gap_count": missing_gaps,
        "fit_samples": len(fit_idx), "_fit_idx": fit_idx,
        "_fit_amplitudes": fit["amplitudes"], "_fit_taus": fit["taus"],
        "_pulse": pulse,
    }
    for i, branch in enumerate(branches, start=1):
        row[f"r{i}_ohm"] = branch["r_ohm"]
        row[f"tau{i}_s"] = branch["tau_s"]
        row[f"c{i}_f"] = branch["c_f"]
    return row


def _csv_value(value: object) -> object:
    if isinstance(value, (float, np.floating)):
        return "" if not math.isfinite(float(value)) else f"{float(value):.9g}"
    return value


def write_pulse_csv(path: Path, rows: list[dict[str, object]],
                    num_branches: int) -> None:
    fields = pulse_fields(num_branches)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: _csv_value(row.get(name, ""))
                             for name in fields})


def _lookup_value_fields(num_branches: int) -> list[str]:
    fields = ["ocv_relaxed_v", "r0_ohm"]
    for i in range(1, num_branches + 1):
        fields += [f"r{i}_ohm", f"tau{i}_s", f"c{i}_f"]
    fields.append("relaxation_rmse_v")
    return fields


def build_lookup(rows: list[dict[str, object]], bin_width: float,
                 num_branches: int) -> list[dict[str, object]]:
    """Build direction-specific median bins from fully accepted pulses only."""
    groups: dict[tuple[str, float], list[dict[str, object]]] = {}
    for row in rows:
        if row["quality_status"] != "accepted":
            continue
        soc = min(max(float(row["soc_parameter"]), 0), 1)
        center = min(round(soc / bin_width) * bin_width, 1.0)
        groups.setdefault((str(row["direction"]), center), []).append(row)
    result = []
    values = _lookup_value_fields(num_branches)
    for (direction, center), members in sorted(
            groups.items(), key=lambda item: (item[0][0], item[0][1])):
        entry: dict[str, object] = {
            "direction": direction, "soc": center,
            "pulse_count": len(members)}
        for field in values:
            entry[field] = float(np.median([float(row[field])
                                            for row in members]))
        result.append(entry)
    return result


def write_lookup_csv(path: Path, lookup: list[dict[str, object]],
                     num_branches: int) -> None:
    fields = ["direction", "soc", "pulse_count"] + _lookup_value_fields(num_branches)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in lookup:
            writer.writerow({name: _csv_value(row.get(name, ""))
                             for name in fields})


def _nest_rc_branches(entry: dict[str, object], num_branches: int) -> dict[str, object]:
    """JSON-only view: fold flat r{i}_ohm/tau{i}_s/c{i}_f into rc_branches.

    The flat entry (also used by write_lookup_csv) is left untouched; this
    builds a separate nested dict rather than mutating it in place.
    """
    branch_keys = {f"r{i}_ohm" for i in range(1, num_branches + 1)}
    branch_keys |= {f"tau{i}_s" for i in range(1, num_branches + 1)}
    branch_keys |= {f"c{i}_f" for i in range(1, num_branches + 1)}
    nested = {k: v for k, v in entry.items() if k not in branch_keys}
    nested["rc_branches"] = [
        {"r_ohm": entry[f"r{i}_ohm"], "tau_s": entry[f"tau{i}_s"],
         "c_f": entry[f"c{i}_f"]}
        for i in range(1, num_branches + 1)]
    return nested


def _esc(text: object) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _svg_chart(path: Path, title: str, ylabel: str,
               series: list[tuple[str, str, list[float], list[float]]],
               xlabel: str = "SOC (fraction)",
               spans: list[tuple[float, float, str]] | None = None,
               width: int = 900, height: int = 520) -> None:
    """Write a lightweight, dependency-free line/scatter chart."""
    all_x = [v for _, _, xs, _ in series for v in xs]
    all_y = [v for _, _, _, ys in series for v in ys if math.isfinite(v)]
    if not all_x or not all_y:
        all_x, all_y = [0, 1], [0, 1]
    xmin, xmax, ymin, ymax = min(all_x), max(all_x), min(all_y), max(all_y)
    if xmax == xmin: xmax += 1
    if ymax == ymin: ymax += 1
    xpad, ypad = .03 * (xmax - xmin), .08 * (ymax - ymin)
    xmin, xmax, ymin, ymax = xmin-xpad, xmax+xpad, ymin-ypad, ymax+ypad
    left, right, top, bottom = 85, width-25, 50, height-65
    sx = lambda x: left + (x-xmin)/(xmax-xmin)*(right-left)
    sy = lambda y: bottom - (y-ymin)/(ymax-ymin)*(bottom-top)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
             '<rect width="100%" height="100%" fill="white"/>',
             f'<text x="{width/2}" y="28" text-anchor="middle" font-family="sans-serif" font-size="18">{_esc(title)}</text>']
    for fraction in np.linspace(0, 1, 6):
        x = left + fraction*(right-left); y = bottom - fraction*(bottom-top)
        xv = xmin + fraction*(xmax-xmin); yv = ymin + fraction*(ymax-ymin)
        parts += [f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{bottom}" stroke="#ddd"/>',
                  f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="#ddd"/>',
                  f'<text x="{x:.1f}" y="{bottom+20}" text-anchor="middle" font-family="sans-serif" font-size="11">{xv:.3g}</text>',
                  f'<text x="{left-8}" y="{y+4:.1f}" text-anchor="end" font-family="sans-serif" font-size="11">{yv:.4g}</text>']
    if spans:
        for lo, hi, color in spans:
            parts.append(f'<rect x="{sx(lo):.1f}" y="{top}" width="{max(sx(hi)-sx(lo),1):.1f}" height="{bottom-top}" fill="{color}" opacity="0.16"/>')
    parts += [f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" stroke="black"/>',
              f'<line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" stroke="black"/>']
    for number, (name, color, xs, ys) in enumerate(series):
        points = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in zip(xs, ys) if math.isfinite(y))
        if points:
            parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2"/>')
            for x, y in zip(xs, ys):
                if math.isfinite(y):
                    parts.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="2.5" fill="{color}"/>')
        parts.append(f'<text x="{right-120}" y="{top+18+number*17}" font-family="sans-serif" font-size="12" fill="{color}">{_esc(name)}</text>')
    parts += [f'<text x="{(left+right)/2}" y="{height-18}" text-anchor="middle" font-family="sans-serif" font-size="13">{_esc(xlabel)}</text>',
              f'<text x="18" y="{(top+bottom)/2}" text-anchor="middle" transform="rotate(-90 18 {(top+bottom)/2})" font-family="sans-serif" font-size="13">{_esc(ylabel)}</text>', '</svg>']
    path.write_text("\n".join(parts), encoding="utf-8")


def _lookup_series(lookup: list[dict[str, object]], field: str):
    colors = {"discharge": "#1f77b4", "charge": "#ff7f0e"}
    result = []
    for direction in ("discharge", "charge"):
        rows = [r for r in lookup if r["direction"] == direction]
        result.append((direction, colors[direction],
                       [float(r["soc"]) for r in rows],
                       [float(r[field]) for r in rows]))
    return result


def _plot_lookup(output: Path, lookup: list[dict[str, object]],
                 num_branches: int) -> None:
    _svg_chart(output / "ocv_vs_soc.svg", "OCV versus SOC", "OCV (V)",
               _lookup_series(lookup, "ocv_relaxed_v"))
    specs = [("r0_ohm", "R0 (ohm)")]
    for i in range(1, num_branches + 1):
        specs += [(f"r{i}_ohm", f"R{i} (ohm)"), (f"tau{i}_s", f"tau{i} (s)"),
                 (f"c{i}_f", f"C{i} (F)")]
    # One requested overview SVG plus individual plots for readable scales.
    for field, label in specs:
        _svg_chart(output / f"{field}_vs_soc.svg", f"{label} versus SOC",
                   label, _lookup_series(lookup, field))
    links = "".join(f'<a href="{field}_vs_soc.svg"><text x="450" y="{100+i*75}" text-anchor="middle" font-family="sans-serif" font-size="24">{label} versus SOC</text></a>' for i, (field, label) in enumerate(specs))
    height = 130 + 75 * len(specs)
    (output / "ecm_parameters_vs_soc.svg").write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="900" height="{height}">'
        '<rect width="100%" height="100%" fill="white"/>'
        '<text x="450" y="38" text-anchor="middle" font-family="sans-serif" font-size="20">Thevenin parameters versus SOC</text>'
        + links + '</svg>', encoding="utf-8")


def _plot_diagnostics(output: Path, data: Samples,
                      rows: list[dict[str, object]]) -> None:
    colors = {"accepted": "#2ca02c", "flagged": "#ff7f0e",
              "rejected": "#d62728"}
    spans = [(float(r["start_time_s"]), float(r["end_time_s"]),
              colors[str(r["quality_status"])]) for r in rows]
    _svg_chart(output / "detected_pulses.svg",
               "Detected pulses (green accepted, orange flagged, red rejected)",
               "Voltage (V)", [("voltage", "#111111", data.t.tolist(), data.voltage.tolist())],
               xlabel="Stitched elapsed time (s)", spans=spans, width=1100)
    _svg_chart(output / "detected_pulse_current.svg", "Detected pulse current",
               "ECM current (A), + discharge",
               [("current", "#9467bd", data.t.tolist(), data.current.tolist())],
               xlabel="Stitched elapsed time (s)", spans=spans, width=1100)
    for row in rows:
        fit_idx = row["_fit_idx"]
        if not isinstance(fit_idx, np.ndarray) or len(fit_idx) == 0:
            continue
        taus = row["_fit_taus"]
        amplitudes = row["_fit_amplitudes"]
        if not all(math.isfinite(tau) and tau > 0 for tau in taus):
            continue
        x = data.t[fit_idx] - data.t[fit_idx][0]
        prediction = float(row["ocv_relaxed_v"]) + sum(
            amplitude * np.exp(-x / tau)
            for amplitude, tau in zip(amplitudes, taus))
        _svg_chart(
            output / f"relaxation_fit_pulse_{int(row['pulse_id']):03d}.svg",
            f"Pulse {row['pulse_id']} {row['direction']} - {row['quality_status']}",
            "Voltage (V)",
            [("measured", "#1f77b4", x.tolist(), data.voltage[fit_idx].tolist()),
             ("fit", "#d62728", x.tolist(), prediction.tolist())],
            xlabel="Relaxation time (s)")


def _load_pseudo_ocv_curve(cfg: CharacterizationConfig
                          ) -> tuple[tuple[np.ndarray, np.ndarray] | None,
                                    dict[str, object] | None]:
    """Load pseudo_ocv.json (see pseudo_ocv.py) if configured, overriding
    cfg.capacity_ah in place with the measured value (printed, not silent)."""
    if not cfg.pseudo_ocv_json:
        return None, None
    with open(cfg.pseudo_ocv_json, encoding="utf-8") as fh:
        pseudo_ocv_data = json.load(fh)
    measured_capacity_ah = float(pseudo_ocv_data["capacity_ah"])
    soc_curve = np.asarray(pseudo_ocv_data["soc"], dtype=float)
    ocv_curve = np.asarray(pseudo_ocv_data["ocv_v"], dtype=float)
    if abs(measured_capacity_ah - cfg.capacity_ah) > 1e-9:
        print(f"Using measured capacity {measured_capacity_ah:.4f} Ah from "
              f"pseudo-OCV data (was {cfg.capacity_ah:.4f} Ah)")
        cfg.capacity_ah = measured_capacity_ah
    source = {
        "path": str(Path(cfg.pseudo_ocv_json).resolve()),
        "capacity_ah": measured_capacity_ah,
        "method": pseudo_ocv_data.get("method", "unknown"),
    }
    return (soc_curve, ocv_curve), source


def characterize(cfg: CharacterizationConfig) -> dict[str, object]:
    cfg.validate()
    input_path = Path(cfg.input_csv).resolve()
    output = Path(cfg.output_dir).resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f"input CSV not found: {input_path}")
    output.mkdir(parents=True, exist_ok=True)
    data = load_samples(str(input_path), cfg.current_threshold_a)
    ocv_curve, pseudo_ocv_source = _load_pseudo_ocv_curve(cfg)
    soc = coulomb_count_soc(data, cfg.capacity_ah, cfg.initial_soc)
    pulses = detect_pulses(data, cfg.current_threshold_a)
    if not pulses:
        raise ValueError("no pulses exceed the configured current threshold")
    rows = [analyze_pulse(data, soc, pulse, cfg, ocv_curve) for pulse in pulses]
    termination_reason = _source_result_reason(input_path)
    if (rows and cfg.reject_terminal_cutoff_pulse and termination_reason
            and termination_reason.casefold().startswith("voltage cutoff reached")):
        terminal = rows[-1]
        reasons = [item for item in str(terminal["quality_reasons"]).split(";")
                   if item]
        if "terminal_pulse_interrupted_by_voltage_cutoff" not in reasons:
            reasons.insert(0, "terminal_pulse_interrupted_by_voltage_cutoff")
        terminal["quality_status"] = "rejected"
        terminal["quality_reasons"] = ";".join(reasons)
    lookup = build_lookup(rows, cfg.soc_bin_width, cfg.num_rc_branches)
    write_pulse_csv(output / "pulse_characterization.csv", rows, cfg.num_rc_branches)
    write_lookup_csv(output / "ecm_lookup_table.csv", lookup, cfg.num_rc_branches)
    _plot_diagnostics(output, data, rows)
    _plot_lookup(output, lookup, cfg.num_rc_branches)
    counts = {status: sum(row["quality_status"] == status for row in rows)
              for status in ("accepted", "flagged", "rejected")}
    branch_terms = " - ".join(f"[R{i}(SOC) || C{i}(SOC)]"
                             for i in range(1, cfg.num_rc_branches + 1))
    export = {
        "model": f"OCV(SOC) - R0(SOC) - {branch_terms}",
        "current_sign": "positive=discharge, negative=charge",
        "num_rc_branches": cfg.num_rc_branches,
        "units": {"soc": "fraction", "ocv": "V", "r0": "ohm",
                  "r": "ohm", "tau": "s", "c": "F"},
        "source_csv": str(input_path),
        "source_result_reason": termination_reason,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "configuration": asdict(cfg),
        "input_quality": {
            "raw_rows": data.raw_rows, "valid_unique_rows": len(data.t),
            "invalid_rows": data.invalid_rows,
            "duplicate_rows_replaced": data.duplicate_rows,
            "resumed_timestamp_segments": data.resumed_segments,
            "median_sample_interval_s": data.median_dt_s},
        "pulse_quality_counts": counts,
        "lookup": [_nest_rc_branches(row, cfg.num_rc_branches) for row in lookup],
    }
    if pseudo_ocv_source:
        export["pseudo_ocv_source"] = pseudo_ocv_source
    with (output / "ecm_parameters.json").open("w", encoding="utf-8") as fh:
        json.dump(export, fh, indent=2, allow_nan=False)
    return export


def _source_result_reason(input_path: Path) -> str | None:
    """Read launchTest's completion reason without modifying its checkpoint."""
    checkpoint = input_path.with_name(input_path.stem+"_checkpoint.json")
    if not checkpoint.is_file():
        return None
    try:
        with checkpoint.open(encoding="utf-8") as fh:
            value = json.load(fh).get("result_reason")
        return str(value) if value else None
    except (OSError, ValueError, TypeError):
        return None


def capacity_from_cell_json(path: str) -> float:
    with open(path, encoding="utf-8") as fh:
        cell = json.load(fh)
    for key in ("nominal_capacity_ah", "capacity_ah"):
        if key in cell:
            return float(cell[key])
    capacity = cell.get("capacity", {})
    for key in ("nominal_ah", "ah"):
        if key in capacity:
            return float(capacity[key])
    raise ValueError(f"cell JSON has no nominal capacity in Ah: {path}")


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_csv")
    capacity = parser.add_mutually_exclusive_group(required=True)
    capacity.add_argument("--cell-json")
    capacity.add_argument("--capacity-ah", type=float)
    parser.add_argument("--initial-soc", type=float, default=1.0)
    parser.add_argument("--expected-pulse-s", type=float, default=60.0)
    parser.add_argument("--current-threshold-a", type=float, default=.1)
    parser.add_argument("--relaxation-start-s", type=float, default=1.0)
    parser.add_argument("--relaxation-end-s", type=float, default=300.0)
    parser.add_argument("--soc-bin-width", type=float, default=.05)
    parser.add_argument("--num-rc-branches", type=int, default=2)
    parser.add_argument("--min-adjacent-tau-ratio", type=float, default=2.5)
    parser.add_argument("--pseudo-ocv-json", default=None,
                        help="pseudo_ocv.json from pseudo_ocv.py -- overrides "
                             "the capacity above and anchors pulse SOC/OCV "
                             "to the measured curve instead of coulomb-counting")
    parser.add_argument("--output-dir", required=True)
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> None:
    args = parse_args(argv)
    capacity = (args.capacity_ah if args.capacity_ah is not None
                else capacity_from_cell_json(args.cell_json))
    result = characterize(CharacterizationConfig(
        input_csv=args.input_csv, output_dir=args.output_dir,
        capacity_ah=capacity, initial_soc=args.initial_soc,
        expected_pulse_s=args.expected_pulse_s,
        current_threshold_a=args.current_threshold_a,
        relaxation_window_s=(args.relaxation_start_s,
                             args.relaxation_end_s),
        soc_bin_width=args.soc_bin_width,
        num_rc_branches=args.num_rc_branches,
        min_adjacent_tau_ratio=args.min_adjacent_tau_ratio,
        pseudo_ocv_json=args.pseudo_ocv_json))
    print(f"Characterization complete: {result['pulse_quality_counts']}")
    print(f"Outputs: {Path(args.output_dir).resolve()}")


if __name__ == "__main__":
    main()
