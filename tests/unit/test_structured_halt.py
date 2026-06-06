"""Option C (v15 internal testing follow-up): structured-error halt classification.

The free-text halt_patterns scan the peer's whole output stream, so a peer
echoing repo content that *describes* an error (a `git log` subject, a
bug-report) can trip a halt on its own echo — the self-referential livelock
the v15 internal testing hit. The structural fix keys the halt off the CLI's OWN
structured status channel, which a quoted echo cannot forge. For `claude
--output-format stream-json` that channel is the terminal
`{"type":"result","is_error":...}` envelope.
"""
from __future__ import annotations

import json

from peers.structured_halt import classify_structured_halt


def _stream(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def test_claude_success_result_is_not_a_halt():
    out = _stream(
        {"type": "system", "subtype": "init", "session_id": "x"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "done"}]}},
        {"type": "result", "subtype": "success", "is_error": False,
         "result": "all good", "total_cost_usd": 0.1},
    )
    assert classify_structured_halt("claude", out, "", 0) is None


def test_claude_is_error_usage_limit_result_halts():
    out = _stream(
        {"type": "result", "subtype": "error_during_execution",
         "is_error": True,
         "result": "You've hit your usage limit. Visit ... to purchase more."},
    )
    verdict = classify_structured_halt("claude", out, "", 1)
    assert verdict is not None
    label, snippet = verdict
    assert label.startswith("structured:claude:")
    assert "usage limit" in snippet


def test_claude_is_error_quota_in_subtype_halts():
    out = _stream(
        {"type": "result", "subtype": "error_quota_exhausted",
         "is_error": True, "result": "stopped"},
    )
    verdict = classify_structured_halt("claude", out, "", 1)
    assert verdict is not None
    assert "quota" in verdict[0]


def test_claude_is_error_generic_execution_is_not_a_halt():
    """is_error alone is not a halt — a transient execution error is
    retryable. Only auth/quota/usage-limit classes halt the whole run."""
    out = _stream(
        {"type": "result", "subtype": "error_during_execution",
         "is_error": True, "result": "tool call failed: connection reset"},
    )
    assert classify_structured_halt("claude", out, "", 1) is None


def test_claude_echoed_git_log_in_text_event_is_not_a_halt():
    """Echo immunity: the v15 trigger — a peer echoing a git-log subject that
    literally contains ERROR + quota-exhausted — lives in an assistant/text
    event, never the result envelope's is_error. It must NOT halt."""
    out = _stream(
        {"type": "assistant", "message": {"content": [{"type": "text",
         "text": "481e64d BUG-122: docs line carries verbatim ERROR + "
                 "quota-exhausted shape"}]}},
        {"type": "result", "subtype": "success", "is_error": False,
         "result": "reviewed git log"},
    )
    assert classify_structured_halt("claude", out, "", 0) is None


def test_codex_text_mode_without_json_events_returns_none():
    """Plain `codex exec` (no --json) emits no structured events — a bare
    usage-limit text line is not a structured event, so the classifier
    returns None and that line stays on the (echo-guarded) regex path."""
    out = "ERROR: You've hit your usage limit. Visit ...\n"
    assert classify_structured_halt("codex", out, "", 1) is None


def test_codex_json_turn_failed_quota_halts():
    """codex --json emits `{"type":"error",...}` + `{"type":"turn.failed",
    "error":{"message":...}}` on failure (verified against codex-cli 0.133).
    A usage-limit/quota message there halts via the structured channel."""
    out = _stream(
        {"type": "thread.started", "thread_id": "x"},
        {"type": "turn.started"},
        {"type": "error", "message":
            '{"type":"error","status":429,"error":{"type":'
            '"usage_limit_reached","message":"You\'ve hit your usage limit."}}'},
        {"type": "turn.failed", "error": {"message":
            '{"error":{"message":"You have hit your usage limit."}}'}},
    )
    verdict = classify_structured_halt("codex", out, "", 1)
    assert verdict is not None
    assert verdict[0].startswith("structured:codex:")
    assert "usage limit" in verdict[1]


def test_codex_json_transient_error_is_not_a_halt():
    """A non-auth/quota codex error (e.g. a bad model / 400) is NOT a halt —
    it is retryable; only the auth/quota/usage-limit vocabulary halts."""
    out = _stream(
        {"type": "turn.started"},
        {"type": "turn.failed", "error": {"message":
            '{"error":{"type":"invalid_request_error","message":'
            '"The model is not supported."}}'}},
    )
    assert classify_structured_halt("codex", out, "", 1) is None


def test_codex_json_echoed_error_in_agent_message_is_not_a_halt():
    """Echo immunity for codex: the agent echoing a quota-shaped line lives in
    an `item.completed`/`agent_message` event, never an `error`/`turn.failed`
    event, so it must NOT halt."""
    out = _stream(
        {"type": "item.completed", "item": {"type": "agent_message",
         "text": "git log shows: 481e64d ERROR you have hit your usage "
                 "limit (quota exhausted)"}},
        {"type": "turn.completed", "usage": {"input_tokens": 5,
         "output_tokens": 2}},
    )
    assert classify_structured_halt("codex", out, "", 0) is None


def test_unknown_tool_returns_none():
    assert classify_structured_halt("opencode", "anything", "", 1) is None


def test_claude_malformed_and_empty_output_returns_none():
    assert classify_structured_halt("claude", "", "", 0) is None
    assert classify_structured_halt("claude", "not json\n{bad", "", 0) is None


def test_opencode_error_event_usage_limit_halts():
    """opencode --format json emits `{"type":"error","error":{"name":...,
    "data":{"message":...}}}` on failure (verified vs opencode 1.15.13). A
    usage-limit/quota message there halts via the structured channel."""
    out = _stream(
        {"type": "step_start", "part": {"type": "step-start"}},
        {"type": "error", "error": {"name": "UsageLimitError",
         "data": {"message": "You have hit your usage limit. Top up to "
                  "continue.", "ref": "err_x"}}},
    )
    verdict = classify_structured_halt("opencode", out, "", 1)
    assert verdict is not None
    assert verdict[0].startswith("structured:opencode:")
    assert "usage limit" in verdict[1]


def test_opencode_success_run_is_not_a_halt():
    out = _stream(
        {"type": "step_start", "part": {"type": "step-start"}},
        {"type": "tool_use", "part": {"type": "tool", "tool": "write"}},
        {"type": "step_finish", "part": {"type": "step-finish",
         "tokens": {"total": 100}, "cost": 0}},
        {"type": "text", "part": {"type": "text", "text": "done"}},
    )
    assert classify_structured_halt("opencode", out, "", 0) is None


def test_opencode_non_quota_error_is_not_a_halt():
    """A config/model error (not auth/quota) is not a halt."""
    out = _stream(
        {"type": "error", "error": {"name": "UnknownError",
         "data": {"message": "Model not found: foo/bar."}}},
    )
    assert classify_structured_halt("opencode", out, "", 1) is None


def test_opencode_echoed_error_in_text_event_is_not_a_halt():
    """Echo immunity: a quota-shaped line the agent prints lands in a `text`
    part, never a top-level `error` event, so it must NOT halt."""
    out = _stream(
        {"type": "text", "part": {"type": "text",
         "text": "the logs show ERROR you have hit your usage limit"}},
        {"type": "step_finish", "part": {"type": "step-finish",
         "tokens": {"total": 5}, "cost": 0}},
    )
    assert classify_structured_halt("opencode", out, "", 0) is None


def test_claude_pathological_json_does_not_raise():
    """W2 (review): deeply-nested JSON raises RecursionError (not a subclass
    of ValueError). The classifier runs in health_guard.invoke() post-join —
    an uncaught raise would crash the tick. It must degrade to None instead."""
    assert classify_structured_halt("claude", "{" * 200000, "", 1) is None
    assert classify_structured_halt("claude", "[" * 200000, "", 1) is None


def test_claude_single_object_json_output_form():
    """`claude --output-format json` (non-stream) emits one result object."""
    out = json.dumps({"type": "result", "subtype": "error", "is_error": True,
                      "result": "authentication failed: token expired"})
    verdict = classify_structured_halt("claude", out, "", 1)
    assert verdict is not None
    assert "authentication" in verdict[0]
