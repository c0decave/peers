"""R2: a real ``Auditor`` adapter (``LLMAuditor``) that drives an injected
one-shot agent runner and parses its output into :class:`Finding` objects.

Honesty note: findings are only *candidates* — the develop frontend feeds them
through adversarial-verify + the spine gates, which re-derive convergence. So a
parse failure must yield ``[]`` (an honest dry round), NEVER a fabricated
finding, and the adapter must never raise into ``drive()``.
"""
from __future__ import annotations

import json
from pathlib import Path

from peers.develop.adapters import LLMAuditor
from peers.develop.ports import Auditor, Finding


def _finding_dict(fid: str, dim: str = "correctness") -> dict:
    return {
        "id": fid,
        "dimension": dim,
        "severity": "high",
        "location": "src/x.py:10",
        "summary": "off-by-one in loop bound",
        "fix": "use <= instead of <",
        "fail_first": "test_x_handles_last_element",
    }


# --- happy path ---------------------------------------------------------------
def test_happy_parses_json_array_of_findings() -> None:
    payload = json.dumps([_finding_dict("F1"), _finding_dict("F2", "security")])
    aud = LLMAuditor(run_agent=lambda prompt: payload)
    out = aud.audit(Path("/repo"), ["correctness", "security"])
    assert isinstance(aud, Auditor)  # satisfies the runtime_checkable protocol
    assert [f.id for f in out] == ["F1", "F2"]
    assert all(isinstance(f, Finding) for f in out)
    assert out[0].fail_first == "test_x_handles_last_element"


def test_happy_extracts_fenced_json_block_from_chatty_output() -> None:
    body = (
        "Here are the findings I discovered:\n\n"
        "```json\n" + json.dumps([_finding_dict("F1")]) + "\n```\n"
        "Let me know if you want more.\n"
    )
    aud = LLMAuditor(run_agent=lambda prompt: body)
    out = aud.audit(Path("/repo"), ["correctness"])
    assert [f.id for f in out] == ["F1"]


# --- sad path -----------------------------------------------------------------
def test_sad_non_json_output_yields_empty_not_fabricated() -> None:
    aud = LLMAuditor(run_agent=lambda prompt: "I could not find anything actionable.")
    assert aud.audit(Path("/repo"), ["correctness"]) == []


def test_sad_runner_raising_is_swallowed_to_empty_never_into_drive() -> None:
    def _boom(_prompt: str) -> str:
        raise RuntimeError("model process died")

    aud = LLMAuditor(run_agent=_boom)
    assert aud.audit(Path("/repo"), ["correctness"]) == []


# --- edge cases ---------------------------------------------------------------
def test_edge_malformed_entries_skipped_kept_entries_survive() -> None:
    payload = json.dumps([
        _finding_dict("F1"),
        {"id": "F2"},                       # missing required fields -> skip
        {"dimension": "correctness"},       # missing id -> skip
        "not even an object",               # wrong type -> skip
        _finding_dict("F3"),
    ])
    out = LLMAuditor(run_agent=lambda p: payload).audit(Path("/repo"), ["correctness"])
    assert [f.id for f in out] == ["F1", "F3"]


def test_edge_findings_capped_at_max() -> None:
    payload = json.dumps([_finding_dict(f"F{i}") for i in range(50)])
    out = LLMAuditor(run_agent=lambda p: payload, max_findings=5).audit(
        Path("/repo"), ["correctness"])
    assert len(out) == 5


def test_edge_dimension_outside_requested_is_dropped() -> None:
    payload = json.dumps([
        _finding_dict("F1", "correctness"),
        _finding_dict("F2", "telepathy"),   # not requested -> drop
    ])
    out = LLMAuditor(run_agent=lambda p: payload).audit(Path("/repo"), ["correctness"])
    assert [f.id for f in out] == ["F1"]


def test_edge_prompt_includes_repo_and_dimensions() -> None:
    seen = {}

    def _capture(prompt: str) -> str:
        seen["prompt"] = prompt
        return "[]"

    LLMAuditor(run_agent=_capture).audit(Path("/srv/myrepo"), ["security", "perf"])
    assert "myrepo" in seen["prompt"]
    assert "security" in seen["prompt"] and "perf" in seen["prompt"]
