"""AsyncGateRunner — Tier-1 Part B.

Runs the *expensive* hard gates (the pytest/coverage-backed ones, chiefly
``no-prior-regression``) against a frozen git SHA in a throwaway ``git
worktree`` so the evaluation can overlap the next peer's turn instead of
blocking the loop. The verdict is identical to a synchronous eval on the
same SHA (same code, same checks) — only the *timing* overlaps.

The peer mutates the live tree during its turn; reading a frozen SHA via a
detached worktree decouples the gate eval from that mutation. The gitignored
``.peers/`` artifacts the gates read (cwd-relative, e.g.
``.peers/passing-baseline.txt``) are mirrored read-only into the worktree,
because ``git worktree add`` only checks out tracked files.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Iterable

from .goal_engine import GoalEngine
from .goals import Goal


class _GateEvalFailed:
    """Sentinel returned when the async eval could not run (git/worktree
    error). Callers MUST treat it as "re-run synchronously" — never as a
    pass or a fail."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "<GATE_EVAL_FAILED>"


GATE_EVAL_FAILED = _GateEvalFailed()

# Static, run-start ``.peers/`` artifacts the expensive gates read. Mirrored
# read-only into the frozen worktree (they are gitignored runtime state, so a
# fresh checkout lacks them). All are immutable for the run's duration.
_MIRROR = (
    "checks",
    "checks.sha256",
    "passing-baseline.txt",
    "api-baseline.txt",
    "goals.yaml",
)
_WORKTREE_PREFIX = "peers-gate-"


class AsyncGateRunner:
    """Submit a SHA to evaluate its expensive gates in the background; take
    the verdict later (joining if it isn't ready yet)."""

    def __init__(
        self,
        repo: Path,
        peers_dir: Path,
        goals: list[Goal],
        expensive_ids: Iterable[str],
        timeout_s: int = 1800,
    ) -> None:
        self.repo = Path(repo)
        self.peers_dir = Path(peers_dir)
        self.goals = list(goals)
        self.expensive_ids = set(expensive_ids)
        self.timeout_s = timeout_s
        # One eval in flight at a time: a single SHA's gates per tick, and we
        # never want two pytest suites contending for the box at once.
        self._ex = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="peers-gate",
        )
        self._futures: dict[str, Future] = {}
        self._order: list[str] = []
        # BUG-504 defense layer: every submit is tagged with the goals
        # generation at submit-time. invalidate_in_flight() bumps the
        # generation so poll_latest can drop stale verdicts even if the
        # clear missed (e.g. a future that finished after the bump).
        self._goals_generation: int = 0
        self._gen_for: dict[str, int] = {}

    def submit(self, sha: str) -> None:
        """Kick off background evaluation of the expensive gates on ``sha``.
        No-op when there are no expensive gates to run. Idempotent per sha: a
        repeat submit of an already-in-flight sha (no-new-commit ticks) keeps
        the original Future instead of overwriting it (which would leak an
        uncancelled eval) and duplicating the order queue (which would desync
        poll_latest/take)."""
        if not self.expensive_ids:
            return
        if sha in self._futures:
            return
        self._futures[sha] = self._ex.submit(self._run, sha)
        self._order.append(sha)
        self._gen_for[sha] = self._goals_generation

    def invalidate_in_flight(self) -> None:
        """BUG-504: drop all in-flight verdicts after a goals reload so a
        future computed against the OLD goal set can never be trusted as a
        fresh verdict for the SAME SHA. Cancels pending work (a running
        worker is not interruptible but its result is discarded by the
        generation bump). Idempotent."""
        for fut in list(self._futures.values()):
            fut.cancel()
        self._futures.clear()
        self._order.clear()
        self._gen_for.clear()
        self._goals_generation += 1

    def take(self, sha: str, block: bool = True):
        """Return the ``{goal_id: GoalResult}`` dict for ``sha``'s expensive
        gates, ``GATE_EVAL_FAILED`` if the eval could not run, or ``None`` if
        ``sha`` was never submitted. With ``block=True`` (default) this joins
        the background eval; normally it finished during the peer turn."""
        fut = self._futures.pop(sha, None)
        if sha in self._order:
            self._order.remove(sha)
        self._gen_for.pop(sha, None)
        if fut is None:
            return None
        try:
            return fut.result(timeout=None if block else 0)
        except Exception:
            return GATE_EVAL_FAILED

    def poll_latest(self):
        """Non-blocking: return ``(sha, results)`` for the most recently
        submitted SHA whose eval has FINISHED, discarding it and any older
        (now superseded) pending evals. Returns ``None`` when nothing has
        finished yet. This is how the loop consumes the freshest available
        expensive verdict without ever blocking on the next peer turn.

        BUG-504: entries whose recorded goals-generation is below the
        current one (because a goals reload invalidated them) are skipped
        and dropped — the caller will fall back to a sync eval against the
        fresh goal set instead of trusting an old-goal verdict."""
        for i in range(len(self._order) - 1, -1, -1):
            sha = self._order[i]
            fut = self._futures.get(sha)
            gen = self._gen_for.get(sha)
            if fut is None:
                continue
            if gen is not None and gen != self._goals_generation:
                # Stale-generation entry; drop it and keep scanning.
                self._futures.pop(sha, None)
                self._gen_for.pop(sha, None)
                self._order.pop(i)
                continue
            if fut.done():
                try:
                    result = fut.result()
                except Exception:
                    result = GATE_EVAL_FAILED
                for old in self._order[: i + 1]:
                    self._futures.pop(old, None)
                    self._gen_for.pop(old, None)
                self._order = self._order[i + 1:]
                return (sha, result)
        return None

    def _run(self, sha: str):
        wt = Path(tempfile.mkdtemp(prefix=_WORKTREE_PREFIX))
        try:
            add = subprocess.run(
                ["git", "-C", str(self.repo), "worktree", "add",
                 "--detach", str(wt), sha],
                capture_output=True, text=True, timeout=120, check=False,
            )
            if add.returncode != 0:
                return GATE_EVAL_FAILED
            self._mirror_peers_dir(wt)
            engine = GoalEngine(self.goals, cwd=wt, timeout_s=self.timeout_s)
            return engine.evaluate_hard_gates(self.expensive_ids)
        except Exception:
            return GATE_EVAL_FAILED
        finally:
            subprocess.run(
                ["git", "-C", str(self.repo), "worktree", "remove",
                 "--force", str(wt)],
                capture_output=True, text=True, timeout=60, check=False,
            )
            shutil.rmtree(wt, ignore_errors=True)

    def _mirror_peers_dir(self, wt: Path) -> None:
        dst_root = wt / ".peers"
        dst_root.mkdir(parents=True, exist_ok=True)
        for name in _MIRROR:
            src = self.peers_dir / name
            if not src.exists():
                continue
            dst = dst_root / name
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)

    def shutdown(self) -> None:
        self._ex.shutdown(wait=False, cancel_futures=True)


def prune_stale_gate_worktrees(repo: Path) -> int:
    """Remove leftover ``peers-gate-*`` worktrees from a crashed run (the
    happy path auto-removes them). Returns the count pruned. Safe at startup."""
    out = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if out.returncode != 0:
        return 0
    pruned = 0
    for line in out.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        path = line[len("worktree "):].strip()
        if Path(path).name.startswith(_WORKTREE_PREFIX):
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "remove", "--force", path],
                capture_output=True, text=True, timeout=60, check=False,
            )
            shutil.rmtree(path, ignore_errors=True)
            pruned += 1
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "prune"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    return pruned
