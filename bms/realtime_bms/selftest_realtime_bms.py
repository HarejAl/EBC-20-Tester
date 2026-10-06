#!/usr/bin/env python3
"""Offline regression for the model, EKF, UKF, and CSV monitor."""

from __future__ import annotations

import csv
import os
import tempfile

import numpy as np

if __package__ in (None, ""):
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from realtime_bms.model import TheveninEcmModel
from realtime_bms.monitor import run_monitor
from realtime_bms.observer import SocObserver


def test_model(num_branches: int = 2) -> TheveninEcmModel:
    soc = np.linspace(0, 1, 11)
    if num_branches == 1:
        taus, rs = [24.0], [.012]
    else:
        taus = np.geomspace(24.0, 300.0, num_branches)
        rs = np.linspace(.012, .018, num_branches)
    rc_branches = [(np.full_like(soc, r), np.full_like(soc, tau))
                  for r, tau in zip(rs, taus)]
    return TheveninEcmModel(
        soc=soc, ocv_v=3.2+.95*soc+.03*soc*soc,
        r0_ohm=.020+.005*(soc-.5)**2,
        rc_branches=rc_branches, capacity_ah=2.0)


def default_q_rc(num_branches: int) -> tuple[float, ...]:
    if num_branches == 1: return (2e-7,)
    return tuple(np.geomspace(2e-7, 2e-8, num_branches))


def write_synthetic(path: str, model: TheveninEcmModel):
    rng = np.random.default_rng(42)
    state = np.array([.88] + [0.0]*model.num_branches)
    rows, time_s = [], 0.0
    for k in range(1400):
        dt = (0.8, 1.0, 1.2)[k % 3]
        time_s += dt
        phase = k % 240
        current = (1.2 if phase < 80 else -0.6 if phase < 120 else
                   0.0 if phase < 180 else .5)
        state = model.transition(state, dt, current)
        voltage = model.voltage(state, current)+rng.normal(0, .0015)
        if k == 600: voltage += .25  # must be rejected by NIS gate
        logged_t = time_s if k < 800 else time_s-800.0  # resume clock reset
        row = [f"{logged_t:.3f}", f"{voltage:.6f}", f"{abs(current):.5f}",
               f"{-current:.5f}", "0.0",
               "discharge" if current > 0 else "charge" if current < 0 else "idle"]
        rows.append(row)
        if k == 300: rows.append(list(row))  # duplicate timestamp
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["t_s", "voltage_v", "current_a",
                         "signed_current_a", "charge_ah", "mode"])
        writer.writerows(rows)
    return state[0]


def run_one(kind: str, model: TheveninEcmModel, source: str,
            folder: str, true_final_soc: float):
    observer = SocObserver(
        model, filter_type=kind, initial_soc=.68, initial_soc_std=.20,
        voltage_std_v=.006, q_soc_per_s=1e-9,
        q_rc_per_s=default_q_rc(model.num_branches), nis_gate=16)
    output = os.path.join(folder, f"{kind}.csv")
    count = run_monitor(source, output, observer, follow=False,
                        checkpoint_path=os.path.join(folder, f"{kind}.json"),
                        print_every=0)
    assert count == 1401  # duplicate is intentionally retained and flagged
    assert abs(observer.filter.x[0]-true_final_soc) < .025, (
        kind, observer.filter.x[0], true_final_soc)
    with open(output, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert any(r["voltage_update_accepted"] == "false" for r in rows)
    assert any(r["status"] == "duplicate_or_reversed_time_skipped" for r in rows)
    assert all(float(r["current_a_ecm"]) > 0 for r in rows
               if float(r["t_s"]) < 75)  # EBC sign was converted
    for i in range(1, model.num_branches+1):
        assert f"v{i}_v" in rows[0]
    return observer.filter.x[0]


def run_branch_count(num_branches: int, folder: str) -> dict[str, float]:
    model = test_model(num_branches)
    source = os.path.join(folder, f"synthetic_ebc_{num_branches}.csv")
    true_final_soc = write_synthetic(source, model)
    return {kind: run_one(kind, model, source, folder, true_final_soc)
            for kind in ("ekf", "ukf")}


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="realtime_bms_test_") as folder:
        estimates = {n: run_branch_count(n, folder) for n in (1, 2, 3)}
    print("REAL-TIME BMS SYNTHETIC CHECKS PASSED")
    print(f"  estimates by branch count: {estimates}")


if __name__ == "__main__":
    main()
