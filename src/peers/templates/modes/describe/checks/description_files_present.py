#!/usr/bin/env python3
"""Hard goal: SPEC.md, ARCHITECTURE.md, DESIGN.md exist + are non-empty.

Each file must:
- exist as a regular file (not symlink) in repo root
- contain >= MIN_BYTES bytes of content

Fail-CLOSED with a single-line diagnostic per missing/short file.
"""
from __future__ import annotations

import sys
from pathlib import Path

REQUIRED_FILES = ["SPEC.md", "ARCHITECTURE.md", "DESIGN.md"]
MIN_BYTES = 500


def main(repo: str = ".") -> int:
    root = Path(repo)
    problems: list[str] = []
    for name in REQUIRED_FILES:
        p = root / name
        if not p.exists():
            problems.append(f"  {name}: missing")
            continue
        if p.is_symlink():
            problems.append(f"  {name}: refusing symlink")
            continue
        if not p.is_file():
            problems.append(f"  {name}: not a regular file")
            continue
        try:
            size = p.stat().st_size
        except OSError as e:
            problems.append(f"  {name}: unreadable: {e}")
            continue
        if size < MIN_BYTES:
            problems.append(
                f"  {name}: too short ({size} < {MIN_BYTES} bytes)"
            )
    if problems:
        print("description_files_present FAIL:")
        for problem in problems:
            print(problem)
        return 1
    print("description_files_present: clean (all 3 files ≥ 500 bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
