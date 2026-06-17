"""STEP-6 — ``ContractImplementer``: the thin real Implementer adapter.

The adapter wires an :class:`peers.develop.ports.AuthoredContract` to the real
implement substrate (``parse_plan`` + ``write_frozen_contracts``) and returns
where the converged branch WOULD live. It does NOT run a live multi-agent
convergence in the unit test — the convergence runner is **injected**
(``run_convergence(project_dir) -> (ok, head_sha, branch)``) so the acceptance
stays deterministic. An unparseable plan returns ``ImplementResult(ok=False,
reason=...)`` — never a crash.
"""
from __future__ import annotations

from pathlib import Path

from peers.develop.adapters import ContractImplementer
from peers.develop.ports import AuthoredContract

_VALID_PLAN = ("# Fix\n\n## Meta\nsurfaces: [lib]\nacceptance: pytest -q\n\n"
               "## Steps\n- [ ] [STEP-1] do it\n  - touches: src/x.py\n")


# ---- happy: freeze a parser-valid contract, run the injected convergence -
def test_adapter_freezes_and_runs(tmp_path):
    impl = ContractImplementer(run_convergence=lambda d: (True, "deadbeef", "feat/dev"))
    res = impl.implement(AuthoredContract(plan_md=_VALID_PLAN, acceptance="pytest -q",
                                          findings=["F1"]), tmp_path)
    assert res.ok is True and res.branch == "feat/dev" and res.head_sha == "deadbeef"


def test_adapter_freezes_a_parser_valid_frozen_contract(tmp_path):
    # Capture the project_dir handed to convergence and prove the frozen
    # contract actually landed there and re-parses (write_frozen_contracts ran).
    from peers_ctl.plan_parser import parse_plan

    captured: dict = {}

    def conv(project_dir):
        d = Path(project_dir)
        captured["plan_ok"] = parse_plan(d / "PLAN.md").name == "Fix"
        captured["acceptance_frozen"] = (d / "contracts" / "acceptance.sh").is_file()
        captured["snapshot_frozen"] = (d / "PLAN.original.md").is_file()
        captured["sha_pinned"] = (d / "contracts.sha").is_file()
        return (True, "abc123", "feat/dev")

    impl = ContractImplementer(run_convergence=conv)
    res = impl.implement(AuthoredContract(plan_md=_VALID_PLAN, acceptance="pytest -q",
                                          findings=["F1"]), tmp_path)
    assert res.ok is True
    assert captured == {"plan_ok": True, "acceptance_frozen": True,
                        "snapshot_frozen": True, "sha_pinned": True}


def test_adapter_freezes_e2e_when_the_contract_carries_one(tmp_path):
    plan = ("# Fix\n\n## Meta\nsurfaces: [cli]\nacceptance: pytest -q\n"
            "e2e: ./run.sh\n\n## Steps\n- [ ] [STEP-1] do it\n  - touches: src/x.py\n")
    seen: dict = {}

    def conv(project_dir):
        seen["e2e"] = (Path(project_dir) / "contracts" / "e2e.sh").is_file()
        return (True, "abc", "feat/dev")

    impl = ContractImplementer(run_convergence=conv)
    res = impl.implement(AuthoredContract(plan_md=plan, acceptance="pytest -q",
                                          findings=["F1"], e2e="./run.sh"), tmp_path)
    assert res.ok is True and seen["e2e"] is True


# ---- sad: an unparseable plan -> ok=False, never a crash -----------------
def test_adapter_rejects_invalid_plan(tmp_path):
    impl = ContractImplementer(run_convergence=lambda d: (True, "x", "y"))
    res = impl.implement(AuthoredContract(plan_md="not a plan", acceptance="x",
                                          findings=[]), tmp_path)
    assert res.ok is False and res.reason


def test_adapter_invalid_plan_does_not_invoke_convergence(tmp_path):
    # the injected convergence must NOT run for a plan that fails validation
    # (develop never edits freehand off an unvalidated contract).
    calls = []
    impl = ContractImplementer(run_convergence=lambda d: calls.append(d) or (True, "x", "y"))
    res = impl.implement(AuthoredContract(plan_md="# Title only\n", acceptance="x",
                                          findings=[]), tmp_path)
    assert res.ok is False and calls == []


# ---- edge: a non-converged convergence maps to ok=False ------------------
def test_adapter_maps_non_converged_run_to_not_ok(tmp_path):
    impl = ContractImplementer(run_convergence=lambda d: (False, None, None))
    res = impl.implement(AuthoredContract(plan_md=_VALID_PLAN, acceptance="pytest -q",
                                          findings=["F1"]), tmp_path)
    assert res.ok is False and res.head_sha is None


def test_adapter_missing_repo_returns_not_ok_without_convergence(tmp_path):
    # kind: edge
    calls = []
    missing = tmp_path / "missing"
    impl = ContractImplementer(run_convergence=lambda d: calls.append(d) or (True, "x", "y"))

    res = impl.implement(AuthoredContract(plan_md=_VALID_PLAN, acceptance="pytest -q",
                                          findings=["F1"]), missing)

    assert res.ok is False
    assert "missing or not a directory" in res.reason
    assert calls == []


def test_adapter_convergence_exception_returns_not_ok(tmp_path):
    def conv(project_dir):
        raise RuntimeError("boom")

    impl = ContractImplementer(run_convergence=conv)
    res = impl.implement(AuthoredContract(plan_md=_VALID_PLAN, acceptance="pytest -q",
                                          findings=["F1"]), tmp_path)

    assert res.ok is False
    assert "implement runner failed: boom" in res.reason


# ---- isolation: the freeze does not pollute the target repo --------------
def test_adapter_does_not_pollute_the_target_repo(tmp_path):
    impl = ContractImplementer(run_convergence=lambda d: (True, "abc", "feat/dev"))
    impl.implement(AuthoredContract(plan_md=_VALID_PLAN, acceptance="pytest -q",
                                    findings=["F1"]), tmp_path)
    # the frozen contract lives in an isolated scratch dir that is cleaned up;
    # the target repo keeps none of it.
    assert not (tmp_path / "contracts.sha").exists()
    assert not (tmp_path / "PLAN.original.md").exists()
    assert not any(c.name.startswith("peers-develop-impl-")
                   for c in tmp_path.iterdir())
