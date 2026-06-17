"""Live-Stream panel: a scrolling, colored view of the active peer's activity.

This widget is a **pure renderer** — it owns NO subprocess and reads NO files.
The app (``PeersTuiApp``) owns the killable streaming subprocess
(``tui_actions.stream_verb``), decodes each raw line with
``tui_actions.decode_stream_line``, and pushes the resulting ``(kind, text)``
rows in via :meth:`LivePanel.append_rows`. Keeping the process lifecycle in the
app (start on show / run-select, STOP-kill on close / run-switch) means this
panel stays trivially testable and never leaks a subprocess.

Source selection (decided + documented, surfaced in the header):
  * a ``claude`` peer is **genuinely live** — the app streams ``peers-ctl peek
    <name>`` (which follows the claude session jsonl through the canonical
    ``peers.peek`` decoder), so each tool-call / text / tool-result appears in
    real time;
  * a ``codex``/``opencode`` peer has **no unified live stream pre-Wave-2**, so
    the app tails the newest completed ``.peers/log/peers/tick-*-<peer>.stdout.log``
    — i.e. **tick-level**, not keystroke-live. The header renders an honest
    ``tick-level (live stream lands in Wave 2)`` hint for those peers so the
    operator is never misled about liveness.

Coloring (per the design tokens): ``text`` normal, ``tool`` cyan
(``.state-tool``), ``result``/``error`` red (``.state-fail``), ``raw`` muted
(``.muted``). Auto-scrolls to the newest line UNLESS the operator has scrolled
up (pause-on-scroll-up); a fresh run / no-peer renders an empty state.
"""
from __future__ import annotations

from textual.containers import VerticalScroll
from textual.widgets import Label, Static

#: cap the number of retained rows so a long run can't grow the DOM unbounded.
_MAX_ROWS = 1000

#: map a decoded-row ``kind`` to its ``.state-*`` / muted CSS class.
_KIND_CLASS = {
    "text": "live-text",
    "tool": "state-tool",
    "result": "state-fail",
    "error": "state-fail",
    "raw": "muted",
}


def kind_class(kind: str) -> str:
    """The CSS class for a decoded-row ``kind`` (unknown -> muted)."""
    return _KIND_CLASS.get(kind, "muted")


def idle_label(idle_s: float | None) -> str:
    """Render the header idle-timer: ``working`` when fresh, ``idle Ns`` when stale.

    ``idle_s`` is the seconds since the last streamed line (``None`` before any
    line arrives). A small threshold keeps a steadily-streaming peer reading as
    ``working`` rather than flickering to ``idle``."""
    if idle_s is None:
        return "working"
    if idle_s < 3.0:
        return "working"
    return f"idle {int(idle_s)}s"


class LivePanel(Static):
    """The Live-Stream cockpit panel (pure renderer; app drives the stream)."""

    can_focus = True  # so the panel can be focused, popped out, and scrolled.

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = "Live-Stream"
        #: True while we should auto-scroll to the newest line. Flips off when the
        #: operator scrolls up (pause-on-scroll-up) and back on at the bottom.
        self._follow = True
        #: count of appended rows (so we can trim to _MAX_ROWS).
        self._row_count = 0

    def compose(self):
        yield Static("", id="live-header", classes="title-row")
        yield Static("", id="live-hint", classes="muted")
        yield VerticalScroll(id="live-body")
        yield Static("", id="live-empty", classes="empty-state")

    # ------------------------------------------------------------------ #
    # header                                                             #
    # ------------------------------------------------------------------ #
    def render_header(
        self,
        *,
        peer: str | None,
        tick: int | None,
        runtime_s: int | None,
        idle_s: float | None,
        tool: str,
        live: bool,
    ) -> None:
        """Update the header (peer · tick · runtime · idle-timer) + liveness hint.

        ``live`` is True only for a genuinely-live (claude) source; otherwise the
        hint announces the tick-level fallback honestly. Safe on None fields."""
        try:
            header = self.query_one("#live-header", Static)
            hint = self.query_one("#live-hint", Static)
        except Exception:
            return
        parts: list[str] = []
        parts.append(f"peer {peer}" if peer else "peer —")
        if tick is not None:
            parts.append(f"tick {tick}")
        if runtime_s is not None:
            parts.append(f"runtime {_fmt_runtime(runtime_s)}")
        parts.append(idle_label(idle_s))
        header.update("  ·  ".join(parts))
        if live:
            hint.update(f"live ({tool}) — following the session jsonl in real time")
            hint.set_class(False, "state-fail")
        else:
            hint.update(
                f"tick-level ({tool or 'peer'}) — completed stdout log; "
                "live stream lands in Wave 2"
            )
            hint.set_class(False, "state-fail")

    # ------------------------------------------------------------------ #
    # body                                                               #
    # ------------------------------------------------------------------ #
    def show_empty(self, message: str) -> None:
        """Render the empty-state (no run / no peer) and clear the body."""
        try:
            body = self.query_one("#live-body", VerticalScroll)
            empty = self.query_one("#live-empty", Static)
        except Exception:
            return
        body.remove_children()
        self._row_count = 0
        body.display = False
        empty.display = True
        empty.update(message)

    def clear(self) -> None:
        """Drop all rows (e.g. on a run switch) and re-arm auto-follow."""
        try:
            body = self.query_one("#live-body", VerticalScroll)
            empty = self.query_one("#live-empty", Static)
        except Exception:
            return
        body.remove_children()
        self._row_count = 0
        self._follow = True
        empty.display = False
        body.display = True

    def append_rows(self, rows: list[tuple[str, str]]) -> None:
        """Append decoded ``(kind, text)`` rows; color + auto-scroll. Fail-soft.

        Auto-scrolls to the newest row only while following (the operator has not
        scrolled up). Trims the oldest rows past ``_MAX_ROWS`` so the DOM stays
        bounded on a long run."""
        rows = [r for r in (rows or []) if isinstance(r, tuple) and len(r) == 2]
        if not rows:
            return
        try:
            body = self.query_one("#live-body", VerticalScroll)
            empty = self.query_one("#live-empty", Static)
        except Exception:
            return
        empty.display = False
        body.display = True
        for kind, text in rows:
            row = Label(str(text))
            row.add_class("live-row")
            row.add_class(kind_class(str(kind)))
            body.mount(row)
            self._row_count += 1
        # trim oldest rows past the cap (keep the DOM bounded).
        if self._row_count > _MAX_ROWS:
            try:
                labels = list(body.query(Label))
                excess = len(labels) - _MAX_ROWS
                for old in labels[:excess]:
                    old.remove()
                self._row_count = min(self._row_count, _MAX_ROWS)
            except Exception:
                pass
        if self._follow:
            try:
                body.scroll_end(animate=False)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # pause-on-scroll-up                                                  #
    # ------------------------------------------------------------------ #
    def on_descendant_focus(self, _event) -> None:  # pragma: no cover - ui glue
        pass

    def _update_follow(self) -> None:
        """Re-evaluate auto-follow from the body's scroll position."""
        try:
            body = self.query_one("#live-body", VerticalScroll)
        except Exception:
            return
        # follow only when the operator is at (or near) the bottom.
        try:
            at_bottom = body.scroll_offset.y >= body.max_scroll_y
        except Exception:
            at_bottom = True
        self._follow = bool(at_bottom)

    def on_mouse_scroll_up(self, _event) -> None:  # pragma: no cover - ui glue
        # the operator scrolled up -> pause auto-follow.
        self._follow = False

    def on_mouse_scroll_down(self, _event) -> None:  # pragma: no cover - ui glue
        self._update_follow()


def _fmt_runtime(seconds: int) -> str:
    """Compact ``Hh Mm Ss`` runtime (drops leading zero units)."""
    try:
        s = max(0, int(seconds))
    except (TypeError, ValueError):
        return "?"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{sec:02d}s"
    return f"{sec}s"
