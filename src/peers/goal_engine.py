"""Evaluates hard gates by running shell commands and checking pass_when."""
from __future__ import annotations

import os
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from peers.goals import Goal, evaluate_pass_when

_GOAL_OUTPUT_CAP_BYTES = 2 * 1024 * 1024
_GOAL_TERM_GRACE_S = 1.0
_GOAL_DIAGNOSTIC_TAIL_CHARS = 1000


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
        # fileobj is typed int | HasFileno; we only ever register IO streams,
        # which carry .close(). A raw-int fileobj (no .close) is skipped.
        close = getattr(stream, "close", None)
        if callable(close):
            try:
                close()
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
                fobj = key.fileobj
                if isinstance(fobj, int):
                    continue  # we only register IO streams, never raw fds
                try:
                    chunk = os.read(fobj.fileno(), 8192)
                except BlockingIOError:
                    continue
                if not chunk:
                    sel.unregister(fobj)
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


def _failure_diagnostic(
    reason: str,
    proc: subprocess.CompletedProcess[str],
) -> str:
    parts = [reason, f"exit_code={proc.returncode}"]
    if proc.stdout:
        parts.append(
            f"stdout-tail={proc.stdout[-_GOAL_DIAGNOSTIC_TAIL_CHARS:]!r}"
        )
    if proc.stderr:
        parts.append(
            f"stderr-tail={proc.stderr[-_GOAL_DIAGNOSTIC_TAIL_CHARS:]!r}"
        )
    return "; ".join(parts)


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
        # Tier-1 memoization: goal_id -> (working-tree + HEAD key, last GoalResult).
        # Only populated for goals marked `cacheable`.
        self._cache: dict[str, tuple[str, GoalResult]] = {}

    def expensive_ids(self) -> set[str]:
        """Hard gates marked `expensive` (pytest/coverage-backed) — eligible to
        run async on a frozen SHA while the next peer thinks (Tier-1 Part B)."""
        return {g.id for g in self.goals
                if g.type == "hard" and getattr(g, "expensive", False)}

    def cheap_ids(self) -> set[str]:
        """Hard gates that are NOT expensive — run synchronously each tick so
        the peer always sees fresh non-pytest gate status."""
        return {g.id for g in self.goals
                if g.type == "hard" and not getattr(g, "expensive", False)}

    def _tree_key(self) -> str | None:
        """Content hash of the ENTIRE working tree (tracked + untracked +
        uncommitted), via `git add -A` into a throwaway index + `write-tree`.

        Two evaluations with the same key see a byte-identical tree, so a
        tree-pure gate's verdict is provably identical. Returns ``None`` on
        any error (not a git repo, git missing, timeout) — callers MUST treat
        ``None`` as "uncacheable, run the gate" (fail-safe). Note: `add -A`
        honors .gitignore, which is correct — ignored paths (caches, .peers/
        runtime state) don't determine a code-pure gate's verdict, and the
        gates that DO depend on runtime state are not marked cacheable.
        """
        idx_dir: Path | None = None
        try:
            idx_dir = Path(tempfile.mkdtemp(prefix="peers-gatekey-"))
            idx = str(idx_dir / "index")
            env = {**os.environ, "GIT_INDEX_FILE": idx}
            add = subprocess.run(
                ["git", "-C", str(self.cwd), "add", "-A"],
                env=env, capture_output=True, timeout=60, check=False,
            )
            if add.returncode != 0:
                return None
            wt = subprocess.run(
                ["git", "-C", str(self.cwd), "write-tree"],
                env=env, capture_output=True, text=True, timeout=60, check=False,
            )
            if wt.returncode != 0:
                return None
            key = (wt.stdout or "").strip()
            return key or None
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None
        finally:
            if idx_dir is not None:
                shutil.rmtree(idx_dir, ignore_errors=True)

    def _head_key(self) -> str | None:
        """Current commit identity for cache invalidation.

        Some gates are accidentally or necessarily history-sensitive (for
        example checks that parse `git log` trailers). Folding HEAD into the
        cache key prevents an empty commit from reusing a cached PASS for a
        byte-identical tree. If HEAD cannot be resolved, disable caching for
        that evaluation rather than guessing.
        """
        try:
            head = subprocess.run(
                ["git", "-C", str(self.cwd), "rev-parse", "--verify", "HEAD"],
                capture_output=True, text=True, timeout=60, check=False,
            )
            if head.returncode != 0:
                return None
            key = (head.stdout or "").strip()
            return key or None
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None

    def _cache_key(self) -> str | None:
        tree_key = self._tree_key()
        if tree_key is None:
            return None
        head_key = self._head_key()
        if head_key is None:
            return None
        return f"{tree_key}:{head_key}"

    def _run_hard_retrying(self, g: Goal) -> GoalResult:
        """Run a hard gate, re-running up to ``g.retry_on_fail`` more times if
        it fails (returning the first PASS). Absorbs a transient flake before
        it can turn the gate red and trip a `stuck:<gate>` halt; a genuine
        failure still fails after the retries."""
        res = self._run_hard(g)
        attempts = getattr(g, "retry_on_fail", 0) or 0
        while res.state == "fail" and attempts > 0:
            res = self._run_hard(g)
            attempts -= 1
        return res

    def evaluate_hard_gates(
        self,
        goal_ids: Iterable[str] | None = None,
    ) -> dict[str, GoalResult]:
        selected = set(goal_ids) if goal_ids is not None else None
        results: dict[str, GoalResult] = {}
        # Compute the cache key at most once per call, and only if some
        # selected gate is actually cacheable (the git add -A costs ~sub-second
        # but is pointless when nothing can be cached).
        cache_key: str | None = None
        cache_key_done = False
        for g in self.goals:
            if g.type != "hard":
                continue
            if selected is not None and g.id not in selected:
                continue
            if getattr(g, "cacheable", False):
                if not cache_key_done:
                    cache_key = self._cache_key()
                    cache_key_done = True
                cached = self._cache.get(g.id)
                if cache_key is not None and cached is not None \
                        and cached[0] == cache_key:
                    prev = cached[1]
                    # Reuse the verdict; mark it as a cache hit (0ms) so logs
                    # show the skip rather than a stale duration.
                    results[g.id] = GoalResult(
                        prev.goal_id, prev.state, 0,
                        diagnostic=(prev.diagnostic
                                    + f" [cached tree={cache_key[:10]}]").strip(),
                        extras=prev.extras,
                    )
                    continue
                res = self._run_hard_retrying(g)
                # Only memoize PASS verdicts. A cached green on a byte-identical
                # tree is provably still green AND stabilizes flaky checks
                # (e.g. a timing-sensitive test) against spurious re-roll-to-red.
                # A FAIL is never cached, so a flaky red always gets re-run and
                # can clear, and a real red is re-surfaced every tick until the
                # peer's fix changes the tree.
                if cache_key is not None and res.state == "pass":
                    self._cache[g.id] = (cache_key, res)
                results[g.id] = res
            else:
                results[g.id] = self._run_hard_retrying(g)
        if selected is None:
            self._last = results
        else:
            self._last = {**self._last, **results}
        return results

    def _run_hard(self, g: Goal) -> GoalResult:
        assert g.cmd is not None, f"hard goal {g.id} has no cmd"
        assert g.pass_when is not None, f"hard goal {g.id} has no pass_when"
        t0 = time.monotonic()
        timeout_s = g.timeout_s if g.timeout_s is not None else self.timeout_s
        try:
            proc = _run_goal_cmd(g.cmd, self.cwd, timeout_s)
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
                diagnostic=_failure_diagnostic(f"pass_when error: {e}", proc),
            )
        return GoalResult(
            g.id,
            "pass" if passed else "fail",
            dur,
            diagnostic="" if passed else _failure_diagnostic(
                "pass_when returned False", proc
            ),
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
