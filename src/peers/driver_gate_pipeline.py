"""Tier-1 Part B: gate-evaluation pipelining mixin for OrchestratorDriver.

Overlaps the expensive (pytest/coverage-backed) gate eval under the next peer
turn via :class:`AsyncGateRunner`, while cheap gates run synchronously each
tick. Kept in its own mixin so the hot driver files stay decomposed.
"""
from __future__ import annotations

from typing import Any

from peers.async_gate_runner import (
    GATE_EVAL_FAILED,
    AsyncGateRunner,
    prune_stale_gate_worktrees,
)
from peers.driver_host import _DriverHost
from peers.goal_engine import GoalResult


class DriverGatePipelineMixin(_DriverHost):
    """Gate pipelining. The host class must set ``self.engine``, ``self.repo``
    and ``self.peer_dir`` before calling ``_init_gate_pipeline``."""

    def _init_gate_pipeline(
        self, goals: list[Any], pipeline_gates: bool, goals_timeout_s: int,
    ) -> None:
        # Off by default (legacy full-sync); enabled for v21 via run config.
        # Only built when there are expensive gates to overlap.
        self.pipeline_gates = pipeline_gates
        self.async_runner = (
            AsyncGateRunner(
                self.repo, self.peer_dir, goals,
                self.engine.expensive_ids(), timeout_s=goals_timeout_s,
            )
            if pipeline_gates and self.engine.expensive_ids()
            else None
        )
        if self.async_runner is not None:
            # Clean any worktrees a previous crashed run left behind.
            prune_stale_gate_worktrees(self.repo)

    def _evaluate_gates_for_tick(self) -> dict[str, GoalResult]:
        """Cheap gates fresh (sync each tick), expensive gates from the
        overlapped async eval when ready, else a sync fallback. With
        pipeline_gates off (or no runner) this is the legacy full-sync eval —
        identical verdicts, only timing differs."""
        runner = getattr(self, "async_runner", None)
        if not getattr(self, "pipeline_gates", False) or runner is None:
            return self.engine.evaluate_hard_gates()
        cheap = self.engine.evaluate_hard_gates(self.engine.cheap_ids())
        polled = runner.poll_latest()
        # Trust the overlapped verdict ONLY when it was computed against the
        # commit we are now judging. poll_latest() returns freshest-DONE, which
        # can be an OLDER sha than current HEAD once the peer has committed
        # again; applying that stale verdict would feed the stuck/two-phase
        # counters a judgement of the wrong tree (and could trip a false
        # stuck:tests-pass — the v19/v20 brick class). On a SHA mismatch (or
        # bootstrap / not-ready / eval-failed) fall back to a synchronous eval
        # of the live tree. NB: a dirty live tree on the SAME committed SHA is
        # intentionally still served from the overlap — expensive gates judge
        # the committed SHA; the terminal fresh-recheck re-judges the live tree
        # before convergence.
        if (
            polled is not None
            and polled[1] is not GATE_EVAL_FAILED
            and polled[0] == self.comm.head_sha()
        ):
            expensive = polled[1]
        else:
            expensive = self.engine.evaluate_hard_gates(
                self.engine.expensive_ids()
            )
        return {**expensive, **cheap}

    def _submit_gate_eval(self, sha: str) -> None:
        """Kick off the overlapped expensive-gate eval for ``sha`` (the SHA the
        peer just produced) so it runs during the next peer turn. No-op when
        pipelining is off."""
        runner = getattr(self, "async_runner", None)
        if runner is not None and getattr(self, "pipeline_gates", False) \
                and sha:
            runner.submit(sha)

    def _terminal_fresh_recheck(
        self, state: dict[str, Any], results: dict[str, GoalResult],
    ) -> dict[str, GoalResult]:
        """Re-run the expensive gates SYNCHRONOUSLY on the current tree and
        re-record, returning the merged verdict. No-op (returns ``results``
        unchanged) when pipelining is off."""
        if not getattr(self, "pipeline_gates", False) or \
                getattr(self, "async_runner", None) is None:
            return results
        fresh = self.engine.evaluate_hard_gates(self.engine.expensive_ids())
        results = {**results, **fresh}
        self._record_results(state, results)
        return results

    def _converged_after_fresh_recheck(
        self, state: dict[str, Any], results: dict[str, GoalResult],
    ) -> bool:
        """Tier-1B B4 convergence gate: True iff all gates are green — but when
        the (possibly stale) pipelined verdict looks all-green, first re-run the
        expensive gates FRESH on the current tree, so convergence/complete is
        never declared on a stale verdict."""
        if not self._all_green_including_soft(state):
            return False
        self._terminal_fresh_recheck(state, results)
        return self._all_green_including_soft(state)
