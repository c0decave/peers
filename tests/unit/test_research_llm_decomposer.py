"""R5: a real research ``Decomposer`` adapter (``LLMDecomposer``) — breaks a
topic into sub-questions via an injected agent. Mirrors the develop LLMAuditor
pattern: fail-closed to an EMPTY DecomposeResult on a runner error / non-JSON /
malformed output (an empty decompose is an honest dry round, never fabricated
sub-questions), and never raises into ``drive()``.
"""
from __future__ import annotations

import json
from pathlib import Path

from peers.research.adapters import LLMDecomposer
from peers.research.ports import Decomposer, DecomposeResult


# --- happy path ---------------------------------------------------------------
def test_happy_parses_json_array_of_sub_questions() -> None:
    payload = json.dumps(["What is X?", "How does Y interact with Z?"])
    dec = LLMDecomposer(run_agent=lambda p: payload)
    out = dec.decompose("the topic", Path("/repo"))
    assert isinstance(dec, Decomposer)
    assert isinstance(out, DecomposeResult)
    assert out.sub_questions == ["What is X?", "How does Y interact with Z?"]


def test_happy_extracts_fenced_block() -> None:
    body = "Here are the sub-questions:\n```json\n" + json.dumps(["q1", "q2"]) + "\n```\n"
    out = LLMDecomposer(run_agent=lambda p: body).decompose("t", Path("/r"))
    assert out.sub_questions == ["q1", "q2"]


# --- sad path -----------------------------------------------------------------
def test_sad_non_json_yields_empty() -> None:
    out = LLMDecomposer(run_agent=lambda p: "I need more detail.").decompose("t", Path("/r"))
    assert out.sub_questions == []


def test_sad_runner_error_yields_empty_never_raises() -> None:
    def boom(_p: str) -> str:
        raise RuntimeError("died")

    assert LLMDecomposer(run_agent=boom).decompose("t", Path("/r")).sub_questions == []


# --- edge cases ---------------------------------------------------------------
def test_edge_non_string_and_empty_entries_dropped() -> None:
    payload = json.dumps(["good one", "", 42, None, "  ", "another"])
    out = LLMDecomposer(run_agent=lambda p: payload).decompose("t", Path("/r"))
    assert out.sub_questions == ["good one", "another"]


def test_edge_capped_at_max() -> None:
    payload = json.dumps([f"q{i}" for i in range(50)])
    out = LLMDecomposer(run_agent=lambda p: payload, max_questions=4).decompose("t", Path("/r"))
    assert len(out.sub_questions) == 4


def test_edge_prompt_includes_topic() -> None:
    seen = {}

    def cap(prompt: str) -> str:
        seen["p"] = prompt
        return "[]"

    LLMDecomposer(run_agent=cap).decompose("MY-UNIQUE-TOPIC", Path("/r"))
    assert "MY-UNIQUE-TOPIC" in seen["p"]


# --- (C) self-referential apparatus guard -------------------------------------
# Defense-in-depth over the shipped relevance gate (A) + §5.2 single-codebase-
# origin fix: a sub-question that interrogates the peers research *apparatus*
# itself (`.peers/config.yaml`, `seed_urls`, the RESEARCH.md/TOPIC.md/run.jsonl
# artifacts) is degenerate — it is trivially true and was the documented driver
# of the q6/q7 vacuous "confirmed" (docs/audits/2026-06-15-research-confirmation-
# seed-vacuity.md). Drop it at the source so it never enters the pipeline.

def test_apparatus_subquestion_about_config_seed_urls_is_dropped() -> None:
    payload = json.dumps([
        "What is the ARM-32 ASLR maximum entropy?",  # real -> kept
        "Does .peers/config.yaml seed_urls include a source for %n?",  # drop
    ])
    out = LLMDecomposer(run_agent=lambda p: payload).decompose("t", Path("/r"))
    assert out.sub_questions == ["What is the ARM-32 ASLR maximum entropy?"]


def test_apparatus_subquestion_about_research_artifacts_is_dropped() -> None:
    payload = json.dumps([
        "Should gap §12.4 be flagged unresolved in RESEARCH.md?",  # drop (artifact)
        "Is TOPIC.md missing a frameworks section?",                # drop (artifact)
        "How does run.jsonl record confirmed claims?",              # drop (artifact)
        "How does CET enforce shadow-stack return addresses?",      # real -> kept
    ])
    out = LLMDecomposer(run_agent=lambda p: payload).decompose("t", Path("/r"))
    assert out.sub_questions == [
        "How does CET enforce shadow-stack return addresses?"
    ]


def test_apparatus_guard_is_case_insensitive() -> None:
    payload = json.dumps(["Does .PEERS/Config.YAML list SEED_URLS for this topic?"])
    out = LLMDecomposer(run_agent=lambda p: payload).decompose("t", Path("/r"))
    assert out.sub_questions == []


def test_apparatus_guard_does_not_over_match_legitimate_questions() -> None:
    # "configuration", "seed", "topic", "research" in ordinary prose are NOT
    # apparatus references — only the concrete peers paths/filenames are.
    payload = json.dumps([
        "How is the kernel ASLR configuration entropy seeded at boot?",
        "What research exists on heap grooming for the topic of UAF?",
    ])
    out = LLMDecomposer(run_agent=lambda p: payload).decompose("t", Path("/r"))
    assert len(out.sub_questions) == 2
