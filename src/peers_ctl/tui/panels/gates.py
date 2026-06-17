"""Gates panel: state-colored list of hard + soft gates for the active run.

Pure renderer over ``list[GateView]`` (Wave-1a) + an optional ``ConvergenceView``.
Holds no file I/O and never crashes on missing/None data. Each gate row gets a
``.state-*`` accent class so the theme colors pass/fail/pending/stuck/cached.

Wave-2 §5.2 adds a **gate-history scrubber**: the operator steps back/forward
through past ticks (``[`` / ``]``; ``\\`` returns to live) to view the gate
stand at a PAST tick. The header then shows the absolute ts + a relative
("vor 3 min") offset + the tick number. The live view (current ``state.json``
gates) is the default and is unchanged when not scrubbing. The history comes
from ``reader.gate_history`` (the per-tick ``gates`` snapshot in ``runs.jsonl``);
with no snapshots the scrubber is a no-op with a hint.
"""
from __future__ import annotations

from datetime import datetime

from textual.containers import VerticalScroll
from textual.widgets import Label, Static

from peers_ctl.tui import reader
from peers_ctl.tui.snapshots import ConvergenceView, GateSnapshotRow, GateView


def gate_state_class(gate: GateView) -> str:
    """Map a gate's *severity* to its `.state-*` accent color class.

    This carries the green/yellow/red SEVERITY color only: a cached PASS reads
    dim; a passing hard gate reads green; a FAILING hard gate ALWAYS reads red
    (red wins regardless of stuckness). "stuck" is NOT a color — it is an
    additive emphasis marker (the orthogonal ``.gate-stuck`` class applied in
    :meth:`GatesPanel.render_gates`), so a wedged hard gate still reads red.
    """
    if getattr(gate, "cached", False):
        return "state-cached"
    if getattr(gate, "kind", "") == "hard":
        st = str(getattr(gate, "state", "unknown"))
        if st == "pass":
            return "state-pass"
        if st == "fail":
            return "state-fail"
        return "state-unknown"
    # soft
    return "state-reached" if str(getattr(gate, "state", "")) == "reached" else "state-pending"


def _rel_de(ts: str, *, now: datetime | None = None) -> str:
    """German relative offset ("vor 3 min") from an ISO ts; '' on parse fail.

    Mirrors the cockpit's relative-time idiom but in the operator's German
    locale (the design's "vor 3 min" header). Pure + total."""
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
        return "jetzt"
    if delta < 60:
        return f"vor {int(delta)} s"
    if delta < 3600:
        return f"vor {int(delta // 60)} min"
    if delta < 86400:
        return f"vor {int(delta // 3600)} h"
    return f"vor {int(delta // 86400)} d"


def _fmt_gap(gap_s: float | None) -> str:
    """Human tick-duration/gap ("+30s" / "+2m"); '' when unknown."""
    if gap_s is None:
        return ""
    try:
        g = float(gap_s)
    except (TypeError, ValueError):
        return ""
    if g < 0:
        return ""
    if g < 60:
        return f"+{int(g)}s"
    if g < 3600:
        return f"+{int(g // 60)}m"
    return f"+{int(g // 3600)}h"


def scrub_header(row: GateSnapshotRow, *, now: datetime | None = None) -> str:
    """Header line for a scrubbed (historical) tick: absolute ts + relative +
    tick number + green tally + gap. Pure + total (safe on a partial row)."""
    it = getattr(row, "iteration", None)
    tick = f"tick {it}" if it is not None else "tick ?"
    ts = str(getattr(row, "ts", "") or "")
    rel = _rel_de(ts, now=now)
    green = getattr(row, "green", 0) or 0
    total = getattr(row, "total", 0) or 0
    gap = _fmt_gap(getattr(row, "gap_s", None))
    parts = [f"⏪ HISTORY · {tick}", f"{green}/{total} green"]
    if ts:
        parts.append(ts)
    if rel:
        parts.append(rel)
    if gap:
        parts.append(f"Δ{gap}")
    return "  ·  ".join(parts)


def _gate_line(gate: GateView) -> str:
    """One compact gate row."""
    gid = str(getattr(gate, "id", "?") or "?")
    kind = str(getattr(gate, "kind", "") or "")
    state = str(getattr(gate, "state", "unknown") or "unknown")
    parts = [f"[{kind}]", gid, state]
    stuck = getattr(gate, "stuck", 0) or 0
    if stuck:
        parts.append(f"stuck×{stuck}")
    if getattr(gate, "cached", False):
        parts.append("cached")
    cons = getattr(gate, "consensus", None)
    if cons:
        parts.append(f"{cons[0]}/{cons[1]}")
    return "  ".join(parts)


class GatesPanel(Static):
    """The Gates cockpit panel.

    Two view modes:

    * **live** (default): renders the current ``state.json`` gates via
      :meth:`render_gates`. ``_scrub_index is None`` selects this mode.
    * **history**: when the operator scrubs (``[`` / ``]``), renders the gate
      stand of a PAST tick from the ``reader.gate_history`` rows fed by
      :meth:`set_history`. ``\\`` returns to live.

    Scrubbing is additive: it never touches the live data and is fully fail-soft
    (no history -> the scrub keys are no-ops; a stale index is re-clamped)."""

    can_focus = True  # so the panel can be focused + popped out as a Window.

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = "Gates"
        #: most recent live render inputs, so a return-to-live repaints exactly.
        self._live_gates: list[GateView] = []
        self._live_convergence: ConvergenceView | None = None
        #: the gate-history rows (oldest..newest); empty -> scrubber disabled.
        self._history: list[GateSnapshotRow] = []
        #: None = live view; otherwise an index into ``self._history``.
        self._scrub_index: int | None = None

    def compose(self):
        yield Static("", id="gates-header", classes="title-row")
        yield VerticalScroll(id="gates-body")

    # ---- scrubber state machine (pure-ish; only repaints) --------------- #

    def set_history(self, rows: list[GateSnapshotRow] | None) -> None:
        """Feed the latest gate-history rows (oldest..newest). Re-clamps an
        active scrub index if the history shrank; never auto-enters history."""
        self._history = [r for r in (rows or []) if isinstance(r, GateSnapshotRow)]
        if self._scrub_index is not None:
            if not self._history:
                self._scrub_index = None
            else:
                self._scrub_index = max(0, min(self._scrub_index, len(self._history) - 1))

    @property
    def scrubbing(self) -> bool:
        """True when showing a historical tick (not the live view)."""
        return self._scrub_index is not None

    def scrub_back(self) -> bool:
        """Step one tick into the past. Entering history starts at the newest
        snapshot. Returns True if the view changed (False = no history / at the
        oldest tick already)."""
        if not self._history:
            return False
        if self._scrub_index is None:
            self._scrub_index = len(self._history) - 1
        elif self._scrub_index > 0:
            self._scrub_index -= 1
        else:
            return False
        self._repaint()
        return True

    def scrub_forward(self) -> bool:
        """Step one tick toward the present. Stepping past the newest snapshot
        returns to the LIVE view. Returns True if the view changed."""
        if self._scrub_index is None:
            return False
        if self._scrub_index < len(self._history) - 1:
            self._scrub_index += 1
        else:
            self._scrub_index = None  # past the newest snapshot -> live
        self._repaint()
        return True

    def scrub_live(self) -> bool:
        """Return to the live view. Returns True if it was scrubbing."""
        if self._scrub_index is None:
            return False
        self._scrub_index = None
        self._repaint()
        return True

    def _repaint(self) -> None:
        if self._scrub_index is None:
            self._render_live()
        else:
            self._render_history(self._history[self._scrub_index])

    # ---- rendering ----------------------------------------------------- #

    def render_gates(
        self,
        gates: list[GateView],
        convergence: ConvergenceView | None = None,
        history: list[GateSnapshotRow] | None = None,
    ) -> None:
        """Update the live data (+ optional history) and paint.

        While the operator is scrubbing, the live data is stored but the
        historical view stays put — a background poll must not yank the
        operator off the tick they're inspecting. Safe on None/garbage."""
        self._live_gates = [g for g in (gates or []) if isinstance(g, GateView)]
        self._live_convergence = convergence
        if history is not None:
            self.set_history(history)
        if self._scrub_index is None:
            self._render_live()
        else:
            self._render_history(self._history[self._scrub_index])

    def _render_live(self) -> None:
        gates = self._live_gates
        convergence = self._live_convergence
        try:
            header = self.query_one("#gates-header", Static)
            body = self.query_one("#gates-body", VerticalScroll)
        except Exception:
            return
        green = sum(
            1 for g in gates
            if (g.kind == "hard" and g.state == "pass")
            or (g.kind == "soft" and g.state == "reached")
        )
        clean = getattr(convergence, "consecutive_clean_ticks", None) if convergence else None
        head = f"{green}/{len(gates)} green"
        if clean is not None:
            head += f"  ·  clean×{clean}"
        if self._history:
            head += f"  ·  [ history {len(self._history)} ticks ([/] scrub)"
        header.update(head)
        body.remove_children()
        if not gates:
            ph = Label("no gates yet — run has not produced gate status", classes="empty-state")
            body.mount(ph)
            return
        for g in gates:
            row = Label(_gate_line(g))
            row.add_class("gate-row")
            row.add_class(gate_state_class(g))
            # "stuck" is an ADDITIVE emphasis marker, orthogonal to the severity
            # color above — it must NOT recolor the row (a stuck FAIL stays red).
            if getattr(g, "stuck", 0) and not getattr(g, "cached", False):
                row.add_class("gate-stuck")
            body.mount(row)

    def _render_history(self, row: GateSnapshotRow) -> None:
        """Paint the gate stand of a PAST tick (read-only; no convergence)."""
        try:
            header = self.query_one("#gates-header", Static)
            body = self.query_one("#gates-body", VerticalScroll)
        except Exception:
            return
        header.update(scrub_header(row) + "   (\\ = live)")
        body.remove_children()
        views = reader.gate_snapshot_views(row)
        if not views:
            body.mount(Label("no gate snapshot for this tick", classes="empty-state"))
            return
        for g in views:
            label = Label(_gate_line(g))
            label.add_class("gate-row")
            label.add_class(gate_state_class(g))
            body.mount(label)
