"""Stateful real-time SOC observer independent of the CSV transport."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .filters import ExtendedKalmanFilter, FilterUpdate, UnscentedKalmanFilter
from .model import TheveninEcmModel


@dataclass
class ObserverResult:
    time_s: float
    dt_s: float
    current_a: float
    voltage_v: float
    soc: float
    soc_std: float
    rc_voltages_v: tuple[float, ...]
    predicted_voltage_v: float
    innovation_v: float
    nis: float
    voltage_update_accepted: bool
    status: str


class SocObserver:
    """Runs one predict/update for each asynchronous tester sample."""

    def __init__(self, model: TheveninEcmModel, filter_type: str = "ekf",
                 initial_soc: float = .8, initial_soc_std: float = .1,
                 initial_rc_std_v: float = .03,
                 q_soc_per_s: float = 1e-10,
                 q_rc_per_s: tuple[float, ...] = (1e-7, 1e-8),
                 voltage_std_v: float = .010, nis_gate: float = 9.0,
                 max_dt_s: float = 30.0,
                 initialize_from_rest_voltage: bool = False,
                 rest_current_threshold_a: float = .05):
        if len(q_rc_per_s) != model.num_branches:
            raise ValueError(
                f"q_rc_per_s has {len(q_rc_per_s)} entries but the model has "
                f"{model.num_branches} RC branches")
        self.model = model
        state = np.array([initial_soc] + [0.0]*model.num_branches)
        covariance = np.diag(
            [initial_soc_std**2] + [initial_rc_std_v**2]*model.num_branches)
        common = dict(
            model=model, state=state, covariance=covariance,
            process_noise=np.array([q_soc_per_s, *q_rc_per_s]),
            voltage_variance_v2=voltage_std_v**2, nis_gate=nis_gate)
        key = filter_type.strip().lower()
        if key == "ekf": self.filter = ExtendedKalmanFilter(**common)
        elif key == "ukf": self.filter = UnscentedKalmanFilter(**common)
        else: raise ValueError("filter_type must be 'ekf' or 'ukf'")
        self.filter_type = key
        self.max_dt_s = float(max_dt_s)
        self.initialize_from_rest_voltage = initialize_from_rest_voltage
        self.rest_current_threshold_a = float(rest_current_threshold_a)
        self.previous_time_s = None
        self.previous_current_a = None
        self.initialized_from_voltage = False

    def process(self, time_s: float, voltage_v: float,
                current_a: float) -> ObserverResult:
        if not all(math.isfinite(v) for v in (time_s, voltage_v, current_a)):
            raise ValueError("sample values must be finite")
        status = "ok"
        if self.previous_time_s is None:
            dt = 0.0
            if (self.initialize_from_rest_voltage
                    and abs(current_a) <= self.rest_current_threshold_a):
                self.filter.x[0] = self.model.soc_from_ocv(voltage_v)
                self.initialized_from_voltage = True
                status = "initialized_from_rest_voltage"
        else:
            dt_raw = time_s-self.previous_time_s
            if dt_raw <= 0:
                # Do not count an identical measurement twice. This can occur
                # at a resume boundary or when a status row is duplicated.
                predicted = self.model.voltage(self.filter.x, current_a)
                innovation = voltage_v-predicted
                x = self.filter.x
                self.previous_current_a = current_a
                return ObserverResult(
                    time_s, 0.0, current_a, voltage_v, float(x[0]),
                    self.filter.soc_std, tuple(x[1:]),
                    predicted, innovation, math.nan, False,
                    "duplicate_or_reversed_time_skipped")
            else:
                # Exact RC discretization remains stable for large dt. Use the
                # whole interval so coulomb counting does not lose charge.
                dt = dt_raw
                if dt > self.max_dt_s: status = "large_dt_gap"
                propagation_current = .5*(self.previous_current_a+current_a)
                self.filter.predict(dt, propagation_current)
        update = self.filter.update(voltage_v, current_a)
        if not update.accepted:
            status = (status+";" if status != "ok" else "")+"voltage_outlier_rejected"
        self.previous_time_s = time_s
        self.previous_current_a = current_a
        x = self.filter.x
        return ObserverResult(
            time_s, dt, current_a, voltage_v, float(x[0]),
            self.filter.soc_std, tuple(x[1:]),
            update.predicted_voltage_v, update.innovation_v,
            update.normalized_innovation_squared, update.accepted, status)

    def export_state(self) -> dict:
        return {
            "filter_type": self.filter_type,
            "num_rc_branches": self.model.num_branches,
            "previous_time_s": self.previous_time_s,
            "previous_current_a": self.previous_current_a,
            **self.filter.export_state(),
        }

    def restore_state(self, saved: dict) -> None:
        if saved.get("filter_type") != self.filter_type:
            raise ValueError("checkpoint filter type does not match")
        saved_branches = saved.get("num_rc_branches")
        if saved_branches is None:
            print("WARNING: checkpoint predates num_rc_branches; assuming "
                  "2 legacy RC branches.")
            saved_branches = 2
        if int(saved_branches) != self.model.num_branches:
            raise ValueError(
                f"checkpoint has {saved_branches} RC branches but the "
                f"current model has {self.model.num_branches}")
        n = self.filter.n
        self.filter.x = np.asarray(saved["state"], dtype=float).reshape(n)
        self.filter.P = np.asarray(saved["covariance"], dtype=float).reshape(n, n)
        self.filter._stabilize()
        self.previous_time_s = saved.get("previous_time_s")
        self.previous_current_a = saved.get("previous_current_a")
