"""Bugs panel: a table of ``BugView`` rows with the blocking-open count highlighted.

Pure renderer over ``list[BugView]`` (Wave-1a ``reader.bug_views``) +
``reader.blocking_open``. Holds no file I/O. An OPEN bug at a blocking severity
(crit/high/med) reads red; a resolved/closed bug reads dim; other open bugs read
yellow. The header highlights the blocking-open count in red when nonzero.
"""
from __future__ import annotations

from textual.containers import VerticalScroll
from textual.widgets import Label, Static

from peers_ctl.tui.snapshots import BugView

_BLOCKING_SEVERITIES = frozenset({"crit", "high", "med"})


def bug_state_class(bug: BugView) -> str:
    """`.state-*` accent for a bug row. A resolved/closed bug is dim; an OPEN
    blocking-severity bug is red; any other open bug is yellow."""
    status = str(getattr(bug, "status", "")).lower()
    if status in ("resolved", "closed", "fixed"):
        return "state-dim"
    sev = str(getattr(bug, "severity", "")).lower()
    if status == "open" and sev in _BLOCKING_SEVERITIES:
        return "state-fail"
    return "state-pending"


def _bug_line(bug: BugView) -> str:
    bid = str(getattr(bug, "id", "?") or "?")
    sev = str(getattr(bug, "severity", "?") or "?")
    status = str(getattr(bug, "status", "?") or "?")
    title = str(getattr(bug, "title", "") or "")
    parts = [bid, f"[{sev}]", status]
    if title:
        parts.append(title)
    return "  ".join(parts)


class BugsPanel(Static):
    """The Bugs cockpit panel."""

    can_focus = True  # so the panel can be focused + popped out as a Window.

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = "Bugs"

    def compose(self):
        yield Static("", id="bugs-header", classes="title-row")
        yield VerticalScroll(id="bugs-body")

    def render_bugs(self, bugs: list[BugView], blocking: int = 0) -> None:
        """Rebuild the bug rows (pure: no I/O). Safe on None/garbage."""
        bugs = [b for b in (bugs or []) if isinstance(b, BugView)]
        try:
            header = self.query_one("#bugs-header", Static)
            body = self.query_one("#bugs-body", VerticalScroll)
        except Exception:
            return
        header.remove_class("state-alert")
        head = f"{len(bugs)} bugs"
        if blocking:
            head += f"  ·  {blocking} blocking"
            header.add_class("state-alert")
        header.update(head)
        body.remove_children()
        if not bugs:
            body.mount(Label("no bugs filed", classes="empty-state"))
            return
        for b in bugs:
            row = Label(_bug_line(b))
            row.add_class("bug-row")
            row.add_class(bug_state_class(b))
            body.mount(row)
