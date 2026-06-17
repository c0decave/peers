from tests.unit._develop_helpers import (_FixedAuditor, _FixedAuthor, _FixedImpl, _F,
                                         _attested_repo)   # reuse Stage-1 helpers

from peers.develop.frontend import DevelopFrontend
from peers.develop.ports import AuthoredContract, ImplementResult
from peers.spine.mode_run import ModeRun, drive
from peers.spine.op_config import OpConfig


def _confirming_fe(sha):
    return DevelopFrontend(
        _FixedAuditor([_F("F1")], once=True),
        _FixedAuthor(AuthoredContract(plan_md="# p", acceptance="pytest -q", findings=["F1"])),
        _FixedImpl(ImplementResult(ok=True, head_sha=sha, branch="feat/x")),
        dimensions=["correctness"], run_tests=lambda c: (0, "1 passed"),
        refuter_factory=lambda f: (lambda i: False))


def test_propagation_emitted_when_branch_set(tmp_path):
    sha = _attested_repo(tmp_path, "claude")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    run = ModeRun(tool=tmp_path, op_config=OpConfig.from_dict({"mode": "develop"}),
                  ledger_path=tmp_path / "run.jsonl", mode_run="r1",
                  branch="peers/run/r1")
    drive(run, _confirming_fe(sha))
    rows = run.ledger.read()
    prop = [r for r in rows if r.event == "propagation"]
    land = [r for r in rows if r.event == "landing"]
    assert prop and prop[-1].witness["kind"] == "git-sha"
    assert prop[-1].witness["sha256"] == sha and prop[-1].author == "claude"
    assert prop[-1].independence is True
    assert land                                          # landing row still emitted (separate)


def test_no_propagation_in_legacy_no_branch_mode(tmp_path):
    sha = _attested_repo(tmp_path, "claude")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    run = ModeRun(tool=tmp_path, op_config=OpConfig.from_dict({"mode": "develop"}),
                  ledger_path=tmp_path / "run.jsonl", mode_run="r1")   # branch=None
    drive(run, _confirming_fe(sha))
    rows = run.ledger.read()
    assert not any(r.event == "propagation" for r in rows)   # legacy: landing only
    assert any(r.event == "landing" for r in rows)
