"""diff_size_per_resolve check — per-path diff cap for Bug-Resolves.

The audit-mode gate caps each path in a `Bug-Resolves` commit at 200
lines (insertions + deletions). This keeps one path reviewable without
punishing TDD fixes that include separate source and test files. BUG-162:
the script ran git with
`check=False` and never inspected return codes, so a missing
`peers-baseline` tag or a `git show` failure would silently report
clean. These tests cover the failure path (git lookups must surface
as exit 1) plus happy/edge/sad: oversized path fails, small commit
passes, multi-file aggregate-over-limit passes, no resolves passes.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


_CHECK = (
    Path(__file__).resolve().parents[2]
    / "src" / "peers" / "templates" / "modes" / "audit" / "checks"
    / "diff_size_per_resolve.py"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout


def _new_repo(tmp_path: Path) -> Path:
    p = tmp_path / "r"
    p.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=p, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=p, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=p, check=True)
    (p / "seed").write_text("seed\n")
    subprocess.run(["git", "add", "seed"], cwd=p, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=p, check=True)
    subprocess.run(["git", "tag", "peers-baseline"], cwd=p, check=True)
    return p


def _run_check(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_CHECK), str(repo)],
        capture_output=True, text=True,
    )


def _commit_resolve(repo: Path, bug_id: str, lines: int) -> None:
    f = repo / f"resolve_{bug_id}.txt"
    f.write_text("x\n" * lines)
    subprocess.run(["git", "-C", str(repo), "add", f.name], check=True)
    body = (
        f"Resolve {bug_id}: test commit\n\n"
        "## Bug-Resolution\n"
        f'{{"resolves": "{bug_id}", "status": "fixed", "note": "t"}}\n\n'
        f"Peer: t\nBug-Resolves: {bug_id}\n"
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", body], check=True,
    )


def _commit_resolve_files(repo: Path, bug_id: str, files: dict[str, int]) -> None:
    for name, lines in files.items():
        f = repo / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x\n" * lines)
        subprocess.run(["git", "-C", str(repo), "add", name], check=True)
    body = (
        f"Resolve {bug_id}: test commit\n\n"
        "## Bug-Resolution\n"
        f'{{"resolves": "{bug_id}", "status": "fixed", "note": "t"}}\n\n'
        f"Peer: t\nBug-Resolves: {bug_id}\n"
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", body], check=True,
    )


def test_clean_when_no_resolves(tmp_path):
    """Happy: no Bug-Resolves commits => clean exit."""
    repo = _new_repo(tmp_path)
    r = _run_check(repo)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "clean" in r.stdout


def test_small_resolve_passes(tmp_path):
    """Happy: a resolve under the 200-line cap passes."""
    repo = _new_repo(tmp_path)
    _commit_resolve(repo, "BUG-001", 50)
    r = _run_check(repo)
    assert r.returncode == 0, (r.stdout, r.stderr)


def test_multi_file_resolve_passes_when_each_path_under_limit(tmp_path):
    """Edge: source+test totals may exceed 200 if each path is reviewable."""
    repo = _new_repo(tmp_path)
    _commit_resolve_files(
        repo,
        "BUG-004",
        {"src/fix.py": 180, "tests/test_fix.py": 80},
    )
    r = _run_check(repo)
    assert r.returncode == 0, (r.stdout, r.stderr)


def test_oversized_resolve_path_fails(tmp_path):
    """Sad: one changed path over 200 lines fails with the diagnostic."""
    repo = _new_repo(tmp_path)
    _commit_resolve(repo, "BUG-002", 300)
    r = _run_check(repo)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "FAIL" in r.stdout
    assert "resolve_BUG-002.txt" in r.stdout
    assert "lines (limit 200)" in r.stdout


def _commit_waiver(repo: Path, target_sha: str, path: str, reason: str) -> None:
    """Land a Diff-Size-Waive commit for `<short_sha>:<path>` with reason."""
    short = target_sha[:8]
    body = (
        f"Waive diff size for {short}:{path}\n\n"
        "## Diff-Size-Waiver\n"
        f'{{"id": "{short}:{path}", "reason": "{reason}"}}\n\n'
        f"Peer: t\nDiff-Size-Waive: {short}:{path}\n"
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--allow-empty", "-q", "-m", body],
        check=True,
    )


def _short(repo: Path, ref: str = "HEAD") -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", ref],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_oversized_resolve_with_valid_waiver_passes(tmp_path):
    """BUG-194 happy: a substantive Diff-Size-Waive landed AFTER the
    oversized resolve waives the violation and the gate is clean."""
    repo = _new_repo(tmp_path)
    _commit_resolve(repo, "BUG-W1", 300)
    sha = _short(repo)
    _commit_waiver(
        repo, sha, "resolve_BUG-W1.txt",
        "Hardening refactor that touches every IO path; cannot be split "
        "without leaving an intermediate commit with a half-applied invariant.",
    )
    r = _run_check(repo)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "clean" in r.stdout
    assert "waived" in r.stdout.lower()


def test_oversized_resolve_with_short_reason_still_fails(tmp_path):
    """BUG-194 edge: a Diff-Size-Waive with a trivial reason (under the
    40-char minimum) does NOT waive the violation."""
    repo = _new_repo(tmp_path)
    _commit_resolve(repo, "BUG-W2", 300)
    sha = _short(repo)
    _commit_waiver(repo, sha, "resolve_BUG-W2.txt", "too short")
    r = _run_check(repo)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "FAIL" in r.stdout


def test_oversized_resolve_waiver_landed_before_fails(tmp_path):
    """BUG-194 sad: a waiver that landed BEFORE the resolve cannot
    pre-waive future oversized fixes — order matters."""
    repo = _new_repo(tmp_path)
    # Land a waiver commit referencing a sha that does not yet exist
    # (use a placeholder), then create the oversized resolve.
    _commit_waiver(
        repo, "deadbeef", "resolve_BUG-W3.txt",
        "Pre-waiver attempt to authorise a future oversized resolve commit.",
    )
    _commit_resolve(repo, "BUG-W3", 300)
    r = _run_check(repo)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "FAIL" in r.stdout


def test_missing_peers_baseline_fails_closed(tmp_path):
    """BUG-162: when `peers-baseline` is missing, the gate must fail
    closed (non-zero exit) instead of silently reporting clean."""
    repo = _new_repo(tmp_path)
    subprocess.run(
        ["git", "-C", str(repo), "tag", "-d", "peers-baseline"], check=True,
    )
    # And ensure there's a resolve commit so a fail-open bug would
    # silently miss real work.
    _commit_resolve(repo, "BUG-003", 300)
    r = _run_check(repo)
    assert r.returncode != 0, (r.stdout, r.stderr)
    # Diagnostic should explain the failure, not pretend everything is fine.
    assert "clean" not in r.stdout.lower() or "fail" in r.stdout.lower() \
        or r.stderr
