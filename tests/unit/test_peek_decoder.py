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
