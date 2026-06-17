"""STEP-6 — the real Implementer adapter (thin; wraps the implement contract).

:class:`ContractImplementer` is the production :class:`peers.develop.ports.Implementer`:
it takes an :class:`~peers.develop.ports.AuthoredContract`, **validates** it with
``peers_ctl.plan_parser.parse_plan`` and **freezes** it with
``peers_ctl.contracts.write_frozen_contracts`` into an isolated scratch dir, then
hands that dir to an **injected** convergence runner and maps the result to an
:class:`~peers.develop.ports.ImplementResult`.

Why injected convergence: the live multi-agent implement loop is heavy and
non-deterministic; injecting ``run_convergence(project_dir) -> (ok, head_sha,
branch)`` keeps the adapter a deterministic, unit-testable seam (Stage 1) while a
Stage-5 worktree-isolated runner is dropped in unchanged later. The adapter's own
guarantee is narrow and load-bearing: **develop never edits freehand** — an
unparseable contract returns ``ImplementResult(ok=False, reason=...)`` and the
convergence runner is NEVER invoked (no work off an unvalidated plan).
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from peers_ctl.contracts import write_frozen_contracts
from peers_ctl.plan_parser import PlanValidationError, parse_plan

from peers.develop.ports import AuthoredContract, Finding, ImplementResult

#: A one-shot agent runner: ``run_agent(prompt) -> raw model text``. Injected so
#: the adapter is unit-testable with a fake; production wires it to a real
#: ``claude -p`` subprocess (see :func:`peers.agent_invoke.agent_runner_from_spec`).
RunAgent = Callable[[str], str]

_FINDING_FIELDS = ("id", "dimension", "severity", "location", "summary", "fix",
                   "fail_first")


def _extract_json_array(text: str) -> list | None:
    """Best-effort recovery of a JSON array from chatty model output. Tries a
    fenced ```json block first, then a bare ``[ ... ]`` span. Returns ``None``
    when nothing parses (the caller treats that as a dry round — never a
    fabricated finding)."""
    if not isinstance(text, str):
        return None
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    candidates = []
    if fence:
        candidates.append(fence.group(1))
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start:end + 1])
    for blob in candidates:
        try:
            parsed = json.loads(blob)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, list):
            return parsed
    return None


class LLMAuditor:
    """A production :class:`peers.develop.ports.Auditor` that asks an injected
    agent to audit a repo for the requested dimensions and parses its reply into
    :class:`Finding` objects.

    Fail-closed: a runner error, non-JSON output, or malformed entries yield an
    empty list (a dry round), never a fabricated or partial finding, and
    :meth:`audit` never raises into the spine ``drive`` loop."""

    def __init__(self, *, run_agent: RunAgent, max_findings: int = 20) -> None:
        if max_findings < 1:
            raise ValueError("max_findings must be >= 1")
        self.run_agent = run_agent
        self.max_findings = max_findings

    def _build_prompt(self, repo: Path, dimensions: list[str]) -> str:
        dims = ", ".join(dimensions)
        return (
            f"Audit the repository at {repo} for these dimensions: {dims}.\n"
            "Return ONLY a JSON array of findings (no prose). Each finding object "
            "must have exactly these string fields: "
            f"{', '.join(_FINDING_FIELDS)}.\n"
            "- id: a short stable handle (e.g. AUD-1)\n"
            "- dimension: one of the requested dimensions\n"
            "- severity: critical|high|medium|low\n"
            "- location: file:line\n"
            "- summary: the concrete defect\n"
            "- fix: the concrete change\n"
            "- fail_first: a test name that would fail today and pass once fixed\n"
            "Only report findings you can ground in the actual code. If you find "
            "nothing actionable, return []."
        )

    def audit(self, repo: Path, dimensions: list[str]) -> list[Finding]:
        try:
            raw = self.run_agent(self._build_prompt(Path(repo), dimensions))
        except Exception:  # noqa: BLE001 — adapter boundary: never raise into drive()
            return []
        parsed = _extract_json_array(raw)
        if parsed is None:
            return []
        wanted = set(dimensions)
        out: list[Finding] = []
        for entry in parsed:
            if len(out) >= self.max_findings:
                break
            if not isinstance(entry, dict):
                continue
            vals: dict[str, str] = {}
            for key in _FINDING_FIELDS:
                value = entry.get(key)
                if not isinstance(value, str) or not value.strip():
                    break
                vals[key] = value
            if len(vals) != len(_FINDING_FIELDS):
                continue
            if vals["dimension"] not in wanted:
                continue
            out.append(Finding(
                id=vals["id"],
                dimension=vals["dimension"],
                severity=vals["severity"],
                location=vals["location"],
                summary=vals["summary"],
                fix=vals["fix"],
                fail_first=vals["fail_first"],
            ))
        return out

#: The injected convergence runner: given the frozen-contract project dir, run
#: (or simulate) the implement loop and report ``(converged, head_sha, branch)``.
RunConvergence = Callable[[Path], "tuple[bool, str | None, str | None]"]


def worktree_convergence(provider, inner):
    """Wrap an inner convergence runner so it executes inside a leased worktree+
    branch (the Stage-5 isolated runner the :class:`ContractImplementer`
    docstring promised). Same :data:`RunConvergence` signature —
    ``ContractImplementer`` is unchanged. ``provider`` is a
    :class:`peers.spine.worktree.WorktreeProvider`; ``inner(worktree_path) ->
    (ok, head_sha, branch)`` is the actual convergence. The lease's ``with`` /
    ``finally`` releases the worktree whether ``inner`` converges or not,
    mirroring ``run_isolated``."""
    def _run_convergence(project_dir):
        # the frozen contract lives under `project_dir` (already isolated by the
        # adapter); lease a worktree off it keyed on its basename so concurrent
        # implements never collide.
        run_id = Path(project_dir).name
        with provider.lease(project_dir, run_id) as ws:
            return inner(ws.worktree_path)
    return _run_convergence


def _extract_json_object(text: str) -> dict | None:
    """Recover a JSON object from chatty model output (fenced block, then a bare
    ``{ ... }`` span). Returns ``None`` when nothing parses."""
    if not isinstance(text, str):
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = []
    if fence:
        candidates.append(fence.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start:end + 1])
    for blob in candidates:
        try:
            parsed = json.loads(blob)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


class LLMAuthor:
    """A production :class:`peers.develop.ports.Author` that turns surviving
    findings into a parser-valid :class:`AuthoredContract` via an injected agent.

    Fail-closed: an empty findings list, a runner error, non-JSON output, a
    missing PLAN body, or a PLAN that ``parse_plan`` rejects all return ``None``
    (a dry round). The adapter NEVER returns an unvalidated contract — develop
    never acts off an invalid plan."""

    def __init__(self, *, run_agent: RunAgent) -> None:
        self.run_agent = run_agent

    def _build_prompt(self, findings: list[Finding], repo: Path) -> str:
        lines = [
            f"You are authoring an implement contract for the repo at {repo}.",
            "Turn these confirmed findings into ONE parser-valid PLAN.md:",
        ]
        for f in findings:
            lines.append(
                f"- [{f.id}] ({f.dimension}/{f.severity}) {f.location}: "
                f"{f.summary} -> fix: {f.fix}; fail-first test: {f.fail_first}")
        lines += [
            "",
            "Return ONLY a JSON object with keys: plan_md (string), acceptance "
            "(string: the acceptance test command), e2e (string or null).",
            "plan_md MUST be a valid PLAN.md with this shape:",
            "  # <title>",
            "  ## Meta",
            "  surfaces: [cli]",
            "  acceptance: <command>",
            "  ## Steps",
            "  - [ ] [STEP-1] <step>",
            "    - touches: <path>",
            "Every step needs a [STEP-N] id and a `touches:` declaration. Do not "
            "invent files that do not exist in the repo.",
        ]
        return "\n".join(lines)

    def author(self, findings: list[Finding], repo: Path) -> AuthoredContract | None:
        if not findings:
            return None
        try:
            raw = self.run_agent(self._build_prompt(findings, Path(repo)))
        except Exception:  # noqa: BLE001 — adapter boundary: never raise into drive()
            return None
        obj = _extract_json_object(raw)
        if obj is None:
            return None
        plan_md = obj.get("plan_md")
        if not isinstance(plan_md, str) or not plan_md.strip():
            return None
        # VALIDATE — a PLAN the parser rejects is never authored (dry round).
        try:
            with tempfile.TemporaryDirectory(prefix="peers-develop-author-") as d:
                plan_path = Path(d) / "PLAN.md"
                plan_path.write_text(plan_md, encoding="utf-8")
                plan = parse_plan(plan_path)
        except (PlanValidationError, OSError):
            return None
        acceptance = obj.get("acceptance")
        if not isinstance(acceptance, str) or not acceptance.strip():
            acceptance = plan.acceptance  # fall back to the PLAN Meta acceptance
        e2e = obj.get("e2e")
        if not isinstance(e2e, str) or not e2e.strip():
            e2e = None
        return AuthoredContract(
            plan_md=plan_md,
            acceptance=acceptance,
            findings=[f.id for f in findings],
            e2e=e2e,
        )


class LLMRefuter:
    """A production refuter factory for develop's adversarial-verify gate.

    ``DevelopFrontend`` defaults to a refute-everything stub (fail-closed but
    inert — no finding survives). This asks an injected agent to refute each
    finding; a finding survives only on a clear "not refuted" vote.

    Fail-closed: a runner error, non-JSON, an ambiguous reply, or a
    missing/non-bool ``refuted`` key all count as REFUTED (True), so a finding
    never survives on noise."""

    def __init__(self, *, run_agent: RunAgent) -> None:
        self.run_agent = run_agent

    def _build_prompt(self, f: Finding) -> str:
        return (
            "Try to REFUTE this claimed finding by checking it against the "
            "actual code. Is it a real, reproducible defect, or is it wrong / "
            "not applicable / already handled?\n"
            f"- id: {f.id}\n- dimension: {f.dimension}\n- location: {f.location}\n"
            f"- summary: {f.summary}\n- proposed fix: {f.fix}\n\n"
            'Respond with ONLY a JSON object {"refuted": true|false} where '
            "refuted=true means it is NOT a real defect. If you are unsure, "
            "refuted=true."
        )

    def refuter_factory(self, finding: Finding):
        """Return a ``refuter(vote_index) -> bool`` (True == refuted) the gate
        calls ``k`` times — one independent refutation attempt per vote."""
        prompt = self._build_prompt(finding)

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


class ContractImplementer:
    """An :class:`peers.develop.ports.Implementer` over the frozen-contract path.

    ``run_convergence`` is injected so the unit test is deterministic; the real
    Stage-5 adapter passes a worktree-backed convergence runner with the same
    signature.
    """

    def __init__(self, *, run_convergence: RunConvergence) -> None:
        self.run_convergence = run_convergence

    def implement(self, contract: AuthoredContract, repo: Path) -> ImplementResult:
        """Validate + freeze ``contract``, then run the injected convergence.

        ``repo`` is the target the convergence ultimately operates on (a Stage-5
        worktree is built from it); the Stage-1 deterministic freeze happens in
        an isolated scratch dir created under it so nothing leaks into the target
        tree. An unparseable plan short-circuits to ``ok=False`` BEFORE any
        scratch dir or convergence call — develop never acts off an invalid
        contract.
        """
        repo = Path(repo)
        if not repo.is_dir():
            return ImplementResult(
                ok=False,
                reason=f"target repo missing or not a directory: {repo}",
            )

        try:
            project_dir = Path(tempfile.mkdtemp(prefix="peers-develop-impl-",
                                                dir=str(repo)))
        except OSError as e:
            return ImplementResult(ok=False, reason=f"cannot create scratch dir: {e}")
        try:
            plan_path = project_dir / "PLAN.md"
            plan_path.write_text(contract.plan_md, encoding="utf-8")

            # VALIDATE — a contract the parser rejects is never implemented.
            try:
                parse_plan(plan_path)
            except PlanValidationError as e:
                return ImplementResult(ok=False, reason=f"invalid contract: {e}")

            # FREEZE — pin the acceptance/e2e scripts + a PLAN snapshot so the
            # convergence runner cannot silently drift the contract.
            write_frozen_contracts(project_dir, contract.acceptance, contract.e2e,
                                   contract.plan_md)

            # CONVERGE — injected; returns (converged, head_sha, branch).
            try:
                ok, head_sha, branch = self.run_convergence(project_dir)
            except Exception as e:
                return ImplementResult(ok=False, reason=f"implement runner failed: {e}")
            if not ok:
                return ImplementResult(ok=False, head_sha=head_sha, branch=branch,
                                       reason="implement did not converge")
            return ImplementResult(ok=True, head_sha=head_sha, branch=branch)
        finally:
            shutil.rmtree(project_dir, ignore_errors=True)
