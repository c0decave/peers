#!/usr/bin/env python3
"""Peer that creates a proper handoff commit, then a junk follow-up
commit with no Peer-Status / Self-Review trailers but still tagged
`Peer:`. After Phase-2 fix H9 the driver accepts this — handoff
trailers may appear on ANY commit in the turn, not just the last."""
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

    tag = uuid.uuid4().hex[:8]
    with open("widget.py", "a") as f:
        f.write(f"work-{tag}\n")
    git("add", "widget.py")
    git("commit", "-q", "-m", (
        f"Real handoff {tag}\n\n## Self-Review\nlooks fine.\n\n"
        f"Self-Review: pass\nPeer-Status: handoff\nPeer: {peer}\n"
    ))

    with open("widget.py", "a") as f:
        f.write(f"# nit fix {tag}\n")
    git("add", "widget.py")
    git("commit", "-q", "-m", f"nit: tidy up\n\nPeer: {peer}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
