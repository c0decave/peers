#!/usr/bin/env python3
"""Fail if a documented CODEMAP signature differs from the real AST signature."""
from __future__ import annotations

import sys
from pathlib import Path

from peers.codemap import CodeMapError, check_signatures, parse_codemap


def main(project_dir: str = ".", codemap: str | None = None) -> int:
    # `codemap` defaults to <project_dir>/CODEMAP.yaml; pass an explicit path
    # to validate a map stored elsewhere (e.g. the primer's `.peers/CODEMAP.yaml`).
    cm_path = Path(codemap) if codemap else Path(project_dir) / "CODEMAP.yaml"
    try:
        cm = parse_codemap(cm_path)
    except CodeMapError as e:
        print(f"signature-match FAIL: {e}")
        return 1
    violations = check_signatures(Path(project_dir), cm)
    if violations:
        print(f"signature-match FAIL: {len(violations)} mismatch(es):")
        for s in violations[:50]:
            print(f"  {s}")
        return 1
    print("signature-match: clean")
    return 0


if __name__ == "__main__":
    _pd = sys.argv[1] if len(sys.argv) >= 2 else "."
    _cm = sys.argv[2] if len(sys.argv) >= 3 else None
    sys.exit(main(_pd, _cm))
