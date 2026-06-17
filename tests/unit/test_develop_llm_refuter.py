"""R-fix (HS-04): a real ``refuter_factory`` for develop. The wired
DevelopFrontend defaulted to the refute-everything stub, so no finding ever
survived verification and the confirmed-work path was dead. ``LLMRefuter`` asks
an injected agent to refute each finding; a finding survives ONLY on a clear
"not refuted / confirmed real" vote.

Fail-closed: a runner error, non-JSON, or an ambiguous reply counts as REFUTED
(True) — an unverifiable finding must not survive on noise.
"""
from __future__ import annotations

import json

from peers.develop.adapters import LLMRefuter
from peers.develop.ports import Finding


def _finding() -> Finding:
    return Finding(id="F1", dimension="correctness", severity="high",
                   location="src/x.py:10", summary="off-by-one", fix="use <=",
                   fail_first="test_last")


# --- happy path: a clear confirm lets the finding survive (refuted=False) -----
def test_happy_confirmed_finding_is_not_refuted() -> None:
    ref = LLMRefuter(run_agent=lambda p: json.dumps({"refuted": False}))
    vote = ref.refuter_factory(_finding())
    assert vote(0) is False   # not refuted -> finding survives


# --- sad path: a clear refute drops the finding -------------------------------
def test_sad_refuted_finding_votes_true() -> None:
    ref = LLMRefuter(run_agent=lambda p: json.dumps({"refuted": True}))
    assert ref.refuter_factory(_finding())(0) is True


def test_sad_runner_error_is_failclosed_refuted() -> None:
    def boom(_p: str) -> str:
        raise RuntimeError("model died")

    assert LLMRefuter(run_agent=boom).refuter_factory(_finding())(0) is True


# --- edge cases ---------------------------------------------------------------
def test_edge_ambiguous_reply_is_failclosed_refuted() -> None:
    ref = LLMRefuter(run_agent=lambda p: "I'm not sure, could go either way.")
    assert ref.refuter_factory(_finding())(0) is True


def test_edge_missing_or_nonbool_refuted_key_is_failclosed() -> None:
    ref = LLMRefuter(run_agent=lambda p: json.dumps({"verdict": "maybe"}))
    assert ref.refuter_factory(_finding())(0) is True


def test_edge_factory_callable_reusable_across_k_votes() -> None:
    calls = {"n": 0}

    def runner(_p: str) -> str:
        calls["n"] += 1
        return json.dumps({"refuted": False})

    vote = LLMRefuter(run_agent=runner).refuter_factory(_finding())
    assert [vote(i) for i in range(3)] == [False, False, False]
    assert calls["n"] == 3   # one independent refutation attempt per vote


def test_edge_prompt_includes_the_finding_to_refute() -> None:
    seen = {}

    def cap(prompt: str) -> str:
        seen["p"] = prompt
        return json.dumps({"refuted": True})

    LLMRefuter(run_agent=cap).refuter_factory(_finding())(0)
    assert "off-by-one" in seen["p"]
