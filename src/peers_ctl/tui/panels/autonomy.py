"""Autonomy / Agentic-OS panels (Wave 1b, Unit J) — forward-looking, empty today.

These five windows watch *what the LLM does autonomously* once the agentic-os
spine becomes operator-runnable: the per-run ``run.jsonl`` ledger, the 4 spine
gates (re-derived), the propagation DAG across runs, a merged autonomy feed, and
the escalation banner (the inverse — when the system hands control back).

**Forward-looking by design (verified):** the spine is NOT yet wired to any
``peers-ctl``-launchable run, so for every run an operator can launch today there
is NO ``.peers/run.jsonl`` and NO ``.peers/spine-runs/`` registry. Each panel
therefore renders an HONEST **empty-state** ("no spine runs present — lights up
when the agentic-os spine is operator-runnable") and **never fabricates data**.
They light up automatically once the readers find real ledgers/registries.

**Honesty rule (Stage-7 F2 carry-forward — enforced here):** these panels NEVER
display CONVERGED / independent off a *stored* ``independence`` flag. They render
ONLY the values the reader RE-DERIVES: :class:`AutonomyLedgerView.converged` and
``.gates`` (via ``is_converged`` / ``evaluate_spine_gates``). The stored
``independence`` field is surfaced in the ledger timeline as an AUDIT note only
(dim, labelled "stored") and is never used to decide a gate, convergence, or the
integrity badge. The reader already does the re-derivation; the panels must not
re-introduce trust in the stored flag.

**Scope of that guarantee (do not over-read it):** what is guaranteed is narrow —
the *stored* ``independence`` flag is never trusted. A displayed CONVERGED is only
as trustworthy as the substrate's RE-DERIVATION itself: gates re-evaluated and the
hash chain verified. That re-derivation still trusts substrate-attested
*authorship* — the ``authorship-attested`` gate only checks the author is not
None, and ``.peers`` is agent-writable, so a hand-written ``run.jsonl`` with a
recomputed-valid hash chain and an arbitrary ``author`` would pass ``verify()`` and
the gate. That is a SUBSTRATE limit, not something these panels introduce or can
close; they simply never add MORE trust than the re-derivation already grants.

All panels are pure renderers: they take the Wave-1a view objects and paint; they
hold no file I/O and never crash on missing/None data.
"""
from __future__ import annotations

from textual.containers import VerticalScroll
from textual.widgets import Label, Static

from peers_ctl.tui.snapshots import AutonomyLedgerView, SpineRunEntry

#: the four spine gates, in canonical order, with their display labels. Keys must
#: match ``peers.spine.gates.evaluate_spine_gates`` output exactly.
SPINE_GATE_LABELS: tuple[tuple[str, str], ...] = (
    ("ModeRun-valid", "ModeRun-valid"),
    ("witness-ledgered", "witness-ledgered"),
    ("authorship-attested", "authorship-attested"),
    ("stop-on-dry", "stop-on-dry"),
)

#: the honest forward-looking empty-state line shared across the autonomy windows.
_EMPTY_LINE = (
    "no spine runs present — lights up when the agentic-os spine is "
    "operator-runnable"
)


def _empty_state(view: AutonomyLedgerView | None) -> bool:
    """True iff the ledger view carries no real ledger (the forward-looking case).

    A view with ``verified is None`` AND no events is the honest "no ledger yet"
    case the empty-state renders. (``verified`` is None only when the ledger is
    absent; a present-but-corrupt ledger has ``verified=False`` and IS shown.)
    """
    if view is None:
        return True
    return view.verified is None and not view.events


# --------------------------------------------------------------------------- #
# 1. Autonomie-Ledger                                                          #
# --------------------------------------------------------------------------- #
class AutonomyLedgerPanel(Static):
    """The per-run autonomy ledger: integrity badge · dry-streak · timeline."""

    can_focus = True

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = "Autonomie-Ledger"

    def compose(self):
        yield Static("", id="autoledger-header", classes="title-row")
        yield VerticalScroll(id="autoledger-body")

    def render_ledger(
        self, view: AutonomyLedgerView | None, *, dry_n: int = 3,
    ) -> None:
        """Paint the integrity badge + dry-streak + timeline. Honest empty-state.

        NOTE: convergence/gates come from the RE-DERIVED view fields only; the
        per-event ``independence`` is shown as a dim audit note, never as truth."""
        try:
            header = self.query_one("#autoledger-header", Static)
            body = self.query_one("#autoledger-body", VerticalScroll)
        except Exception:
            return
        body.remove_children()
        header.remove_class("state-pass")
        header.remove_class("state-fail")
        header.remove_class("state-dim")
        if _empty_state(view):
            header.add_class("state-dim")
            header.update("· no ledger")
            body.mount(Label(_EMPTY_LINE, classes="empty-state"))
            return
        assert view is not None
        # integrity badge: verify() is the hash-chain check (fail-closed).
        if view.verified is True:
            header.add_class("state-pass")
            badge = "✓ integrity verified"
        else:
            header.add_class("state-fail")
            badge = "⚠ integrity FAILED (tampered/corrupt ledger)"
        streak = f"dry-streak {view.dry_streak}/{dry_n}"
        if view.dry_streak >= dry_n:
            streak += " → STOP"
        header.update(f"{badge}  ·  {streak}")
        if not view.events:
            body.mount(Label("ledger present, no events yet", classes="muted"))
            return
        # HONESTY HARDENING: per-event ``status`` is a FORGEABLE stored field. On
        # a failed-integrity ledger (verify() is False -> tampered/corrupt) a
        # stored ``status: pass`` could paint a reassuring green row right next to
        # the red integrity badge. So when the chain is NOT verified we neutralize
        # every event-row status color to a dim/neutral class — a tampered ledger
        # must show nothing reassuring. Only a verified ledger gets status colors.
        ledger_ok = view.verified is True
        for ev in view.events:
            body.mount(self._event_row(ev, verified=ledger_ok))

    @staticmethod
    def _event_row(ev: dict, *, verified: bool = True) -> Label:
        event = str(ev.get("event") or "?")
        status = ev.get("status")
        author = ev.get("author")
        subject = ev.get("subject")
        parts = [event]
        if status:
            parts.append(str(status))
        if subject:
            parts.append(str(subject)[:48])
        if author:
            parts.append(f"({author})")
        # the STORED independence flag: shown as an audit note ONLY — never used
        # to color/decide anything (honesty rule). Dim + explicitly labelled.
        if ev.get("independence"):
            parts.append("[stored independence — audit only]")
        row = Label("  ".join(parts), classes="autoledger-event")
        # On a failed-integrity ledger every status color is neutralized to dim
        # (the stored status is untrustworthy) — see ``render_ledger``. Only when
        # the hash-chain verifies do we trust ``status`` enough to color by it.
        st = str(status or "").lower()
        if not verified:
            row.add_class("state-dim")
        elif st == "pass":
            row.add_class("state-pass")
        elif st in ("fail", "error"):
            row.add_class("state-fail")
        else:
            row.add_class("state-dim")
        return row


# --------------------------------------------------------------------------- #
# 2. Spine-Gates (the 4, re-derived)                                          #
# --------------------------------------------------------------------------- #
class SpineGatesPanel(Static):
    """The 4 spine gates + the re-derived CONVERGED verdict (read-only)."""

    can_focus = True

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = "Spine-Gates"

    def compose(self):
        yield Static("", id="spinegates-header", classes="title-row")
        yield VerticalScroll(id="spinegates-body")

    def render_gates(self, view: AutonomyLedgerView | None) -> None:
        """Paint the 4 gates pass/fail (RE-DERIVED) + CONVERGED. Empty-state honest.

        Reads ONLY ``view.gates`` / ``view.converged`` (re-derived by the reader
        via ``evaluate_spine_gates`` / ``is_converged``). Never the stored flag."""
        try:
            header = self.query_one("#spinegates-header", Static)
            body = self.query_one("#spinegates-body", VerticalScroll)
        except Exception:
            return
        body.remove_children()
        header.remove_class("state-pass")
        header.remove_class("state-fail")
        header.remove_class("state-dim")
        if _empty_state(view) or not (view and view.gates):
            header.add_class("state-dim")
            header.update("· no spine gates")
            body.mount(Label(_EMPTY_LINE, classes="empty-state"))
            return
        assert view is not None
        gates = view.gates
        npass = sum(1 for _k, _v in SPINE_GATE_LABELS if gates.get(_k) is True)
        header.update(f"{npass}/{len(SPINE_GATE_LABELS)} gates pass")
        for key, label in SPINE_GATE_LABELS:
            passed = gates.get(key) is True
            glyph = "✓" if passed else "✗"
            row = Label(f"{glyph} {label}", classes="spinegate-row")
            row.add_class("state-pass" if passed else "state-fail")
            body.mount(row)
        # the re-derived CONVERGED verdict (the honesty seam made visible).
        conv = bool(view.converged)
        verdict = Label(
            ("✓ CONVERGED (re-derived)" if conv
             else "· not converged (re-derived)"),
            classes="spinegate-verdict",
        )
        verdict.add_class("state-converged" if conv else "state-dim")
        body.mount(verdict)


# --------------------------------------------------------------------------- #
# 3. Propagations-DAG                                                          #
# --------------------------------------------------------------------------- #
class PropagationsPanel(Static):
    """Nodes = spine runs, edges = propagations (from_run → to_run). Empty today."""

    can_focus = True

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = "Propagations-DAG"

    def compose(self):
        yield Static("", id="propagations-header", classes="title-row")
        yield VerticalScroll(id="propagations-body")

    def render_dag(
        self,
        runs: list[SpineRunEntry] | None,
        edges: list[tuple[str, str, str]] | None = None,
    ) -> None:
        """Paint the run nodes + propagation edges. Honest empty-state.

        ``edges`` is a list of ``(from_run, to_run, artifact)``; in Wave 1 there
        are no spine runs -> an empty list -> the forward-looking empty-state."""
        runs = [r for r in (runs or []) if isinstance(r, SpineRunEntry)]
        edges = list(edges or [])
        try:
            header = self.query_one("#propagations-header", Static)
            body = self.query_one("#propagations-body", VerticalScroll)
        except Exception:
            return
        body.remove_children()
        header.remove_class("state-dim")
        if not runs and not edges:
            header.add_class("state-dim")
            header.update("· no spine runs / no propagation edges yet")
            body.mount(Label(_EMPTY_LINE, classes="empty-state"))
            return
        header.update(f"{len(runs)} runs  ·  {len(edges)} edges")
        for r in runs:
            mr = str(getattr(r, "mode_run", None) or "?")
            br = getattr(r, "branch", None)
            line = f"● {mr}" + (f"  [{br}]" if br else "")
            body.mount(Label(line, classes="propagation-node"))
        for frm, to, art in edges:
            edge = f"  {frm} → {to}" + (f"  ({art})" if art else "")
            body.mount(Label(edge, classes="propagation-edge state-info"))


# --------------------------------------------------------------------------- #
# 4. Autonomie-Feed                                                           #
# --------------------------------------------------------------------------- #
class AutonomyFeedPanel(Static):
    """A merged chronological view over ledger/gates/propagation. Empty today."""

    can_focus = True

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = "Autonomie-Feed"

    def compose(self):
        yield Static("", id="autofeed-header", classes="title-row")
        yield VerticalScroll(id="autofeed-body")

    def render_feed(
        self,
        view: AutonomyLedgerView | None,
        runs: list[SpineRunEntry] | None = None,
    ) -> None:
        """Merge the ledger events + run nodes into one chronological feed.

        Forward-looking: with no ledger and no runs this is the honest empty-state.
        The convergence note uses the RE-DERIVED ``view.converged`` only."""
        runs = [r for r in (runs or []) if isinstance(r, SpineRunEntry)]
        try:
            header = self.query_one("#autofeed-header", Static)
            body = self.query_one("#autofeed-body", VerticalScroll)
        except Exception:
            return
        body.remove_children()
        header.remove_class("state-dim")
        empty = _empty_state(view) and not runs
        if empty:
            header.add_class("state-dim")
            header.update("· feed empty")
            body.mount(Label(_EMPTY_LINE, classes="empty-state"))
            return
        lines: list[str] = []
        for r in runs:
            mr = str(getattr(r, "mode_run", None) or "?")
            lines.append(f"run {mr}: registered")
        if view is not None:
            for ev in view.events:
                event = str(ev.get("event") or "?")
                subject = ev.get("subject")
                author = ev.get("author")
                line = f"ledger: {event}"
                if subject:
                    line += f" {str(subject)[:40]}"
                if author:
                    line += f" ({author})"
                lines.append(line)
            # the convergence note is RE-DERIVED, never the stored flag.
            if view.converged:
                lines.append("→ CONVERGED (re-derived) — awaiting your approval")
        header.update(f"{len(lines)} events")
        for line in lines:
            body.mount(Label(line, classes="autofeed-row"))


# --------------------------------------------------------------------------- #
# 5. Eskalations-Banner                                                       #
# --------------------------------------------------------------------------- #
class EscalationBannerPanel(Static):
    """The inverse of autonomy: a prominent RED banner when the system escalates.

    Reads ``reader.escalation_state`` -> ``{halted, concerns, halted_excerpt}``.
    When a ``HALTED.md`` / ``CONCERNS.md`` is present it shows a loud red banner
    ("⚠ das System eskaliert an dich"); otherwise it stays quiet/dim."""

    can_focus = True

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = "Eskalations-Banner"

    def compose(self):
        yield Static("", id="escalation-header", classes="title-row")
        yield VerticalScroll(id="escalation-body")

    def render_escalation(self, state: dict | None) -> None:
        """Paint the banner from an escalation_state dict. Quiet when no markers."""
        state = state if isinstance(state, dict) else {}
        halted = bool(state.get("halted"))
        concerns = bool(state.get("concerns"))
        excerpt = str(state.get("halted_excerpt") or "")
        try:
            header = self.query_one("#escalation-header", Static)
            body = self.query_one("#escalation-body", VerticalScroll)
        except Exception:
            return
        body.remove_children()
        header.remove_class("state-fail")
        header.remove_class("state-dim")
        header.remove_class("escalation-active")
        if not halted and not concerns:
            header.add_class("state-dim")
            header.update("· quiet — the system has not escalated")
            body.mount(Label(
                "nothing to escalate — the run is autonomous", classes="muted"))
            return
        # ESCALATION: a prominent red banner.
        header.add_class("state-fail")
        header.add_class("escalation-active")
        header.update("⚠ das System eskaliert an dich")
        markers = []
        if halted:
            markers.append("HALTED.md")
        if concerns:
            markers.append("CONCERNS.md")
        banner = Label(
            "⚠ " + " + ".join(markers) + " present — the run handed control back",
            classes="escalation-banner",
        )
        banner.add_class("state-fail")
        body.mount(banner)
        if excerpt:
            body.mount(Label(excerpt.strip()[:1024], classes="escalation-excerpt muted"))
