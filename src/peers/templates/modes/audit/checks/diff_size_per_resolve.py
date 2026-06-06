#!/usr/bin/env python3
"""Exit 1 if any path in a Bug-Resolves commit exceeds the diff limit.

BUG-194: a substantive Diff-Size-Waive commit landed AFTER the oversized
resolve can grandfather a specific `<short_sha>:<path>` entry. The waiver
must carry a Peer trailer, name the entry in a Diff-Size-Waive trailer,
and include a JSON block with a `reason` (or `note`) of at least
_WAIVER_REASON_MIN characters. This mirrors the TDD gate's
Bug-Reproduce-Waive; future oversized fixes cannot be
pre-waived.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys


LIMIT = 200
_WAIVER_REASON_MIN = 40
_JSON_BLOCK = re.compile(r"\{[\s\S]*?\}", re.MULTILINE)


def _run_git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, check=False,
    )


def _first_json(body: str) -> object:
    for m in _JSON_BLOCK.finditer(body):
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
    return None


def _trailers(body: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in body.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if not key or " " in key:
            continue
        out.setdefault(key, []).append(val)
    return out


def _load_waivers(repo: str, valid_shas: set[str]) -> set[str]:
    """Return `<short_sha>:<path>` entries with a valid post-fix waiver.

    A waiver is valid iff it lands NEWER than its target resolve in
    `git log` newest-first order. `valid_shas` are full shas of the
    resolves we're checking; waivers reference them by short_sha prefix.
    """
    log = _run_git(
        repo, "log", "peers-baseline..HEAD",
        "--grep=^Diff-Size-Waive:", "--format=%H%x00%B%x1e",
    )
    if log.returncode != 0:
        return set()
    order_log = _run_git(repo, "log", "peers-baseline..HEAD", "--format=%H")
    # newest-first → lower index == newer
    order = {sha: i for i, sha in enumerate(order_log.stdout.splitlines())}
    waived: set[str] = set()
    for chunk in log.stdout.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        sha, _, body = chunk.partition("\x00")
        sha = sha.strip()
        trailers = _trailers(body)
        ids = trailers.get("Diff-Size-Waive", [])
        if not ids or not trailers.get("Peer"):
            continue
        json_block = _first_json(body)
        if not isinstance(json_block, dict):
            continue
        reason = str(json_block.get("reason") or json_block.get("note") or "")
        if len(reason.strip()) < _WAIVER_REASON_MIN:
            continue
        waiver_idx = order.get(sha, -1)
        for ent in ids:
            short_sha, _, _path = ent.partition(":")
            if not short_sha or not _path:
                continue
            matched = next(
                (s for s in valid_shas if s.startswith(short_sha)), None,
            )
            if matched is None:
                continue
            target_idx = order.get(matched, -1)
            if waiver_idx < 0 or target_idx < 0 or waiver_idx >= target_idx:
                continue
            waived.add(f"{matched[:8]}:{_path}")
    return waived


def main(repo: str = ".") -> int:
    log = _run_git(
        repo, "log", "peers-baseline..HEAD",
        "--grep=^Bug-Resolves:", "--format=%H",
    )
    if log.returncode != 0:
        # fail closed when git can't enumerate resolves
        # (missing peers-baseline tag, bad repo, etc). Silently
        # reporting clean would hide oversized fixes from the gate.
        sys.stderr.write(
            f"diff_size_per_resolve: git log failed (exit "
            f"{log.returncode}): {log.stderr.strip()}\n"
        )
        return 1
    commits = log.stdout.splitlines()
    waived = _load_waivers(repo, set(commits))
    over: list[str] = []
    waived_count = 0
    for sha in commits:
        show = _run_git(repo, "show", "--numstat", "--format=", sha)
        if show.returncode != 0:
            sys.stderr.write(
                f"diff_size_per_resolve: git show failed for {sha[:8]} "
                f"(exit {show.returncode}): {show.stderr.strip()}\n"
            )
            return 1
        for line in show.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3 or "-" in parts[:2]:
                continue
            ins = int(parts[0])
            deleted = int(parts[1])
            total = ins + deleted
            if total > LIMIT:
                entry = f"{sha[:8]}:{parts[2]}"
                if entry in waived:
                    waived_count += 1
                else:
                    over.append(f"{entry}: {total} lines (limit {LIMIT})")
    if over:
        print("diff_size_per_resolve FAIL:\n  " + "\n  ".join(over))
        return 1
    suffix = f", {waived_count} waived" if waived_count else ""
    print(
        f"diff_size_per_resolve: clean "
        f"({len(commits)} resolves, all paths <= {LIMIT}{suffix})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
