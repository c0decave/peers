"""Launch wizard (Unit I): create + start a project — the write surface.

``LaunchWizardScreen`` is a :class:`~textual.screen.ModalScreen` that collects
the form (path/name · modes · peers · driver · lang · host/container · budget ·
plan · template), shows the operator the **exact** ``new`` + ``start`` commands
it will run, and then runs them **off the UI thread** via ``run_worker(thread=
True)`` with an **explicit cwd** (the target project dir — NEVER the inherited
TUI cwd, because ``new --plan`` runs an up-to-60s acceptance preflight in cwd).

CRITICAL design rule (the whole point of this layer): the wizard NEVER
reimplements scaffolding. It only builds argv via the existing
``actions.build_new_argv`` / ``build_start_argv`` and runs the REAL verbs via
``actions.run_verb`` — so every substrate guard / hash-chain / contract-freeze
stays authoritative.

On open it runs ``actions.doctor_preflight(config_dir)`` and shows an OK/WARN/
MISS summary; if host ``peers``/podman is missing it disables the relevant
host/container choice and (when nothing is launchable) the launch button.
"""
from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    LoadingIndicator,
    Select,
    SelectionList,
    Static,
    Switch,
)
from textual.widgets.selection_list import Selection

from peers_ctl.tui import actions
from peers_ctl.tui.screens.wizard_support import (
    DEFAULT_MODES,
    HostCapabilities,
    doctor_capabilities,
    parse_modes_list,
)

#: a generous timeout for the ``new`` verb — covers the up-to-60s ``--plan``
#: acceptance preflight + git clone for ``--template`` + scaffold overhead
#: (run_verb docstring note: pass >= ~90s for the new --plan path).
_NEW_TIMEOUT_S = 120.0
#: a wider timeout for the ``new`` verb when ``--template`` (a cold repo clone,
#: e.g. internal testing) and/or ``--container`` (image bring-up) is selected — a cold
#: clone + the 60s preflight can blow past 120s.
_NEW_TEMPLATE_TIMEOUT_S = 300.0
#: ``start`` is detached + returns quickly; a modest timeout is enough.
_START_TIMEOUT_S = 60.0

#: driver / lang / template choices for the Select controls.
_DRIVERS = ("orchestrator", "hooks", "sessions")
_LANGS = ("python", "js", "rust", "go")
_TEMPLATES = ("(none)", "internal testing")


class LaunchWizardScreen(ModalScreen):
    """Modal form to ``new`` + ``start`` a project (the launch write surface)."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, *, config_dir: Path | str | None = None) -> None:
        super().__init__()
        self._config_dir = str(config_dir) if config_dir is not None else None
        #: doctor-derived host capabilities (set on mount).
        self._caps: HostCapabilities | None = None
        #: whether a launch is currently in flight (debounce + UI lock).
        self._launching = False

    # ------------------------------------------------------------------ #
    # layout                                                             #
    # ------------------------------------------------------------------ #
    def compose(self) -> ComposeResult:
        with VerticalScroll(id="wizard-dialog"):
            yield Static("Launch a run — new + start", id="wizard-title")
            yield Static("running preflight…", id="wizard-doctor",
                         classes="muted")

            yield Label("project path", classes="wizard-label")
            yield Input(placeholder="/path/to/project", id="wiz-path")

            yield Label("modes (space toggles)", classes="wizard-label")
            yield SelectionList(id="wiz-modes")

            # NOTE: peers/models are configured in ``.peers/config.yaml`` (there
            # is no ``--peers`` flag); this is a non-interactive reminder, not a
            # form field, so the write surface has no silent no-op control.
            yield Static("(peers/models are set in .peers/config.yaml)",
                         id="wiz-peers-note", classes="muted")

            with Horizontal(classes="wizard-row"):
                with Vertical(classes="wizard-col"):
                    yield Label("driver", classes="wizard-label")
                    yield Select(
                        [(d, d) for d in _DRIVERS], id="wiz-driver",
                        value="orchestrator", allow_blank=False)
                with Vertical(classes="wizard-col"):
                    yield Label("lang", classes="wizard-label")
                    yield Select(
                        [(label, label) for label in _LANGS], id="wiz-lang",
                        value="python", allow_blank=False)

            with Horizontal(classes="wizard-row"):
                with Vertical(classes="wizard-col"):
                    yield Label("template", classes="wizard-label")
                    yield Select(
                        [(t, t) for t in _TEMPLATES], id="wiz-template",
                        value="(none)", allow_blank=False)
                with Vertical(classes="wizard-col"):
                    yield Label("host  ↔  container", classes="wizard-label")
                    yield Switch(value=False, id="wiz-container")

            yield Label("plan path (implement)", classes="wizard-label")
            yield Input(placeholder="(optional) PLAN.md", id="wiz-plan")

            yield Label("budget", classes="wizard-label")
            with Horizontal(classes="wizard-row"):
                yield Input(placeholder="max-ticks", id="wiz-max-ticks",
                            classes="wizard-budget")
                yield Input(placeholder="max-runtime e.g. 4h",
                            id="wiz-max-runtime", classes="wizard-budget")
                yield Input(placeholder="max-usd", id="wiz-max-usd",
                            classes="wizard-budget")

            # the exact commands we will run (transparency) + result tail.
            yield Static("", id="wiz-cmd-preview", classes="wizard-cmd")
            yield Static("", id="wiz-result")
            yield LoadingIndicator(id="wiz-spinner")

            with Horizontal(id="wizard-buttons"):
                yield Button("Launch", id="wiz-launch", variant="success")
                yield Button("Cancel", id="wiz-cancel", variant="default")

    def on_mount(self) -> None:
        # spinner is hidden until a launch is in flight.
        self.query_one("#wiz-spinner", LoadingIndicator).display = False
        # run doctor + load the modes list off the UI thread (both shell verbs).
        self.run_worker(self._load_doctor, thread=True, name="wizard_doctor",
                        group="wizard_preflight")
        self.run_worker(self._load_modes, thread=True, name="wizard_modes",
                        group="wizard_modes")

    # ------------------------------------------------------------------ #
    # preflight: doctor + modes (off-thread reads)                        #
    # ------------------------------------------------------------------ #
    def _load_doctor(self) -> HostCapabilities:
        res = actions.doctor_preflight(self._config_dir)
        return doctor_capabilities(res.ok, res.lines)

    def _load_modes(self) -> list[str]:
        res = actions.run_verb(
            actions._base(self._config_dir) + ["modes", "list"], timeout=30.0)
        parsed = parse_modes_list(res.stdout)
        return parsed or list(DEFAULT_MODES)

    def on_worker_state_changed(self, event) -> None:
        from textual.worker import WorkerState
        worker = event.worker
        if worker.state is not WorkerState.SUCCESS:
            if worker.state is WorkerState.ERROR:
                if worker.group == "wizard_modes":
                    self._apply_modes(list(DEFAULT_MODES))
                elif worker.group == "wizard_preflight":
                    self._apply_caps(HostCapabilities(
                        ok=False, peers_present=True, podman_present=True,
                        summary="doctor: preflight failed"))
                elif worker.group == "wizard_launch":
                    self._on_launch_done(None)
            return
        if worker.group == "wizard_preflight" and isinstance(worker.result, HostCapabilities):
            self._apply_caps(worker.result)
        elif worker.group == "wizard_modes" and isinstance(worker.result, list):
            self._apply_modes(worker.result)
        elif worker.group == "wizard_launch":
            self._on_launch_done(worker.result)

    def _apply_modes(self, modes: list[str]) -> None:
        try:
            sel = self.query_one("#wiz-modes", SelectionList)
        except Exception:
            return
        sel.clear_options()
        sel.add_options([Selection(m, m) for m in modes])

    def _apply_caps(self, caps: HostCapabilities) -> None:
        self._caps = caps
        try:
            label = self.query_one("#wizard-doctor", Static)
        except Exception:
            return
        flag = "OK" if caps.ok else "WARN"
        host = "host✓" if caps.peers_present else "host✗(peers missing)"
        cont = "container✓" if caps.podman_present else "container✗(podman missing)"
        label.update(f"[{flag}] {caps.summary} · {host} · {cont}")
        label.set_class(not caps.ok, "state-degraded")
        label.set_class(caps.ok, "state-pass")
        self._gate_container_switch()
        self._gate_launch_button()

    def _gate_container_switch(self) -> None:
        """Disable the container switch when podman is missing; force host when
        peers (host) is missing."""
        caps = self._caps
        try:
            switch = self.query_one("#wiz-container", Switch)
        except Exception:
            return
        if caps is None:
            return
        if not caps.podman_present:
            switch.value = False
            switch.disabled = True
        elif not caps.peers_present:
            # host peers missing -> force the container path on.
            switch.value = True
            switch.disabled = True
        else:
            switch.disabled = False

    def _gate_launch_button(self) -> None:
        """Disable Launch when NEITHER host nor container is available, or a
        launch is already in flight."""
        caps = self._caps
        try:
            btn = self.query_one("#wiz-launch", Button)
        except Exception:
            return
        launchable = True
        if caps is not None and not caps.peers_present and not caps.podman_present:
            launchable = False
        btn.disabled = self._launching or not launchable

    # ------------------------------------------------------------------ #
    # form -> argv                                                        #
    # ------------------------------------------------------------------ #
    def _selected_modes(self) -> list[str]:
        try:
            return list(self.query_one("#wiz-modes", SelectionList).selected)
        except Exception:
            return []

    def _input(self, wid: str) -> str:
        try:
            return self.query_one(wid, Input).value.strip()
        except Exception:
            return ""

    def _select(self, wid: str) -> str | None:
        try:
            val = self.query_one(wid, Select).value
        except Exception:
            return None
        if val is None or val == Select.BLANK:
            return None
        return str(val)

    def _opt_int(self, wid: str) -> int | None:
        raw = self._input(wid)
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def _opt_float(self, wid: str) -> float | None:
        raw = self._input(wid)
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    @staticmethod
    def _resolve_target(raw: str) -> str:
        """Resolve the form's path to an ABSOLUTE target, mirroring how
        ``peers-ctl new`` itself resolves its positional arg.

        A bare name (no separator) → ``$PEERS_PROJECTS_ROOT/<name>`` (default
        ``~/c0de/peers-c0de/<name>``); an absolute / ``/``-containing path is
        used verbatim (expanded + resolved). This guarantees the derived cwd is
        absolute and never ``.``/the inherited TUI cwd, and that the path the
        verb receives AGREES with that cwd (so ``new`` creates what we expect)."""
        if not raw:
            return raw
        try:
            # mirror cmd_new's bare-name resolution (cli.expand_project_arg).
            from peers_ctl.cli import expand_project_arg
            return str(expand_project_arg(Path(raw)))
        except Exception:
            # defensive fallback: still guarantee an absolute path (never ``.``).
            return str(Path(raw).expanduser().resolve())

    def build_argvs(self) -> tuple[list[str], list[str], str]:
        """Build the (new_argv, start_argv, project_name) from the form.

        Pure: only assembles argv via the action builders, no side effects."""
        raw = self._input("#wiz-path")
        path = self._resolve_target(raw)
        name = Path(path).name if path else ""
        container = bool(self._safe_switch("#wiz-container"))
        template = self._select("#wiz-template")
        if template == "(none)":
            template = None
        plan = self._input("#wiz-plan") or None
        new_argv = actions.build_new_argv(
            path=path,
            modes=self._selected_modes() or None,
            driver=self._select("#wiz-driver"),
            container=container,
            lang=self._select("#wiz-lang"),
            plan=plan,
            template=template,
            config_dir=self._config_dir,
        )
        start_argv = actions.build_start_argv(
            name,
            max_ticks=self._opt_int("#wiz-max-ticks"),
            max_usd=self._opt_float("#wiz-max-usd"),
            max_runtime=self._input("#wiz-max-runtime") or None,
            container=container,
            config_dir=self._config_dir,
        )
        return new_argv, start_argv, name

    def _safe_switch(self, wid: str) -> bool:
        try:
            return bool(self.query_one(wid, Switch).value)
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # launch (off-thread, explicit cwd)                                   #
    # ------------------------------------------------------------------ #
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "wiz-cancel":
            self.action_cancel()
        elif event.button.id == "wiz-launch":
            self._launch()

    def _launch(self) -> None:
        if self._launching:
            return
        if not self._input("#wiz-path"):
            self.query_one("#wiz-result", Static).update(
                "✗ a project path is required")
            return
        new_argv, start_argv, name = self.build_argvs()
        # the ABSOLUTE target — already resolved (mirrors cmd_new); the argv
        # path AND the derived cwd both come from this single source of truth.
        abs_target = self._resolve_target(self._input("#wiz-path"))
        container = bool(self._safe_switch("#wiz-container"))
        template = self._select("#wiz-template")
        templated = bool(template) and template != "(none)"
        # show the EXACT commands we are about to run (transparency).
        preview = (
            "$ " + " ".join(new_argv) + "\n"
            "$ " + " ".join(start_argv)
        )
        self.query_one("#wiz-cmd-preview", Static).update(preview)
        self.query_one("#wiz-result", Static).update("launching…")
        self._launching = True
        self.query_one("#wiz-spinner", LoadingIndicator).display = True
        self._gate_launch_button()
        # CRITICAL: an EXPLICIT cwd (the ABSOLUTE target project dir / its
        # parent), NEVER the inherited TUI cwd — new --plan runs an up-to-60s
        # acceptance preflight in cwd.
        cwd = self._target_cwd(abs_target)
        # a cold ``--template`` clone (and/or container bring-up) + the 60s
        # preflight can exceed the default 120s; widen the new timeout then.
        new_timeout = (_NEW_TEMPLATE_TIMEOUT_S
                       if (templated or container) else _NEW_TIMEOUT_S)

        def _run() -> actions.VerbResult:
            new_res = actions.run_verb(new_argv, cwd=cwd, timeout=new_timeout)
            if new_res.rc != 0:
                return new_res  # don't start if scaffolding failed
            return actions.run_verb(start_argv, cwd=cwd, timeout=_START_TIMEOUT_S)

        self.run_worker(_run, thread=True, name="wizard_launch",
                        group="wizard_launch")

    @staticmethod
    def _target_cwd(path: str) -> str:
        """The explicit cwd for ``new``/``start``: the project dir if it exists,
        else its parent (so ``new`` can create it), else ``path`` verbatim.

        Callers pass an already-ABSOLUTE target (see ``_resolve_target``); this
        helper additionally makes any stray relative input absolute so it can
        NEVER return ``.``/the inherited TUI cwd (defense in depth — that cwd is
        where ``new --plan`` runs an up-to-60s acceptance preflight)."""
        p = Path(path)
        if not p.is_absolute():
            p = p.expanduser().resolve()
        if p.is_dir():
            return str(p)
        parent = p.parent
        if str(parent) and parent.is_dir():
            return str(parent)
        return str(p)

    def _on_launch_done(self, result) -> None:
        self._launching = False
        try:
            self.query_one("#wiz-spinner", LoadingIndicator).display = False
        except Exception:
            pass
        self._gate_launch_button()
        try:
            out = self.query_one("#wiz-result", Static)
        except Exception:
            return
        if result is None:
            out.update("✗ launch failed (worker error)")
            out.set_class(True, "state-fail")
            return
        rc = getattr(result, "rc", -1)
        tail = (getattr(result, "stdout", "") or "")[-800:]
        err = (getattr(result, "stderr", "") or "")[-800:]
        ok = rc == 0
        head = "✓ launched" if ok else f"✗ failed (rc={rc})"
        body = "\n".join(p for p in (tail, err) if p.strip())
        out.update(f"{head}\n{body}".rstrip())
        out.set_class(ok, "state-pass")
        out.set_class(not ok, "state-fail")

    # ------------------------------------------------------------------ #
    # dismiss                                                            #
    # ------------------------------------------------------------------ #
    def action_cancel(self) -> None:
        if self._launching:
            return  # don't drop a screen with a launch in flight
        self.dismiss(None)
