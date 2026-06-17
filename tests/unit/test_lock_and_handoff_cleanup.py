import os
from pathlib import Path

import pytest

from peers.state_store import release_run_lock
from peers.turn_manager import sweep_legacy_handoff_msg, write_handoff_msg


def test_release_run_lock_is_idempotent(tmp_path: Path):
    peers = tmp_path / ".peers"
    peers.mkdir()
    lock = peers / "run.lock"
    lock.write_text("1\n")

    release_run_lock(peers)
    release_run_lock(peers)

    assert not lock.exists()


def test_write_handoff_msg_uses_peers_dir(tmp_path: Path):
    write_handoff_msg(tmp_path, "hello")

    assert (tmp_path / ".peers" / "handoff-msg.txt").read_text() == "hello"
    assert not (tmp_path / "handoff-msg.txt").exists()


def test_sweep_legacy_handoff_msg_moves_dotfile(tmp_path: Path):
    (tmp_path / ".handoff-msg.txt").write_text("legacy")

    sweep_legacy_handoff_msg(tmp_path)

    assert not (tmp_path / ".handoff-msg.txt").exists()
    assert (tmp_path / ".peers" / "handoff-msg.txt").read_text() == "legacy"


def test_sweep_legacy_handoff_msg_refuses_hardlinked_dotfile(tmp_path: Path):
    """Sad path: legacy migration must not copy hard-linked scratch files."""
    victim = tmp_path / "victim.txt"
    victim.write_text("sensitive")
    legacy = tmp_path / ".handoff-msg.txt"
    os.link(victim, legacy)

    sweep_legacy_handoff_msg(tmp_path)

    assert victim.read_text() == "sensitive"
    assert legacy.exists()
    assert not (tmp_path / ".peers" / "handoff-msg.txt").exists()


def test_sweep_legacy_handoff_msg_refuses_oversized_dotfile(tmp_path: Path):
    """Edge path: oversized legacy scratch is left alone rather than copied."""
    legacy = tmp_path / ".handoff-msg.txt"
    legacy.write_text("x" * (64 * 1024 + 1))

    sweep_legacy_handoff_msg(tmp_path)

    assert legacy.exists()
    assert not (tmp_path / ".peers" / "handoff-msg.txt").exists()


def test_write_handoff_msg_overwrites_existing(tmp_path: Path):
    """Happy path: rewriting replaces content (atomic temp+rename)."""
    write_handoff_msg(tmp_path, "first")
    write_handoff_msg(tmp_path, "second longer payload")

    assert (
        tmp_path / ".peers" / "handoff-msg.txt"
    ).read_text() == "second longer payload"


def test_write_handoff_msg_does_not_follow_symlinked_leaf(tmp_path: Path):
    """BUG-183: a project-controlled symlink at .peers/handoff-msg.txt
    must not redirect the substrate write to its target. The atomic
    no-follow writer either rejects the swap or replaces the symlink
    atomically with a real file — either way, the victim it pointed at
    must NOT be written through."""
    peers = tmp_path / ".peers"
    peers.mkdir()
    victim = tmp_path / "outside.txt"
    victim.write_text("untouched")
    (peers / "handoff-msg.txt").symlink_to(victim)

    try:
        write_handoff_msg(tmp_path, "redirected")
    except OSError:
        # Reject path: symlink must still be a symlink (or unchanged) and
        # victim untouched.
        pass
    assert victim.read_text() == "untouched", (
        "symlink target must not be rewritten through the symlink"
    )
    leaf = peers / "handoff-msg.txt"
    # After a successful write, the leaf must be a regular file pointing
    # nowhere else. After a rejected write, the leaf is still a symlink to
    # `victim` (untouched), which is also fine.
    if leaf.exists() and not leaf.is_symlink():
        assert leaf.read_text() == "redirected"


def test_write_handoff_msg_refuses_symlinked_parent(tmp_path: Path):
    """BUG-183: a swapped .peers directory (symlink to another dir) must
    not let the write escape the control directory."""
    real_parent = tmp_path / "real_peers"
    real_parent.mkdir()
    elsewhere = tmp_path / "attacker_dir"
    elsewhere.mkdir()
    # .peers itself is a symlink to attacker_dir.
    os.symlink(elsewhere, tmp_path / ".peers")

    with pytest.raises(OSError):
        write_handoff_msg(tmp_path, "payload")

    assert not (elsewhere / "handoff-msg.txt").exists(), (
        "write must not land inside attacker-controlled directory"
    )


def test_write_handoff_msg_does_not_clobber_hardlinked_inode(tmp_path: Path):
    """BUG-183 edge: pre-planted hardlink at the leaf must not lead to
    clobbering the shared inode. The atomic writer uses temp+rename, so
    the hardlinked sibling inode is replaced atomically and the other
    name keeps its original content.
    """
    peers = tmp_path / ".peers"
    peers.mkdir()
    other = tmp_path / "other.txt"
    other.write_text("shared")
    os.link(other, peers / "handoff-msg.txt")

    try:
        write_handoff_msg(tmp_path, "would clobber")
    except OSError:
        pass

    assert other.read_text() == "shared", (
        "shared inode must not be truncated through the hardlink"
    )
