"""Tick-Verlauf panel: a scrollable timeline of per-tick ``TickEntry`` rows.

Pure renderer over ``list[TickEntry]`` (Wave-1a ``reader.tick_entries``). Holds no
file I/O. Each row shows the wall-clock ts + a relative offset + the tick's
classification / commit-Δ / tokens / $. Selecting (highlighting) a row emits a
:class:`TicksPanel.TickSelected` message carrying the tick's ``head_after`` sha so
the app can drive the Diff window. A success/fail tick colors green/red; the
synthetic ``exit`` row reads dim.
"""
from __future__ import annotations

from datetime import datetime

from textual.message import Message
from textual.widgets import Label, ListItem, ListView, Static

from peers_ctl.tui.snapshots import TickEntry


def tick_state_class(tick: TickEntry) -> str:
    """`.state-*` accent for a tick row. The exit row is dim; a failed tick reads
    red; a successful tick green; an unknown-success tick is muted/info."""
    if getattr(tick, "is_exit", False):
        return "state-dim"
    success = getattr(tick, "success", None)
    if success is True:
        return "state-pass"
    if success is False:
        return "state-fail"
    return "state-info"


def _short(sha: str | None) -> str:
    return (sha or "")[:8]


def _rel(ts: str, *, now: datetime | None = None) -> str:
    """Best-effort relative offset (``5m ago``) from an ISO ts; '' on parse fail."""
    if not ts:
        return ""
    try:
        when = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return ""
    ref = now or datetime.now(when.tzinfo)
    try:
        delta = (ref - when).total_seconds()
    except (TypeError, ValueError):
        return ""
    if delta < 0:
        return "now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _tick_line(tick: TickEntry, *, now: datetime | None = None) -> str:
    """One compact timeline row."""
    if getattr(tick, "is_exit", False):
        reason = getattr(tick, "exit_reason", None) or "exit"
        return f"■ exit  {reason}  {_rel(tick.ts, now=now)}"
    parts = []
    it = getattr(tick, "iteration", None)
    parts.append(f"it{it}" if it is not None else "it?")
    peer = getattr(tick, "peer", None)
    if peer:
        parts.append(str(peer))
    cls = getattr(tick, "classification", None)
    if cls:
        parts.append(str(cls))
    before, after = _short(getattr(tick, "head_before", None)), _short(getattr(tick, "head_after", None))
    if before and after and before != after:
        parts.append(f"{before}→{after}")
    elif after:
        parts.append(after)
    tok = getattr(tick, "tokens", 0) or 0
    if tok:
        parts.append(f"{tok:,}t")
    usd = getattr(tick, "usd", 0.0) or 0.0
    if usd:
        parts.append(f"${usd:.2f}")
    rel = _rel(tick.ts, now=now)
    if rel:
        parts.append(rel)
    return "  ".join(parts)


class TickRow(ListItem):
    """A ListItem remembering its TickEntry (so selection can read head_after)."""

    def __init__(self, tick: TickEntry, *, now: datetime | None = None) -> None:
        self.tick = tick
        label = Label(_tick_line(tick, now=now))
        label.add_class(tick_state_class(tick))
        super().__init__(label)


class TicksPanel(Static):
    """The Tick-Verlauf cockpit panel."""

    can_focus = True  # so the panel can be focused + popped out as a Window.

    class TickSelected(Message):
        """Posted when a tick row is highlighted; carries the head_after sha."""

        def __init__(self, sha: str | None) -> None:
            self.sha = sha
            super().__init__()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = "Tick-Verlauf"

    def compose(self):
        yield Static("", id="ticks-header", classes="title-row")
        yield ListView(id="ticks-list")
        yield Static("", id="ticks-empty", classes="empty-state")

    def render_ticks(
        self, ticks: list[TickEntry], *, now: datetime | None = None,
    ) -> None:
        """Rebuild the timeline (pure: no I/O). Safe on None/garbage."""
        ticks = [t for t in (ticks or []) if isinstance(t, TickEntry)]
        try:
            header = self.query_one("#ticks-header", Static)
            lv = self.query_one("#ticks-list", ListView)
            empty = self.query_one("#ticks-empty", Static)
        except Exception:
            return
        lv.clear()
        if not ticks:
            lv.display = False
            empty.display = True
            empty.update("no ticks yet — run has not produced runs.jsonl")
            header.update("0 ticks")
            return
        empty.display = False
        lv.display = True
        header.update(f"{len(ticks)} ticks")
        for t in ticks:
            lv.append(TickRow(t, now=now))
        # newest tick last; select it so the Diff defaults to the latest commit.
        lv.index = len(ticks) - 1

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        item = event.item
        if isinstance(item, TickRow):
            self.post_message(self.TickSelected(item.tick.head_after))
