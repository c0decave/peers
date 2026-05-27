#!/usr/bin/env python3
"""Peer that commits cleanly but with Self-Review: needs-review
(instead of the required `pass`). Exercises the trailer-validation
branch in OrchestratorDriver._post_run."""
from __future__ import annotations

import os
import subprocess
import sys
import uuid


def git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


def main() -> int:
    sys.stdin.read()
    peer = os.environ.get("FAKE_PEER_NAME", "claude")
    git("config", "user.email", "fake@peer")
    git("config", "user.name", peer)

    with open("widget.py", "a") as f:
        f.write(f"work-{uuid.uuid4().hex[:8]}\n")
    git("add", "widget.py")
    body = (
        "Fake peer turn (wrong trailer)\n\n"
        "## Self-Review\n"
        "I am unsure; flagging for review.\n\n"
        "Self-Review: needs-review\n"
        "Peer-Status: handoff\n"
        f"Peer: {peer}\n"
    )
    git("commit", "-q", "-m", body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
