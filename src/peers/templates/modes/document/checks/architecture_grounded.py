#!/usr/bin/env python3
"""Fail if ARCHITECTURE.md has a dangling [[id]] anchor, leaves a public
subsystem uncovered, or still holds a seed placeholder. The HARD, deterministic
moat for the human-docs prose; accuracy is the soft architecture-cross-review.
"""
from __future__ import annotations

import sys
from pathlib import Path

from peers.codemap import CodeMapError, check_architecture, parse_codemap


def main(project_dir: str = ".", codemap: str | None = None) -> int:
    # `codemap` defaults to <project_dir>/CODEMAP.yaml; pass an explicit path to
    # validate a map stored elsewhere (e.g. the primer's .peers/CODEMAP.yaml).
    cm_path = Path(codemap) if codemap else Path(project_dir) / "CODEMAP.yaml"
    try:
        cm = parse_codemap(cm_path)
    except CodeMapError as e:
        print(f"architecture-grounded FAIL: {e}")
        return 1
    violations = check_architecture(project_dir, cm)
    if violations:
        print(f"architecture-grounded FAIL: {len(violations)} issue"
              f"{'' if len(violations) == 1 else 's'}:")
        for s in violations[:50]:
            print(f"  {s}")
        return 1
    print("architecture-grounded: clean (anchors resolve, all subsystems "
          "covered)")
    return 0


if __name__ == "__main__":
    _pd = sys.argv[1] if len(sys.argv) >= 2 else "."
    _cm = sys.argv[2] if len(sys.argv) >= 3 else None
    sys.exit(main(_pd, _cm))
