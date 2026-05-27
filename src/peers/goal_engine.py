"""Evaluates hard gates by running shell commands and checking pass_when."""
from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from peers.goals import Goal, evaluate_pass_when

_GOAL_OUTPUT_CAP_BYTES = 2 * 1024 * 1024
_GOAL_TERM_GRACE_S = 1.0


def _append_capped(
    chunks: list[bytes], size: int, chunk: bytes,
) -> tuple[int, bool]:
    remaining = max(0, _GOAL_OUTPUT_CAP_BYTES - size)
    if remaining:
        chunks.append(chunk[:remaining])
    return size + min(len(chunk), remaining), len(chunk) > remaining


def _decode_capped(chunks: list[bytes], truncated: bool) -> str:
    text = b"".join(chunks).decode("utf-8", errors="replace")
    if truncated:
        text += (
            f"\n... <goal output truncated at "
            f"{_GOAL_OUTPUT_CAP_BYTES} bytes> ...\n"
        )
    return text


def _goal_process_group(proc: subprocess.Popen) -> int | None:
    try:
        return os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        return None


def _signal_goal_tree(
    proc: subprocess.Popen,
    sig: int,
    pgid: int | None = None,
) -> None:
    try:
        os.killpg(pgid if pgid is not None else os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError):
        pass


def _close_selector_streams(sel: selectors.BaseSelector) -> None:
    for key in list(sel.get_map().values()):
        stream = key.fileobj
        try:
            sel.unregister(stream)
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass


def _run_goal_cmd(cmd: str, cwd: Path, timeout_s: int
                  ) -> subprocess.CompletedProcess[str]:
    """Run a shell goal cmd in its OWN process group so we can kill the
    whole tree on timeout. Replaces subprocess.run(shell=True,timeout=N)
    which only kills the immediate child.

    Mimics subprocess.run's CompletedProcess return shape.
    """
    proc = subprocess.Popen(
        ["/bin/sh", "-c", cmd], cwd=str(cwd),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    pgid = _goal_process_group(proc)
    assert proc.stdout is not None
    assert proc.stderr is not None
    sel = selectors.DefaultSelector()
    # BUG-001 (2026-05-24): the selector and the two pipe FDs must be
    # released on EVERY exit path — normal completion, timeout, *or*
    # mid-loop exception. The substrate runs many goal evaluations per
    # tick; without the finally, the epoll FD and pipe FDs accumulate
    # toward NPROC/NOFILE.
    try:
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        stdout_size = 0
        stderr_size = 0
        stdout_truncated = False
        stderr_truncated = False
        for stream, name in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            sel.register(stream, selectors.EVENT_READ, name)

        deadline = time.monotonic() + timeout_s
        term_deadline: float | None = None
        timed_out = False
        while sel.get_map() or proc.poll() is None:
            now = time.monotonic()
            if not timed_out and now >= deadline:
                timed_out = True
                term_deadline = now + _GOAL_TERM_GRACE_S
                _signal_goal_tree(proc, signal.SIGTERM, pgid)
            if timed_out and term_deadline is not None and now >= term_deadline:
                _signal_goal_tree(proc, signal.SIGKILL, pgid)
                break

            if sel.get_map():
                events = sel.select(timeout=0.05)
            else:
                time.sleep(0.05)
                continue
            for key, _mask in events:
                stream = key.fileobj
                try:
                    chunk = os.read(stream.fileno(), 8192)
                except BlockingIOError:
                    continue
                if not chunk:
                    sel.unregister(stream)
                    continue
                if key.data == "stdout":
                    stdout_size, did_truncate = _append_capped(
                        stdout_chunks, stdout_size, chunk
                    )
                    stdout_truncated = stdout_truncated or did_truncate
                else:
                    stderr_size, did_truncate = _append_capped(
                        stderr_chunks, stderr_size, chunk
                    )
                    stderr_truncated = stderr_truncated or did_truncate
        if timed_out:
            try:
                rc = proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                _signal_goal_tree(proc, signal.SIGKILL, pgid)
                rc = proc.returncode if proc.returncode is not None else -signal.SIGKILL
        else:
            rc = proc.wait()
        stdout = _decode_capped(stdout_chunks, stdout_truncated)
        stderr = _decode_capped(stderr_chunks, stderr_truncated)
        if timed_out:
            raise subprocess.TimeoutExpired(
                cmd, timeout_s, output=stdout, stderr=stderr,
            )
        return subprocess.CompletedProcess(
            args=cmd, returncode=rc,
            stdout=stdout, stderr=stderr,
        )
    finally:
        _close_selector_streams(sel)
        sel.close()
        # Popen.__del__ eventually closes these, but only when the
        # object is GC'd; explicit close keeps the FD count flat per
        # invocation rather than relying on the cycle collector.
        for s in (proc.stdout, proc.stderr):
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass


@dataclass
class GoalResult:
    goal_id: str
    state: str  # "pass" | "fail"
    duration_ms: int
    diagnostic: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


class GoalEngine:
    def __init__(
        self,
        goals: list[Goal],
        cwd: Path,
        timeout_s: int = 120,
    ) -> None:
        self.goals = goals
        self.cwd = Path(cwd)
        self.timeout_s = timeout_s
        self._last: dict[str, GoalResult] = {}

    def evaluate_hard_gates(self) -> dict[str, GoalResult]:
        results: dict[str, GoalResult] = {}
        for g in self.goals:
            if g.type != "hard":
                continue
            results[g.id] = self._run_hard(g)
        self._last = results
        return results

    def _run_hard(self, g: Goal) -> GoalResult:
        assert g.cmd is not None, f"hard goal {g.id} has no cmd"
        assert g.pass_when is not None, f"hard goal {g.id} has no pass_when"
        t0 = time.monotonic()
        try:
            proc = _run_goal_cmd(g.cmd, self.cwd, self.timeout_s)
        except subprocess.TimeoutExpired as e:
            dur = int((time.monotonic() - t0) * 1000)
            tail_out = (e.stdout or "")[-1000:] if isinstance(e.stdout, str) else ""
            tail_err = (e.stderr or "")[-1000:] if isinstance(e.stderr, str) else ""
            diag = "timeout"
            if tail_out or tail_err:
                diag += f"; stdout-tail={tail_out!r}; stderr-tail={tail_err!r}"
            return GoalResult(g.id, "fail", dur, diagnostic=diag)
        dur = int((time.monotonic() - t0) * 1000)
        ctx = {
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "cwd": self.cwd,
        }
        try:
            passed = evaluate_pass_when(g.pass_when, ctx)
        except Exception as e:
            return GoalResult(
                g.id, "fail", dur,
                diagnostic=f"pass_when error: {e}",
            )
        return GoalResult(
            g.id,
            "pass" if passed else "fail",
            dur,
            diagnostic="" if passed else "pass_when returned False",
        )

    def all_green(self) -> bool:
        """All HARD gates pass. A configuration with zero hard gates
        is trivially green (the caller still has to verify soft-goal
        consensus separately). Returns False before the first
        evaluate_hard_gates() call when there ARE hard gates."""
        # Differentiate "no hard goals declared" from "hard goals declared
        # but not yet evaluated": the former is green, the latter is not.
        has_hard = any(g.type == "hard" for g in self.goals)
        if not has_hard:
            return True
        if not self._last:
            return False
        return all(r.state == "pass" for r in self._last.values())
