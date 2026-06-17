"""observability tests: tick markers, runs.jsonl tails on success,
per-tick stdout/stderr/prompt logs, and --verbose echo.

Each test drives a real OrchestratorDriver against a tmpdir repo +
the configurable `fake_peer_chatty.py` fixture, then asserts the
observable behavior (file contents / stderr substrings / jsonl
entries). Mocks are avoided — these contracts only matter end-to-end.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT_FOR_TESTS = Path(__file__).parent.parent.parent
CHATTY_PEER = ROOT_FOR_TESTS / "tests" / "fixtures" / "fake_peer_chatty.py"


# --- helpers ----------------------------------------------------------


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


def _build_driver(target: Path, peer_dir: Path, *, verbose: bool = False,
                  goals=None):
    """Build a driver wired to the chatty fake peer + a never-passing
    hard goal so the loop actually runs the tick.
    """
    from peers.driver_orchestrator import OrchestratorDriver
    from peers.peer_spec import PeerSpec
    from peers.goals import Goal

    fake = [sys.executable, str(CHATTY_PEER)]
    if goals is None:
        goals = [Goal(
            id="never", type="hard",
            cmd="false", pass_when="exit_code == 0",
        )]
    return OrchestratorDriver(
        repo=target, peer_dir=peer_dir,
        goals=goals,
        peer_specs=[
            PeerSpec(name="claude", tool="claude",
                     argv=tuple(fake), prompt_mode="stdin"),
            PeerSpec(name="codex", tool="codex",
                     argv=tuple(fake), prompt_mode="stdin"),
        ],
        idle_timeout_s=10, absolute_max_runtime_s=20,
        verbose=verbose,
    )


@pytest.fixture
def fresh_repo(tmp_path: Path):
    target = _init_repo(tmp_path / "repo")
    peer_dir = target / ".peers"
    peer_dir.mkdir()
    (peer_dir / "log").mkdir()
    return target, peer_dir


@pytest.fixture
def _restore_env():
    """Snapshot and restore FAKE_PEER_* env vars between tests."""
    keys = ("FAKE_PEER_STDOUT", "FAKE_PEER_STDERR",
            "FAKE_PEER_NO_COMMIT", "FAKE_PEER_EXIT_CODE")
    saved = {k: os.environ.get(k) for k in keys}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _read_runs_jsonl(peer_dir: Path) -> list[dict]:
    p = peer_dir / "log" / "runs.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


# --- tick-event markers -----------------------------------------


def test_loop_prints_tick_start_marker_to_stderr(
    fresh_repo, capfd, _restore_env,
):
    """Stderr from a 1-tick run contains the start marker."""
    target, peer_dir = fresh_repo
    drv = _build_driver(target, peer_dir)
    drv.run(max_ticks=1)
    captured = capfd.readouterr()
    assert "peers: tick 1 peer=" in captured.err
    assert "starting..." in captured.err


def test_loop_prints_tick_end_marker_with_handoff_sha_on_success(
    fresh_repo, capfd, _restore_env,
):
    """Successful tick prints `... handoff head=<8hex>` on stderr."""
    target, peer_dir = fresh_repo
    drv = _build_driver(target, peer_dir)
    drv.run(max_ticks=1)
    captured = capfd.readouterr()
    assert "peers: tick 1 handoff head=" in captured.err
    # head=<8hex> dur=<int>s — extract and sanity-check the hex.
    import re
    m = re.search(r"peers: tick 1 handoff head=([0-9a-f]{8}) dur=\d+s",
                  captured.err)
    assert m is not None, captured.err


def test_loop_prints_tick_end_marker_with_no_handoff(
    fresh_repo, capfd, _restore_env,
):
    """Peer ran cleanly but didn't commit → tick-end marker uses
    `no-handoff` (2026-05-26 UX fix; previously the confusing
    `fail(success)`)."""
    target, peer_dir = fresh_repo
    os.environ["FAKE_PEER_NO_COMMIT"] = "1"
    drv = _build_driver(target, peer_dir)
    drv.run(max_ticks=1)
    captured = capfd.readouterr()
    assert "peers: tick 1 no-handoff" in captured.err, captured.err
    # head=no-new-commit since the peer didn't commit anything.
    assert "head=no-new-commit" in captured.err


# --- stdout/stderr tail on success ticks ------------------------


def test_runs_jsonl_persists_stdout_tail_on_success(
    fresh_repo, _restore_env,
):
    """A successful tick records stdout_tail in runs.jsonl."""
    target, peer_dir = fresh_repo
    os.environ["FAKE_PEER_STDOUT"] = "hello world to stdout\n"
    drv = _build_driver(target, peer_dir)
    drv.run(max_ticks=1)
    entries = _read_runs_jsonl(peer_dir)
    tick_entries = [e for e in entries if e.get("event") != "exit"]
    assert len(tick_entries) == 1, entries
    e = tick_entries[0]
    assert e["success"] is True, e
    assert "stdout_tail" in e
    assert "hello world" in e["stdout_tail"]


def test_runs_jsonl_success_tail_shorter_than_fail_tail(
    tmp_path: Path, _restore_env,
):
    """cap: success stdout_tail <= 200 bytes; fail stdout_tail can
    be up to 400.
    """
    big = "x" * 5000
    # Run 1: success with big stdout.
    target1 = _init_repo(tmp_path / "r1")
    pd1 = target1 / ".peers"
    pd1.mkdir()
    (pd1 / "log").mkdir()
    os.environ["FAKE_PEER_STDOUT"] = big
    drv1 = _build_driver(target1, pd1)
    drv1.run(max_ticks=1)
    success_entries = [e for e in _read_runs_jsonl(pd1)
                       if e.get("event") != "exit"]
    assert success_entries[0]["success"] is True
    # success path uses 200-byte stdout cap.
    assert len(success_entries[0]["stdout_tail"]) <= 200

    # Run 2: fail (non-zero exit -> classification "process-fail") with
    # big stdout. Using exit_code rather than no-commit so the run's
    # `classification` is non-success and the wider fail cap kicks in.
    target2 = _init_repo(tmp_path / "r2")
    pd2 = target2 / ".peers"
    pd2.mkdir()
    (pd2 / "log").mkdir()
    os.environ["FAKE_PEER_STDOUT"] = big
    os.environ["FAKE_PEER_EXIT_CODE"] = "1"
    os.environ["FAKE_PEER_NO_COMMIT"] = "1"
    drv2 = _build_driver(target2, pd2)
    drv2.run(max_ticks=1)
    fail_entries = [e for e in _read_runs_jsonl(pd2)
                    if e.get("event") != "exit"]
    assert fail_entries[0]["success"] is False
    assert fail_entries[0]["classification"] != "success", fail_entries[0]
    # Fail cap is 400 bytes (non-success classification path).
    assert len(fail_entries[0]["stdout_tail"]) <= 400
    # And strictly larger than the success cap (proves the branch
    # selection works when stdout is bigger than both caps).
    assert len(fail_entries[0]["stdout_tail"]) > 200


def test_process_fail_tick_marker_surfaces_stderr_cause(
    fresh_repo, capfd, _restore_env,
):
    """A failed agent tick names its cause inline. Regression: claude refusing
    `--dangerously-skip-permissions` as root showed only
    `process-fail head=no-new-commit dur=0s`, with the real reason buried in a
    per-tick .stderr.log — opaque for the operator."""
    import re
    target, peer_dir = fresh_repo
    os.environ["FAKE_PEER_EXIT_CODE"] = "1"
    os.environ["FAKE_PEER_NO_COMMIT"] = "1"
    os.environ["FAKE_PEER_STDERR"] = (
        "--dangerously-skip-permissions cannot be used with root/sudo "
        "privileges for security reasons\n"
    )
    drv = _build_driver(target, peer_dir)
    drv.run(max_ticks=1)
    err = capfd.readouterr().err
    # The marker line itself must carry the reason (a bare tee elsewhere would
    # not match this `... dur=Ns -- <stderr>` shape).
    assert re.search(
        r"process-fail head=\S+ dur=\d+s -- "
        r"--dangerously-skip-permissions cannot be used with root",
        err,
    ), err


def test_first_error_line_helper():
    from peers.driver_observability import _first_error_line
    assert _first_error_line("\n\n  boom: the real cause\nsecond line\n") == "boom: the real cause"
    assert _first_error_line("") == ""
    assert _first_error_line("   \n  \n") == ""
    long = _first_error_line("x" * 400)
    assert long.endswith("…") and len(long) == 200


# --- per-tick peer output logs ----------------------------------


def test_peer_stdout_written_to_tick_log_file(
    fresh_repo, _restore_env,
):
    """`.peers/log/peers/tick-00001-<peer>.stdout.log` contains the
    peer's full stdout.
    """
    target, peer_dir = fresh_repo
    os.environ["FAKE_PEER_STDOUT"] = "this is full output\n"
    drv = _build_driver(target, peer_dir)
    drv.run(max_ticks=1)
    log_dir = peer_dir / "log" / "peers"
    # Don't hard-code the peer name — TurnManager picks based on order.
    matches = list(log_dir.glob("tick-00001-*.stdout.log"))
    assert len(matches) == 1, list(log_dir.iterdir())
    assert "this is full output" in matches[0].read_text()


def test_peer_stderr_written_to_tick_log_file(
    fresh_repo, _restore_env,
):
    """Same for stderr → `tick-00001-<peer>.stderr.log`."""
    target, peer_dir = fresh_repo
    os.environ["FAKE_PEER_STDERR"] = "this went to stderr\n"
    drv = _build_driver(target, peer_dir)
    drv.run(max_ticks=1)
    log_dir = peer_dir / "log" / "peers"
    matches = list(log_dir.glob("tick-00001-*.stderr.log"))
    assert len(matches) == 1, list(log_dir.iterdir())
    assert "this went to stderr" in matches[0].read_text()


def test_empty_peer_stdout_no_log_file_created(
    fresh_repo, _restore_env,
):
    """No zero-byte stdout file when the peer is silent."""
    target, peer_dir = fresh_repo
    # Only stderr — stdout stays empty.
    os.environ["FAKE_PEER_STDERR"] = "noise\n"
    drv = _build_driver(target, peer_dir)
    drv.run(max_ticks=1)
    log_dir = peer_dir / "log" / "peers"
    stdout_files = list(log_dir.glob("tick-00001-*.stdout.log"))
    stderr_files = list(log_dir.glob("tick-00001-*.stderr.log"))
    assert stdout_files == [], stdout_files
    assert len(stderr_files) == 1


# --- prompt log -------------------------------------------------


def test_prompt_written_to_tick_log_file(
    fresh_repo, _restore_env,
):
    """`.peers/log/prompts/tick-00001-<peer>.txt` contains the prompt
    sent to that peer.
    """
    target, peer_dir = fresh_repo
    drv = _build_driver(target, peer_dir)
    drv.run(max_ticks=1)
    prompt_dir = peer_dir / "log" / "prompts"
    matches = list(prompt_dir.glob("tick-00001-*.txt"))
    assert len(matches) == 1, list(prompt_dir.iterdir())
    text = matches[0].read_text()
    # The builder always includes the peer name + goal status in the
    # prompt; assert one stable substring.
    assert "claude" in text or "codex" in text
    assert len(text) > 0


# --- Wave-2 §5.2: per-tick gates snapshot in runs.jsonl --------------


def test_runs_jsonl_records_hard_gate_snapshot(fresh_repo, _restore_env):
    """Happy path: a tick records the per-gate hard verdicts in a compact
    ``gates`` map sourced from the in-memory ``goals_status``."""
    target, peer_dir = fresh_repo
    drv = _build_driver(target, peer_dir)  # one never-passing hard gate
    drv.run(max_ticks=1)
    ticks = [e for e in _read_runs_jsonl(peer_dir) if e.get("event") != "exit"]
    assert len(ticks) == 1, ticks
    gates = ticks[0].get("gates")
    assert isinstance(gates, dict), ticks[0]
    # Hard gates are a compact {gate_id: state} map under "hard".
    assert gates.get("hard", {}).get("never") == "fail", gates


def test_runs_jsonl_records_soft_consensus_snapshot(fresh_repo, _restore_env):
    """Happy path: a soft goal with in-memory consensus status is recorded as
    a compact ``<count>/<needed>`` string under ``gates['soft']``, sourced from
    the driver's ``soft_status`` + the goal's ``consensus_needed``."""
    from peers.goals import Goal
    from peers.health_guard import RunResult

    target, peer_dir = fresh_repo
    goals = [
        Goal(id="never", type="hard", cmd="false", pass_when="exit_code == 0"),
        Goal(id="review", type="soft", reviewer="other",
             prompt="review it", consensus_needed=2),
    ]
    drv = _build_driver(target, peer_dir, goals=goals)
    state = {
        "iteration": 4,
        "peers": {"claude": {"last_run": {}, "state": "healthy"}},
        "budget": {},
        # In-memory results the driver has at runs.jsonl-write time.
        "goals_status": {"never": {"state": "fail", "duration_ms": 1}},
        "soft_status": {"review": {"consensus_count": 1}},
    }
    run = RunResult(
        classification="success", exit_code=0, duration_ms=5,
        stdout="", stderr="",
    )
    drv._append_run_log(state, "claude", run, success=True)
    ticks = [e for e in _read_runs_jsonl(peer_dir) if e.get("event") != "exit"]
    assert len(ticks) == 1, ticks
    gates = ticks[0].get("gates")
    assert isinstance(gates, dict), ticks[0]
    assert gates.get("hard", {}).get("never") == "fail", gates
    soft = gates.get("soft", {})
    # 1 of 2 consensus reviews so far -> "1/2" (count/needed from the goal).
    assert soft.get("review") == "1/2", gates


def test_exit_line_has_no_gates_field(fresh_repo, _restore_env):
    """The synthetic ``{"event":"exit"}`` line carries no ``gates`` field."""
    target, peer_dir = fresh_repo
    drv = _build_driver(target, peer_dir)
    drv.run(max_ticks=1)
    exits = [e for e in _read_runs_jsonl(peer_dir) if e.get("event") == "exit"]
    assert len(exits) == 1, _read_runs_jsonl(peer_dir)
    assert "gates" not in exits[0], exits[0]


def test_gates_snapshot_omitted_when_source_garbage(fresh_repo, _restore_env):
    """Sad path: a garbage ``goals_status`` must not raise into the tick —
    the entry is still written, just without the ``gates`` field."""
    target, peer_dir = fresh_repo
    drv = _build_driver(target, peer_dir)
    state = {
        "iteration": 7,
        "peers": {"claude": {"last_run": {}, "state": "healthy"}},
        "budget": {},
        "goals_status": "not-a-dict",   # garbage
        "soft_status": 12345,           # garbage
    }
    from peers.health_guard import RunResult
    run = RunResult(
        classification="success", exit_code=0, duration_ms=5,
        stdout="", stderr="",
    )
    # Must not raise.
    drv._append_run_log(state, "claude", run, success=True)
    ticks = [e for e in _read_runs_jsonl(peer_dir) if e.get("event") != "exit"]
    assert len(ticks) == 1, ticks
    # Field omitted on a garbage source (fail-closed), tick still logged.
    assert "gates" not in ticks[0], ticks[0]
    assert ticks[0]["iteration"] == 7


def test_gates_snapshot_bounded_when_many_gates(fresh_repo, _restore_env):
    """Edge: a project with an absurd number of gates must not bloat the
    line unboundedly — the snapshot caps the number of entries."""
    from peers.driver_observability import _GATES_SNAPSHOT_MAX_ENTRIES
    from peers.health_guard import RunResult

    target, peer_dir = fresh_repo
    drv = _build_driver(target, peer_dir)
    n = _GATES_SNAPSHOT_MAX_ENTRIES + 50
    state = {
        "iteration": 3,
        "peers": {"claude": {"last_run": {}, "state": "healthy"}},
        "budget": {},
        "goals_status": {
            f"g{i}": {"state": "pass", "duration_ms": 1} for i in range(n)
        },
        "soft_status": {},
    }
    run = RunResult(
        classification="success", exit_code=0, duration_ms=5,
        stdout="", stderr="",
    )
    drv._append_run_log(state, "claude", run, success=True)
    ticks = [e for e in _read_runs_jsonl(peer_dir) if e.get("event") != "exit"]
    gates = ticks[0]["gates"]
    total = len(gates.get("hard", {})) + len(gates.get("soft", {}))
    assert total <= _GATES_SNAPSHOT_MAX_ENTRIES, total
    # Substrate-side invariant: a truncated snapshot is FLAGGED with
    # "_truncated": True so a consumer can tell the row is incomplete.
    assert gates.get("_truncated") is True, gates


def test_gates_snapshot_clamps_huge_consensus(fresh_repo, _restore_env):
    """Edge / defense-in-depth: a corrupt in-memory ``consensus_count`` (e.g.
    ``10**100``) must NOT render an unbounded ``n/m`` string. The magnitude is
    clamped so the numerator stays <= 10 digits; no raise, the tick is logged."""
    from peers.goals import Goal
    from peers.health_guard import RunResult

    target, peer_dir = fresh_repo
    goals = [
        Goal(id="never", type="hard", cmd="false", pass_when="exit_code == 0"),
        Goal(id="review", type="soft", reviewer="other",
             prompt="review it", consensus_needed=2),
    ]
    drv = _build_driver(target, peer_dir, goals=goals)
    state = {
        "iteration": 9,
        "peers": {"claude": {"last_run": {}, "state": "healthy"}},
        "budget": {},
        "goals_status": {"never": {"state": "fail", "duration_ms": 1}},
        # A corrupt/hostile consensus_count: an absurdly large integer.
        "soft_status": {"review": {"consensus_count": 10**100}},
    }
    run = RunResult(
        classification="success", exit_code=0, duration_ms=5,
        stdout="", stderr="",
    )
    # Must not raise.
    drv._append_run_log(state, "claude", run, success=True)
    ticks = [e for e in _read_runs_jsonl(peer_dir) if e.get("event") != "exit"]
    assert len(ticks) == 1, ticks
    assert ticks[0]["iteration"] == 9
    soft = ticks[0]["gates"]["soft"]
    cons = soft["review"]
    assert isinstance(cons, str) and "/" in cons, cons
    numerator = cons.split("/", 1)[0]
    # Clamped to <= 10**9 -> at most 10 decimal digits, never the 101-digit
    # rendering of the raw 10**100 value.
    assert len(numerator) <= 10, cons
    assert int(numerator) == 10**9, cons


def test_old_runs_jsonl_line_without_gates_still_parses(fresh_repo):
    """Edge / backward-compat: an old runs.jsonl line WITHOUT the ``gates``
    field still parses via the TUI reader (the new field is additive)."""
    from peers_ctl.tui import reader

    target, peer_dir = fresh_repo
    log = peer_dir / "log"
    runs = log / "runs.jsonl"
    # An old (pre-Wave-2) tick line: no `gates` key at all.
    old_line = json.dumps({
        "ts": "2026-06-11T00:00:00+00:00", "iteration": 1, "peer": "claude",
        "tool": "claude", "classification": "success", "exit_code": 0,
        "duration_ms": 10, "success": True,
    })
    runs.write_text(old_line + "\n")
    entries = reader.tick_entries(runs)
    assert len(entries) == 1, entries
    assert entries[0].iteration == 1
    assert entries[0].is_exit is False


# --- rotation: .log AND .stream.jsonl tee files -------------------


def _rotation_harness(peer_dir: Path):
    """A minimal object exposing the rotation mixin against ``peer_dir``.

    Avoids spinning up a whole driver — ``_maybe_rotate_peer_logs`` only needs
    ``self.peer_dir`` + a no-op ``_verify_peer_dir_identity`` (which the rotation
    path doesn't call) and the ``log/peers`` subdir.
    """
    from peers.driver_observability import DriverObservabilityMixin

    class _Harness(DriverObservabilityMixin):
        def __init__(self, pd: Path) -> None:
            self.peer_dir = pd

    (peer_dir / "log" / "peers").mkdir(parents=True, exist_ok=True)
    return _Harness(peer_dir)


def _peers_log_dir(peer_dir: Path) -> Path:
    return peer_dir / "log" / "peers"


def test_rotation_gzips_oldest_log_file_over_threshold(fresh_repo):
    """Happy path (unchanged behavior): once the ``.log`` group exceeds the
    threshold, the oldest ``.log`` is gzipped and the raw file removed."""
    from peers.driver_observability import DriverObservabilityMixin

    target, peer_dir = fresh_repo
    h = _rotation_harness(peer_dir)
    log_dir = _peers_log_dir(peer_dir)
    n = DriverObservabilityMixin._PEER_LOG_ROTATE_THRESHOLD + 1
    for i in range(n):
        (log_dir / f"tick-{i:05d}-claude.stdout.log").write_text(f"log {i}\n")

    h._maybe_rotate_peer_logs()

    oldest_raw = log_dir / "tick-00000-claude.stdout.log"
    oldest_gz = log_dir / "tick-00000-claude.stdout.log.gz"
    assert not oldest_raw.exists(), "oldest raw .log should be reclaimed"
    assert oldest_gz.exists(), "oldest .log should be gzipped"
    # raw .log count is now back at the threshold.
    raw_logs = list(log_dir.glob("tick-*-claude.stdout.log"))
    assert len(raw_logs) == DriverObservabilityMixin._PEER_LOG_ROTATE_THRESHOLD


def test_rotation_gzips_oldest_tee_stream_file_over_threshold(fresh_repo):
    """Fix-1 (MAJOR): the Wave-2 tee ``.stream.jsonl`` files end in ``.jsonl``,
    NOT ``.log`` — they must rotate under the SAME threshold so a long run with
    the tee enabled can't grow the directory without bound."""
    from peers.driver_observability import DriverObservabilityMixin

    target, peer_dir = fresh_repo
    h = _rotation_harness(peer_dir)
    log_dir = _peers_log_dir(peer_dir)
    n = DriverObservabilityMixin._PEER_LOG_ROTATE_THRESHOLD + 1
    for i in range(n):
        (log_dir / f"tick-{i:05d}-claude.stream.jsonl").write_text(
            f'{{"i": {i}}}\n')

    h._maybe_rotate_peer_logs()

    oldest_raw = log_dir / "tick-00000-claude.stream.jsonl"
    oldest_gz = log_dir / "tick-00000-claude.stream.jsonl.gz"
    assert not oldest_raw.exists(), "oldest raw .stream.jsonl should be reclaimed"
    assert oldest_gz.exists(), "oldest tee stream should be gzipped"
    raw_tee = list(log_dir.glob("tick-*-claude.stream.jsonl"))
    assert len(raw_tee) == DriverObservabilityMixin._PEER_LOG_ROTATE_THRESHOLD


def test_rotation_gzips_oldest_tee_stderr_stream_file_over_threshold(fresh_repo):
    """Edge: the stderr tee variant ``.stream.err.jsonl`` is also covered."""
    from peers.driver_observability import DriverObservabilityMixin

    target, peer_dir = fresh_repo
    h = _rotation_harness(peer_dir)
    log_dir = _peers_log_dir(peer_dir)
    n = DriverObservabilityMixin._PEER_LOG_ROTATE_THRESHOLD + 1
    for i in range(n):
        (log_dir / f"tick-{i:05d}-claude.stream.err.jsonl").write_text(
            f'{{"e": {i}}}\n')

    h._maybe_rotate_peer_logs()

    oldest_gz = log_dir / "tick-00000-claude.stream.err.jsonl.gz"
    assert not (log_dir / "tick-00000-claude.stream.err.jsonl").exists()
    assert oldest_gz.exists(), "oldest stderr tee stream should be gzipped"


def test_rotation_groups_are_independent_below_threshold(fresh_repo):
    """Sad/edge: with each group AT (not over) the threshold, neither rotates —
    the two groups are counted independently, not pooled (which would wrongly
    trip rotation early)."""
    from peers.driver_observability import DriverObservabilityMixin

    target, peer_dir = fresh_repo
    h = _rotation_harness(peer_dir)
    log_dir = _peers_log_dir(peer_dir)
    n = DriverObservabilityMixin._PEER_LOG_ROTATE_THRESHOLD
    for i in range(n):
        (log_dir / f"tick-{i:05d}-claude.stdout.log").write_text("x\n")
        (log_dir / f"tick-{i:05d}-claude.stream.jsonl").write_text("y\n")

    h._maybe_rotate_peer_logs()

    # neither group over threshold -> nothing gzipped, nothing reclaimed.
    assert list(log_dir.glob("*.gz")) == []
    assert len(list(log_dir.glob("tick-*-claude.stdout.log"))) == n
    assert len(list(log_dir.glob("tick-*-claude.stream.jsonl"))) == n


# --- --verbose flag ---------------------------------------------


def test_verbose_flag_echoes_peer_stdout_to_stderr(
    fresh_repo, capfd, _restore_env,
):
    """`verbose=True` prints the `=== peer=...` header and
    `[peer-stdout]`-prefixed lines on substrate stderr.
    """
    target, peer_dir = fresh_repo
    os.environ["FAKE_PEER_STDOUT"] = "verbose hello\n"
    drv = _build_driver(target, peer_dir, verbose=True)
    drv.run(max_ticks=1)
    err = capfd.readouterr().err
    assert "=== peer=" in err
    assert "[peer-stdout] verbose hello" in err


def test_verbose_flag_off_no_echo_in_stderr(
    fresh_repo, capfd, _restore_env,
):
    """`verbose=False` (default): no `[peer-stdout]` prefix appears.
    Tick markers from are fine.
    """
    target, peer_dir = fresh_repo
    os.environ["FAKE_PEER_STDOUT"] = "should not appear\n"
    drv = _build_driver(target, peer_dir, verbose=False)
    drv.run(max_ticks=1)
    err = capfd.readouterr().err
    assert "[peer-stdout]" not in err
    # Sanity: tick markers should still be there.
    assert "peers: tick 1" in err
