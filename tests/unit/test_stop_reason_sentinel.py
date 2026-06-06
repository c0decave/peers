"""Tests for the orchestrator writing `.peers/last-stop-reason.txt`.

step 2a: when `peers run` exits, write a sentinel file so
`peers-ctl` reconcile can distinguish clean self-termination
("stopped") from a hard process death ("crashed"). Without this, v6
and v7 — both clean convergence runs — showed up as "crashed" in
`peers-ctl list`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from peers.driver_orchestrator import OrchestratorDriver
from peers.peer_spec import PeerSpec


def _init_repo(repo: Path) -> Path:
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "x").write_text("x")
    subprocess.run(["git", "add", "x"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def _specs() -> list[PeerSpec]:
    return [
        PeerSpec(name=n, tool=n if n in ("claude", "codex") else "claude",
                 argv=("true",), prompt_mode="stdin")
        for n in ("claude", "codex")
    ]


def test_max_ticks_exit_writes_sentinel(
    tmp_path: Path, monkeypatch,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    peer_dir = repo / ".peers"
    peer_dir.mkdir(mode=0o700)
    monkeypatch.setattr(
        "peers.driver_orchestrator._run_recon",
        lambda r, pd, force=False: "recon: stub",
    )
    drv = OrchestratorDriver(
        repo=repo, peer_dir=peer_dir, goals=[], peer_specs=_specs(),
    )
    drv.run(max_ticks=0)

    sentinel = peer_dir / "last-stop-reason.txt"
    assert sentinel.exists()
    content = sentinel.read_text()
    # Format: "<reason> <iso_timestamp>\n"
    parts = content.split()
    assert len(parts) >= 2
    assert parts[0] == "max_ticks", f"expected max_ticks, got {parts[0]!r}"
    # Has an ISO-8601-ish timestamp
    assert "T" in parts[1]


def test_sentinel_has_private_mode(
    tmp_path: Path, monkeypatch,
) -> None:
    """Sentinel must be 0o600 — peer_dir is private, sentinel inherits."""
    repo = _init_repo(tmp_path / "repo")
    peer_dir = repo / ".peers"
    peer_dir.mkdir(mode=0o700)
    monkeypatch.setattr(
        "peers.driver_orchestrator._run_recon",
        lambda r, pd, force=False: "recon: stub",
    )
    drv = OrchestratorDriver(
        repo=repo, peer_dir=peer_dir, goals=[], peer_specs=_specs(),
    )
    drv.run(max_ticks=0)
    sentinel = peer_dir / "last-stop-reason.txt"
    mode = sentinel.stat().st_mode & 0o777
    # Must not be world/group writable
    assert mode & 0o022 == 0, f"unsafe mode {oct(mode)}"


def test_sentinel_overwritten_on_re_run(
    tmp_path: Path, monkeypatch,
) -> None:
    """A subsequent run overwrites the previous sentinel (always
    reflects the LAST exit)."""
    repo = _init_repo(tmp_path / "repo")
    peer_dir = repo / ".peers"
    peer_dir.mkdir(mode=0o700)
    # Plant an old sentinel from a previous "crash" — should be replaced.
    (peer_dir / "last-stop-reason.txt").write_text(
        "old-content 2026-05-24T10:00:00+00:00\n",
    )
    monkeypatch.setattr(
        "peers.driver_orchestrator._run_recon",
        lambda r, pd, force=False: "recon: stub",
    )
    drv = OrchestratorDriver(
        repo=repo, peer_dir=peer_dir, goals=[], peer_specs=_specs(),
    )
    drv.run(max_ticks=0)

    new_content = (peer_dir / "last-stop-reason.txt").read_text()
    assert "old-content" not in new_content
    assert "max_ticks" in new_content


def test_sentinel_refuses_symlinked_tmp(
    tmp_path: Path, monkeypatch,
) -> None:
    """BUG-152: a pre-planted .peers/last-stop-reason.txt.tmp symlink to
    an outside same-user writable file must not become the chmod/write
    target. The atomic write helper refuses symlinked leaves, so the
    sentinel write either lands on a regular file or is skipped — the
    outside victim is never touched."""
    repo = _init_repo(tmp_path / "repo")
    peer_dir = repo / ".peers"
    peer_dir.mkdir(mode=0o700)
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched\n")
    # Plant the tmp leaf as a symlink to the victim BEFORE the run.
    tmp_link = peer_dir / "last-stop-reason.txt.tmp"
    tmp_link.symlink_to(victim)
    monkeypatch.setattr(
        "peers.driver_orchestrator._run_recon",
        lambda r, pd, force=False: "recon: stub",
    )
    drv = OrchestratorDriver(
        repo=repo, peer_dir=peer_dir, goals=[], peer_specs=_specs(),
    )
    drv.run(max_ticks=0)
    # The victim must not have been overwritten.
    assert victim.read_text() == "untouched\n"


def test_lock_held_does_not_clobber_sentinel(
    tmp_path: Path, monkeypatch,
) -> None:
    """When run() returns lock-held (another instance owns .peers/),
    the sentinel from the LIVE run must NOT be overwritten — the
    failed-to-acquire-lock case is informational, not a real exit."""
    import fcntl
    repo = _init_repo(tmp_path / "repo")
    peer_dir = repo / ".peers"
    peer_dir.mkdir(mode=0o700)
    (peer_dir / "last-stop-reason.txt").write_text(
        "complete 2026-05-25T10:00:00+00:00\n",
    )
    monkeypatch.setattr(
        "peers.driver_orchestrator._run_recon",
        lambda r, pd, force=False: "recon: stub",
    )
    # Hold the lock externally.
    lock_fp = open(peer_dir / "run.lock", "a")
    fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        drv = OrchestratorDriver(
            repo=repo, peer_dir=peer_dir, goals=[], peer_specs=_specs(),
        )
        result = drv.run(max_ticks=0)
        assert result.get("reason") == "lock-held"
    finally:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
        lock_fp.close()

    # Original sentinel preserved
    content = (peer_dir / "last-stop-reason.txt").read_text()
    assert "complete" in content
    assert "10:00:00" in content
