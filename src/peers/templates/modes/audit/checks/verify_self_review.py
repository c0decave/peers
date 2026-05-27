#!/usr/bin/env python3
"""Verify the latest handoff commit carries a well-formed self-review.

Exit 0 if OK, 1 otherwise (with a diagnostic on stderr that the
substrate will forward into the next prompt).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys


_TRAILER_RE = re.compile(r"^([A-Za-z][A-Za-z0-9-]{1,}):\s*(.*?)\s*$")
_URL_SCHEME_KEYS = {"http", "https", "ftp", "ftps", "ssh", "file", "ws", "wss"}


def _parse_trailers(message: str) -> dict[str, str]:
    trailers: dict[str, str] = {}
    lines = [line.rstrip("\r") for line in message.rstrip().splitlines()]
    for line in reversed(lines):
        if line.strip() == "":
            break
        m = _TRAILER_RE.match(line)
        if not m:
            break
        key, value = m.group(1), m.group(2)
        if key.lower() in _URL_SCHEME_KEYS or value.startswith("//"):
            break
        if key not in trailers:
            trailers[key] = value
    return trailers


def _git(*args: str) -> str:
    r = subprocess.run(["git", *args], check=True,
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.stdout


def _latest_handoff() -> str | None:
    try:
        # Bound history scan; NUL-safe field separator handles any byte
        # value in commit bodies.
        log = _git("log", "-z", "-n", "1000", "--format=%H%x00%B")
    except subprocess.CalledProcessError:
        return None
    parts = log.split("\x00")
    if parts and parts[-1] == "":
        parts.pop()
    i = 0
    while i + 1 < len(parts):
        sha = parts[i].lstrip("\n")
        body = parts[i + 1]
        i += 2
        t = _parse_trailers(body)
        if t.get("Peer-Status") == "handoff":
            return sha
    return None


def main() -> int:
    p = argparse.ArgumentParser(
        description=(
            "Verify the most recent commit carrying "
            "`Peer-Status: handoff` also carries `Self-Review: pass` "
            "and a `## Self-Review` body section."
        )
    )
    p.parse_args()

    sha = _latest_handoff()
    if sha is None:
        print("no handoff commit found in history", file=sys.stderr)
        return 1
    try:
        body = _git("show", "-s", "--format=%B", sha)
    except subprocess.CalledProcessError as e:
        print(f"git show failed: {e}", file=sys.stderr)
        return 1

    trailers = _parse_trailers(body)
    if trailers.get("Self-Review") != "pass":
        print(
            f"handoff {sha[:8]} is missing `Self-Review: pass` trailer",
            file=sys.stderr,
        )
        return 1
    if not re.search(r"(?m)^##\s+Self-Review\b", body):
        print(
            f"handoff {sha[:8]} body has no `## Self-Review` section",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
