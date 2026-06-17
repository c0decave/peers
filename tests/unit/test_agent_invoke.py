"""Shared one-shot agent-invocation seam used by the develop/research LLM
adapters: substitute the prompt into a peer argv, run it once, return the
combined stdout+stderr text. Tested against the real fake-CLI fixtures so the
subprocess path itself is exercised (not mocked)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from peers.agent_invoke import (
    agent_runner_from_spec,
    extract_json_array,
    final_agent_text,
    run_agent_once,
)

FIX = Path(__file__).resolve().parents[1] / "fixtures"


# --- stream-json unwrap (the real-LLM-vs-fake gap) ----------------------------
# Peers' default peer argv runs claude with `--output-format stream-json
# --verbose`, which emits a SERIES of JSON event lines, not a plain answer. The
# LLM adapters expect the model's final text to extract a JSON array/object, so
# the seam must collapse the transcript to the final result text first; without
# it every research/develop/find-bugs/bring-up round went dry on a real peer.
_STREAM_JSON = "\n".join([
    json.dumps({"type": "system", "subtype": "init", "session_id": "x"}),
    json.dumps({"type": "assistant",
                "message": {"content": [{"type": "text", "text": "thinking"}]}}),
    json.dumps({"type": "result", "subtype": "success", "is_error": False,
                "result": '["qA", "qB"]'}),
])


def test_final_agent_text_unwraps_streamjson_result() -> None:
    assert final_agent_text(_STREAM_JSON) == '["qA", "qB"]'
    assert extract_json_array(final_agent_text(_STREAM_JSON)) == ["qA", "qB"]


def test_final_agent_text_passthrough_plain_text() -> None:
    assert final_agent_text('["qA"]') == '["qA"]'
    assert final_agent_text("just prose, no json here") == "just prose, no json here"


def test_final_agent_text_falls_back_to_assistant_text_without_result_event() -> None:
    sj = "\n".join([
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "assistant",
                     "message": {"content": [{"type": "text", "text": "answer here"}]}}),
    ])
    assert "answer here" in final_agent_text(sj)


def test_agent_runner_unwraps_streamjson_so_adapters_see_clean_text(tmp_path) -> None:
    """End-to-end at the adapter seam: a stream-json-emitting peer is unwrapped so
    extract_json_array recovers the sub-questions (the research dry-round bug)."""
    import types
    script = tmp_path / "fake_streamjson.py"
    script.write_text(
        "import json\n"
        "print(json.dumps({'type':'result','subtype':'success',"
        "'result':'[\\\"only-question\\\"]'}))\n"
    )
    spec = types.SimpleNamespace(
        argv=("python3", str(script), "{PROMPT}"),
        prompt_mode="argv-substitute",
    )
    run_agent = agent_runner_from_spec(spec)
    raw = run_agent("decompose this topic")
    assert extract_json_array(raw) == ["only-question"], f"raw={raw!r}"


# --- happy path ---------------------------------------------------------------
def test_happy_returns_stdout_text() -> None:
    out = run_agent_once("hi", argv=["sh", str(FIX / "fake_cli_ok.sh"), "{PROMPT}"])
    assert "doing work" in out


def test_happy_substitutes_prompt_into_argv() -> None:
    out = run_agent_once("MARKER-PROMPT-123", argv=["/bin/echo", "{PROMPT}"])
    assert "MARKER-PROMPT-123" in out


# --- sad path -----------------------------------------------------------------
def test_sad_nonzero_exit_still_returns_output_incl_stderr() -> None:
    # a failing run may still have emitted model text; don't lose it.
    out = run_agent_once("hi", argv=["sh", str(FIX / "fake_cli_fail.sh"), "{PROMPT}"])
    assert "bang" in out


def test_sad_timeout_raises() -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        run_agent_once(
            "hi", argv=["sh", str(FIX / "fake_cli_hang.sh"), "{PROMPT}"],
            timeout_s=0.5,
        )


# --- edge cases ---------------------------------------------------------------
def test_edge_runner_from_spec_builds_a_working_callable() -> None:
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _Spec:
        name: str
        tool: str
        argv: tuple[str, ...]

    spec = _Spec("claude", "claude", ("/bin/echo", "{PROMPT}"))
    run = agent_runner_from_spec(spec)
    assert "ABC" in run("ABC")


def test_edge_cwd_is_respected(tmp_path: Path) -> None:
    out = run_agent_once("hi", argv=["pwd"], cwd=tmp_path)
    assert str(tmp_path) in out


# --- CB-3: prompt_mode=stdin delivery ----------------------------------------
def test_happy_stdin_mode_delivers_prompt_on_stdin() -> None:
    # `cat` echoes stdin; with stdin=True the prompt is piped, not argv-substituted.
    out = run_agent_once("PIPED-PROMPT-XYZ", argv=["cat"], stdin=True)
    assert "PIPED-PROMPT-XYZ" in out


def test_edge_runner_from_spec_honors_stdin_prompt_mode() -> None:
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _Spec:
        name: str
        tool: str
        argv: tuple[str, ...]
        prompt_mode: str

    spec = _Spec("claude", "claude", ("cat",), "stdin")
    run = agent_runner_from_spec(spec)
    assert "STDIN-OK" in run("STDIN-OK")


def test_sad_empty_argv_raises() -> None:
    with pytest.raises(ValueError):
        run_agent_once("x", argv=[])


# --- TQ-06: the shared JSON extractors ---------------------------------------
def test_extract_json_array_happy_sad_edge() -> None:
    from peers.agent_invoke import extract_json_array
    assert extract_json_array('[1, 2, 3]') == [1, 2, 3]                  # happy
    assert extract_json_array('prose ```json\n["a"]\n``` more') == ["a"]  # fenced
    assert extract_json_array('no json here') is None                    # sad
    assert extract_json_array('{"k": 1}') is None                        # object, not array
    assert extract_json_array(None) is None                              # edge: non-str


def test_extract_json_object_happy_sad_edge() -> None:
    from peers.agent_invoke import extract_json_object
    assert extract_json_object('{"k": 1}') == {"k": 1}                   # happy
    assert extract_json_object('x ```json\n{"a": true}\n``` y') == {"a": True}  # fenced
    assert extract_json_object('nope') is None                           # sad
    assert extract_json_object('[1,2]') is None                          # array, not object
