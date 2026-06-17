"""Tasks/Steps panel: phase band + PLAN done/total bar + bugs summary.

Pure renderer (no file I/O). It renders from a :class:`RunSnapshot` (for the
convergence phase + mode), a ``plan_progress`` tuple (done/total/[PlanStep]), and
a bugs summary (``total`` + ``blocking``). Implement-mode-aware: the
convergence-phase widget is hidden when ``snapshot.convergence.convergence_phase``
is ``None`` (i.e. non-implement runs have no phase band).
"""
from __future__ import annotations

from textual.containers import VerticalScroll
from textual.widgets import Label, ProgressBar, Static

from peers_ctl.tui.snapshots import PlanStep, RunSnapshot


def _phase_class(phase: str | None) -> str:
    """Phase band accent: a terminal/converged phase reads green, an in-flight
    phase reads cyan (info). Unknown -> muted."""
    if not phase:
        return "state-unknown"
    p = str(phase).lower()
    if "converg" in p or "complete" in p or "done" in p:
        return "state-converged"
    return "state-info"


class TasksPanel(Static):
    """The Tasks/Steps cockpit panel."""

    can_focus = True  # so the panel can be focused + popped out as a Window.

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = "Tasks / Steps"

    def compose(self):
        yield Static("", id="tasks-phase", classes="title-row")
        yield Static("", id="tasks-plan-head", classes="muted")
        yield ProgressBar(id="tasks-plan-bar", total=100, show_eta=False)
        yield Static("", id="tasks-bugs", classes="muted")
        yield VerticalScroll(id="tasks-steps")

    def render_tasks(
        self,
        snapshot: RunSnapshot | None,
        plan: tuple[int, int, list[PlanStep]] | None = None,
        *,
        bugs_total: int = 0,
        bugs_blocking: int = 0,
    ) -> None:
        """Rebuild the panel (pure: no I/O). Safe on None/garbage."""
        try:
            phase = self.query_one("#tasks-phase", Static)
            plan_head = self.query_one("#tasks-plan-head", Static)
            bar = self.query_one("#tasks-plan-bar", ProgressBar)
            bugs = self.query_one("#tasks-bugs", Static)
            steps_body = self.query_one("#tasks-steps", VerticalScroll)
        except Exception:
            return

        # ---- phase band (implement-mode-aware) -------------------------------
        conv = getattr(snapshot, "convergence", None) if snapshot else None
        conv_phase = getattr(conv, "convergence_phase", None) if conv else None
        # remove any stale phase accent class before re-applying.
        for cls in ("state-info", "state-converged", "state-unknown"):
            phase.remove_class(cls)
        if conv_phase is None:
            # Non-implement run: hide the convergence-phase widget. Show the
            # generic clean-tick count instead so the band is not blank.
            phase.display = False
        else:
            phase.display = True
            extra = getattr(conv, "phase_b_extra_ticks", None)
            txt = f"phase: {conv_phase}"
            if extra is not None:
                txt += f"  ·  +{extra} ticks"
            clean = getattr(conv, "consecutive_clean_ticks", None)
            if clean is not None:
                txt += f"  ·  clean×{clean}"
            phase.update(txt)
            phase.add_class(_phase_class(conv_phase))

        # ---- PLAN done/total bar --------------------------------------------
        done, total, plan_steps = (plan or (0, 0, []))
        if total > 0:
            plan_head.display = True
            bar.display = True
            plan_head.update(f"PLAN: {done}/{total} steps")
            bar.update(total=total, progress=done)
        else:
            # no plan checklist -> hide the bar, show an honest note.
            plan_head.display = True
            bar.display = False
            plan_head.update("PLAN: no checklist (non-implement run or no PLAN.md)")

        # ---- bugs summary line ----------------------------------------------
        bug_txt = f"bugs: {bugs_total} open"
        bugs.remove_class("state-alert")
        if bugs_blocking:
            bug_txt += f"  ·  {bugs_blocking} blocking"
            bugs.add_class("state-alert")
        bugs.update(bug_txt)

        # ---- step list -------------------------------------------------------
        steps_body.remove_children()
        plan_steps = [s for s in (plan_steps or []) if isinstance(s, PlanStep)]
        if not plan_steps:
            steps_body.mount(Label(
                "no PLAN steps — run has no checklist", classes="empty-state"))
            return
        for s in plan_steps:
            glyph = "[x]" if s.done else "[ ]"
            row = Label(f"{glyph} {s.text}")
            row.add_class("plan-step")
            row.add_class("state-pass" if s.done else "state-pending")
            steps_body.mount(row)
