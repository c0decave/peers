"""Decode claude session jsonl events for operator-facing peeks."""
from __future__ import annotations

import json
import os
import stat as _stat
import time
from collections import deque
from pathlib import Path
from typing import IO, Iterator


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
    newest: tuple[float, Path] | None = None
    for path in candidates:
        try:
            st = path.lstat()
        except OSError:
            continue
        if not _stat.S_ISREG(st.st_mode):
            continue
        if newest is None or st.st_mtime > newest[0]:
            newest = (st.st_mtime, path)
    return newest[1] if newest is not None else None


def _is_regular_session_jsonl(path: Path) -> bool:
    """True iff ``path`` is a regular file entry, without following symlinks."""
    try:
        st = path.lstat()
    except OSError:
        return False
    return _stat.S_ISREG(st.st_mode)


def _open_session_jsonl(path: Path) -> IO[str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode):
            raise OSError(f"refusing non-regular session jsonl: {path}")
        return os.fdopen(fd, "r", encoding="utf-8", errors="replace")
    except Exception:
        os.close(fd)
        raise


def _read_existing_lines(
    path: Path,
    last: int | None,
) -> tuple[list[str], int, tuple[int, int] | None]:
    try:
        with _open_session_jsonl(path) as f:
            stat = os.fstat(f.fileno())
            if last is None:
                lines = list(f)
            else:
                lines = list(deque(f, maxlen=last))
            return lines, f.tell(), (stat.st_dev, stat.st_ino)
    except OSError:
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
            with _open_session_jsonl(jsonl_path) as f:
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
        except OSError:
            pos = 0
            identity = None
        time.sleep(0.5)
