import os
import time
from pathlib import Path

from peers.health_guard import claude_session_jsonl_path, jsonl_mtime_within


def test_claude_session_jsonl_path_encodes_absolute_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    path = claude_session_jsonl_path("/mnt/ext~/c0de/project")

    assert path == (
        tmp_path / ".claude" / "projects"
        / "-mnt-ext-home-user-c0de-project"
    )


def test_claude_session_jsonl_path_requires_home_and_absolute(monkeypatch):
    monkeypatch.delenv("HOME", raising=False)
    assert claude_session_jsonl_path("/work") is None
    monkeypatch.setenv("HOME", "/tmp")
    assert claude_session_jsonl_path("relative") is None


def test_jsonl_mtime_within_picks_newest_session(tmp_path: Path):
    old = tmp_path / "old.jsonl"
    new = tmp_path / "new.jsonl"
    old.write_text("{}\n")
    new.write_text("{}\n")
    stale = time.time() - 3600
    os.utime(old, (stale, stale))

    assert jsonl_mtime_within(tmp_path, within_seconds=60) is True


def test_jsonl_mtime_within_false_for_missing_or_old(tmp_path: Path):
    old = tmp_path / "old.jsonl"
    old.write_text("{}\n")
    stale = time.time() - 3600
    os.utime(old, (stale, stale))

    assert jsonl_mtime_within(tmp_path, within_seconds=60) is False
    assert jsonl_mtime_within(tmp_path / "missing", within_seconds=60) is False
