"""Wiring: ``make_develop_frontend`` assembles the real develop adapters
(LLMAuditor + LLMAuthor + ContractImplementer over the AgentConvergenceRunner)
into an operator-runnable DevelopFrontend. The agents are injected so this is
deterministic; production (the `peers develop` CLI) wires them to the configured
peer spec via :func:`peers.agent_invoke.agent_runner_from_spec`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from peers.develop.adapters import ContractImplementer, LLMAuditor, LLMAuthor
from peers.develop.assembly import make_develop_frontend
from peers.develop.frontend import DevelopFrontend
from peers.develop.ports import AuthoredContract, Finding

VALID_PLAN = (
    "# Fix\n\n## Meta\nsurfaces: [cli]\nacceptance: test -f fix.txt\n\n"
    "## Steps\n- [ ] [STEP-1] create the marker\n  - touches: fix.txt\n"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "seed").write_text("x", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def test_happy_assembles_the_real_adapter_types() -> None:
    fe = make_develop_frontend(
        Path("/repo"),
        run_agent=lambda p: "[]",
        impl_run_agent=lambda p, w: "",
        dimensions=["correctness"],
        run_tests=lambda c: (0, "ok"),
    )
    assert isinstance(fe, DevelopFrontend)
    assert isinstance(fe.auditor, LLMAuditor)
    assert isinstance(fe.author, LLMAuthor)
    assert isinstance(fe.implementer, ContractImplementer)
    assert fe.dimensions == ["correctness"]


def test_cb4_default_bar_runner_is_threaded_per_run_tool() -> None:
    # CB-4 (fleet-only): without an explicit run_tests, make_develop_frontend must
    # wire a run_tests_factory so prepare() binds the bar runner to run.tool (the
    # leased worktree) at call time -- not freeze a runner bound to the construction
    # repo. An explicitly injected run_tests (the deterministic test seam) is used
    # verbatim, so no factory is wired.
    fe_default = make_develop_frontend(
        Path("/repo"), run_agent=lambda p: "[]", impl_run_agent=lambda p, w: "",
        dimensions=["correctness"])
    assert fe_default.run_tests_factory is not None

    fe_injected = make_develop_frontend(
        Path("/repo"), run_agent=lambda p: "[]", impl_run_agent=lambda p, w: "",
        dimensions=["correctness"], run_tests=lambda c: (0, "ok"))
    assert fe_injected.run_tests_factory is None


def test_happy_wires_a_real_refuter_not_the_refute_all_stub() -> None:
    # HS-04 regression: the wired frontend must use a REAL refuter, else every
    # finding is refuted and the confirmed-work path is dead.
    fe = make_develop_frontend(
        Path("/repo"),
        run_agent=lambda p: '{"refuted": false}',   # the model confirms findings
        impl_run_agent=lambda p, w: "", dimensions=["correctness"],
        run_tests=lambda c: (0, "ok"))
    f = Finding(id="F1", dimension="correctness", severity="high",
                location="x", summary="s", fix="f", fail_first="t")
    assert fe.refuter_factory(f)(0) is False   # a confirming model -> survives


def test_happy_implement_converges_commits_and_attests(tmp_path: Path) -> None:
    from peers.attest import attested_peer

    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    seen = {}

    def impl_agent(prompt: str, workdir) -> str:
        seen["prompt"] = prompt
        (Path(workdir) / "fix.txt").write_text("done", encoding="utf-8")
        return "implemented"

    fe = make_develop_frontend(
        repo, run_agent=lambda p: "[]", impl_run_agent=impl_agent,
        dimensions=["correctness"], run_tests=lambda c: (0, "ok"),
        attest_peer="develop",
    )
    contract = AuthoredContract(plan_md=VALID_PLAN, acceptance="test -f fix.txt",
                                findings=["F1"])
    result = fe.implementer.implement(contract, repo)
    assert result.ok is True
    assert result.head_sha and result.head_sha != base
    assert attested_peer(repo, result.head_sha) == "develop"
    # the impl agent was prompted WITH the contract body
    assert "create the marker" in seen["prompt"]


def test_sad_implement_unparseable_contract_is_not_ok(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    fe = make_develop_frontend(
        repo, run_agent=lambda p: "[]",
        impl_run_agent=lambda p, w: (Path(w) / "fix.txt").write_text("x") or "",
        dimensions=["correctness"], run_tests=lambda c: (0, "ok"),
    )
    bad = AuthoredContract(plan_md="not a plan", acceptance="true", findings=["F1"])
    result = fe.implementer.implement(bad, repo)
    assert result.ok is False
    assert _git(repo, "rev-parse", "HEAD") == base   # nothing committed


def test_sad_no_op_agent_with_trivial_acceptance_does_not_forge_convergence(tmp_path: Path) -> None:
    # HS-01 regression: a no-op agent + a trivially-passing acceptance must NOT
    # manufacture a converged/attested commit. The implementer's scratch dir
    # lives under the repo; it must be excluded from the real-work diff guard.
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    fe = make_develop_frontend(
        repo, run_agent=lambda p: "[]",
        impl_run_agent=lambda p, w: "I changed nothing",   # no-op
        dimensions=["correctness"], run_tests=lambda c: (0, "ok"))
    contract = AuthoredContract(plan_md=VALID_PLAN, acceptance="true",  # always passes
                                findings=["F1"])
    result = fe.implementer.implement(contract, repo)
    assert result.ok is False                       # vacuous green is not convergence
    assert _git(repo, "rev-parse", "HEAD") == base  # nothing committed
    assert _git(repo, "status", "--porcelain") == ""  # no scratch pollution / dirty tree


def test_sad_agent_tampering_the_frozen_acceptance_is_rejected(tmp_path: Path) -> None:
    # HS-02 regression: an agent that rewrites the frozen acceptance.sh to pass
    # must be caught by the contract integrity check, not allowed to converge.
    import glob
    import os

    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")

    def tamper(_prompt: str, workdir) -> str:
        for accp in glob.glob(str(Path(workdir) / "peers-develop-impl-*" /
                                  "contracts" / "acceptance.sh")):
            os.chmod(accp, 0o644)
            Path(accp).write_text("exit 0\n", encoding="utf-8")
        return "tampered the oracle, made no real fix"

    fe = make_develop_frontend(
        repo, run_agent=lambda p: "[]", impl_run_agent=tamper,
        dimensions=["correctness"], run_tests=lambda c: (0, "ok"),
        convergence_budget=2)
    contract = AuthoredContract(plan_md=VALID_PLAN, acceptance="test -f fix.txt",
                                findings=["F1"])
    result = fe.implementer.implement(contract, repo)
    assert result.ok is False
    assert _git(repo, "rev-parse", "HEAD") == base


def test_edge_acceptance_failing_does_not_converge(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    fe = make_develop_frontend(
        repo, run_agent=lambda p: "[]",
        impl_run_agent=lambda p, w: "noop",   # never creates fix.txt
        dimensions=["correctness"], run_tests=lambda c: (0, "ok"),
        convergence_budget=2,
    )
    contract = AuthoredContract(plan_md=VALID_PLAN, acceptance="test -f fix.txt",
                                findings=["F1"])
    result = fe.implementer.implement(contract, repo)
    assert result.ok is False
    assert _git(repo, "rev-parse", "HEAD") == base
