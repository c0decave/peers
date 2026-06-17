#!/usr/bin/env python3
"""Hard goal: last N commits touching description files are non-substantive.

Substantive = any of:
- adds >= LARGE_DIFF_LINES (default 100) lines, OR
- introduces a new `##`-level section heading not present in the
  parent revision, OR
- has deletion ratio >= LARGE_DELETION_RATIO (default 0.5).

Convergence threshold N is read from .peers/config.yaml ->
goals.describe_convergence_n (default 2).

The check is intentionally STRICT: small wording fixes and reformat
ticks let the run terminate; anything that changes structure or bulk
content keeps it going so peers refine until quiet.

Fail-CLOSED on git/IO errors.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from peers.safe_io import read_bytes_under_root_no_follow

try:
    import yaml
except ImportError:  # pragma: no cover - yaml is required everywhere else
    yaml = None  # type: ignore

DESCRIPTION_FILES = ["SPEC.md", "ARCHITECTURE.md", "DESIGN.md"]
DEFAULT_N = 2
LARGE_DIFF_LINES = 100
LARGE_DELETION_RATIO = 0.5
MAX_CONFIG_BYTES = 512 * 1024


def _git(*args: str, cwd: Path) -> str:
    res = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=False,
    )
    return res.stdout


def _read_convergence_n(repo: Path) -> int:
    if yaml is None:
        # PyYAML is a hard dependency; its absence is anomalous. If a
        # .peers/config.yaml exists we cannot parse it to honor a possibly
        # STRICTER describe_convergence_n — silently using DEFAULT_N would
        # weaken a configured HARD gate (FU-1 defense-in-depth), so fail
        # CLOSED. With no config there is nothing configured to violate, so
        # DEFAULT_N is safe.
        if (repo / ".peers" / "config.yaml").exists():
            raise RuntimeError(
                "PyYAML unavailable but .peers/config.yaml exists; cannot "
                "honor a configured describe_convergence_n (failing closed)"
            )
        return DEFAULT_N
    try:
        raw = read_bytes_under_root_no_follow(
            repo, (".peers", "config.yaml"),
            max_bytes=MAX_CONFIG_BYTES + 1,
        )
    except FileNotFoundError:
        return DEFAULT_N
    except (OSError, ValueError) as e:
        raise RuntimeError(f"config.yaml unreadable: {e}") from e
    if len(raw) > MAX_CONFIG_BYTES:
        raise RuntimeError(
            f"config.yaml exceeds {MAX_CONFIG_BYTES}-byte limit"
        )
    try:
        cfg_text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise RuntimeError(f"config.yaml unreadable: {e}") from e
    try:
        cfg = yaml.safe_load(cfg_text)
    except yaml.YAMLError:
        return DEFAULT_N
    if not isinstance(cfg, dict):
        return DEFAULT_N
    raw = (cfg.get("goals") or {}).get("describe_convergence_n", DEFAULT_N)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_N
    return n if n >= 1 else DEFAULT_N


def _recent_commits_touching_docs(
    repo: Path, n: int,
) -> list[str]:
    """Returns up to N most recent SHAs that touched any of the
    description files. May return < N if there aren't that many."""
    out = _git(
        "log", f"-n{n * 10}", "--format=%H",  # over-fetch then filter
        "--",
        *DESCRIPTION_FILES,
        cwd=repo,
    )
    return [line for line in out.splitlines() if line.strip()][:n]


def _section_headings(text: str) -> set[str]:
    return set(re.findall(r"^##\s+(.+?)\s*$", text, re.MULTILINE))


def _classify_commit(
    repo: Path, sha: str,
) -> tuple[bool, str]:
    """Returns (is_substantive, reason)."""
    numstat = _git(
        "show", "--numstat", "--format=", sha, "--",
        *DESCRIPTION_FILES,
        cwd=repo,
    )
    total_added = 0
    total_deleted = 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        try:
            added = int(parts[0]) if parts[0] != "-" else 0
            deleted = int(parts[1]) if parts[1] != "-" else 0
        except ValueError:
            continue
        total_added += added
        total_deleted += deleted
    if total_added >= LARGE_DIFF_LINES:
        return True, f"added {total_added} lines (>= {LARGE_DIFF_LINES})"
    total = total_added + total_deleted
    if total > 0 and (total_deleted / total) >= LARGE_DELETION_RATIO:
        return True, (
            f"deletion ratio {total_deleted}/{total} "
            f"(>= {LARGE_DELETION_RATIO})"
        )
    # Check section-heading diff: new `##` added that wasn't in parent.
    for fname in DESCRIPTION_FILES:
        new_text = _git("show", f"{sha}:{fname}", cwd=repo)
        parent_text = _git("show", f"{sha}^:{fname}", cwd=repo)
        new_secs = _section_headings(new_text)
        parent_secs = _section_headings(parent_text)
        added_secs = new_secs - parent_secs
        if added_secs:
            return True, (
                f"{fname} added section(s): {', '.join(sorted(added_secs))}"
            )
    return False, "non-substantive"


def main(repo: str = ".") -> int:
    root = Path(repo).resolve()
    try:
        n = _read_convergence_n(root)
    except RuntimeError as e:
        print(f"description_converged FAIL: {e}")
        return 1
    commits = _recent_commits_touching_docs(root, n)
    if len(commits) == 0:
        print(
            "description_converged FAIL: "
            "no commits touching SPEC.md/ARCHITECTURE.md/DESIGN.md yet",
        )
        return 1
    if len(commits) < n:
        print(
            "description_converged FAIL: only "
            f"{len(commits)}/{n} required commits to description files",
        )
        return 1
    substantive_findings: list[str] = []
    for sha in commits:
        sub, reason = _classify_commit(root, sha)
        if sub:
            substantive_findings.append(f"  {sha[:7]}: {reason}")
    if substantive_findings:
        print(
            f"description_converged FAIL: "
            f"last {n} commits to description files include "
            f"{len(substantive_findings)} substantive edit(s):",
        )
        for line in substantive_findings:
            print(line)
        return 1
    print(
        f"description_converged: clean "
        f"(last {n} commits to description files are non-substantive)",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
