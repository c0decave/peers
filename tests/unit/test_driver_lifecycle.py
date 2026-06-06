"""Direct unit tests for ``peers.driver_lifecycle.DriverLifecycleMixin``.

The mixin's methods are exercised in full through the orchestrator
integration tests, but the dirty-worktree probe, the stop-reason
sentinel, and the symlink refusal are individually small enough that a
focused unit test pins each contract without spinning up a real driver.

A tiny fake driver class supplies the mixin attributes the methods
expect (``peer_dir``, ``repo``, ``_peer_dir_identity``); we deliberately
do NOT mount the full orchestrator surface so a refactor here can't
break unrelated checks.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from peers.driver_lifecycle import DriverLifecycleMixin


class _FakeDriver(DriverLifecycleMixin):
    """Minimal stand-in for the orchestrator driver."""

    def __init__(self, peer_dir: Path, repo: Path | None = None) -> None:
        self.peer_dir = peer_dir
        self.repo = repo or peer_dir.parent
        self._peer_dir_identity: tuple[int, int] | None = None


# --- _write_stop_reason ---------------------------------------------------

def test_write_stop_reason_creates_sentinel_with_reason_text(tmp_path):
    # happy: a clean stop writes the reason + ISO timestamp into the
    # sentinel file so peers-ctl reconcile can read it.
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir()
    drv = _FakeDriver(peer_dir)

    drv._write_stop_reason("stopped")

    sentinel = peer_dir / "last-stop-reason.txt"
    assert sentinel.is_file()
    text = sentinel.read_text()
    assert text.startswith("stopped ")
    # ISO timestamp ends with a newline
    assert text.endswith("\n")


def test_write_stop_reason_overwrites_previous_value_edge(tmp_path):
    # edge: subsequent calls atomically replace the sentinel — the
    # sentinel only ever reflects the LATEST stop reason, never an
    # appended history (which would silently grow without bound).
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir()
    drv = _FakeDriver(peer_dir)

    drv._write_stop_reason("budget:max_runtime")
    drv._write_stop_reason("complete")

    sentinel = peer_dir / "last-stop-reason.txt"
    text = sentinel.read_text()
    assert text.startswith("complete ")
    assert "budget:max_runtime" not in text


def test_write_stop_reason_handles_unwritable_dir_gracefully_sad(
    tmp_path, capsys,
):
    # sad: best-effort contract — a failure to write the sentinel
    # must NOT abort the run. Point peer_dir at a path that does
    # NOT exist (parent of tmp_path's never-created sibling) so the
    # tmp/replace dance raises and the warning falls through.
    drv = _FakeDriver(tmp_path / "no-such-peer-dir")
    drv._write_stop_reason("anything")  # must not raise
    err = capsys.readouterr().err
    assert "failed to write stop-reason sentinel" in err


# --- _capture_peer_dir_identity ------------------------------------------

def test_capture_peer_dir_identity_returns_dev_inode_pair(tmp_path):
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir()
    drv = _FakeDriver(peer_dir)

    dev, inode = drv._capture_peer_dir_identity()

    st = peer_dir.lstat()
    assert (dev, inode) == (st.st_dev, st.st_ino)


def test_capture_peer_dir_identity_refuses_symlink_edge(tmp_path):
    # edge: a peer_dir that resolves through a symlink would let a
    # malicious peer redirect substrate writes. The mixin must refuse.
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "linky"
    link.symlink_to(real)
    drv = _FakeDriver(link)

    with pytest.raises(RuntimeError, match="symlink"):
        drv._capture_peer_dir_identity()


def test_capture_peer_dir_identity_refuses_non_directory_sad(tmp_path):
    # sad: peer_dir pointing at a regular file (operator typo, leftover
    # from a botched scaffold) must refuse rather than later raise some
    # confusing OSError deep in state_store.save().
    not_a_dir = tmp_path / "oops"
    not_a_dir.write_text("not a dir")
    drv = _FakeDriver(not_a_dir)

    with pytest.raises(RuntimeError, match="not a directory"):
        drv._capture_peer_dir_identity()


def test_capture_peer_dir_identity_raises_on_missing_path_sad(tmp_path):
    drv = _FakeDriver(tmp_path / "ghost")

    with pytest.raises(RuntimeError, match="unavailable"):
        drv._capture_peer_dir_identity()


# --- _verify_peer_dir_identity -------------------------------------------

def test_verify_peer_dir_identity_first_call_seeds_then_succeeds(tmp_path):
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir()
    drv = _FakeDriver(peer_dir)

    drv._verify_peer_dir_identity()  # seeds
    drv._verify_peer_dir_identity()  # idempotent


def test_verify_peer_dir_identity_detects_swap_under_us_edge(tmp_path):
    # edge: peer_dir replaced after init (e.g. operator `rm -rf` +
    # `mkdir`) — the dev/inode tuple changes and the mixin refuses
    # any further control-plane IO.
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir()
    drv = _FakeDriver(peer_dir)
    drv._verify_peer_dir_identity()

    import shutil
    shutil.rmtree(peer_dir)
    peer_dir.mkdir()

    with pytest.raises(RuntimeError, match="changed while the loop"):
        drv._verify_peer_dir_identity()


# --- _dirty_worktree ------------------------------------------------------

def test_dirty_worktree_returns_false_for_clean_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=a",
         "commit", "--allow-empty", "-m", "seed"],
        cwd=repo, check=True, capture_output=True,
    )
    drv = _FakeDriver(tmp_path, repo=repo)

    assert drv._dirty_worktree() is False


def test_dirty_worktree_returns_true_for_uncommitted_change(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "x.txt").write_text("dirty")
    drv = _FakeDriver(tmp_path, repo=repo)

    assert drv._dirty_worktree() is True


def test_dirty_worktree_fails_safe_when_git_probe_errors_sad(tmp_path):
    # sad: git unavailable / non-repo / locked index — the probe
    # returns "dirty" so the caller cannot mistakenly trust a
    # silently-broken git as evidence of cleanliness.
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    drv = _FakeDriver(tmp_path, repo=not_a_repo)
    state: dict[str, Any] = {}

    assert drv._dirty_worktree(state) is True
    # The warning gets surfaced into the state so the operator can see
    # the underlying cause in the next prompt.
    assert state.get("warnings"), "dirty-worktree probe must record a warning"
    assert "git status returned" in state["warnings"][0]
