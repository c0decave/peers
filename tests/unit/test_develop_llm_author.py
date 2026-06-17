"""R3: a real ``Author`` adapter (``LLMAuthor``) that turns surviving findings
into a *parser-valid* implement contract via an injected agent.

Load-bearing honesty rule (mirrors develop "never edits freehand"): the adapter
MUST validate the model's PLAN.md with ``parse_plan`` and return ``None`` (a dry
round) when it is invalid, missing, or unparseable — it never hands an
unvalidated contract downstream, and never raises into ``drive()``.
"""
from __future__ import annotations

import json
from pathlib import Path

from peers.develop.adapters import LLMAuthor
from peers.develop.ports import Author, AuthoredContract, Finding

VALID_PLAN = (
    "# Fix off-by-one\n\n"
    "## Meta\n"
    "surfaces: [cli]\n"
    "acceptance: pytest -q tests/acceptance/\n\n"
    "## Steps\n"
    "- [ ] [STEP-1] correct the loop bound\n"
    "  - touches: src/x.py\n"
)


def _finding(fid: str = "F1") -> Finding:
    return Finding(id=fid, dimension="correctness", severity="high",
                   location="src/x.py:10", summary="off-by-one", fix="use <=",
                   fail_first="test_last_element")


def _reply(plan: str = VALID_PLAN, acceptance: str = "pytest -q") -> str:
    return json.dumps({"plan_md": plan, "acceptance": acceptance, "e2e": None})


# --- happy path ---------------------------------------------------------------
def test_happy_produces_parser_valid_contract_with_finding_ids() -> None:
    auth = LLMAuthor(run_agent=lambda p: _reply())
    contract = auth.author([_finding("F1"), _finding("F2")], Path("/repo"))
    assert isinstance(auth, Author)
    assert isinstance(contract, AuthoredContract)
    assert contract.acceptance == "pytest -q"
    assert contract.findings == ["F1", "F2"]
    assert "## Steps" in contract.plan_md


# --- sad path -----------------------------------------------------------------
def test_sad_invalid_plan_md_returns_none_not_a_bad_contract() -> None:
    bad = "# no steps section\n\n## Meta\nsurfaces: [cli]\nacceptance: pytest\n"
    auth = LLMAuthor(run_agent=lambda p: _reply(plan=bad))
    assert auth.author([_finding()], Path("/repo")) is None


def test_sad_non_json_output_returns_none() -> None:
    auth = LLMAuthor(run_agent=lambda p: "I cannot author this safely.")
    assert auth.author([_finding()], Path("/repo")) is None


def test_sad_runner_raising_is_swallowed_to_none() -> None:
    def _boom(_p: str) -> str:
        raise RuntimeError("died")

    assert LLMAuthor(run_agent=_boom).author([_finding()], Path("/repo")) is None


# --- edge cases ---------------------------------------------------------------
def test_edge_empty_findings_returns_none_without_calling_agent() -> None:
    calls = {"n": 0}

    def _runner(_p: str) -> str:
        calls["n"] += 1
        return _reply()

    assert LLMAuthor(run_agent=_runner).author([], Path("/repo")) is None
    assert calls["n"] == 0


def test_edge_missing_plan_md_key_returns_none() -> None:
    auth = LLMAuthor(run_agent=lambda p: json.dumps({"acceptance": "pytest"}))
    assert auth.author([_finding()], Path("/repo")) is None


def test_edge_missing_acceptance_falls_back_to_plan_meta_acceptance() -> None:
    # the contract still needs an acceptance script; if the model omits the
    # top-level field but the PLAN.md Meta declares one, use that.
    auth = LLMAuthor(run_agent=lambda p: json.dumps({"plan_md": VALID_PLAN}))
    contract = auth.author([_finding()], Path("/repo"))
    assert contract is not None
    assert contract.acceptance.strip() != ""


def test_edge_prompt_includes_finding_summaries() -> None:
    seen = {}

    def _capture(prompt: str) -> str:
        seen["p"] = prompt
        return _reply()

    LLMAuthor(run_agent=_capture).author([_finding("F1")], Path("/repo"))
    assert "off-by-one" in seen["p"]
