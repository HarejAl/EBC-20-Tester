"""Offline regression for the startup link check (read path + write path).

Tests ebc.runner._verify_link / _verify_pulse_response's validation logic
directly against a lightweight fake device (not a real transport/simulator),
since what's under test here is the accept/reject logic itself, not the
serial stack -- that part is already covered by selftest_reconnect.py.
"""

from __future__ import annotations

import os

from _paths import ROOT
from ebc.celllib import CellLibrary
from ebc.runner import _verify_link, _verify_pulse_response

cell = CellLibrary(os.path.join(ROOT, "config", "cells")).get("LP604765")


class _FakeMeasurement:
    def __init__(self, voltage: float, current: float = 0.0, mode: str = "idle"):
        self.voltage = voltage
        self.current = current
        self.mode = mode


class _FakeDev:
    """Replays a fixed measurement sequence through the real controller
    protocol (on_sample, then controller(m, t) -> next cmd/None/nan-stop),
    without any real transport underneath."""

    def __init__(self, measurements: list[_FakeMeasurement]):
        self._measurements = list(measurements)

    def run(self, controller, period_s, max_duration_s, on_sample,
           no_response_timeout_s=None) -> str:
        t = 0.0
        for m in self._measurements:
            on_sample(m, t)
            cmd = controller(m, t)
            if cmd is not None and cmd != cmd:  # NaN sentinel = stop
                return "controller requested stop"
            t += period_s
            if t >= max_duration_s:
                return "duration limit reached"
        return "duration limit reached"

    def stop(self) -> None:
        pass

    def set_discharge(self, current_a: float) -> None:
        self._commanded_discharge_a = current_a


def check_good_voltage() -> None:
    dev = _FakeDev([_FakeMeasurement(3.7)])
    m, elapsed = _verify_link(dev, cell, lambda m, t: None)
    assert m.voltage == 3.7
    assert elapsed >= 0.0


def check_implausible_voltage_rejected() -> None:
    # Cell window is 2.75-4.25 V; 0.05 V is nowhere near plausible.
    dev = _FakeDev([_FakeMeasurement(0.05)])
    try:
        _verify_link(dev, cell, lambda m, t: None)
        raised = False
    except RuntimeError as exc:
        raised = True
        assert "implausible voltage" in str(exc)
    assert raised, "an implausible startup voltage must be rejected, not trusted"


def check_no_measurement_rejected() -> None:
    dev = _FakeDev([])
    try:
        _verify_link(dev, cell, lambda m, t: None)
        raised = False
    except RuntimeError as exc:
        raised = True
        assert "no measurement" in str(exc)
    assert raised, "zero measurements during the link check must be rejected"


def check_pulse_confirmed() -> None:
    pulse_a = cell.nominal_capacity_ah / 10.0
    dev = _FakeDev([_FakeMeasurement(3.7, current=pulse_a, mode="discharge")])
    elapsed = _verify_pulse_response(dev, cell, lambda m, t: None)
    assert elapsed >= 0.0


def check_pulse_unconfirmed_rejected() -> None:
    # Device stays idle the whole time despite the commanded discharge --
    # exactly the "write path is dead" symptom this check exists to catch.
    dev = _FakeDev([_FakeMeasurement(3.7, current=0.0, mode="idle")] * 10)
    try:
        _verify_pulse_response(dev, cell, lambda m, t: None)
        raised = False
    except RuntimeError as exc:
        raised = True
        assert "never confirmed" in str(exc)
    assert raised, "an unconfirmed write-path pulse must be rejected"


def main() -> None:
    check_good_voltage()
    check_implausible_voltage_rejected()
    check_no_measurement_rejected()
    check_pulse_confirmed()
    check_pulse_unconfirmed_rejected()
    print("STARTUP LINK CHECK REGRESSION PASSED")
    print("  plausible voltage -> accepted")
    print("  implausible voltage -> rejected")
    print("  zero measurements -> rejected")
    print("  confirmed C/10 pulse -> accepted")
    print("  unconfirmed C/10 pulse -> rejected")


if __name__ == "__main__":
    main()
