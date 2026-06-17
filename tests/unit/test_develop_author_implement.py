"""STEP-4 — ``run()`` AUTHOR → IMPLEMENT → confirmed-work (attested + witnessed).

Surviving findings are AUTHORED into a contract; a ``None`` author is a dry
round. A successful IMPLEMENT of a contract records ``confirmed-work`` whose
``author`` is the **substrate-attested** peer of the returned ``head_sha`` and
whose witness is the ``git-sha`` of that commit — so the spine
``witness-ledgered`` gate accepts it. A non-converged implement, or a
non-resolvable ``head_sha``, is a dry round (no gate-passing confirm).
"""
from __future__ import annotations

from peers.develop.frontend import DevelopFrontend
from peers.develop.ports import AuthoredContract, ImplementResult
from peers.spine.gates import evaluate_spine_gates

from tests.unit._develop_helpers import (_F, _FixedAuditor, _FixedAuthor,
                                          _FixedImpl, _NullAuditor, _NullAuthor,
                                          _NullImpl, _attested_repo, _run)


def _fe(**kw) -> DevelopFrontend:
    base = dict(auditor=_NullAuditor(), author=_NullAuthor(),
                implementer=_NullImpl(), dimensions=["correctness"],
                run_tests=lambda cmd: (0, "1 passed"),
                refuter_factory=lambda f: (lambda i: False))   # survives by default
    base.update(kw)
    return DevelopFrontend(**base)


# ---- happy: a real attested+witnessed confirmed unit ---------------------
def test_confirmed_unit_is_attested_and_witnessed(tmp_path):
    sha = _attested_repo(tmp_path, "claude")          # real commit + attest note
    fe = _fe(auditor=_FixedAuditor([_F("F1")]),
             author=_FixedAuthor(AuthoredContract(plan_md="# p",
                                                  acceptance="pytest -q",
                                                  findings=["F1"])),
             implementer=_FixedImpl(ImplementResult(ok=True, head_sha=sha,
                                                    branch="feat/x")))
    run = _run(tmp_path)
    fe.prepare(run)
    fe.run(run)
    rows = run.ledger.read()
    cw = [r for r in rows if r.event == "confirmed-work"]
    assert cw and cw[-1].author == "claude" and cw[-1].independence is True
    assert cw[-1].subject == "F1"
    # every row of a run carries its mode_run -> a ledger filtered by run id
    # must not silently drop confirmed units (regression guard).
    assert cw[-1].mode_run == "r1"
    assert cw[-1].witness["kind"] == "git-sha" and cw[-1].witness["sha256"] == sha
    # the minted confirmed-work row must actually pass the spine witness gate:
    assert evaluate_spine_gates(rows, mode_run="r1",
                                repo=tmp_path)["witness-ledgered"] is True


def test_confirm_is_followed_by_a_branch_pr_landing(tmp_path):
    sha = _attested_repo(tmp_path, "claude")
    fe = _fe(auditor=_FixedAuditor([_F("F1")]),
             author=_FixedAuthor(AuthoredContract(plan_md="# p",
                                                  acceptance="pytest -q",
                                                  findings=["F1"])),
             implementer=_FixedImpl(ImplementResult(ok=True, head_sha=sha,
                                                    branch="feat/x")))
    run = _run(tmp_path)
    fe.prepare(run)
    fe.run(run)
    land = [r for r in run.ledger.read() if r.event == "landing"]
    assert land and land[-1].subject == "feat/x"
    assert land[-1].witness["landing"] == "branch-pr"
    assert land[-1].mode_run == "r1"
    # landing is NOT a substrate-attested authorship event -> no author/independence.
    assert land[-1].author is None and land[-1].independence is False


# ---- sad: a converged-but-fake head_sha is not a gated confirm -----------
def test_non_resolvable_sha_is_not_a_gated_confirm(tmp_path):
    _attested_repo(tmp_path, "claude")
    fe = _fe(auditor=_FixedAuditor([_F("F1")]),
             author=_FixedAuthor(AuthoredContract(plan_md="# p",
                                                  acceptance="pytest -q",
                                                  findings=["F1"])),
             implementer=_FixedImpl(ImplementResult(ok=True, head_sha="deadbeef",
                                                    branch="feat/x")))
    run = _run(tmp_path)
    fe.prepare(run)
    fe.run(run)
    rows = run.ledger.read()
    # a non-resolvable sha never mints a confirmed-work row -> the round is dry.
    assert not any(r.event == "confirmed-work" for r in rows)
    assert rows[-1].event == "dry-round"
    assert evaluate_spine_gates(rows, mode_run="r1",
                                repo=tmp_path)["witness-ledgered"] is False


# ---- sad: author cannot produce a contract -> dry round ------------------
def test_author_returns_none_is_dry(tmp_path):
    _attested_repo(tmp_path, "claude")
    fe = _fe(auditor=_FixedAuditor([_F("F1")]), author=_FixedAuthor(None))
    run = _run(tmp_path)
    fe.prepare(run)
    fe.run(run)
    rows = run.ledger.read()
    assert not any(r.event == "confirmed-work" for r in rows)
    assert rows[-1].event == "dry-round"


# ---- sad: a non-converged implement -> dry round -------------------------
def test_non_converged_implement_is_dry(tmp_path):
    _attested_repo(tmp_path, "claude")
    fe = _fe(auditor=_FixedAuditor([_F("F1")]),
             author=_FixedAuthor(AuthoredContract(plan_md="# p",
                                                  acceptance="pytest -q",
                                                  findings=["F1"])),
             implementer=_FixedImpl(ImplementResult(ok=False, reason="no converge")))
    run = _run(tmp_path)
    fe.prepare(run)
    fe.run(run)
    rows = run.ledger.read()
    assert not any(r.event == "confirmed-work" for r in rows)
    assert rows[-1].event == "dry-round"


# ---- edge: an empty findings list does not crash the confirm path --------
def test_empty_findings_contract_confirms_with_no_subject(tmp_path):
    # guard: drive() does NOT catch IndexError, so subject must be None (not a
    # crash) when the authored contract carries no source finding ids.
    sha = _attested_repo(tmp_path, "claude")
    fe = _fe(auditor=_FixedAuditor([_F("F1")]),
             author=_FixedAuthor(AuthoredContract(plan_md="# p",
                                                  acceptance="pytest -q",
                                                  findings=[])),
             implementer=_FixedImpl(ImplementResult(ok=True, head_sha=sha,
                                                    branch="feat/x")))
    run = _run(tmp_path)
    fe.prepare(run)
    fe.run(run)
    cw = [r for r in run.ledger.read() if r.event == "confirmed-work"]
    assert cw and cw[-1].subject is None and cw[-1].author == "claude"
