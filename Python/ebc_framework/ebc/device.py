"""
High-level EBC-A20 controller.

This is the API your experiments use. It wraps a transport (real or simulator),
handles connect/handshake, exposes set_discharge / set_charge / stop, and runs a
polling loop that decodes status frames into Measurement objects.

Safety model (important for a real cell):
  - Every charge/discharge command carries the DEVICE's own cutoff field
    (low-voltage cutoff for discharge, CV target + taper current for charge).
    That is the hardware backstop and works even if the PC/loop stalls.
  - The software loop adds a second, independent check (SafetyLimits) and will
    command STOP if any soft limit is crossed.
  Never rely on the software layer alone.
"""

from __future__ import annotations
import math
import time
from dataclasses import dataclass
from typing import Callable, Optional

from . import protocol as P
from .transport import SerialTransport, SimulatorTransport


@dataclass
class SafetyLimits:
    v_max: float = 4.25         # hard ceiling, software stop
    v_min: float = 2.50         # hard floor, software stop
    i_max: float = 5.0          # |current| ceiling (used by the post-hoc
                                # measured-current safety trip; the larger of
                                # charge/discharge so it doesn't false-trigger)
    # Per-direction ceilings (cell's own rating), used to clamp *before*
    # sending a command -- never raises, just silently caps the request.
    i_max_charge: float = 5.0
    i_max_discharge: float = 5.0
    # device-side backstops (passed into the command frames)
    discharge_cutoff_v: float = 2.50
    charge_cv: float = 4.20
    charge_taper_a: float = 0.10


class EBCA20:
    # A command is retried only after several consecutive contradictory
    # measurements.  This filters one stale status frame while ensuring that a
    # lost STOP/START packet cannot leave the cell in the wrong state for an
    # entire protocol step.
    COMMAND_MISMATCH_SAMPLES = 3
    COMMAND_CONFIRM_SAMPLES = 2
    COMMAND_MAX_RETRIES = 3

    def __init__(self, transport, limits: Optional[SafetyLimits] = None):
        self._t = transport
        self.limits = limits or SafetyLimits()
        self._last: Optional[P.Measurement] = None
        self._stopped_reason: Optional[str] = None
        self._desired_signed_a = 0.0

    # ---- construction helpers ----
    @classmethod
    def on_serial(cls, port: str, limits: Optional[SafetyLimits] = None):
        return cls(SerialTransport(port), limits)

    @classmethod
    def on_simulator(cls, limits: Optional[SafetyLimits] = None, **sim_kwargs):
        return cls(SimulatorTransport(**sim_kwargs), limits)

    # ---- lifecycle ----
    def connect(self) -> None:
        self._t.write(P.build_connect())

    def disconnect(self) -> None:
        self.stop()
        self._t.write(P.build_disconnect())
        self._t.close()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.disconnect()

    # ---- commands ----
    def set_discharge(self, current_a: float) -> None:
        current_a = self._clamp_current(
            current_a, self.limits.i_max_discharge, P.EBC_MAX_DISCHARGE_A,
            "discharge")
        self._desired_signed_a = -current_a
        self._t.write(P.build_start_discharge(
            current_a, cutoff_v=self.limits.discharge_cutoff_v))

    def set_charge(self, current_a: float) -> None:
        current_a = self._clamp_current(
            current_a, self.limits.i_max_charge, P.EBC_MAX_CHARGE_A, "charge")
        self._desired_signed_a = current_a
        self._t.write(P.build_start_charge(
            current_a, charge_v=self.limits.charge_cv,
            cutoff_a=self.limits.charge_taper_a))

    def set_current(self, signed_a: float) -> None:
        """
        Battery-centric sign: positive = charge, negative = discharge.

        Confirmed on real hardware 2026-07-01: a start_charge/start_discharge
        frame sent while the device is already actively driving current is
        silently ignored -- it only takes effect from idle. So every setpoint
        change forces an explicit stop (with a brief settle delay) first, not
        just the transitions to/from zero.
        """
        if signed_a == 0:
            self.stop()
            return
        self.stop()
        time.sleep(0.3)
        if signed_a > 0:
            self.set_charge(signed_a)
        else:
            self.set_discharge(-signed_a)

    def stop(self) -> None:
        self._desired_signed_a = 0.0
        self._t.write(P.build_stop())

    def _command_is_coherent(self, m: P.Measurement) -> bool:
        """Whether a measurement agrees with the last requested operating state."""
        desired = self._desired_signed_a
        zero_tolerance_a = 0.03
        if desired == 0.0:
            return abs(m.current) <= zero_tolerance_a

        target = abs(desired)
        tolerance = max(0.05, 0.15 * target)
        if desired < 0.0:
            return (m.mode == "discharge" and m.current > zero_tolerance_a
                    and abs(m.current - target) <= tolerance)

        # During CC-CV charging the current is expected to taper below the
        # requested CC value, so only an excessive current is contradictory.
        return (m.mode == "charge" and m.current > zero_tolerance_a
                and m.current <= target + tolerance)

    def _command_description(self) -> str:
        desired = self._desired_signed_a
        if desired < 0:
            return f"discharge at {abs(desired):.3f} A"
        if desired > 0:
            return f"charge at {desired:.3f} A"
        return "idle (0 A)"

    def _clamp_current(self, current_a: float, cell_limit_a: float,
                       ebc_limit_a: float, direction: str) -> float:
        """
        Silently cap a requested current to the stricter of the cell's own
        rating and the EBC-A20's absolute hardware ceiling. Never raises --
        a test that over-scales a profile should just run at the clamped
        rate, and the logged data (the device's own measured current) will
        reflect what was actually applied, not what was originally asked for.
        """
        # Command currents are encoded in 0.01 A increments. Round the cap
        # downward so encoding can never round a limit such as 1.906 A up to
        # 1.91 A and thereby exceed the value declared by the cell file.
        raw_cap = min(cell_limit_a, ebc_limit_a)
        cap = math.floor(raw_cap * 100.0 + 1e-9) / 100.0
        requested = abs(current_a)
        if requested > cap + 1e-9:
            print(f"  [safety] {direction} current {requested:.3f} A exceeds "
                  f"limit {cap:.3f} A -- clamped to {cap:.3f} A")
            return cap
        return requested

    # ---- measurement ----
    def poll(self) -> Optional[P.Measurement]:
        """Return the most recent decoded measurement, or None if none yet."""
        latest = None
        for frame in self._t.read_frames():
            m = P.parse_status(frame)
            if m is not None:
                latest = m
        if latest is not None:
            self._last = latest
        return latest

    @property
    def last(self) -> Optional[P.Measurement]:
        return self._last

    def check_safety(self, m: P.Measurement) -> Optional[str]:
        """Return a reason string if a soft limit is violated, else None."""
        if m.voltage >= self.limits.v_max:
            return f"v_max {self.limits.v_max} V reached ({m.voltage:.3f})"
        if m.voltage <= self.limits.v_min and m.mode == "discharge":
            return f"v_min {self.limits.v_min} V reached ({m.voltage:.3f})"
        # Check the measured current against the active direction's cell limit
        # as well as the tester's own hardware ceiling. Using one shared
        # max(charge, discharge) value could miss an overcurrent in the
        # lower-rated direction.
        if m.mode == "charge":
            current_limit = min(self.limits.i_max_charge,
                                P.EBC_MAX_CHARGE_A)
        elif m.mode == "discharge":
            current_limit = min(self.limits.i_max_discharge,
                                P.EBC_MAX_DISCHARGE_A)
        else:
            current_limit = self.limits.i_max
        if abs(m.current) > current_limit + 1e-9:
            return (f"{m.mode} current limit {current_limit:.3f} A exceeded "
                    f"({m.current:.3f} A)")
        return None

    def run(self, controller: Callable[[P.Measurement, float], Optional[float]],
            period_s: float = 1.0, max_duration_s: Optional[float] = None,
            on_sample: Optional[Callable[[P.Measurement, float], None]] = None
            ) -> str:
        """
        Generic control loop.

          controller(measurement, t_elapsed) -> next signed current (A),
              or None to keep the present setpoint, or float('nan') to stop.
          on_sample(measurement, t_elapsed) -> called for logging each sample.

        Time base: if the transport exposes a `time_accel` (the simulator),
        the elapsed time handed to the controller is accelerated so that
        profile-step durations compress together with the cell physics. On real
        hardware time_accel is absent and t_elapsed is true wall-clock seconds.

        Returns a string describing why the loop ended.
        """
        accel = getattr(self._t, "time_accel", 1.0) or 1.0
        t0 = time.time()
        next_tick = t0
        mismatch_samples = 0
        coherent_samples = 0
        command_retries = 0
        while True:
            now = time.time()
            t_elapsed = (now - t0) * accel
            if max_duration_s is not None and t_elapsed >= max_duration_s:
                self.stop()
                return "duration limit reached"

            m = self.poll()
            if m is not None:
                reason = self.check_safety(m)
                if reason is not None:
                    self.stop()
                    return f"safety stop: {reason}"
                if on_sample is not None:
                    on_sample(m, t_elapsed)

                cmd = controller(m, t_elapsed)
                if cmd is not None:
                    if cmd != cmd:  # NaN sentinel = controller requests stop
                        self.stop()
                        return "controller requested stop"
                    self.set_current(cmd)
                    # This measurement predates the command that was just
                    # issued, so confirmation starts with the next frame.
                    mismatch_samples = 0
                    coherent_samples = 0
                    command_retries = 0
                elif self._command_is_coherent(m):
                    mismatch_samples = 0
                    coherent_samples += 1
                    if coherent_samples >= self.COMMAND_CONFIRM_SAMPLES:
                        command_retries = 0
                else:
                    coherent_samples = 0
                    mismatch_samples += 1
                    if mismatch_samples >= self.COMMAND_MISMATCH_SAMPLES:
                        requested = self._desired_signed_a
                        observed = f"{m.mode} at {m.current:.3f} A"
                        if command_retries >= self.COMMAND_MAX_RETRIES:
                            wanted = self._command_description()
                            self.stop()
                            return ("safety stop: command confirmation failed; "
                                    f"requested {wanted}, observed {observed}")
                        command_retries += 1
                        print("  [control] requested state not confirmed "
                              f"({observed}); retry {command_retries}/"
                              f"{self.COMMAND_MAX_RETRIES}: "
                              f"{self._command_description()}")
                        self.set_current(requested)
                        mismatch_samples = 0

            next_tick += period_s / accel
            sleep = next_tick - time.time()
            if sleep > 0:
                time.sleep(sleep)
