#!/usr/bin/env python3
"""A scripted peer: reads prompt from stdin, runs a canned script of
git operations against the cwd, exits 0. Used to drive the orchestrator
deterministically in tests.

Behaviour:
- Each invocation appends a unique marker to widget.py so the diff is
  non-empty, then creates a handoff commit with a proper self-review.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid


def git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


def main() -> int:
    sys.stdin.read()  # consume prompt
    peer = os.environ.get("FAKE_PEER_NAME", "claude")
    git("config", "user.email", "fake@peer")
    git("config", "user.name", peer)

    marker = f"work-{uuid.uuid4().hex[:8]}\n"
    with open("widget.py", "a") as f:
        f.write(marker)
    git("add", "widget.py")

    body = (
        "Fake peer turn\n\n"
        "## Self-Review\n"
        "Re-read the diff; nothing concerning.\n\n"
        "Self-Review: pass\n"
        "Peer-Status: handoff\n"
        f"Peer: {peer}\n"
    )
    git("commit", "-q", "-m", body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
