#!/usr/bin/env python3
"""Fail if any CODEMAP entry does not resolve to a real symbol in its file."""
from __future__ import annotations

import sys
from pathlib import Path

from peers.codemap import CodeMapError, check_grounded, parse_codemap


def main(project_dir: str = ".", codemap: str | None = None) -> int:
    # `codemap` defaults to <project_dir>/CODEMAP.yaml (the committed document-
    # mode deliverable). Pass an explicit path to validate a CODEMAP that lives
    # elsewhere — e.g. the free primer's `.peers/CODEMAP.yaml`. Symbols always
    # resolve against `project_dir`, decoupled from where the map is stored.
    cm_path = Path(codemap) if codemap else Path(project_dir) / "CODEMAP.yaml"
    try:
        cm = parse_codemap(cm_path)
    except CodeMapError as e:
        print(f"grounded FAIL: {e}")
        return 1
    violations = check_grounded(project_dir, cm)
    if violations:
        print(f"grounded FAIL: {len(violations)} ungrounded entr"
              f"{'y' if len(violations) == 1 else 'ies'}:")
        for s in violations[:50]:
            print(f"  {s}")
        return 1
    print(f"grounded: clean ({len(cm.entries)} entries all resolve)")
    return 0


if __name__ == "__main__":
    _pd = sys.argv[1] if len(sys.argv) >= 2 else "."
    _cm = sys.argv[2] if len(sys.argv) >= 3 else None
    sys.exit(main(_pd, _cm))
