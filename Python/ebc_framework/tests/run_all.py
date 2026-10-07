#!/usr/bin/env python3
"""Run every offline regression test in this folder (no tester needed)."""

import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    failed = []
    for path in sorted(glob.glob(os.path.join(HERE, "selftest*.py"))):
        name = os.path.basename(path)
        print(f"=== {name}")
        result = subprocess.run([sys.executable, path], cwd=HERE,
                                capture_output=True, text=True)
        output = result.stdout if result.returncode == 0 else (
            result.stdout + result.stderr)
        tail = output.strip().splitlines()[-3:]
        print("\n".join("    " + line for line in tail))
        if result.returncode != 0:
            failed.append(name)
    print()
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    print("ALL OFFLINE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
