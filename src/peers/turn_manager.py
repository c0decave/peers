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
    # Bug B: default rotation threshold lowered from 2 → 1 in 1.6.x.
    # Old default needed 3 consecutive fails to rotate (consec > 2). v12
    # showed that wastes ~67min when one peer is stuck — the OTHER peer
    # should get a chance after one wasted retry. Operators who want the
    # legacy behavior (let peer finish what it started) can override via
    # state['config']['max_retries'] = 2 or the constructor argument.
    DEFAULT_MAX_RETRIES = 1

    # A degraded peer is benched while a healthy one exists, but after this
    # many iterations since it was degraded it gets ONE recovery turn instead
    # of being starved forever (v17 finding). 0 disables recovery.
    DEFAULT_RECOVERY_INTERVAL = 8

    def __init__(self, state: dict[str, Any],
                 max_retries: int = DEFAULT_MAX_RETRIES,
                 recovery_interval: int = DEFAULT_RECOVERY_INTERVAL) -> None:
        self.state = state
        self.max_retries = max_retries
        self.recovery_interval = recovery_interval

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> "TurnManager":
        """Construct honoring state['config']['max_retries'] override.

        Falls back to DEFAULT_MAX_RETRIES when the config key is missing
        or not a non-negative int. Use this factory in the orchestrator
        so operator-supplied config.yaml settings reach the rotation logic.
        """
        cfg = state.get("config") or {}
        raw = cfg.get("max_retries", cls.DEFAULT_MAX_RETRIES)
        try:
            mr = int(raw)
        except (TypeError, ValueError):
            mr = cls.DEFAULT_MAX_RETRIES
        if mr < 0:
            mr = cls.DEFAULT_MAX_RETRIES
        raw_ri = cfg.get("recovery_interval", cls.DEFAULT_RECOVERY_INTERVAL)
        try:
            ri = int(raw_ri)
        except (TypeError, ValueError):
            ri = cls.DEFAULT_RECOVERY_INTERVAL
        if ri < 0:
            ri = cls.DEFAULT_RECOVERY_INTERVAL
        return cls(state, max_retries=mr, recovery_interval=ri)

    def current(self) -> str:
        """Active peer name.

        if the peer at `turn_index` is `degraded` and AT LEAST
        ONE non-degraded peer exists, hop forward over consecutive
        degraded peers and pin the turn_index there. This prevents the
        loop from burning ticks retrying a known-degraded peer while a
        healthy one waits next in rotation.

        Item 13: also hop OVER `recovery`-role peers as long as at least
        one non-recovery, non-degraded peer exists. Recovery peers are
        spares; they only enter rotation when every default peer is
        degraded. peer_roles lives at state['peer_roles'][peer_name]
        and is populated by the driver from PeerSpec.role at startup.

        If ALL peers are degraded, fall back to the original rotation
        target — the HALT-all-degraded check in the driver will pick it
        up at the next loop iteration.
        """
        order = self.state["peer_order"]
        idx = self.state["turn_index"]
        peers = self.state.get("peers") or {}
        roles = self.state.get("peer_roles") or {}
        iteration = self.state.get("iteration", 0)

        def _recovery_due(name: str) -> bool:
            # A degraded (NOT halted) peer earns a single recovery turn once
            # `recovery_interval` iterations have elapsed since it was degraded
            # (v17 anti-starvation). A failed recovery bumps degraded_at_iter,
            # re-benching it for another interval; a success heals it.
            if self.recovery_interval <= 0:
                return False
            p = peers.get(name, {})
            if p.get("state") != "degraded":
                return False
            # Measure the cooldown from the last failed recovery attempt if one
            # happened (recovery_cooldown_iter), else from first degradation
            # (degraded_at_iter, which stays put for operator visibility).
            since = p.get("recovery_cooldown_iter")
            if not isinstance(since, int) or isinstance(since, bool):
                since = p.get("degraded_at_iter")
            if not isinstance(since, int) or isinstance(since, bool):
                return False
            return (iteration - since) >= self.recovery_interval

        def _eligible(name: str, *, allow_recovery: bool) -> bool:
            st = peers.get(name, {}).get("state")
            if st == "halted":
                return False
            if st == "degraded" and not _recovery_due(name):
                return False
            if roles.get(name) == "recovery" and not allow_recovery:
                return False
            return True

        # Any non-recovery healthy peer? If so, skip recovery peers.
        has_default_healthy = any(
            _eligible(p, allow_recovery=False) for p in order
        )
        # Any healthy peer at all (including recovery)? If not, no skip.
        if not any(_eligible(p, allow_recovery=True) for p in order):
            return order[idx]
        n = len(order)
        for _ in range(n):
            name = order[idx]
            if _eligible(name, allow_recovery=not has_default_healthy):
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

    def advance(self, success: bool, rate_limited: bool = False) -> None:
        peer = self.current()
        peers = self.state["peers"]
        if rate_limited:
            # v17 finding: a transient server rate-limit is not the peer's
            # fault. Hand the turn to the OTHER peer (whose tick is itself the
            # backoff) without charging a consecutive-fail — the rate-limited
            # peer retries when rotation returns to it.
            peers[peer]["consecutive_fails"] = 0
            self._rotate()
            return
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
