"""
Plot a logged EBC-A20 test CSV (voltage / signed current / charge vs time).

Standalone -- just reads whatever launchTest.py (or any of the VS Code launchers)
already wrote to disk. Run it after a test finishes for a one-shot plot, or with
--watch in a second terminal while the test is still running elsewhere, to
refresh the plot periodically as new rows get appended.

Usage:
    python plot_log.py run.csv                      # one-shot plot
    python plot_log.py run.csv --watch              # refresh every 2s while a test runs
    python plot_log.py run.csv --watch --interval 5
    python plot_log.py                              # no path given -> file-picker dialog
    (Running this file directly, e.g. VS Code's Run button, also opens the
    picker since no CLI argument gets passed either way.)
"""
import argparse
import csv
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "results")

import matplotlib.pyplot as plt
plt.style.use(os.path.join(SCRIPT_DIR, "journal.mplstyle"))


def pick_file() -> str | None:
    """Open a native file-picker dialog defaulting to results/. Returns None
    if the user cancels."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    initial_dir = RESULTS_DIR if os.path.isdir(RESULTS_DIR) else SCRIPT_DIR
    path = filedialog.askopenfilename(
        title="Select a logged EBC-A20 CSV",
        initialdir=initial_dir,
        filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
    )
    root.destroy()
    return path or None


def read_csv(path):
    t, v, i, q = [], [], [], []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                t.append(float(row["t_s"]))
                v.append(float(row["voltage_v"]))
                i.append(float(row["signed_current_a"]))
                q.append(float(row["charge_ah"]))
            except (KeyError, ValueError):
                continue     # skip a partially-flushed last row
    return t, v, i, q


def make_figure(title):
    mm = 1 / 25.4
    fig, (ax_v, ax_i, ax_q) = plt.subplots(
        3, 1, figsize=(170 * mm, 150 * mm), sharex=True, layout="constrained")
    fig.suptitle(title)
    line_v, = ax_v.plot([], [])
    line_i, = ax_i.plot([], [])
    line_q, = ax_q.plot([], [])
    ax_v.set_ylabel("Voltage (V)")
    ax_i.set_ylabel("Current (A, +charge/-discharge)")
    ax_q.set_ylabel("Charge (Ah)")
    ax_q.set_xlabel("Time (s)")
    for ax in (ax_v, ax_i, ax_q):
        ax.spines[["top", "right"]].set_visible(False)
    return fig, (ax_v, ax_i, ax_q), (line_v, line_i, line_q)


def update(fig, axes, lines, t, v, i, q):
    lines[0].set_data(t, v)
    lines[1].set_data(t, i)
    lines[2].set_data(t, q)
    for ax in axes:
        ax.relim()
        ax.autoscale_view()
    fig.canvas.draw_idle()


def main():
    ap = argparse.ArgumentParser(
        description="Plot a logged EBC-A20 test CSV, standalone or live.")
    ap.add_argument("csv", nargs="?", default=None,
                    help="path to the logged CSV (omit to pick a file "
                         "interactively)")
    ap.add_argument("--watch", action="store_true",
                     help="keep refreshing as the file grows "
                          "(run alongside a still-running test)")
    ap.add_argument("--interval", type=float, default=2.0,
                     help="refresh interval in seconds for --watch (default 2)")
    args = ap.parse_args()

    csv_path = args.csv or pick_file()
    if not csv_path:
        print("No file selected -- exiting.")
        return

    title = os.path.splitext(os.path.basename(csv_path))[0]
    fig, axes, lines = make_figure(title)

    if not args.watch:
        t, v, i, q = read_csv(csv_path)
        update(fig, axes, lines, t, v, i, q)
        plt.show()
        return

    plt.ion()
    plt.show(block=False)
    print(f"Watching {csv_path} -- refreshing every {args.interval}s.\n"
          "Close the plot window (or Ctrl-C here) to stop watching.")
    try:
        while plt.fignum_exists(fig.number):
            if os.path.exists(csv_path):
                t, v, i, q = read_csv(csv_path)
                if t:
                    update(fig, axes, lines, t, v, i, q)
            plt.pause(args.interval)
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
