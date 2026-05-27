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


def _build_driver(target: Path, peer_dir: Path, *, verbose: bool = False):
    """Build a driver wired to the chatty fake peer + a never-passing
    hard goal so the loop actually runs the tick.
    """
    from peers.driver_orchestrator import OrchestratorDriver
    from peers.peer_spec import PeerSpec
    from peers.goals import Goal

    fake = [sys.executable, str(CHATTY_PEER)]
    never_pass = Goal(
        id="never", type="hard",
        cmd="false", pass_when="exit_code == 0",
    )
    return OrchestratorDriver(
        repo=target, peer_dir=peer_dir,
        goals=[never_pass],
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
