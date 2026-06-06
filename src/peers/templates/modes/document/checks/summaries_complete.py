#!/usr/bin/env python3
"""Fail if any CODEMAP entry lacks a substantive summary (undocumented surface).

The structural gates prove the map points at real code; this gate proves it
actually documents it. It is the build-driving gate for `document` mode — a
freshly seeded structural CODEMAP has no summaries, so this fails until the
peers have written a real summary for every entry.
"""
from __future__ import annotations

import sys
from pathlib import Path

from peers.codemap import CodeMapError, check_summaries, parse_codemap


def main(project_dir: str = ".", codemap: str | None = None) -> int:
    # `codemap` defaults to <project_dir>/CODEMAP.yaml; pass an explicit path
    # to validate a map stored elsewhere (e.g. the primer's `.peers/CODEMAP.yaml`).
    cm_path = Path(codemap) if codemap else Path(project_dir) / "CODEMAP.yaml"
    try:
        cm = parse_codemap(cm_path)
    except CodeMapError as e:
        print(f"summaries-complete FAIL: {e}")
        return 1
    violations = check_summaries(cm)
    if violations:
        print(f"summaries-complete FAIL: {len(violations)} undocumented entr"
              f"{'y' if len(violations) == 1 else 'ies'}:")
        for s in violations[:50]:
            print(f"  {s}")
        return 1
    print(f"summaries-complete: clean ({len(cm.entries)} entries documented)")
    return 0


if __name__ == "__main__":
    _pd = sys.argv[1] if len(sys.argv) >= 2 else "."
    _cm = sys.argv[2] if len(sys.argv) >= 3 else None
    sys.exit(main(_pd, _cm))
