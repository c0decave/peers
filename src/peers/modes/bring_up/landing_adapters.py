"""Production LLM + convergence adapters for the bring-up **landing** Fixer.

:func:`peers.modes.bring_up.assembly.make_landing_fixer` wires the real
``verify_claim`` but leaves its three irreducibly-LLM collaborators INJECTED:
``diagnose``, ``refuter_factory``, and ``implement``. This module supplies the
production implementations so ``peers bring-up --fixer landing`` is reachable,
mirroring develop's adapters (fail-closed JSON parsing) and REUSING develop's
:class:`~peers.develop.convergence.AgentConvergenceRunner` for the implement step
(a landing requires a REAL diff + acceptance pass + a substrate-attest note — the
runner cannot manufacture a confirmed commit, and bring-up's frontend re-validates
the returned sha as a second independent gate).

Honesty posture (mirrors develop): every adapter fails CLOSED — an agent error,
non-JSON, or a malformed reply yields the SAFE outcome (an UNDIAGNOSED diagnosis
that licenses no fix; a REFUTED vote; a None head_sha), never a fabricated
tool-bug, a survived refutation, or a forged landing.

KNOWN LIMITATIONS (S3 adversarial review — honest disclosure, not silently shipped):

* The implement step converges on the case's ORACLE verdict. The landing fixer is
  therefore only as trustworthy as the oracle. A ``differential`` oracle that
  reports the tool's SELF-REPORTED status (e.g. a ``findings.sqlite3`` row) can in
  principle be GAMED by the implement agent writing that verdict artifact directly
  instead of fixing the tool — the project's own bring-up design doc flags this as
  a critical seam and mandates the differential adapter require INDEPENDENT evidence
  (a proof-bundle / replay-sha / a real sanitizer crash), which is NOT yet
  implemented. Until it is: use ``--fixer landing`` with a ``runtime`` /
  ``test-suite`` oracle whose verdict re-derives from the tool's actual behaviour
  each run (the runner re-runs the driver before the oracle reads, so a
  regenerating driver clobbers any hand-edited verdict), OR ensure the differential
  driver fully regenerates its verdict store from the tool. (The vacuous-green guard
  + ``.peers`` exclusion stop scratch-only / empty-diff forges; they do NOT vet that
  a real diff is in tool SOURCE vs a verdict artifact.)
* Single-repo ``peers bring-up --fixer landing`` records confirmed-work via the
  frontend's write-time gate (real commit + ``resolve_author`` non-None) and the
  AgentConvergenceRunner attests the real committed range, so the row is reachable +
  attested under honest operation. It does NOT additionally run the spine's
  post-run ``evaluate_spine_gates`` (the HONEST-01 reachability re-derivation) — a
  defense-in-depth asymmetry vs the fleet/auto-merge landing path, consistent with
  the single-repo ``peers develop`` / ``peers research`` CLIs. Non-exploitable
  today; a candidate hardening for a future pass.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from peers.develop.adapters import RunAgent, _extract_json_object
from peers.develop.convergence import AgentConvergenceRunner

from .fixer import Diagnosis, FixAttempt
from .models import Case
from .oracle import Judgment

#: ``sweep(case, workdir, run_id) -> Judgment`` — re-run ONE case through the tool
#: driver + oracles in ``workdir`` and adjudicate. This is the implement step's
#: acceptance: a fix converges only when the case the loop is failing on now PASSES
#: the SAME judgment the loop uses, so the fixer can never "converge" on a different
#: bar. ``run_id`` is the loop's ``run.mode_run`` (NOT a literal) so a driver whose
#: command template references ``{run}`` is judged on the identical run identity the
#: loop uses (S3 review finding: a hardcoded run_id was a latent divergent-bar bug).
SweepOne = Callable[[Case, Path, str], Judgment]

#: ``run_agent(prompt, workdir) -> text`` — one implement turn editing ``workdir``.
RunAgentInDir = Callable[[str, Path], str]

#: Root-cause labels the LandingFixer acts on; anything else is undiagnosed and
#: fails closed (no landing). Kept here so the parser and the fixer agree.
_UNDIAGNOSED = "undiagnosed"


class LLMDiagnoser:
    """Root-cause a failing bring-up case via an injected agent.

    Returns ``Diagnosis(root_cause=tool-bug|corpus-error, brief=...)`` on a clean
    reply; ANY error / non-JSON / missing field / unknown root-cause yields
    ``root_cause='undiagnosed'`` (the LandingFixer then lands nothing). It NEVER
    defaults to ``tool-bug`` — an unparseable diagnosis must not license a fix.
    """

    def __init__(self, *, run_agent: RunAgent) -> None:
        self.run_agent = run_agent

    def _build_prompt(self, case: Case, judgment: Judgment) -> str:
        return (
            "A bring-up corpus case is FAILING against the tool-under-test. "
            "Root-cause it: is this a genuine defect in the TOOL ('tool-bug') or "
            "is the corpus ground-truth itself wrong ('corpus-error')?\n"
            f"- case id: {case.id}\n- failing oracle: {judgment.failing_oracle}\n"
            f"- bug kind so far: {judgment.bug_kind}\n- signature: {judgment.signature}\n\n"
            'Respond with ONLY a JSON object: {"root_cause": "tool-bug"|"corpus-error", '
            '"brief": "<one-line root cause + the concrete fix to make>", '
            '"confidence": "high"|"medium"|"low"}. Ground it in the actual tool code; '
            "if you cannot, say corpus-error or omit the field."
        )

    def diagnose(self, case: Case, judgment: Judgment, run: Any) -> Diagnosis:
        try:
            raw = self.run_agent(self._build_prompt(case, judgment))
        except Exception:  # noqa: BLE001 — adapter boundary: never raise into the loop
            return Diagnosis(root_cause=_UNDIAGNOSED, brief="diagnoser error")
        obj = _extract_json_object(raw)
        if not isinstance(obj, dict):
            return Diagnosis(root_cause=_UNDIAGNOSED, brief="non-JSON diagnosis")
        root = obj.get("root_cause")
        brief = obj.get("brief")
        if root not in ("tool-bug", "corpus-error"):
            return Diagnosis(root_cause=_UNDIAGNOSED, brief="unknown root-cause")
        # A tool-bug with no actionable brief cannot drive an implement -> fail closed.
        # (A corpus-error needs no fix brief, but require a non-empty one anyway so a
        # blank reply is never read as a confident verdict.)
        if not isinstance(brief, str) or not brief.strip():
            return Diagnosis(root_cause=_UNDIAGNOSED, brief="no brief")
        confidence = obj.get("confidence")
        return Diagnosis(
            root_cause=root, brief=brief.strip(),
            confidence=confidence if isinstance(confidence, str) else "")


class LLMDiagnosisRefuter:
    """Adversarial refuter factory for the landing Fixer's verify seam.

    Asks an injected agent to REFUTE a ``tool-bug`` diagnosis before any code is
    written. Fail-closed: an error, non-JSON, an ambiguous reply, or a
    missing/non-bool ``refuted`` key all count as REFUTED (True), so a diagnosis
    survives only on a clear ``{"refuted": false}``. Mirrors develop's LLMRefuter.
    """

    def __init__(self, *, run_agent: RunAgent) -> None:
        self.run_agent = run_agent

    def _build_prompt(self, case: Case, diagnosis: Diagnosis) -> str:
        return (
            "Try to REFUTE this root-cause diagnosis of a failing tool case by "
            "checking it against the actual tool code. Is it a real tool defect, "
            "or is the diagnosis wrong / not applicable / actually a corpus error?\n"
            f"- case id: {case.id}\n- diagnosed root cause: {diagnosis.root_cause}\n"
            f"- brief: {diagnosis.brief}\n\n"
            'Respond with ONLY a JSON object {"refuted": true|false} where '
            "refuted=true means it is NOT a real tool defect worth fixing. If you "
            "are unsure, refuted=true."
        )

    def refuter_factory(self, case: Case, diagnosis: Diagnosis):
        """Return ``refuter(vote_index) -> bool`` (True == refuted) for ``verify_claim``."""
        prompt = self._build_prompt(case, diagnosis)

        def _refute(_vote_index: int) -> bool:
            try:
                raw = self.run_agent(prompt)
            except Exception:  # noqa: BLE001 — fail-closed: unverifiable -> refuted
                return True
            obj = _extract_json_object(raw)
            if not isinstance(obj, dict) or not isinstance(obj.get("refuted"), bool):
                return True
            return obj["refuted"]

        return _refute


def make_landing_implement(
    *,
    impl_run_agent: RunAgentInDir,
    sweep: SweepOne,
    attest_peer: str,
    budget: int = 5,
):
    """Build the LandingFixer ``implement(case, diagnosis, run) -> FixAttempt``.

    Reuses develop's :class:`AgentConvergenceRunner`: it drives ``impl_run_agent``
    inside ``run.tool`` until the case's acceptance (``sweep(case, workdir).passed``)
    passes on a REAL committed diff, attests the commit to ``attest_peer``, and
    returns its head sha. A budget-exhausted or vacuous-green (empty-diff) attempt
    returns ``head_sha=None`` — an honest non-landing, never a forged commit. The
    bring-up frontend re-validates the sha (resolves_to_commit + resolve_author) as
    a second, independent gate.
    """
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1:
        raise ValueError(f"make_landing_implement: budget must be int >= 1 (got {budget!r})")
    if not isinstance(attest_peer, str) or not attest_peer.strip():
        raise ValueError("make_landing_implement: attest_peer must be a non-empty string")

    def implement(case: Case, diagnosis: Diagnosis, run: Any) -> FixAttempt:
        # Use the loop's own run identity for the acceptance sweep so the implement
        # bar is the SAME judgment the loop converges on (S3 review #4).
        run_id = getattr(run, "mode_run", None) or "landing-acceptance"

        def run_acceptance(workdir) -> tuple[bool, str]:
            try:
                j = sweep(case, Path(workdir), run_id)
            except Exception as e:  # noqa: BLE001 — a sweep crash is a non-pass, not a run crash
                return (False, f"acceptance sweep raised: {e}")
            return (bool(j.passed), j.signature or j.bug_kind or "")

        runner = AgentConvergenceRunner(
            run_agent=impl_run_agent,
            run_acceptance=run_acceptance,
            budget=budget,
            attest_peer=attest_peer,
            contract_md=diagnosis.brief,
            commit_message=f"bring-up: fix {case.id}",
            # S3 review #1 (defense-in-depth): exclude peers' own metadata tree from
            # the convergence diff so a fix that only writes under .peers/ (e.g. the
            # acceptance scratch .peers/bringup-work, or memory/ledger) can NEVER
            # satisfy the vacuous-green guard or ride into the commit. A real tool
            # fix touches the TOOL's source, never .peers/. This makes the guard
            # independent of the target's .gitignore (mirrors develop's exclude=).
            exclude=(".peers",),
        )
        try:
            converged, head_sha, _branch = runner(run.tool)
        except Exception as e:  # noqa: BLE001 — a convergence crash is an honest no-landing
            return FixAttempt(head_sha=None, detail=f"implement crashed: {e}")
        return FixAttempt(
            head_sha=head_sha if converged else None,
            detail=f"converged={converged}")

    return implement
