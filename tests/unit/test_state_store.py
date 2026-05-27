import json
import os
from pathlib import Path

import pytest

from peers.state_store import StateStore, DEFAULT_STATE, SCHEMA_VERSION


def test_loads_default_when_missing(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    state = store.load()
    assert state == DEFAULT_STATE


def test_load_refuses_symlinked_state_file(tmp_path: Path):
    bait = tmp_path / "bait.json"
    bait.write_text(json.dumps(DEFAULT_STATE))
    (tmp_path / "state.json").symlink_to(bait)

    with pytest.raises(OSError):
        StateStore(tmp_path / "state.json").load()


def test_persists_and_reloads(tmp_path: Path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    state = store.load()
    state["iteration"] = 5
    state["turn_index"] = 1
    store.save(state)

    on_disk = json.loads(path.read_text())
    assert on_disk["iteration"] == 5
    assert on_disk["turn_index"] == 1


def test_atomic_write_uses_temp_then_rename(tmp_path: Path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.save({"iteration": 1, "peer_order": ["claude", "codex"],
                "turn_index": 0,
                "peers": {"claude": {"state": "healthy"},
                          "codex": {"state": "healthy"}}})
    assert path.exists()
    assert not (tmp_path / "state.json.tmp").exists()


def test_save_refuses_symlinked_temp_file(tmp_path: Path):
    path = tmp_path / "state.json"
    bait = tmp_path / "bait"
    bait.write_text("keep me")
    (tmp_path / "state.json.tmp").symlink_to(bait)

    with pytest.raises(OSError):
        StateStore(path).save({
            "iteration": 1,
            "peer_order": ["claude", "codex"],
            "turn_index": 0,
            "peers": {"claude": {"state": "healthy"},
                      "codex": {"state": "healthy"}},
        })

    assert bait.read_text() == "keep me"


def test_default_state_has_required_keys():
    for key in ("schema_version", "iteration", "peer_order", "turn_index",
                "budget", "goals_status", "stuck_counter", "peers"):
        assert key in DEFAULT_STATE


def test_default_peers_have_health_fields():
    for name in ("claude", "codex"):
        entry = DEFAULT_STATE["peers"][name]
        assert entry["state"] == "healthy"
        assert entry["consecutive_fails"] == 0


def test_default_schema_version_is_current():
    assert DEFAULT_STATE["schema_version"] == SCHEMA_VERSION


def test_load_does_not_share_mutable_default(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    s1 = store.load()
    s1["iteration"] = 99
    s2 = store.load()
    assert s2["iteration"] == 0


def test_load_merges_default_under_partial_file(tmp_path: Path):
    """A partial state file (missing keys) must not crash; the defaults
    fill in the gaps."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "iteration": 42,
        "peer_order": ["claude", "codex"],
        "turn_index": 1,
        "peers": {"claude": {"state": "healthy"},
                  "codex": {"state": "healthy"}},
    }))
    loaded = StateStore(path).load()
    assert loaded["iteration"] == 42
    assert loaded["turn_index"] == 1
    # Filled from default:
    assert loaded["budget"]["max_iterations"] == 200
    assert loaded["peers"]["claude"]["state"] == "healthy"
    assert loaded["peers"]["codex"]["state"] == "healthy"
    assert loaded["stuck_counter"] == {}


def test_load_raises_on_corrupt_json(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text("{not valid json")
    with pytest.raises(RuntimeError) as exc:
        StateStore(path).load()
    assert "corrupt" in str(exc.value).lower()
    assert str(path) in str(exc.value)


def test_load_rejects_oversized_state_file(tmp_path: Path):
    from peers.state_store import _STATE_FILE_MAX_BYTES

    path = tmp_path / "state.json"
    path.write_bytes(b"x" * (_STATE_FILE_MAX_BYTES + 1))

    with pytest.raises(RuntimeError, match="state file too large"):
        StateStore(path).load()


def test_load_accepts_state_file_at_size_cap(tmp_path: Path):
    from peers.state_store import _STATE_FILE_MAX_BYTES

    path = tmp_path / "state.json"
    data = json.dumps(DEFAULT_STATE).encode()
    assert len(data) < _STATE_FILE_MAX_BYTES
    path.write_bytes(data + b" " * (_STATE_FILE_MAX_BYTES - len(data)))

    loaded = StateStore(path).load()

    assert loaded["schema_version"] == SCHEMA_VERSION
    assert loaded["peer_order"] == DEFAULT_STATE["peer_order"]


def test_load_rejects_invalid_turn_index(tmp_path: Path):
    """A hand-edited state with turn_index out of range must be rejected
    at load instead of crashing deep in TurnManager."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "peer_order": ["claude", "codex"],
        "turn_index": 99,
        "peers": {"claude": {"state": "healthy"},
                  "codex": {"state": "healthy"}},
    }))
    with pytest.raises(RuntimeError, match="turn_index"):
        StateStore(path).load()


def test_load_rejects_invalid_peer_state(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "peer_order": ["claude", "codex"],
        "turn_index": 0,
        "peers": {"claude": {"state": "exploded"},
                  "codex": {"state": "healthy"}},
    }))
    with pytest.raises(RuntimeError, match="state"):
        StateStore(path).load()


def test_load_rejects_non_mapping_peer_entry(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "peer_order": ["claude", "codex"],
        "turn_index": 0,
        "peers": {"claude": "not-a-map",
                  "codex": {"state": "healthy"}},
    }))

    with pytest.raises(RuntimeError, match="peers.claude must be a mapping"):
        StateStore(path).load()


def test_default_state_has_wasted_runtime_field():
    assert "wasted_runtime_s" in DEFAULT_STATE["budget"]


def test_save_fsyncs(tmp_path: Path, monkeypatch):
    """Defends against power-loss leaving zero-byte state."""
    calls: list[int] = []
    real_fsync = os.fsync

    def spy_fsync(fd):
        calls.append(fd)
        real_fsync(fd)

    import peers.state_store as ss
    monkeypatch.setattr(ss.os, "fsync", spy_fsync)
    StateStore(tmp_path / "state.json").save(
        {"iteration": 1, "peer_order": ["claude", "codex"],
         "turn_index": 0, "peers": {"claude": {}, "codex": {}}}
    )
    assert calls, "os.fsync was not called during save"


# --- v1 → v2 migration ----------------------------------------

def test_migrates_v1_state_with_whose_turn(tmp_path: Path):
    """A legacy v1 state.json (with whose_turn and tools.{claude,codex})
    must be auto-migrated to v2 in-memory on load."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "iteration": 17,
        "whose_turn": "codex",
        "tools": {
            "claude": {"state": "healthy", "consecutive_fails": 0,
                       "recent_fails": 0},
            "codex": {"state": "degraded", "consecutive_fails": 2,
                      "recent_fails": 3},
        },
    }))
    state = StateStore(path).load()
    assert state["schema_version"] == SCHEMA_VERSION
    assert state["peer_order"] == ["claude", "codex"]
    assert state["turn_index"] == 1  # was "codex"
    assert "tools" not in state  # renamed away
    assert state["peers"]["codex"]["state"] == "degraded"
    assert state["peers"]["codex"]["recent_fails"] == 3


def test_migration_writes_pre_migration_backup(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"whose_turn": "claude", "tools": {}}))
    StateStore(path).load()
    backup = tmp_path / "state.json.pre-migration"
    assert backup.exists()
    snap = json.loads(backup.read_text())
    assert snap["whose_turn"] == "claude"


def test_migration_refuses_symlinked_pre_migration_backup(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"whose_turn": "claude", "tools": {}}))
    bait = tmp_path / "bait"
    bait.write_text("keep me")
    (tmp_path / "state.json.pre-migration").symlink_to(bait)

    with pytest.raises(RuntimeError, match="symlinked backup"):
        StateStore(path).load()

    assert bait.read_text() == "keep me"


def test_state_store_with_cfg_peer_order(tmp_path: Path):
    """If a config-derived peer_order is passed to StateStore, the
    default state for a missing file is shaped to those peers."""
    store = StateStore(tmp_path / "state.json",
                       peer_order=["alpha", "beta", "gamma"])
    s = store.load()
    assert s["peer_order"] == ["alpha", "beta", "gamma"]
    assert set(s["peers"].keys()) == {"alpha", "beta", "gamma"}


def test_postv5_state_store_accepts_unavailable_state(tmp_path):
    """(post-v5 fix): the state_store schema validator must
    accept `unavailable` as a peer state. v5 tick 2 halt-class match
    set peer.state='unavailable' and crashed the save path because
    _VALID_PEER_STATES didn't list it (RuntimeError on _save_state
    → orchestrator died, leaving the unhalt'd state on disk and the
    container `--rm`-deleted, registry stuck at `crashed`).

    The missing entry was the single observable cause; this test
    nails the contract."""
    from peers.state_store import StateStore
    store = StateStore(tmp_path / "state.json",
                       peer_order=["claude", "codex"])
    state = store.load()
    state["peers"]["codex"]["state"] = "unavailable"
    state["peers"]["codex"]["unavailable_reason"] = (
        "halt-pattern: authentication failed"
    )
    state["peers"]["codex"]["unavailable_at_iter"] = 2
    # If 'unavailable' is not a valid state, save() raises here.
    store.save(state)
    reloaded = store.load()
    assert reloaded["peers"]["codex"]["state"] == "unavailable"
    assert reloaded["peers"]["codex"]["unavailable_reason"].startswith(
        "halt-pattern:"
    )
    assert reloaded["peers"]["codex"]["unavailable_at_iter"] == 2
