"""Tier-2 C3/C4: faster composite-AND hang kill + hung-tool diagnostic.

When enabled (``hang_kill_s`` set, shorter than ``idle_timeout_s``) the guard
kills a genuine hang sooner than the coarse idle timeout — but ONLY when every
liveness signal is silent: stdout cadence, session-jsonl mtime, AND an active
API socket. Any one live signal keeps the peer alive (so rate-limit backoff and
slow thinking are never mistaken for a hang). Default OFF (hang_kill_s=None):
behaviour is byte-for-byte the legacy idle/absolute timeout path.
"""
from __future__ import annotations

import time
from pathlib import Path

from peers.health_guard import HealthGuard


def _sleeper(seconds: float) -> list[str]:
    # A child that is silent on stdout/stderr for `seconds`, then exits 0.
    return ["bash", "-lc", f"sleep {seconds}"]


def test_hang_kill_disabled_by_default_no_fast_kill(tmp_path: Path) -> None:
    # hang_kill_s defaults to None -> no fast kill. A 2s-silent child with a
    # generous idle/absolute timeout runs to completion (exit 0 -> success).
    r = HealthGuard(tmp_path).invoke(
        _sleeper(2), prompt="", idle_timeout_s=100, absolute_max_runtime_s=100,
    )
    assert r.classification == "success"


def test_hang_kill_fires_when_all_signals_silent(
    tmp_path: Path, monkeypatch,
) -> None:
    # All signals silent: no jsonl, socket + proc-state unavailable -> the
    # composite hang-kill fires at ~hang_kill_s, before the 100s idle/abs caps.
    monkeypatch.setattr("peers.health_guard.socket_active", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "peers.health_guard.proc_state_alive", lambda *_a, **_k: None)
    # ensure no claude session jsonl looks fresh
    monkeypatch.setenv("HOME", str(tmp_path))
    t0 = time.monotonic()
    r = HealthGuard(tmp_path).invoke(
        _sleeper(30), prompt="",
        idle_timeout_s=100, absolute_max_runtime_s=100, hang_kill_s=1,
    )
    elapsed = time.monotonic() - t0
    assert r.classification == "idle-timeout"
    assert elapsed < 15  # fired by hang_kill_s, not the 100s idle cap


def test_hang_kill_kept_alive_by_fresh_jsonl(
    tmp_path: Path, monkeypatch,
) -> None:
    # stdout silent past hang_kill_s, but the session jsonl is fresh -> NOT a
    # hang (this models rate-limit backoff: the CLI keeps writing jsonl). The
    # child completes normally.
    monkeypatch.setattr("peers.health_guard.socket_active", lambda *_a, **_k: None)
    jsonl_dir = tmp_path / "jsonl"
    jsonl_dir.mkdir()
    fresh = jsonl_dir / "session.jsonl"
    fresh.write_text("{}\n")
    monkeypatch.setattr(
        "peers.health_guard.claude_session_jsonl_path", lambda _cwd: jsonl_dir,
    )
    # Model rate-limit backoff: the CLI keeps writing the session jsonl while it
    # retries (stdout stays silent). The child touches the jsonl every 0.5s for
    # ~3s, so jsonl_mtime_within(hang_kill_s) is continuously True -> no kill.
    child = ["bash", "-lc",
             f"for i in $(seq 1 6); do touch {fresh}; sleep 0.5; done"]
    r = HealthGuard(tmp_path).invoke(
        child, prompt="",
        idle_timeout_s=100, absolute_max_runtime_s=100, hang_kill_s=1,
    )
    assert r.classification == "success"


def test_hang_kill_kept_alive_by_active_socket(
    tmp_path: Path, monkeypatch,
) -> None:
    # stdout + jsonl silent, but the API socket is actively transferring ->
    # keep-alive, no hang-kill.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("peers.health_guard.socket_active", lambda *_a, **_k: True)
    r = HealthGuard(tmp_path).invoke(
        _sleeper(3), prompt="",
        idle_timeout_s=100, absolute_max_runtime_s=100, hang_kill_s=1,
    )
    assert r.classification == "success"


def test_hang_kill_kept_alive_by_proc_state(
    tmp_path: Path, monkeypatch,
) -> None:
    # The awaiting-response blind spot: stdout + jsonl + socket all silent, but
    # the peer's thread is blocked in a recv/epoll syscall (proc_state_alive
    # True) -> a model thinking before its first token must NOT be hang-killed.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("peers.health_guard.socket_active", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "peers.health_guard.proc_state_alive", lambda *_a, **_k: True)
    r = HealthGuard(tmp_path).invoke(
        _sleeper(3), prompt="",
        idle_timeout_s=100, absolute_max_runtime_s=100, hang_kill_s=1,
    )
    assert r.classification == "success"


# --- C4: hung-tool diagnostic ---------------------------------------------

def test_hung_tool_diagnostic_flags_unmatched_tool_use() -> None:
    from peers.health_guard import hung_tool_diagnostic
    tail = (
        '{"type":"assistant"}\n'
        '{"type":"tool_use","name":"Bash","id":"t1"}\n'
    )
    diag = hung_tool_diagnostic(tail)
    assert diag is not None
    assert "Bash" in diag


def test_hung_tool_diagnostic_none_when_tool_result_present() -> None:
    from peers.health_guard import hung_tool_diagnostic
    tail = (
        '{"type":"tool_use","name":"Bash","id":"t1"}\n'
        '{"type":"tool_result","tool_use_id":"t1"}\n'
    )
    assert hung_tool_diagnostic(tail) is None


def test_hung_tool_diagnostic_none_when_no_tools() -> None:
    from peers.health_guard import hung_tool_diagnostic
    assert hung_tool_diagnostic('{"type":"assistant"}\n') is None
