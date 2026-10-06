"""Offline regression for capacity_test()'s tolerance of a single corrupted
or misdecoded status frame during an otherwise-normal discharge.

Reproduces the real-hardware failure seen 2026-07-25: one stray
mode="unknown" sample amid a genuine 2C discharge (voltage never moved,
current never actually rose) ended the whole capacity test after ~1s
instead of the expected ~30 min. Confirms the fix (require several
consecutive non-discharge samples, not one) without undoing the original
purpose of that check -- a real end-of-discharge must still be caught
promptly.
"""

from __future__ import annotations

from ebc.experiment import capacity_test


class _FakeMeasurement:
    def __init__(self, voltage: float, current: float = 0.0,
                mode: str = "idle", charge: float = 0.0):
        self.voltage = voltage
        self.current = current
        self.mode = mode
        self.charge = charge


class _FakeLimits:
    discharge_cutoff_v = None


class _FakeDev:
    """Replays a fixed measurement sequence through capacity_test()'s real
    controller protocol, without any real transport underneath."""

    def __init__(self, measurements: list[_FakeMeasurement]):
        self._measurements = list(measurements)
        self.limits = _FakeLimits()

    def set_discharge(self, current_a: float) -> None:
        self._commanded_a = current_a

    def run(self, controller, period_s, max_duration_s, on_sample) -> str:
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


def check_single_glitch_does_not_end_discharge() -> None:
    measurements = (
        [_FakeMeasurement(4.2, current=3.8, mode="discharge")] * 3 +
        [_FakeMeasurement(4.2, current=0.0, mode="unknown")] +   # the glitch
        [_FakeMeasurement(4.2, current=3.8, mode="discharge")] * 20
    )
    dev = _FakeDev(measurements)
    result = capacity_test(dev, current_a=3.8, v_cutoff=2.75, max_duration_s=100)
    assert result.reason == "duration limit reached", (
        "a single corrupted sample must not end the discharge", result.reason)
    assert result.duration_s > 20, result.duration_s


def check_genuine_cutoff_still_ends_discharge() -> None:
    """A real end-of-discharge -- sustained non-discharge mode, matching the
    device's own cutoff backstop kicking in -- must still end the test
    promptly. This is the original behavior the debounce must not undo."""
    measurements = (
        [_FakeMeasurement(3.5, current=3.8, mode="discharge")] * 5 +
        [_FakeMeasurement(3.6, current=0.0, mode="idle")] * 10
    )
    dev = _FakeDev(measurements)
    result = capacity_test(dev, current_a=3.8, v_cutoff=2.75, max_duration_s=100)
    assert result.reason == "controller requested stop", result.reason
    assert result.duration_s <= 9, (
        "a genuine sustained cutoff must still end promptly, not linger", result.duration_s)


def check_voltage_cutoff_still_ends_immediately() -> None:
    """Hitting v_cutoff must still end the discharge on the very sample it
    happens, unaffected by the mode-departure debounce."""
    measurements = (
        [_FakeMeasurement(3.5, current=3.8, mode="discharge")] * 3 +
        [_FakeMeasurement(2.70, current=3.8, mode="discharge")]
    )
    dev = _FakeDev(measurements)
    result = capacity_test(dev, current_a=3.8, v_cutoff=2.75, max_duration_s=100)
    assert result.reason == "controller requested stop", result.reason
    assert result.duration_s <= 4, result.duration_s


def main() -> None:
    check_single_glitch_does_not_end_discharge()
    check_genuine_cutoff_still_ends_discharge()
    check_voltage_cutoff_still_ends_immediately()
    print("CAPACITY TEST GLITCH-TOLERANCE CHECKS PASSED")
    print("  single corrupted mode sample -> discharge continues")
    print("  sustained mode departure -> discharge still ends promptly")
    print("  voltage cutoff -> still ends immediately, unaffected")


if __name__ == "__main__":
    main()
