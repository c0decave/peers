#!/usr/bin/env python3
"""Peer that edits files but never commits, then exits 0.
Reproduces the dogfood-known-issue: uncommitted work in working tree."""
from __future__ import annotations

import os
import sys
import uuid


def main() -> int:
    sys.stdin.read()
    peer = os.environ.get("FAKE_PEER_NAME", "claude")
    with open("widget.py", "a") as f:
        f.write(f"# leaked-by-{peer}-{uuid.uuid4().hex[:8]}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
