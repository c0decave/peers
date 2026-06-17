"""STEP-3 — ``run()`` AUDIT → adversarial VERIFY.

One develop round audits the tool, then runs EACH finding through
:func:`peers.spine.adversarial_verify.verify_claim` with ``k`` refuters from the
injected ``refuter_factory``. A finding the refuters kill is dropped (ledgered as
a ``gate`` row with status ``fail``); with no surviving finding the round records
a ``dry-round``. The refuters are deterministic fakes — no LLM.
"""
from __future__ import annotations

from peers.develop.frontend import DevelopFrontend
from peers.develop.ports import Finding

from tests.unit._develop_helpers import (_F, _FixedAuditor, _NullAuditor,
                                          _NullAuthor, _NullImpl, _run)


def _fe(**kw) -> DevelopFrontend:
    base = dict(auditor=_NullAuditor(), author=_NullAuthor(),
                implementer=_NullImpl(), dimensions=["correctness"],
                run_tests=lambda cmd: (0, "1 passed"))
    base.update(kw)
    return DevelopFrontend(**base)


# ---- sad: an unsound finding is refuted and the round is dry -------------
def test_unsound_finding_rejected_and_round_is_dry(tmp_path):
    findings = [Finding(id="F1", dimension="security", severity="high",
                        location="x:1", summary="s", fix="f", fail_first="t")]
    fe = _fe(auditor=_FixedAuditor(findings),
             refuter_factory=lambda finding: (lambda i: True), k=3)
    run = _run(tmp_path)
    fe.prepare(run)
    fe.run(run)
    rows = run.ledger.read()
    gate = [r for r in rows if r.event == "gate" and r.subject == "F1"]
    assert gate and gate[-1].status == "fail"
    assert gate[-1].witness["k"] == 3 and gate[-1].witness["refuted"] == 3
    assert rows[-1].event == "dry-round"          # nothing confirmed this round


# ---- happy: a sound finding survives the verify gate ---------------------
def test_sound_finding_survives_the_verify_gate(tmp_path):
    # refuter that never refutes -> survives; with a Null author it still ends
    # the round dry, but the gate row records the survival (status pass).
    fe = _fe(auditor=_FixedAuditor([_F("F1")]),
             refuter_factory=lambda f: (lambda i: False), k=2)
    run = _run(tmp_path)
    fe.prepare(run)
    fe.run(run)
    gate = [r for r in run.ledger.read() if r.event == "gate" and r.subject == "F1"]
    assert gate and gate[-1].status == "pass"
    assert gate[-1].witness["refuted"] == 0


# ---- edge: empty audit -> straight to a dry round, no gate rows ----------
def test_no_findings_is_a_dry_round(tmp_path):
    fe = _fe(auditor=_NullAuditor())
    run = _run(tmp_path)
    fe.prepare(run)
    fe.run(run)
    rows = run.ledger.read()
    assert not any(r.event == "gate" for r in rows)
    assert rows[-1].event == "dry-round" and rows[-1].status == "dry"


# ---- edge: every finding is voted on, survivors and casualties separated -
def test_multiple_findings_partitioned_by_verify(tmp_path):
    # F1 survives (refuter False), F2 is killed (refuter True). Both get a gate
    # row; only F1 survives -> but the Null author still makes the round dry.
    findings = [_F("F1"), _F("F2")]

    def refuter_factory(finding):
        return (lambda i: finding.id == "F2")     # F2 refuted, F1 not

    fe = _fe(auditor=_FixedAuditor(findings), refuter_factory=refuter_factory, k=3)
    run = _run(tmp_path)
    fe.prepare(run)
    fe.run(run)
    rows = run.ledger.read()
    by_subj = {r.subject: r.status for r in rows if r.event == "gate"}
    assert by_subj == {"F1": "pass", "F2": "fail"}


# ---- sad: an erroring refuter counts as a refutation (fail-closed) -------
def test_erroring_refuter_kills_the_finding(tmp_path):
    def boom(i):
        raise RuntimeError("refuter blew up")

    fe = _fe(auditor=_FixedAuditor([_F("F1")]),
             refuter_factory=lambda f: boom, k=3)
    run = _run(tmp_path)
    fe.prepare(run)
    fe.run(run)
    rows = run.ledger.read()
    gate = [r for r in rows if r.event == "gate" and r.subject == "F1"]
    assert gate and gate[-1].status == "fail"     # uncertainty never clears it
    assert rows[-1].event == "dry-round"
