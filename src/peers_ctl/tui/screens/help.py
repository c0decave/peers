"""HelpScreen — a modal cheat-sheet of every keybinding, grouped by purpose.

Opened with ``?`` and dismissed with ``?``/``escape``/``q``. The binding table
is the single source of truth (:data:`HELP_GROUPS`) shared with the Footer/help
so the docs can never drift from the live keymap. Pure presentation — no I/O.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Label, Static

#: The complete keymap, grouped for the help cheat-sheet. Each row is
#: ``(keys, description)``. Kept in sync with ``PeersTuiApp.BINDINGS``.
HELP_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Navigate", (
        ("↑ / k", "previous run (fleet)"),
        ("↓ / j", "next run (fleet)"),
        ("← / h / shift+tab", "previous panel / window"),
        ("→ / l / tab", "next panel / window"),
        ("enter", "open / drill into the selected row"),
    )),
    ("Focus a window", (
        ("p", "Live-Stream (focus / toggle)"),
        ("g", "Gates"),
        ("t", "Tasks / Steps"),
        ("b", "Bugs"),
        ("d", "Diff"),
    )),
    ("Actions", (
        ("n", "new run (launch wizard)"),
        ("s", "stop the active run"),
        ("r", "resume the active run"),
        ("a", "ack-block (acknowledge a blocked step)"),
        ("m", "amend (change acceptance)"),
    )),
    ("Windows", (
        ("1 – 9", "toggle a cockpit panel on / off"),
        ("o / space", "pop the focused panel out into a floating window"),
        ("x", "close the focused floating window"),
        ("f1", "window switcher (alt-tab over floating windows)"),
    )),
    ("Autonomy (Agentic-OS — empty until the spine is runnable)", (
        ("f5", "Autonomie-Ledger (re-derived integrity + dry-streak)"),
        ("f6", "Spine-Gates (the 4, re-derived → CONVERGED)"),
        ("f7", "Propagations-DAG (run → run propagation edges)"),
        ("f8", "Autonomie-Feed (merged chronological view)"),
        ("f9", "Eskalations-Banner (HALTED.md / CONCERNS.md)"),
    )),
    ("General", (
        ("?", "this help"),
        ("q", "quit"),
    )),
)


class HelpScreen(ModalScreen):
    """A modal overlay listing every keybinding, grouped."""

    BINDINGS = [
        Binding("question_mark,escape,q", "dismiss", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-dialog"):
            yield Static("peers-ctl — keybindings", id="help-title")
            for group, rows in HELP_GROUPS:
                yield Static(group, classes="help-group")
                for keys, desc in rows:
                    line = Label(f"  {keys:<22}  {desc}", classes="help-row")
                    yield line
            yield Static("? / esc / q to close", id="help-foot",
                         classes="muted")

    def action_dismiss(self, result=None) -> None:
        self.dismiss(result)
