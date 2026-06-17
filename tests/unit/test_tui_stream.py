"""Wave-1b Unit H: the streaming-subprocess helper + the Live-Stream decoder.

Both are pure-ish substrate (no Textual) so they run under the default-python
interpreter — they are NOT skipif-guarded. ``stream_verb`` launches a long-lived
list-arg subprocess (NO shell), yields its stdout lines incrementally, and
``close()`` kills the whole process group. ``decode_stream_line`` turns one raw
peer-stream line into a ``(kind, text)`` the Live panel colors.

Tests cover all three categories per the project rule:
  * happy: a fixture command streams 3 lines -> 3 yielded lines; a claude
    session-jsonl event decodes to TEXT/TOOL/RES; a codex turn event decodes.
  * sad:   a bad binary -> no crash, error surfaced, no lines; a non-JSON line
    for codex/opencode -> a raw row; garbage -> raw.
  * edge:  close() mid-stream terminates the process (it is gone afterwards);
    an empty line; an unknown tool falls back to raw.
"""
from __future__ import annotations

import json
import sys
import time

from peers_ctl.tui import actions as A

# --------------------------------------------------------------------------- #
# a short-lived fixture command that flushes 3 numbered lines, then exits.     #
# --------------------------------------------------------------------------- #
_THREE_LINES = (
    "import sys\n"
    "for i in range(3):\n"
    "    print(i)\n"
    "    sys.stdout.flush()\n"
)

# a long-lived fixture: prints one line, then sleeps "forever" so close() has
# something live to kill.
_SLEEP_FOREVER = (
    "import sys, time\n"
    "print('alive')\n"
    "sys.stdout.flush()\n"
    "time.sleep(60)\n"
)


# --------------------------------------------------------------------------- #
# stream_verb — happy                                                          #
# --------------------------------------------------------------------------- #
def test_stream_verb_happy_yields_all_lines():
    handle = A.stream_verb([sys.executable, "-u", "-c", _THREE_LINES])
    try:
        got: list[str] = []
        deadline = time.time() + 10.0
        while len(got) < 3 and time.time() < deadline:
            line = handle.read_line(timeout=0.5)
            if line is not None:
                got.append(line.rstrip("\n"))
        assert got == ["0", "1", "2"]
        assert handle.error is None
    finally:
        handle.close()


def test_stream_verb_happy_iter_lines_drains_to_eof():
    handle = A.stream_verb([sys.executable, "-u", "-c", _THREE_LINES])
    try:
        # iter_lines() blocks-collects until the process closes its stdout.
        got = [ln.rstrip("\n") for ln in handle.iter_lines()]
        assert got == ["0", "1", "2"]
    finally:
        handle.close()


# --------------------------------------------------------------------------- #
# stream_verb — sad: a bad binary never crashes; error is surfaced             #
# --------------------------------------------------------------------------- #
def test_stream_verb_sad_bad_binary_surfaces_error_no_crash():
    handle = A.stream_verb(["this-binary-does-not-exist-zzz", "--nope"])
    try:
        # no lines, no raise; the spawn error is surfaced on the handle.
        assert handle.read_line(timeout=0.5) is None
        assert handle.error is not None
        assert not handle.is_running()
        # iter_lines on a failed spawn yields nothing.
        assert list(handle.iter_lines()) == []
    finally:
        handle.close()  # must be safe even on a never-spawned handle


# --------------------------------------------------------------------------- #
# stream_verb — edge: close() mid-stream kills the live process               #
# --------------------------------------------------------------------------- #
def test_stream_verb_edge_close_terminates_running_process():
    handle = A.stream_verb([sys.executable, "-u", "-c", _SLEEP_FOREVER])
    # wait for the first line so we know it is live.
    deadline = time.time() + 10.0
    first = None
    while first is None and time.time() < deadline:
        first = handle.read_line(timeout=0.5)
    assert first is not None and first.rstrip("\n") == "alive"
    assert handle.is_running()
    pid = handle.pid
    assert pid is not None

    handle.close()

    # after close() the process must be gone (poll until reaped, bounded).
    gone_deadline = time.time() + 5.0
    while handle.is_running() and time.time() < gone_deadline:
        time.sleep(0.05)
    assert not handle.is_running()
    # double-close is idempotent / safe.
    handle.close()


def test_stream_verb_edge_close_is_idempotent_on_finished_process():
    handle = A.stream_verb([sys.executable, "-u", "-c", _THREE_LINES])
    list(handle.iter_lines())  # drain to natural exit
    assert not handle.is_running()
    handle.close()
    handle.close()  # no raise


# --------------------------------------------------------------------------- #
# decode_stream_line — claude (reuse peers.peek.decode_event)                  #
# --------------------------------------------------------------------------- #
def _claude_text_event(text: str) -> str:
    return json.dumps({
        "type": "assistant",
        "timestamp": "2026-06-11T10:00:00.000Z",
        "message": {"content": [{"type": "text", "text": text}]},
    })


def _claude_tool_event(name: str) -> str:
    return json.dumps({
        "type": "assistant",
        "timestamp": "2026-06-11T10:00:01.000Z",
        "message": {"content": [{"type": "tool_use", "name": name, "input": {}}]},
    })


def _claude_result_event(is_error: bool) -> str:
    return json.dumps({
        "type": "user",
        "timestamp": "2026-06-11T10:00:02.000Z",
        "message": {"content": [
            {"type": "tool_result", "is_error": is_error, "content": "out"}]},
    })


def test_decode_claude_text_is_text_kind():
    rows = A.decode_stream_line(_claude_text_event("hello world"), tool="claude")
    assert len(rows) == 1
    kind, text = rows[0]
    assert kind == "text"
    assert "hello world" in text
    assert "TEXT" in text


def test_decode_claude_tool_is_tool_kind():
    rows = A.decode_stream_line(_claude_tool_event("Bash"), tool="claude")
    assert len(rows) == 1
    kind, text = rows[0]
    assert kind == "tool"
    assert "Bash" in text


def test_decode_claude_tool_result_error_is_result_kind():
    rows = A.decode_stream_line(_claude_result_event(is_error=True), tool="claude")
    assert len(rows) == 1
    kind, _text = rows[0]
    assert kind == "result"


def test_decode_claude_noise_event_yields_nothing():
    # a NOISE_TYPES event (decode_event yields nothing) -> no rows.
    noise = json.dumps({"type": "queue-operation", "message": {"content": "x"}})
    assert A.decode_stream_line(noise, tool="claude") == []


# --------------------------------------------------------------------------- #
# decode_stream_line — codex / opencode JSON events                           #
# --------------------------------------------------------------------------- #
def test_decode_codex_turn_completed_json_line():
    line = json.dumps({"type": "turn.completed",
                       "usage": {"input_tokens": 10, "output_tokens": 5}})
    rows = A.decode_stream_line(line, tool="codex")
    assert len(rows) == 1
    kind, text = rows[0]
    assert kind in ("result", "text")
    assert "turn.completed" in text


def test_decode_codex_error_event_is_error_kind():
    line = json.dumps({"type": "error", "message": "boom"})
    rows = A.decode_stream_line(line, tool="codex")
    assert len(rows) == 1
    kind, text = rows[0]
    assert kind == "error"
    assert "boom" in text


def test_decode_opencode_json_message_line():
    line = json.dumps({"type": "message", "text": "thinking"})
    rows = A.decode_stream_line(line, tool="opencode")
    assert len(rows) == 1
    kind, text = rows[0]
    assert kind in ("text", "raw")
    assert "thinking" in text


# --------------------------------------------------------------------------- #
# decode_stream_line — sad/edge: non-JSON, empty, unknown tool -> raw          #
# --------------------------------------------------------------------------- #
def test_decode_non_json_line_is_raw():
    rows = A.decode_stream_line("just some plain log text", tool="codex")
    assert rows == [("raw", "just some plain log text")]


def test_decode_claude_non_json_line_is_raw():
    # a torn / non-json line on the claude stream still fails soft to raw.
    rows = A.decode_stream_line("not json at all", tool="claude")
    assert rows == [("raw", "not json at all")]


def test_decode_empty_line_yields_nothing():
    assert A.decode_stream_line("", tool="claude") == []
    assert A.decode_stream_line("   \n", tool="codex") == []


def test_decode_unknown_tool_falls_back_to_raw():
    rows = A.decode_stream_line('{"type":"x"}', tool="mystery-tool")
    assert rows == [("raw", '{"type":"x"}')]


def test_decode_stream_line_truncates_raw():
    # `.peers` logs are agent-writable: a malformed / huge line must NOT reach
    # the Live panel unbounded. Every returned raw row is truncated to the same
    # limit the JSON/claude branches already use (the default of `_truncate`).
    limit = 160
    huge = "x" * 100_000
    # 1) non-JSON line on a JSON tool (codex) -> raw fall-through.
    for tool in ("codex", "opencode", "claude"):
        rows = A.decode_stream_line(huge, tool=tool)
        assert rows, f"{tool} produced no rows"
        for kind, text in rows:
            assert len(text) <= limit, (tool, kind, len(text))
    # 2) unknown-tool / default path -> raw fall-through.
    rows = A.decode_stream_line(huge, tool="mystery-tool")
    assert rows
    for _kind, text in rows:
        assert len(text) <= limit, len(text)
