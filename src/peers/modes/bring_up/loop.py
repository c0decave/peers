"""Phase-4 — the bring-up loop state machine (pure, git-free).

``step()`` does a FULL re-sweep of every non-terminal case each round
(regression-complete: a fix that breaks a previously-green case is caught next
round), classifies failures, and tries to fix the worst one. Terminal states:
 - green     — the case passes the oracle this sweep,
 - excluded  — a corpus-error (never fixed into the tool; filed against the corpus),
 - stuck     — its per-case fix budget is exhausted (escalated).
Convergence = a full sweep with no non-terminal failure left. ``sweep_one`` and
``fix_one`` are injected; the spine/ledger/landing wiring lives in the frontend.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .models import Case, require_unique_case_ids
from .oracle import Judgment

_FAIL_PRIORITY = {"tool-bug": 0, "needs-adjudication": 1}


@dataclass
class RoundOutcome:
    kind: str                 # converged | fixed | no-fix | escalated | idle
    detail: str = ""
    converged: bool = False
    fixed: str | None = None
    newly_green: list[str] = field(default_factory=list)
    newly_excluded: list[str] = field(default_factory=list)
    newly_stuck: list[str] = field(default_factory=list)


class BringUpLoop:
    def __init__(
        self,
        cases: list[Case],
        *,
        sweep_one: Callable[[Case], Judgment],
        fix_one: Callable[[Case, Judgment], bool],
        per_case_fix_budget: int = 10,
    ) -> None:
        self._cases = list(cases)
        require_unique_case_ids(self._cases)
        self._by_id = {c.id: c for c in self._cases}
        self._sweep_one = sweep_one
        self._fix_one = fix_one
        self._budget = per_case_fix_budget
        self._fix_attempts: dict[str, int] = {}
        self.green: set[str] = set()
        self.excluded: set[str] = set()
        self.stuck: set[str] = set()
        self.converged = False

    def _terminal(self, cid: str) -> bool:
        return cid in self.excluded or cid in self.stuck

    def _has_pending(self) -> bool:
        return any(c.id not in self.green and not self._terminal(c.id)
                   for c in self._cases)

    def _state_str(self) -> str:
        return (f"{len(self.green)} green / {len(self.excluded)} excluded / "
                f"{len(self.stuck)} stuck")

    def step(self) -> RoundOutcome:
        if self.converged:
            return RoundOutcome("idle", "converged", converged=True)

        # FULL re-sweep of every non-terminal case (regression-complete).
        results: dict[str, Judgment] = {}
        for case in self._cases:
            if self._terminal(case.id):
                continue
            results[case.id] = self._sweep_one(case)
        self.green = {cid for cid, j in results.items() if j.passed}

        newly_excluded: list[str] = []
        for cid, j in results.items():
            if not j.passed and j.bug_kind == "corpus-error":
                self.excluded.add(cid)
                newly_excluded.append(cid)

        failures = [
            (self._by_id[cid], j) for cid, j in results.items()
            if not j.passed and j.bug_kind != "corpus-error"
        ]
        if not failures:
            self.converged = True
            return RoundOutcome("converged", self._state_str(),
                                converged=True, newly_green=sorted(self.green),
                                newly_excluded=newly_excluded)

        case, j = min(failures, key=lambda cf: _FAIL_PRIORITY.get(cf[1].bug_kind, 2))

        # anti-thrash: escalate a case whose fix budget is exhausted.
        if self._fix_attempts.get(case.id, 0) >= self._budget:
            self.stuck.add(case.id)
            converged = not self._has_pending()
            self.converged = converged
            return RoundOutcome("escalated", f"{case.id}:{j.bug_kind}",
                                converged=converged, newly_stuck=[case.id],
                                newly_excluded=newly_excluded,
                                newly_green=sorted(self.green))

        self._fix_attempts[case.id] = self._fix_attempts.get(case.id, 0) + 1
        made = self._fix_one(case, j)
        return RoundOutcome(
            "fixed" if made else "no-fix", f"{case.id}:{j.bug_kind}:{j.signature}",
            converged=False, fixed=case.id if made else None,
            newly_green=sorted(self.green), newly_excluded=newly_excluded)

    def summary(self) -> dict:
        pending = [c.id for c in self._cases
                   if c.id not in self.green and not self._terminal(c.id)]
        return {
            "total": len(self._cases),
            "green": len(self.green),
            "excluded": len(self.excluded),
            "stuck": len(self.stuck),
            "pending": len(pending),
            "converged": self.converged,
        }
