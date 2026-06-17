"""Peers panel: state-colored per-peer health for the active run.

Pure renderer over ``list[PeerView]`` (Wave-1a) + the ``current_peer`` name.
Holds no file I/O and never crashes on missing/None data. Each peer row gets a
``.state-*`` accent class; the current (whose turn it is) peer is marked cyan.
"""
from __future__ import annotations

from textual.containers import VerticalScroll
from textual.widgets import Label, Static

from peers_ctl.tui.snapshots import PEER_STATES, PeerView

#: peer health state -> `.state-*` accent class. Unknown strings -> unknown.
_PEER_STATE_CLASS = {
    "healthy": "state-healthy",
    "degraded": "state-degraded",
    "halted": "state-halted",
    "unavailable": "state-unavailable",
}


def peer_state_class(peer: PeerView, current_peer: str | None = None) -> str:
    """Accent class for a peer row. The current peer (its turn) reads cyan;
    otherwise color by health state."""
    if current_peer is not None and str(getattr(peer, "name", "")) == str(current_peer):
        return "state-current"
    st = str(getattr(peer, "state", "unavailable"))
    return _PEER_STATE_CLASS.get(st, "state-unknown")


def _recent_glyphs(recent_runs: list) -> str:
    """Render recent runs as glyphs: ✓ success, ✗ fail, ~ productive-no-handoff."""
    out = []
    for r in (recent_runs or [])[-8:]:
        if r is True or r == 1:
            out.append("✓")
        elif r is False or r == 0:
            out.append("✗")
        elif r == 0.5:
            out.append("~")
        else:
            out.append("·")
    return "".join(out)


def _peer_line(peer: PeerView, current_peer: str | None) -> str:
    """One compact peer row."""
    name = str(getattr(peer, "name", "?") or "?")
    state = str(getattr(peer, "state", "unavailable") or "unavailable")
    if state not in PEER_STATES:
        state = f"{state}?"
    parts = []
    if current_peer is not None and name == str(current_peer):
        parts.append("▶")
    parts.append(name)
    parts.append(state)
    fails = getattr(peer, "consecutive_fails", 0.0) or 0.0
    if fails:
        parts.append(f"f{fails:g}")
    glyphs = _recent_glyphs(getattr(peer, "recent_runs", []))
    if glyphs:
        parts.append(glyphs)
    return "  ".join(parts)


class PeersPanel(Static):
    """The Peers cockpit panel."""

    can_focus = True  # so the panel can be focused + popped out as a Window.

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = "Peers"

    def compose(self):
        yield Static("", id="peers-header", classes="title-row")
        yield VerticalScroll(id="peers-body")

    def render_peers(
        self,
        peers: list[PeerView],
        current_peer: str | None = None,
    ) -> None:
        """Rebuild the peer rows (pure: no I/O). Safe on None/garbage."""
        peers = [p for p in (peers or []) if isinstance(p, PeerView)]
        try:
            header = self.query_one("#peers-header", Static)
            body = self.query_one("#peers-body", VerticalScroll)
        except Exception:
            return
        healthy = sum(1 for p in peers if str(getattr(p, "state", "")) == "healthy")
        head = f"{healthy}/{len(peers)} healthy"
        if current_peer:
            head += f"  ·  turn: {current_peer}"
        header.update(head)
        body.remove_children()
        if not peers:
            ph = Label("no peers in state — run not started", classes="empty-state")
            body.mount(ph)
            return
        for p in peers:
            row = Label(_peer_line(p, current_peer))
            row.add_class("peer-row")
            row.add_class(peer_state_class(p, current_peer))
            body.mount(row)
