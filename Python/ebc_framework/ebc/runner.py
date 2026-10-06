"""
Test runner: turn a JSON test definition + cell library into an executed run.

Supported test types:
  - "dst"       : dynamic staircase current profile (SOC validation). A
                  custom "profile" is normally read as literal per-step
                  C-rate; add "profile_peak_c_rate" to instead treat it as
                  shape-only (normalized to its own peak magnitude) and
                  rescale so the peak reaches that many C.
  - "charge"    : constant-current charge to the cell's max_v
  - "discharge" : constant-current discharge to min_v (SOH capacity)
  - "cycle"     : repeated charge / rest / discharge / rest
  - "hppc"      : discharge pulse / rest, repeated until v_min (resistance/
                  relaxation characterization)
  - "rest"      : passive observation at zero applied current
  - "protocol"  : composite [discharge (dst/hppc/crate) -> rest -> CC-CV
                  charge -> rest], repeated "repeat" times; either phase may
                  be omitted (JSON null) for a charge-only or discharge-only
                  run. Optional "order" (default ["discharge", "charge"])
                  controls phase sequence -- set ["charge", "discharge"] to
                  charge first. See config/tests/cellAlex_protocol.json. A
                  "dst" discharge phase loops its staircase (repeat=None)
                  until "v_min", then automatically continues to the rest
                  and CC-CV charge phases. Its "c_rate" scales the built-in
                  illustrative staircase; add a custom, shape-only "profile"
                  (normalized to its own peak magnitude) to rescale that
                  instead, so the peak reaches exactly "c_rate" C. See
                  config/tests/dst_protocol_custom.json.

Currents in test files are C-rate (current_unit="C") and scaled by the cell's
nominal capacity, or absolute amps (current_unit="A"). This literal scaling
is bypassed by "profile_peak_c_rate" (dst) or a custom "profile" (protocol's
dst discharge), both of which normalize to the profile's own peak instead.

Every run derives SafetyLimits from the cell so the voltage/current bounds you
defined in the cell JSON are enforced both software-side and (via cutoff fields)
device-side.
"""

from __future__ import annotations
import json
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .celllib import Cell, CellLibrary
from .device import EBCA20
from .protocol import Measurement, PT_STATUS_CHARGE_END
from .experiment import (DataLogger, ProfileController, ProfileStep,
                         dst_profile, profile_from_entries, capacity_test,
                         CapacityResult)


def _charge_target_v(config: dict, cell: Cell) -> float:
    """Use nominal max by default; explicit requests may reach hard max."""
    requested = float(config.get("v_max", cell.max_v))
    target = min(requested, cell.hard_max_v)
    if requested > cell.hard_max_v:
        print(f"  [safety] requested charge target {requested:.3f} V exceeds "
              f"hard limit {cell.hard_max_v:.3f} V -- capped")
    return target


def _discharge_target_v(config: dict, key: str, cell: Cell) -> float:
    """Use nominal min by default; explicit requests may reach hard min."""
    requested = float(config.get(key, cell.min_v))
    target = max(requested, cell.hard_min_v)
    if requested < cell.hard_min_v:
        print(f"  [safety] requested discharge target {requested:.3f} V is "
              f"below hard limit {cell.hard_min_v:.3f} V -- capped")
    return target


@dataclass
class TestDef:
    raw: dict

    @property
    def name(self) -> str: return self.raw["name"]
    @property
    def type(self) -> str: return self.raw["type"]
    @property
    def cell_id(self) -> Optional[str]:
        """Cell requested by the test, or None when selected at launch time."""
        value = self.raw.get("cell")
        if value is None:
            return None
        value = str(value).strip()
        return value or None
    @property
    def sample_period_s(self) -> float:
        return float(self.raw.get("sample_period_s", 1.0))
    @property
    def initial_rest_s(self) -> float:
        return float(self.raw.get("initial_rest_s", 10.0))
    @property
    def max_duration_s(self) -> Optional[float]:
        v = self.raw.get("max_duration_s")
        return float(v) if v is not None else None

    def amps(self, value: float, cell: Cell) -> float:
        if self.raw.get("current_unit", "C").upper() == "C":
            return cell.c_to_amps(value)
        return value

    @classmethod
    def load(cls, path: str) -> "TestDef":
        with open(path) as fh:
            return cls(json.load(fh))


# ---------------- HPPC-style pulse controller ----------------

class PulseRestController:
    """
    Discharge pulse -> rest, repeated until v_min is reached mid-pulse.
    Used as the run() controller for the "hppc" test type. No charge phase --
    each pulse steps SOC down a bit further, giving resistance/relaxation
    data across the usable SOC range.
    """

    def __init__(self, discharge_a: float, pulse_s: float, rest_s: float,
                 v_min: float):
        self.discharge_a = discharge_a
        self.pulse_s = pulse_s
        self.rest_s = rest_s
        self.v_min = v_min

        self.phase = "discharge"        # discharge|rest
        self._phase_start: Optional[float] = None
        self._last_cmd: Optional[float] = None
        self._resume_phase_elapsed_s = 0.0

    def restore(self, state: dict) -> None:
        phase = state.get("pulse_phase", "discharge")
        if phase not in ("discharge", "rest"):
            raise ValueError(f"invalid saved HPPC phase: {phase}")
        self.phase = phase
        self._resume_phase_elapsed_s = max(
            float(state.get("pulse_phase_elapsed_s", 0.0)), 0.0)
        self._phase_start = None
        self._last_cmd = None

    def snapshot(self, t: float) -> dict:
        elapsed = (t - self._phase_start if self._phase_start is not None
                   else self._resume_phase_elapsed_s)
        return {"pulse_phase": self.phase,
                "pulse_phase_elapsed_s": max(elapsed, 0.0)}

    def __call__(self, m: Measurement, t: float) -> Optional[float]:
        if self._phase_start is None:
            self._phase_start = t - self._resume_phase_elapsed_s
            self._resume_phase_elapsed_s = 0.0

        if self.phase == "discharge":
            # Check voltage alone, not paired with mode=="discharge": the
            # device's own discharge_cutoff_v backstop can auto-stop and flip
            # to idle in the same instant the threshold is crossed, so mode
            # may already read "idle" by the time we observe the crossing.
            if m.voltage <= self.v_min:
                return float("nan")     # cutoff reached -- stop
            if t - self._phase_start >= self.pulse_s:
                self._phase_start = t
                self.phase = "rest"
                return self._emit(0.0)
            return self._emit(-self.discharge_a)

        # rest
        # Do not trust a single STOP packet.  If the tester still reports
        # current, repeat STOP on every sample.  Without this check a lost STOP
        # would turn the complete nominal rest interval into one long pulse.
        if m.mode != "idle" or abs(m.current) > 0.03:
            return 0.0
        if t - self._phase_start >= self.rest_s:
            self._phase_start = t
            self.phase = "discharge"
            return self._emit(-self.discharge_a)
        return self._emit(0.0)

    def _emit(self, cmd: float) -> Optional[float]:
        if cmd != self._last_cmd:
            self._last_cmd = cmd
            return cmd
        return None


# ---------------- CC-CV charge controller ----------------

class CCCVChargeController:
    """
    Constant-current charge to v_max, then let the device's own CV/taper
    cutoff (charge_cv/charge_taper_a in SafetyLimits) bring it to idle.
    Used standalone by the "charge" test type, and as the charge phase of
    the composite "protocol" test type.
    """

    def __init__(self, v_max: float):
        self.v_max = v_max
        self._charged = False

    def __call__(self, m: Measurement, t: float) -> Optional[float]:
        if m.mode == "charge":
            self._charged = True
        # Reaching v_max is the start of the CV phase, not the end of charge.
        # Leave the EBC's CC-CV command active until its hardware taper cutoff
        # produces CHRG_CCCV_END (0x16) or the tester becomes idle.
        if self._charged and (m.pt == PT_STATUS_CHARGE_END or
                              m.mode == "idle"):
            return float("nan")
        return None


# ---------------- rest (zero-current hold) controller ----------------

class RestController:
    """Holds at zero current for duration_s, then signals stop."""

    def __init__(self, duration_s: float):
        self.duration_s = duration_s
        self._t0: Optional[float] = None

    def __call__(self, m: Measurement, t: float) -> Optional[float]:
        if self._t0 is None:
            self._t0 = t
        if t - self._t0 >= self.duration_s:
            return float("nan")
        return None


# ---------------- startup link check (read path + write path) ----------------

LINK_CHECK_VOLTAGE_MARGIN_FRACTION = 0.10
LINK_CHECK_NO_RESPONSE_TIMEOUT_S = 5.0
# The device emits a status frame only every ~1-2 s, and the first frame
# after a start-discharge command still reports 0.000 A -- the current shows
# up from the following frame on. Confirmed on real hardware 2026-09-25: a
# 2.0 s window often saw just that one not-yet-ramped frame and wrongly
# failed the write-path check on a perfectly working link. Keep the window
# wide enough for several frames, and the budget above it so a short
# initial_rest_s skips the check rather than truncating it.
PULSE_CHECK_MIN_BUDGET_S = 7.0
PULSE_CHECK_DURATION_S = 6.0
PULSE_CHECK_CONFIRM_FRACTION = 0.3


def _verify_link(dev: EBCA20, cell: Cell, on_sample) -> tuple[Measurement, float]:
    """Confirm the read path is actually alive and sane: wait for one real
    measurement (fast timeout, not the general in-run watchdog) and check
    its voltage is physically plausible for this cell. Catches "connected"
    but silently deaf/wrong-port/garbled-data situations in seconds instead
    of the full test's max_duration_s.
    """
    captured: dict = {}

    def controller(m: Measurement, t: float) -> Optional[float]:
        captured["m"], captured["t"] = m, t
        return float("nan")

    dev.run(controller, period_s=0.5, max_duration_s=8.0, on_sample=on_sample,
            no_response_timeout_s=LINK_CHECK_NO_RESPONSE_TIMEOUT_S)
    m = captured.get("m")
    if m is None:
        raise RuntimeError("no measurement received during startup link check")
    margin = LINK_CHECK_VOLTAGE_MARGIN_FRACTION * (cell.hard_max_v - cell.hard_min_v)
    if not (cell.hard_min_v - margin <= m.voltage <= cell.hard_max_v + margin):
        raise RuntimeError(
            f"implausible voltage reading at startup: {m.voltage:.3f} V "
            f"(expected roughly {cell.hard_min_v:.2f}-{cell.hard_max_v:.2f} V "
            "for this cell) -- check wiring, the selected cell, and the COM "
            "port before trusting this run")
    return m, float(captured.get("t", 0.0))


def _verify_pulse_response(dev: EBCA20, cell: Cell, on_sample) -> float:
    """Confirm the write path actually works, not just reads: command a
    brief C/10 discharge and require the device to report back a matching
    discharge current before treating the connection as trustworthy.
    """
    pulse_a = cell.nominal_capacity_ah / 10.0
    dev.set_discharge(pulse_a)
    confirmed = {"ok": False, "t": 0.0}

    def controller(m: Measurement, t: float) -> Optional[float]:
        confirmed["t"] = t
        if (m.mode == "discharge"
                and m.current > PULSE_CHECK_CONFIRM_FRACTION * pulse_a):
            confirmed["ok"] = True
            return float("nan")
        return None

    dev.run(controller, period_s=0.3, max_duration_s=PULSE_CHECK_DURATION_S,
            on_sample=on_sample,
            no_response_timeout_s=LINK_CHECK_NO_RESPONSE_TIMEOUT_S)
    dev.stop()
    if not confirmed["ok"]:
        raise RuntimeError(
            f"commanded a {pulse_a:.3f} A (C/10) discharge pulse at startup "
            "but the device never confirmed it -- current is being read but "
            "commands don't appear to be taking effect; check the link "
            "before trusting this run")
    return float(confirmed["t"])


# ---------------- cycle controller ----------------

class CycleController:
    """
    Charge -> rest -> discharge -> rest, repeated. Used as the run() controller.
    Tracks per-cycle discharge capacity via the device's charge counter.
    """

    def __init__(self, charge_a: float, discharge_a: float,
                 v_max: float, v_min: float,
                 rest_after_charge_s: float, rest_after_discharge_s: float,
                 repeat: int):
        self.charge_a = charge_a
        self.discharge_a = discharge_a
        self.v_max, self.v_min = v_max, v_min
        self.rest_c = rest_after_charge_s
        self.rest_d = rest_after_discharge_s
        self.repeat = repeat

        self.phase = "charge"          # charge|rest_c|discharge|rest_d|done
        self.cycle = 0
        self._phase_start: Optional[float] = None
        self._last_cmd: Optional[float] = None
        self.cycle_capacities: list[float] = []
        self._resume_phase_elapsed_s = 0.0

    def restore(self, state: dict) -> None:
        phase = state.get("cycle_phase", "charge")
        valid = {"charge", "rest_c", "discharge", "rest_d", "done"}
        if phase not in valid:
            raise ValueError(f"invalid saved cycle phase: {phase}")
        self.phase = phase
        self.cycle = max(int(state.get("cycle_index", 0)), 0)
        self.cycle_capacities = [
            float(value) for value in state.get("cycle_capacities", [])]
        self._resume_phase_elapsed_s = max(
            float(state.get("cycle_phase_elapsed_s", 0.0)), 0.0)
        self._phase_start = None
        self._last_cmd = None

    def snapshot(self, t: float) -> dict:
        elapsed = (t - self._phase_start if self._phase_start is not None
                   else self._resume_phase_elapsed_s)
        return {
            "cycle_phase": self.phase,
            "cycle_index": self.cycle,
            "cycle_phase_elapsed_s": max(elapsed, 0.0),
            "cycle_capacities": self.cycle_capacities,
        }

    def __call__(self, m: Measurement, t: float) -> Optional[float]:
        if self._phase_start is None:
            self._phase_start = t - self._resume_phase_elapsed_s
            self._resume_phase_elapsed_s = 0.0

        if self.phase == "charge":
            charge_finished = (
                m.pt == PT_STATUS_CHARGE_END or
                (self._last_cmd == +self.charge_a and m.mode == "idle"
                 and t - self._phase_start > 1.0)
            )
            if charge_finished:
                self._phase_start = t
                self.phase = "rest_c"
                return 0.0
            return self._emit(+self.charge_a)

        if self.phase == "rest_c":
            if t - self._phase_start >= self.rest_c:
                self._phase_start = t
                self.phase = "discharge"
                return self._emit(-self.discharge_a)
            return self._emit(0.0)

        if self.phase == "discharge":
            if m.voltage <= self.v_min and m.mode == "discharge":
                self.cycle_capacities.append(m.charge)
                self._phase_start = t
                self.phase = "rest_d"
                return 0.0
            return self._emit(-self.discharge_a)

        if self.phase == "rest_d":
            if t - self._phase_start >= self.rest_d:
                self.cycle += 1
                if self.cycle >= self.repeat:
                    self.phase = "done"
                    return float("nan")
                self._phase_start = t
                self.phase = "charge"
                return self._emit(+self.charge_a)
            return self._emit(0.0)

        return float("nan")

    def _emit(self, cmd: float) -> Optional[float]:
        if cmd != self._last_cmd:
            self._last_cmd = cmd
            return cmd
        return None


# ---------------- composite protocol test ("protocol") ----------------

class _LogAdapter:
    """Wraps a plain (m, t) -> None callable so it matches DataLogger.log's
    signature, for reuse with capacity_test()'s logger= parameter."""

    def __init__(self, fn):
        self._fn = fn

    def log(self, m: Measurement, t: float, **extra) -> None:
        self._fn(m, t)


class _OffsetLogger:
    """Keep timestamps continuous when a test follows an initial rest."""

    def __init__(self, logger: DataLogger, offset_s: float):
        self._logger = logger
        self._offset_s = offset_s

    def log(self, m: Measurement, t: float, **extra) -> None:
        self._logger.log(m, self._offset_s + t, **extra)

    @property
    def rows(self) -> int:
        return self._logger.rows


class _TrackingLogger:
    """Track the absolute last timestamp through nested offset loggers."""

    def __init__(self, logger: DataLogger):
        self._logger = logger
        self.last_t = 0.0

    def log(self, m: Measurement, t: float, **extra) -> None:
        self.last_t = max(self.last_t, float(t))
        self._logger.log(m, t, **extra)

    @property
    def rows(self) -> int:
        return self._logger.rows


class RecoveryRestController:
    """Hold zero current and repeat STOP while measured current persists."""

    def __init__(self, duration_s: float):
        self.duration_s = float(duration_s)

    def __call__(self, m: Measurement, t: float) -> Optional[float]:
        if t >= self.duration_s:
            return float("nan")
        if m.mode != "idle" or abs(m.current) > 0.03:
            return 0.0
        return None


def _run_protocol(test: "TestDef", cell: Cell, dev: EBCA20,
                  logger: DataLogger, resume_state: Optional[dict] = None,
                  progress: Optional[Callable[[dict, float], None]] = None
                  ) -> "RunResult":
    """
    Composite test: an optional discharge phase (dst / hppc / crate), an
    optional rest, an optional CC-CV charge phase, an optional rest --
    repeated "repeat" times, in the order given by "order" (default
    ["discharge", "charge"]; set to ["charge", "discharge"] to charge first).
    Either phase may be omitted (JSON null) for a charge-only or
    discharge-only run. Either phase's own "max_duration_s" overrides the
    test-level default for that phase only (e.g. a time-limited partial
    discharge alongside a longer-budget charge phase in the same test).

    max_duration_s is applied as a per-phase safety ceiling (each discharge/
    charge/rest sub-run gets its own budget), not a whole-test ceiling.
    """
    period = test.sample_period_s
    repeat = int(test.raw.get("repeat", 1))
    order = test.raw.get("order", ["discharge", "charge"])
    dcfg = test.raw.get("discharge")
    ccfg = test.raw.get("charge")

    # t_elapsed resets to 0 inside every dev.run() call; accumulate a global
    # offset so the logged/plotted time axis keeps increasing across phases.
    time_state = {"offset": 0.0, "last": 0.0}
    state = {"reason": "nothing to do", "abort": False}
    progress_state = {"cycle_index": 0, "phase_index": 0,
                      "stage": "between_phases", "phase_elapsed_s": 0.0}
    phase_time_base = {"value": 0.0}
    controller_snapshot = {"fn": None}

    def on_sample(m: Measurement, te: float) -> None:
        time_state["last"] = te
        logger.log(m, time_state["offset"] + te)
        progress_state["phase_elapsed_s"] = phase_time_base["value"] + te
        snapshot_fn = controller_snapshot["fn"]
        if snapshot_fn is not None:
            progress_state["controller_state"] = snapshot_fn(te)
        else:
            progress_state.pop("controller_state", None)
        if progress is not None:
            progress(dict(progress_state), time_state["offset"] + te)

    def advance_offset() -> None:
        time_state["offset"] += time_state["last"]
        time_state["last"] = 0.0

    def rest(duration_s: float, stage: str,
             already_elapsed_s: float = 0.0) -> None:
        # Time spent stopped overnight counts toward a protocol rest. A fresh
        # mandatory 10-second safety rest is still performed by run_test().
        remaining_s = max(duration_s - already_elapsed_s, 0.0)
        if remaining_s > 0:
            progress_state["stage"] = stage
            progress_state["phase_elapsed_s"] = already_elapsed_s
            phase_time_base["value"] = already_elapsed_s
            controller_snapshot["fn"] = None
            print(f"{stage.replace('_', ' ').title()}: {remaining_s:g} s")
            dev.stop()
            rest_reason = dev.run(
                RestController(remaining_s), period_s=period,
                max_duration_s=remaining_s, on_sample=on_sample)
            if rest_reason.startswith("safety stop:"):
                state["reason"] = rest_reason
                state["abort"] = True
            print(f"{stage.replace('_', ' ').title()} complete.")
            advance_offset()

    def do_discharge(resuming_rest_s: float = 0.0,
                     resume_active_state: Optional[dict] = None) -> None:
        proto = dcfg["protocol"]
        amps = cell.c_to_amps(float(dcfg["c_rate"]))
        v_min = _discharge_target_v(dcfg, "v_min", cell)
        # Per-phase override so e.g. a partial time-limited discharge can use
        # a shorter budget than a full CC-CV charge phase in the same test.
        phase_max_s = dcfg.get("max_duration_s", test.max_duration_s)

        if resuming_rest_s <= 0.0:
            print(f"Discharge phase: {proto}, target {v_min:.3f} V")
            progress_state["stage"] = "discharge"
            prior_elapsed_s = float((resume_active_state or {}).get(
                "phase_elapsed_s", 0.0))
            phase_time_base["value"] = prior_elapsed_s
            progress_state["phase_elapsed_s"] = prior_elapsed_s
            saved_controller = (resume_active_state or {}).get(
                "controller_state", {})
            remaining_max_s = (max(float(phase_max_s) - prior_elapsed_s, 0.0)
                               if phase_max_s is not None else None)
            if proto == "dst":
                # A custom "profile" is normalized to its own peak magnitude
                # (the largest |current| step maps to c_rate); omit it to
                # keep using the illustrative built-in staircase instead.
                steps = (profile_from_entries(dcfg["profile"], amps)
                         if "profile" in dcfg else dst_profile(scale_a=amps))
                ctrl = ProfileController(steps=steps, repeat=None, v_min=v_min)
                if saved_controller:
                    ctrl.restore(saved_controller)
                controller_snapshot["fn"] = ctrl.snapshot
                dev.set_current(steps[ctrl._idx].current_a)
                state["reason"] = dev.run(ctrl, period_s=period,
                                          max_duration_s=remaining_max_s,
                                          on_sample=on_sample)
            elif proto == "hppc":
                ctrl = PulseRestController(
                    discharge_a=amps, pulse_s=float(dcfg["pulse_s"]),
                    rest_s=float(dcfg["pulse_rest_s"]), v_min=v_min)
                if saved_controller:
                    ctrl.restore(saved_controller)
                controller_snapshot["fn"] = ctrl.snapshot
                if ctrl.phase == "discharge":
                    dev.set_discharge(amps)
                else:
                    dev.stop()
                state["reason"] = dev.run(ctrl, period_s=period,
                                          max_duration_s=remaining_max_s,
                                          on_sample=on_sample)
            elif proto == "crate":
                controller_snapshot["fn"] = None
                capacity_result = capacity_test(
                    dev, current_a=amps, v_cutoff=v_min,
                    logger=_LogAdapter(on_sample),
                    max_duration_s=(remaining_max_s if remaining_max_s
                                    is not None else 6 * 3600))
                state["reason"] = capacity_result.reason
            else:
                raise ValueError(f"unknown discharge protocol: {proto}")
            advance_offset()
            if state["reason"].startswith("safety stop:"):
                state["abort"] = True
                return
        rest(float(dcfg.get("rest_after_s", 0)), "rest_after_discharge",
             resuming_rest_s)

    def do_charge(resuming_rest_s: float = 0.0,
                  resume_active_state: Optional[dict] = None) -> None:
        proto = ccfg.get("protocol", "cc_cv")
        if proto != "cc_cv":
            raise ValueError(f"unknown charge protocol: {proto}")
        amps_c = cell.c_to_amps(float(ccfg["c_rate"]))
        v_max = _charge_target_v(ccfg, cell)
        cutoff_a = (cell.c_to_amps(float(ccfg["cutoff_c"]))
                   if "cutoff_c" in ccfg else cell.charge_taper_a)
        phase_max_s = ccfg.get("max_duration_s", test.max_duration_s)

        if resuming_rest_s <= 0.0:
            print(f"Charge phase: CC-CV at {amps_c:.3f} A, "
                  f"target {v_max:.3f} V, taper {cutoff_a:.3f} A")
            progress_state["stage"] = "charge"
            prior_elapsed_s = float((resume_active_state or {}).get(
                "phase_elapsed_s", 0.0))
            phase_time_base["value"] = prior_elapsed_s
            progress_state["phase_elapsed_s"] = prior_elapsed_s
            controller_snapshot["fn"] = None
            remaining_max_s = (max(float(phase_max_s) - prior_elapsed_s, 0.0)
                               if phase_max_s is not None else None)
            dev.limits.charge_cv = v_max
            dev.limits.charge_taper_a = cutoff_a
            dev.set_charge(amps_c)
            ctrl = CCCVChargeController(v_max=v_max)
            state["reason"] = dev.run(ctrl, period_s=period,
                                      max_duration_s=remaining_max_s,
                                      on_sample=on_sample)
            advance_offset()
            if state["reason"].startswith("safety stop:"):
                state["abort"] = True
                return
        rest(float(ccfg.get("rest_after_s", 0)), "rest_after_charge",
             resuming_rest_s)

    saved = resume_state or {}
    start_cycle = int(saved.get("cycle_index", 0))
    start_phase = int(saved.get("phase_index", 0))
    saved_stage = saved.get("stage", "between_phases")
    saved_phase_elapsed = float(saved.get("phase_elapsed_s", 0.0))
    saved_at = float(saved.get("saved_at_epoch", time.time()))
    offline_elapsed = max(time.time() - saved_at, 0.0)

    for cycle_index in range(start_cycle, max(repeat, 1)):
        first_phase = start_phase if cycle_index == start_cycle else 0
        for phase_index in range(first_phase, len(order)):
            phase = order[phase_index]
            progress_state["cycle_index"] = cycle_index
            progress_state["phase_index"] = phase_index
            resume_rest_s = 0.0
            resume_active_state = None
            if cycle_index == start_cycle and phase_index == first_phase:
                expected_rest = f"rest_after_{phase}"
                if saved_stage == expected_rest:
                    resume_rest_s = saved_phase_elapsed + offline_elapsed
                elif saved_stage == phase:
                    resume_active_state = saved
            if phase == "discharge" and dcfg:
                do_discharge(resume_rest_s, resume_active_state)
            elif phase == "charge" and ccfg:
                do_charge(resume_rest_s, resume_active_state)
            elif phase not in ("discharge", "charge"):
                raise ValueError(f"unknown protocol phase in order: {phase}")

            if state["abort"]:
                return RunResult(test.name, cell.id, state["reason"],
                                 logger.rows)

            next_cycle, next_phase = cycle_index, phase_index + 1
            if next_phase >= len(order):
                next_cycle, next_phase = cycle_index + 1, 0
            progress_state.update({
                "cycle_index": next_cycle, "phase_index": next_phase,
                "stage": "between_phases", "phase_elapsed_s": 0.0,
            })
            if progress is not None:
                progress(dict(progress_state), time_state["offset"])

    return RunResult(test.name, cell.id, state["reason"], logger.rows)


# ---------------- dispatcher ----------------

@dataclass
class RunResult:
    test_name: str
    cell_id: str
    reason: str
    samples: int
    capacity_ah: Optional[float] = None
    cycle_capacities: Optional[list[float]] = None


def _run_test_body(test: TestDef, cell: Cell, dev: EBCA20,
                   logger: DataLogger, resume_state: Optional[dict] = None,
                   progress: Optional[Callable[[dict], None]] = None
                   ) -> RunResult:
    dev.limits = cell.safety_limits()
    period = test.sample_period_s
    t = test.type

    resume_state = resume_state or {}
    resume_csv_s = float(resume_state.get("csv_elapsed_s", 0.0))
    session_base_s = resume_csv_s + (period if resume_state else 0.0)
    if session_base_s:
        logger = _OffsetLogger(logger, session_base_s)

    # Every test begins from a settled, zero-current state. Keep this in the
    # central dispatcher so it also applies to existing JSON definitions.
    initial_rest_s = test.initial_rest_s
    if initial_rest_s < 0:
        raise ValueError("initial_rest_s cannot be negative")
    if initial_rest_s > 0:
        print(f"Initial rest: {initial_rest_s:g} s")
        rest_state = {"last_t": 0.0}

        def on_rest_sample(m: Measurement, te: float) -> None:
            rest_state["last_t"] = te
            logger.log(m, te)

        dev.stop()

        # Spend part of the rest that was going to happen anyway on proving
        # the link actually works, instead of silently trusting it: a read
        # check (implausible/no voltage -> fails in seconds, not at
        # max_duration_s) and, budget permitting, a real C/10 write-and-
        # confirm pulse. Both consume time from initial_rest_s itself rather
        # than adding to it.
        link_measurement, link_check_elapsed_s = _verify_link(dev, cell, on_rest_sample)
        print(f"  Link check OK: V={link_measurement.voltage:.3f} V")
        remaining_after_link_s = max(initial_rest_s - link_check_elapsed_s, 0.0)
        if remaining_after_link_s >= PULSE_CHECK_MIN_BUDGET_S:
            pulse_check_elapsed_s = _verify_pulse_response(dev, cell, on_rest_sample)
            print(f"  Write-path check OK: confirmed a "
                  f"{cell.nominal_capacity_ah/10:.3f} A (C/10) discharge pulse")
        else:
            pulse_check_elapsed_s = 0.0
            print(f"  Skipping write-path pulse check: initial_rest_s leaves "
                  f"only {remaining_after_link_s:.1f}s (need "
                  f"{PULSE_CHECK_MIN_BUDGET_S:g}s)")
        dev.stop()
        remaining_rest_s = max(
            initial_rest_s - link_check_elapsed_s - pulse_check_elapsed_s, 0.0)
        rest_reason = dev.run(
            RestController(remaining_rest_s), period_s=period,
            max_duration_s=remaining_rest_s,
            on_sample=on_rest_sample)
        if rest_reason not in ("controller requested stop",
                               "duration limit reached"):
            return RunResult(
                test.name, cell.id,
                f"initial rest did not complete: {rest_reason}", logger.rows)
        print("Initial rest complete.")
        logger = _OffsetLogger(logger, rest_state["last_t"])

    phase_base_s = session_base_s + (rest_state["last_t"]
                                     if initial_rest_s > 0 else 0.0)

    def save_progress(state: dict, session_elapsed_s: float) -> None:
        if progress is not None:
            payload = dict(state)
            payload["csv_elapsed_s"] = phase_base_s + session_elapsed_s
            progress(payload)

    # Persist the completed safety rest before interpreting/starting the main
    # phase. A configuration error after this point can then resume with a
    # correct CSV time offset instead of duplicating the rest timestamps.
    save_progress({"stage": "main", "test_type": t,
                   "phase_elapsed_s": 0.0}, 0.0)

    saved_main = resume_state if resume_state.get("stage") == "main" else {}
    prior_main_elapsed_s = float(saved_main.get("phase_elapsed_s", 0.0))
    main_snapshot = {"fn": None}

    def on_sample(m: Measurement, te: float):
        logger.log(m, te)
        state = {"stage": "main", "test_type": t,
                 "phase_elapsed_s": prior_main_elapsed_s + te}
        if main_snapshot["fn"] is not None:
            state["controller_state"] = main_snapshot["fn"](te)
        save_progress(state, te)

    def remaining_duration(default_s: Optional[float]) -> Optional[float]:
        if default_s is None:
            return None
        return max(float(default_s) - prior_main_elapsed_s, 0.0)

    if t == "rest":
        if test.max_duration_s is None or test.max_duration_s <= 0:
            raise ValueError("rest test requires a positive max_duration_s")
        duration_s = remaining_duration(test.max_duration_s)
        print(f"Passive observation: zero applied current for "
              f"{duration_s:g} s")
        # Explicitly stop the tester before observing. RestController never
        # requests a non-zero setpoint; it only ends the run after duration_s.
        dev.stop()
        ctrl = RestController(duration_s)
        reason = dev.run(ctrl, period_s=period,
                         max_duration_s=duration_s,
                         on_sample=on_sample)
        if reason == "duration limit reached":
            reason = "passive observation complete"
        return RunResult(test.name, cell.id, reason, logger.rows)

    if t == "dst":
        # "profile_peak_c_rate" treats the profile's "current" values as
        # normalized to their own peak magnitude and rescales so that peak
        # reaches this many C, regardless of what units they're written in.
        # Omit it to keep interpreting "current" as a literal C-rate per step.
        peak_c_rate = test.raw.get("profile_peak_c_rate")
        if peak_c_rate is not None:
            steps = profile_from_entries(test.raw["profile"],
                                         cell.c_to_amps(float(peak_c_rate)))
        else:
            steps = [ProfileStep(current_a=test.amps(s["current"], cell),
                                 duration_s=float(s["duration_s"]))
                     for s in test.raw["profile"]]
        ctrl = ProfileController(steps=steps,
                                 repeat=int(test.raw.get("repeat", 1)))
        saved_controller = dict(saved_main.get("controller_state", {}))
        if saved_controller:
            saved_idx = min(int(saved_controller.get("step_index", 0)),
                            max(len(steps) - 1, 0))
            if steps and steps[saved_idx].current_a == 0:
                saved_controller["step_elapsed_s"] = float(
                    saved_controller.get("step_elapsed_s", 0.0)) + max(
                        time.time() - float(resume_state.get(
                            "saved_at_epoch", time.time())), 0.0)
            ctrl.restore(saved_controller)
        main_snapshot["fn"] = ctrl.snapshot
        if steps:
            dev.set_current(steps[ctrl._idx].current_a)
        reason = dev.run(ctrl, period_s=period,
                         max_duration_s=remaining_duration(test.max_duration_s),
                         on_sample=on_sample)
        return RunResult(test.name, cell.id, reason, logger.rows)

    if t == "charge":
        # Accept both standalone syntax (`current`) and the same nested
        # `charge` block used by composite protocols. This makes a charge-only
        # file easy to derive without silently discarding CV/taper settings.
        charge_cfg = test.raw.get("charge")
        if isinstance(charge_cfg, dict):
            if charge_cfg.get("protocol", "cc_cv") != "cc_cv":
                raise ValueError("standalone charge only supports cc_cv")
            if "c_rate" not in charge_cfg:
                raise ValueError("charge block requires c_rate")
            amps = cell.c_to_amps(float(charge_cfg["c_rate"]))
            v_max = _charge_target_v(charge_cfg, cell)
            cutoff_a = (cell.c_to_amps(float(charge_cfg["cutoff_c"]))
                        if "cutoff_c" in charge_cfg
                        else cell.charge_taper_a)
            charge_max_s = charge_cfg.get("max_duration_s",
                                          test.max_duration_s)
            rest_after_s = float(charge_cfg.get("rest_after_s", 0.0))
        else:
            if "current" not in test.raw:
                raise ValueError(
                    "charge test requires current or a charge.c_rate block")
            amps = test.amps(test.raw["current"], cell)
            v_max = cell.max_v
            cutoff_a = cell.charge_taper_a
            charge_max_s = test.max_duration_s
            rest_after_s = 0.0

        resumed_charge_rest = resume_state.get("stage") == "rest_after_charge"
        main_last_t = {"value": 0.0}
        original_on_sample = on_sample

        def on_charge_sample(m: Measurement, te: float) -> None:
            main_last_t["value"] = te
            original_on_sample(m, te)

        if not resumed_charge_rest:
            print(f"Charge phase: CC-CV at {amps:.3f} A, "
                  f"target {v_max:.3f} V, taper {cutoff_a:.3f} A")
            dev.limits.charge_cv = v_max
            dev.limits.charge_taper_a = cutoff_a
            dev.set_charge(amps)
            ctrl = CCCVChargeController(v_max=v_max)
            reason = dev.run(
                ctrl, period_s=period,
                max_duration_s=remaining_duration(charge_max_s),
                on_sample=on_charge_sample)
        else:
            reason = "charge complete; resuming final rest"

        if rest_after_s > 0 and not reason.startswith("safety stop:"):
            already_rested_s = 0.0
            if resumed_charge_rest:
                already_rested_s = float(
                    resume_state.get("phase_elapsed_s", 0.0)) + max(
                        time.time() - float(resume_state.get(
                            "saved_at_epoch", time.time())), 0.0)
            remaining_rest_s = max(rest_after_s - already_rested_s, 0.0)
            if remaining_rest_s > 0:
                print(f"Final rest: {remaining_rest_s:g} s")
                charge_time_s = (0.0 if resumed_charge_rest
                                 else main_last_t["value"])
                rest_logger = _OffsetLogger(logger, charge_time_s)
                rest_last_t = {"value": 0.0}

                def on_charge_rest(m: Measurement, te: float) -> None:
                    rest_last_t["value"] = te
                    rest_logger.log(m, te)
                    save_progress({
                        "stage": "rest_after_charge", "test_type": t,
                        "phase_elapsed_s": already_rested_s + te,
                    }, charge_time_s + te)

                dev.stop()
                reason = dev.run(
                    RestController(remaining_rest_s), period_s=period,
                    max_duration_s=remaining_rest_s,
                    on_sample=on_charge_rest)
                if reason == "duration limit reached":
                    reason = "charge and final rest complete"
                print("Final rest complete.")
        return RunResult(test.name, cell.id, reason, logger.rows)

    if t == "discharge":
        amps = test.amps(test.raw["current"], cell)
        v_cutoff = _discharge_target_v(test.raw, "v_cutoff", cell)
        res: CapacityResult = capacity_test(
            dev, current_a=amps, v_cutoff=v_cutoff,
            logger=_LogAdapter(on_sample),
            max_duration_s=(remaining_duration(
                test.max_duration_s or 6 * 3600) or 0.0))
        return RunResult(test.name, cell.id, res.reason or "discharge complete",
                         logger.rows, capacity_ah=res.capacity_ah)

    if t == "hppc":
        amps = test.amps(test.raw["pulse_current"], cell)
        v_min = _discharge_target_v(test.raw, "v_min", cell)
        ctrl = PulseRestController(
            discharge_a=amps,
            pulse_s=float(test.raw["pulse_s"]),
            rest_s=float(test.raw["rest_s"]),
            v_min=v_min,
        )
        saved_controller = dict(saved_main.get("controller_state", {}))
        if saved_controller:
            if saved_controller.get("pulse_phase") == "rest":
                saved_controller["pulse_phase_elapsed_s"] = float(
                    saved_controller.get("pulse_phase_elapsed_s", 0.0)) + max(
                        time.time() - float(resume_state.get(
                            "saved_at_epoch", time.time())), 0.0)
            ctrl.restore(saved_controller)
        main_snapshot["fn"] = ctrl.snapshot
        if ctrl.phase == "discharge":
            dev.set_discharge(amps)
        else:
            dev.stop()
        reason = dev.run(ctrl, period_s=period,
                         max_duration_s=remaining_duration(test.max_duration_s),
                         on_sample=on_sample)
        return RunResult(test.name, cell.id, reason, logger.rows)

    if t == "protocol":
        return _run_protocol(test, cell, dev, logger, resume_state,
                             save_progress)

    if t == "cycle":
        ctrl = CycleController(
            charge_a=test.amps(test.raw["charge_current"], cell),
            discharge_a=test.amps(test.raw["discharge_current"], cell),
            v_max=cell.max_v, v_min=cell.min_v,
            rest_after_charge_s=float(test.raw.get("rest_after_charge_s", 300)),
            rest_after_discharge_s=float(
                test.raw.get("rest_after_discharge_s", 300)),
            repeat=int(test.raw.get("repeat", 1)),
        )
        saved_controller = dict(saved_main.get("controller_state", {}))
        if saved_controller:
            if saved_controller.get("cycle_phase") in ("rest_c", "rest_d"):
                saved_controller["cycle_phase_elapsed_s"] = float(
                    saved_controller.get("cycle_phase_elapsed_s", 0.0)) + max(
                        time.time() - float(resume_state.get(
                            "saved_at_epoch", time.time())), 0.0)
            ctrl.restore(saved_controller)
        main_snapshot["fn"] = ctrl.snapshot
        if ctrl.phase == "charge":
            dev.set_charge(ctrl.charge_a)
        elif ctrl.phase == "discharge":
            dev.set_discharge(ctrl.discharge_a)
        else:
            dev.stop()
        reason = dev.run(ctrl, period_s=period,
                         max_duration_s=remaining_duration(test.max_duration_s),
                         on_sample=on_sample)
        return RunResult(test.name, cell.id, reason, logger.rows,
                         cycle_capacities=ctrl.cycle_capacities)

    raise ValueError(f"unknown test type: {t}")


def _is_recoverable_voltage_cutoff(test: TestDef, cell: Cell,
                                   result: RunResult, dev: EBCA20) -> bool:
    """Distinguish normal low-voltage termination from genuine hard faults."""
    reason = result.reason.casefold()
    if reason.startswith("safety stop: v_min"):
        return True
    if result.reason == "controller requested stop" and test.type in (
            "hppc", "discharge"):
        return True
    if result.reason == "controller requested stop" and test.type == "dst":
        target = _discharge_target_v(test.raw, "v_min", cell)
        return dev.last is not None and dev.last.voltage <= target + 0.02
    return False


def run_test(test: TestDef, cell: Cell, dev: EBCA20,
             logger: DataLogger, resume_state: Optional[dict] = None,
             progress: Optional[Callable[[dict], None]] = None) -> RunResult:
    """Run a test and finish recoverable voltage cutoffs with a safe rest.

    Low-voltage termination is a normal end-of-discharge condition: STOP is
    issued immediately and measurements continue for a default 600-second
    recovery rest. Command mismatch, overcurrent, and hard overvoltage remain
    aborting faults and never enter this path.
    """
    saved = resume_state or {}
    tracked = _TrackingLogger(logger)
    resuming_recovery = saved.get("stage") == "voltage_cutoff_recovery_rest"
    recovery_elapsed = 0.0
    if resuming_recovery:
        tracked.last_t = float(saved.get("csv_elapsed_s", 0.0))
        recovery_elapsed = float(saved.get("phase_elapsed_s", 0.0))
        recovery_elapsed += max(
            time.time()-float(saved.get("saved_at_epoch", time.time())), 0.0)
        cutoff_reason = str(saved.get("cutoff_reason", "voltage cutoff reached"))
        result = RunResult(test.name, cell.id, cutoff_reason, tracked.rows)
        print("Resuming voltage-cutoff recovery rest.")
    else:
        result = _run_test_body(test, cell, dev, tracked, resume_state, progress)
        if not _is_recoverable_voltage_cutoff(test, cell, result, dev):
            return result
        cutoff_reason = result.reason

    recovery_s = float(test.raw.get("voltage_cutoff_rest_s", 600.0))
    if recovery_s < 0:
        raise ValueError("voltage_cutoff_rest_s cannot be negative")
    remaining_recovery_s = max(recovery_s-recovery_elapsed, 0.0)
    print(f"Voltage cutoff reached. STOP sent; recovery rest remaining: "
          f"{remaining_recovery_s:g} s")
    dev.stop()
    if remaining_recovery_s == 0:
        return RunResult(test.name, cell.id,
                         ("voltage cutoff reached; recovery rest disabled"
                          if recovery_s == 0 else
                          f"voltage cutoff reached; recovery rest complete ({recovery_s:g} s)"),
                         tracked.rows, result.capacity_ah,
                         result.cycle_capacities)

    # Ignore only the already-triggered low-voltage floor while commanding
    # zero current. All other device safety checks and command confirmation
    # remain active. RecoveryRestController repeats STOP if current persists.
    original_v_min = dev.limits.v_min
    rest_offset = tracked.last_t + test.sample_period_s
    rest_logger = _OffsetLogger(tracked, rest_offset)
    rest_last = {"value": 0.0}

    def on_recovery_sample(m: Measurement, te: float) -> None:
        rest_last["value"] = te
        rest_logger.log(m, te)
        if progress is not None:
            progress({
                "stage": "voltage_cutoff_recovery_rest",
                "test_type": test.type,
                "phase_elapsed_s": recovery_elapsed + te,
                "csv_elapsed_s": rest_offset + te,
                "cutoff_reason": cutoff_reason,
            })

    try:
        dev.limits.v_min = -float("inf")
        rest_reason = dev.run(
            RecoveryRestController(remaining_recovery_s),
            period_s=test.sample_period_s,
            max_duration_s=remaining_recovery_s,
            on_sample=on_recovery_sample)
    finally:
        dev.limits.v_min = original_v_min
        dev.stop()

    if rest_reason not in ("controller requested stop", "duration limit reached"):
        reason = ("voltage cutoff reached; recovery rest failed: "
                  f"{rest_reason}")
    else:
        reason = f"voltage cutoff reached; recovery rest complete ({recovery_s:g} s)"
        print("Voltage-cutoff recovery rest complete.")
    return RunResult(test.name, cell.id, reason, tracked.rows,
                     result.capacity_ah, result.cycle_capacities)
