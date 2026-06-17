"""Diff panel: renders a ``commit_diff`` string for a selected tick's commit.

Pure renderer over a diff *string* (the app calls ``reader.commit_diff(repo, sha)``
off-thread and hands the result here). Holds no file I/O of its own. It colors
added lines green, removed lines red, hunk headers cyan, and the diff/file
headers as titles — a lightweight syntax pass over unified-diff text. An empty
string (no sha selected, or a fail-soft git error) shows a friendly empty-state.
"""
from __future__ import annotations

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Label, Static


def _diff_line_class(line: str) -> str | None:
    """`.state-*` accent for one unified-diff line (None = no accent)."""
    if line.startswith("@@"):
        return "state-info"            # hunk header -> cyan
    if line.startswith("+++") or line.startswith("---") or line.startswith("diff "):
        return "title-row"             # file/diff headers -> bold title
    if line.startswith("+"):
        return "state-pass"            # added -> green
    if line.startswith("-"):
        return "state-fail"            # removed -> red
    return None


class DiffPanel(Static):
    """The Diff cockpit panel."""

    can_focus = True  # so the panel can be focused + popped out as a Window.

    #: cap on rendered lines so a huge (but capped) diff can't mount thousands of
    #: widgets; the reader already byte-caps, this bounds the widget count too.
    _MAX_LINES = 600

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = "Diff"
        self._sha: str | None = None

    def compose(self):
        yield Static("", id="diff-header", classes="title-row")
        yield VerticalScroll(id="diff-body")

    def render_diff(self, diff_text: str, *, sha: str | None = None) -> None:
        """Rebuild the diff body from a diff string (pure: no I/O)."""
        self._sha = sha
        try:
            header = self.query_one("#diff-header", Static)
            body = self.query_one("#diff-body", VerticalScroll)
        except Exception:
            return
        header.update(f"commit {sha[:8]}" if sha else "no commit selected")
        body.remove_children()
        diff_text = diff_text or ""
        if not diff_text.strip():
            body.mount(Label(
                "no diff — select a tick, or the commit is unavailable",
                classes="empty-state"))
            return
        lines = diff_text.splitlines()
        truncated = len(lines) > self._MAX_LINES
        for line in lines[: self._MAX_LINES]:
            row = Label(Text(line))
            row.add_class("diff-line")
            cls = _diff_line_class(line)
            if cls:
                row.add_class(cls)
            body.mount(row)
        if truncated:
            body.mount(Label("… diff truncated", classes="empty-state"))
