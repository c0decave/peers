#!/usr/bin/env python3
"""Exit 1 if any Bug-Resolves commit exceeds the reviewable diff limit."""
from __future__ import annotations

import subprocess
import sys


LIMIT = 200


def main(repo: str = ".") -> int:
    commits = subprocess.run(
        ["git", "-C", repo, "log", "peers-baseline..HEAD", "--grep=^Bug-Resolves:", "--format=%H"],
        capture_output=True, text=True, check=False,
    ).stdout.splitlines()
    over: list[str] = []
    for sha in commits:
        lines = subprocess.run(
            ["git", "-C", repo, "show", "--stat", "--format=", sha],
            capture_output=True, text=True, check=False,
        ).stdout.strip().splitlines()
        if not lines:
            continue
        ins = deleted = 0
        for token in lines[-1].split(","):
            token = token.strip()
            if "insertion" in token:
                ins = int(token.split()[0])
            elif "deletion" in token:
                deleted = int(token.split()[0])
        total = ins + deleted
        if total > LIMIT:
            over.append(f"{sha[:8]}: {total} lines (limit {LIMIT})")
    if over:
        print("diff_size_per_resolve FAIL:\n  " + "\n  ".join(over))
        return 1
    print(f"diff_size_per_resolve: clean ({len(commits)} resolves, all <= {LIMIT})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
