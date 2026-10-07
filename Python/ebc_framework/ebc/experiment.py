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
from datetime import datetime
from typing import Optional

from .protocol import Measurement
from .device import EBCA20, SafetyLimits


# Default ceiling on a sample-to-sample gap that still counts as continuous
# current flow when building cumulative_ah. Real dropouts observed on this
# hardware were 9-25 s, so this must stay well under that; callers running a
# slow sample_period_s raise it via DataLogger(max_gap_s=...).
MAX_INTEGRATION_GAP_S = 5.0


class DataLogger:
    """Logs t_s (elapsed test time, what every analysis tool integrates and
    resumes against) alongside wall_clock (real local time of the sample).

    t_s stays the primary axis on purpose: it's monotonic across a resume
    (offset-stitched) and is what characterize_hppc/validate_profile/the BMS
    all assume. wall_clock is naturally continuous across a process restart
    with no stitching needed, so a real stop-and-resume shows up as a large
    wall_clock jump alongside a small/zero t_s jump -- exactly what marks
    where a gap happened and how long the tester was actually down.
    """

    def __init__(self, path: str, extra_fields: Optional[list[str]] = None,
                 append: bool = False,
                 max_gap_s: float = MAX_INTEGRATION_GAP_S):
        self.path = path
        self._extra = extra_fields or []
        self._max_gap_s = float(max_gap_s)
        has_data = append and os.path.isfile(path) and os.path.getsize(path) > 0
        self._include_wall_clock = True
        self._include_cumulative = True
        self._cumulative_ah = 0.0
        self._last_t: Optional[float] = None
        self._last_signed_a = 0.0
        if has_data:
            with open(path, newline="") as existing:
                header = next(csv.reader(existing), [])
            # A file logged before this column existed keeps its old schema;
            # mixing column counts within one CSV would corrupt it.
            self._include_wall_clock = "wall_clock" in header
            self._include_cumulative = "cumulative_ah" in header
            if self._include_cumulative:
                self._restore_cumulative(path)
        self._fh = open(path, "a" if append else "w", newline="")
        self._w = csv.writer(self._fh)
        fields = ["t_s"]
        if self._include_wall_clock:
            fields.append("wall_clock")
        fields += ["voltage_v", "current_a", "signed_current_a", "charge_ah"]
        if self._include_cumulative:
            fields.append("cumulative_ah")
        fields.append("mode")
        if not has_data:
            self._w.writerow(fields + self._extra)
        if has_data:
            with open(path, newline="") as existing:
                self.rows = max(sum(1 for _ in existing) - 1, 0)
        else:
            self.rows = 0

    def _restore_cumulative(self, path: str) -> None:
        """Continue the running total across a resume or auto-reconnect."""
        last = None
        with open(path, newline="") as existing:
            for row in csv.DictReader(existing):
                last = row
        if not last:
            return
        try:
            self._cumulative_ah = float(last["cumulative_ah"])
            self._last_t = float(last["t_s"])
            self._last_signed_a = float(last["signed_current_a"])
        except (KeyError, TypeError, ValueError):
            self._cumulative_ah = 0.0
            self._last_t = None
            self._last_signed_a = 0.0

    def _accumulate(self, signed_a: float, t_elapsed: float) -> None:
        """Integrate charge over the interval that just ended.

        Zero-order hold on the interval's *starting* current: the tester
        holds a commanded setpoint until the next command, so the previous
        sample's value is what actually flowed for that interval.

        Gaps longer than max_gap_s are skipped rather than integrated. They
        are reconnect holes, and no current flows across them -- confirmed on
        hardware 2026-09-25, where voltage relaxed by the full IR drop during
        a dropout -- so integrating would invent charge that never moved.
        Measured dropouts were 9-25 s against a 1-2 s sample cadence, so the
        threshold has to sit just above the normal cadence, not at some round
        number well above it.
        """
        if self._last_t is not None:
            dt = t_elapsed - self._last_t
            if 0.0 < dt <= self._max_gap_s:
                self._cumulative_ah += self._last_signed_a * dt / 3600.0
        self._last_t = t_elapsed
        self._last_signed_a = signed_a

    def log(self, m: Measurement, t_elapsed: float, **extra) -> None:
        row = [f"{t_elapsed:.3f}"]
        if self._include_wall_clock:
            row.append(datetime.now().isoformat(sep=" ", timespec="milliseconds"))
        row += [f"{m.voltage:.4f}", f"{m.current:.4f}",
               f"{m.signed_current:.4f}", f"{m.charge:.5f}"]
        if self._include_cumulative:
            self._accumulate(m.signed_current, t_elapsed)
            row.append(f"{self._cumulative_ah:.6f}")
        row.append(m.mode)
        self._w.writerow(row + [extra.get(k, "") for k in self._extra])
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


def profile_from_entries(entries: list[dict], peak_amps: float
                         ) -> list[ProfileStep]:
    """Build a staircase from JSON {"current", "duration_s"} entries whose
    `current` is normalized to the profile's own peak magnitude (the largest
    |current| in `entries` maps to `peak_amps`, regardless of what scale the
    entries happen to be written in), rather than an absolute current."""
    values = [float(e["current"]) for e in entries]
    peak_magnitude = max((abs(v) for v in values), default=0.0)
    if peak_magnitude <= 0:
        raise ValueError("profile has no nonzero current entries to scale against")
    return [ProfileStep(current_a=value / peak_magnitude * peak_amps,
                        duration_s=float(entry["duration_s"]))
            for entry, value in zip(entries, values)]


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
             "v_start": None, "v_end": None, "discharging": False,
             "non_discharge_streak": 0}
    # A single misdecoded/corrupted status frame must never look like a
    # legitimate end of discharge. Confirmed on real hardware 2026-07-25: one
    # stray mode="unknown" sample (voltage never actually moved, current
    # never actually rose) ended a 2C capacity test after ~1s instead of the
    # expected ~30 min. Require several consecutive non-discharge samples --
    # the same debounce philosophy already used for command confirmation
    # elsewhere (see EBCA20.COMMAND_MISMATCH_SAMPLES) -- rather than trusting
    # any one sample on its own.
    NON_DISCHARGE_CONFIRM_SAMPLES = 3

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
            state["non_discharge_streak"] = 0
        if m.voltage <= v_cutoff:
            return float("nan")
        # At high current the IR/RC voltage sag can be large enough that once
        # the device's own discharge_cutoff_v backstop auto-stops it, the
        # very next (now-relaxed, unloaded) sample rebounds back above
        # v_cutoff before we observe a sample at/below threshold -- so also
        # stop once the device has genuinely (not just momentarily) left
        # "discharge" mode after having genuinely been discharging, confirmed
        # offline 2026-07-01 (cellAlex at 1C stalled the full
        # max_duration_s without this).
        if state["discharging"] and m.mode != "discharge":
            state["non_discharge_streak"] += 1
            if state["non_discharge_streak"] >= NON_DISCHARGE_CONFIRM_SAMPLES:
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
