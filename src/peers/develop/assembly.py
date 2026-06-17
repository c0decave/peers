"""Wiring: assemble an operator-runnable develop ``DevelopFrontend`` from the
real adapters (the `peers develop` CLI + the fleet builder both call this).

``run_agent`` (one-shot ``prompt -> text``) drives AUDIT + AUTHOR;
``impl_run_agent`` (``prompt, workdir -> text``) drives the IMPLEMENT
convergence inside the target tree. Both are injected so the assembly is
deterministic in tests; production wires them to the configured peer spec via
:func:`peers.agent_invoke.agent_runner_from_spec` /
:func:`peers.agent_invoke.run_agent_once`.

The convergence ``run_convergence(project_dir)`` the :class:`ContractImplementer`
calls is built here: it reads the frozen ``contracts/acceptance.sh`` the
implementer pinned, runs it as the pass/fail oracle, and drives the agent to
edit the target tree (``project_dir.parent`` — the repo, or the fleet's leased
worktree) until acceptance passes on a real diff.
"""
from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path

from peers.develop.adapters import (
    ContractImplementer,
    LLMAuditor,
    LLMAuthor,
    LLMRefuter,
    RunAgent,
)
from peers.develop.convergence import AgentConvergenceRunner, RunAgentInDir
from peers.develop.frontend import DevelopFrontend

#: Acceptance/bar command timeout (s).
_CMD_TIMEOUT_S = 600.0


def _default_run_tests(repo: Path) -> Callable[[str], "tuple[int, str] | None"]:
    """A real ``run_tests(cmd) -> (rc, output)`` that runs ``cmd`` in ``repo``.
    Returns ``None`` when the command cannot be launched (an absent bar)."""
    def _run_tests(cmd: str) -> "tuple[int, str] | None":
        try:
            r = subprocess.run(
                shlex.split(cmd), cwd=str(repo), capture_output=True, text=True,
                timeout=_CMD_TIMEOUT_S, check=False)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        return (r.returncode, (r.stdout or "") + (r.stderr or ""))

    return _run_tests


def make_develop_frontend(
    repo: str | Path,
    *,
    run_agent: RunAgent,
    impl_run_agent: RunAgentInDir,
    dimensions: list[str],
    run_tests: Callable[[str], "tuple[int, str] | None"] | None = None,
    convergence_budget: int = 5,
    attest_peer: str = "develop",
    k: int = 2,
) -> DevelopFrontend:
    """Assemble a DevelopFrontend with the real LLM adapters + convergence."""
    repo = Path(repo)
    auditor = LLMAuditor(run_agent=run_agent)
    author = LLMAuthor(run_agent=run_agent)
    refuter = LLMRefuter(run_agent=run_agent)

    def run_convergence(project_dir) -> tuple[bool, str | None, str | None]:
        from peers_ctl.contracts import ContractsMismatch, verify_contracts

        project_dir = Path(project_dir)
        target = project_dir.parent  # the implementer made project_dir under repo
        plan = project_dir / "PLAN.md"
        contract_md = plan.read_text(encoding="utf-8") if plan.is_file() else None
        acc_script = project_dir / "contracts" / "acceptance.sh"

        def run_acceptance(workdir: Path) -> tuple[bool, str]:
            # HS-02: re-verify the frozen contract's pinned shas BEFORE trusting
            # its acceptance script — an agent that rewrote acceptance.sh (to
            # forge a pass) is caught here and fails CLOSED.
            try:
                verify_contracts(project_dir)
            except ContractsMismatch as e:
                return (False, f"frozen contract tampered: {e}")
            try:
                r = subprocess.run(
                    ["sh", str(acc_script)], cwd=str(workdir),
                    capture_output=True, text=True, timeout=_CMD_TIMEOUT_S,
                    check=False)
            except (OSError, subprocess.SubprocessError) as e:
                return (False, f"acceptance could not run: {e}")
            return (r.returncode == 0, (r.stdout or "") + (r.stderr or ""))

        runner = AgentConvergenceRunner(
            run_agent=impl_run_agent,
            run_acceptance=run_acceptance,
            budget=convergence_budget,
            attest_peer=attest_peer,
            contract_md=contract_md,
            exclude=(project_dir.name,),  # never let the scratch dir count as work
        )
        return runner(target)

    implementer = ContractImplementer(run_convergence=run_convergence)
    # CB-4: with no explicit run_tests, thread the bar runner per run.tool via a
    # factory (so a FLEET run infers the bar inside its leased worktree, not the
    # construction repo). An explicitly injected run_tests (deterministic test seam)
    # is used verbatim -- no factory.
    if run_tests is not None:
        bound_run_tests, run_tests_factory = run_tests, None
    else:
        bound_run_tests, run_tests_factory = _default_run_tests(repo), _default_run_tests
    return DevelopFrontend(
        auditor, author, implementer,
        dimensions=list(dimensions),
        run_tests=bound_run_tests,
        run_tests_factory=run_tests_factory,
        k=k,
        refuter_factory=refuter.refuter_factory,
    )
