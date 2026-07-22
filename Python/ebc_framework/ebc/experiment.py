"""
Experiment toolkit for SOC/SOH algorithm validation.

Provides:
  - DataLogger: timestamped CSV of t, V, I, charge, mode, soc_ref (optional).
  - ProfileController: plays a staircase current profile (DST/FUDS/custom),
    pairs with EBCA20.run().
  - capacity_test: full discharge at constant current to measure usable Ah
    (the SOH ground truth).
  - pseudo_ocv_test: low-current discharge whose terminal voltage approximates
    the OCV-SOC curve (useful reference for model-based SOC estimators).

These produce the reference data your estimator is compared against.
"""

from __future__ import annotations
import csv
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from .protocol import Measurement
from .device import EBCA20, SafetyLimits


class DataLogger:
    def __init__(self, path: str, extra_fields: Optional[list[str]] = None,
                 append: bool = False):
        self.path = path
        self._extra = extra_fields or []
        has_data = append and os.path.isfile(path) and os.path.getsize(path) > 0
        self._fh = open(path, "a" if append else "w", newline="")
        self._w = csv.writer(self._fh)
        if not has_data:
            self._w.writerow(["t_s", "voltage_v", "current_a",
                              "signed_current_a", "charge_ah", "mode"] +
                             self._extra)
        if has_data:
            with open(path, newline="") as existing:
                self.rows = max(sum(1 for _ in existing) - 1, 0)
        else:
            self.rows = 0

    def log(self, m: Measurement, t_elapsed: float, **extra) -> None:
        self._w.writerow([
            f"{t_elapsed:.3f}", f"{m.voltage:.4f}", f"{m.current:.4f}",
            f"{m.signed_current:.4f}", f"{m.charge:.5f}", m.mode,
        ] + [extra.get(k, "") for k in self._extra])
        self.rows += 1
        # Flush every row (not batched) so a second process (e.g. plot_log.py
        # --watch) can tail this file live -- negligible overhead at the
        # EBC's ~1 Hz sample rate.
        self._fh.flush()

    def close(self) -> None:
        self._fh.flush()
        self._fh.close()


@dataclass
class ProfileStep:
    current_a: float          # signed: + charge, - discharge, 0 rest
    duration_s: float


@dataclass
class ProfileController:
    """
    Plays a list of ProfileStep. Use as the `controller` arg to EBCA20.run().

    Granularity is per-step (>= a few seconds is ideal for the EBC's serial
    cadence), which matches DST-style profiles perfectly.
    """
    steps: list[ProfileStep]
    repeat: Optional[int] = 1     # None = loop forever (stopped externally,
                                   # e.g. by v_min or the caller's max_duration_s)
    v_min: Optional[float] = None # stop as soon as voltage drops to/below this,
                                   # regardless of where in the profile we are
    _idx: int = field(default=0, init=False)
    _step_start: Optional[float] = field(default=None, init=False)
    _cycle: int = field(default=0, init=False)
    _last_cmd: Optional[float] = field(default=None, init=False)
    _resume_step_elapsed_s: float = field(default=0.0, init=False)

    def restore(self, state: dict) -> None:
        """Restore a saved staircase position before starting a new run."""
        if self.steps:
            self._idx = min(max(int(state.get("step_index", 0)), 0),
                            len(self.steps) - 1)
        self._cycle = max(int(state.get("cycle", 0)), 0)
        self._resume_step_elapsed_s = max(
            float(state.get("step_elapsed_s", 0.0)), 0.0)
        self._step_start = None
        self._last_cmd = None

    def snapshot(self, t_elapsed: float) -> dict:
        elapsed = (t_elapsed - self._step_start
                   if self._step_start is not None
                   else self._resume_step_elapsed_s)
        return {"step_index": self._idx, "cycle": self._cycle,
                "step_elapsed_s": max(elapsed, 0.0)}

    def __call__(self, m: Measurement, t_elapsed: float) -> Optional[float]:
        # Check voltage alone, not paired with mode -- the device's own
        # discharge_cutoff_v backstop can auto-stop in the same instant the
        # threshold is crossed, so mode may already read something else by
        # the time we observe it (see PulseRestController for the same fix).
        if self.v_min is not None and m.voltage <= self.v_min:
            return float("nan")

        if self._step_start is None:
            self._step_start = t_elapsed - self._resume_step_elapsed_s
            self._resume_step_elapsed_s = 0.0

        step = self.steps[self._idx]
        if t_elapsed - self._step_start >= step.duration_s:
            self._idx += 1
            self._step_start = t_elapsed
            if self._idx >= len(self.steps):
                self._idx = 0
                self._cycle += 1
                if self.repeat is not None and self._cycle >= self.repeat:
                    return float("nan")     # signal stop
            step = self.steps[self._idx]

        if step.current_a != self._last_cmd:
            self._last_cmd = step.current_a
            return step.current_a
        return None                          # hold present setpoint


# ----------------- standard DST-like profile -----------------

def dst_profile(scale_a: float = 1.0) -> list[ProfileStep]:
    """
    A compact DST-style staircase (percent of a reference current, scaled to
    `scale_a`). Negative = discharge. Illustrative, not the exact FUDS/USABC
    table — swap in the standard values you cite in your paper.
    """
    pattern = [
        (0.0, 5), (-0.20, 8), (-0.50, 12), (0.30, 8), (-0.80, 10),
        (-0.40, 12), (0.20, 6), (-1.00, 8), (0.0, 4), (-0.30, 12),
    ]
    return [ProfileStep(current_a=p * scale_a, duration_s=d) for p, d in pattern]


# ----------------- reference measurements -----------------

@dataclass
class CapacityResult:
    capacity_ah: float
    energy_wh: float
    duration_s: float
    v_start: float
    v_end: float
    reason: str = ""


def capacity_test(dev: EBCA20, current_a: float, v_cutoff: float,
                  logger: Optional[DataLogger] = None,
                  max_duration_s: float = 6 * 3600) -> CapacityResult:
    """
    Constant-current discharge to v_cutoff; integrates charge and energy.
    This is the SOH ground truth: repeat over the cell's life (or across cells)
    to get the capacity-fade curve your SOH estimator must track.
    """
    dev.limits.discharge_cutoff_v = v_cutoff
    state = {"q": 0.0, "wh": 0.0, "t_prev": None,
             "v_start": None, "v_end": None, "discharging": False}

    def ctrl(m: Measurement, t: float):
        if state["v_start"] is None:
            state["v_start"] = m.voltage
        state["v_end"] = m.voltage
        if state["t_prev"] is not None:
            dt = t - state["t_prev"]
            state["wh"] += m.voltage * m.current * dt / 3600.0
        state["t_prev"] = t
        # device integrates charge in its own time base; trust its Ah counter
        state["q"] = m.charge
        if m.mode == "discharge":
            state["discharging"] = True
        if m.voltage <= v_cutoff:
            return float("nan")
        # At high current the IR/RC voltage sag can be large enough that once
        # the device's own discharge_cutoff_v backstop auto-stops it, the
        # very next (now-relaxed, unloaded) sample rebounds back above
        # v_cutoff before we observe a sample at/below threshold -- so also
        # stop once we see the device leave "discharge" mode after having
        # genuinely been discharging, confirmed on the simulator 2026-07-01
        # (cellAlex at 1C stalled the full max_duration_s without this).
        if state["discharging"] and m.mode != "discharge":
            return float("nan")
        return None

    def on_sample(m, t):
        if logger:
            logger.log(m, t)

    dev.set_discharge(current_a)
    reason = dev.run(ctrl, period_s=1.0, max_duration_s=max_duration_s,
                     on_sample=on_sample)
    return CapacityResult(
        capacity_ah=state["q"], energy_wh=state["wh"],
        duration_s=state["t_prev"] or 0.0,
        v_start=state["v_start"] or 0.0, v_end=state["v_end"] or 0.0,
        reason=reason,
    )
