"""
ebc — control & experiment framework for the ZKETECH EBC-A20 battery tester.

Protocol verified against the documented reverse-engineering at
https://pop.fsck.pl/hardware/zketech-ebc-a20.html, cross-checked with
Kazhuu/ebc-battery-tester (FRAMES.md) and dev-strom/esp-ebc-mqtt.
"""
from .device import EBCA20, SafetyLimits
from .protocol import Measurement
from .experiment import (
    DataLogger, ProfileController, ProfileStep, dst_profile,
    capacity_test, CapacityResult,
)
from .celllib import Cell, CellLibrary
from .runner import TestDef, run_test, RunResult, CycleController

__all__ = [
    "EBCA20", "SafetyLimits", "Measurement",
    "DataLogger", "ProfileController", "ProfileStep", "dst_profile",
    "capacity_test", "CapacityResult",
    "Cell", "CellLibrary",
    "TestDef", "run_test", "RunResult", "CycleController",
]
