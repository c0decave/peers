"""Round-robin turn rotation across n peers (n >= 2).

State contract (schema v2):
- state["peer_order"]: list[str], at least 2 names, no duplicates.
- state["turn_index"]: int, 0 <= turn_index < len(peer_order).
- state["peers"][name]["consecutive_fails"]: int.

The active peer is `peer_order[turn_index]`. `advance(success=True)`
rotates +1 modulo len. `advance(success=False)` keeps the same peer
until `consecutive_fails > max_retries`, then rotates and resets the
counter (so the next time the peer comes back around, it gets a fresh
retry budget).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def _handoff_msg_path(project_root: Path) -> Path:
    return Path(project_root) / ".peers" / "handoff-msg.txt"


def write_handoff_msg(project_root: Path, text: str) -> Path:
    """Write substrate handoff scratch under ``.peers/``.

    Older experiments wrote a root-level ``.handoff-msg.txt`` which then
    showed up as untracked work. This helper keeps the scratch file in
    the control directory where it belongs.
    """
    path = _handoff_msg_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def sweep_legacy_handoff_msg(project_root: Path) -> None:
    """Move/remove legacy root-level handoff scratch files best-effort."""
    root = Path(project_root)
    target = _handoff_msg_path(root)
    for name in (".handoff-msg.txt", "handoff-msg.txt"):
        legacy = root / name
        try:
            if not legacy.exists() or legacy.is_symlink():
                continue
            text = legacy.read_text(encoding="utf-8", errors="replace")
            if not target.exists():
                write_handoff_msg(root, text)
            legacy.unlink()
        except OSError:
            continue


class TurnManager:
    def __init__(self, state: dict[str, Any], max_retries: int = 2) -> None:
        self.state = state
        self.max_retries = max_retries

    def current(self) -> str:
        """Active peer name.

        if the peer at `turn_index` is `degraded` and AT LEAST
        ONE non-degraded peer exists, hop forward over consecutive
        degraded peers and pin the turn_index there. This prevents the
        loop from burning ticks retrying a known-degraded peer while a
        healthy one waits next in rotation.

        If ALL peers are degraded, fall back to the original rotation
        target — the HALT-all-degraded check in the driver will pick it
        up at the next loop iteration.
        """
        order = self.state["peer_order"]
        idx = self.state["turn_index"]
        peers = self.state.get("peers") or {}
        # Any healthy peer at all? If not, no point skipping.
        if not any(
            peers.get(p, {}).get("state") not in ("degraded", "halted")
            for p in order
        ):
            return order[idx]
        n = len(order)
        for _ in range(n):
            name = order[idx]
            if peers.get(name, {}).get("state") not in ("degraded", "halted"):
                self.state["turn_index"] = idx
                return name
            idx = (idx + 1) % n
        return order[self.state["turn_index"]]

    def other(self) -> str:
        """Convenience for the n=2 case: the OTHER peer's name.

        For n>2 the substrate uses `others()` instead; this is kept for
        backward compat with n=2 callers.
        """
        order = self.state["peer_order"]
        if len(order) != 2:
            raise ValueError(
                "TurnManager.other() is only valid for n=2; use others() "
                "for n>2"
            )
        return order[(self.state["turn_index"] + 1) % 2]

    def others(self) -> list[str]:
        cur_idx = self.state["turn_index"]
        return [n for i, n in enumerate(self.state["peer_order"])
                if i != cur_idx]

    def _rotate(self) -> None:
        n = len(self.state["peer_order"])
        self.state["turn_index"] = (self.state["turn_index"] + 1) % n

    def advance(self, success: bool) -> None:
        peer = self.current()
        peers = self.state["peers"]
        if success:
            peers[peer]["consecutive_fails"] = 0
            self._rotate()
            return
        peers[peer]["consecutive_fails"] += 1
        if peers[peer]["consecutive_fails"] > self.max_retries:
            # Reset BEFORE rotating so the peer gets a fresh retry
            # budget the next time control returns to it.
            peers[peer]["consecutive_fails"] = 0
            self._rotate()
