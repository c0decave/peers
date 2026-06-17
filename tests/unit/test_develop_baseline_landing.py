from tests.unit._baseline_landing_helpers import (
    _attested_repo, _FileAuthor, _NullAuthor, _green, _norun)
from peers.develop.frontend import DevelopFrontend
from peers.develop.ports import Finding, AuthoredContract, ImplementResult
from peers.spine.mode_run import ModeRun, drive
from peers.spine.op_config import OpConfig
from peers.spine.gates import evaluate_spine_gates, all_pass

def _F(fid="F1"):
    return Finding(id=fid, dimension="correctness", severity="med", location="x:1",
                   summary="s", fix="f", fail_first="t")

class _FixedAuditor:
    def __init__(self, findings, once=False):
        self.f = findings
        self.once = once
        self.n = 0
    def audit(self, repo, dimensions):
        self.n += 1
        return self.f if (not self.once or self.n == 1) else []
class _FixedAuthor:
    def __init__(self, c): self.c = c
    def author(self, findings, repo): return self.c
class _FixedImpl:
    def __init__(self, r): self.r = r
    def implement(self, contract, repo): return self.r

def _run(tmp_path):
    return ModeRun(tool=tmp_path, op_config=OpConfig.from_dict({"mode": "develop"}),
                   ledger_path=tmp_path / "run.jsonl", mode_run="r1")

def test_absent_bar_gets_a_baseline_before_any_change(tmp_path):
    # NO pytest marker -> absent bar; the injected author CAN characterize -> built ->
    # the round is NOT blocked and proceeds. The baseline is built during prepare()
    # (before any audit/edit), satisfying the Stage-4 'before any change' verify bar.
    sha = _attested_repo(tmp_path, "claude")
    fe = DevelopFrontend(_FixedAuditor([_F()], once=True),
        _FixedAuthor(AuthoredContract(plan_md="# p", acceptance="pytest -q", findings=["F1"])),
        _FixedImpl(ImplementResult(ok=True, head_sha=sha, branch="feat/x")),
        dimensions=["correctness"], run_tests=_green,
        refuter_factory=lambda f: (lambda i: False), baseline_author=_FileAuthor())
    run = _run(tmp_path)
    fe.prepare(run)
    assert fe.bar.kind == "present" and fe.bar.provenance == "built"
    assert fe._blocked is False
    rows = run.ledger.read()
    # the baseline-built row precedes ANY confirmed-work (built before change) — a clear
    # two-step assertion (no list concatenation, no inline conditional):
    events = [r.event for r in rows]
    assert "baseline-built" in events
    if "confirmed-work" in events:
        assert events.index("baseline-built") < events.index("confirmed-work")

def test_uncharacterizable_bar_blocks_honest_stop(tmp_path):
    # NO marker AND the author cannot characterize -> uncharacterizable -> blocked.
    _attested_repo(tmp_path, "claude")
    fe = DevelopFrontend(_FixedAuditor([_F()]),
        _FixedAuthor(AuthoredContract(plan_md="# p", acceptance="pytest -q", findings=["F1"])),
        _FixedImpl(ImplementResult(ok=True, head_sha="x", branch="feat/x")),
        dimensions=["correctness"], run_tests=_norun,
        refuter_factory=lambda f: (lambda i: False), baseline_author=_NullAuthor())
    run = _run(tmp_path)
    fe.prepare(run)
    assert fe.bar.kind == "absent" and fe._blocked is True
    fe.run(run)
    assert run.ledger.read()[-1].event == "dry-round"      # honest stop, no freehand work

def test_develop_run_produces_a_verified_unmerged_landing_contract(tmp_path):
    # THE Stage-4 verify bar: a develop run yields a verified branch whose landing
    # contract is mergeable=True — and it is NOT merged (the contract records, only).
    sha = _attested_repo(tmp_path, "claude")
    fe = DevelopFrontend(_FixedAuditor([_F()], once=True),
        _FixedAuthor(AuthoredContract(plan_md="# p", acceptance="pytest -q", findings=["F1"])),
        _FixedImpl(ImplementResult(ok=True, head_sha=sha, branch="feat/x")),
        dimensions=["correctness"], run_tests=_green,
        refuter_factory=lambda f: (lambda i: False), baseline_author=_FileAuthor())
    run = _run(tmp_path)
    out = drive(run, fe)
    rows = run.ledger.read()
    landing = [r for r in rows if r.event == "landing"]
    assert landing and landing[-1].witness["kind"] == "url"        # advisory, not re-derived
    contract = landing[-1].witness["contract"]
    # Stage 6: branch-pr is now the CONDITIONAL default (S2). This run lands branch-pr
    # because op_config.landing defaults to "branch-pr" (not the "auto-merge" token) and
    # it is a legacy single-HEAD run -- NOT because of an unconditional clamp. to_witness
    # carries landing_mode INSIDE the contract dict so the develop e2e reads it directly:
    assert contract["mergeable"] is True and contract["landing_mode"] == "branch-pr"
    assert contract["head_sha"] == sha
    # NOT merged: branch-pr only; the spine never merged (no 'merge' event exists).
    assert not any(r.event == "merge" for r in rows)
    assert out["mergeable"] is True and out["baseline_provenance"] == "built"
    assert all_pass(evaluate_spine_gates(rows, mode_run="r1", repo=tmp_path)) is True

def test_interpret_re_derives_mergeable_ignoring_tampered_landing_text(tmp_path):
    # 'records, never asserts' locked at the interpret() boundary: a FORGED landing row
    # whose stored contract claims mergeable=True must NOT fool interpret() while the live
    # gates (no confirmed-work -> witness-ledgered=False) say not-mergeable. interpret()
    # RE-DERIVES from the live ledger, never trusts the url-kind advisory text.
    from peers.spine.op_config import load_op_config
    fe = DevelopFrontend(_FixedAuditor([_F()], once=True),
        _FixedAuthor(AuthoredContract(plan_md="# p", acceptance="pytest -q", findings=["F1"])),
        _FixedImpl(ImplementResult(ok=True, head_sha="x", branch="feat/x")),  # head 'x' never resolves
        dimensions=["correctness"], run_tests=_green,
        refuter_factory=lambda f: (lambda i: False), baseline_author=_FileAuthor())
    run = _run(tmp_path)
    load_op_config(run.op_config, run.ledger, mode_run="r1")
    fe.prepare(run)                       # baseline built; bar present/built
    # forge a landing row whose STORED contract lies that it is mergeable:
    run.ledger.append(event="landing", status="ok", subject="feat/x",
        witness={"kind": "url", "uri": "feat/x", "landing": "branch-pr",
                 "contract": {"mergeable": True, "gates": {}, "head_sha": "x",
                              "self_hosting": False, "landing_mode": "branch-pr",
                              "mode_run": "r1"}},
        mode_run="r1")
    out = fe.interpret(run)
    # no confirmed-work exists -> witness-ledgered is False -> re-derived mergeable is False,
    # DESPITE the stored text claiming True:
    assert out["mergeable"] is False
    assert evaluate_spine_gates(run.ledger.read(), mode_run="r1",
                                repo=tmp_path)["witness-ledgered"] is False

def test_no_baseline_author_keeps_stage1_absent_blocks(tmp_path):
    # Backwards-compat: with NO baseline_author injected, an absent bar blocks exactly
    # as Stage 1 did (the build path is opt-in via the injected port).
    fe = DevelopFrontend(_FixedAuditor([_F()]),
        _FixedAuthor(AuthoredContract(plan_md="# p", acceptance="pytest -q", findings=["F1"])),
        _FixedImpl(ImplementResult(ok=True, head_sha="x", branch="feat/x")),
        dimensions=["correctness"], run_tests=_norun,
        refuter_factory=lambda f: (lambda i: False))   # no baseline_author
    run = _run(tmp_path)
    fe.prepare(run)
    assert fe.bar.kind == "absent" and fe._blocked is True
