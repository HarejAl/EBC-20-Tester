"""Read-only follower/replay for the EBC logger CSV."""

from __future__ import annotations

import csv
import json
import math
import os
import statistics
import time
from dataclasses import asdict
from pathlib import Path

from .model import TheveninEcmModel
from .observer import ObserverResult, SocObserver


def output_fields(num_branches: int) -> list[str]:
    return [
        "t_s", "voltage_v", "current_a_ecm", "soc", "soc_percent",
        "soc_std", "soc_2sigma_low", "soc_2sigma_high",
        *[f"v{i}_v" for i in range(1, num_branches+1)],
        "predicted_voltage_v", "innovation_v", "nis",
        "voltage_update_accepted", "dt_s", "status",
    ]


class CsvFollower:
    """Incrementally read complete CSV rows while another process appends."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.file = None
        self.headers = None
        self.offset = 0
        self.pending = ""

    def _open_if_ready(self) -> bool:
        if self.file is not None: return True
        if not os.path.isfile(self.path): return False
        self.file = open(self.path, "r", newline="", encoding="utf-8-sig")
        header = self.file.readline()
        if not header.endswith(("\n", "\r")):
            self.file.close(); self.file = None; return False
        self.headers = next(csv.reader([header]))
        self.offset = self.file.tell()
        return True

    def read_new(self) -> list[dict[str, str]]:
        if not self._open_if_ready(): return []
        if os.path.getsize(self.path) < self.offset:
            # File was replaced/truncated. Re-open and consume its new header.
            self.file.close(); self.file = None; self.offset = 0; self.pending = ""
            return self.read_new()
        self.file.seek(self.offset)
        chunk = self.file.read()
        self.offset = self.file.tell()
        text = self.pending+chunk
        lines = text.splitlines(keepends=True)
        self.pending = ""
        if lines and not lines[-1].endswith(("\n", "\r")):
            self.pending = lines.pop()
        rows = []
        for line in lines:
            values = next(csv.reader([line]))
            if len(values) == len(self.headers):
                rows.append(dict(zip(self.headers, values)))
        return rows

    def close(self) -> None:
        if self.file is not None: self.file.close()


class TimeStitcher:
    """Keep elapsed time monotonic across resumed CSV time resets."""

    def __init__(self):
        self.offset = 0.0
        self.previous_raw = None
        self.previous_stitched = None
        self.positive_dts = []

    def __call__(self, raw_time: float) -> float:
        raw = float(raw_time)
        if self.previous_raw is not None:
            delta = raw-self.previous_raw
            if delta > 1e-9:
                self.positive_dts.append(delta)
                self.positive_dts = self.positive_dts[-100:]
            elif delta < -1e-9:
                nominal = statistics.median(self.positive_dts) if self.positive_dts else 1.0
                self.offset = self.previous_stitched+nominal-raw
        stitched = raw+self.offset
        self.previous_raw, self.previous_stitched = raw, stitched
        return stitched


def _parse_sample(row: dict[str, str], stitch: TimeStitcher):
    raw_t = float(row["t_s"])
    voltage = float(row["voltage_v"])
    if "signed_current_a" in row and row["signed_current_a"].strip():
        # EBC: charge positive. ECM/BMS: discharge positive.
        current = -float(row["signed_current_a"])
    elif "current_a" in row:
        magnitude = abs(float(row["current_a"]))
        mode = row.get("mode", "").strip().lower()
        current = magnitude if mode == "discharge" else -magnitude if mode == "charge" else 0.0
    else:
        raise ValueError("source CSV needs signed_current_a or current_a")
    if not all(math.isfinite(v) for v in (raw_t, voltage, current)):
        raise ValueError("non-finite source sample")
    return stitch(raw_t), voltage, current


def _output_row(result: ObserverResult) -> dict[str, object]:
    low = max(result.soc-2*result.soc_std, 0.0)
    high = min(result.soc+2*result.soc_std, 1.0)
    row = {
        "t_s": f"{result.time_s:.3f}", "voltage_v": f"{result.voltage_v:.6f}",
        "current_a_ecm": f"{result.current_a:.6f}", "soc": f"{result.soc:.8f}",
        "soc_percent": f"{100*result.soc:.5f}", "soc_std": f"{result.soc_std:.8f}",
        "soc_2sigma_low": f"{low:.8f}", "soc_2sigma_high": f"{high:.8f}",
        "predicted_voltage_v": f"{result.predicted_voltage_v:.6f}",
        "innovation_v": f"{result.innovation_v:.6f}", "nis": f"{result.nis:.6f}",
        "voltage_update_accepted": str(result.voltage_update_accepted).lower(),
        "dt_s": f"{result.dt_s:.6f}", "status": result.status,
    }
    for i, v in enumerate(result.rc_voltages_v, start=1):
        row[f"v{i}_v"] = f"{v:.8f}"
    return row


def save_checkpoint(path: str, observer: SocObserver, source_csv: str,
                    processed_rows: int) -> None:
    payload = {"version": 2, "source_csv": os.path.abspath(source_csv),
               "processed_rows": processed_rows, "saved_epoch": time.time(),
               "observer": observer.export_state()}
    target = Path(path)
    temporary = target.with_suffix(target.suffix+".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, target)


def run_monitor(input_csv: str, output_csv: str, observer: SocObserver,
                follow: bool = True, poll_interval_s: float = .5,
                idle_timeout_s: float | None = None,
                checkpoint_path: str | None = None,
                checkpoint_every: int = 30,
                print_every: int = 5,
                stop_event=None) -> int:
    """Process existing rows, then optionally follow appended rows.

    The function opens only the CSV. It has no device/serial access.
    """
    input_csv, output_csv = os.path.abspath(input_csv), os.path.abspath(output_csv)
    if input_csv == output_csv:
        raise ValueError("BMS output must differ from the source measurement CSV")
    Path(output_csv).parent.mkdir(parents=True, exist_ok=True)
    follower, stitch = CsvFollower(input_csv), TimeStitcher()
    processed = invalid = 0
    last_activity = time.monotonic()
    fields = output_fields(observer.model.num_branches)
    # A monitor run is reproducible from the immutable source; start a fresh
    # estimate rather than silently appending a second pass to an old output.
    with open(output_csv, "w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=fields)
        writer.writeheader(); out.flush()
        try:
            while True:
                rows = follower.read_new()
                for row in rows:
                    try:
                        t, voltage, current = _parse_sample(row, stitch)
                        result = observer.process(t, voltage, current)
                    except (KeyError, TypeError, ValueError):
                        invalid += 1; continue
                    writer.writerow(_output_row(result)); out.flush()
                    processed += 1; last_activity = time.monotonic()
                    if print_every and processed % print_every == 0:
                        marker = ("" if result.voltage_update_accepted else
                                  "  [update skipped]" if "duplicate" in result.status
                                  else "  [V outlier]")
                        print(f"t={result.time_s:9.1f}s  V={voltage:7.4f} V  "
                              f"I={current:+7.3f} A  SOC={100*result.soc:6.2f}% "
                              f"±{200*result.soc_std:5.2f}%{marker}")
                    if checkpoint_path and processed % checkpoint_every == 0:
                        save_checkpoint(checkpoint_path, observer, input_csv, processed)
                if rows: continue
                if not follow: break
                if stop_event is not None and stop_event.is_set(): break
                if idle_timeout_s is not None and time.monotonic()-last_activity >= idle_timeout_s:
                    break
                time.sleep(max(poll_interval_s, .05))
        except KeyboardInterrupt:
            print("\nBMS monitor stopped by user.")
        finally:
            follower.close()
    if checkpoint_path: save_checkpoint(checkpoint_path, observer, input_csv, processed)
    print(f"BMS rows written: {processed}; invalid source rows skipped: {invalid}")
    return processed


def make_observer(model: TheveninEcmModel, settings: dict) -> SocObserver:
    allowed = {
        "filter_type", "initial_soc", "initial_soc_std", "initial_rc_std_v",
        "q_soc_per_s", "q_rc_per_s", "voltage_std_v",
        "nis_gate", "max_dt_s", "initialize_from_rest_voltage",
        "rest_current_threshold_a"}
    return SocObserver(model, **{k: v for k, v in settings.items() if k in allowed})
