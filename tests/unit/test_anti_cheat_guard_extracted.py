from __future__ import annotations

import itertools
import subprocess
from pathlib import Path

from peers.anti_cheat_guard import AntiCheatGuard, is_test_only_commit


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "README").write_text("x\n")
    _git(path, "add", "README")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_is_test_only_commit_accepts_test_paths(tmp_path: Path):
    repo = _init_repo(tmp_path / "r")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_x(): pass\n")
    _git(repo, "add", "tests/test_x.py")
    _git(repo, "commit", "-q", "-m", "tests\n\nPeer: claude\n")

    assert is_test_only_commit(repo, "HEAD") is True


def test_guard_classifies_test_only_diff(tmp_path: Path):
    repo = _init_repo(tmp_path / "r")
    before = _head(repo)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_x(): pass\n")
    _git(repo, "add", "tests/test_x.py")
    _git(repo, "commit", "-q", "-m", "tests\n\nPeer: claude\n")

    guard = AntiCheatGuard(repo, before, lambda: _head(repo))

    assert guard.diff_stats_since_invoke() == (1, 0)
    assert "only test files" in guard.classify_cheating({})


def test_guard_reads_justified_test_only_marker(tmp_path: Path):
    repo = _init_repo(tmp_path / "r")
    before = _head(repo)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_x(): pass\n")
    _git(repo, "add", "tests/test_x.py")
    _git(
        repo, "commit", "-q", "-m",
        "tests only\n\nJUSTIFIED-TEST-ONLY: fixture-only repository has no production tree\n",
    )

    guard = AntiCheatGuard(repo, before, lambda: _head(repo))

    assert guard.test_only_justification() == (
        "fixture-only repository has no production tree"
    )


def test_detect_tampering_happy_records_recent_diff_stats(tmp_path: Path):
    repo = _init_repo(tmp_path / "r")
    before = _head(repo)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_x(): pass\n")
    (repo / "src.py").write_text("def f():\n    return 1\n")
    _git(repo, "add", "tests/test_x.py", "src.py")
    _git(repo, "commit", "-q", "-m", "mixed change\n")

    guard = AntiCheatGuard(repo, before, lambda: _head(repo))
    state: dict = {}

    guard.detect_tampering(state)

    assert state["recent_diff_stats"] == {
        _head(repo): {"test_lines": 1, "src_lines": 2}
    }
    assert "warnings" not in state


def test_detect_tampering_recent_diff_stats_stays_bounded_BUG_204(
    tmp_path: Path,
):
    """BUG-204 reproducer: ``detect_tampering`` appends a per-handoff entry
    into ``state['recent_diff_stats'][head_sha]`` on every call but never
    pruned. Over a long run (default budget caps at 200 iterations / 6h) the
    dict grew without bound, slowing each tick's atomic state-rewrite and
    eventually pushing the dashboard state-read past its 5MB max. Expected:
    cap at the last N (~50) shas so the dict stays bounded."""
    repo = _init_repo(tmp_path / "r")
    before = _head(repo)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_x(): pass\n")
    _git(repo, "add", "tests/test_x.py")
    _git(repo, "commit", "-q", "-m", "tests\n")

    counter = itertools.count()
    guard = AntiCheatGuard(repo, before, lambda: f"sha-{next(counter):08d}")

    state: dict = {"warnings": [], "recent_diff_stats": {}}
    for _ in range(200):
        guard.detect_tampering(state)

    assert len(state["recent_diff_stats"]) <= 50, (
        "BUG-204: recent_diff_stats grew to "
        f"{len(state['recent_diff_stats'])} entries without bound; expected "
        "the implementation to cap stored handoffs (~50) so long sessions "
        "don't bloat state.json past the dashboard limit"
    )


def test_detect_tampering_sad_malformed_recent_diff_stats_is_replaced(
    tmp_path: Path,
):
    repo = _init_repo(tmp_path / "r")
    before = _head(repo)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_x(): pass\n")
    _git(repo, "add", "tests/test_x.py")
    _git(repo, "commit", "-q", "-m", "tests\n")

    guard = AntiCheatGuard(repo, before, lambda: _head(repo))
    state: dict = {"warnings": [], "recent_diff_stats": "corrupt"}

    guard.detect_tampering(state)

    assert state["recent_diff_stats"] == {
        _head(repo): {"test_lines": 1, "src_lines": 0}
    }
    assert any("test-tampering" in warning for warning in state["warnings"])
