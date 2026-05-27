#!/usr/bin/env python3
"""A configurable scripted peer for observability tests.

Environment variables (all optional):
- FAKE_PEER_NAME: peer name to claim in the handoff commit (default "claude")
- FAKE_PEER_STDOUT: string to write to stdout before exiting
- FAKE_PEER_STDERR: string to write to stderr before exiting
- FAKE_PEER_NO_COMMIT: if set, skip the handoff commit (drives a fail-tick)
- FAKE_PEER_EXIT_CODE: integer exit code (default 0)
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

    stdout_text = os.environ.get("FAKE_PEER_STDOUT", "")
    stderr_text = os.environ.get("FAKE_PEER_STDERR", "")
    if stdout_text:
        sys.stdout.write(stdout_text)
        sys.stdout.flush()
    if stderr_text:
        sys.stderr.write(stderr_text)
        sys.stderr.flush()

    if not os.environ.get("FAKE_PEER_NO_COMMIT"):
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

    return int(os.environ.get("FAKE_PEER_EXIT_CODE", "0"))


if __name__ == "__main__":
    sys.exit(main())
