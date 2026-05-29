from __future__ import annotations

import hashlib
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from peers.anti_cheat_guard import AntiCheatGuard
from peers.budget_accountant import (
    BudgetCheck,
    account_tokens_usd,
    record_tick_accounting,
)
from peers.bug_hunt import count_new_blocking_or_flag_bug_reports
from peers.comm_layer import HybridCommLayer
from peers.driver_helpers import (
    _AUTO_SKEPTIC_PROMPT_PREFIX,
    PHASE_IMPLEMENTATION,
    _hash_goals_yaml,
    _load_phase_prompt,
    _resolve_peer_role,
    _resolve_phase,
    _should_checkpoint,
)
from peers.goal_engine import GoalResult
from peers.health_guard import RunResult
from peers.peer_spec import PeerSpec
from peers.prompt_builder import build_prompt
from peers.safe_io import read_text_no_symlink
from peers.skeptic_engine import PHASE_B_SKEPTIC_GATES, SkepticEngine
from peers.turn_manager import TurnManager


# Item 7: convergence-wall hard halt. v9-v12 all burned full budget
# struggling on tests-pass + no-prior-regression. After N consecutive
# red ticks on a watched gate, exit cleanly with stop-reason `stuck:<gate>`
# instead of letting the loop spin until max_runtime.
_DEFAULT_STUCK_HALT_AFTER = 5
_DEFAULT_STUCK_HALT_GATES = ("tests-pass", "no-prior-regression")


def compute_stuck_gate_halt_reason(state: dict[str, Any]) -> str | None:
    """Return `stuck:<gate>` if any watched gate stuck >= threshold ticks.

    Threshold default 5, override via state['config']['goals']['stuck_halt_after'].
    Watched-gate set defaults to (tests-pass, no-prior-regression), override
    via state['config']['goals']['stuck_halt_gates'] (list of goal ids).
    A threshold of 0 disables the halt entirely (legacy behavior).
    """
    cfg_goals = ((state.get("config") or {}).get("goals") or {})
    raw_n = cfg_goals.get("stuck_halt_after", _DEFAULT_STUCK_HALT_AFTER)
    try:
        threshold = int(raw_n)
    except (TypeError, ValueError):
        threshold = _DEFAULT_STUCK_HALT_AFTER
    if threshold <= 0:
        return None
    raw_gates = cfg_goals.get("stuck_halt_gates")
    if raw_gates:
        watched = tuple(str(g) for g in raw_gates)
    else:
        watched = _DEFAULT_STUCK_HALT_GATES
    stuck = state.get("stuck_counter") or {}
    # Pick the worst (highest count) watched gate that crossed threshold.
    worst_gate: str | None = None
    worst_count = -1
    for gate in watched:
        count = int(stuck.get(gate, 0))
        if count >= threshold and count > worst_count:
            worst_gate = gate
            worst_count = count
    if worst_gate is None:
        return None
    return f"stuck:{worst_gate}"


class DriverTickHooksMixin:
    def _record_phase(self, state: dict[str, Any]) -> None:
        """Stamp the upcoming tick's phase into state.json.

        Called at the start of every tick BEFORE the prompt is built or
        the peer runs. state["iteration"] is the 0-indexed count of
        completed ticks → equals the index of the tick about to fire.
        For non-implement modes this always resolves to "implementation"
        (strict backward-compat for audit / security / thorough / custom
        user modes). For implement-mode the first three ticks resolve to
        recon → alignment → architecture (Phase 0 prep prelude).

        The actual Phase 0 prompt overlay is applied later by
        `_prepare_tick_prompt` (Tasks 4.2-4.4 — see `_load_phase_prompt`).
        Here we only persist the phase string and emit a one-line
        operator marker for non-implementation phases so it's obvious
        from the substrate log which prelude tick is firing.
        """
        phase = _resolve_phase(self.mode_name, state["iteration"])
        state["phase"] = phase
        if phase != PHASE_IMPLEMENTATION:
            print(
                f"peers: phase={phase} (tick {state['iteration']}, "
                f"mode={self.mode_name}) — Phase 0 prompt overlay active",
                file=sys.stderr, flush=True,
            )

    def _maybe_checkpoint_exit(
        self, state: dict[str, Any], ticks: int,
    ) -> dict[str, Any] | None:
        """Task 4.5: pause the loop when --checkpoint was requested
        and Phase 0 just completed.

        Captures the previous phase, calls `_record_phase` to advance
        it, then asks `_should_checkpoint` whether the architecture →
        implementation boundary was just crossed AND
        `.peers/checkpoint_requested` is on disk. When yes, drops an
        `.peers/awaiting_user` marker (best-effort), logs an operator
        message, and returns the loop's exit dict with sentinel
        `checkpoint:phase-0-complete`. Returns None on the normal
        (non-checkpoint) path so the caller proceeds with the tick.
        """
        prev_phase = state.get("phase")
        self._record_phase(state)
        curr_phase = state.get("phase", PHASE_IMPLEMENTATION)
        if not _should_checkpoint(
            self.peer_dir,
            prev_phase=prev_phase, curr_phase=curr_phase,
        ):
            return None
        reason = "checkpoint:phase-0-complete"
        try:
            (self.peer_dir / "awaiting_user").write_text(
                f"checkpoint at iter={state.get('iteration', 0)}\n"
                "review RECON.md + PLAN.aligned.md + "
                "ARCHITECTURE.intended.md, then run "
                "`peers-ctl resume <project>` + "
                "`peers-ctl start <project>` to continue.\n",
                encoding="utf-8",
            )
        except OSError as e:
            print(
                f"peers: warning, failed to write awaiting_user marker: {e!r}",
                file=sys.stderr, flush=True,
            )
        print(
            f"peers: {reason} — Phase 0 prep complete "
            f"(iter={state.get('iteration', 0)}). Loop pausing for "
            "operator review. Clear with `peers-ctl resume <project>` "
            "then re-launch with `peers-ctl start <project>`.",
            file=sys.stderr, flush=True,
        )
        return self._exit_with_fresh_results(state, reason, ticks)

    def _update_convergence_counter(self, state: dict[str, Any]) -> None:
        """thorough-mode convergence counter. Counts ticks that
        landed WITHOUT a new crit/high/med Bug-Report or weak-fix/shallow-
        fix flag-bug since the tick started. Cheap to compute every tick
        (single `git log <range>`); the actual gating is in
        `convergence_reached.py`. Wrapped to never crash the main loop on
        an audit-side glitch.

        Skipped in dry_run because commits were reset above; counting
        commits in an empty range would increment trivially every tick
        and make convergence-reached pass without doing real work.
        """
        if self.dry_run:
            return
        since = self._head_before_invoke
        if since is None:
            return
        try:
            n_blocking = count_new_blocking_or_flag_bug_reports(
                self.repo, since,
            )
        except Exception:
            n_blocking = 0
        if n_blocking == 0:
            state["consecutive_clean_ticks"] = state.get(
                "consecutive_clean_ticks", 0
            ) + 1
        else:
            state["consecutive_clean_ticks"] = 0
        # Re-save after the counter mutation. Cheap.
        self._save_state(state)

    def _exit_with_fresh_results(
        self, state: dict[str, Any], reason: str, ticks: int,
    ) -> dict[str, Any]:
        results = self.engine.evaluate_hard_gates()
        self._record_results(state, results)
        self._save_state(state)
        self._append_exit_event(reason, ticks)
        return {"reason": reason, "state": state}

    def _pre_tick_exit(
        self, state: dict[str, Any], max_ticks: int | None, ticks: int,
    ) -> tuple[dict[str, Any] | None, dict[str, GoalResult]]:
        if max_ticks is not None and ticks >= max_ticks:
            return self._exit_with_fresh_results(state, "max_ticks", ticks), {}
        budget_reason = BudgetCheck(state).reason()
        if budget_reason is not None:
            reason = f"budget:{budget_reason}"
            return self._exit_with_fresh_results(state, reason, ticks), {}
        # Item 7: tests-pass + no-prior-regression as convergence wall.
        # v9-v12 burned full budgets struggling on these gates. After
        # stuck_halt_after (default 5) consecutive red ticks on a critical
        # gate, give up cleanly with stop-reason `stuck:<gate>` instead
        # of letting the loop spin until max_runtime fires.
        stuck_reason = compute_stuck_gate_halt_reason(state)
        if stuck_reason is not None:
            return self._exit_with_fresh_results(state, stuck_reason, ticks), {}
        mutation_reason = self._goal_mutation_reason()
        if mutation_reason is not None:
            reason = f"goal-mutation:{mutation_reason}"
            self._save_state(state)
            self._append_exit_event(reason, ticks)
            return {"reason": reason, "state": state}, {}
        results = self.engine.evaluate_hard_gates()
        self._record_results(state, results)
        # Task 6.5: maintain implement-mode two-phase convergence counters
        # before consulting `_all_green_including_soft`. No-op for other
        # modes (strict backward-compat).
        self._update_two_phase_counters(state, results)
        if self._all_green_including_soft(state):
            if self.mode_name == "hunt-open-ended":
                state.setdefault("warnings", []).append(
                    "hunt-open-ended: convergence signals are progress "
                    "only; continuing until budget exhaustion or halt-class."
                )
                self._save_state(state)
                return None, results
            # auto-skeptic re-audit before declaring complete.
            # When convergence is fresh (no skeptic run yet for this
            # convergence cycle), set a flag that injects a critical-
            # re-audit prompt into the next tick. If that tick stays
            # clean → really terminal next time around. If it surfaces
            # a new blocking bug → counter resets, normal loop continues.
            current_iter = state.get("iteration", 0)
            last_skeptic_at = state.get("_auto_skeptic_ran_at", -2)
            if (self.auto_skeptic_enabled
                    and current_iter - last_skeptic_at > 1):
                state["_auto_skeptic_prompt_pending"] = True
                self._save_state(state)
                print(
                    f"peers: convergence-reached at iter={current_iter}; "
                    "running auto-skeptic re-audit tick before terminal "
                    "exit (disable with --without-post-convergence-skeptic)",
                    file=sys.stderr, flush=True,
                )
                return None, results
            # Task 6.5: implement-mode requires two-phase convergence
            # before declaring "complete". Non-implement modes fall
            # through to the original exit unchanged.
            if self.mode_name == "implement":
                if state.get("convergence_phase") != "complete":
                    self._save_state(state)
                    return None, results
            self._save_state(state)
            self._append_exit_event("complete", ticks)
            return {"reason": "complete", "state": state}, results
        return None, results

    _PHASE_B_SKEPTIC_GATES = PHASE_B_SKEPTIC_GATES

    def _update_two_phase_counters(
        self, state: dict[str, Any], results: dict[str, GoalResult],
    ) -> None:
        """Task 6.5: maintain implement-mode convergence_phase machine.

        Pure additive: writes `convergence_phase`, `consecutive_hard_
        green_ticks`, and `phase_b_extra_ticks` to state on implement-
        mode runs only. Other modes are short-circuited so their
        state.json carries no Task 6.5 fields.
        """
        SkepticEngine(
            self.mode_name,
            phase_b_skeptic_gates=self._PHASE_B_SKEPTIC_GATES,
        ).update_two_phase_counters(state, results)

    def _account_tokens_usd(
        self, state: dict[str, Any], tool: str, run: Any,
    ) -> tuple[int, float]:
        """G5: parse tokens/$ from this run's output, keyed by the
        PeerSpec's `tool` (two peers both running `claude` share the
        same parser). Returns (tokens_this_tick, usd_this_tick) for
        the run-log."""
        return account_tokens_usd(state, tool, run)

    def _handle_pattern_match_and_halt(
        self, state: dict[str, Any], ticks: int, upcoming_tick: int,
        peer: str, run: Any,
    ) -> dict[str, Any] | None:
        """+II (post-2026-05-24): emit one stderr marker per
        api-error tick (so the operator can see WHICH pattern killed
        the peer without grepping runs.jsonl) and, when the matched
        pattern was a HALT class (AUTH/QUOTA), tear the loop down
        with a peer-unavailable exit_event instead of degrading and
        retrying. Returns the loop's exit dict on halt; None
        otherwise."""
        if (run.classification == "api-error"
                and run.matched_error_pattern):
            halt_tag = " HALT-CLASS" if run.halt_required else ""
            print(
                f"peers: tick {upcoming_tick} peer={peer}{halt_tag} "
                f"matched-pattern source={run.matched_error_source or '?'} "
                f"pattern={run.matched_error_pattern[:80]} "
                f"snippet={run.matched_error_snippet[:120]!r}",
                file=sys.stderr, flush=True,
            )
        if not run.halt_required:
            return None
        self._write_peer_output_logs(upcoming_tick, peer, run)
        pinfo = state["peers"].setdefault(peer, {})
        pinfo["state"] = "unavailable"
        pinfo["unavailable_reason"] = (
            f"halt-pattern: {run.matched_error_pattern[:80]}"
        )
        pinfo["unavailable_at_iter"] = state.get("iteration", 0)
        pinfo["unavailable_snippet"] = run.matched_error_snippet[:200]
        print(
            f"peers: HALT — peer={peer} hit halt-class pattern. "
            "Operator action required (re-login, top-up, etc.). "
            f"Pattern: {run.matched_error_pattern[:80]}",
            file=sys.stderr, flush=True,
        )
        reason = f"peer-unavailable:{peer}"
        self._save_state(state)
        self._append_exit_event(reason, ticks)
        return {"reason": reason, "state": state}

    def _prepare_tick_prompt(
        self, state: dict[str, Any], tm: TurnManager,
        results: dict[str, GoalResult],
    ) -> tuple[str, PeerSpec, str]:
        peer = tm.current()
        others = tm.others()
        other_for_prompt = others[0] if len(others) == 1 else ", ".join(others)
        inbox = self._read_inbox(others, state, receiver=peer)
        stuck = any(
            state["stuck_counter"].get(gid, 0) >= 10
            for gid, gr in results.items() if gr.state == "fail"
        )
        warnings = self._pop_prompt_warnings(state)
        prompt = build_prompt(
            peer=peer, other=other_for_prompt,
            goals=self.goals, results=results,
            inbox=inbox, stuck=stuck,
            warnings=warnings,
            soft_reviews_pending=self._soft_reviews_pending(state, peer),
            comm_variant=self.comm_variant,
            all_peer_names=list(self.peer_names),
        )
        # Tasks 4.2-4.4: implement-mode Phase 0 prompt overlay. When
        # the current phase has a shipped template (recon / alignment /
        # architecture), prepend it to the regular prompt. Other phases
        # (and non-implement modes) get None back and skip the overlay.
        phase = state.get("phase", PHASE_IMPLEMENTATION)
        phase_prompt = _load_phase_prompt(self.mode_name, phase)
        if phase_prompt is not None:
            prompt = phase_prompt + "\n\n" + prompt
        # Task 6.2: blind-review tick role overlay. During implement-
        # mode's implementation phase, each tick is either an
        # implementer-tick (writes IMPLEMENTATION_NOTES.md) or a
        # reviewer-tick (writes REVIEW_NOTES.md without peeking) — see
        # `_resolve_peer_role`. The role-specific prompt is loaded via
        # the same `_load_phase_prompt` mechanism by treating
        # `blind_review_<role>` as a pseudo-phase name; the underlying
        # template lookup is path-traversal-safe.
        tick_for_role = state.get("iteration", 0)
        role = _resolve_peer_role(self.mode_name, phase, tick_for_role)
        if role in ("implementer", "reviewer"):
            role_prompt = _load_phase_prompt(
                self.mode_name, f"blind_review_{role}",
            )
            if role_prompt is not None:
                prompt = role_prompt + "\n\n" + prompt
        # post-convergence skeptic re-audit overlay. When
        # _auto_skeptic_prompt_pending is set (by _pre_tick_exit on
        # first detection of convergence), prepend a critical-re-audit
        # header to the regular prompt and record that the skeptic ran
        # at this iteration (= the tick about to fire, iteration+1).
        if state.pop("_auto_skeptic_prompt_pending", False):
            state["_auto_skeptic_ran_at"] = state.get("iteration", 0) + 1
            prompt = _AUTO_SKEPTIC_PROMPT_PREFIX + "\n\n" + prompt
        return peer, self.peers_by_name[peer], prompt

    def _pop_prompt_warnings(self, state: dict[str, Any]) -> list[str]:
        warnings = state.pop("warnings", [])
        if len(warnings) <= 50:
            return warnings
        return (
            warnings[:5]
            + [f"... <{len(warnings) - 55} warnings omitted> ..."]
            + warnings[-50:]
        )

    def _anti_cheat_guard(self) -> AntiCheatGuard:
        return AntiCheatGuard(
            self.repo,
            self._head_before_invoke,
            self.comm.head_sha,
        )

    def _apply_anti_cheating_outcome(
        self, state: dict[str, Any], peer: str, success: bool,
    ) -> bool:
        return self._anti_cheat_guard().apply_outcome(state, peer, success)

    def _test_only_justification(self) -> str | None:
        return self._anti_cheat_guard().test_only_justification()

    def _apply_dry_run_reset(
        self, state: dict[str, Any], success: bool,
    ) -> bool:
        if not self.dry_run or self._head_before_invoke is None:
            return success
        try:
            subprocess.run(
                ["git", "reset", "--hard", self._head_before_invoke],
                cwd=self.repo, check=True, capture_output=True,
            )
            return success
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode(
                "utf-8", errors="replace"
            )[-400:]
            state.setdefault("warnings", []).append(
                "dry-run reset FAILED — peer's commits remain in the "
                "working tree (dry-run guarantee broken). git reset "
                f"stderr: {stderr!r}"
            )
            return False

    def _record_tick_accounting(
        self, state: dict[str, Any], success: bool, tick_dt: int,
        peer: str | None = None,
    ) -> None:
        record_tick_accounting(state, success, tick_dt, peer=peer)

    def _record_results(self, state: dict[str, Any],
                        results: dict[str, GoalResult]) -> None:
        for gid, r in results.items():
            prev = state["goals_status"].get(gid, {}).get("state")
            state["goals_status"][gid] = {
                "state": r.state,
                "diagnostic": r.diagnostic,
                "duration_ms": r.duration_ms,
            }
            if r.state == "fail":
                if prev == "fail":
                    state["stuck_counter"][gid] = \
                        state["stuck_counter"].get(gid, 0) + 1
                else:
                    state["stuck_counter"][gid] = 1
            else:
                state["stuck_counter"].pop(gid, None)

    def _read_inbox(self, others: list[str],
                    state: dict[str, Any],
                    receiver: str | None = None) -> list[str]:
        bookmarks = state.setdefault("last_inbox_sha", {})
        msgs: list[str] = []
        for other in others:
            last_seen = bookmarks.get(other)
            if last_seen is None:
                # First call: seed cursor at current HEAD so we don't replay
                # all history as inbox.
                bookmarks[other] = self.comm.head_sha()
                continue
            try:
                commits = self.comm.new_commits_by(peer=other,
                                                   since=last_seen)
            except subprocess.CalledProcessError as e:
                state.setdefault("warnings", []).append(
                    f"git error reading inbox for {other}: {e}"
                )
                continue
            for c in commits:
                msgs.append(f"[{other}] {c.subject} ({c.sha[:8]})")
            if commits:
                bookmarks[other] = commits[-1].sha

        # in `comm: hybrid` mode the
        # driver previously WROTE-but-never-READ from the file channel.
        # Peers were instructed (via HYBRID_COMM_BLOCK in the prompt)
        # to drop markdown files at .peers/comms/<from>-to-<to>/ but
        # the substrate ingested none of them — the channel was
        # effectively write-only. Fix: when hybrid is active AND we
        # know which peer is about to run (receiver), fetch their inbox
        # files from each other peer, surface them in the prompt's
        # inbox section, and archive them once consumed.
        if isinstance(self.comm, HybridCommLayer) and receiver is not None:
            self._verify_peer_dir_identity()
            for other in others:
                try:
                    paths = self.comm.fetch_new(other, receiver)
                except OSError as e:
                    state.setdefault("warnings", []).append(
                        f"hybrid inbox read error for {other}→{receiver}: {e}"
                    )
                    continue
                for p in paths:
                    try:
                        text = read_text_no_symlink(p, max_bytes=4001)
                    except OSError as e:
                        state.setdefault("warnings", []).append(
                            f"hybrid inbox skipped {p.name}: {e}"
                        )
                        continue
                    # Trim — long bodies bloat the prompt without value.
                    snippet = text[:4000]
                    if len(text) > 4000:
                        snippet += "\n... (truncated)"
                    msgs.append(
                        f"[{other} → file {p.name}]\n{snippet}"
                    )
                    try:
                        self.comm.archive(p)
                    except OSError as e:
                        state.setdefault("warnings", []).append(
                            f"hybrid inbox archive failed for {p.name}: {e}"
                        )
        return msgs

    def _post_run(self, state: dict[str, Any], peer: str,
                  run: RunResult) -> bool:
        info: dict[str, Any] = {
            "classification": run.classification,
            "duration_ms": run.duration_ms,
        }
        state["peers"][peer]["last_run"] = info
        # Bug C: reset the half-fail flag each tick. Set true only when
        # we explicitly detect productive-commit-no-handoff below.
        state["peers"][peer]["last_tick_productive_no_handoff"] = False

        # Dogfood-R2 finding: claude in -p (print) mode is silent
        # while it works. With a too-low idle_timeout_s, the
        # HealthGuard kills it AFTER it has already committed a valid
        # handoff. Treat idle-timeout + valid handoff as partial-
        # success: the peer's contract was met (`## Self-Review` +
        # trailers), only the print-and-exit step got cut off.
        # Other non-success classifications (process-fail, api-error,
        # absolute-timeout) are NOT promoted — those leave incomplete
        # work much more often.
        accept_despite_class = (run.classification == "idle-timeout")
        if run.classification != "success" and not accept_despite_class:
            info["soft_fail_reason"] = f"run classification {run.classification}"
            return False

        # Verify the peer actually produced a handoff commit.
        since = self._head_before_invoke
        try:
            new_commits = self.comm.new_commits_by(peer=peer, since=since)
        except Exception as e:
            info["soft_fail_reason"] = f"cannot read git: {e}"
            return False

        if not new_commits:
            info["soft_fail_reason"] = (
                "no commit by peer this turn"
                + (f" (classification was {run.classification})"
                   if run.classification != "success" else "")
            )
            return False

        # H9: fast-forward only — reject amend / rebase that rewrites
        # the previous head_sha out of history.
        if since is not None and not self._is_ancestor(since, "HEAD"):
            info["soft_fail_reason"] = (
                f"history was rewritten: {since[:8]} is no longer an "
                "ancestor of HEAD (amend / rebase / reset is not allowed)"
            )
            return False

        # Look for Peer-Review-Of commits in this turn (G4 soft reviews).
        # also count per-tick ingest/reject so runs.jsonl shows
        # WHY a soft-consensus might be stuck at 0/2 — operators were
        # previously left grepping for "soft-review ignored" in warnings.
        #
        # Count ingestions directly from the helper's return value.
        # An earlier version differenced summed history-list lengths
        # before/after the loop, which silently under-counted whenever a
        # goal's history was already at its 20-entry cap: each new
        # ingest shifted an old entry out, the delta landed at 0, and
        # `soft_reviews_rejected` was inflated to `soft_seen`.
        soft_seen = 0
        soft_ingested = 0
        for c in new_commits:
            if c.trailers.get("Peer-Review-Of"):
                soft_seen += 1
            if self._record_soft_review_from_commit(
                    state, c, reviewer=peer):
                soft_ingested += 1
        info["soft_reviews_seen"] = soft_seen
        info["soft_reviews_ingested"] = soft_ingested
        info["soft_reviews_rejected"] = soft_seen - soft_ingested

        # ANY commit in this turn carrying the handoff trailers counts as
        # a valid handoff — peer is allowed to add tidy-up commits AFTER
        # the handoff commit. (Stricter "last commit must be handoff" was
        # too brittle; see integration test test_trailing_junk_commit.)
        if any(
            c.trailers.get("Peer-Status") == "handoff"
            and c.trailers.get("Self-Review") == "pass"
            for c in new_commits
        ):
            # If we got here via idle-timeout (the peer was killed
            # AFTER committing), flag it so the operator knows to
            # raise idle_timeout_s — and so the next prompt can
            # mention it.
            if run.classification == "idle-timeout":
                info["partial_handoff_rescued"] = True
                state.setdefault("warnings", []).append(
                    f"healthguard: {peer!r} hit idle_timeout_s after "
                    "committing a valid handoff. Work was kept. "
                    "Consider raising health.idle_timeout_s if this "
                    "happens repeatedly (claude -p is silent while "
                    "working)."
                )
            return True

        # Bug C: peer DID commit productive work this turn (new_commits
        # non-empty per the check at line 502) but no commit carries the
        # handoff trailers. Old behavior counted this as a full fail —
        # v12 tick 9+10 saw claude produce real review + test commits
        # without trailers, both classified as full no-handoff fails
        # toward DEGRADED. That's punishing busy peers for formatting.
        #
        # New semantics (half-fail): productive-commit-no-handoff returns
        # False (so the operator-readable label stays "no-handoff" and
        # the next prompt can carry the warning), but a sentinel flag is
        # set on the peer state. recent_fails / DEGRADED logic reads this
        # flag and treats the tick as 0.5 instead of 1.0.
        peer_state = state["peers"].setdefault(peer, {})
        peer_state["last_tick_productive_no_handoff"] = True
        info["productive_no_handoff"] = True
        info["soft_fail_reason"] = (
            "productive commit(s) this turn but no Peer-Status: handoff "
            "+ Self-Review: pass trailers (half-fail credit toward DEGRADED)"
        )
        return False

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        r = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=self.repo, capture_output=True,
        )
        return r.returncode == 0

    _TEST_PATH_RE = None  # lazy compile in _detect_tampering

    def _diff_stats_since_invoke(self) -> tuple[int, int] | None:
        """Returns (test_lines_added_or_removed, src_lines_…) for the
        diff between `_head_before_invoke` and HEAD. None if anything
        is amiss."""
        return self._anti_cheat_guard().diff_stats_since_invoke()

    def _classify_cheating(self, state: dict[str, Any]) -> str | None:
        """classify the turn's diff for anti-cheating
        triggers. Returns a human-readable reason if it's clearly
        cheating, else None.

        Currently the only hard-block trigger is "only test files
        changed in this turn". Coverage-sanity / fail→pass flip
        detection is kept as a warning in `_detect_tampering`
        (less specific, more prone to false positives).
        """
        return self._anti_cheat_guard().classify_cheating(state)

    def _stderr_text(e: subprocess.CalledProcessError) -> str:
        if e.stderr is None:
            return str(e)
        if isinstance(e.stderr, bytes):
            return e.stderr.decode("utf-8", errors="replace").strip()
        return str(e.stderr).strip()

    def _revert_handoff(self, reason: str) -> bool:
        """Run `git revert --no-commit <since>..HEAD` then commit the
        merged revert. Returns True on success.

        If neither the revert nor the destructive fallback works, the
        cheating commit stays in the tree — we MUST surface this loud
        and clear so the user knows the integrity guarantee broke.
        """
        return self._anti_cheat_guard().revert_handoff(reason)

    def _detect_tampering(self, state: dict[str, Any]) -> None:
        """G8 + G9: best-effort anti-cheating heuristics, run AFTER
        each successful tick. Findings are appended to
        state['warnings'] and surfaced in the next prompt; we do not
        revert the commit automatically (too aggressive)."""
        self._anti_cheat_guard().detect_tampering(state)

    def _goal_mutation_reason(self) -> str | None:
        """G7 + H1: detects in-flight tampering of goals.yaml. Compares
        the file's CURRENT hash to the IN-MEMORY snapshot captured by
        OrchestratorDriver.__init__, NOT to the on-disk goals.sha256.
        A peer that rewrites both files together can no longer fool
        the check.

        (e) (post-2026-05-24): allow paired feature work. v4
        tick 17 added `Goal.timeout_s` field + applied it in goals.yaml
        as one commit, which the substrate halted as gaming. A peer
        that legitimately extends the substrate must edit BOTH the
        engine code AND goals.yaml together; pure goals.yaml edits
        without an accompanying source change in the SAME HEAD commit
        still halt. `.peers/` files do NOT count as paired — they're
        all control-plane and a peer coordinating an attack against
        the gate would land there too.
        """
        self._verify_peer_dir_identity()
        gfile = self.peer_dir / "goals.yaml"
        if self._goal_hash_snapshot is None:
            return None
        if not gfile.exists():
            return "goals.yaml disappeared since loop start"
        try:
            actual = _hash_goals_yaml(gfile)
        except (OSError, ValueError) as e:
            return f"goals.yaml unreadable or unsafe during mutation check: {e}"
        if self._goal_hash_snapshot == actual:
            return None

        # (e): is the change paired with an out-of-.peers
        # source edit in the most recent commit? If yes, treat as
        # feature work, refresh the snapshot, and let the loop carry
        # on.
        if self._head_commit_pairs_goals_with_source(gfile):
            # Refresh the snapshot so the *next* tick doesn't keep
            # halting on the same delta.
            self._goal_hash_snapshot = actual
            return None

        return (
            f"goals.yaml hash changed since loop start "
            f"(expected {self._goal_hash_snapshot[:8]}, "
            f"got {actual[:8]}). If intentional, the change must land "
            "in a commit that ALSO touches a source file outside "
            "`.peers/` (e.g., src/) — pure control-plane edits still "
            "trip the mutation guard."
        )

    def _head_commit_pairs_goals_with_source(self, gfile: Path) -> bool:
        """(e): returns True iff HEAD's tree contains the
        current goals.yaml content (i.e., the edit is committed, not
        an uncommitted working-tree change) AND HEAD's commit touched
        at least one file that is NOT under `.peers/`.

        Safe-fail: any git error or unexpected output returns False so
        the calling code falls through to the existing halt-and-block
        behavior."""
        try:
            r = subprocess.run(
                ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                capture_output=True, check=True, text=True, timeout=10,
            )
            head_sha = r.stdout.strip()
            if not re.fullmatch(r"[0-9a-f]{7,64}", head_sha):
                return False
            # Confirm working tree's goals.yaml content matches HEAD's
            # blob — guards against an uncommitted mid-loop edit
            # masquerading as the paired commit.
            blob = subprocess.run(
                ["git", "-C", str(self.repo), "show",
                 f"{head_sha}:.peers/goals.yaml"],
                capture_output=True, check=True, timeout=10,
            )
            if hashlib.sha256(blob.stdout).hexdigest() != _hash_goals_yaml(gfile):
                return False
            # What files did HEAD touch?
            files = subprocess.run(
                ["git", "-C", str(self.repo),
                 "diff-tree", "--no-commit-id", "--name-only", "-r",
                 head_sha],
                capture_output=True, check=True, text=True, timeout=10,
            )
            touched = [
                p for p in files.stdout.split("\n") if p
            ]
            # Need: .peers/goals.yaml is in there AND at least one
            # path that is NOT under .peers/.
            has_goals = ".peers/goals.yaml" in touched
            has_source = any(not p.startswith(".peers/") for p in touched)
            return has_goals and has_source
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                FileNotFoundError, OSError, ValueError):
            return False
