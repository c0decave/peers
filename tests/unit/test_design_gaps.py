"""Tests covering Phase-2 design-gap closures: G1 hybrid comm-layer,
G2 hooks-driver (peers tick), G4 soft-reviews + consensus, G5
token/USD parsing, G7 goal-mutation lock, G8 test-tampering detector,
G11 --dry-run."""
from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path

import pytest

from peers.comm_layer import HybridCommLayer
from peers.driver_orchestrator import (
    OrchestratorDriver,
    _parse_codex_tokens,
    _parse_claude_tokens,
)
from peers.peer_spec import PeerSpec
from peers.state_store import DEFAULT_STATE

ROOT = Path(__file__).parent.parent.parent


def _specs(*names: str) -> list[PeerSpec]:
    return [PeerSpec(name=n, tool=n if n in ("claude", "codex") else "claude",
                     argv=("true",), prompt_mode="stdin") for n in names]


# --- G1: HybridCommLayer -----------------------------------------------

def test_hybrid_send_and_fetch_messages(tmp_path: Path):
    pd = tmp_path / ".peers"
    pd.mkdir()
    layer = HybridCommLayer(tmp_path, pd)
    p1 = layer.send("claude", "codex", "first ping", "Hello.")
    p2 = layer.send("claude", "codex", "second ping", "World.")
    msgs = layer.fetch_new("claude", "codex")
    assert msgs == [p1, p2]
    assert "first" in p1.read_text() and "from: claude" in p1.read_text()


def test_hybrid_archive_moves_message(tmp_path: Path):
    pd = tmp_path / ".peers"
    pd.mkdir()
    layer = HybridCommLayer(tmp_path, pd)
    p = layer.send("a", "b", "topic", "body")
    layer.archive(p)
    assert not p.exists()
    archived = list((pd / "comms" / "archive").iterdir())
    assert len(archived) == 1


# --- G2: peers tick ----------------------------------------------------

def test_peers_tick_runs_exactly_one_iteration(tmp_path: Path):
    import json as _json
    import os as _os
    target = tmp_path / "t"
    target.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=target,
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=target,
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=target,
                   check=True, capture_output=True)
    (target / "README").write_text("x")
    subprocess.run(["git", "add", "README"], cwd=target,
                   check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=target,
                   check=True, capture_output=True)

    env = _os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    r = subprocess.run(
        [sys.executable, "-m", "peers", "-C", str(target), "init"],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 0, r.stderr

    fake = ROOT / "tests" / "fixtures" / "fake_peer.py"
    cfg = target / ".peers" / "config.yaml"
    cfg.write_text(
        "driver: orchestrator\ncomm: git\n"
        "peers:\n"
        f"  - {{name: claude, tool: claude, argv: ['{sys.executable}', '{fake}']}}\n"
        f"  - {{name: codex,  tool: codex,  argv: ['{sys.executable}', '{fake}']}}\n"
        "budget: {max_iterations: 10, max_runtime_s: 60,"
        " max_consecutive_failures: 10}\n"
        "health: {idle_timeout_s: 30, absolute_max_runtime_s: 30}\n"
    )

    r = subprocess.run(
        [sys.executable, "-m", "peers", "-C", str(target), "tick"],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 0, r.stderr
    state = _json.loads((target / ".peers" / "state.json").read_text())
    assert state["iteration"] == 1


# --- G5: token / USD parsing ------------------------------------------

def test_parse_codex_tokens():
    out = "doing work\ntokens used\n1,461\nfinal output\n"
    tok, usd = _parse_codex_tokens(out)
    assert tok == 1461
    assert usd == 0.0


def test_parse_claude_tokens():
    out = "blah\n2,345 tokens used.\nCost: $0.04\n"
    tok, usd = _parse_claude_tokens(out)
    assert tok == 2345
    assert abs(usd - 0.04) < 1e-9


def test_parse_claude_tokens_json_envelope():
    """`claude -p --output-format json` envelope."""
    out = (
        '{"type":"result","subtype":"success","is_error":false,'
        '"duration_ms":1234,"num_turns":2,'
        '"result":"OK","session_id":"abc",'
        '"total_cost_usd":0.0517,'
        '"usage":{"input_tokens":12,"cache_creation_input_tokens":200,'
        '"cache_read_input_tokens":3000,"output_tokens":45}}'
    )
    tok, usd = _parse_claude_tokens(out)
    assert tok == 12 + 200 + 3000 + 45
    assert abs(usd - 0.0517) < 1e-9


def test_parse_claude_tokens_stream_json():
    """`claude -p --output-format stream-json --verbose` — last line
    carries the result envelope; earlier lines do not have usage."""
    out = (
        '{"type":"system","subtype":"init","session_id":"x"}\n'
        '{"type":"assistant","message":{"content":[]}}\n'
        '{"type":"result","subtype":"success",'
        '"total_cost_usd":1.5,'
        '"usage":{"input_tokens":1,"output_tokens":2,'
        '"cache_read_input_tokens":3}}\n'
    )
    tok, usd = _parse_claude_tokens(out)
    assert tok == 6
    assert abs(usd - 1.5) < 1e-9


def test_parse_claude_tokens_json_with_banner():
    """A banner line before the JSON envelope still parses."""
    out = (
        "claude CLI vX.Y.Z — initializing\n"
        '{"usage":{"input_tokens":10,"output_tokens":20},'
        '"total_cost_usd":0.01}\n'
    )
    tok, usd = _parse_claude_tokens(out)
    assert tok == 30
    assert abs(usd - 0.01) < 1e-9


def test_parse_claude_tokens_malformed_json_falls_back():
    """Broken JSON-ish output → fallback to text regex. internal testing BUG-004 tightened the fallback: now requires the
    literal `tokens used` and `Cost:` keywords on their own line so
    arbitrary token-mention prose can't pollute the counter."""
    out = '{"usage":bad json here\n1,500 tokens used\nCost: $0.02'
    tok, usd = _parse_claude_tokens(out)
    assert tok == 1500
    assert abs(usd - 0.02) < 1e-9


def test_parse_claude_tokens_ignores_prose_token_mentions():
    """BUG-004 regression: a narrative `1,234 tokens` (no
    "used" keyword, no leading $) must NOT be added to the counter
    — the old regex summed every token-mention in stdout."""
    out = (
        "Sure, I'll keep this under 200 tokens of detail.\n"
        "The API returned 1024 tokens of context in the prompt.\n"
        "Price ~$0.05 per million.\n"
    )
    tok, usd = _parse_claude_tokens(out)
    assert tok == 0
    assert usd == 0.0


def test_parse_claude_tokens_silent_default_mode():
    """Default claude -p output (no JSON, no token line) → (0, 0)."""
    out = "Sure, I've added the feature. Done.\n"
    tok, usd = _parse_claude_tokens(out)
    assert tok == 0
    assert usd == 0.0


# --- G4: soft reviews + consensus -------------------------------------

def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "README").write_text("x")
    _git(path, "add", "README")
    _git(path, "commit", "-q", "-m", "init")
    return path


class _Commit:
    def __init__(self, body, trailers, sha):
        self.body = body
        self.trailers = trailers
        self.sha = sha


def test_soft_review_consensus_advances_on_consecutive_pass(tmp_path: Path):
    from peers.goals import Goal
    repo = _init_repo(tmp_path / "r")
    soft = Goal(
        id="docs-complete", type="soft",
        prompt="check", reviewer="other", consensus_needed=2,
    )
    drv = OrchestratorDriver(
        repo=repo, peer_dir=repo / ".peers",
        goals=[soft],
        peer_specs=_specs("claude", "codex"),
    )
    state = copy.deepcopy(DEFAULT_STATE)

    body1 = '## Review\n\n{"pass": true, "notes": "ok"}\n\n'
    drv._record_soft_review_from_commit(
        state,
        _Commit(body1,
                {"Peer-Review-Of": "docs-complete", "Peer": "codex"},
                "a" * 40),
        reviewer="codex",
    )
    assert state["soft_status"]["docs-complete"]["consensus_count"] == 1

    drv._record_soft_review_from_commit(
        state,
        _Commit(body1,
                {"Peer-Review-Of": "docs-complete", "Peer": "claude"},
                "b" * 40),
        reviewer="claude",
    )
    assert state["soft_status"]["docs-complete"]["consensus_count"] == 2


def test_soft_review_fail_resets_consensus(tmp_path: Path):
    from peers.goals import Goal
    repo = _init_repo(tmp_path / "r")
    soft = Goal(id="g", type="soft", prompt="p", reviewer="other",
                consensus_needed=2)
    drv = OrchestratorDriver(
        repo=repo, peer_dir=repo / ".peers",
        goals=[soft],
        peer_specs=_specs("claude", "codex"),
    )
    state = copy.deepcopy(DEFAULT_STATE)
    state["soft_status"] = {
        "g": {"consensus_count": 1, "last_pass": True, "history": []},
    }

    drv._record_soft_review_from_commit(
        state,
        _Commit('{"pass": false}',
                {"Peer-Review-Of": "g", "Peer": "claude"}, "c" * 40),
        reviewer="claude",
    )
    assert state["soft_status"]["g"]["consensus_count"] == 0
    assert state["soft_status"]["g"]["last_pass"] is False


# --- G7: goal-mutation lock --------------------------------------------

def test_goal_mutation_detected(tmp_path: Path):
    repo = _init_repo(tmp_path / "r")
    pd = repo / ".peers"
    pd.mkdir()
    (pd / "goals.yaml").write_text("goals: []\n")
    import hashlib
    expected = hashlib.sha256(b"goals: []\n").hexdigest()
    (pd / "goals.sha256").write_text(expected + "\n")
    drv = OrchestratorDriver(
        repo=repo, peer_dir=pd, goals=[],
        peer_specs=_specs("claude", "codex"),
    )
    assert drv._goal_mutation_reason() is None

    (pd / "goals.yaml").write_text("goals:\n  - id: new\n    type: hard\n")
    reason = drv._goal_mutation_reason()
    assert reason is not None
    assert "hash" in reason


def test_phase1_goal_mutation_allowed_when_paired_with_src_change(
    tmp_path: Path,
):
    """(e) (post-2026-05-24): a peer that lands a goals.yaml
    edit ALONGSIDE a `src/` change in the SAME commit is doing feature
    work (e.g., v4 tick 17 added `Goal.timeout_s` field + updated
    no-prior-regression to use it). The mutation guard halted v4 even
    though the change was legitimate. Allow when both files are in
    HEAD's tree.

    Happy path: goals.yaml + src/ in same commit → no halt."""
    repo = _init_repo(tmp_path / "r")
    pd = repo / ".peers"
    pd.mkdir()
    (pd / "goals.yaml").write_text("goals: []\n")
    (repo / "src").mkdir()
    (repo / "src" / "x.py").write_text("y = 1\n")
    _git(repo, "add", ".peers/goals.yaml", "src/x.py")
    _git(repo, "commit", "-q", "-m", "init goals + src")
    drv = OrchestratorDriver(
        repo=repo, peer_dir=pd, goals=[],
        peer_specs=_specs("claude", "codex"),
    )
    assert drv._goal_mutation_reason() is None

    # Peer simulates a paired feature commit:
    #  - edits goals.yaml AND
    #  - edits src/x.py
    # then commits BOTH together (single commit, two files).
    (pd / "goals.yaml").write_text(
        "goals:\n  - id: new\n    type: hard\n    timeout_s: 300\n"
    )
    (repo / "src" / "x.py").write_text("y = 1\ntimeout_s = 300\n")
    _git(repo, "add", ".peers/goals.yaml", "src/x.py")
    _git(repo, "commit", "-q", "-m", "Add per-goal timeout_s + apply")

    # behavior: paired feature work is allowed, no halt reason.
    assert drv._goal_mutation_reason() is None


def test_phase1_goal_mutation_halts_on_pure_goals_yaml_edit(tmp_path: Path):
    """(e) sad: goals.yaml edited WITHOUT an accompanying
    src/ change in the same commit → halt as before (gaming
    suspicion preserved). This is also the case for uncommitted
    working-tree edits."""
    repo = _init_repo(tmp_path / "r")
    pd = repo / ".peers"
    pd.mkdir()
    (pd / "goals.yaml").write_text("goals: []\n")
    (repo / "src").mkdir()
    (repo / "src" / "x.py").write_text("y = 1\n")
    _git(repo, "add", ".peers/goals.yaml", "src/x.py")
    _git(repo, "commit", "-q", "-m", "init")
    drv = OrchestratorDriver(
        repo=repo, peer_dir=pd, goals=[],
        peer_specs=_specs("claude", "codex"),
    )
    # Pure goals.yaml edit (no companion src/ change).
    (pd / "goals.yaml").write_text(
        "goals:\n  - id: relaxed\n    type: hard\n"
    )
    _git(repo, "add", ".peers/goals.yaml")
    _git(repo, "commit", "-q", "-m", "loosen the gate")
    reason = drv._goal_mutation_reason()
    assert reason is not None
    assert "hash" in reason or "paired" in reason


def test_phase1_goal_mutation_halts_when_companion_is_inside_peers(
    tmp_path: Path,
):
    """(e) edge: a .peers-only commit (e.g., touching
    .peers/checks/*.py + .peers/goals.yaml) must STILL halt — both
    files are control-plane and the peer could be coordinating an
    attack against the gate. 'Paired feature work' means goals.yaml
    + a real source file OUTSIDE `.peers/`."""
    repo = _init_repo(tmp_path / "r")
    pd = repo / ".peers"
    pd.mkdir()
    (pd / "goals.yaml").write_text("goals: []\n")
    (pd / "checks").mkdir()
    (pd / "checks" / "x.py").write_text("# nothing\n")
    _git(repo, "add", ".peers/goals.yaml", ".peers/checks/x.py")
    _git(repo, "commit", "-q", "-m", "init")
    drv = OrchestratorDriver(
        repo=repo, peer_dir=pd, goals=[],
        peer_specs=_specs("claude", "codex"),
    )
    (pd / "goals.yaml").write_text(
        "goals:\n  - id: relaxed\n    type: hard\n"
    )
    (pd / "checks" / "x.py").write_text("# changed\n")
    _git(repo, "add", ".peers/goals.yaml", ".peers/checks/x.py")
    _git(repo, "commit", "-q", "-m", "control-plane only")
    reason = drv._goal_mutation_reason()
    assert reason is not None


# --- G8: test-tampering detector --------------------------------------

def test_driver_uses_hybrid_comm_layer_when_configured(tmp_path: Path):
    """G1 end-to-end wire: comm_variant='hybrid' actually instantiates
    HybridCommLayer."""
    repo = _init_repo(tmp_path / "r")
    pd = repo / ".peers"
    pd.mkdir()
    drv = OrchestratorDriver(
        repo=repo, peer_dir=pd, goals=[],
        peer_specs=_specs("claude", "codex"),
        comm_variant="hybrid",
    )
    assert isinstance(drv.comm, HybridCommLayer)
    assert drv.comm_variant == "hybrid"


def test_driver_rejects_unknown_comm_variant(tmp_path: Path):
    repo = _init_repo(tmp_path / "r")
    pd = repo / ".peers"
    pd.mkdir()
    with pytest.raises(ValueError, match="comm_variant"):
        OrchestratorDriver(
            repo=repo, peer_dir=pd, goals=[],
            peer_specs=_specs("claude", "codex"),
            comm_variant="carrier-pigeon",
        )


def test_soft_review_appears_in_prompt(tmp_path: Path):
    """G4 end-to-end wire: a pending soft goal lands in the prompt
    with the JSON-answer format spelled out."""
    from peers.goals import Goal
    from peers.prompt_builder import build_prompt

    soft = Goal(
        id="docs-complete", type="soft",
        prompt="Are all public docs current?",
        reviewer="other", consensus_needed=2,
    )
    p = build_prompt(
        peer="claude", other="codex",
        goals=[soft], results={},
        inbox=[], stuck=False,
        soft_reviews_pending=[soft],
    )
    assert "docs-complete" in p
    assert "Peer-Review-Of" in p
    assert "JSON" in p
    assert "Are all public docs current?" in p


def test_driver_computes_pending_soft_reviews(tmp_path: Path):
    from peers.goals import Goal
    repo = _init_repo(tmp_path / "r")
    pd = repo / ".peers"
    pd.mkdir()
    soft = Goal(
        id="g", type="soft", prompt="p", reviewer="other",
        consensus_needed=2,
    )
    soft_green = Goal(
        id="green", type="soft", prompt="p", reviewer="other",
        consensus_needed=2,
    )
    drv = OrchestratorDriver(
        repo=repo, peer_dir=pd, goals=[soft, soft_green],
        peer_specs=_specs("claude", "codex"),
    )
    state = copy.deepcopy(DEFAULT_STATE)
    # Mark `green` as already at consensus
    state["soft_status"] = {
        "green": {"consensus_count": 2, "last_pass": True, "history": []},
    }
    pending = drv._soft_reviews_pending(state, "claude")
    assert [g.id for g in pending] == ["g"]


def test_tampering_warning_on_test_only_diff(tmp_path: Path):
    repo = _init_repo(tmp_path / "r")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_a.py").write_text("def test_x(): pass\n")
    _git(repo, "add", "tests/test_a.py")
    _git(repo, "commit", "-q", "-m", "add tests\n\nPeer: claude\n")
    pd = repo / ".peers"
    pd.mkdir()
    (pd / "log").mkdir()
    drv = OrchestratorDriver(
        repo=repo, peer_dir=pd, goals=[],
        peer_specs=_specs("claude", "codex"),
    )
    state = copy.deepcopy(DEFAULT_STATE)
    # Pre-invoke head was the initial commit.
    drv._head_before_invoke = subprocess.run(
        ["git", "rev-list", "--max-parents=0", "HEAD"],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()
    drv._detect_tampering(state)
    warnings = state.get("warnings", [])
    assert any("test-tampering" in w for w in warnings), warnings


# --- reviewer modes -----------------------------------------

def test_reviewer_quorum_passes_after_threshold(tmp_path: Path):
    """quorum: '2/3' needs 2 pass:true reviews within the last 3
    submissions to flip the goal green."""
    from peers.goals import Goal
    repo = _init_repo(tmp_path / "r")
    pd = repo / ".peers"
    pd.mkdir()
    soft = Goal(
        id="q", type="soft", prompt="p",
        reviewer="quorum", quorum_num=2, quorum_den=3,
    )
    drv = OrchestratorDriver(
        repo=repo, peer_dir=pd, goals=[soft],
        peer_specs=_specs("claude", "codex", "claude-2"),
    )
    state = copy.deepcopy(DEFAULT_STATE)
    state["peer_order"] = ["claude", "codex", "claude-2"]

    # Three pass:true reviews — meets 2/3.
    for i, reviewer in enumerate(["claude", "codex", "claude-2"]):
        drv._record_soft_review_from_commit(
            state,
            _Commit('{"pass": true}',
                    {"Peer-Review-Of": "q", "Peer": reviewer},
                    str(i) * 40),
            reviewer=reviewer,
        )
    assert drv._all_green_including_soft(state) or \
        drv._soft_goal_passed(soft, state["soft_status"]["q"],
                              n_peers=len(state["peer_order"]))


def test_reviewer_both_requires_per_peer_consensus(tmp_path: Path):
    """reviewer: both with consensus_needed=1: every non-self peer
    must have submitted at least one pass:true review."""
    from peers.goals import Goal
    repo = _init_repo(tmp_path / "r")
    pd = repo / ".peers"
    pd.mkdir()
    soft = Goal(
        id="b", type="soft", prompt="p",
        reviewer="both", consensus_needed=1,
    )
    drv = OrchestratorDriver(
        repo=repo, peer_dir=pd, goals=[soft],
        peer_specs=_specs("claude", "codex"),
    )
    state = copy.deepcopy(DEFAULT_STATE)

    # Only claude has reviewed → not green yet.
    drv._record_soft_review_from_commit(
        state,
        _Commit('{"pass": true}',
                {"Peer-Review-Of": "b", "Peer": "claude"}, "a" * 40),
        reviewer="claude",
    )
    sg = state["soft_status"]["b"]
    assert not drv._soft_goal_passed(soft, sg, n_peers=2)

    # codex also reviews → now consensus is reached.
    drv._record_soft_review_from_commit(
        state,
        _Commit('{"pass": true}',
                {"Peer-Review-Of": "b", "Peer": "codex"}, "b" * 40),
        reviewer="codex",
    )
    assert drv._soft_goal_passed(soft, sg, n_peers=2)


def test_reviewer_alternating_picks_correct_peer(tmp_path: Path):
    from peers.goals import Goal
    repo = _init_repo(tmp_path / "r")
    pd = repo / ".peers"
    pd.mkdir()
    soft = Goal(
        id="alt", type="soft", prompt="p",
        reviewer="alternating", consensus_needed=2,
    )
    drv = OrchestratorDriver(
        repo=repo, peer_dir=pd, goals=[soft],
        peer_specs=_specs("claude", "codex"),
    )
    state = copy.deepcopy(DEFAULT_STATE)
    # First turn: alt_cursor=0 → claude.
    pending = drv._soft_reviews_pending(state, "claude")
    assert [g.id for g in pending] == ["alt"]
    pending = drv._soft_reviews_pending(state, "codex")
    assert pending == []  # not codex's turn for this goal

    # A review by claude advances cursor.
    drv._record_soft_review_from_commit(
        state,
        _Commit('{"pass": true}',
                {"Peer-Review-Of": "alt", "Peer": "claude"}, "a" * 40),
        reviewer="claude",
    )
    pending = drv._soft_reviews_pending(state, "codex")
    assert [g.id for g in pending] == ["alt"]
