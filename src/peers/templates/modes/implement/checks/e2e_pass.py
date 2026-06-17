#!/usr/bin/env python3
"""Exit 1 if the frozen `.peers/contracts/e2e.sh` does not exit 0.

Third hard gate for implement-mode. Conditional: only projects whose plan
declares a UI surface (`surfaces: [web|gui|...]`) carry an `e2e.sh`
contract. CLI/library-only projects skip this gate entirely.

SHA-verification (via :func:`peers_ctl.contracts.verify_contracts`) runs
before the script itself, so a peer that tampers with `e2e.sh` to
short-circuit the check fails the gate before any code runs.

Skip (exit 0) when `.peers/contracts/e2e.sh` is absent -- the project
has no UI surface and therefore no e2e contract to honour.

Pass (exit 0) when `.peers/contracts/e2e.sh` runs cleanly.
Fail (exit 1) when:

* contracts SHA verification fails (file tampered)
* e2e.sh exits non-zero (test failure) -- last 20 lines of combined
  stdout+stderr are echoed with a truncation marker when longer
* e2e.sh times out (default 900s = 15 min, longer than acceptance
  because browser/playwright suites are slow)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from peers_ctl.contracts import ContractsMismatch, verify_contracts


def main(project_dir: str = ".", timeout: int = 900) -> int:
    project_root = Path(project_dir).resolve()
    plan_dir = project_root / ".peers"
    e2e = plan_dir / "contracts" / "e2e.sh"

    if not e2e.is_file():
        print("e2e-pass: skipped (no .peers/contracts/e2e.sh — non-UI project)")
        return 0

    try:
        verify_contracts(plan_dir)
    except (ContractsMismatch, OSError) as e:
        print(f"e2e-pass FAIL: contract tampered: {e}")
        return 1

    try:
        proc = subprocess.run(
            ["/bin/sh", str(e2e)],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"e2e-pass FAIL: timed out after {timeout}s")
        return 1

    if proc.returncode != 0:
        print(f"e2e-pass FAIL: exit {proc.returncode}")
        combined = (proc.stdout or "") + (proc.stderr or "")
        lines = combined.splitlines()
        if len(lines) > 20:
            print(f"... (truncated, showing last 20 of {len(lines)} lines)")
            lines = lines[-20:]
        for line in lines:
            print(line)
        return 1

    print("e2e-pass: clean (exit 0)")
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
