"""Decode claude session jsonl events for operator-facing peeks."""
from __future__ import annotations

import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Iterator


NOISE_TYPES = {"queue-operation", "ai-title", "last-prompt", "attachment"}
MAX_RENDERED_VALUE = 100


def _ts(ev: dict) -> str:
    ts = str(ev.get("timestamp", ""))
    return ts[11:19] if len(ts) >= 19 else "?"


def _trunc(value: object, limit: int = MAX_RENDERED_VALUE) -> str:
    text = str(value).replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _event_type(ev: dict) -> str:
    t = ev.get("type")
    return t if isinstance(t, str) and t else "?"


def decode_event(ev: dict) -> Iterator[str]:
    """Yield one-line summaries for the operator-relevant event parts."""
    t = _event_type(ev)
    if t in NOISE_TYPES:
        return
    msg = ev.get("message")
    if not isinstance(msg, dict):
        return
    content = msg.get("content")
    ts = _ts(ev)
    if isinstance(content, str):
        yield f"{ts} {t:9s} TEXT: {_trunc(content)}"
        return
    if not isinstance(content, list):
        return
    for item in content:
        if not isinstance(item, dict):
            continue
        ct = item.get("type")
        if ct == "tool_use":
            name = item.get("name", "?")
            yield f"{ts} {t:9s} TOOL: {name}({_trunc(item.get('input', {}), 80)})"
        elif ct == "text":
            yield f"{ts} {t:9s} TEXT: {_trunc(item.get('text', ''))}"
        elif ct == "tool_result":
            err = bool(item.get("is_error", False))
            yield f"{ts} {t:9s} RES:  err={err} {_trunc(item.get('content', ''), 80)}"


def newest_session_jsonl(jsonl_dir: Path) -> Path | None:
    """Return the newest ``*.jsonl`` session file in a claude project dir."""
    if not jsonl_dir.is_dir():
        return None
    try:
        candidates = list(jsonl_dir.glob("*.jsonl"))
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _read_existing_lines(
    path: Path,
    last: int | None,
) -> tuple[list[str], int, tuple[int, int] | None]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            stat = os.fstat(f.fileno())
            if last is None:
                lines = list(f)
            else:
                lines = list(deque(f, maxlen=last))
            return lines, f.tell(), (stat.st_dev, stat.st_ino)
    except FileNotFoundError:
        return [], 0, None


def tail_session(
    jsonl_path: Path,
    *,
    follow: bool = True,
    last: int | None = None,
) -> Iterator[str]:
    """Yield decoded lines from a session jsonl, optionally following it."""
    lines, pos, identity = _read_existing_lines(jsonl_path, last)
    for line in lines:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            yield from decode_event(ev)
    if not follow:
        return
    while True:
        try:
            with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
                stat = os.fstat(f.fileno())
                current_identity = (stat.st_dev, stat.st_ino)
                if identity is not None and current_identity != identity:
                    pos = 0
                elif stat.st_size < pos:
                    pos = 0
                identity = current_identity
                f.seek(pos)
                while True:
                    line = f.readline()
                    if not line:
                        break
                    pos = f.tell()
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(ev, dict):
                        yield from decode_event(ev)
        except FileNotFoundError:
            pos = 0
            identity = None
        time.sleep(0.5)
