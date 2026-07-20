"""
Cell library: load cell definitions from JSON and derive safety limits.

A cell file captures the datasheet facts (nominal capacity, voltage window,
current ratings) plus hard emergency-stop bounds. Test files reference a cell
by id; currents in tests are given as C-rate and scaled by the cell's nominal
capacity at runtime, so the same test definition works across cells.
"""

from __future__ import annotations
import json
import os
from dataclasses import dataclass

from .device import SafetyLimits


@dataclass
class Cell:
    id: str
    description: str
    chemistry: str
    nominal_capacity_ah: float
    nominal_voltage_v: float
    # voltage window
    max_v: float
    min_v: float
    storage_v: float
    # current ratings
    max_charge_a: float
    max_discharge_a: float
    standard_charge_a: float
    standard_discharge_a: float
    # emergency software stops
    hard_max_v: float
    hard_min_v: float
    charge_taper_a: float

    @classmethod
    def from_dict(cls, d: dict) -> "Cell":
        v = d["voltage"]
        i = d["current"]
        s = d.get("safety", {})
        return cls(
            id=d["id"],
            description=d.get("description", ""),
            chemistry=d.get("chemistry", ""),
            nominal_capacity_ah=float(d["nominal_capacity_ah"]),
            nominal_voltage_v=float(d.get("nominal_voltage_v", 0.0)),
            max_v=float(v["max_v"]),
            min_v=float(v["min_v"]),
            storage_v=float(v.get("storage_v", v["min_v"])),
            max_charge_a=float(i["max_charge_a"]),
            max_discharge_a=float(i["max_discharge_a"]),
            standard_charge_a=float(i.get("standard_charge_a",
                                          i["max_charge_a"] / 2)),
            standard_discharge_a=float(i.get("standard_discharge_a",
                                             i["max_discharge_a"] / 2)),
            hard_max_v=float(s.get("hard_max_v", v["max_v"] + 0.05)),
            hard_min_v=float(s.get("hard_min_v", v["min_v"] - 0.05)),
            charge_taper_a=float(s.get("charge_taper_a",
                                       0.02 * float(d["nominal_capacity_ah"]))),
        )

    def c_to_amps(self, c_rate: float) -> float:
        """Convert a (signed) C-rate to amps using nominal capacity."""
        return c_rate * self.nominal_capacity_ah

    def safety_limits(self, i_cap_a: float | None = None) -> SafetyLimits:
        """
        Build SafetyLimits for this cell.

        The current ceiling is the larger of charge/discharge ratings unless an
        explicit i_cap_a is given. The EBC-A20 charge ceiling (5 A) is enforced
        separately by the device guard.
        """
        i_max = i_cap_a if i_cap_a is not None else max(
            self.max_charge_a, self.max_discharge_a)
        return SafetyLimits(
            v_max=self.hard_max_v,
            v_min=self.hard_min_v,
            i_max=i_max,
            i_max_charge=self.max_charge_a,
            i_max_discharge=self.max_discharge_a,
            discharge_cutoff_v=self.min_v,
            charge_cv=self.max_v,
            charge_taper_a=self.charge_taper_a,
        )


class CellLibrary:
    """Loads and indexes cell JSON files from a directory."""

    def __init__(self, directory: str):
        self.directory = directory
        self._cells: dict[str, Cell] = {}
        self._load_all()

    def _load_all(self) -> None:
        if not os.path.isdir(self.directory):
            return
        for fn in os.listdir(self.directory):
            if fn.endswith(".json"):
                with open(os.path.join(self.directory, fn)) as fh:
                    cell = Cell.from_dict(json.load(fh))
                    self._cells[cell.id] = cell

    def get(self, cell_id: str) -> Cell:
        if cell_id not in self._cells:
            raise KeyError(
                f"cell '{cell_id}' not found in {self.directory}. "
                f"Available: {', '.join(sorted(self._cells)) or '(none)'}")
        return self._cells[cell_id]

    def ids(self) -> list[str]:
        return sorted(self._cells)
