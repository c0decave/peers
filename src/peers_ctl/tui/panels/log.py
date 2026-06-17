"""Log panel: recent merged events (warnings history + last-stop-reason).

Pure renderer over ``list[LogRow]`` (Wave-1a ``reader.log_lines``). Holds no file
I/O. A warning row reads yellow; the stop-reason row reads green for a clean
self-termination (``stopped``/``converged``/``complete``) and red for a crash.
"""
from __future__ import annotations

from textual.containers import VerticalScroll
from textual.widgets import Label, Static

from peers_ctl.tui.snapshots import LogRow

#: stop reasons that read green (a clean self-termination); anything else red.
_CLEAN_STOP_PREFIXES = ("stopped", "converged", "complete", "done", "halt-clean")


def log_state_class(row: LogRow) -> str:
    """`.state-*` accent for a log row. Warnings are yellow; a clean stop is
    green; a crash/other stop is red."""
    kind = str(getattr(row, "kind", ""))
    if kind == "warning":
        return "state-pending"
    text = str(getattr(row, "text", "")).lower()
    if any(text.startswith(p) for p in _CLEAN_STOP_PREFIXES):
        return "state-pass"
    return "state-fail"


def _log_line(row: LogRow) -> str:
    it = getattr(row, "iteration", None)
    prefix = f"it{it}" if it is not None else "·"
    kind = str(getattr(row, "kind", "")).upper()[:4]
    text = str(getattr(row, "text", "") or "")
    return f"{prefix}  [{kind}]  {text}"


class LogPanel(Static):
    """The Log cockpit panel."""

    can_focus = True  # so the panel can be focused + popped out as a Window.

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = "Log"

    def compose(self):
        yield Static("", id="log-header", classes="title-row")
        yield VerticalScroll(id="log-body")

    def render_log(self, rows: list[LogRow]) -> None:
        """Rebuild the log rows (pure: no I/O). Safe on None/garbage."""
        rows = [r for r in (rows or []) if isinstance(r, LogRow)]
        try:
            header = self.query_one("#log-header", Static)
            body = self.query_one("#log-body", VerticalScroll)
        except Exception:
            return
        header.update(f"{len(rows)} events")
        body.remove_children()
        if not rows:
            body.mount(Label("no log events yet", classes="empty-state"))
            return
        for r in rows:
            row = Label(_log_line(r))
            row.add_class("log-row")
            row.add_class(log_state_class(r))
            body.mount(row)
