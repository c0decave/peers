from pathlib import Path

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
