"""Structured-error halt classification (Option C — v15 internal testing follow-up).

The free-text ``health.halt_patterns`` scan the peer's whole output stream, so
a peer echoing repo content that *describes* an error (a ``git log`` subject, a
bug-report body) can trip a halt on its own echo — the self-referential
livelock the v15 internal testing hit. The structural fix is to derive the halt from
the CLI's OWN structured status channel, which a quoted echo cannot forge.

For ``claude --output-format stream-json`` that channel is the terminal
``{"type": "result", "is_error": ..., "subtype": ...}`` envelope. We classify a
halt ONLY from that envelope, gated on ``is_error``: a git-log echo is an
``assistant``/``text`` event and never sets the result envelope's ``is_error``,
so structured classification is immune to the echo class by construction.

``codex exec --json`` (``error``/``turn.failed`` events) and ``opencode
--format json`` (top-level ``error`` events) expose the same kind of structured
status channel and are handled the same way. A peer run without a JSON/event
format returns ``None`` here and stays on the (echo-guarded) regex halt path in
``health_guard``. This module is purely additive: it only ever ASSERTS a halt
the regex path might miss, and only when the CLI itself reports an
unrecoverable auth/quota/usage-limit error.
"""
from __future__ import annotations

import json
import re

# Halt classes the operator MUST act on (re-login / top-up). Matched ONLY
# against the structured result envelope's own subtype/message fields — never
# the free transcript — so an echoed line carrying these words cannot trigger
# a halt. Case-insensitive here is safe precisely because the is_error gate +
# envelope scoping already exclude incidental prose.
_HALT_VOCAB = re.compile(
    r"(quota[ _-]?exhausted"
    r"|hit your usage limit"
    r"|usage[ _-]?limit[ _-]?(?:reached|exceeded|exhausted)"
    r"|authentication[ _-]?(?:failed|error)"
    r"|(?:invalid|missing|expired)[ _-]?api[ _-]?key"
    r"|not[ _-]?(?:logged|signed)[ _-]?in"
    r"|(?:please\s+)?run\s+/login)",
    re.IGNORECASE,
)


def _claude_result_envelope(stdout: str) -> dict | None:
    """Return claude's terminal stream-json ``result`` envelope, or ``None``.

    Scans for the LAST ``{"type": "result", ...}`` object (stream-json emits
    one terminal result event). Only the result envelope is considered —
    ``assistant``/``text`` events, where echoed repo content lives, are
    ignored. Also accepts a single ``result`` object for
    ``--output-format json`` (non-stream).
    """
    found: dict | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        # RecursionError (deeply-nested JSON) is a RuntimeError, NOT a
        # ValueError/JSONDecodeError — catch it too so pathological peer
        # output degrades to "no envelope" instead of crashing the tick.
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            found = obj
    if found is None:
        try:
            obj = json.loads(stdout.strip())
        except (ValueError, RecursionError):
            obj = None
        if isinstance(obj, dict) and obj.get("type") == "result":
            found = obj
    return found


def _codex_error_blobs(stdout: str) -> list[str]:
    """Error-message strings from codex --json ``error`` / ``turn.failed``
    events (verified against codex-cli 0.133: a failing turn emits
    ``{"type":"error","message":...}`` and ``{"type":"turn.failed","error":
    {"message":...}}``). ONLY those structured error events are read — the
    agent's own text lives in ``item.completed``/``agent_message`` events, so
    an echoed error string in the transcript is ignored (echo-immune).
    """
    blobs: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(obj, dict):
            continue
        etype = obj.get("type")
        if etype == "error" and isinstance(obj.get("message"), str):
            blobs.append(obj["message"])
        elif etype == "turn.failed":
            err = obj.get("error")
            if isinstance(err, dict) and isinstance(err.get("message"), str):
                blobs.append(err["message"])
    return blobs


def _opencode_error_blobs(stdout: str) -> list[str]:
    """Error strings from opencode --format json ``error`` events (verified
    against opencode 1.15.13: ``{"type":"error","error":{"name":…,"data":
    {"message":…}}}``). ONLY top-level ``error`` events are read — the agent's
    own text lives in ``text``/``tool`` part events, so an echoed error line
    in the transcript is ignored (echo-immune)."""
    blobs: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(obj, dict) or obj.get("type") != "error":
            continue
        err = obj.get("error")
        if not isinstance(err, dict):
            continue
        parts: list[str] = []
        if isinstance(err.get("name"), str):
            parts.append(err["name"])
        data = err.get("data")
        if isinstance(data, dict) and isinstance(data.get("message"), str):
            parts.append(data["message"])
        elif isinstance(data, str):
            parts.append(data)
        if parts:
            blobs.append(" ".join(parts))
    return blobs


def _verdict(tool: str, blob: str) -> tuple[str, str] | None:
    m = _HALT_VOCAB.search(blob)
    if m is None:
        return None
    klass = re.sub(r"[ _-]+", "-", m.group(1).strip().lower())
    return (f"structured:{tool}:{klass}", blob[:200])


# Transient, RETRYABLE server-side errors — a temporary 429/5xx/overloaded that
# the server itself flags as "not your usage limit". Distinct from _HALT_VOCAB
# (which is the operator-must-act class). A transient error must NOT halt the
# run and must NOT count as a hard process-fail against peer health; the loop
# backs off and retries the same peer. (v17 internal testing operator finding: a
# transient 429 was misclassified process-fail -> degraded the peer -> the
# turn manager then benched it for the rest of the run.)
_TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504, 529})
# Standard HTTP reason-phrases for the transient/retryable classes. Deliberately
# NO bare status numbers (I1): a number like "503" can appear incidentally in an
# unrelated structured error ("exited with code 503") and a false positive would
# mask a real failure as a harmless retry. claude carries the clean numeric
# `api_error_status` field for the numeric path; codex/opencode rate-limit
# messages carry these phrases (e.g. "429 Too Many Requests", "Overloaded").
_TRANSIENT_VOCAB = re.compile(
    r"(rate[ _-]?limit"
    r"|overloaded"
    r"|temporarily (?:limiting|unavailable)"
    r"|too many requests"
    r"|service unavailable"
    r"|bad gateway"
    r"|gateway time-?out"
    r"|try again (?:later|in|soon))",
    re.IGNORECASE,
)


def _transient_verdict(
    tool: str, blob: str, status: int | None = None,
) -> tuple[str, str] | None:
    # An unrecoverable auth/quota/usage-limit error is a HALT, never transient —
    # let _verdict/classify_structured_halt own it.
    if _HALT_VOCAB.search(blob):
        return None
    if status in _TRANSIENT_STATUS or _TRANSIENT_VOCAB.search(blob):
        return (f"transient:{tool}:rate-limited", blob[:200])
    return None


def classify_structured_halt(
    tool: str,
    stdout: str,
    stderr: str,
    exit_code: int | None = None,
) -> tuple[str, str] | None:
    """Return ``(reason_label, snippet)`` if ``tool``'s STRUCTURED output
    reports an unrecoverable auth/quota/usage-limit halt, else ``None``.

    Both ``claude`` (stream-json ``result`` envelope) and ``codex --json``
    (``error``/``turn.failed`` events) expose a structured status channel
    here; the halt is read ONLY from those status events, never the assistant
    transcript, so an echoed error line cannot forge it. Other tools return
    ``None`` and keep the regex halt path. A generic structured error (a
    transient execution / bad-request failure) is NOT a halt — it is
    retryable; only the auth/quota/usage-limit vocabulary halts the run.
    """
    if tool == "claude":
        env = _claude_result_envelope(stdout)
        if env is None or env.get("is_error") is not True:
            return None
        parts = [
            str(env[key]) for key in ("subtype", "result", "error", "message")
            if isinstance(env.get(key), str)
        ]
        return _verdict("claude", " ".join(parts))
    if tool == "codex":
        for blob in _codex_error_blobs(stdout):
            verdict = _verdict("codex", blob)
            if verdict is not None:
                return verdict
        return None
    if tool == "opencode":
        for blob in _opencode_error_blobs(stdout):
            verdict = _verdict("opencode", blob)
            if verdict is not None:
                return verdict
        return None
    return None


def classify_structured_transient(
    tool: str,
    stdout: str,
    stderr: str,
    exit_code: int | None = None,
) -> tuple[str, str] | None:
    """Return ``(reason_label, snippet)`` if ``tool``'s STRUCTURED output
    reports a TRANSIENT, retryable server error (HTTP 429/5xx, overloaded,
    "temporarily limiting · not your usage limit"), else ``None``.

    Read ONLY from the same structured status channel as
    ``classify_structured_halt`` (claude's ``result`` envelope, codex/opencode
    ``error`` events), so an echoed error line in the transcript cannot forge
    it. An unrecoverable auth/quota/usage-limit error is NOT transient — it is a
    halt and is owned by ``classify_structured_halt``; this function defers to
    it via ``_transient_verdict``'s halt-vocab precedence check.
    """
    if tool == "claude":
        env = _claude_result_envelope(stdout)
        if env is None or env.get("is_error") is not True:
            return None
        status = env.get("api_error_status")
        if not isinstance(status, int) or isinstance(status, bool):
            status = None
        parts = [
            str(env[key]) for key in ("subtype", "result", "error", "message")
            if isinstance(env.get(key), str)
        ]
        return _transient_verdict("claude", " ".join(parts), status)
    if tool == "codex":
        for blob in _codex_error_blobs(stdout):
            verdict = _transient_verdict("codex", blob)
            if verdict is not None:
                return verdict
        return None
    if tool == "opencode":
        for blob in _opencode_error_blobs(stdout):
            verdict = _transient_verdict("opencode", blob)
            if verdict is not None:
                return verdict
        return None
    return None
