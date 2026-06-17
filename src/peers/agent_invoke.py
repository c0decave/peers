"""One-shot agent invocation — the production seam the develop/research LLM
adapters inject. It substitutes the prompt into a peer ``argv`` and runs the
tool once, returning the combined stdout+stderr (token/cost banners and model
text appear across both, mirroring
:func:`peers.budget_accountant.account_tokens_usd`).

This is deliberately a *one-shot* helper (audit/author discovery turns), NOT the
long-lived streamed tick loop (that is :mod:`peers.health_guard`). Adapters keep
it injected so they stay unit-testable with fakes; production wires it to the
configured peer spec via :func:`agent_runner_from_spec`.
"""
from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

#: Default one-shot timeout (s). Audit/author turns are bounded single calls.
DEFAULT_AGENT_TIMEOUT_S = 600.0


def extract_json_array(text: str) -> list | None:
    """Best-effort recovery of a JSON array from chatty agent output (a fenced
    ```json block first, then a bare ``[ ... ]`` span). Returns ``None`` when
    nothing parses — callers treat that as a dry round, never fabricated data."""
    if not isinstance(text, str):
        return None
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    candidates = []
    if fence:
        candidates.append(fence.group(1))
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start:end + 1])
    for blob in candidates:
        try:
            parsed = json.loads(blob)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, list):
            return parsed
    return None


def extract_json_object(text: str) -> dict | None:
    """Best-effort recovery of a JSON object from chatty agent output. ``None``
    when nothing parses."""
    if not isinstance(text, str):
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = []
    if fence:
        candidates.append(fence.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start:end + 1])
    for blob in candidates:
        try:
            parsed = json.loads(blob)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def final_agent_text(raw: str) -> str:
    """Collapse a claude-code stream-json transcript to the model's final text.

    The default peer argv runs claude with ``--output-format stream-json
    --verbose``, which emits a SERIES of JSON event lines (system / assistant /
    result / ...) rather than a plain answer. The LLM adapters expect the model's
    final text so they can recover a JSON array/object from it; run on the raw
    transcript, ``extract_json_*`` grabs unrelated spans and fails, turning every
    research/develop/find-bugs/bring-up round into a silent dry-round on a real
    peer. Collapse to the LAST ``result`` event's ``result`` string, falling back
    to concatenated assistant text. Output that isn't recognisable stream-json
    (plain text, or another shape) is returned UNCHANGED (fail-safe: never lose a
    plain-text answer)."""
    if not isinstance(raw, str) or "{" not in raw:
        return raw
    result_text: str | None = None
    assistant_chunks: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(ev, dict) or "type" not in ev:
            continue
        if ev.get("type") == "result" and isinstance(ev.get("result"), str):
            result_text = ev["result"]
        elif ev.get("type") == "assistant":
            msg = ev.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, list):
                for block in content:
                    if (isinstance(block, dict) and block.get("type") == "text"
                            and isinstance(block.get("text"), str)):
                        assistant_chunks.append(block["text"])
    if result_text is not None:
        return result_text
    if assistant_chunks:
        return "\n".join(assistant_chunks)
    return raw


def run_agent_once(
    prompt: str,
    *,
    argv: Sequence[str],
    cwd: str | Path | None = None,
    timeout_s: float = DEFAULT_AGENT_TIMEOUT_S,
    env: Mapping[str, str] | None = None,
    stdin: bool = False,
) -> str:
    """Run ``argv`` once, returning stdout+stderr.

    With ``stdin=False`` (``argv-substitute`` peers) the prompt replaces the
    ``{PROMPT}`` argv element. With ``stdin=True`` (``prompt_mode: stdin`` peers,
    e.g. opencode) the prompt is piped on stdin and argv is run as-is — without
    this, a stdin peer would receive NO prompt (CB-3).

    A non-zero exit does NOT raise (the model may still have emitted usable
    text); a timeout DOES raise :class:`subprocess.TimeoutExpired`. The prompt is
    never passed through a shell."""
    if not argv:
        raise ValueError("argv must be non-empty")
    proc = subprocess.run(
        list(argv) if stdin else [a.replace("{PROMPT}", prompt) for a in argv],
        input=prompt if stdin else None,
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=dict(env) if env is not None else None,
        check=False,
    )
    return (proc.stdout or "") + (proc.stderr or "")


def agent_runner_from_spec(
    spec: Any,
    *,
    cwd: str | Path | None = None,
    timeout_s: float = DEFAULT_AGENT_TIMEOUT_S,
    env: Mapping[str, str] | None = None,
) -> Callable[[str], str]:
    """Build a ``run_agent(prompt) -> text`` callable bound to a peer spec's
    ``argv`` (the shape the LLM adapters inject)."""
    argv = tuple(getattr(spec, "argv", ()) or ())
    if not argv:
        raise ValueError("spec has no argv to invoke")
    use_stdin = getattr(spec, "prompt_mode", "argv-substitute") == "stdin"

    def _run(prompt: str) -> str:
        # Unwrap stream-json so the JSON-parsing adapters (decompose / refute /
        # synthesize / diagnose) see the model's FINAL text, not the event stream.
        return final_agent_text(run_agent_once(
            prompt, argv=argv, cwd=cwd, timeout_s=timeout_s, env=env,
            stdin=use_stdin))

    return _run
