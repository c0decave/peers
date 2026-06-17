"""STEP-1 — develop ports: Finding + the three capability Protocols."""
from __future__ import annotations

import pytest

from peers.develop.ports import (Auditor, Author, AuthoredContract, Finding,
                                  Implementer, ImplementResult)


def test_finding_shape():
    f = Finding(id="F1", dimension="security", severity="high",
                location="src/x.py:10", summary="missing input check",
                fix="validate n", fail_first="tests/test_x.py::test_rejects_bad")
    assert f.id == "F1" and f.dimension == "security"
    assert f.fail_first == "tests/test_x.py::test_rejects_bad"


def test_protocols_are_runtime_checkable():
    class _A:
        def audit(self, repo, dimensions):
            return []

    class _Au:
        def author(self, findings, repo):
            return None

    class _Im:
        def implement(self, contract, repo):
            return ImplementResult(ok=False)

    assert isinstance(_A(), Auditor)
    assert isinstance(_Au(), Author)
    assert isinstance(_Im(), Implementer)


def test_non_conforming_object_is_not_an_auditor():
    # edge/sad: a class missing the method (or with the wrong name) must NOT
    # satisfy the runtime-checkable Protocol — the seam is real, not nominal.
    class _NotAuditor:
        def inspect(self, repo, dimensions):
            return []

    assert not isinstance(_NotAuditor(), Auditor)
    assert not isinstance(object(), Author)


def test_result_dataclasses():
    c = AuthoredContract(plan_md="# x", acceptance="pytest -q", findings=["F1"])
    r = ImplementResult(ok=True, head_sha="abc", branch="feat/x")
    assert c.findings == ["F1"] and r.ok is True
    assert c.e2e is None and r.reason == ""


def test_authored_contract_defaults_are_not_shared():
    # sad/edge: mutable default (findings list) must be per-instance, not a
    # shared class-level list (the classic dataclass field(default_factory) bug).
    a = AuthoredContract(plan_md="# a", acceptance="x")
    b = AuthoredContract(plan_md="# b", acceptance="y")
    a.findings.append("F1")
    assert a.findings == ["F1"] and b.findings == []


def test_finding_round_trip_preserves_all_seven_fields():
    # happy: a fully-populated Finding round-trips every field unchanged. Guards
    # the field names/order that the AUTHOR seam and the confirmed-work ledger
    # subject path in frontend.py depend on (id is the verify + ledger subject).
    f = Finding(id="F9", dimension="perf", severity="low", location="src/m.py:5",
                summary="hot loop", fix="memoize",
                fail_first="tests/t.py::test_fast")
    assert (f.id, f.dimension, f.severity, f.location, f.summary, f.fix,
            f.fail_first) == ("F9", "perf", "low", "src/m.py:5", "hot loop",
                              "memoize", "tests/t.py::test_fast")


def test_runtime_checkable_protocol_isinstance_is_name_only_boundary():
    # edge: runtime_checkable Protocols match on METHOD NAME, not signature — a
    # wrong-arity audit() still satisfies isinstance(Auditor). Pins this known
    # structural-typing boundary so a future adapter author is not lulled into
    # thinking the Protocol validates the call signature for them.
    class _WrongArity:
        def audit(self):            # deliberately missing (repo, dimensions)
            return []

    assert isinstance(_WrongArity(), Auditor) is True


def test_finding_missing_required_field_raises_typeerror():
    # sad: Finding declares NO defaults — constructing it without every field
    # must raise TypeError, so a half-built finding can never silently become a
    # confirmed-work subject carrying empty/garbage attributes.
    with pytest.raises(TypeError):
        Finding(id="F1")
