#!/usr/bin/env python3
"""Require Dependency-Justification trailers for newly added deps."""
from __future__ import annotations

import subprocess
import sys


DEP_FILES = ["pyproject.toml", "requirements.txt", "package.json", "Cargo.toml", "go.mod"]


def _baseline(repo: str) -> str:
    if subprocess.run(
        ["git", "-C", repo, "rev-parse", "--verify", "peers-baseline"],
        capture_output=True, check=False,
    ).returncode == 0:
        return "peers-baseline"
    roots = subprocess.run(
        ["git", "-C", repo, "rev-list", "--max-parents=0", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.splitlines()
    return roots[-1] if roots else "HEAD"


def changed_dep_lines(repo: str) -> list[str]:
    out = subprocess.run(
        ["git", "-C", repo, "diff", f"{_baseline(repo)}..HEAD", "--unified=0", "--", *DEP_FILES],
        capture_output=True, text=True, check=False,
    ).stdout
    return [
        line[1:].strip()
        for line in out.splitlines()
        if line.startswith("+") and not line.startswith("+++") and line[1:].strip()
    ]


def justified(repo: str) -> set[str]:
    log = subprocess.run(
        ["git", "-C", repo, "log", f"{_baseline(repo)}..HEAD", "--format=%B"],
        capture_output=True, text=True, check=False,
    ).stdout
    out: set[str] = set()
    for line in log.splitlines():
        if line.startswith("Dependency-Justification:"):
            out.add(line.split(":", 1)[1].strip().split()[0].lower().rstrip(","))
    return out


def main(repo: str = ".") -> int:
    allowed = justified(repo)
    missing = []
    for line in changed_dep_lines(repo):
        pkg = line.split()[0].split("=")[0].split(">")[0].split("<")[0].strip("\"',").lower()
        if pkg and not line.startswith("#") and pkg not in allowed:
            missing.append(f"{pkg} (no Dependency-Justification: trailer)")
    if missing:
        print("deps_justified FAIL:\n  " + "\n  ".join(missing))
        return 1
    print("deps_justified: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
