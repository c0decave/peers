"""The opt-in **landing** Fixer for bring-up — the ``n>=2 diagnose -> verify ->
implement`` collaborator that :class:`~peers.modes.bring_up.frontend.EscalateOnlyFixer`
deliberately is NOT.

Where the default Fixer only classifies + escalates (never writes to the
tool-under-test), :class:`LandingFixer` closes the loop: it root-cause-diagnoses
a failure, **adversarially verifies the diagnosis BEFORE any code is written**
(so a mislabelled / refuted "bug" is never fixed into the tool), then drives the
implement step that lands + attests the fix and reports its real ``head_sha``.

Honesty contract (mirrors develop's AUDIT->VERIFY->AUTHOR->IMPLEMENT seam and
the reproduce engine's attested-confirm; the order is load-bearing):

  1. a failure already classified ``corpus-error`` is NEVER fixed into the tool;
  2. a root-cause diagnosis of ``corpus-error`` (a mislabelled ground-truth) is
     filed against the corpus, never "fixed" into the tool;
  3. a ``tool-bug`` diagnosis must SURVIVE adversarial verification (k refuters)
     — checked BEFORE implement, so refuters can stop a non-bug becoming a commit;
  4. only an implement step that produced a real landed commit (a ``head_sha``)
     is a landing; anything else is an honest non-landing.

The result is consumed by :meth:`BringUpFrontend._fix`, which re-validates the
sha as a SECOND, independent gate (``resolves_to_commit`` + ``resolve_author``):
a Fixer that lies about landing or returns a fake/unattested sha writes nothing.

Collaborators are injected (the LLM/develop/verify steps), so this module stays
loop- and LLM-agnostic and is unit-tested as pure orchestration. The concrete
wiring (reusing develop's implementer + ``spine.adversarial_verify`` + attest)
lives in :func:`peers.modes.bring_up.assembly.make_landing_fixer`.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .frontend import FixResult
from .models import Case
from .oracle import Judgment


@dataclass(frozen=True)
class Diagnosis:
    """The root-cause verdict + the brief handed to the implement step.

    ``root_cause`` is one of ``"tool-bug"`` (a genuine defect in the
    tool-under-test) or ``"corpus-error"`` (the corpus ground-truth is wrong).
    Any other value is treated as undiagnosed and fails CLOSED (no landing).
    """

    root_cause: str
    brief: str
    confidence: str = ""


@dataclass(frozen=True)
class FixAttempt:
    """What the implement step reports: the landed+attested commit sha, or
    ``None`` when no fix was produced (an honest give-up, not a landing)."""

    head_sha: str | None
    detail: str = ""


# Injected collaborator signatures (documented for callers / make_landing_fixer):
Diagnoser = Callable[[Case, Judgment, Any], Diagnosis]
Verifier = Callable[[Case, Diagnosis, Any], bool]
Implementer = Callable[[Case, Diagnosis, Any], FixAttempt]


class LandingFixer:
    """A live :class:`~peers.modes.bring_up.frontend.Fixer`: diagnose ->
    adversarially verify the diagnosis -> implement (land + attest)."""

    def __init__(
        self,
        *,
        diagnose: Diagnoser,
        verify: Verifier,
        implement: Implementer,
    ) -> None:
        self._diagnose = diagnose
        self._verify = verify
        self._implement = implement

    def fix(self, case: Case, judgment: Judgment, run: Any) -> FixResult:
        # (1) a pre-classified corpus-error is never fixed into the tool.
        if judgment.bug_kind == "corpus-error":
            return FixResult(landed=False, hint="corpus-error: not landed into tool")

        # (2) root-cause it. A diagnosed corpus-error is filed, not fixed.
        diag = self._diagnose(case, judgment, run)
        if diag.root_cause == "corpus-error":
            return FixResult(
                landed=False, hint=f"diagnosed corpus-error: {diag.brief[:80]}")
        if diag.root_cause != "tool-bug":
            return FixResult(
                landed=False, hint=f"undiagnosed root-cause {diag.root_cause!r}")

        # (3) verify the diagnosis BEFORE writing any code — refuters can stop a
        # non-bug from ever becoming a commit in the tool.
        if not self._verify(case, diag, run):
            return FixResult(
                landed=False, hint="diagnosis refuted by adversarial verify")

        # (4) implement: lands + attests, reports the real head_sha (or None).
        attempt = self._implement(case, diag, run)
        if not attempt.head_sha:
            return FixResult(
                landed=False, hint=f"no fix produced: {attempt.detail[:80]}")
        return FixResult(
            landed=True, head_sha=attempt.head_sha, hint=f"landed: {diag.brief[:60]}")
