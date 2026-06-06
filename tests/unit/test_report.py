"""Direct unit tests for ``peers.report.summarise_stream_json_log``.

The summary helper aggregates Claude's ``--output-format stream-json``
events into a tiny structured record (tool counts, text emissions, cost,
turn count, error flag). It is consumed by the orchestrator after each
peer run; a regression here silently corrupts per-tick observability
output, which is why we cover happy/edge/sad paths directly rather than
relying on indirect coverage through the orchestrator integration tests.
"""
from __future__ import annotations

import json

from peers.report import StreamSummary, summarise_stream_json_log


def test_summarise_parses_assistant_tool_use_and_text_happy():
    # happy: a normal assistant+result pair produces the expected tally.
    log = "\n".join([
        json.dumps({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash",
                     "input": {"command": "echo hi"}},
                    {"type": "tool_use", "name": "Bash",
                     "input": {"command": "echo bye"}},
                    {"type": "text", "text": "thinking"},
                ]
            },
        }),
        json.dumps({
            "type": "result",
            "total_cost_usd": 0.0125,
            "num_turns": 3,
            "is_error": False,
        }),
    ])

    summary = summarise_stream_json_log(log)

    assert isinstance(summary, StreamSummary)
    assert summary.tool_counts["Bash"] == 2
    assert summary.text_emissions == 1
    assert summary.total_cost_usd == 0.0125
    assert summary.num_turns == 3
    assert summary.is_error is False


def test_summarise_handles_empty_log_text_edge():
    # edge: an empty / whitespace-only input must not crash and must
    # return a StreamSummary with zeroed defaults — operators frequently
    # see this when a peer is starved before it emits any event.
    for blob in ("", "   ", "\n\n", "\r\n\r\n"):
        summary = summarise_stream_json_log(blob)
        assert summary.tool_counts == {}
        assert summary.text_emissions == 0
        assert summary.total_cost_usd is None
        assert summary.num_turns is None
        assert summary.is_error is None


def test_summarise_handles_unicode_in_tool_name_and_text_edge():
    # edge: tool names and text blocks may carry unicode (rare, but
    # peers can pass non-ASCII through). The aggregator stores names
    # verbatim — confirm the round-trip and the text counter both fire.
    log = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "🛠️Build", "input": {}},
            {"type": "text", "text": "ÜberRoundtrip"},
        ]},
    })

    summary = summarise_stream_json_log(log)

    assert summary.tool_counts["🛠️Build"] == 1
    assert summary.text_emissions == 1


def test_summarise_handles_oversized_log_with_thousands_of_events_edge():
    # edge: a long peer run produces O(n) events; the aggregator must
    # remain linear (no n^2 dict-blowup) and bound memory growth to
    # the tool_counts dict only. We assert the final counts, not the
    # speed — the test would just hang if the impl were quadratic.
    events = []
    for _ in range(2000):
        events.append(json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "Read", "input": {}},
            ]},
        }))
    summary = summarise_stream_json_log("\n".join(events))
    assert summary.tool_counts["Read"] == 2000


def test_summarise_skips_malformed_json_lines_sad():
    # sad: garbage interleaved between valid events must be skipped
    # silently (peers occasionally emit a partial line at process
    # death; we don't want a single bad line to nuke the entire
    # summary).
    log = "\n".join([
        "not-json-at-all",
        json.dumps({"type": "assistant",
                    "message": {"content": [
                        {"type": "tool_use", "name": "Edit", "input": {}},
                    ]}}),
        "{also not valid json",
        "[1, 2, 3]",  # valid JSON but not a dict
        json.dumps({"type": "result", "total_cost_usd": 0.01,
                    "num_turns": 1, "is_error": True}),
    ])

    summary = summarise_stream_json_log(log)

    assert summary.tool_counts["Edit"] == 1
    assert summary.text_emissions == 0
    assert summary.total_cost_usd == 0.01
    assert summary.num_turns == 1
    assert summary.is_error is True


def test_summarise_rejects_wrong_types_in_result_fields_sad():
    # sad: a malformed `result` event with non-numeric cost / non-int
    # turns / non-bool is_error must not propagate the bogus types —
    # the summary defaults each to None so downstream renderers don't
    # crash on `f"{cost:.4f}"`.
    log = json.dumps({
        "type": "result",
        "total_cost_usd": "not-a-number",
        "num_turns": "many",
        "is_error": "maybe",
    })

    summary = summarise_stream_json_log(log)

    assert summary.total_cost_usd is None
    assert summary.num_turns is None
    assert summary.is_error is None


def test_summarise_handles_content_field_that_is_not_a_list_sad():
    # sad: an assistant event whose `content` is a dict / string / None
    # is malformed for stream-json shape — the aggregator must skip it
    # without raising on `for item in content`.
    for bogus_content in ("just a string", {"a": 1}, None, 42):
        log = json.dumps({
            "type": "assistant",
            "message": {"content": bogus_content},
        })
        summary = summarise_stream_json_log(log)
        assert summary.tool_counts == {}
        assert summary.text_emissions == 0


def test_summarise_rejects_bool_as_numeric():
    """BUG-131 (v16 internal testing): bool is an int subclass, so a malformed
    result event with total_cost_usd: true / num_turns: false must NOT be
    coerced to 1.0 / False — non-numeric values are rejected (None)."""
    line = json.dumps(
        {"type": "result", "total_cost_usd": True, "num_turns": False}
    )
    s = summarise_stream_json_log(line + "\n")
    assert s.total_cost_usd is None
    assert s.num_turns is None
