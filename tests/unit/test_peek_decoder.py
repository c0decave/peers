import json
import os
import time

from peers.peek import decode_event, newest_session_jsonl, tail_session


def test_decode_event_tool_text_and_result():
    tool = {
        "type": "assistant",
        "timestamp": "2026-05-27T13:00:00.000Z",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Bash",
                 "input": {"command": "echo hi"}},
                {"type": "text", "text": "working"},
            ]
        },
    }
    result = {
        "type": "user",
        "timestamp": "2026-05-27T13:00:01.000Z",
        "message": {
            "content": [
                {"type": "tool_result", "content": "boom",
                 "is_error": True},
            ]
        },
    }

    lines = list(decode_event(tool)) + list(decode_event(result))

    assert any("TOOL" in line and "Bash" in line for line in lines)
    assert any("TEXT" in line and "working" in line for line in lines)
    assert any("RES" in line and "err=True" in line for line in lines)


def test_decode_event_skips_noise():
    for event_type in ("queue-operation", "ai-title", "last-prompt"):
        assert list(decode_event({"type": event_type})) == []


def test_newest_session_jsonl(tmp_path):
    old = tmp_path / "a.jsonl"
    new = tmp_path / "b.jsonl"
    old.write_text("{}\n")
    new.write_text("{}\n")
    stale = time.time() - 3600
    os.utime(old, (stale, stale))

    assert newest_session_jsonl(tmp_path) == new


def test_tail_session_no_follow_decodes_existing_lines(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(json.dumps({
        "type": "assistant",
        "timestamp": "2026-05-27T13:00:00.000Z",
        "message": {"content": [{"type": "text", "text": "hello"}]},
    }) + "\n")

    assert list(tail_session(path, follow=False)) == [
        "13:00:00 assistant TEXT: hello"
    ]


def test_decode_event_reads_string_message_as_TEXT():
    # Happy path: when message.content is a bare string (not a list of
    # blocks), the decoder must still emit a TEXT line for the operator.
    ev = {
        "type": "assistant",
        "timestamp": "2026-05-27T13:00:00.000Z",
        "message": {"content": "plain message body"},
    }
    lines = list(decode_event(ev))
    assert lines == ["13:00:00 assistant TEXT: plain message body"]


def test_decode_event_truncates_long_text_to_bounded_size():
    # Edge: an oversized content string must be truncated to
    # MAX_RENDERED_VALUE so a single noisy event can't blow the tail
    # renderer's line width.
    from peers.peek import MAX_RENDERED_VALUE
    huge = "x" * (MAX_RENDERED_VALUE + 200)
    ev = {
        "type": "assistant",
        "timestamp": "2026-05-27T13:00:00.000Z",
        "message": {"content": huge},
    }
    [line] = list(decode_event(ev))
    body = line.split("TEXT: ", 1)[1]
    assert len(body) <= MAX_RENDERED_VALUE
    assert body.endswith("...")


def test_tail_session_rejects_malformed_json_lines(tmp_path):
    # Sad: a corrupt/garbage line inside the JSONL stream must NOT raise
    # — the tailer skips it and keeps decoding the rest of the file.
    path = tmp_path / "s.jsonl"
    path.write_text(
        "{not-json\n"
        + json.dumps({
            "type": "assistant",
            "timestamp": "2026-05-27T13:00:00.000Z",
            "message": {"content": [{"type": "text", "text": "ok"}]},
        }) + "\n"
    )
    out = list(tail_session(path, follow=False))
    assert out == ["13:00:00 assistant TEXT: ok"]
