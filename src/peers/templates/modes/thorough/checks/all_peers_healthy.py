#!/usr/bin/env python3
"""Exit 0 if every peer in state.peers is `healthy` or `degraded`.
Exit 1 if any peer is `unavailable` (a halt_patterns match
landed and the substrate halted with `peer-unavailable:<peer>`) or
if `state.exit_events` carries a peer-unavailable record.

The gate is intentionally STRICTER than the legacy halt-all-degraded
check: a single AUTH/QUOTA shape is enough. Operator action is the
only way to recover — a re-login or a top-up — and that's exactly the
case where silently degrading wastes the next N hours of budget on a
peer that cannot recover by itself.

`unavailable_reason` / `unavailable_at_iter` / `unavailable_snippet`
written by the orchestrator surface in the diagnostic line so the
operator does NOT have to grep runs.jsonl to find the offending
pattern. Fails CLOSED on unreadable input."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from peers.safe_io import read_text_no_symlink


def main(root: str = ".") -> int:
    state_path = Path(root) / ".peers" / "state.json"
    if not state_path.is_file():
        # No ticks yet → trivially healthy. Same convention as
        # convergence_reached.
        print("all_peers_healthy: no state.json yet (no ticks ran)")
        return 0
    try:
        # BUG-102/103: read via safe_io — refuse a symlinked state.json
        # (CWE-59) and decode with replacement so non-UTF-8 bytes fail the
        # gate via JSONDecodeError instead of an uncaught UnicodeDecodeError.
        state = json.loads(read_text_no_symlink(state_path))
    except (OSError, json.JSONDecodeError) as e:
        print(f"all_peers_healthy FAIL: state.json unreadable: {e}")
        return 1
    peers = state.get("peers") or {}
    if not isinstance(peers, dict):
        print(
            "all_peers_healthy FAIL: state.peers is not a mapping "
            f"(got {type(peers).__name__})"
        )
        return 1
    unavailable: list[str] = []
    for name, info in peers.items():
        if not isinstance(info, dict):
            continue
        if info.get("state") == "unavailable":
            reason = info.get("unavailable_reason", "no reason recorded")
            at_iter = info.get("unavailable_at_iter", "?")
            snippet = info.get("unavailable_snippet", "")
            line = f"{name} @iter={at_iter}: {reason}"
            if snippet:
                line += f" snippet={snippet[:120]!r}"
            unavailable.append(line)
    # Also surface exit_events with `peer-unavailable:` so a halted
    # run shows the gate as red on the next `peers verify`.
    for ev in state.get("exit_events") or []:
        if not isinstance(ev, dict):
            continue
        reason = ev.get("reason", "")
        if isinstance(reason, str) and reason.startswith("peer-unavailable:"):
            unavailable.append(f"exit_event: {reason}")
    if not unavailable:
        print(f"all_peers_healthy: {len(peers)} peer(s) ok")
        return 0
    print("all_peers_healthy FAIL: " + "; ".join(unavailable))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
