import json
import os
import time
from pathlib import Path

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


def test_newest_session_jsonl_skips_candidate_removed_during_lstat(
    tmp_path, monkeypatch
):
    old = tmp_path / "a.jsonl"
    gone = tmp_path / "b.jsonl"
    old.write_text("{}\n")
    gone.write_text("{}\n")
    stale = time.time() - 3600
    os.utime(old, (stale, stale))
    # Production refuses to follow symlinks, so it calls Path.lstat (not stat);
    # patch the method the code actually uses or the TOCTOU-vanish branch is
    # never exercised.
    real_lstat = Path.lstat

    def disappearing_lstat(self, *args, **kwargs):
        if self == gone:
            raise FileNotFoundError(str(self))
        return real_lstat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", disappearing_lstat)

    assert newest_session_jsonl(tmp_path) == old


def test_newest_session_jsonl_returns_none_when_all_candidates_vanish(
    tmp_path, monkeypatch
):
    first = tmp_path / "a.jsonl"
    second = tmp_path / "b.jsonl"
    first.write_text("{}\n")
    second.write_text("{}\n")
    real_lstat = Path.lstat

    def all_disappear(self, *args, **kwargs):
        if self.suffix == ".jsonl":
            raise FileNotFoundError(str(self))
        return real_lstat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", all_disappear)

    assert newest_session_jsonl(tmp_path) is None


def test_newest_session_jsonl_skips_symlinked_candidate(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    old = sessions / "a.jsonl"
    old.write_text("{}\n")
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n")
    linked = sessions / "b.jsonl"
    try:
        linked.symlink_to(outside)
    except OSError as exc:
        import pytest

        pytest.skip(f"symlink creation unavailable for this platform: {exc}")
    stale = time.time() - 3600
    os.utime(old, (stale, stale))

    assert newest_session_jsonl(sessions) == old


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


def test_tail_session_no_follow_refuses_symlinked_jsonl(tmp_path):
    outside = tmp_path / "outside.jsonl"
    outside.write_text(json.dumps({
        "type": "assistant",
        "timestamp": "2026-05-27T13:00:00.000Z",
        "message": {"content": [{"type": "text", "text": "outside"}]},
    }) + "\n")
    link = tmp_path / "linked.jsonl"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        import pytest

        pytest.skip(f"symlink creation unavailable for this platform: {exc}")

    assert list(tail_session(link, follow=False)) == []


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
