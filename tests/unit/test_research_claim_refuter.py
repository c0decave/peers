"""R5: a real research claim refuter (fixes the HS-04-style inert default in
ResearchFrontend). A claim survives verification only on a clear "supported"
vote; fail-closed (error/ambiguous/non-bool) counts as refuted.
"""
from __future__ import annotations

import json

from peers.research.adapters import LLMClaimRefuter
from peers.research.ports import Claim, Witness


def _claim() -> Claim:
    return Claim(id="c1", text="X causes Y", status="",
                 witnesses=[Witness(kind="code-location", uri="a.py:1",
                                    content_hash="h", resolved_origin="a.py")],
                 load_bearing=True)


def test_happy_supported_claim_survives() -> None:
    r = LLMClaimRefuter(run_agent=lambda p: json.dumps({"refuted": False}))
    assert r.refuter_factory(_claim())(0) is False


def test_sad_refuted_claim_votes_true() -> None:
    r = LLMClaimRefuter(run_agent=lambda p: json.dumps({"refuted": True}))
    assert r.refuter_factory(_claim())(0) is True


def test_sad_error_is_failclosed() -> None:
    def boom(_p: str) -> str:
        raise RuntimeError("x")

    assert LLMClaimRefuter(run_agent=boom).refuter_factory(_claim())(0) is True


def test_edge_ambiguous_is_failclosed() -> None:
    r = LLMClaimRefuter(run_agent=lambda p: "hmm maybe")
    assert r.refuter_factory(_claim())(0) is True


def test_edge_prompt_includes_claim_text() -> None:
    seen = {}

    def cap(p: str) -> str:
        seen["p"] = p
        return json.dumps({"refuted": True})

    LLMClaimRefuter(run_agent=cap).refuter_factory(_claim())(0)
    assert "X causes Y" in seen["p"]


def test_edge_votes_use_distinct_prompt_angles() -> None:
    # RC-05: k votes must not be byte-identical prompts (no vote diversity).
    prompts = []

    def cap(p: str) -> str:
        prompts.append(p)
        return json.dumps({"refuted": False})

    vote = LLMClaimRefuter(run_agent=cap).refuter_factory(_claim())
    [vote(i) for i in range(3)]
    assert len(set(prompts)) == 3   # three distinct refutation angles
