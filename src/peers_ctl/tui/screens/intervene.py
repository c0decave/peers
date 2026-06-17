"""Mid-run intervention modals (Unit I): stop · resume · ack-block · amend.

Each modal is the audited write surface for ONE run-control verb. It (a) shows
exactly what will happen + the **exact CLI command** (the argv joined for
display), (b) requires an explicit confirm, (c) runs the REAL verb via
``actions.run_verb`` **off the UI thread**, and (d) shows the audit result
(rc/stdout/stderr — and for ``stop``, the fail-closed RuntimeError text is
surfaced, not swallowed).

The two **contract-touching** ops get an extra human gate — **type-to-confirm**:
  * ``ack-block`` (mutates PLAN.md + the ``blocks.log`` hash-chain) — the
    operator must type the exact STEP id;
  * ``amend`` (re-pins the frozen acceptance gate + the ``contracts.log``
    hash-chain) — the operator must type the project name.
The confirm button stays disabled until the typed text exactly matches, and the
modal is visually distinct (``⚠ auditiert — berührt eingefrorene Contracts``).

CRITICAL design rule: these modals NEVER reimplement write logic. They only
build argv via the existing ``actions.build_*_argv`` builders and run the real
verb — the substrate's guards/hash-chains/contract-freezes stay authoritative.
"""
from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, LoadingIndicator, Static

from peers_ctl.tui import actions

#: timeout for the run-control verbs — stop's fail-closed podman probe can take
#: a moment; keep it generous but bounded.
_VERB_TIMEOUT_S = 90.0


class _InterveneScreen(ModalScreen):
    """Base modal: show the exact command, confirm, run the verb, show the audit.

    Subclasses implement :meth:`_build_argv` (from form fields) + :meth:`_title`
    / :meth:`_describe`, and may set :attr:`contract_touching` / override
    :meth:`_confirm_token` to require type-to-confirm."""

    #: contract-touching ops get the warning banner + type-to-confirm gate.
    contract_touching: bool = False
    #: whether this op requires a type-to-confirm Input (the confirm field is
    #: always composed for these, with the *token* resolved dynamically — the
    #: token may depend on a field, e.g. ack-block's step id, that is empty at
    #: compose time). Defaults to ``contract_touching`` in ``__init_subclass__``
    #: (defense-in-depth: a future contract-touching modal auto-inherits the
    #: human gate even if its author forgets to set ``requires_type_confirm``).
    requires_type_confirm: bool = False

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        # Enforce the invariant the docstring promises: ANY contract-touching
        # modal requires type-to-confirm. We only DEFAULT it on (set True when
        # the subclass did not declare it on its own class body) — a subclass
        # may still explicitly opt a non-contract op into the gate, and an
        # explicit value on the subclass is left untouched.
        if getattr(cls, "contract_touching", False) and (
            "requires_type_confirm" not in cls.__dict__
        ):
            cls.requires_type_confirm = True

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        *,
        project_name: str,
        config_dir: Path | str | None = None,
    ) -> None:
        super().__init__()
        self._project_name = project_name
        self._config_dir = str(config_dir) if config_dir is not None else None
        self._verb_running = False

    # -- subclass contract --------------------------------------------------- #
    def _verb_title(self) -> str:
        raise NotImplementedError

    def _describe(self) -> str:
        raise NotImplementedError

    def _build_argv(self) -> list[str]:
        raise NotImplementedError

    def _extra_fields(self) -> ComposeResult:
        """Yield any per-verb input rows (reason / step / acceptance)."""
        return iter(())

    def _confirm_token(self) -> str | None:
        """The exact text the operator must type to enable confirm, or None if
        this op is not type-to-confirm gated."""
        return None

    # -- layout -------------------------------------------------------------- #
    def compose(self) -> ComposeResult:
        dialog_classes = "intervene-danger" if self.contract_touching else ""
        with VerticalScroll(id="intervene-dialog", classes=dialog_classes):
            yield Static(self._verb_title(), id="intervene-title")
            if self.contract_touching:
                yield Static(
                    "⚠ auditiert — berührt eingefrorene Contracts",
                    id="intervene-warn", classes="state-alert")
            yield Static(self._describe(), id="intervene-desc",
                         classes="muted")
            yield from self._extra_fields()
            # the EXACT CLI command this modal will run (transparency).
            yield Static("", id="intervene-cmd", classes="wizard-cmd")
            if self.requires_type_confirm:
                # always composed for type-to-confirm ops; the required token is
                # resolved dynamically (it may depend on a field that is empty
                # at compose time, e.g. ack-block's step id). The label updates
                # as the token changes; the confirm Input is always present.
                yield Label(
                    "type the confirmation text to enable confirm",
                    id="intervene-confirm-label", classes="state-alert")
                yield Input(id="intervene-confirm")
            yield Static("", id="intervene-result")
            yield LoadingIndicator(id="intervene-spinner")
            with Horizontal(id="intervene-buttons"):
                yield Button("Confirm", id="intervene-confirm-btn",
                             variant="error" if self.contract_touching else "primary")
                yield Button("Cancel", id="intervene-cancel-btn",
                             variant="default")

    def on_mount(self) -> None:
        self.query_one("#intervene-spinner", LoadingIndicator).display = False
        # paint the live command preview + set the initial confirm-gate state.
        self._refresh_command()
        self._refresh_confirm_label()
        self._refresh_confirm_gate()

    # -- command preview + confirm gate -------------------------------------- #
    def _refresh_command(self) -> None:
        try:
            argv = self._build_argv()
        except Exception:
            argv = []
        try:
            self.query_one("#intervene-cmd", Static).update(
                "$ " + " ".join(argv) if argv else "(incomplete)")
        except Exception:
            pass

    def _typed_confirm(self) -> str:
        try:
            return self.query_one("#intervene-confirm", Input).value
        except Exception:
            return ""

    def _confirm_satisfied(self) -> bool:
        if not self.requires_type_confirm:
            return True
        token = self._confirm_token()
        # an empty/None token (the dependent field, e.g. the step id, is not yet
        # filled) can NEVER be satisfied — you cannot confirm an empty token.
        if not token:
            return False
        return self._typed_confirm() == token

    def _refresh_confirm_gate(self) -> None:
        try:
            btn = self.query_one("#intervene-confirm-btn", Button)
        except Exception:
            return
        btn.disabled = self._verb_running or not self._confirm_satisfied()

    def _refresh_confirm_label(self) -> None:
        """Update the type-to-confirm prompt with the live required token."""
        if not self.requires_type_confirm:
            return
        try:
            label = self.query_one("#intervene-confirm-label", Static)
        except Exception:
            return
        token = self._confirm_token()
        if token:
            label.update(f"type exactly  {token}  to confirm")
        else:
            label.update("fill the fields above, then type the "
                         "confirmation text")

    def on_input_changed(self, event: Input.Changed) -> None:
        # any field change updates the command preview; a field the token
        # depends on changes the required token; the confirm field (re)gates
        # the confirm button.
        self._refresh_command()
        self._refresh_confirm_label()
        self._refresh_confirm_gate()

    # -- run ----------------------------------------------------------------- #
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "intervene-cancel-btn":
            self.action_cancel()
        elif event.button.id == "intervene-confirm-btn":
            self._confirm()

    def _confirm(self) -> None:
        if self._verb_running or not self._confirm_satisfied():
            return
        try:
            argv = self._build_argv()
        except Exception:
            argv = []
        if not argv:
            self.query_one("#intervene-result", Static).update(
                "✗ command incomplete — fill the required fields")
            return
        self._verb_running = True
        self.query_one("#intervene-spinner", LoadingIndicator).display = True
        self.query_one("#intervene-result", Static).update("running…")
        self._refresh_confirm_gate()

        def _run() -> actions.VerbResult:
            return actions.run_verb(argv, timeout=_VERB_TIMEOUT_S)

        self.run_worker(_run, thread=True, name="intervene_run",
                        group="intervene_run")

    def on_worker_state_changed(self, event) -> None:
        from textual.worker import WorkerState
        worker = event.worker
        if worker.group != "intervene_run":
            return
        if worker.state is WorkerState.SUCCESS:
            self._on_done(worker.result)
        elif worker.state is WorkerState.ERROR:
            self._on_done(None)

    def _on_done(self, result) -> None:
        self._verb_running = False
        try:
            self.query_one("#intervene-spinner", LoadingIndicator).display = False
        except Exception:
            pass
        self._refresh_confirm_gate()
        try:
            out = self.query_one("#intervene-result", Static)
        except Exception:
            return
        if result is None:
            out.update("✗ the verb worker errored")
            out.set_class(True, "state-fail")
            return
        rc = getattr(result, "rc", -1)
        ok = rc == 0
        stdout = (getattr(result, "stdout", "") or "")[-800:]
        stderr = (getattr(result, "stderr", "") or "")[-800:]
        head = "✓ done" if ok else f"✗ refused / failed (rc={rc})"
        # stop can legitimately fail-closed (podman missing/timeout) — surface
        # the RuntimeError text from stderr rather than swallowing it.
        body = "\n".join(p for p in (stdout, stderr) if p.strip())
        out.update(f"{head}\n{body}".rstrip())
        out.set_class(ok, "state-pass")
        out.set_class(not ok, "state-fail")

    def action_cancel(self) -> None:
        if self._verb_running:
            return
        self.dismiss(None)


# --------------------------------------------------------------------------- #
# stop                                                                         #
# --------------------------------------------------------------------------- #
class StopScreen(_InterveneScreen):
    """Stop the active run (``stop <name> --grace-s``)."""

    def __init__(self, *, project_name, config_dir=None, grace_s: float = 10.0):
        super().__init__(project_name=project_name, config_dir=config_dir)
        self._grace_s = grace_s

    def _verb_title(self) -> str:
        return f"Stop run — {self._project_name}"

    def _describe(self) -> str:
        return (f"Gracefully stop '{self._project_name}' "
                f"(SIGTERM, {self._grace_s:g}s grace, then SIGKILL). "
                "stop can legitimately refuse (fail-closed) — the audit text "
                "is shown below.")

    def _build_argv(self) -> list[str]:
        return actions.build_stop_argv(
            self._project_name, grace_s=self._grace_s,
            config_dir=self._config_dir)


# --------------------------------------------------------------------------- #
# resume                                                                       #
# --------------------------------------------------------------------------- #
class ResumeScreen(_InterveneScreen):
    """Resume the active run (``resume <project_name>``)."""

    def _verb_title(self) -> str:
        return f"Resume run — {self._project_name}"

    def _describe(self) -> str:
        return f"Resume '{self._project_name}' from its last checkpoint."

    def _build_argv(self) -> list[str]:
        return actions.build_resume_argv(
            self._project_name, config_dir=self._config_dir)


# --------------------------------------------------------------------------- #
# ack-block (type-to-confirm: the STEP id)                                     #
# --------------------------------------------------------------------------- #
class AckBlockScreen(_InterveneScreen):
    """Acknowledge a blocked step (``ack-block <project> <step_id> --reason``).

    Contract-touching: mutates PLAN.md + the ``blocks.log`` hash-chain. The
    operator must type the exact STEP id to confirm."""

    contract_touching = True
    requires_type_confirm = True

    def __init__(self, *, project_name, config_dir=None,
                 step_id: str = "", reason: str = ""):
        super().__init__(project_name=project_name, config_dir=config_dir)
        self._step_default = step_id
        self._reason_default = reason

    def _verb_title(self) -> str:
        return f"Ack-block a step — {self._project_name}"

    def _describe(self) -> str:
        return ("Acknowledge a blocked PLAN step so the run can proceed past "
                "it. This writes the blocks hash-chain — irreversible audit.")

    def _extra_fields(self) -> ComposeResult:
        yield Label("step id", classes="wizard-label")
        yield Input(value=self._step_default, placeholder="STEP-3",
                    id="intervene-step")
        yield Label("reason", classes="wizard-label")
        yield Input(value=self._reason_default,
                    placeholder="why this step is acknowledged",
                    id="intervene-reason")

    def _step(self) -> str:
        try:
            return self.query_one("#intervene-step", Input).value.strip()
        except Exception:
            return ""

    def _reason(self) -> str:
        try:
            return self.query_one("#intervene-reason", Input).value.strip()
        except Exception:
            return ""

    def _confirm_token(self) -> str | None:
        # type-to-confirm the exact STEP id (only once a step id is entered).
        step = self._step()
        return step or None

    def _build_argv(self) -> list[str]:
        step = self._step()
        reason = self._reason()
        if not step or not reason:
            return []
        return actions.build_ack_block_argv(
            project_name=self._project_name, step_id=step, reason=reason,
            config_dir=self._config_dir)


# --------------------------------------------------------------------------- #
# amend (type-to-confirm: the project name)                                    #
# --------------------------------------------------------------------------- #
class AmendScreen(_InterveneScreen):
    """Amend the frozen acceptance gate (``amend <project> --acceptance --reason``).

    Contract-touching: re-pins the frozen acceptance gate + writes the
    ``contracts.log`` hash-chain. The operator must type the project name to
    confirm."""

    contract_touching = True
    requires_type_confirm = True

    def __init__(self, *, project_name, config_dir=None,
                 acceptance: str = "", reason: str = ""):
        super().__init__(project_name=project_name, config_dir=config_dir)
        self._acceptance_default = acceptance
        self._reason_default = reason

    def _verb_title(self) -> str:
        return f"Amend acceptance — {self._project_name}"

    def _describe(self) -> str:
        return ("Re-pin the frozen acceptance command for this run. This writes "
                "the contracts hash-chain — a contract-level change.")

    def _extra_fields(self) -> ComposeResult:
        yield Label("acceptance command", classes="wizard-label")
        yield Input(value=self._acceptance_default,
                    placeholder="pytest -q", id="intervene-acceptance")
        yield Label("reason", classes="wizard-label")
        yield Input(value=self._reason_default,
                    placeholder="why re-pin the acceptance gate",
                    id="intervene-reason")

    def _acceptance(self) -> str:
        try:
            return self.query_one("#intervene-acceptance", Input).value.strip()
        except Exception:
            return ""

    def _reason(self) -> str:
        try:
            return self.query_one("#intervene-reason", Input).value.strip()
        except Exception:
            return ""

    def _confirm_token(self) -> str | None:
        # type-to-confirm the project name.
        return self._project_name or None

    def _build_argv(self) -> list[str]:
        acceptance = self._acceptance()
        reason = self._reason()
        if not acceptance or not reason:
            return []
        return actions.build_amend_argv(
            project_name=self._project_name, acceptance=acceptance,
            reason=reason, config_dir=self._config_dir)
