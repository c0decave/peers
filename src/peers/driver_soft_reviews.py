from __future__ import annotations

from typing import Any

from peers.driver_helpers import _extract_first_json_object
from peers.goals import Goal


_SOFT_CONSENSUS_FALSY = frozenset({"false", "0", "no", "off", ""})


def soft_consensus_required_for_convergence(state: dict[str, Any]) -> bool:
    """Item 8: operators can disable the soft-consensus convergence gate
    via .peers/config.yaml -> goals.soft_consensus_required: false.

    Background: v9 + v10 both exited budget:max_runtime (not 'complete')
    because _all_green_including_soft required every soft goal to have
    peer-review consensus, even after all hard gates had been green for
    many ticks. Operators who want hard-gate-driven convergence can opt
    out. Default True preserves legacy strict semantics.
    """
    cfg_goals = ((state.get("config") or {}).get("goals") or {})
    raw = cfg_goals.get("soft_consensus_required", True)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() not in _SOFT_CONSENSUS_FALSY
    return bool(raw)


class DriverSoftReviewsMixin:
    def _soft_reviews_pending(self, state: dict[str, Any],
                              current_peer: str) -> list[Goal]:
        """Return soft goals whose consensus isn't yet reached AND the
        current peer is expected to weigh in on this turn.

        Reviewer modes (matches goals.VALID_REVIEWER_MODES):
        - other: any non-active peer reviews on each of its turns.
        - both: all non-active peers must review.
        - alternating: review duty rotates over peer_order independent
          of TurnManager — see _alternating_reviewer for the index.
        - quorum: same scheduling as `other`, but consensus tallying
          uses quorum_num/quorum_den (see _record_soft_review_from_commit).
        """
        order = state["peer_order"]
        out: list[Goal] = []
        soft_status = state.get("soft_status", {})
        for g in self.goals:
            if g.type != "soft":
                continue
            sg = soft_status.get(g.id, {})
            if self._soft_goal_passed(g, sg, n_peers=len(order)):
                continue  # already green
            mode = g.reviewer or "other"
            if mode in ("other", "both", "quorum"):
                # Any non-author peer (i.e. anyone whose turn it currently
                # isn't, but tracking the "author" perspective is the
                # job of the consensus tally; here we just say "current
                # peer is eligible to review while it's their turn").
                out.append(g)
            elif mode == "alternating":
                # The current peer is eligible iff their index matches
                # the rotating reviewer slot.
                idx = self._alternating_reviewer_index(state, g)
                if idx is not None and order[idx] == current_peer:
                    out.append(g)
        return out

    def _alternating_reviewer_index(self, state: dict[str, Any],
                                    g: Goal) -> int | None:
        """For reviewer=alternating: tracks a per-goal rotating cursor
        in state['soft_status'][g.id]['alt_cursor']. Advances after
        each successful review (see _record_soft_review_from_commit).
        """
        sg = state.setdefault("soft_status", {}).setdefault(
            g.id,
            {"consensus_count": 0, "last_pass": None,
             "history": [], "alt_cursor": 0},
        )
        cursor = sg.get("alt_cursor", 0)
        n = len(state["peer_order"])
        if n <= 0:
            return None
        return cursor % n

    def _soft_goal_passed(self, g: Goal, sg: dict[str, Any],
                          n_peers: int) -> bool:
        """Centralizes "is this soft goal considered green" given the
        reviewer mode."""
        mode = g.reviewer or "other"
        if mode == "quorum":
            assert g.quorum_num and g.quorum_den, "quorum without N/M"
            # Last `quorum_den` reviews must contain ≥ quorum_num pass.
            recent = sg.get("history", [])[-g.quorum_den:]
            if len(recent) < g.quorum_den:
                return False
            return sum(1 for r in recent if r.get("pass")) >= g.quorum_num
        if mode == "both":
            # Every peer must have submitted `consensus_needed`
            # consecutive pass:true reviews. With n=2 that literally
            # means "both peers reviewed"; for n>2 it means "all peers
            # reviewed" (the mode's name is preserved but its semantics
            # generalise to n peers).
            per_peer = sg.get("per_peer", {})
            need = g.consensus_needed
            reviewers_needed = max(n_peers, 1)
            sufficient_reviewers = sum(
                1 for v in per_peer.values()
                if v.get("consensus_count", 0) >= need
            )
            return sufficient_reviewers >= reviewers_needed
        # other / alternating: a single rolling counter.
        return sg.get("consensus_count", 0) >= g.consensus_needed

    def _all_green_including_soft(self, state: dict[str, Any]) -> bool:
        """All hard gates pass AND (optionally) all soft goals have consensus.

        Item 8 escape valve: when state.config.goals.soft_consensus_required
        is false, only hard gates need to be green. v9 + v10 ended
        budget:max_runtime because soft consensus was never reached even
        though hard gates were stable green for many ticks. Operators who
        want to ship as soon as hard gates settle can opt out.
        """
        if not self.engine.all_green():
            return False
        if not soft_consensus_required_for_convergence(state):
            return True
        n = len(state["peer_order"])
        for g in self.goals:
            if g.type != "soft":
                continue
            sg = state.get("soft_status", {}).get(g.id, {})
            if not self._soft_goal_passed(g, sg, n_peers=n):
                return False
        return True

    def _record_soft_review_from_commit(self, state: dict[str, Any],
                                        commit, reviewer: str) -> bool:
        """G4: a peer can ship a soft review by committing with body
        containing `## Review` plus a `Peer-Review-Of: <goal_id>`
        trailer. The body must be parseable as JSON (one block).

        Parsing failures used to be silent — the peer would never
        learn why their review didn't count. We now surface each
        failure as a warning that lands in the next prompt.

        Returns True when the review is ingested into
        `soft_status[gid].history`, False when the commit is not a
        review (no trailer), targets an unknown soft goal, or carries
        no parseable JSON. `_post_run` reads this return value to keep
        the runs.jsonl `soft_reviews_ingested` counter accurate even
        when the history list is at its 20-entry cap (where a
        delta-of-lengths reads as 0 after the trim).
        """
        goal_id = commit.trailers.get("Peer-Review-Of")
        if not goal_id:
            return False
        target_goal = next(
            (g for g in self.goals if g.id == goal_id and g.type == "soft"),
            None,
        )
        if target_goal is None:
            state.setdefault("warnings", []).append(
                f"soft-review ignored: commit {commit.sha[:8]} carries "
                f"Peer-Review-Of: {goal_id!r} but no soft goal with "
                "that id exists in goals.yaml."
            )
            return False
        # Extract first JSON object from body.
        #
        # the old regex
        # `\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}` only handled ONE level of
        # brace nesting, so a soft review carrying a structured payload
        # like `{"pass": true, "details": {"by_section": {...}}}` was
        # silently rejected. Use a brace-counter so arbitrary nesting
        # works (same logic as bug_hunt._first_json_block).
        body = commit.body
        payload = _extract_first_json_object(body)
        if payload is None:
            state.setdefault("warnings", []).append(
                f"soft-review ignored: commit {commit.sha[:8]} for "
                f"goal {goal_id!r} has no parseable JSON object in body. "
                "Re-emit as a fresh commit with a single `{...}` block."
            )
            return False
        passed = bool(payload.get("pass"))
        soft = state.setdefault("soft_status", {}).setdefault(
            goal_id,
            {"consensus_count": 0, "last_pass": None,
             "history": [], "alt_cursor": 0, "per_peer": {}},
        )
        mode = target_goal.reviewer or "other"

        # Rolling counter (used ONLY by other/alternating). Bumping it
        # in `both`/`quorum` mode would leak stale "green" state if the
        # user later edits the goal to `reviewer: other`.
        if mode in ("other", "alternating"):
            if passed:
                if soft.get("last_pass") is True:
                    soft["consensus_count"] = (
                        soft.get("consensus_count", 0) + 1
                    )
                else:
                    soft["consensus_count"] = 1
                soft["last_pass"] = True
            else:
                soft["consensus_count"] = 0
                soft["last_pass"] = False

        # Per-peer counter (used by `both`).
        per_peer = soft.setdefault("per_peer", {})
        pp = per_peer.setdefault(reviewer, {"consensus_count": 0,
                                            "last_pass": None})
        if passed:
            if pp.get("last_pass") is True:
                pp["consensus_count"] = pp.get("consensus_count", 0) + 1
            else:
                pp["consensus_count"] = 1
            pp["last_pass"] = True
        else:
            pp["consensus_count"] = 0
            pp["last_pass"] = False

        # Alternating cursor advances on every recorded review (pass
        # or fail), so duty rotates regardless of outcome.
        if mode == "alternating":
            n = len(state.get("peer_order") or [])
            if n > 0:
                soft["alt_cursor"] = (soft.get("alt_cursor", 0) + 1) % n

        soft.setdefault("history", []).append({
            "pass": passed,
            "reviewer": reviewer,
            "sha": commit.sha,
            "notes": payload.get("notes", ""),
        })
        # Keep history bounded.
        soft["history"] = soft["history"][-20:]
        return True
