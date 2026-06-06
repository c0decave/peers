"""Transient server rate-limit handling (v17 internal testing operator finding).

A claude tick that hit an HTTP 429 ("Server is temporarily limiting requests
(not your usage limit) - Rate limited") was classified `process-fail`, which
counted against peer health -> 2 such ticks degraded claude -> the turn manager
then permanently benched the degraded peer while the healthy peer existed
(starvation), leaving 60% of the run single-peer.

A transient 429/503/529/overloaded reported on the CLI's STRUCTURED status
channel must instead be classified `rate-limited`: not a halt, not a hard
process-fail, no health penalty -> back off and retry the SAME peer.
"""
from __future__ import annotations

import json

from peers.health_guard import HealthGuard
from peers.structured_halt import (
    classify_structured_halt,
    classify_structured_transient,
)


def _stream(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


# --- structured_halt.classify_structured_transient (pure) ------------------

def test_claude_429_result_envelope_is_transient():
    out = _stream(
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {}}]}},
        {"type": "result", "subtype": "success", "is_error": True,
         "api_error_status": 429,
         "result": "API Error: Server is temporarily limiting requests "
                   "(not your usage limit) · Rate limited"},
    )
    verdict = classify_structured_transient("claude", out, "", 1)
    assert verdict is not None
    label, snippet = verdict
    assert "rate" in label.lower() or "429" in label
    # And it must NOT be misread as an unrecoverable halt.
    assert classify_structured_halt("claude", out, "", 1) is None


def test_claude_usage_limit_is_not_transient_halt_owns_it():
    out = _stream(
        {"type": "result", "subtype": "error_during_execution",
         "is_error": True,
         "result": "You've hit your usage limit. Visit ... to purchase more."},
    )
    # The unrecoverable usage-limit is a HALT, not a transient retry.
    assert classify_structured_transient("claude", out, "", 1) is None
    assert classify_structured_halt("claude", out, "", 1) is not None


def test_claude_success_is_not_transient():
    out = _stream(
        {"type": "result", "subtype": "success", "is_error": False,
         "result": "all good"},
    )
    assert classify_structured_transient("claude", out, "", 0) is None


def test_claude_overloaded_529_is_transient():
    out = _stream(
        {"type": "result", "is_error": True, "api_error_status": 529,
         "result": "Overloaded"},
    )
    assert classify_structured_transient("claude", out, "", 1) is not None


def test_codex_transient_rate_limit_via_vocab():
    out = _stream(
        {"type": "turn.failed", "error": {
            "message": "stream error: 429 Too Many Requests; rate limited, "
                       "retry later"}},
    )
    assert classify_structured_transient("codex", out, "", 1) is not None


def test_unknown_tool_is_none():
    assert classify_structured_transient("madeup", "boom", "", 1) is None


def test_incidental_status_number_is_not_transient():
    """I1 regression: a structured error that merely CONTAINS a number like 503
    (e.g. a non-zero process exit code echoed in the message) must NOT be
    misread as a transient rate-limit — that would mask a real failure as a
    harmless retry. Only HTTP reason-phrases (or claude's numeric
    api_error_status field) count."""
    out = _stream(
        {"type": "result", "is_error": True,
         "result": "Tool crashed: process exited with code 503"},
    )
    assert classify_structured_transient("claude", out, "", 1) is None
    # But the genuine numeric status field IS honored.
    out2 = _stream(
        {"type": "result", "is_error": True, "api_error_status": 503,
         "result": "internal error"},
    )
    assert classify_structured_transient("claude", out2, "", 1) is not None


# --- health_guard end-to-end: classification == "rate-limited" -------------

def test_invoke_classifies_claude_429_as_rate_limited(tmp_path):
    """A claude run whose stream-json result envelope reports a transient 429
    and exits 1 must classify `rate-limited` (NOT process-fail), and must NOT
    set halt_required."""
    hg = HealthGuard(cwd=tmp_path)
    envelope = json.dumps({
        "type": "result", "subtype": "success", "is_error": True,
        "api_error_status": 429,
        "result": "API Error: Server is temporarily limiting requests "
                  "(not your usage limit) · Rate limited",
    })
    script = tmp_path / "fake_claude_429.sh"
    script.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' '{envelope}'\n"
        "sleep 0.2\n"
        "exit 1\n"
    )
    script.chmod(0o755)
    r = hg.invoke(
        [str(script)], prompt="ignored",
        idle_timeout_s=10, absolute_max_runtime_s=10,
        tool="claude",
    )
    assert r.classification == "rate-limited"
    assert r.halt_required is False
