"""Phase-6 — BringUpFrontend: the operator-launchable spine ModeFrontend.

Wires the corpus adapter (P1), tool-driver runner (P2), layered oracle (P3), loop
state machine (P4), and cross-run memory (P5) onto the spine ``drive()`` loop.
This is the FIRST operator-launchable ``drive()`` entry — it closes the
no-operator-entry orphan the 2026-06-12 plans analysis flagged.

Per-round: ``run()`` drives ONE loop step. A landed fix is recorded as a
substrate-attested ``confirmed-work`` row (real progress — resets stop-on-dry);
every other outcome (no-fix / escalated / converged / idle) is a ``dry-round``,
so the run terminates on stop-on-dry once it converges or genuinely stalls. A
case that passes a real oracle sweep records a memory FACT at the live tool-sha.
"""
from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from peers.spine.authorship import resolve_author
from peers.spine.gates import resolves_to_commit
from peers.spine.mode_run import ModeRun

from .loop import BringUpLoop, RoundOutcome
from .manifest import BringUpManifest
from .models import Case, require_unique_case_ids
from .oracle import Judgment, adjudicate


@dataclass(frozen=True)
class FixResult:
    """What a Fixer reports: did it land an attested fix, plus an optional hint."""

    landed: bool
    head_sha: str | None = None
    hint: str | None = None


@runtime_checkable
class Fixer(Protocol):
    """The n>=2 diagnose -> fix -> land collaborator (the LLM step). A real Fixer
    LANDS + ATTESTS its fix and returns its head sha; a needs-adjudication it
    concludes is a corpus-error returns ``landed=False`` (a mislabelled
    ground-truth is never 'fixed' into the tool)."""

    def fix(self, case: Case, judgment: Judgment, run: ModeRun) -> FixResult:
        ...


class EscalateOnlyFixer:
    """The default Fixer: classify + escalate, never land.

    A run with this Fixer is an *observe-and-report* pass — it sweeps the
    corpus, classifies every case, and escalates failures for a human/agent to
    triage, without ever writing to the tool. The operator opts into a live
    landing Fixer (reusing develop/implement + adversarial_verify) when they
    want the loop to actually fix; until then this is the safe default. Pair it
    with :meth:`BringUpFrontend.sweep_and_report` (one honest pass) rather than
    the iterative loop, whose stop-on-dry would otherwise cut the pass short.
    """

    def fix(self, case: Case, judgment: Judgment, run: ModeRun) -> FixResult:
        kind = getattr(judgment, "bug_kind", None) or "unknown"
        return FixResult(landed=False, hint=f"escalate-only: {kind}")


def _git_head_sha(repo: Path) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = r.stdout.strip().lower()
    return sha if r.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else None


class BringUpFrontend:
    def __init__(self, manifest: BringUpManifest, *, cases: list[Case], runner,
                 oracles: list, fixer: Fixer, memory=None,
                 head_sha: Callable[[Path], str | None] | None = None,
                 is_escalate_only: bool = False) -> None:
        self._manifest = manifest
        self._cases = list(cases)
        self._runner = runner
        self._oracles = list(oracles)
        self._fixer = fixer
        self._memory = memory
        self._head_sha = head_sha or _git_head_sha
        self._is_escalate_only = is_escalate_only
        self._loop: BringUpLoop | None = None
        self._run: ModeRun | None = None
        self._last: RoundOutcome | None = None

    def _active_run(self) -> ModeRun:
        """The ModeRun set by prepare()/run(). The sweep/fix callbacks only fire
        from inside BringUpLoop.step(), which run() calls AFTER assigning
        self._run — so a None here means a callback was driven out of order
        (before prepare()/run()). Surface it fail-closed as a clear error rather
        than an opaque None-attribute crash deep in _work()/append_attested()."""
        run = self._run
        if run is None:
            raise RuntimeError("BringUpFrontend used before prepare()/run()")
        return run

    def _work(self, run: ModeRun) -> Path:
        d = run.tool / ".peers" / "bringup-work"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _sweep(self, case: Case) -> Judgment:
        run = self._active_run()
        obs = self._runner.run(case, work=self._work(run), run_id=run.mode_run)
        verdicts = [o.judge(case, obs, work=self._work(run)) for o in self._oracles]
        j = adjudicate(case, verdicts)
        if j.passed and self._memory is not None:
            sha = self._head_sha(run.tool)
            if sha:
                self._memory.record_fact(case.id, sha)
        return j

    def _fix(self, case: Case, judgment: Judgment) -> bool:
        run = self._active_run()
        res = self._fixer.fix(case, judgment, run)
        if self._memory is not None and res.hint:
            self._memory.record_hint(case.id, self._head_sha(run.tool) or "", res.hint)
        # Defense-in-depth: only record an attested
        # confirmed-work row for a fix that is a REAL commit AND substrate-attested,
        # with a witness the spine gates can re-derive (kind=git-sha). A Fixer that
        # lies about landing / returns a fake or unattested sha writes nothing.
        if (res.landed and res.head_sha
                and resolves_to_commit(run.tool, res.head_sha)
                and resolve_author(run.tool, res.head_sha) is not None):
            run.ledger.append_attested(
                run.tool, res.head_sha, event="confirmed-work", status="pass",
                subject=case.id,
                witness={"kind": "git-sha", "uri": res.head_sha,
                         "sha256": res.head_sha, "case": case.id,
                         "bug_kind": judgment.bug_kind},
                independence=True, mode_run=run.mode_run)
            return True
        return False

    def prepare(self, run: ModeRun) -> None:
        self._run = run
        self._loop = BringUpLoop(
            self._cases, sweep_one=self._sweep, fix_one=self._fix,
            per_case_fix_budget=self._manifest.budget.per_case_fix_budget)

    def run(self, run: ModeRun) -> None:
        self._run = run
        if self._loop is None:
            raise RuntimeError("BringUpFrontend.run() called before prepare()")
        out = self._loop.step()
        self._last = out
        if out.kind != "fixed":   # a fixed round already wrote an attested row
            run.ledger.append(
                event="dry-round", status="dry",
                witness={"kind": "dry", "reason": out.kind, "detail": out.detail},
                mode_run=run.mode_run)

    def interpret(self, run: ModeRun) -> dict:
        summary = self._loop.summary() if self._loop else {}
        summary["last"] = self._last.kind if self._last else None
        return summary

    def sweep_and_report(self, run: ModeRun) -> dict:
        """One honest pass: classify EVERY case once and terminate 'complete'.

        The escalate-only entry. The iterative ``drive()`` loop cannot be used
        for escalate-only because, with a Fixer that never lands, every round
        is a dry-round and stop-on-dry (``dry_n``, default 3) fires BEFORE the
        per-case fix budget (default 10) escalates anything — so the run would
        stop with most cases unclassified and ``stuck == 0``, contradicting the
        "terminates honestly with stuck>0" intent (next-steps plan §3). This
        path sidesteps that interplay entirely: it sweeps each case once,
        classifies it (green / excluded corpus-error / escalated tool-bug or
        needs-adjudication), writes one ``bringup-verdict`` row per case, and
        ends with a single ``stop`` row. Fails CLOSED on duplicate case-ids.
        """
        self._run = run
        require_unique_case_ids(self._cases)
        counts = {"total": len(self._cases), "green": 0, "excluded": 0,
                  "escalated": 0}
        for case in self._cases:
            j = self._sweep(case)
            if j.passed:
                verdict = "green"
            elif j.bug_kind == "corpus-error":
                verdict = "excluded"
            else:
                verdict = "escalated"
            counts[verdict] += 1
            run.ledger.append(
                event="bringup-verdict", status=verdict, subject=case.id,
                witness={"kind": "bringup-verdict", "case": case.id,
                         "passed": j.passed, "bug_kind": j.bug_kind,
                         "signature": j.signature,
                         "failing_oracle": j.failing_oracle},
                mode_run=run.mode_run)
        run.ledger.append(
            event="stop", status="complete",
            witness={"kind": "bringup-sweep", "counts": counts},
            mode_run=run.mode_run)
        return {**counts, "converged": True, "mode": "escalate-only-sweep"}
