"""Tests for the `thorough` mode: convergence_reached HARD gate +
state.consecutive_clean_ticks counter."""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path


# --- Helpers ----------------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    _git(p, "init", "-q", "-b", "main")
    _git(p, "config", "user.email", "t@t")
    _git(p, "config", "user.name", "t")
    (p / "seed").write_text("seed")
    _git(p, "add", "seed")
    _git(p, "commit", "-q", "-m", "init")
    return p


def _commit(p: Path, msg: str) -> str:
    # Force a unique tree by writing a unique blob keyed on msg hash.
    fname = p / f"f-{abs(hash(msg)) & 0xffffff:06x}"
    fname.write_text(msg)
    _git(p, "add", fname.name)
    _git(p, "commit", "-q", "-m", msg)
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=p, capture_output=True, text=True,
    )
    return out.stdout.strip()


def _head(p: Path) -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=p, capture_output=True, text=True,
    )
    return out.stdout.strip()


# --- Counter behavior (tests 1-4) -------------------------------------------


def test_thorough_convergence_counter_increments_on_clean_tick(tmp_path: Path):
    """A tick with no new commits at all = clean tick = count 0."""
    from peers.bug_hunt import count_new_blocking_or_flag_bug_reports

    repo = _init_repo(tmp_path / "repo")
    since = _head(repo)
    # No new commits since `since` → clean.
    n = count_new_blocking_or_flag_bug_reports(repo, since)
    assert n == 0


def test_thorough_convergence_counter_resets_on_blocking_report(
    tmp_path: Path,
):
    """A tick that files a Bug-Report at severity high should count > 0."""
    from peers.bug_hunt import count_new_blocking_or_flag_bug_reports

    repo = _init_repo(tmp_path / "repo")
    since = _head(repo)
    msg = textwrap.dedent("""\
        Bug-XYZ: high-sev finding

        ## Bug-Report
        {
          "id": "BUG-099",
          "severity": "high",
          "title": "auth bypass"
        }

        Peer: claude
        Bug-Report: BUG-099
    """)
    _commit(repo, msg)
    n = count_new_blocking_or_flag_bug_reports(repo, since)
    assert n == 1


def test_thorough_convergence_counter_ignored_info_only_report(
    tmp_path: Path,
):
    """Info/low severity Bug-Reports must NOT count (otherwise loop never
    converges on "info: missing docstring" noise)."""
    from peers.bug_hunt import count_new_blocking_or_flag_bug_reports

    repo = _init_repo(tmp_path / "repo")
    since = _head(repo)
    msg = textwrap.dedent("""\
        Bug-INFO: missing docstring

        ## Bug-Report
        {
          "id": "BUG-100",
          "severity": "info",
          "title": "missing docstring"
        }

        Peer: claude
        Bug-Report: BUG-100
    """)
    _commit(repo, msg)
    n = count_new_blocking_or_flag_bug_reports(repo, since)
    assert n == 0


def test_thorough_convergence_counter_resets_on_weak_fix_flag_bug(
    tmp_path: Path,
):
    """A `weak-fix:` flag-bug counts even without a parseable severity in
    the JSON block — flag-bugs are inherently blocking."""
    from peers.bug_hunt import count_new_blocking_or_flag_bug_reports

    repo = _init_repo(tmp_path / "repo")
    since = _head(repo)
    msg = textwrap.dedent("""\
        Attack landed — weak fix for BUG-099

        ## Bug-Report
        {
          "id": "weak-fix:BUG-099",
          "title": "PoC bypasses sanitization"
        }

        Peer: codex
        Bug-Report: weak-fix:BUG-099
    """)
    _commit(repo, msg)
    n = count_new_blocking_or_flag_bug_reports(repo, since)
    assert n == 1


# --- Gate script behavior (tests 5-6) ---------------------------------------


def _convergence_script() -> Path:
    return (
        Path(__file__).parent.parent.parent
        / "src" / "peers" / "templates" / "modes" / "thorough"
        / "checks" / "convergence_reached.py"
    )


def test_thorough_gate_passes_at_threshold(tmp_path: Path):
    """state.json with consecutive_clean_ticks=3 + default N=3 → exit 0."""
    peers_dir = tmp_path / ".peers"
    peers_dir.mkdir()
    (peers_dir / "state.json").write_text(json.dumps(
        {"schema_version": 2, "consecutive_clean_ticks": 3}
    ))
    result = subprocess.run(
        [sys.executable, str(_convergence_script()), str(tmp_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "clean" in result.stdout


def test_thorough_gate_respects_config_override(tmp_path: Path):
    """N=5 from config but only 3 clean ticks recorded → exit 1."""
    peers_dir = tmp_path / ".peers"
    peers_dir.mkdir()
    (peers_dir / "state.json").write_text(json.dumps(
        {"schema_version": 2, "consecutive_clean_ticks": 3}
    ))
    (peers_dir / "config.yaml").write_text(
        "goals:\n  convergence_n: 5\n"
    )
    result = subprocess.run(
        [sys.executable, str(_convergence_script()), str(tmp_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "3/5" in result.stdout


def test_convergence_reached_refuses_symlinked_config_BUG_195(tmp_path: Path):
    """BUG-195: config.yaml must not be read through a project symlink."""
    peers_dir = tmp_path / ".peers"
    peers_dir.mkdir()
    (peers_dir / "state.json").write_text(json.dumps(
        {"schema_version": 2, "consecutive_clean_ticks": 1}
    ))
    real_config = tmp_path / "outside-config.yaml"
    real_config.write_text("goals:\n  convergence_n: 1\n")
    (peers_dir / "config.yaml").symlink_to(real_config)

    result = subprocess.run(
        [sys.executable, str(_convergence_script()), str(tmp_path)],
        capture_output=True, text=True,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "config.yaml unreadable" in result.stdout
    assert "clean" not in result.stdout


# --- Integration: orchestrator hook persists the counter (test 7) -----------


ROOT_FOR_TESTS = Path(__file__).parent.parent.parent


def test_thorough_consecutive_clean_ticks_persists_in_saved_state(
    tmp_path: Path,
):
    """End-to-end: drive a single real tick with a clean-handoff fake
    peer, then re-load state.json from disk and assert the orchestrator
    hook bumped `consecutive_clean_ticks` to 1 and persisted it.

    This is the spec's "Bei einem frischen run mit --max-ticks 1:
    state.consecutive_clean_ticks ist gesetzt" check. It exercises the
    code path in OrchestratorDriver._loop that wraps
    count_new_blocking_or_flag_bug_reports + _save_state, end-to-end —
    something the other six tests in this file do not cover.
    """
    from peers.driver_orchestrator import OrchestratorDriver
    from peers.peer_spec import PeerSpec
    from peers.goals import Goal

    target = _init_repo(tmp_path / "repo")
    peer_dir = target / ".peers"
    peer_dir.mkdir()
    (peer_dir / "log").mkdir()
    fake = [sys.executable, str(ROOT_FOR_TESTS / "tests" / "fixtures"
                                / "fake_peer.py")]
    # A never-passing hard goal so the loop actually runs the tick
    # instead of short-circuiting via all_green.
    never_pass = Goal(
        id="never", type="hard",
        cmd="false", pass_when="exit_code == 0",
    )
    drv = OrchestratorDriver(
        repo=target, peer_dir=peer_dir,
        goals=[never_pass],
        peer_specs=[
            PeerSpec(name="claude", tool="claude",
                     argv=tuple(fake), prompt_mode="stdin"),
            PeerSpec(name="codex", tool="codex",
                     argv=tuple(fake), prompt_mode="stdin"),
        ],
        idle_timeout_s=10, absolute_max_runtime_s=20,
    )
    drv.run(max_ticks=1)

    # Read state.json back from disk — the assertion is specifically
    # that the orchestrator hook PERSISTED the counter; in-memory would
    # not catch a missing _save_state call.
    state_on_disk = json.loads((peer_dir / "state.json").read_text())
    # fake_peer.py produces a single handoff commit with no Bug-Report
    # trailers → counted as a clean tick → counter should be exactly 1.
    assert state_on_disk["consecutive_clean_ticks"] == 1, state_on_disk


# --- (post-2026-05-24): all_peers_healthy HARD gate ----------------


_GATE_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "src/peers/templates/modes/thorough/checks/all_peers_healthy.py"
)


def _run_gate(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_GATE_SCRIPT), str(root)],
        capture_output=True, text=True, check=False,
    )


def test_phase2_all_peers_healthy_passes_when_no_state_yet(tmp_path: Path):
    """Happy/edge: no state.json yet = no ticks ran = trivially OK.
    Mirrors `convergence_reached` no-state-yet convention."""
    r = _run_gate(tmp_path)
    assert r.returncode == 0
    assert "no state.json yet" in r.stdout


def test_phase2_all_peers_healthy_passes_when_all_healthy(tmp_path: Path):
    """Happy: every peer is `healthy` or `degraded` (legacy state) —
    gate exits 0. `degraded` is NOT a halt class; recovery is the
    legacy behavior."""
    pd = tmp_path / ".peers"
    pd.mkdir()
    (pd / "state.json").write_text(json.dumps({
        "peers": {
            "claude": {"state": "healthy"},
            "codex": {"state": "degraded"},
        },
        "exit_events": [],
    }))
    r = _run_gate(tmp_path)
    assert r.returncode == 0
    assert "all_peers_healthy" in r.stdout


def test_phase2_all_peers_healthy_fails_when_peer_unavailable(tmp_path: Path):
    """Sad: a peer at `state=unavailable` (halt_patterns hit) flips
    the gate red. The diagnostic must name the peer and surface the
    `unavailable_reason` annotation so operator action is obvious."""
    pd = tmp_path / ".peers"
    pd.mkdir()
    (pd / "state.json").write_text(json.dumps({
        "peers": {
            "claude": {"state": "healthy"},
            "codex": {
                "state": "unavailable",
                "unavailable_reason": (
                    "halt-pattern: authentication failed"
                ),
                "unavailable_at_iter": 5,
                "unavailable_snippet": (
                    "2026-05-25T... ERROR auth: authentication failed"
                ),
            },
        },
        "exit_events": [],
    }))
    r = _run_gate(tmp_path)
    assert r.returncode == 1
    assert "all_peers_healthy FAIL" in r.stdout
    assert "codex" in r.stdout
    assert "authentication failed" in r.stdout


def test_phase2_all_peers_healthy_fails_on_exit_event(tmp_path: Path):
    """Edge: even if peer state was cleared, an exit_event of
    `peer-unavailable:<peer>` is enough to flip the gate red — useful
    on the FIRST `peers verify` after a halted run before any cleanup."""
    pd = tmp_path / ".peers"
    pd.mkdir()
    (pd / "state.json").write_text(json.dumps({
        "peers": {
            "claude": {"state": "healthy"},
            "codex": {"state": "healthy"},
        },
        "exit_events": [
            {"reason": "peer-unavailable:codex", "ticks": 5},
        ],
    }))
    r = _run_gate(tmp_path)
    assert r.returncode == 1
    assert "exit_event" in r.stdout
    assert "peer-unavailable:codex" in r.stdout


def test_phase2_all_peers_healthy_fails_closed_on_corrupt_state(tmp_path: Path):
    """Sad: unreadable state.json → fail closed with a clear
    message, NOT silent green."""
    pd = tmp_path / ".peers"
    pd.mkdir()
    (pd / "state.json").write_text("{not json")
    r = _run_gate(tmp_path)
    assert r.returncode == 1
    assert "unreadable" in r.stdout


def test_phase2_thorough_goals_yaml_carries_all_peers_healthy(tmp_path: Path):
    """The shipped thorough mode must declare the new hard gate so
    `peers init --modes thorough` picks it up. Otherwise the halt
    classification has no visible follow-through in goals."""
    import yaml as _yaml
    goals = _yaml.safe_load(
        (Path(__file__).resolve().parents[2]
         / "src/peers/templates/modes/thorough/goals.yaml"
        ).read_text()
    )
    ids = [g["id"] for g in goals["goals"]]
    assert "all-peers-healthy" in ids


# --- BUG-102 / BUG-103 (v15 internal testing): the two thorough-mode state-reading
# gates must read state.json via safe_io no-symlink (refuse a symlinked
# state.json — CWE-59) and fail CLOSED on non-UTF-8 bytes instead of crashing
# with an uncaught UnicodeDecodeError (CWE-755). ------------------------------

_CONVERGENCE_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "src/peers/templates/modes/thorough/checks/convergence_reached.py"
)


def _run_convergence(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_CONVERGENCE_SCRIPT), str(root)],
        capture_output=True, text=True, check=False,
    )


def test_all_peers_healthy_refuses_symlinked_state(tmp_path: Path):
    """BUG-102: a swapped .peers/state.json symlink must not redirect the
    gate's read — refuse (fail closed) rather than follow it."""
    pd = tmp_path / ".peers"
    pd.mkdir()
    real = tmp_path / "real_state.json"
    real.write_text(json.dumps(
        {"peers": {"claude": {"state": "healthy"}}, "exit_events": []}
    ))
    (pd / "state.json").symlink_to(real)
    r = _run_gate(tmp_path)
    assert r.returncode == 1, r.stdout
    assert "unreadable" in r.stdout


def test_all_peers_healthy_fails_closed_on_non_utf8_state(tmp_path: Path):
    """BUG-103: a non-UTF-8 state.json fails the gate cleanly (return 1 +
    diagnostic), not via an uncaught UnicodeDecodeError traceback."""
    pd = tmp_path / ".peers"
    pd.mkdir()
    (pd / "state.json").write_bytes(b"\xff\xfe\x00 not utf-8 \x81\x82")
    r = _run_gate(tmp_path)
    assert r.returncode == 1
    assert "unreadable" in r.stdout
    assert "Traceback" not in r.stderr


def test_all_peers_healthy_fails_closed_on_invalid_utf8_inside_state_BUG_257(
    tmp_path: Path,
):
    """BUG-257: invalid UTF-8 inside a JSON string is still corrupt state.

    The gate must fail closed instead of replacement-decoding the byte,
    loading JSON successfully, and missing the unavailable peer state.
    """
    pd = tmp_path / ".peers"
    pd.mkdir()
    (pd / "state.json").write_bytes(
        b'{"peers":{"claude":{"state":"unavailable\xff"}},"exit_events":[]}'
    )
    r = _run_gate(tmp_path)
    assert r.returncode == 1
    assert "unreadable" in r.stdout
    assert "Traceback" not in r.stderr


def test_convergence_reached_refuses_symlinked_state(tmp_path: Path):
    """BUG-102: the convergence gate must not follow a symlinked state.json."""
    pd = tmp_path / ".peers"
    pd.mkdir()
    real = tmp_path / "real_state.json"
    real.write_text(json.dumps({"consecutive_clean_ticks": 5}))
    (pd / "state.json").symlink_to(real)
    r = _run_convergence(tmp_path)
    assert r.returncode == 1, r.stdout
    assert "unreadable" in r.stdout


def test_convergence_reached_fails_closed_on_non_utf8_state(tmp_path: Path):
    """BUG-103: a non-UTF-8 state.json fails the convergence gate cleanly."""
    pd = tmp_path / ".peers"
    pd.mkdir()
    (pd / "state.json").write_bytes(b"\xff\xfe\x00 not utf-8 \x81\x82")
    r = _run_convergence(tmp_path)
    assert r.returncode == 1
    assert "unreadable" in r.stdout
    assert "Traceback" not in r.stderr


def test_convergence_reached_fails_closed_on_invalid_utf8_inside_state_BUG_257(
    tmp_path: Path,
):
    """BUG-257: embedded invalid UTF-8 must not be replacement-decoded."""
    pd = tmp_path / ".peers"
    pd.mkdir()
    (pd / "state.json").write_bytes(
        b'{"consecutive_clean_ticks":999,"note":"\xff"}'
    )
    r = _run_convergence(tmp_path)
    assert r.returncode == 1
    assert "unreadable" in r.stdout
    assert "Traceback" not in r.stderr


def test_convergence_reached_fails_closed_on_non_object_state_BUG_241(
    tmp_path: Path,
):
    """BUG-241: valid JSON that is not a mapping is still malformed state."""
    pd = tmp_path / ".peers"
    pd.mkdir()
    (pd / "state.json").write_text("[1, 2]\n")
    r = _run_convergence(tmp_path)
    assert r.returncode == 1
    assert "FAIL" in r.stdout
    assert "Traceback" not in r.stderr


def test_convergence_reached_fails_closed_on_non_mapping_config_BUG_243(
    tmp_path: Path,
):
    """BUG-243: truthy non-mapping YAML config shapes are malformed."""
    for name, config_text in {
        "top": "[1]\n",
        "goals": "goals:\n  - 1\n",
    }.items():
        root = tmp_path / name
        pd = root / ".peers"
        pd.mkdir(parents=True)
        (pd / "state.json").write_text(json.dumps(
            {"schema_version": 2, "consecutive_clean_ticks": 0}
        ))
        (pd / "config.yaml").write_text(config_text)
        r = _run_convergence(root)
        assert r.returncode == 1
        assert "FAIL" in r.stdout
        assert "Traceback" not in r.stderr


def test_convergence_reached_fails_closed_on_non_integer_counter_BUG_244(
    tmp_path: Path,
):
    """BUG-244: malformed clean-tick counters should not traceback."""
    pd = tmp_path / ".peers"
    pd.mkdir()
    (pd / "state.json").write_text(json.dumps(
        {"schema_version": 2, "consecutive_clean_ticks": "abc"}
    ))
    r = _run_convergence(tmp_path)
    assert r.returncode == 1
    assert "FAIL" in r.stdout
    assert "Traceback" not in r.stderr


def test_all_peers_healthy_fails_closed_on_non_object_state_BUG_241(
    tmp_path: Path,
):
    """BUG-241: all_peers_healthy must not traceback on non-mapping state."""
    pd = tmp_path / ".peers"
    pd.mkdir()
    (pd / "state.json").write_text("[1, 2]\n")
    r = _run_gate(tmp_path)
    assert r.returncode == 1
    assert "FAIL" in r.stdout
    assert "Traceback" not in r.stderr


def test_all_peers_healthy_fails_closed_on_non_mapping_peer_BUG_256(
    tmp_path: Path,
):
    """BUG-256: malformed peer entries must not be counted as healthy."""
    pd = tmp_path / ".peers"
    pd.mkdir()
    (pd / "state.json").write_text(json.dumps({
        "peers": {
            "claude": "unavailable",
            "codex": {"state": "healthy"},
        },
        "exit_events": [],
    }))
    r = _run_gate(tmp_path)
    assert r.returncode == 1
    assert "all_peers_healthy FAIL" in r.stdout
    assert "state.peers.claude is not a mapping" in r.stdout
    assert "Traceback" not in r.stderr


def test_all_peers_healthy_fails_closed_on_non_string_snippet_BUG_245(
    tmp_path: Path,
):
    """BUG-245: malformed unavailable_snippet must not crash diagnostics."""
    pd = tmp_path / ".peers"
    pd.mkdir()
    (pd / "state.json").write_text(json.dumps({
        "peers": {
            "codex": {
                "state": "unavailable",
                "unavailable_reason": "halt-pattern: auth failed",
                "unavailable_at_iter": 5,
                "unavailable_snippet": 123,
            },
        },
        "exit_events": [],
    }))
    r = _run_gate(tmp_path)
    assert r.returncode == 1
    assert "all_peers_healthy FAIL" in r.stdout
    assert "Traceback" not in r.stderr


def test_all_peers_healthy_fails_closed_on_non_list_exit_events_BUG_248(
    tmp_path: Path,
):
    """BUG-248: malformed exit_events should not traceback or pass."""
    bad_values = [
        123,
        {"reason": "peer-unavailable:codex"},
        "peer-unavailable:codex",
    ]
    for idx, exit_events in enumerate(bad_values):
        root = tmp_path / f"case-{idx}"
        pd = root / ".peers"
        pd.mkdir(parents=True)
        (pd / "state.json").write_text(json.dumps({
            "peers": {},
            "exit_events": exit_events,
        }))
        r = _run_gate(root)
        assert r.returncode == 1
        assert "all_peers_healthy FAIL" in r.stdout
        assert "state.exit_events is not a list" in r.stdout
        assert "Traceback" not in r.stderr
