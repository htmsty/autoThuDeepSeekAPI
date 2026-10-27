"""Run one or more packaged executables with ``--selftest`` and report exit codes.

Usage:
    python tools/run_selftest.py dist/autoThuDeepSeekAPI/autoThuDeepSeekAPI.exe
    python tools/run_selftest.py path/to/folder.exe path/to/single-file.exe
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    failures = 0
    for value in sys.argv[1:]:
        executable = Path(value).resolve()
        if not executable.is_file():
            print(f"[MISSING] {executable}")
            failures += 1
            continue
        started = time.monotonic()
        print(f"[RUN] {executable}", flush=True)
        try:
            result = subprocess.run(
                [str(executable), "--selftest"],
                cwd=executable.parent,
                timeout=180,
                check=False,
            )
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - started
            print(f"[FAIL] timed out after {elapsed:.1f}s", flush=True)
            failures += 1
            continue
        elapsed = time.monotonic() - started
        label = "OK" if result.returncode == 0 else "FAIL"
        print(f"[{label}] exit={result.returncode} elapsed={elapsed:.1f}s", flush=True)
        if result.returncode != 0:
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
