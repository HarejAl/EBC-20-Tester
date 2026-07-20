"""
Test runner: turn a JSON test definition + cell library into an executed run.

Supported test types:
  - "dst"       : dynamic staircase current profile (SOC validation)
  - "charge"    : constant-current charge to the cell's max_v
  - "discharge" : constant-current discharge to min_v (SOH capacity)
  - "cycle"     : repeated charge / rest / discharge / rest
  - "hppc"      : discharge pulse / rest, repeated until v_min (resistance/
                  relaxation characterization)
  - "protocol"  : composite [discharge (dst/hppc/crate) -> rest -> CC-CV
                  charge -> rest], repeated "repeat" times; either phase may
                  be omitted (JSON null) for a charge-only or discharge-only
                  run. Optional "order" (default ["discharge", "charge"])
                  controls phase sequence -- set ["charge", "discharge"] to
                  charge first. See config/tests/cellAlex_protocol.json.

Currents in test files are C-rate (current_unit="C") and scaled by the cell's
nominal capacity, or absolute amps (current_unit="A").

Every run derives SafetyLimits from the cell so the voltage/current bounds you
defined in the cell JSON are enforced both software-side and (via cutoff fields)
device-side.
"""

from __future__ import annotations
import json
import time
from dataclasses import dataclass
from typing import Optional

from .celllib import Cell, CellLibrary
from .device import EBCA20
from .protocol import Measurement
from .experiment import (DataLogger, ProfileController, ProfileStep,
                         dst_profile, capacity_test, CapacityResult)


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

    def __call__(self, m: Measurement, t: float) -> Optional[float]:
        if self._phase_start is None:
            self._phase_start = t

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
        # terminate when CV target reached, or device auto-stopped (idle)
        # after having charged (taper/cutoff completed)
        if m.voltage >= self.v_max and m.mode == "charge":
            return float("nan")
        if self._charged and m.mode == "idle":
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

    def __call__(self, m: Measurement, t: float) -> Optional[float]:
        if self._phase_start is None:
            self._phase_start = t

        if self.phase == "charge":
            if (m.voltage >= self.v_max and m.mode == "charge") or \
               (self._last_cmd == +self.charge_a and m.mode == "idle"
                    and t - self._phase_start > 1.0):
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


def _run_protocol(test: "TestDef", cell: Cell, dev: EBCA20,
                  logger: DataLogger) -> "RunResult":
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
    state = {"reason": "nothing to do"}

    def on_sample(m: Measurement, te: float) -> None:
        time_state["last"] = te
        logger.log(m, time_state["offset"] + te)

    def advance_offset() -> None:
        time_state["offset"] += time_state["last"]
        time_state["last"] = 0.0

    def rest(duration_s: float) -> None:
        if duration_s > 0:
            dev.stop()
            state["reason"] = dev.run(
                RestController(duration_s), period_s=period,
                max_duration_s=duration_s + 60, on_sample=on_sample)
            advance_offset()

    def do_discharge() -> None:
        proto = dcfg["protocol"]
        amps = cell.c_to_amps(float(dcfg["c_rate"]))
        # Never allow a lower cutoff than the cell's own min_v floor.
        v_min = max(float(dcfg.get("v_min", cell.min_v)), cell.min_v)
        # Per-phase override so e.g. a partial time-limited discharge can use
        # a shorter budget than a full CC-CV charge phase in the same test.
        phase_max_s = dcfg.get("max_duration_s", test.max_duration_s)

        if proto == "dst":
            steps = dst_profile(scale_a=amps)
            ctrl = ProfileController(steps=steps, repeat=None, v_min=v_min)
            dev.set_current(steps[0].current_a)
            state["reason"] = dev.run(ctrl, period_s=period,
                                      max_duration_s=phase_max_s,
                                      on_sample=on_sample)
        elif proto == "hppc":
            ctrl = PulseRestController(
                discharge_a=amps, pulse_s=float(dcfg["pulse_s"]),
                rest_s=float(dcfg["pulse_rest_s"]), v_min=v_min)
            dev.set_discharge(amps)
            state["reason"] = dev.run(ctrl, period_s=period,
                                      max_duration_s=phase_max_s,
                                      on_sample=on_sample)
        elif proto == "crate":
            capacity_test(
                dev, current_a=amps, v_cutoff=v_min,
                logger=_LogAdapter(on_sample),
                max_duration_s=phase_max_s or 6 * 3600)
            state["reason"] = "discharge complete"
        else:
            raise ValueError(f"unknown discharge protocol: {proto}")
        advance_offset()
        rest(float(dcfg.get("rest_after_s", 0)))

    def do_charge() -> None:
        proto = ccfg.get("protocol", "cc_cv")
        if proto != "cc_cv":
            raise ValueError(f"unknown charge protocol: {proto}")
        amps_c = cell.c_to_amps(float(ccfg["c_rate"]))
        # Never allow the CV target above the cell's own max_v ceiling.
        v_max = min(float(ccfg.get("v_max", cell.max_v)), cell.max_v)
        cutoff_a = (cell.c_to_amps(float(ccfg["cutoff_c"]))
                   if "cutoff_c" in ccfg else cell.charge_taper_a)
        phase_max_s = ccfg.get("max_duration_s", test.max_duration_s)

        dev.limits.charge_cv = v_max
        dev.limits.charge_taper_a = cutoff_a
        dev.set_charge(amps_c)
        ctrl = CCCVChargeController(v_max=v_max)
        state["reason"] = dev.run(ctrl, period_s=period,
                                  max_duration_s=phase_max_s,
                                  on_sample=on_sample)
        advance_offset()
        rest(float(ccfg.get("rest_after_s", 0)))

    for _ in range(max(repeat, 1)):
        for phase in order:
            if phase == "discharge" and dcfg:
                do_discharge()
            elif phase == "charge" and ccfg:
                do_charge()
            elif phase not in ("discharge", "charge"):
                raise ValueError(f"unknown protocol phase in order: {phase}")

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


def run_test(test: TestDef, cell: Cell, dev: EBCA20,
             logger: DataLogger) -> RunResult:
    dev.limits = cell.safety_limits()
    period = test.sample_period_s
    t = test.type

    def on_sample(m: Measurement, te: float):
        logger.log(m, te)

    if t == "dst":
        steps = [ProfileStep(current_a=test.amps(s["current"], cell),
                             duration_s=float(s["duration_s"]))
                 for s in test.raw["profile"]]
        ctrl = ProfileController(steps=steps,
                                 repeat=int(test.raw.get("repeat", 1)))
        if steps:
            dev.set_current(steps[0].current_a)
        reason = dev.run(ctrl, period_s=period,
                         max_duration_s=test.max_duration_s,
                         on_sample=on_sample)
        return RunResult(test.name, cell.id, reason, logger.rows)

    if t == "charge":
        amps = test.amps(test.raw["current"], cell)
        dev.set_charge(amps)
        ctrl = CCCVChargeController(v_max=cell.max_v)
        reason = dev.run(ctrl, period_s=period,
                         max_duration_s=test.max_duration_s,
                         on_sample=on_sample)
        return RunResult(test.name, cell.id, reason, logger.rows)

    if t == "discharge":
        amps = test.amps(test.raw["current"], cell)
        # A test may request a more conservative (higher) cutoff than the
        # cell's own min_v, but never a lower one -- min_v is the safety floor.
        v_cutoff = max(float(test.raw.get("v_cutoff", cell.min_v)), cell.min_v)
        res: CapacityResult = capacity_test(
            dev, current_a=amps, v_cutoff=v_cutoff, logger=logger,
            max_duration_s=test.max_duration_s or 6 * 3600)
        return RunResult(test.name, cell.id, "discharge complete",
                         logger.rows, capacity_ah=res.capacity_ah)

    if t == "hppc":
        amps = test.amps(test.raw["pulse_current"], cell)
        # Never allow a lower cutoff than the cell's own min_v floor.
        v_min = max(float(test.raw.get("v_min", cell.min_v)), cell.min_v)
        ctrl = PulseRestController(
            discharge_a=amps,
            pulse_s=float(test.raw["pulse_s"]),
            rest_s=float(test.raw["rest_s"]),
            v_min=v_min,
        )
        dev.set_discharge(amps)
        reason = dev.run(ctrl, period_s=period,
                         max_duration_s=test.max_duration_s,
                         on_sample=on_sample)
        return RunResult(test.name, cell.id, reason, logger.rows)

    if t == "protocol":
        return _run_protocol(test, cell, dev, logger)

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
        dev.set_charge(ctrl.charge_a)
        reason = dev.run(ctrl, period_s=period,
                         max_duration_s=test.max_duration_s,
                         on_sample=on_sample)
        return RunResult(test.name, cell.id, reason, logger.rows,
                         cycle_capacities=ctrl.cycle_capacities)

    raise ValueError(f"unknown test type: {t}")
