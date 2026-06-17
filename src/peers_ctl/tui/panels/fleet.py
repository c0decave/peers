"""Fleet sidebar panel: a state-colored ``ListView`` of the registered projects.

Pure renderer over ``list[FleetEntry]`` (Wave-1a ``reader.fleet_entries``). It
holds no file I/O and never crashes on missing/None data: an empty fleet shows a
friendly empty-state row. Selecting/highlighting a row emits the entry's name so
the app can switch the active run.
"""
from __future__ import annotations

from rich.text import Text
from textual.message import Message
from textual.widgets import Label, ListItem, ListView, Static

from peers_ctl.tui.snapshots import FleetEntry

#: registry state string -> the `.state-*` accent class for the row.
_FLEET_STATE_CLASS = {
    "running": "state-info",
    "fresh": "state-dim",
    "stopped": "state-dim",
    "crashed": "state-fail",
    "unknown": "state-unknown",
}


def fleet_state_class(entry: FleetEntry) -> str:
    """The accent class for a fleet row: alert always wins (red), else by state."""
    if getattr(entry, "alert", False):
        return "state-alert"
    return _FLEET_STATE_CLASS.get(str(getattr(entry, "state", "unknown")), "state-unknown")


def _entry_line(entry: FleetEntry) -> Text:
    """One compact row: ``name  state  it<iter>  g<green>/<total>  [!]``."""
    name = str(getattr(entry, "name", "") or "?")
    state = str(getattr(entry, "state", "unknown") or "unknown")
    parts = [name, state]
    it = getattr(entry, "iteration", None)
    if it is not None:
        parts.append(f"it{it}")
    g, t = getattr(entry, "gates_green", None), getattr(entry, "gates_total", None)
    if g is not None and t is not None:
        parts.append(f"g{g}/{t}")
    if getattr(entry, "alert", False):
        parts.append("[!]")
    return Text("  ".join(parts))


class FleetRow(ListItem):
    """A single fleet ListItem that remembers which project it represents."""

    def __init__(self, entry: FleetEntry) -> None:
        self.entry = entry
        label = Label(_entry_line(entry))
        label.add_class(fleet_state_class(entry))
        super().__init__(label)


class FleetPanel(Static):
    """The left sidebar. Renders a ``ListView`` of fleet rows + an empty-state."""

    class RunSelected(Message):
        """Posted when the highlighted fleet project changes."""

        def __init__(self, entry: FleetEntry) -> None:
            self.entry = entry
            super().__init__()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = "Fleet"
        self._entries: list[FleetEntry] = []

    def compose(self):
        yield ListView(id="fleet-list")
        yield Static("", id="fleet-empty", classes="empty-state")

    def render_entries(self, entries: list[FleetEntry]) -> None:
        """Rebuild the list from ``entries`` (pure: no I/O). Safe on None/garbage."""
        entries = [e for e in (entries or []) if isinstance(e, FleetEntry)]
        self._entries = entries
        try:
            lv = self.query_one("#fleet-list", ListView)
            empty = self.query_one("#fleet-empty", Static)
        except Exception:
            return
        prev = self._highlighted_name()
        lv.clear()
        if not entries:
            lv.display = False
            empty.display = True
            empty.update("no projects registered — `peers-ctl new <path>` to add one")
            return
        empty.display = False
        lv.display = True
        for e in entries:
            lv.append(FleetRow(e))
        # restore the prior selection if it still exists, else select the first.
        target = 0
        if prev is not None:
            for i, e in enumerate(entries):
                if e.name == prev:
                    target = i
                    break
        lv.index = target

    def _highlighted_name(self) -> str | None:
        try:
            lv = self.query_one("#fleet-list", ListView)
        except Exception:
            return None
        child = lv.highlighted_child
        if isinstance(child, FleetRow):
            return child.entry.name
        return None

    def selected_entry(self) -> FleetEntry | None:
        """The currently highlighted FleetEntry, or None."""
        try:
            lv = self.query_one("#fleet-list", ListView)
        except Exception:
            return None
        child = lv.highlighted_child
        return child.entry if isinstance(child, FleetRow) else None

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        item = event.item
        if isinstance(item, FleetRow):
            self.post_message(self.RunSelected(item.entry))
