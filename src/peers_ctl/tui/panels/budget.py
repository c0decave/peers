"""Budget & Health panel: runtime/tokens/$ bars + wasted-runtime + failures.

Pure renderer over a :class:`BudgetView` (Wave-1a). It holds no file I/O and
never crashes on missing/None data. Spend bars color by how close they are to
their cap (green under 75%, yellow 75-90%, red over 90%); an *uncapped* metric
(``max_* is None``) is shown as a plain spend figure with no bar (an OAuth run
has no meaningful $ cap — see the global "OAuth -> no max_usd hard cap" rule).
``consecutive_failures`` reads red once it climbs.
"""
from __future__ import annotations

from textual.containers import VerticalScroll
from textual.widgets import Label, ProgressBar, Static

from peers_ctl.tui.snapshots import BudgetView

#: spend-fraction thresholds for the bar color.
_WARN_FRAC = 0.75
_CRIT_FRAC = 0.90


def budget_fraction_class(spent: float, cap: float | None) -> str:
    """`.state-*` accent for a spend bar by its fraction of the cap.

    Uncapped (cap None/0) -> dim (no pressure). Otherwise green/yellow/red as the
    spend approaches the cap. A spend at/over the cap reads red (fail)."""
    if not cap or cap <= 0:
        return "state-dim"
    frac = spent / cap
    if frac >= _CRIT_FRAC:
        return "state-fail"
    if frac >= _WARN_FRAC:
        return "state-pending"
    return "state-pass"


def _fmt_runtime(s: int) -> str:
    s = max(0, int(s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{sec:02d}s"
    return f"{sec}s"


class BudgetPanel(Static):
    """The Budget & Health cockpit panel."""

    can_focus = True  # so the panel can be focused + popped out as a Window.

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = "Budget & Health"

    def compose(self):
        yield Static("", id="budget-runtime")
        yield ProgressBar(id="budget-runtime-bar", total=100, show_eta=False)
        yield Static("", id="budget-tokens")
        yield ProgressBar(id="budget-tokens-bar", total=100, show_eta=False)
        yield Static("", id="budget-usd")
        yield ProgressBar(id="budget-usd-bar", total=100, show_eta=False)
        yield Static("", id="budget-failures", classes="title-row")
        yield Static("wasted runtime", classes="muted")
        yield VerticalScroll(id="budget-wasted")

    def _metric(self, head_id: str, bar_id: str, label: str,
                spent: float, cap: float | None, fmt) -> None:
        try:
            head = self.query_one(f"#{head_id}", Static)
            bar = self.query_one(f"#{bar_id}", ProgressBar)
        except Exception:
            return
        for cls in ("state-pass", "state-pending", "state-fail", "state-dim"):
            head.remove_class(cls)
        cls = budget_fraction_class(spent, cap)
        head.add_class(cls)
        if cap and cap > 0:
            head.update(f"{label}: {fmt(spent)} / {fmt(cap)}")
            bar.display = True
            # use integer scaled progress so float $ caps still render.
            bar.update(total=1000, progress=min(1000, int(spent / cap * 1000)))
        else:
            head.update(f"{label}: {fmt(spent)} (uncapped)")
            bar.display = False

    def render_budget(self, budget: BudgetView | None) -> None:
        """Rebuild the panel (pure: no I/O). Safe on None/garbage."""
        if not isinstance(budget, BudgetView):
            budget = BudgetView(
                spent_runtime_s=0, max_runtime_s=None, spent_tokens=0,
                max_tokens=None, spent_usd=0.0, max_usd=None, max_usd_mode=None,
                max_usd_mode_reason=None, consecutive_failures=0, wasted_runtime=[],
            )
        self._metric("budget-runtime", "budget-runtime-bar", "runtime",
                     budget.spent_runtime_s, budget.max_runtime_s, _fmt_runtime)
        self._metric("budget-tokens", "budget-tokens-bar", "tokens",
                     budget.spent_tokens, budget.max_tokens,
                     lambda v: f"{int(v):,}")
        self._metric("budget-usd", "budget-usd-bar", "$",
                     budget.spent_usd, budget.max_usd, lambda v: f"${v:.2f}")

        try:
            fails = self.query_one("#budget-failures", Static)
            wasted_body = self.query_one("#budget-wasted", VerticalScroll)
        except Exception:
            return
        fails.remove_class("state-fail")
        fails.remove_class("state-pass")
        cf = int(budget.consecutive_failures or 0)
        fails.update(f"consecutive failures: {cf}")
        fails.add_class("state-fail" if cf > 0 else "state-pass")

        wasted_body.remove_children()
        wasted = [w for w in (budget.wasted_runtime or []) if isinstance(w, dict)]
        if not wasted:
            wasted_body.mount(Label("none", classes="empty-state"))
            return
        for w in wasted:
            it = w.get("iteration")
            peer = w.get("peer")
            dur = w.get("duration_s")
            row = Label(f"it{it}  {peer}  {_fmt_runtime(int(dur or 0))}")
            row.add_class("state-pending")
            wasted_body.mount(row)
