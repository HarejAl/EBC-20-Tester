#!/usr/bin/env python3
"""Offline replay/tuning regression using the synthetic two-RC EBC log."""

from __future__ import annotations

import csv
import json
import os
import tempfile

if __package__ in (None, ""):
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from realtime_bms.observer import SocObserver
from realtime_bms.offline import (load_measurements, reference_array,
                                  replay_samples, write_diagnostics)
from realtime_bms.selftest_realtime_bms import test_model, write_synthetic
from realtime_bms.tuning import (TuningSpace, TuningWeights, tune_filters,
                                 write_tuning_outputs)


def main() -> None:
    model = test_model()
    with tempfile.TemporaryDirectory(prefix="bms_offline_test_") as folder:
        source = os.path.join(folder, "synthetic.csv")
        write_synthetic(source, model)
        samples = load_measurements(source)
        reference, kind = reference_array(
            samples, "coulomb_count", .88, model.capacity_ah)
        fixed = dict(initial_soc=.75, initial_soc_std=.2,
                     initial_rc_std_v=.03, nis_gate=16, max_dt_s=30)
        observer = SocObserver(
            model, filter_type="ekf", q_soc_per_s=1e-10,
            q_rc_per_s=(1e-7, 1e-8),
            voltage_std_v=.006, **fixed)
        replay = replay_samples(samples, reference, observer, .2)
        assert replay.metrics["soc_rmse"] < .02
        replay_folder = os.path.join(folder, "replay")
        write_diagnostics(replay_folder, replay, {"reference_kind": kind},
                          model.num_branches)
        assert {"offline_replay.csv", "offline_metrics.json",
                "soc_replay.svg", "voltage_replay.svg"} <= set(os.listdir(replay_folder))

        space = TuningSpace(
            filter_types=("ekf", "ukf"), q_soc_per_s=(1e-11, 1e-9),
            q_rc_fast_per_s=(1e-7,), q_rc_decay_ratio=(.1,),
            voltage_std_v=(.006, .015))
        weights = TuningWeights()
        ranked = tune_filters(model, samples, reference, space, fixed,
                              score_start_fraction=.2, weights=weights,
                              progress_every=0)
        assert len(ranked) == 8 and ranked[0]["status"] == "ok"
        tuning_folder = os.path.join(folder, "tuning")
        export = write_tuning_outputs(
            tuning_folder, ranked, model, samples, reference, fixed, kind,
            .2, weights, space)
        assert os.path.isfile(os.path.join(tuning_folder, "tuning_results.csv"))
        with open(os.path.join(tuning_folder, "best_kf_config.json"),
                  encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["observer_settings"] == export["observer_settings"]
        with open(os.path.join(tuning_folder, "tuning_results.csv"),
                  newline="", encoding="utf-8") as fh:
            assert len(list(csv.DictReader(fh))) == 8
        print("OFFLINE BMS REPLAY/TUNING CHECKS PASSED")
        print(f"  replay SOC RMSE: {replay.metrics['soc_rmse']:.6g}")
        print(f"  best candidate: {export['observer_settings']}")


if __name__ == "__main__":
    main()
