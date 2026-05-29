from __future__ import annotations

import sys
from typing import Any

from peers.state_store import current_peer_name


def _driver_module() -> Any:
    import peers.driver_orchestrator as driver_orchestrator
    return driver_orchestrator


class DriverPeerHealthMixin:
    def _update_peer_health(self, state: dict[str, Any], peer: str,
                            success: bool) -> None:
        """Track per-peer recent failures (sliding window of 5). A peer
        with ≥ 3 failures out of the last 5 is marked `degraded`; a
        single success returns it to `healthy`. This is asymmetric on
        purpose (slow degrade, fast recovery): the loop has otherwise
        no leverage to retry a peer that just had a streak.
        adds an extra degrade trigger: ≥ 2 consecutive anti-
        cheating reverts also degrade the peer.

        (Audit note: a mixed [F,T,F,T,F] pattern can thrash
        degraded↔healthy across consecutive ticks. We accept the
        theoretical thrash to keep recovery responsive — a stuck-flag
        bias check happens at the loop level via stuck_counter.)"""
        t = state["peers"][peer]
        history = t.setdefault("recent_runs", [])  # list of bool OR float
        # Bug C: productive-commit-no-handoff counts as 0.5 fail (not 1.0).
        # Peers committing real work but missing the handoff trailer should
        # be hinted toward fixing the format, not penalized into DEGRADED.
        # Real-fails (idle-timeout, no-commit, history-rewrite) still 1.0.
        productive_no_handoff = bool(
            t.get("last_tick_productive_no_handoff", False)
        )
        if success:
            entry: bool | float = True
        elif productive_no_handoff:
            entry = 0.5
        else:
            entry = False
        history.append(entry)
        if len(history) > 5:
            del history[:-5]
        # Treat True as 0.0 fail, False as 1.0 fail, float entry as
        # (1 - value). Stored as float so DEGRADED threshold sees the
        # exact fractional total without rounding-up surprises.
        recent_fails = 0.0
        for x in history:
            if x is True:
                pass
            elif x is False:
                recent_fails += 1.0
            else:
                recent_fails += (1.0 - float(x))
        t["recent_fails"] = recent_fails
        prev_state = t.get("state")
        if success and prev_state == "degraded":
            t["state"] = "healthy"
            # (post-2026-05-24): clear degraded annotations
            # when peer recovers, so `peers-ctl status` doesn't show
            # stale "degraded since tick N" when state is healthy.
            t.pop("degraded_reason", None)
            t.pop("degraded_at_iter", None)
        elif (not success) and recent_fails >= 3:
            t["state"] = "degraded"
            if prev_state != "degraded":
                self._record_degraded_annotations(
                    state, peer,
                    "recent-fails:{0}/5".format(
                        int(recent_fails) if recent_fails == int(recent_fails)
                        else round(recent_fails, 1)
                    ),
                )
        elif t.get("failed_cheating", 0) >= 2:
            t["state"] = "degraded"
            if prev_state != "degraded":
                self._record_degraded_annotations(
                    state, peer, "anti-cheating-reverts:{0}".format(
                        t.get("failed_cheating", 0),
                    ),
                )

    def _record_degraded_annotations(
        self, state: dict[str, Any], peer: str, base_reason: str,
    ) -> None:
        """(post-2026-05-24): when a peer is first marked
        degraded, persist (a) WHY it was marked (`degraded_reason`)
        and (b) WHICH iteration noticed (`degraded_at_iter`). Both
        surface in `peers-ctl status` and runs.jsonl so the operator
        can decide if it's recoverable (transient api-error → maybe
        wait) or terminal (auth-failed → re-login).

        Also emits one stderr line — substrate-level visibility for
        anyone tailing the container log."""
        t = state["peers"][peer]
        last_run = t.get("last_run") or {}
        classification = last_run.get("classification", "")
        matched = last_run.get("matched_error_pattern", "")
        snippet = last_run.get("matched_error_snippet", "")
        reason_bits = [base_reason]
        if classification:
            reason_bits.append(f"last={classification}")
        if matched:
            reason_bits.append(f"pattern={matched[:60]}")
        reason = " | ".join(reason_bits)
        t["degraded_reason"] = reason
        t["degraded_at_iter"] = state.get("iteration", 0)
        print(
            f"peers: peer={peer} marked DEGRADED at iter="
            f"{state.get('iteration', 0)}: {reason}"
            + (f"\n  snippet: {snippet[:200]}" if snippet else ""),
            file=sys.stderr, flush=True,
        )

    def _maybe_halt(self, state: dict[str, Any]) -> None:
        """If ALL peers are degraded, write HALTED.md and mark state."""
        self._verify_peer_dir_identity()
        order = state["peer_order"]
        all_degraded = all(
            state["peers"][p].get("state") in ("degraded", "halted")
            for p in order
        )
        if not all_degraded:
            return
        # All peers degraded → halt-state across the board.
        for p in order:
            state["peers"][p]["state"] = "halted"
        halted_path = self.peer_dir / "HALTED.md"
        if halted_path.exists():
            return
        diag_lines = [
            "# Peers loop halted: all peers degraded",
            "",
            f"Iteration: {state['iteration']}",
            f"Whose turn was next: {current_peer_name(state)}",
            "",
            "## Peer health",
        ]
        for peer in order:
            t = state["peers"][peer]
            diag_lines.append(
                f"- {peer}: state={t.get('state')} "
                f"recent_fails={t.get('recent_fails')} "
                f"last_run={t.get('last_run')}"
            )
        diag_lines += [
            "",
            "## What to do",
            "",
            "- Check `.peers/log/runs.jsonl` for the failure patterns.",
            "- Verify Claude/Codex auth tokens are valid.",
            "- Adjust `health.idle_timeout_s` or `error_patterns` if " +
            "false positives are killing healthy runs.",
            "- Delete this file once resolved; the loop will pick back up.",
        ]
        try:
            _driver_module().write_text_no_symlink(
                halted_path, "\n".join(diag_lines) + "\n",
            )
        except Exception as e:
            print(
                f"peers: CRITICAL: could not write HALTED.md "
                f"({halted_path}): {e}. Loop is supposed to halt now "
                "but the marker file is missing; check disk space + "
                "permissions.",
                file=sys.stderr,
            )
