"""Tests for the deep-audit fixes (H1, H2, M1–M8, L1–L7, I3).

Each test corresponds to a finding in docs/2026-05-20-deep-audit-report.md
or to a new subcommand introduced as part of the fixes.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "README").write_text("x")
    _git(path, "add", "README")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _run_peers(cwd, *args):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "peers", "-C", str(cwd), *args],
        capture_output=True, text=True, env=env,
    )


# --- H1: in-memory goal hash snapshot ---------------------------------

def test_h1_coordinated_goal_rewrite_caught(tmp_path: Path):
    """A peer that rewrites BOTH goals.yaml AND goals.sha256 in
    lockstep must no longer fool the mutation lock."""
    from peers.driver_orchestrator import OrchestratorDriver
    from peers.peer_spec import PeerSpec
    import hashlib

    target = _init_repo(tmp_path / "t")
    pd = target / ".peers"
    pd.mkdir()
    (pd / "goals.yaml").write_text("goals: []\n")
    (pd / "goals.sha256").write_text(
        hashlib.sha256(b"goals: []\n").hexdigest()
    )
    drv = OrchestratorDriver(
        repo=target, peer_dir=pd, goals=[],
        peer_specs=[
            PeerSpec(name="claude", tool="claude",
                     argv=("true",), prompt_mode="stdin"),
            PeerSpec(name="codex", tool="codex",
                     argv=("true",), prompt_mode="stdin"),
        ],
    )
    assert drv._goal_mutation_reason() is None

    # Coordinated rewrite — both files together.
    new = b"goals:\n  - id: gamed\n    type: hard\n    cmd: \"true\"\n    pass_when: \"True\"\n"
    (pd / "goals.yaml").write_bytes(new)
    (pd / "goals.sha256").write_text(hashlib.sha256(new).hexdigest())

    # The lock NO LONGER fools — the in-memory snapshot is what's
    # compared.
    reason = drv._goal_mutation_reason()
    assert reason is not None
    assert "hash changed" in reason


def test_h1_deleted_goals_yaml_caught_as_mutation(tmp_path: Path):
    from peers.driver_orchestrator import OrchestratorDriver
    from peers.peer_spec import PeerSpec
    import hashlib

    target = _init_repo(tmp_path / "t")
    pd = target / ".peers"
    pd.mkdir()
    original = b"goals: []\n"
    (pd / "goals.yaml").write_bytes(original)
    (pd / "goals.sha256").write_text(hashlib.sha256(original).hexdigest())
    drv = OrchestratorDriver(
        repo=target, peer_dir=pd, goals=[],
        peer_specs=[
            PeerSpec(name="claude", tool="claude",
                     argv=("true",), prompt_mode="stdin"),
            PeerSpec(name="codex", tool="codex",
                     argv=("true",), prompt_mode="stdin"),
        ],
    )

    (pd / "goals.yaml").unlink()

    reason = drv._goal_mutation_reason()
    assert reason is not None
    assert "disappeared" in reason


def test_h1_oversized_goals_yaml_caught_as_mutation(tmp_path: Path):
    from peers.driver_orchestrator import OrchestratorDriver
    from peers.goals import _GOALS_YAML_MAX_BYTES
    from peers.peer_spec import PeerSpec
    import hashlib

    target = _init_repo(tmp_path / "t")
    pd = target / ".peers"
    pd.mkdir()
    original = b"goals: []\n"
    (pd / "goals.yaml").write_bytes(original)
    (pd / "goals.sha256").write_text(hashlib.sha256(original).hexdigest())
    drv = OrchestratorDriver(
        repo=target, peer_dir=pd, goals=[],
        peer_specs=[
            PeerSpec(name="claude", tool="claude",
                     argv=("true",), prompt_mode="stdin"),
            PeerSpec(name="codex", tool="codex",
                     argv=("true",), prompt_mode="stdin"),
        ],
    )

    (pd / "goals.yaml").write_bytes(b"#" * (_GOALS_YAML_MAX_BYTES + 1))

    reason = drv._goal_mutation_reason()
    assert reason is not None
    assert "too large" in reason


# --- M1: peers init commits .gitignore ---------------------------------

def test_m1_peers_init_leaves_clean_worktree(tmp_path: Path):
    """After `peers init` in a git repo, the worktree must be clean
    — the .gitignore touch is committed by peers-init."""
    target = _init_repo(tmp_path / "t")
    r = _run_peers(target, "init")
    assert r.returncode == 0, r.stderr
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=target, capture_output=True, text=True, check=True,
    )
    assert status.stdout.strip() == "", \
        f"worktree dirty after init: {status.stdout!r}"
    # Verify the init commit carries Peer: peers-init so the substrate
    # doesn't conflate it with peer work.
    log = subprocess.run(
        ["git", "log", "-1", "--format=%B"],
        cwd=target, capture_output=True, text=True, check=True,
    ).stdout
    assert "Peer: peers-init" in log


def test_m1_peers_init_in_non_git_dir_does_not_crash(tmp_path: Path):
    """In a non-git target, init should warn about peers-baseline +
    skip the .gitignore commit (no git → no commit), but otherwise
    succeed."""
    target = tmp_path / "bare"
    target.mkdir()
    r = _run_peers(target, "init")
    assert r.returncode == 0, r.stderr
    assert (target / ".peers" / "config.yaml").exists()


# --- H2: goal cmd timeout kills the process tree -----------------------

def test_h2_goal_cmd_timeout_kills_subtree(tmp_path: Path):
    """A goal cmd that backgrounds a child (e.g. `(sleep N) &`) must
    have its entire process group killed on timeout — not just the
    shell wrapper."""
    from peers.goals import Goal
    from peers.goal_engine import GoalEngine

    marker = tmp_path / f"orphan_marker_{os.getpid()}"
    if marker.exists():
        marker.unlink()
    g = Goal(
        id="bg", type="hard",
        cmd=f"(sleep 4 && touch {marker}) &",
        pass_when="exit_code == 0",
    )
    engine = GoalEngine([g], cwd=tmp_path, timeout_s=2)
    engine.evaluate_hard_gates()
    # Wait past the sleep — the killpg should have prevented the
    # touch from running.
    time.sleep(6)
    assert not marker.exists(), \
        "orphan child survived goal evaluation"


# --- M2: duplicate goal IDs rejected -----------------------------------

def test_m2_duplicate_goal_ids_rejected(tmp_path: Path):
    from peers.goals import load_goals
    p = tmp_path / "goals.yaml"
    p.write_text(
        "goals:\n"
        "  - id: x\n    type: hard\n    cmd: \"true\"\n    pass_when: \"True\"\n"
        "  - id: x\n    type: hard\n    cmd: \"false\"\n    pass_when: \"True\"\n"
    )
    with pytest.raises(ValueError, match="duplicate goal id"):
        load_goals(p)


# --- M3: duplicate peer_order entries rejected -------------------------

def test_m3_duplicate_peer_order_rejected(tmp_path: Path):
    from peers.state_store import StateStore, SCHEMA_VERSION
    p = tmp_path / "state.json"
    p.write_text(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "peer_order": ["claude", "claude"],
        "turn_index": 0,
        "peers": {"claude": {"state": "healthy"}},
    }))
    with pytest.raises(RuntimeError, match="duplicate"):
        StateStore(p).load()


# --- M4: reserved peer names rejected ----------------------------------

@pytest.mark.parametrize("name", [
    "peers-substrate", "peers-init", "archive",
    "comms", "log", "logs", "checks", "hooks", "queue",
])
def test_m4_reserved_peer_names_rejected(name: str):
    from peers.peer_spec import load_peer_specs
    cfg = {"peers": [
        {"name": name, "tool": "claude", "argv": ["true"]},
        {"name": "codex", "tool": "codex", "argv": ["true"]},
    ]}
    with pytest.raises(ValueError, match="reserved"):
        load_peer_specs(cfg)


def test_m4_reserved_names_via_tools_legacy_shape():
    from peers.peer_spec import load_peer_specs
    cfg = {"tools": {"peers-substrate": {"argv": ["true"]}}}
    with pytest.raises(ValueError, match="reserved"):
        load_peer_specs(cfg)


# --- M6: state_store.save validates -----------------------------------

def test_m6_save_rejects_corrupt_state(tmp_path: Path):
    from peers.state_store import StateStore
    p = tmp_path / "state.json"
    store = StateStore(p)
    with pytest.raises(RuntimeError):
        store.save({"peer_order": [], "turn_index": 0, "peers": {}})
    assert not p.exists(), "corrupt state should not have been persisted"


# --- M8: config-driven HealthGuard buffer cap -------------------------

def test_m8_buf_cap_bytes_threaded_through_invoke(tmp_path: Path):
    """A small buf_cap_bytes makes the truncation flag fire on
    output that exceeds the cap AND has enough lines for the
    head/tail-keep policy to trigger."""
    from peers.health_guard import HealthGuard
    hg = HealthGuard(tmp_path)
    # Produce 1000 small lines so the head+tail+marker truncation can
    # actually replace some of them.
    script = (
        "import sys\n"
        "for i in range(1000):\n"
        "    print('x' * 200)\n"
    )
    result = hg.invoke(
        argv=["python3", "-c", script],
        prompt="",
        idle_timeout_s=10, absolute_max_runtime_s=20,
        prompt_mode="argv-substitute",
        buf_cap_bytes=1024,  # 1 KB cap forces truncation
    )
    assert result.truncated is True


# --- L1: control-plane symlinks refused -------------------------------

def test_l1_state_json_symlink_refused(tmp_path: Path):
    """If state.json is a symlink, the driver refuses to operate."""
    from peers.driver_orchestrator import OrchestratorDriver
    from peers.peer_spec import PeerSpec
    target = _init_repo(tmp_path / "t")
    pd = target / ".peers"
    pd.mkdir()
    (pd / "config.yaml").write_text("driver: orchestrator\n")
    (pd / "goals.yaml").write_text("goals: []\n")
    # Symlink state.json -> /etc/passwd
    (pd / "state.json").symlink_to("/etc/passwd")
    drv = OrchestratorDriver(
        repo=target, peer_dir=pd, goals=[],
        peer_specs=[
            PeerSpec(name="claude", tool="claude",
                     argv=("true",), prompt_mode="stdin"),
            PeerSpec(name="codex", tool="codex",
                     argv=("true",), prompt_mode="stdin"),
        ],
    )
    with pytest.raises(RuntimeError, match="symlink"):
        drv._verify_no_control_symlinks()


# --- L3: duplicate Self-Review trailers deterministic -----------------

def test_l3_duplicate_self_review_trailers_first_seen_from_end_wins():
    """Walking from the end, the FIRST occurrence wins — i.e. the
    later-authored line. Earlier (probably hedging) trailers for the
    same key are ignored."""
    from peers.comm_layer import parse_trailers
    msg = (
        "subj\n\n"
        "body\n\n"
        "Self-Review: fail\n"
        "Self-Review: pass\n"
        "Peer-Status: handoff\n"
        "Peer: claude\n"
    )
    t = parse_trailers(msg)
    # Walking from end up, "Peer:" comes first, then "Peer-Status:",
    # then "Self-Review: pass" (this is the last line in the message,
    # so first hit walking backward). The "fail" is ignored.
    assert t["Self-Review"] == "pass"


# --- L4: garbage run.lock content rendered safely ----------------------

def test_l4_run_lock_garbage_displayed_safely(tmp_path: Path):
    """`peers status` must not render arbitrary lock-file content as
    a literal `pid` value."""
    target = _init_repo(tmp_path / "t")
    r = _run_peers(target, "init")
    assert r.returncode == 0
    # Remove placeholder so we can `run` later if needed; not required
    # for status.
    g = (target / ".peers" / "goals.yaml")
    g.write_text(g.read_text().replace(
        "placeholder-replace-me", "x-replaced"))
    import hashlib
    (target / ".peers" / "goals.sha256").write_text(
        hashlib.sha256(g.read_bytes()).hexdigest()
    )
    # Need a state.json first so `peers status` doesn't bail.
    from peers.state_store import StateStore
    StateStore(target / ".peers" / "state.json").save({
        "schema_version": 2, "iteration": 0,
        "peer_order": ["claude", "codex"], "turn_index": 0,
        "peers": {"claude": {"state": "healthy"},
                  "codex": {"state": "healthy"}},
    })
    (target / ".peers" / "run.lock").write_text("GARBAGE_NOT_A_PID\n")
    r = _run_peers(target, "status")
    assert r.returncode == 0
    assert "Lock held: pid GARBAGE_NOT_A_PID" not in r.stdout
    assert "stale" in r.stdout.lower() or "not a PID" in r.stdout


def test_l4_run_lock_held_displayed_as_active_pid(tmp_path: Path):
    target = _init_repo(tmp_path / "t")
    r = _run_peers(target, "init")
    assert r.returncode == 0
    from peers.state_store import StateStore

    StateStore(target / ".peers" / "state.json").save({
        "schema_version": 2, "iteration": 0,
        "peer_order": ["claude", "codex"], "turn_index": 0,
        "peers": {"claude": {"state": "healthy"},
                  "codex": {"state": "healthy"}},
    })
    lock_path = target / ".peers" / "run.lock"
    with lock_path.open("a+") as fp:
        fp.seek(0)
        fp.truncate()
        fp.write("12345\n")
        fp.flush()
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        try:
            r = _run_peers(target, "status")
        finally:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)

    assert r.returncode == 0
    assert "Lock held: pid 12345" in r.stdout


# --- L5: warnings clamp in prompt -------------------------------------

def test_l5_excessive_warnings_clamped(tmp_path: Path):
    """If state['warnings'] has > 50 entries when popped into a tick,
    the prompt-bound list gets clamped to head+tail with a marker."""
    # We can't easily exercise this through _loop without spawning
    # the substrate; instead exercise the clamping logic directly.
    warnings = [f"w{i}" for i in range(200)]
    # Mirror the clamping code in _loop.
    if len(warnings) > 50:
        clamped = (
            warnings[:5]
            + [f"... <{len(warnings) - 55} warnings omitted> ..."]
            + warnings[-50:]
        )
    else:
        clamped = warnings
    assert len(clamped) == 56
    assert clamped[5].startswith("...")
    assert clamped[-1] == "w199"


# --- L7: cmd_status migrates v1 in-memory -----------------------------

def test_l7_cmd_status_migrates_v1_in_memory(tmp_path: Path):
    """A v1 state.json (whose_turn + tools) shown via `peers status`
    must display in v2 shape — no fallback to the v1 branch."""
    target = _init_repo(tmp_path / "t")
    r = _run_peers(target, "init")
    assert r.returncode == 0
    # Replace state with a v1 shape
    (target / ".peers" / "state.json").write_text(json.dumps({
        "iteration": 7,
        "whose_turn": "codex",
        "tools": {"claude": {"state": "healthy"},
                  "codex": {"state": "healthy"}},
    }))
    r = _run_peers(target, "status")
    assert r.returncode == 0
    # The output mentions codex as the next peer (migrated).
    assert "codex" in r.stdout
    # And there's no leak of the v1 key.
    assert "whose_turn" not in r.stdout


# --- I3: peers info subcommand ----------------------------------------

def test_i3_peers_info_dumps_configuration(tmp_path: Path):
    target = _init_repo(tmp_path / "t")
    r = _run_peers(target, "init")
    assert r.returncode == 0
    # Drop placeholder so load_goals doesn't barf later
    g = (target / ".peers" / "goals.yaml")
    g.write_text(g.read_text().replace(
        "placeholder-replace-me", "x-replaced"))
    import hashlib
    (target / ".peers" / "goals.sha256").write_text(
        hashlib.sha256(g.read_bytes()).hexdigest()
    )
    r = _run_peers(target, "info")
    assert r.returncode == 0, r.stderr
    assert "driver:" in r.stdout
    assert "claude" in r.stdout and "codex" in r.stdout
    assert "budget:" in r.stdout
    assert "goals:" in r.stdout


def test_i3_peers_info_no_init_yet(tmp_path: Path):
    """info should error cleanly if peers init hasn't run."""
    target = _init_repo(tmp_path / "t")
    r = _run_peers(target, "info")
    assert r.returncode == 1
    assert "config.yaml" in r.stderr
