"""Tier-2 hang_kill REAL-PROCESS fault injection (no liveness mocks).

The companion test_health_guard_hang_kill.py validates the composite-AND kill
decision with `proc_state_alive` / `socket_active` monkeypatched. That proves the
*decision logic* but never exercises the live `/proc` reader against the real
kernel. This module closes that gap: it spawns real child processes in real
kernel states and drives the *unmocked* composite through HealthGuard.invoke,
validating exactly the three scenarios the design must get right before
`hang_kill_s` is ever shipped on:

  (a) a genuine hang  (futex deadlock, non-I/O blocking)  -> MUST kill fast
  (b) a thinking peer (blocked in read/recv I/O-wait)     -> MUST NOT kill
  (c) a busy peer     (on-CPU spin, stat state R)          -> MUST NOT kill
  (d) rate-limit backoff (proc-state genuinely dead, but   -> MUST NOT kill
      session jsonl kept fresh)

Only `claude_session_jsonl_path` is redirected (the test owns where the session
jsonl lives); the liveness signals themselves read real `/proc`. Requires a
Linux host that exposes /proc/<pid>/task/<tid>/syscall (skipped otherwise).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from peers.health_guard import HealthGuard
from peers.liveness import proc_state_alive

# Self-check: this host must expose /proc/<pid>/syscall for the real signal to
# mean anything. If proc_state_alive can't read our own pid, skip the module.
import os as _os  # noqa: E402

_PROC_OK = proc_state_alive(_os.getpid()) is not None
pytestmark = pytest.mark.skipif(
    not _PROC_OK, reason="host /proc does not expose per-thread syscall state")


def _py(code: str) -> list[str]:
    return ["python3", "-c", code]


# A python child that deadlocks in futex: acquire a lock twice. Main thread
# blocks in syscall 202 (futex) forever -> non-I/O blocking -> proc_state False.
_FUTEX_DEADLOCK = _py(
    "import threading; l = threading.Lock(); l.acquire(); l.acquire()")

# A python child whose main thread blocks in os.read (syscall 0, I/O-wait) for
# ~3s, then a sidecar thread unblocks it and it exits 0.
_BLOCKED_READ = _py(
    "import os, threading, time\n"
    "r, w = os.pipe()\n"
    "threading.Thread(target=lambda: (time.sleep(3), os.write(w, b'x')),\n"
    "                 daemon=True).start()\n"
    "os.read(r, 1)\n")

# A python child that busy-spins on-CPU (stat state R) for ~3s then exits 0.
_CPU_SPIN = _py(
    "import time\n"
    "end = time.monotonic() + 3\n"
    "while time.monotonic() < end:\n"
    "    pass\n")


def test_real_futex_deadlock_is_hang_killed(tmp_path: Path, monkeypatch) -> None:
    # (a) genuine hang: stdout silent, no jsonl, no API socket, and the real
    # proc-state signal is False (futex). The composite MUST fire near
    # hang_kill_s, well before the 100s idle/absolute caps.
    monkeypatch.setenv("HOME", str(tmp_path))  # no fresh claude jsonl anywhere
    t0 = time.monotonic()
    r = HealthGuard(tmp_path).invoke(
        _FUTEX_DEADLOCK, prompt="",
        idle_timeout_s=100, absolute_max_runtime_s=100, hang_kill_s=1,
    )
    elapsed = time.monotonic() - t0
    assert r.classification == "idle-timeout"
    assert elapsed < 20, f"hang-kill should fire near hang_kill_s, took {elapsed:.1f}s"


def test_real_blocked_read_keeps_peer_alive(tmp_path: Path, monkeypatch) -> None:
    # (b) thinking peer: stdout/jsonl/socket silent, but the main thread is
    # blocked in read() (real I/O-wait) -> proc_state_alive True keeps it alive
    # past hang_kill_s; the child exits 0 on its own -> success, not killed.
    monkeypatch.setenv("HOME", str(tmp_path))
    r = HealthGuard(tmp_path).invoke(
        _BLOCKED_READ, prompt="",
        idle_timeout_s=100, absolute_max_runtime_s=100, hang_kill_s=1,
    )
    assert r.classification == "success"


def test_real_cpu_spin_keeps_peer_alive(tmp_path: Path, monkeypatch) -> None:
    # (c) busy peer: silent on stdout but on-CPU (stat R) -> proc_state_alive
    # True -> not a hang. Child finishes its ~3s spin and exits 0.
    monkeypatch.setenv("HOME", str(tmp_path))
    r = HealthGuard(tmp_path).invoke(
        _CPU_SPIN, prompt="",
        idle_timeout_s=100, absolute_max_runtime_s=100, hang_kill_s=1,
    )
    assert r.classification == "success"


def test_real_sleeper_kept_alive_by_jsonl_when_proc_state_dead(
    tmp_path: Path, monkeypatch,
) -> None:
    # (d) rate-limit backoff: the child is a pure sleeper, so the REAL
    # proc_state signal is False (nanosleep is not I/O-wait) and there is no API
    # socket — yet the CLI keeps the session jsonl fresh while it retries. The
    # jsonl signal alone must keep the peer alive (no kill). Proves jsonl
    # keep-alive is independent of proc-state.
    monkeypatch.setenv("HOME", str(tmp_path))
    jsonl_dir = tmp_path / "jsonl"
    jsonl_dir.mkdir()
    fresh = jsonl_dir / "session.jsonl"
    fresh.write_text("{}\n")
    monkeypatch.setattr(
        "peers.health_guard.claude_session_jsonl_path", lambda _cwd: jsonl_dir)

    stop = threading.Event()

    def keep_fresh() -> None:
        # touch the jsonl every 0.4s so jsonl_mtime_within(hang_kill_s) stays True
        while not stop.is_set():
            fresh.touch()
            time.sleep(0.4)

    toucher = threading.Thread(target=keep_fresh, daemon=True)
    toucher.start()
    try:
        r = HealthGuard(tmp_path).invoke(
            ["bash", "-lc", "sleep 3"], prompt="",
            idle_timeout_s=100, absolute_max_runtime_s=100, hang_kill_s=1,
        )
    finally:
        stop.set()
        toucher.join(timeout=2)
    assert r.classification == "success"
