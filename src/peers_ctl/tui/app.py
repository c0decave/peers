"""The runnable Textual cockpit (Wave 1b, Units F + G).

``run(config_dir)`` (the entrypoint ``cmd_tui`` already calls) builds and runs
:class:`PeersTuiApp`. The app is a master-detail shell: a permanent **Fleet
sidebar** (left) + a tiled **cockpit** of the loop-layer panels (Gates, Peers,
Tasks/Steps, Tick-Verlauf, Budget&Health visible by default; Bugs, Konsens/Review,
Log, Diff start hidden and toggle on).

Data flow (the :class:`DataHub` mixed into the app):
  * a slow timer (~3s) re-reads ``reader.fleet_entries(config_dir=...)`` and
    repaints the Fleet sidebar;
  * a fast timer (~1s) re-reads the *selected* run's ``reader.run_snapshot(...)``
    plus the cheap loop-layer readers (``plan_progress``, ``tick_entries``,
    ``bug_views``, ``commit_review_view``, ``log_lines``) in a **thread worker**
    (so a slow disk read can't block the UI) and posts a :class:`SnapshotReady`
    message that repaints the cockpit;
  * highlighting a fleet row switches the active run and triggers an immediate
    snapshot read.

Window toggling + pop-out (the chosen mechanism — see the design note below):

  * **Toggling** is handled by :class:`PanelBar`, a small custom taskbar I dock at
    the bottom. It lists every cockpit panel; clicking a button (or pressing its
    number key ``1``-``9``) shows/hides that *tiled* panel in place. The default
    mission-control set (Gates, Peers, Tasks, Ticks, Budget) starts visible; the
    new windows (Bugs, Review, Log, Diff) start hidden.
  * **Pop-out** uses the REAL ``textual_window`` 0.8 API. ``o``/``space`` pops the
    focused tiled panel into a floating ``textual_window.Window`` (drag/resize/
    maximize/snap): I mount a *fresh* panel instance of the same type inside a
    ``Window`` and repaint it from the last snapshot, then hide the tiled twin.
    ``x`` closes the focused floating window (``Window.remove_window()``), which
    re-shows the tiled twin. ``f1`` opens ``textual_window.WindowSwitcher`` (alt-
    tab over floating windows). I chose **native-tiled panels + Window pop-out
    wrappers** (not "every panel is a Window") because the tiled mission-control
    layout is the default and most-used view; Windows are an on-demand overlay.

Real-API notes (verified against the installed textual-window 0.8 + a spike):
  * ``Window(*children, id=..., mode='temporary', start_open=True, ...)`` auto-
    registers with the singleton window manager on ``__init__`` and auto-adds a
    button to a mounted ``textual_window.WindowBar`` on its ``_dom_ready``.
  * A ``Window`` REQUIRES ``min_width``/``min_height`` to be set (it raises a
    ``ValueError`` in ``_calculate_all_sizes`` otherwise) — set here via CSS on
    ``.popout-window`` (``min-width``/``min-height``).
  * ``Window.remove_window()`` removes it from the DOM AND unregisters it from the
    manager + its WindowBar button. ``app.query(Window)`` is the DOM-scoped truth
    (the manager is a process singleton, so never assert on it across pilots).

All reads go through the Wave-1a ``reader`` (fail-soft, no reconcile); the UI
never touches the filesystem itself and never crashes on missing data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Footer, Header, Static
from textual.worker import Worker, WorkerState
from textual_window import Window, WindowBar, WindowSwitcher

from peers_ctl.tui import layout as layout_mod
from peers_ctl.tui import reader
from peers_ctl.tui.actions import (
    StreamHandle,
    build_peek_argv,
    decode_stream_line,
    stream_verb,
)
from peers_ctl.tui.panels import (
    AutonomyFeedPanel,
    AutonomyLedgerPanel,
    BudgetPanel,
    BugsPanel,
    DiffPanel,
    EscalationBannerPanel,
    FleetPanel,
    GatesPanel,
    LivePanel,
    LogPanel,
    PeersPanel,
    PropagationsPanel,
    ReviewPanel,
    SpineGatesPanel,
    TasksPanel,
    TicksPanel,
)
from peers_ctl.tui.screens import (
    AckBlockScreen,
    AmendScreen,
    HelpScreen,
    LaunchWizardScreen,
    ResumeScreen,
    StopScreen,
)
from peers_ctl.tui.snapshots import (
    AutonomyLedgerView,
    BugView,
    CommitReviewRow,
    FleetEntry,
    GateSnapshotRow,
    LogRow,
    PlanStep,
    RunSnapshot,
    SpineRunEntry,
    TickEntry,
)

#: adaptive polling cadence (seconds): the fleet is cheap+slow, the selected run
#: refreshes fast. Module constants so tests can reason about / monkeypatch them.
FLEET_INTERVAL_S = 3.0
RUN_INTERVAL_S = 1.0

#: how many commits the Konsens/Review window pulls per refresh.
_REVIEW_LIMIT = 20


@dataclass(frozen=True)
class LoopData:
    """The bundle of cheap loop-layer reads the worker gathers for one run.

    Composed off the UI thread so a slow disk read can't block painting. Every
    field has a safe default so a partial/failed read still paints something."""
    snapshot: RunSnapshot
    plan: tuple[int, int, list[PlanStep]] = (0, 0, [])
    ticks: list[TickEntry] = field(default_factory=list)
    #: per-tick gate-stand snapshots (Wave-2 §5.2) for the history scrubber.
    gate_history: list[GateSnapshotRow] = field(default_factory=list)
    bugs: list[BugView] = field(default_factory=list)
    blocking: int = 0
    reviews: list[CommitReviewRow] = field(default_factory=list)
    log: list[LogRow] = field(default_factory=list)
    # ---- autonomy / agentic-os layer (forward-looking; empty today) ------- #
    #: re-derived autonomy ledger view (verified None + no events == no ledger).
    autonomy: AutonomyLedgerView | None = None
    #: spine-run registry entries (empty until the Wave-2 registry exists).
    spine_runs: list[SpineRunEntry] = field(default_factory=list)
    #: escalation markers ({halted, concerns, halted_excerpt}); quiet by default.
    escalation: dict = field(default_factory=dict)


#: Each toggleable cockpit panel: (panel id, number key, factory). Order is the
#: PanelBar + number-key order. ``visible_default`` marks the mission-control set.
@dataclass(frozen=True)
class PanelSpec:
    pid: str
    key: str
    title: str
    factory: type
    visible_default: bool


PANEL_SPECS: tuple[PanelSpec, ...] = (
    PanelSpec("gates-panel", "1", "Gates", GatesPanel, True),
    PanelSpec("peers-panel", "2", "Peers", PeersPanel, True),
    PanelSpec("tasks-panel", "3", "Tasks", TasksPanel, True),
    PanelSpec("ticks-panel", "4", "Ticks", TicksPanel, True),
    PanelSpec("budget-panel", "5", "Budget", BudgetPanel, True),
    PanelSpec("bugs-panel", "6", "Bugs", BugsPanel, False),
    PanelSpec("review-panel", "7", "Konsens", ReviewPanel, False),
    PanelSpec("log-panel", "8", "Log", LogPanel, False),
    PanelSpec("diff-panel", "9", "Diff", DiffPanel, False),
    # The Live-Stream panel is toggled/focused with `p` (not a number key); it is
    # in the cockpit + PanelBar like the rest and starts visible (mission set).
    PanelSpec("live-panel", "p", "Live", LivePanel, True),
    # ---- forward-looking autonomy / agentic-os windows (hidden by default) - #
    # No number key (1-9 are taken by the loop layer): these toggle via their
    # PanelBar button + the function-key bindings f5-f9. They render an honest
    # empty-state until the agentic-os spine becomes operator-runnable.
    PanelSpec("autonomy-ledger-panel", "f5", "Ledger", AutonomyLedgerPanel, False),
    PanelSpec("spine-gates-panel", "f6", "Spine-Gates", SpineGatesPanel, False),
    PanelSpec("propagations-panel", "f7", "Propagations", PropagationsPanel, False),
    PanelSpec("autonomy-feed-panel", "f8", "Auto-Feed", AutonomyFeedPanel, False),
    PanelSpec("escalation-panel", "f9", "Eskalation", EscalationBannerPanel, False),
)

#: the panel ids of the 5 forward-looking autonomy windows (for paint + restore).
_AUTONOMY_PIDS = (
    "autonomy-ledger-panel", "spine-gates-panel", "propagations-panel",
    "autonomy-feed-panel", "escalation-panel",
)

#: id of the Live-Stream panel (used by the streaming lifecycle).
_LIVE_PID = "live-panel"

#: how often the Live window pumps queued stream lines into the panel + ticks the
#: idle-timer. Cheap (just drains an in-memory queue), so faster than the snapshot.
_LIVE_PUMP_INTERVAL_S = 0.4

#: how many decoded lines we drain from the stream per pump (bounds UI work).
_LIVE_PUMP_BATCH = 200

#: backoff before a SAME-KEY relaunch of an EOF-prone live source (the legacy
#: claude ``peek`` subprocess can EOF on its own; ``tail -F`` never does). Without
#: this, an EOF'd ``peek`` would be re-spawned on every ~1s re-evaluate (churn).
_LIVE_EOF_RELAUNCH_BACKOFF_S = 3.0


class SnapshotReady(Message):
    """Posted from the snapshot thread worker with a fresh LoopData bundle."""

    def __init__(self, data: LoopData) -> None:
        self.data = data
        super().__init__()


class PanelBar(Static):
    """A small custom taskbar that toggles the tiled cockpit panels.

    Distinct from ``textual_window.WindowBar`` (which manages *floating* windows):
    this one shows/hides the *tiled* panels in place. Each button carries the
    panel id; clicking it (or pressing its number key) toggles that panel."""

    class PanelToggled(Message):
        def __init__(self, pid: str) -> None:
            self.pid = pid
            super().__init__()

    def compose(self) -> ComposeResult:
        with Horizontal(id="panelbar-row"):
            for spec in PANEL_SPECS:
                yield Button(
                    f"{spec.key} {spec.title}",
                    id=f"toggle-{spec.pid}",
                    classes="panelbar-btn",
                )

    def set_active(self, pid: str, active: bool) -> None:
        try:
            btn = self.query_one(f"#toggle-{pid}", Button)
        except Exception:
            return
        btn.set_class(active, "panelbar-active")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid.startswith("toggle-"):
            self.post_message(self.PanelToggled(bid[len("toggle-"):]))


class PeersTuiApp(App):
    """The peers-ctl mission-control cockpit."""

    CSS_PATH = "app.tcss"
    TITLE = "peers-ctl"
    SUB_TITLE = "mission control"

    BINDINGS = [
        # navigate
        ("down,j", "fleet_down", "Next"),
        ("up,k", "fleet_up", "Prev"),
        ("right,l,tab", "focus_next_panel", "Panel →"),
        ("left,h,shift+tab", "focus_prev_panel", "Panel ←"),
        ("enter", "drill", "Open"),
        # focus a window
        ("p", "live", "Live"),
        ("g", "focus_gates", "Gates"),
        # gate-history scrubber (Wave-2 §5.2): step the Gates window back/forward
        # through past ticks; backslash returns to the live view.
        ("left_square_bracket", "gate_scrub_back", "Gate ⏪"),
        ("right_square_bracket", "gate_scrub_fwd", "Gate ⏩"),
        ("backslash", "gate_scrub_live", "Gate live"),
        ("t", "focus_tasks", "Tasks"),
        ("b", "focus_bugs", "Bugs"),
        ("d", "focus_diff", "Diff"),
        # intervention actions (wired in Unit I; placeholders now)
        ("n", "new_run", "New"),
        ("s", "stop_run", "Stop"),
        ("r", "resume_run", "Resume"),
        ("a", "ack_block", "Ack"),
        ("m", "amend_run", "Amend"),
        # windows
        ("o,space", "popout", "Pop out"),
        ("x", "close_window", "Close win"),
        ("f1", "window_switcher", "Switch win"),
        ("1", "toggle_1", "Gates"),
        ("2", "toggle_2", "Peers"),
        ("3", "toggle_3", "Tasks"),
        ("4", "toggle_4", "Ticks"),
        ("5", "toggle_5", "Budget"),
        ("6", "toggle_6", "Bugs"),
        ("7", "toggle_7", "Konsens"),
        ("8", "toggle_8", "Log"),
        ("9", "toggle_9", "Diff"),
        # autonomy / agentic-os windows (forward-looking, empty-state today)
        ("f5", "toggle_autonomy_ledger", "Ledger"),
        ("f6", "toggle_spine_gates", "Spine-Gates"),
        ("f7", "toggle_propagations", "Propagations"),
        ("f8", "toggle_autonomy_feed", "Auto-Feed"),
        ("f9", "toggle_escalation", "Eskalation"),
        # general
        ("question_mark", "help", "Help"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, config_dir: Path | str | None = None) -> None:
        super().__init__()
        self.config_dir: Path | None = Path(config_dir) if config_dir is not None else None
        #: the active run (project name+path) the cockpit is showing.
        self._active_name: str | None = None
        self._active_path: str | None = None
        #: the last LoopData painted (so a pop-out / Diff drive can repaint).
        self._last_data: LoopData | None = None
        #: the sha currently shown in the Diff panel (driven by tick selection).
        self._diff_sha: str | None = None
        #: the sha the Diff panel was last asked to render (dedup git forks).
        self._diff_rendered_sha: str | None = None
        # ---- Live-Stream lifecycle (Unit H) -------------------------------- #
        #: the live streaming subprocess (None when no stream is running). Always
        #: killed on close/run-switch so it can never leak.
        self._stream: StreamHandle | None = None
        #: (name, peer, tool, live) the current stream was started for — so a
        #: re-evaluate is a no-op when nothing changed.
        self._stream_key: tuple[str, str, str, bool] | None = None
        #: the peer/tool the live stream is decoding (for line decoding).
        self._stream_tool: str = "claude"
        #: monotonic time of the last streamed line (for the idle-timer).
        self._stream_last_line_at: float | None = None
        #: monotonic time the current key's source last EOF'd (relaunch backoff).
        #: Set when a same-key EOF'd source is observed; gates the relaunch so an
        #: EOF-prone ``peek`` is not re-spawned on every ~1s re-evaluate.
        self._stream_eof_at: float | None = None
        #: a small overridable hook so tests can inject a fake stream source
        #: (argv, cwd, live) without a real peers-ctl/peek dependency.
        self._stream_source_factory = None
        # ---- layout persistence (Unit J) ----------------------------------- #
        #: where the cockpit layout (visible panels + geometry) is persisted.
        #: Resolved from the same config dir the rest of peers_ctl uses.
        self._layout_path = layout_mod.default_layout_path(config_dir=self.config_dir)
        #: the layout loaded at mount (fail-soft -> the default on anything bad).
        self._layout: dict = layout_mod.default_layout()

    # ------------------------------------------------------------------ #
    # layout                                                             #
    # ------------------------------------------------------------------ #
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="shell"):
            yield FleetPanel(id="fleet-sidebar")
            with Vertical(id="cockpit"):
                for spec in PANEL_SPECS:
                    panel = spec.factory(id=spec.pid, classes="panel")
                    panel.display = spec.visible_default
                    yield panel
        # floating-window plumbing (textual_window): a taskbar for floating
        # windows + the alt-tab switcher. Both auto-wire via the singleton mgr.
        yield WindowBar(start_open=True)
        yield WindowSwitcher(cycle_key="f1")
        yield PanelBar(id="panelbar")
        yield Footer()

    def on_mount(self) -> None:
        # restore the persisted layout (which panels are visible) BEFORE the
        # first paint. Fail-soft: a bad layout file degraded to the default in
        # load_layout, so this never blocks launch.
        self._restore_layout()
        # paint once immediately so the first frame is populated, then poll.
        self._sync_panelbar()
        self._refresh_fleet()
        self.set_interval(FLEET_INTERVAL_S, self._refresh_fleet)
        self.set_interval(RUN_INTERVAL_S, self._refresh_run)
        # Live-Stream: pump queued lines into the panel + tick the idle-timer.
        self.set_interval(_LIVE_PUMP_INTERVAL_S, self._pump_live)
        self._evaluate_live_stream()

    def on_unmount(self) -> None:
        # persist the layout on teardown (fail-soft) so the next launch restores
        # the operator's arrangement, then never leak the streaming subprocess.
        self._save_layout()
        self._stop_stream()

    # ------------------------------------------------------------------ #
    # layout persistence (Unit J)                                         #
    # ------------------------------------------------------------------ #
    def _restore_layout(self) -> None:
        """Apply the persisted visible-panel set. Fail-soft (never blocks launch).

        Geometry restore is best-effort/forward-looking — the tiled cockpit has
        no per-panel geometry yet, so we restore visibility (the load-bearing
        bit) and keep the geometry record for when floating windows persist."""
        try:
            self._layout = layout_mod.load_layout(self._layout_path)
        except Exception:
            self._layout = layout_mod.default_layout()
        visible = self._layout.get("visible", {})
        if not isinstance(visible, dict):
            return
        for spec in PANEL_SPECS:
            want = visible.get(spec.pid)
            if not isinstance(want, bool):
                continue
            try:
                panel = self.query_one(f"#{spec.pid}")
            except Exception:
                continue
            if panel.display != want:
                panel.display = want
                # showing/hiding the Live panel must (re)evaluate its stream.
                if spec.pid == _LIVE_PID:
                    self._evaluate_live_stream()

    def _capture_layout(self) -> dict:
        """Snapshot the current visible-panel set into a layout dict.

        Starts from the last-known layout (``self._layout``) and overlays only the
        panels we can actually query *right now*. This is load-bearing on teardown:
        during ``on_unmount`` the panels may already be removed, so a from-scratch
        capture would lose every toggle and reset to defaults — starting from the
        retained record preserves the operator's arrangement instead."""
        prev = self._layout.get("visible", {})
        visible: dict[str, bool] = dict(prev) if isinstance(prev, dict) else {}
        for spec in PANEL_SPECS:
            try:
                visible[spec.pid] = bool(self.query_one(f"#{spec.pid}").display)
            except Exception:
                continue  # panel gone (teardown) -> keep the retained value
        return {"visible": visible, "windows": {}}

    def _save_layout(self) -> None:
        """Persist the current layout. Fail-soft: a write failure never crashes."""
        try:
            captured = self._capture_layout()
            # keep the in-memory record current so a later (teardown) capture
            # starts from the freshest known state, not a stale one.
            self._layout = captured
            layout_mod.save_layout(self._layout_path, captured)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # DataHub: polling + worker reads                                     #
    # ------------------------------------------------------------------ #
    def _refresh_fleet(self) -> None:
        """Slow timer: re-read the registry and repaint the sidebar. Fail-soft."""
        try:
            entries = reader.fleet_entries(config_dir=self.config_dir)
        except Exception:
            entries = []
        try:
            panel = self.query_one(FleetPanel)
        except Exception:
            return
        panel.render_entries(entries)
        if self._active_name is None:
            sel = panel.selected_entry()
            if sel is not None:
                self._set_active(sel)
                self._refresh_run()

    def _refresh_run(self) -> None:
        """Fast timer: read the active run's loop-layer data OFF the UI thread."""
        name, path = self._active_name, self._active_path
        if not name or not path:
            return

        def _read() -> LoopData:
            return _read_loop_data(Path(path), str(name))

        self.run_worker(_read, thread=True, exclusive=True, group="snapshot",
                        name="run_snapshot")

    def on_snapshot_ready(self, message: SnapshotReady) -> None:
        # Guard against painting a stale snapshot: only paint if it still matches
        # the active run (the user may have switched away).
        if message.data.snapshot.name != self._active_name:
            return  # stale: a newer run was selected
        self._last_data = message.data
        self._paint_cockpit(message.data)
        # the active peer may have rotated this tick -> re-evaluate the stream.
        self._evaluate_live_stream()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        worker = event.worker
        if worker.state is not WorkerState.SUCCESS:
            return
        if worker.group == "snapshot" and isinstance(worker.result, LoopData):
            self.post_message(SnapshotReady(worker.result))
        elif worker.group == "diff" and isinstance(worker.result, tuple):
            diff_text, sha = worker.result
            # only paint if the result is still for the selected sha (not stale).
            if sha == self._diff_sha:
                self._paint_diff(diff_text, sha)

    def _paint_diff(self, diff_text: str, sha: str | None) -> None:
        try:
            self.query_one(DiffPanel).render_diff(diff_text, sha=sha)
        except Exception:
            pass

    def _paint_cockpit(self, data: LoopData) -> None:
        """Repaint every loop-layer panel from a LoopData bundle. Empty-aware."""
        snap = data.snapshot
        present = bool(getattr(snap, "state_present", False))
        suffix = "" if present else " (no live state)"
        self._paint_panel(GatesPanel, "Gates", snap.name, suffix,
                          lambda p: p.render_gates(
                              snap.gates if present else [],
                              snap.convergence if present else None,
                              history=data.gate_history))
        self._paint_panel(PeersPanel, "Peers", snap.name, suffix,
                          lambda p: p.render_peers(
                              snap.peers if present else [],
                              snap.current_peer if present else None))
        self._paint_panel(TasksPanel, "Tasks / Steps", snap.name, suffix,
                          lambda p: p.render_tasks(
                              snap, data.plan,
                              bugs_total=len(data.bugs),
                              bugs_blocking=data.blocking))
        self._paint_panel(TicksPanel, "Tick-Verlauf", snap.name, suffix,
                          lambda p: p.render_ticks(data.ticks))
        self._paint_panel(BudgetPanel, "Budget & Health", snap.name, suffix,
                          lambda p: p.render_budget(snap.budget if present else None))
        self._paint_panel(BugsPanel, "Bugs", snap.name, suffix,
                          lambda p: p.render_bugs(data.bugs, data.blocking))
        self._paint_panel(ReviewPanel, "Konsens / Review", snap.name, suffix,
                          lambda p: p.render_reviews(data.reviews))
        self._paint_panel(LogPanel, "Log", snap.name, suffix,
                          lambda p: p.render_log(data.log))
        # ---- autonomy / agentic-os layer (forward-looking; empty today) --- #
        # These render directly from the re-derived spine readers (NOT gated on
        # the classic state.json presence — the spine ledger is independent).
        # The honesty rule is enforced in the panels: convergence/gates come
        # only from the re-derived view, never a stored independence flag.
        self._paint_autonomy(data)
        # the Diff panel: if no tick selected yet, default to the latest tick sha.
        # Only re-resolve git when the target sha actually changes (avoid a git
        # fork on every poll tick for an unchanged selection).
        if self._diff_sha is None:
            self._diff_sha = _latest_tick_sha(data.ticks)
        if self._diff_sha != self._diff_rendered_sha:
            self._repaint_diff()

    def _paint_panel(self, cls, title, name, suffix, paint) -> None:
        try:
            panel = self.query_one(cls)
        except Exception:
            return
        panel.border_title = f"{title} — {name}{suffix}"
        paint(panel)

    def _paint_autonomy(self, data: LoopData) -> None:
        """Repaint the 5 forward-looking autonomy panels from a LoopData bundle.

        Honest empty-state: with no run.jsonl / no spine-runs registry the views
        are empty and each panel renders the forward-looking placeholder. The
        honesty rule (no convergence off a stored independence flag) lives in the
        panels — here we only hand over the RE-DERIVED reader views."""
        name = data.snapshot.name
        for cls, title, paint in (
            (AutonomyLedgerPanel, "Autonomie-Ledger",
             lambda p: p.render_ledger(data.autonomy)),
            (SpineGatesPanel, "Spine-Gates",
             lambda p: p.render_gates(data.autonomy)),
            (PropagationsPanel, "Propagations-DAG",
             lambda p: p.render_dag(data.spine_runs)),
            (AutonomyFeedPanel, "Autonomie-Feed",
             lambda p: p.render_feed(data.autonomy, data.spine_runs)),
            (EscalationBannerPanel, "Eskalations-Banner",
             lambda p: p.render_escalation(data.escalation)),
        ):
            try:
                self.query_one(cls)
            except Exception:
                continue
            self._paint_panel(cls, title, name, "", paint)

    def _repaint_diff(self) -> None:
        """(Re)render the Diff panel from the current sha (reads git off-thread)."""
        path, sha = self._active_path, self._diff_sha
        # record what we are (about to be) rendering so the per-poll dedup in
        # _paint_cockpit doesn't re-fork git for an unchanged sha.
        self._diff_rendered_sha = sha
        if not path or not sha:
            try:
                self.query_one(DiffPanel).render_diff("", sha=None)
            except Exception:
                pass
            return

        def _read() -> tuple[str, str]:
            try:
                return (reader.commit_diff(Path(path), sha), sha)
            except Exception:
                return ("", sha)

        self.run_worker(_read, thread=True, exclusive=True, group="diff",
                        name="commit_diff")

    def on_ticks_panel_tick_selected(self, message: TicksPanel.TickSelected) -> None:
        """A tick row was highlighted — drive the Diff window from its sha."""
        if message.sha and message.sha != self._diff_sha:
            self._diff_sha = message.sha
            self._repaint_diff()

    # ------------------------------------------------------------------ #
    # Live-Stream lifecycle (Unit H)                                      #
    # ------------------------------------------------------------------ #
    def _live_panel(self) -> LivePanel | None:
        # prefer a popped-out Live twin (the visible instance) if one is up, so
        # decoded lines + the header paint where the operator is looking.
        for w in self.query(Window):
            if w.id == f"win-{_LIVE_PID}":
                try:
                    return w.query_one(LivePanel)
                except Exception:
                    break
        try:
            return self.query_one(f"#{_LIVE_PID}", LivePanel)
        except Exception:
            return None

    def _live_is_active(self) -> bool:
        """True iff the Live panel is visible (tiled OR popped out)."""
        try:
            if self.query_one(f"#{_LIVE_PID}").display:
                return True
        except Exception:
            pass
        for w in self.query(Window):
            if w.id == f"win-{_LIVE_PID}":
                return True
        return False

    def _live_source(self, name: str, path: str, peer: str, tool: str):
        """Return ``(argv, cwd, live)`` for streaming ``peer``'s activity.

        Source selection (documented in ``panels/live.py``):
          * **Wave-2 unified tee (preferred, all peers):** when a
            ``tick-*-<peer>.stream.jsonl`` exists (substrate run with the live
            tee on), tail IT — codex/opencode become genuinely live just like
            claude, decoded via ``decode_stream_line(tool=...)``.
          * legacy fallback when no tee file exists:
              - ``claude`` -> ``peers-ctl peek <name>`` (follows the session
                jsonl through the canonical decoder);
              - ``codex``/``opencode`` -> tail the newest completed per-tick
                stdout log (tick-level).
        A test hook (``_stream_source_factory``) can override this to inject a
        fake source without a real peers-ctl/peek dependency."""
        if self._stream_source_factory is not None:
            return self._stream_source_factory(name, path, peer, tool)
        # Prefer the Wave-2 unified live tee for ALL peers when present. It is
        # genuinely live (the substrate flushes each os.read chunk), so the
        # panel shows real-time activity for codex/opencode too. `tail -F`
        # follows the file across tick rotation.
        tee = _newest_tee_stream(Path(path), peer)
        if tee is not None:
            return (["tail", "-n", "+1", "-F", str(tee)], None, True)
        if tool == "claude":
            # legacy live: peek follows the claude session jsonl + decodes it.
            # Route through the validated builder (refuses a flag-like / non-slug
            # name) rather than splicing the name into argv directly.
            cfg = str(self.config_dir) if self.config_dir is not None else None
            peek_argv = build_peek_argv(str(name), config_dir=cfg)
            if peek_argv is None:
                return (None, None, False)
            return (peek_argv, None, True)
        # codex/opencode legacy fallback: no tee -> tail the newest completed
        # per-tick stdout log (tick-level). `tail -F` follows rotation.
        log = _newest_tick_log(Path(path), peer)
        if log is None:
            return (None, None, False)
        return (["tail", "-n", "+1", "-F", str(log)], None, False)

    def _evaluate_live_stream(self) -> None:
        """(Re)start or stop the live stream to match the active run + peer.

        Called on mount, on run-switch, on tick refresh (peer may rotate), and
        when the Live window is shown/hidden. Idempotent: a no-op when the
        (name, peer, tool, live) key is unchanged. The subprocess is ALWAYS
        stopped (killed) before a new one starts and whenever Live is inactive,
        so it can never leak."""
        panel = self._live_panel()
        if panel is None:
            return
        if not self._live_is_active():
            # Live not visible -> stop the stream (don't burn a subprocess).
            if self._stream is not None:
                self._stop_stream()
            return
        name, path = self._active_name, self._active_path
        peer = None
        data = self._last_data
        if data is not None and getattr(data.snapshot, "state_present", False):
            peer = data.snapshot.current_peer
        if not name or not path or not peer:
            self._stop_stream()
            panel.show_empty(
                "no live peer yet — select a running run with an active peer")
            return
        tool = "claude"
        try:
            tool = reader.peer_tool(Path(path), peer)
        except Exception:
            tool = "claude"
        # Resolve the source FIRST so the stream key carries the source's ACTUAL
        # liveness (`src_live`), not a tool-only guess. `_live_source` spawns
        # nothing — it just returns `(argv, cwd, src_live)`. Keying on `src_live`
        # makes the initial header paint and the pump-loop header paint agree:
        # a codex/opencode peer WITH a live tee is genuinely live, and must not
        # flip back to the Wave-2 hint on the next pump.
        argv, cwd, src_live = self._live_source(str(name), str(path), str(peer), tool)
        key = (str(name), str(peer), str(tool), bool(src_live))
        if (
            key == self._stream_key
            and self._stream is not None
            and self._stream.is_running()
        ):
            return  # nothing changed AND the handle is live; keep it
        # Key changed OR the handle EOF'd on its own (e.g. a followed `tail`
        # target rotated away, or the legacy `peek` subprocess exited) -> kill
        # any dead handle and relaunch, so the peer doesn't go permanently silent
        # until a run/peer switch.
        same_key_relaunch = key == self._stream_key
        # Relaunch backoff (Fix 4): the legacy claude `peek` subprocess can EOF
        # on its own and would otherwise be re-spawned on every ~1s re-evaluate.
        # `tail -F` never EOFs, so this only ever throttles the EOF-prone peek
        # path. On a same-key relaunch within the backoff window, leave the
        # (already-stopped) handle alone and keep the accumulated lines.
        if same_key_relaunch and self._is_eof_prone_source(argv):
            import time as _time
            now = _time.monotonic()
            if self._stream_eof_at is None:
                self._stream_eof_at = now
            elif (now - self._stream_eof_at) < _LIVE_EOF_RELAUNCH_BACKOFF_S:
                return  # within backoff -> skip the churny re-spawn
        self._stop_stream(reset_eof=False)
        # Only wipe the panel on a genuine run/peer switch. A same-key relaunch
        # (the handle EOF'd on its own) must PRESERVE the accumulated lines and
        # just re-attach a fresh follow — otherwise an EOF'd source would clear
        # the history on every refresh.
        if not same_key_relaunch:
            panel.clear()
            # genuine key change -> clear any stale EOF-backoff timestamp.
            self._stream_eof_at = None
        if argv is None:
            panel.show_empty(
                f"no live source for {peer} ({tool}) yet — "
                "tick-level stream lands in Wave 2")
            self._stream_key = key
            self._stream_tool = tool
            return
        self._stream = stream_verb(argv, cwd=cwd)
        self._stream_key = key
        self._stream_tool = tool
        self._stream_last_line_at = None
        if self._stream.error is not None:
            panel.show_empty(f"live stream unavailable: {self._stream.error}")
        self._paint_live_header(peer, tool, src_live)

    @staticmethod
    def _is_eof_prone_source(argv: list[str] | None) -> bool:
        """True iff ``argv`` is the legacy claude ``peek`` source (which can EOF
        on its own). The ``tail -F`` tee/log sources never EOF, so they are not
        throttled by the relaunch backoff."""
        return argv is not None and "peek" in argv

    def _paint_live_header(self, peer: str | None, tool: str, live: bool) -> None:
        panel = self._live_panel()
        if panel is None:
            return
        tick = None
        runtime = None
        data = self._last_data
        if data is not None and getattr(data.snapshot, "state_present", False):
            tick = getattr(data.snapshot, "iteration", None)
            budget = getattr(data.snapshot, "budget", None)
            if budget is not None:
                runtime = getattr(budget, "spent_runtime_s", None)
        idle = None
        if self._stream_last_line_at is not None:
            import time as _time
            idle = max(0.0, _time.monotonic() - self._stream_last_line_at)
        panel.render_header(peer=peer, tick=tick, runtime_s=runtime,
                            idle_s=idle, tool=tool, live=live)

    def _pump_live(self) -> None:
        """Drain queued stream lines into the Live panel + refresh the idle-timer.

        Runs on a fast timer. Reads are non-blocking (the StreamHandle buffers in
        a background thread). Decodes each raw line by the peer's tool and appends
        the colored rows. Always refreshes the header so the idle-timer ticks."""
        panel = self._live_panel()
        if panel is None:
            return
        handle = self._stream
        if handle is not None:
            import time as _time
            rows: list[tuple[str, str]] = []
            for _ in range(_LIVE_PUMP_BATCH):
                line = handle.read_line(timeout=0.0)
                if line is None:
                    break
                self._stream_last_line_at = _time.monotonic()
                rows.extend(decode_stream_line(line, tool=self._stream_tool))
            if rows:
                panel.append_rows(rows)
        # refresh the header (idle-timer ticks even with no new lines).
        if self._stream_key is not None:
            _name, peer, tool, live = self._stream_key
            self._paint_live_header(peer, tool, live)

    def _stop_stream(self, *, reset_eof: bool = True) -> None:
        """Kill the live streaming subprocess (idempotent). NEVER leaks.

        ``reset_eof`` (default True) also clears the EOF relaunch-backoff
        timestamp. The same-key relaunch path passes ``reset_eof=False`` so the
        backoff window survives the stop+relaunch and an EOF-prone ``peek`` is
        not re-spawned every ~1s."""
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self._stream_key = None
        self._stream_last_line_at = None
        if reset_eof:
            self._stream_eof_at = None

    def action_live(self) -> None:
        """`p`: focus the Live window (showing + starting its stream if hidden)."""
        try:
            panel = self.query_one(f"#{_LIVE_PID}")
        except Exception:
            return
        if not panel.display:
            self._toggle_panel(_LIVE_PID)  # show it (also re-evaluates stream)
        self._evaluate_live_stream()
        try:
            panel.focus()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # window toggling (PanelBar) + pop-out (textual_window)               #
    # ------------------------------------------------------------------ #
    def _sync_panelbar(self) -> None:
        try:
            bar = self.query_one(PanelBar)
        except Exception:
            return
        for spec in PANEL_SPECS:
            try:
                panel = self.query_one(f"#{spec.pid}")
            except Exception:
                continue
            bar.set_active(spec.pid, bool(panel.display))

    def on_panel_bar_panel_toggled(self, message: PanelBar.PanelToggled) -> None:
        self._toggle_panel(message.pid)

    def _toggle_panel(self, pid: str) -> None:
        try:
            panel = self.query_one(f"#{pid}")
        except Exception:
            return
        panel.display = not panel.display
        self._sync_panelbar()
        # showing/hiding the Live panel starts/stops its stream.
        if pid == _LIVE_PID:
            self._evaluate_live_stream()
        # persist the change immediately (belt-and-suspenders alongside on_unmount)
        # so an abrupt teardown still keeps the operator's arrangement. Fail-soft.
        self._save_layout()

    # number-key toggles map to the PANEL_SPECS order.
    def _toggle_by_key(self, key: str) -> None:
        for spec in PANEL_SPECS:
            if spec.key == key:
                self._toggle_panel(spec.pid)
                return

    def action_toggle_1(self) -> None: self._toggle_by_key("1")
    def action_toggle_2(self) -> None: self._toggle_by_key("2")
    def action_toggle_3(self) -> None: self._toggle_by_key("3")
    def action_toggle_4(self) -> None: self._toggle_by_key("4")
    def action_toggle_5(self) -> None: self._toggle_by_key("5")
    def action_toggle_6(self) -> None: self._toggle_by_key("6")
    def action_toggle_7(self) -> None: self._toggle_by_key("7")
    def action_toggle_8(self) -> None: self._toggle_by_key("8")
    def action_toggle_9(self) -> None: self._toggle_by_key("9")

    # autonomy windows (forward-looking, empty-state today).
    def action_toggle_autonomy_ledger(self) -> None:
        self._toggle_panel("autonomy-ledger-panel")

    def action_toggle_spine_gates(self) -> None:
        self._toggle_panel("spine-gates-panel")

    def action_toggle_propagations(self) -> None:
        self._toggle_panel("propagations-panel")

    def action_toggle_autonomy_feed(self) -> None:
        self._toggle_panel("autonomy-feed-panel")

    def action_toggle_escalation(self) -> None:
        self._toggle_panel("escalation-panel")

    def _focused_panel_spec(self) -> PanelSpec | None:
        """The PanelSpec of the panel containing the focused widget, if any."""
        node = self.focused
        while node is not None:
            nid = getattr(node, "id", None)
            for spec in PANEL_SPECS:
                if nid == spec.pid:
                    return spec
            node = node.parent
        return None

    def action_popout(self) -> None:
        """Pop the focused tiled panel into a floating textual_window.Window."""
        spec = self._focused_panel_spec()
        if spec is None:
            # nothing focused -> default to the first visible panel.
            for s in PANEL_SPECS:
                try:
                    if self.query_one(f"#{s.pid}").display:
                        spec = s
                        break
                except Exception:
                    continue
        if spec is None:
            return
        self.pop_out_panel(spec)

    def pop_out_panel(self, spec: PanelSpec) -> None:
        """Mount a floating Window holding a fresh twin of ``spec``'s panel."""
        win_id = f"win-{spec.pid}"
        # already popped out -> bring it forward instead of duplicating.
        for w in self.query(Window):
            if w.id == win_id:
                w.open_window()
                return
        twin = spec.factory(id=f"{spec.pid}-pop", classes="panel")
        window = Window(
            twin,
            id=win_id,
            mode="temporary",
            start_open=True,
            allow_maximize=True,
            classes="popout-window",
        )
        # hide the tiled twin while the float is up (it re-shows on close).
        try:
            self.query_one(f"#{spec.pid}").display = False
        except Exception:
            pass
        self.mount(window)
        # repaint the floating twin from the last data once it is mounted.
        self.call_after_refresh(self._repaint_popout, spec, twin)

    def _repaint_popout(self, spec: PanelSpec, twin) -> None:
        data = self._last_data
        if data is None:
            return
        snap = data.snapshot
        present = bool(getattr(snap, "state_present", False))
        try:
            if isinstance(twin, GatesPanel):
                twin.render_gates(snap.gates if present else [],
                                  snap.convergence if present else None,
                                  history=data.gate_history)
            elif isinstance(twin, PeersPanel):
                twin.render_peers(snap.peers if present else [],
                                  snap.current_peer if present else None)
            elif isinstance(twin, TasksPanel):
                twin.render_tasks(snap, data.plan, bugs_total=len(data.bugs),
                                  bugs_blocking=data.blocking)
            elif isinstance(twin, TicksPanel):
                twin.render_ticks(data.ticks)
            elif isinstance(twin, BudgetPanel):
                twin.render_budget(snap.budget if present else None)
            elif isinstance(twin, BugsPanel):
                twin.render_bugs(data.bugs, data.blocking)
            elif isinstance(twin, ReviewPanel):
                twin.render_reviews(data.reviews)
            elif isinstance(twin, LogPanel):
                twin.render_log(data.log)
            elif isinstance(twin, DiffPanel):
                twin.render_diff("", sha=self._diff_sha)
            elif isinstance(twin, AutonomyLedgerPanel):
                twin.render_ledger(data.autonomy)
            elif isinstance(twin, SpineGatesPanel):
                twin.render_gates(data.autonomy)
            elif isinstance(twin, PropagationsPanel):
                twin.render_dag(data.spine_runs)
            elif isinstance(twin, AutonomyFeedPanel):
                twin.render_feed(data.autonomy, data.spine_runs)
            elif isinstance(twin, EscalationBannerPanel):
                twin.render_escalation(data.escalation)
        except Exception:
            pass

    def on_window_closed(self, event) -> None:
        # textual_window posts a WindowClosed message when a window is removed;
        # re-show the tiled twin (best-effort across API versions).
        self._reshow_tiled_for_closed()

    def action_close_window(self) -> None:
        """Close the focused floating window (re-showing its tiled twin)."""
        node = self.focused
        target: Window | None = None
        while node is not None:
            if isinstance(node, Window):
                target = node
                break
            node = node.parent
        if target is None:
            wins = list(self.query(Window))
            if wins:
                target = wins[-1]
        if target is None:
            return
        pid = (target.id or "").removeprefix("win-")
        target.remove_window()
        self.call_after_refresh(self._reshow_tiled, pid)

    def _reshow_tiled(self, pid: str) -> None:
        try:
            self.query_one(f"#{pid}").display = True
        except Exception:
            pass
        self._sync_panelbar()

    def _reshow_tiled_for_closed(self) -> None:
        # re-show any tiled panel whose float is gone.
        open_floats = {(w.id or "").removeprefix("win-") for w in self.query(Window)}
        for spec in PANEL_SPECS:
            if spec.pid not in open_floats:
                try:
                    panel = self.query_one(f"#{spec.pid}")
                    if not panel.display and spec.visible_default:
                        panel.display = True
                except Exception:
                    continue

    def action_window_switcher(self) -> None:
        """Open the textual_window alt-tab switcher over floating windows."""
        try:
            self.query_one(WindowSwitcher).show()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # selection                                                          #
    # ------------------------------------------------------------------ #
    def _set_active(self, entry: FleetEntry) -> None:
        self._active_name = entry.name
        self._active_path = entry.path
        # reset the Diff selection when the run changes.
        self._diff_sha = None
        self._diff_rendered_sha = None
        # the run switched -> stop the old stream + clear the Live panel; a fresh
        # stream starts once the new run's snapshot (with its peer) lands.
        self._stop_stream()
        panel = self._live_panel()
        if panel is not None:
            panel.clear()
            panel.show_empty("loading live stream…")

    def on_fleet_panel_run_selected(self, message: FleetPanel.RunSelected) -> None:
        entry = message.entry
        if entry.name != self._active_name:
            self._set_active(entry)
            self._refresh_run()

    # ------------------------------------------------------------------ #
    # keybindings                                                        #
    # ------------------------------------------------------------------ #
    def action_fleet_down(self) -> None:
        self._move_fleet(1)

    def action_fleet_up(self) -> None:
        self._move_fleet(-1)

    def _move_fleet(self, delta: int) -> None:
        from textual.widgets import ListView
        try:
            lv = self.query_one("#fleet-list", ListView)
        except Exception:
            return
        idx = lv.index
        if idx is None:
            lv.index = 0
            return
        count = len(lv)
        if count:
            lv.index = max(0, min(count - 1, idx + delta))

    # ---- panel focus + cycling ---------------------------------------- #
    def _visible_panel_ids(self) -> list[str]:
        out: list[str] = []
        for spec in PANEL_SPECS:
            try:
                if self.query_one(f"#{spec.pid}").display:
                    out.append(spec.pid)
            except Exception:
                continue
        return out

    def _focus_panel(self, pid: str) -> None:
        """Focus a panel by id, showing it (+ re-evaluating Live) if hidden."""
        try:
            panel = self.query_one(f"#{pid}")
        except Exception:
            return
        if not panel.display:
            self._toggle_panel(pid)
        try:
            panel.focus()
        except Exception:
            pass

    def _cycle_panel(self, delta: int) -> None:
        vis = self._visible_panel_ids()
        if not vis:
            return
        cur = self._focused_panel_spec()
        if cur is None or cur.pid not in vis:
            self._focus_panel(vis[0])
            return
        i = (vis.index(cur.pid) + delta) % len(vis)
        self._focus_panel(vis[i])

    def action_focus_next_panel(self) -> None:
        self._cycle_panel(1)

    def action_focus_prev_panel(self) -> None:
        self._cycle_panel(-1)

    def action_focus_gates(self) -> None:
        self._focus_panel("gates-panel")

    # ---- gate-history scrubber (Wave-2 §5.2) --------------------------- #
    def _gates_panel(self) -> "GatesPanel | None":
        """The (first) live GatesPanel, or None if it isn't mounted."""
        try:
            return self.query_one(GatesPanel)
        except Exception:
            return None

    def action_gate_scrub_back(self) -> None:
        """`[`: step the Gates window one tick into the past."""
        panel = self._gates_panel()
        if panel is not None:
            panel.scrub_back()

    def action_gate_scrub_fwd(self) -> None:
        """`]`: step the Gates window one tick toward the present (past the
        newest snapshot returns to the live view)."""
        panel = self._gates_panel()
        if panel is not None:
            panel.scrub_forward()

    def action_gate_scrub_live(self) -> None:
        """`\\`: return the Gates window to the live view."""
        panel = self._gates_panel()
        if panel is not None:
            panel.scrub_live()

    def action_focus_tasks(self) -> None:
        self._focus_panel("tasks-panel")

    def action_focus_bugs(self) -> None:
        self._focus_panel("bugs-panel")

    def action_focus_diff(self) -> None:
        self._focus_panel("diff-panel")

    def action_drill(self) -> None:
        """`enter`: drill into the focused row. Currently the Fleet list selects
        the highlighted run; panel-level drill lands with the wizard/intervention
        modals in Unit I."""
        node = self.focused
        from textual.widgets import ListView
        if isinstance(node, ListView) and node.id == "fleet-list":
            sel = None
            try:
                sel = self.query_one(FleetPanel).selected_entry()
            except Exception:
                sel = None
            if sel is not None and sel.name != self._active_name:
                self._set_active(sel)
                self._refresh_run()

    # ---- help --------------------------------------------------------- #
    def action_help(self) -> None:
        """`?`: open the modal HelpScreen (cheat-sheet of all bindings)."""
        try:
            self.push_screen(HelpScreen())
        except Exception:
            pass

    # ---- launch wizard + intervention modals (Unit I) ----------------- #
    def _config_dir_str(self) -> str | None:
        return str(self.config_dir) if self.config_dir is not None else None

    def _require_active(self, verb: str) -> str | None:
        """Return the selected run's name, or surface a notice + None if none.

        The intervention verbs act on the currently-selected fleet run; without
        one there is nothing to operate on."""
        name = self._active_name
        if not name:
            try:
                self.notify(
                    f"select a run first to `{verb}` it",
                    title="no run selected", severity="warning")
            except Exception:
                pass
            return None
        return name

    def action_new_run(self) -> None:
        """`n`: open the launch wizard (create + start a project)."""
        try:
            self.push_screen(
                LaunchWizardScreen(config_dir=self.config_dir))
        except Exception:
            pass

    def action_stop_run(self) -> None:
        """`s`: stop the selected run (confirm + audit)."""
        name = self._require_active("stop")
        if name is None:
            return
        try:
            self.push_screen(
                StopScreen(project_name=name, config_dir=self.config_dir))
        except Exception:
            pass

    def action_resume_run(self) -> None:
        """`r`: resume the selected run (confirm + audit)."""
        name = self._require_active("resume")
        if name is None:
            return
        try:
            self.push_screen(
                ResumeScreen(project_name=name, config_dir=self.config_dir))
        except Exception:
            pass

    def action_ack_block(self) -> None:
        """`a`: ack-block a step on the selected run (type-to-confirm)."""
        name = self._require_active("ack-block")
        if name is None:
            return
        try:
            self.push_screen(
                AckBlockScreen(project_name=name, config_dir=self.config_dir))
        except Exception:
            pass

    def action_amend_run(self) -> None:
        """`m`: amend the acceptance gate on the selected run (type-to-confirm)."""
        name = self._require_active("amend")
        if name is None:
            return
        try:
            self.push_screen(
                AmendScreen(project_name=name, config_dir=self.config_dir))
        except Exception:
            pass

    # ---- vim keys inert while a text input is focused ----------------- #
    def check_action(self, action: str, _parameters):
        """Disable the single-letter vim/action bindings while a text input is
        focused, so typing into a wizard/intervention field never triggers an
        action. The arrow keys + function keys stay live (they don't collide)."""
        if action in _INPUT_INERT_ACTIONS and self._text_input_focused():
            return False
        return True

    def _text_input_focused(self) -> bool:
        node = self.focused
        if node is None:
            return False
        # Input / TextArea (and any subclass) capture text -> single letters
        # must reach them, not fire app actions.
        from textual.widgets import Input
        if isinstance(node, Input):
            return True
        try:
            from textual.widgets import TextArea
            if isinstance(node, TextArea):
                return True
        except Exception:
            pass
        return False


#: actions bound to a single letter / vim key that must be inert while a text
#: input is focused (so typing letters into a form never fires them). The arrow
#: keys, tab, and function keys are NOT here — they don't collide with typing.
_INPUT_INERT_ACTIONS = frozenset({
    "fleet_down", "fleet_up", "live", "focus_gates", "focus_tasks",
    "focus_bugs", "focus_diff", "new_run", "stop_run", "resume_run",
    "ack_block", "amend_run", "help", "quit",
    "gate_scrub_back", "gate_scrub_fwd", "gate_scrub_live",
    "toggle_1", "toggle_2", "toggle_3", "toggle_4", "toggle_5",
    "toggle_6", "toggle_7", "toggle_8", "toggle_9", "popout", "close_window",
})


# Live-Stream source-file resolution lives in the textual-free `reader`
# module (so the selection logic is covered in default CI); these thin
# aliases keep the call-sites here readable.
_newest_tee_stream = reader.newest_tee_stream
_newest_tick_log = reader.newest_tick_log


def _read_loop_data(project_path: Path, name: str) -> LoopData:
    """Gather the run snapshot + cheap loop-layer reads (called off-thread).

    Every read is independently fail-soft so a partial failure still paints. The
    whole call is wrapped defensively so a worker exception can never bubble."""
    peer_dir = project_path / ".peers"
    try:
        snap = reader.run_snapshot(project_path, name)
    except Exception:
        snap = RunSnapshot(
            name=str(name), state_present=False, iteration=0,
            mode=None, phase=None, current_peer=None,
        )
    try:
        plan = reader.plan_progress(project_path)
    except Exception:
        plan = (0, 0, [])
    try:
        ticks = reader.tick_entries(peer_dir / "log" / "runs.jsonl")
    except Exception:
        ticks = []
    try:
        gate_hist = reader.gate_history(peer_dir / "log" / "runs.jsonl")
    except Exception:
        gate_hist = []
    try:
        bugs = reader.bug_views(peer_dir / "bugs.jsonl")
    except Exception:
        bugs = []
    try:
        blocking = reader.blocking_open(bugs)
    except Exception:
        blocking = 0
    try:
        reviews = reader.commit_review_view(project_path, limit=_REVIEW_LIMIT)
    except Exception:
        reviews = []
    try:
        log = reader.log_lines(project_path)
    except Exception:
        log = []
    # ---- autonomy / agentic-os layer (forward-looking; empty today) ------- #
    # Every read is fail-soft and returns the honest empty-state today (no
    # run.jsonl, no spine-runs registry). The panels render the empty-state.
    try:
        autonomy = reader.autonomy_ledger_view(
            peer_dir / "run.jsonl", repo=project_path)
    except Exception:
        autonomy = None
    try:
        spine = reader.spine_runs(project_path)
    except Exception:
        spine = []
    try:
        escalation = reader.escalation_state(project_path)
    except Exception:
        escalation = {}
    return LoopData(
        snapshot=snap, plan=plan, ticks=ticks, gate_history=gate_hist,
        bugs=bugs, blocking=blocking, reviews=reviews, log=log,
        autonomy=autonomy, spine_runs=spine, escalation=escalation,
    )


def _latest_tick_sha(ticks: list[TickEntry]) -> str | None:
    """The newest non-exit tick's head_after sha (the latest run commit), or None."""
    for t in reversed(ticks or []):
        if not getattr(t, "is_exit", False) and getattr(t, "head_after", None):
            return t.head_after
    return None


def run(config_dir: Path | str | None = None) -> int:
    """Build and run the cockpit; return 0 on clean exit (cmd_tui's contract)."""
    PeersTuiApp(config_dir=config_dir).run()
    return 0
