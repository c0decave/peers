from __future__ import annotations

import re
import sys
from typing import Any

from peers.driver_helpers import _extract_first_json_object
from peers.driver_host import _DriverHost
from peers.goals import Goal
from peers.regression_baseline import verify_baseline_digests


_SOFT_CONSENSUS_FALSY = frozenset({"false", "0", "no", "off", ""})
_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_REVIEW_SECTION_RE = re.compile(r"(?im)^##[ \t]+Review[ \t]*\r?$")


def _looks_like_git_sha(value: str) -> bool:
    return bool(_GIT_SHA_RE.fullmatch(value.strip()))


def _review_section_body(body: str) -> str | None:
    match = _REVIEW_SECTION_RE.search(body)
    if match is None:
        return None
    return body[match.end():]


def _default_soft_status() -> dict[str, Any]:
    return {
        "consensus_count": 0,
        "last_pass": None,
        "history": [],
        "alt_cursor": 0,
        "per_peer": {},
    }


def _soft_status_map(state: dict[str, Any]) -> dict[str, Any]:
    soft_status = state.get("soft_status", {})
    if isinstance(soft_status, dict):
        return soft_status
    return {}


def _mutable_soft_status_map(state: dict[str, Any]) -> dict[str, Any]:
    soft_status = state.get("soft_status")
    if isinstance(soft_status, dict):
        return soft_status
    soft_status = {}
    state["soft_status"] = soft_status
    return soft_status


def _soft_goal_status(soft_status: dict[str, Any], goal_id: str) -> dict[str, Any]:
    status = soft_status.get(goal_id, {})
    if isinstance(status, dict):
        return status
    return {}


def _config_goals_map(state: dict[str, Any]) -> dict[str, Any]:
    config = state.get("config", {})
    if not isinstance(config, dict):
        return {}
    goals = config.get("goals", {})
    if isinstance(goals, dict):
        return goals
    return {}


def _mutable_soft_goal_status(
    soft_status: dict[str, Any],
    goal_id: str,
) -> dict[str, Any]:
    status = soft_status.get(goal_id)
    if isinstance(status, dict):
        return status
    status = _default_soft_status()
    soft_status[goal_id] = status
    return status


def _valid_consensus_count(entry: object, need: int) -> bool:
    if not isinstance(entry, dict):
        return False
    count = entry.get("consensus_count", 0)
    return (
        isinstance(count, int)
        and not isinstance(count, bool)
        and count >= need
    )


def _next_consensus_count(entry: dict[str, Any]) -> int:
    count = entry.get("consensus_count", 0)
    if isinstance(count, int) and not isinstance(count, bool):
        return count + 1
    return 1


def soft_consensus_required_for_convergence(state: dict[str, Any]) -> bool:
    """Item 8: operators can disable the soft-consensus convergence gate
    via .peers/config.yaml -> goals.soft_consensus_required: false.

    Background: v9 + v10 both exited budget:max_runtime (not 'complete')
    because _all_green_including_soft required every soft goal to have
    peer-review consensus, even after all hard gates had been green for
    many ticks. Operators who want hard-gate-driven convergence can opt
    out. Default True preserves legacy strict semantics.
    """
    cfg_goals = _config_goals_map(state)
    raw = cfg_goals.get("soft_consensus_required", True)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() not in _SOFT_CONSENSUS_FALSY
    return bool(raw)


class DriverSoftReviewsMixin(_DriverHost):
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
        soft_status = _soft_status_map(state)
        for g in self.goals:
            if g.type != "soft":
                continue
            sg = _soft_goal_status(soft_status, g.id)
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
        soft_status = _mutable_soft_status_map(state)
        sg = _mutable_soft_goal_status(soft_status, g.id)
        cursor = sg.get("alt_cursor", 0)
        n = len(state["peer_order"])
        if n <= 0:
            return None
        if not isinstance(cursor, int) or isinstance(cursor, bool):
            cursor = 0
        return cursor % n

    def _soft_goal_passed(self, g: Goal, sg: dict[str, Any],
                          n_peers: int) -> bool:
        """Centralizes "is this soft goal considered green" given the
        reviewer mode."""
        if not isinstance(sg, dict):
            return False
        mode = g.reviewer or "other"
        if mode == "quorum":
            assert g.quorum_num and g.quorum_den, "quorum without N/M"
            # Last `quorum_den` reviews must contain ≥ quorum_num pass.
            history = sg.get("history", [])
            if not isinstance(history, list):
                history = []
            recent = history[-g.quorum_den:]
            if len(recent) < g.quorum_den:
                return False
            # full-depth-analysis #5: the quorum_num passes must come from
            # quorum_num DISTINCT reviewers — a single peer passing its own work
            # repeatedly does not form an independent quorum.
            distinct_passers = {
                r.get("reviewer") for r in recent
                if isinstance(r, dict) and r.get("pass") is True
                and r.get("reviewer") is not None
            }
            return len(distinct_passers) >= g.quorum_num
        if mode == "both":
            # Every peer must have submitted `consensus_needed`
            # consecutive pass:true reviews. With n=2 that literally
            # means "both peers reviewed"; for n>2 it means "all peers
            # reviewed" (the mode's name is preserved but its semantics
            # generalise to n peers).
            per_peer = sg.get("per_peer", {})
            if not isinstance(per_peer, dict):
                per_peer = {}
            need = g.consensus_needed
            reviewers_needed = max(n_peers, 1)
            sufficient_reviewers = sum(
                1 for v in per_peer.values()
                if _valid_consensus_count(v, need)
            )
            return sufficient_reviewers >= reviewers_needed
        # other / alternating: a single rolling counter.
        return _valid_consensus_count(sg, g.consensus_needed)

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
        # FU-1: refuse to converge if a peer forged an agent-writable baseline
        # (.peers/passing-baseline.txt / skip-baseline.txt) mid-run. The
        # run-start digest anchor lives in the orchestrator's process memory
        # (self._baseline_digests), unreachable by the agent subprocess, so a
        # forged on-disk baseline that made a HARD gate "pass" is caught here
        # and convergence is blocked. Applies regardless of soft-consensus
        # opt-out, since it guards a HARD-gate integrity property.
        anchored = getattr(self, "_baseline_digests", {})
        if anchored:
            forged = verify_baseline_digests(self.peer_dir, anchored)
            if forged:
                print(
                    "peers: REFUSING convergence — run-start baseline forged "
                    f"(digest mismatch) for gate(s): {', '.join(forged)}. The "
                    "no-prior-regression / no-skipped-tests guarantee for "
                    "these gates is void; failing closed.",
                    file=sys.stderr, flush=True,
                )
                return False
        if not soft_consensus_required_for_convergence(state):
            return True
        n = len(state["peer_order"])
        soft_status = _soft_status_map(state)
        for g in self.goals:
            if g.type != "soft":
                continue
            sg = _soft_goal_status(soft_status, g.id)
            if not self._soft_goal_passed(g, sg, n_peers=n):
                return False
        return True

    def _soft_goal_for_review_target(self, goal_id: str | None) -> Goal | None:
        if not goal_id:
            return None
        return next(
            (g for g in self.goals if g.id == goal_id and g.type == "soft"),
            None,
        )

    def _peer_review_trailer_is_soft_goal(self, goal_id: str | None) -> bool:
        """True when Peer-Review-Of should count as soft-review accounting."""
        if not goal_id:
            return False
        if self._soft_goal_for_review_target(goal_id) is not None:
            return True
        # The general peer protocol also uses Peer-Review-Of for product
        # code reviews targeted at commits. Those are not soft-goal attempts.
        return not _looks_like_git_sha(goal_id)

    def _record_soft_review_from_commit(self, state: dict[str, Any],
                                        commit, reviewer: str) -> bool:
        """G4: a peer can ship a soft review by committing with body
        containing `## Review` plus a `Peer-Review-Of: <goal_id>`
        trailer. SHA-shaped Peer-Review-Of values are product code-review
        targets, not soft reviews. The body must be parseable as JSON (one
        block).

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
        target_goal = self._soft_goal_for_review_target(goal_id)
        if target_goal is None:
            if _looks_like_git_sha(goal_id):
                return False
            state.setdefault("warnings", []).append(
                f"soft-review ignored: commit {commit.sha[:8]} carries "
                f"Peer-Review-Of: {goal_id!r} but no soft goal with "
                "that id exists in goals.yaml."
            )
            return False
        # Extract first JSON object from the required Review section.
        #
        # the old regex
        # `\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}` only handled ONE level of
        # brace nesting, so a soft review carrying a structured payload
        # like `{"pass": true, "details": {"by_section": {...}}}` was
        # silently rejected. Use a brace-counter so arbitrary nesting
        # works (same logic as bug_hunt._first_json_block).
        body = commit.body
        review_body = _review_section_body(body)
        if review_body is None:
            state.setdefault("warnings", []).append(
                f"soft-review ignored: commit {commit.sha[:8]} for "
                f"goal {goal_id!r} is missing a `## Review` section "
                "before its JSON object."
            )
            return False
        payload = _extract_first_json_object(review_body)
        if payload is None:
            state.setdefault("warnings", []).append(
                f"soft-review ignored: commit {commit.sha[:8]} for "
                f"goal {goal_id!r} has no parseable JSON object in body. "
                "Re-emit as a fresh commit with a single `{...}` block."
            )
            return False
        pass_value = payload.get("pass")
        if not isinstance(pass_value, bool):
            state.setdefault("warnings", []).append(
                f"soft-review ignored: commit {commit.sha[:8]} for "
                f"goal {goal_id!r} has non-boolean `pass` value "
                f"{pass_value!r}. Re-emit with pass:true or pass:false."
            )
            return False
        passed = pass_value
        soft_status = _mutable_soft_status_map(state)
        soft = _mutable_soft_goal_status(soft_status, goal_id)
        mode = target_goal.reviewer or "other"

        # Rolling counter (used ONLY by other/alternating). Bumping it
        # in `both`/`quorum` mode would leak stale "green" state if the
        # user later edits the goal to `reviewer: other`.
        if mode in ("other", "alternating"):
            if passed:
                # full-depth-analysis #5: only ADVANCE consensus when the new pass
                # comes from a DIFFERENT reviewer than the prior counted pass — else
                # ONE peer reviewing its OWN work across consecutive turns (the other
                # peer benched/degraded) self-satisfies the n>=2 independent-review
                # premise. A same-reviewer repeat resets to a single distinct vote.
                if (soft.get("last_pass") is True
                        and soft.get("last_reviewer") != reviewer):
                    soft["consensus_count"] = _next_consensus_count(soft)
                else:
                    soft["consensus_count"] = 1
                soft["last_pass"] = True
            else:
                soft["consensus_count"] = 0
                soft["last_pass"] = False
            soft["last_reviewer"] = reviewer

        # Per-peer counter (used by `both`).
        per_peer = soft.get("per_peer", {})
        if not isinstance(per_peer, dict):
            per_peer = {}
            soft["per_peer"] = per_peer
        pp = per_peer.setdefault(reviewer, {"consensus_count": 0,
                                            "last_pass": None})
        if not isinstance(pp, dict):
            pp = {"consensus_count": 0, "last_pass": None}
            per_peer[reviewer] = pp
        if passed:
            if pp.get("last_pass") is True:
                pp["consensus_count"] = _next_consensus_count(pp)
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
                cursor = soft.get("alt_cursor", 0)
                if not isinstance(cursor, int) or isinstance(cursor, bool):
                    cursor = 0
                soft["alt_cursor"] = (cursor + 1) % n

        history = soft.get("history", [])
        if not isinstance(history, list):
            history = []
        history.append({
            "pass": passed,
            "reviewer": reviewer,
            "sha": commit.sha,
            "notes": payload.get("notes", ""),
        })
        # Keep history bounded.
        soft["history"] = history[-20:]
        return True
