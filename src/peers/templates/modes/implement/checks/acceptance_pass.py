#!/usr/bin/env python3
"""Exit 1 if the frozen `.peers/contracts/acceptance.sh` does not exit 0.

Second hard gate for implement-mode. The frozen contract is the user's
definition of "done"; if it does not pass we have not met acceptance.

SHA-verification (via :func:`peers_ctl.contracts.verify_contracts`) runs
before the script itself, so a peer that tampers with `acceptance.sh`
to short-circuit the check fails the gate before any code runs.

Pass (exit 0) when `.peers/contracts/acceptance.sh` runs cleanly.
Fail (exit 1) when:

* `.peers/contracts/acceptance.sh` missing
* contracts SHA verification fails (file tampered)
* acceptance.sh exits non-zero (test failure) -- last 20 lines of
  combined stdout+stderr are echoed with a truncation marker when longer
* acceptance.sh times out (default 600s = 10 min, overridable)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from peers_ctl.contracts import ContractsMismatch, verify_contracts


def main(project_dir: str = ".", timeout: int = 600) -> int:
    project_root = Path(project_dir).resolve()
    plan_dir = project_root / ".peers"
    acceptance = plan_dir / "contracts" / "acceptance.sh"

    if not acceptance.is_file():
        print("acceptance-pass FAIL: .peers/contracts/acceptance.sh not found")
        return 1

    try:
        verify_contracts(plan_dir)
    except ContractsMismatch as e:
        print(f"acceptance-pass FAIL: contract tampered: {e}")
        return 1

    try:
        proc = subprocess.run(
            ["/bin/sh", str(acceptance)],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"acceptance-pass FAIL: timed out after {timeout}s")
        return 1

    if proc.returncode != 0:
        print(f"acceptance-pass FAIL: exit {proc.returncode}")
        combined = (proc.stdout or "") + (proc.stderr or "")
        lines = combined.splitlines()
        if len(lines) > 20:
            print(f"... (truncated, showing last 20 of {len(lines)} lines)")
            lines = lines[-20:]
        for line in lines:
            print(line)
        return 1

    print("acceptance-pass: clean (exit 0)")
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
