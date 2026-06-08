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
    """BUG-181: the writer used to use the predictable ``state.json.tmp``
    name; a pre-planted symlink there would be refused via O_NOFOLLOW.
    After BUG-181 the temp name is randomized per call, so the
    legacy pre-planted symlink is bypassed entirely — the save still
    succeeds AND the bait remains untouched (strictly stronger
    guarantee). The pre-planted symlink stays on disk; we just route
    around it."""
    path = tmp_path / "state.json"
    bait = tmp_path / "bait"
    bait.write_text("keep me")
    (tmp_path / "state.json.tmp").symlink_to(bait)

    StateStore(path).save({
        "iteration": 1,
        "peer_order": ["claude", "codex"],
        "turn_index": 0,
        "peers": {"claude": {"state": "healthy"},
                  "codex": {"state": "healthy"}},
    })

    assert path.exists()
    # The legacy predictable name is never written, so the bait survives.
    assert bait.read_text() == "keep me"


_VALID_STATE = {
    "iteration": 1,
    "peer_order": ["claude", "codex"],
    "turn_index": 0,
    "peers": {"claude": {"state": "healthy"},
              "codex": {"state": "healthy"}},
}


def test_save_refuses_symlinked_parent_before_temp_write(tmp_path: Path):
    """BUG-118 (v15 internal testing): save must refuse a symlinked PARENT before
    writing any state bytes. The leaf no-follow guard only protects the final
    path component; the kernel still resolves a symlinked parent, so without
    this the state bytes land in the attacker's target dir (and only the
    later durability fsync — BUG-114 — refused it). save now opens the parent
    via a no-follow (dev/ino-rechecked) dir_fd and writes relative to it."""
    evil = tmp_path / "evil"
    evil.mkdir()
    link = tmp_path / "work"
    link.symlink_to(evil, target_is_directory=True)

    with pytest.raises(OSError):
        StateStore(link / "state.json").save(dict(_VALID_STATE))

    assert not (evil / "state.json").exists()
    assert not (evil / "state.json.tmp").exists()


def test_save_refuses_late_hardlinked_temp_without_truncating(
    tmp_path: Path, monkeypatch,
):
    """BUG-119: a hardlink raced onto the writer's temp file
    before the open must not clobber the linked victim. BUG-181
    randomized the temp filename and added O_EXCL, so the same race
    now lands as a FileExistsError on the create rather than passing
    the nlink check — both outcomes satisfy the safety property: the
    bait keeps its bytes."""
    path = tmp_path / "state.json"
    bait = tmp_path / "bait"
    bait.write_text("keep me")

    real_open = os.open
    raced = {"done": False}

    def racing_open(p, flags, *args, **kwargs):
        # Match the new <name>.<random>.tmp scheme by basename pattern.
        name_str = str(p)
        if (
            not raced["done"]
            and "state.json." in name_str
            and name_str.endswith(".tmp")
            and (flags & os.O_CREAT)
        ):
            raced["done"] = True
            # Plant a hardlink at the exact name the writer is about to
            # try creating, so O_EXCL trips on the create.
            try:
                os.link(bait, name_str, dst_dir_fd=kwargs.get("dir_fd"))
            except (TypeError, OSError):
                # Fall back without dir_fd; relative paths land in cwd
                # only when we have the dir_fd, but the racing case is
                # already exercised via O_EXCL collision below.
                try:
                    os.link(bait, tmp_path / name_str)
                except OSError as e2:
                    pytest.skip(f"hard links unavailable: {e2}")
        return real_open(p, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)

    with pytest.raises(OSError):
        StateStore(path).save(dict(_VALID_STATE))

    assert raced["done"], "race hook never fired — test did not exercise path"
    assert bait.read_text() == "keep me"


def test_save_does_not_fsync_through_symlinked_parent(tmp_path: Path, monkeypatch):
    """BUG-114: if `.peers/` is swapped for a symlink to an
    attacker dir, save must neither write state into it nor fsync it. Since
    BUG-118 the parent is refused up front (O_NOFOLLOW + dev/ino recheck), so
    save raises before any bytes are written — strictly stronger than the
    original BUG-114 guarantee (which only refused the post-write dir-fsync)."""
    import stat as _stat

    evil = tmp_path / "evil"
    evil.mkdir()
    link = tmp_path / "work"
    link.symlink_to(evil, target_is_directory=True)
    evil_id = (evil.stat().st_dev, evil.stat().st_ino)

    fsynced_dirs: list[tuple[int, int]] = []
    real_fsync = os.fsync

    def spy_fsync(fd):
        try:
            st = os.fstat(fd)
            if _stat.S_ISDIR(st.st_mode):
                fsynced_dirs.append((st.st_dev, st.st_ino))
        except OSError:
            pass
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy_fsync)

    store = StateStore(link / "state.json")
    with pytest.raises(OSError):
        store.save(dict(_VALID_STATE))

    assert evil_id not in fsynced_dirs, (
        "durability dir-fsync followed the symlinked parent into the "
        "attacker directory"
    )
    assert not (evil / "state.json").exists()
    assert not (evil / "state.json.tmp").exists()


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


def test_load_rejects_invalid_utf8_state_file_BUG_262(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_bytes(
        b'{"schema_version": 2,'
        b'"peer_order": ["claude", "co\xffdex"],'
        b'"turn_index": 0,'
        b'"peers": {'
        b'  "claude": {"state": "healthy"},'
        b'  "co\xffdex": {"state": "healthy"}'
        b'}}'
    )

    with pytest.raises(RuntimeError, match="UTF-8|utf-8"):
        StateStore(path).load()


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


def test_load_rejects_non_list_peer_order_BUG_253(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "peer_order": 123,
        "turn_index": 0,
        "peers": {"claude": {"state": "healthy"},
                  "codex": {"state": "healthy"}},
    }))

    with pytest.raises(RuntimeError, match="peer_order"):
        StateStore(path).load()


def test_load_rejects_non_mapping_peers_BUG_253(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "peer_order": ["claude", "codex"],
        "turn_index": 0,
        "peers": ["not", "a", "mapping"],
    }))

    with pytest.raises(RuntimeError, match="peers must be a mapping"):
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

    # The durable write now lives in safe_io.atomic_write_text_in_dir_no_symlink;
    # patch the shared os.fsync (same module object both modules reference) so
    # the spy catches it regardless of which module issues the fsync.
    monkeypatch.setattr(os, "fsync", spy_fsync)
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
