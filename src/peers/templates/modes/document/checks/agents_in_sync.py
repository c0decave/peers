#!/usr/bin/env python3
"""Fail if AGENTS.md is missing or has drifted from the CODEMAP render.

AGENTS.md is a deterministic render of the verified CODEMAP — this gate enforces
that they stay byte-identical, so the agent guide can never silently drift from
the source of truth. Regenerate with `peers agents-doc`.
"""
from __future__ import annotations

import sys
from pathlib import Path

from peers.codemap import CodeMapError, parse_codemap
from peers.codemap_gen import check_agents_sync


def main(project_dir: str = ".", codemap: str | None = None) -> int:
    # `codemap` defaults to <project_dir>/CODEMAP.yaml. AGENTS.md is always read
    # from <project_dir>/AGENTS.md (the repo-root deliverable).
    cm_path = Path(codemap) if codemap else Path(project_dir) / "CODEMAP.yaml"
    try:
        cm = parse_codemap(cm_path)
    except CodeMapError as e:
        print(f"agents-in-sync FAIL: {e}")
        return 1
    violations = check_agents_sync(Path(project_dir), cm)
    if violations:
        print(f"agents-in-sync FAIL: {violations[0]}")
        return 1
    print(f"agents-in-sync: clean (AGENTS.md matches CODEMAP, {len(cm.entries)} entries)")
    return 0


if __name__ == "__main__":
    _pd = sys.argv[1] if len(sys.argv) >= 2 else "."
    _cm = sys.argv[2] if len(sys.argv) >= 3 else None
    sys.exit(main(_pd, _cm))
