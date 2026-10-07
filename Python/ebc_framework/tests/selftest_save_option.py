"""Offline regression for launchTest.py's --no-save option.

Runs the real connection_test definition end to end through launchTest.main()
with the serial port swapped for the fake tester, once saving and once with
--no-save, and checks which files were (not) written.
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile

import _paths  # noqa: F401  (adds framework root to sys.path)
import launchTest
from fake_tester import fake_device


def run(argv: list[str]):
    original = launchTest.EBCA20.on_serial
    launchTest.EBCA20.on_serial = classmethod(
        lambda cls, port, limits=None: fake_device(
            limits=limits, capacity_ah=1.9, soc0=0.6, emit_period_s=0.0,
            time_accel=40.0))
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            result = launchTest.main(argv)
    finally:
        launchTest.EBCA20.on_serial = original
    return result, out.getvalue()


def results_snapshot() -> set[str]:
    found = set()
    for root, _dirs, files in os.walk(launchTest.RESULTS_DIR):
        found.update(os.path.join(root, name) for name in files)
    return found


def check_saved_run() -> None:
    with tempfile.TemporaryDirectory() as folder:
        csv_path = os.path.join(folder, "saved.csv")
        result, _ = run(["connection_test", "--cell", "LP604765",
                         "--port", "FAKE", "--out", csv_path])
        assert result is not None and result.samples > 0, result
        for suffix in (".csv", "_meta.json", "_checkpoint.json"):
            path = csv_path[:-4] + suffix
            assert os.path.isfile(path), f"missing {path}"
        with open(csv_path) as fh:
            rows = sum(1 for _ in fh) - 1
        assert rows == result.samples, (rows, result.samples)


def check_unsaved_run() -> None:
    before = results_snapshot()
    result, printed = run(["connection_test", "--cell", "LP604765",
                           "--port", "FAKE", "--no-save"])
    assert result is not None and result.samples > 0, result
    assert "NOT SAVED" in printed
    assert "t_exp" in printed, "samples must still be printed"
    assert results_snapshot() == before, "--no-save wrote files to results/"


def main() -> None:
    check_saved_run()
    check_unsaved_run()
    print("SAVE OPTION CHECKS PASSED")
    print("  default -> CSV + metadata + checkpoint written")
    print("  --no-save -> samples printed, nothing written")


if __name__ == "__main__":
    main()
