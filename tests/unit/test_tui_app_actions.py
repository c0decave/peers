"""Headless pilots for Wave-1b Unit I: the launch wizard + intervention modals.

Skipif-guarded (the default ``.[dev]`` CI has no Textual). The pilots drive
``PeersTuiApp.run_test()`` / the modal screens and assert the SECURITY contract:

  * **Wizard:** open it, fill the form, submit → ``run_verb`` is monkeypatched to
    CAPTURE argv (NO real launch); the captured ``new`` + ``start`` argv match
    the form (modes csv, host/container flag, budget flags, plan); ``doctor_
    preflight`` is called on open and gates the buttons; the ``new`` run uses an
    EXPLICIT cwd (the target dir, not the TUI cwd).
  * **Intervention:** stop → confirm → ``build_stop_argv`` verb invoked; ack-block
    / amend → the confirm button is DISABLED until the exact type-to-confirm text
    is entered, then enabled → the right verb is invoked; the displayed command
    equals the argv.

``run_verb`` is monkeypatched everywhere so NO real peers-ctl is ever launched.
One pilot writes the wizard screenshot SVG to /tmp.
"""
from __future__ import annotations

import importlib.util

import pytest

textual_missing = importlib.util.find_spec("textual") is None
pytestmark = pytest.mark.skipif(
    textual_missing,
    reason="textual extra (peers[tui]) not installed; TUI pilot needs the optional GUI dependency",
)

if not textual_missing:
    import yaml

    from peers_ctl.tui import actions as A
    from peers_ctl.tui.app import PeersTuiApp
    from peers_ctl.tui.screens import wizard as wizard_mod
    from peers_ctl.tui.screens import intervene as intervene_mod
    from peers_ctl.tui.screens.intervene import (
        AckBlockScreen,
        AmendScreen,
        ResumeScreen,
        StopScreen,
        _InterveneScreen,
    )
    from peers_ctl.tui.screens.wizard import LaunchWizardScreen
    from textual.widgets import Button, Input, Select, SelectionList, Switch


# --------------------------------------------------------------------------- #
# fixtures + helpers                                                           #
# --------------------------------------------------------------------------- #
def _config(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    proj = tmp_path / "proj"
    (proj / ".peers").mkdir(parents=True, exist_ok=True)
    (proj / ".peers" / "state.json").write_text(
        '{"iteration": 1, "mode": "develop", "peer_order": ["claude"], '
        '"turn_index": 0, "peers": {"claude": {"state": "healthy"}}}')
    (cfg / "projects.yaml").write_text(yaml.safe_dump(
        {"projects": [{"name": "proj", "path": str(proj),
                       "state": "running", "pid": 9}]}))
    return cfg, proj


class _Capture:
    """A monkeypatch target for ``run_verb`` that records argv + cwd and returns
    a canned success WITHOUT launching anything."""

    def __init__(self, rc=0, stdout="ok", stderr=""):
        self.calls: list[dict] = []
        self._rc, self._stdout, self._stderr = rc, stdout, stderr

    def __call__(self, argv, *, cwd=None, timeout=120.0):
        self.calls.append({"argv": list(argv), "cwd": cwd, "timeout": timeout})
        return A.VerbResult(rc=self._rc, stdout=self._stdout,
                            stderr=self._stderr, timed_out=False)


async def _settle(pilot, n=6):
    await pilot.pause()
    for _ in range(n):
        await pilot.pause(0.2)


# --------------------------------------------------------------------------- #
# wizard: doctor preflight on open + button gating                            #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_wizard_runs_doctor_preflight_on_open(tmp_path, monkeypatch):
    cfg, _ = _config(tmp_path)
    called = {"doctor": 0}

    def fake_doctor(config_dir=None):
        called["doctor"] += 1
        return A.DoctorResult(
            ok=True, rc=0,
            lines=["  [OK]    podman   5.8.2",
                   "  [OK]    peers version   host=1.6.0",
                   "Summary: 8 ok, 0 warn, 0 miss."])

    # monkeypatch on the actions module the wizard imports.
    monkeypatch.setattr(wizard_mod.actions, "doctor_preflight", fake_doctor)
    monkeypatch.setattr(wizard_mod.actions, "run_verb",
                        _Capture(stdout="NAME  VER\naudit  v1  builtin  x\n"))

    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await _settle(pilot)
        await pilot.press("n")
        await _settle(pilot)
        assert isinstance(app.screen, LaunchWizardScreen)
        # doctor_preflight ran on open.
        assert called["doctor"] >= 1
        # the doctor summary is shown + the Launch button is enabled (host ok).
        from textual.widgets import Static
        doc = str(app.screen.query_one("#wizard-doctor", Static).render())
        assert "Summary" in doc or "OK" in doc
        assert app.screen.query_one("#wiz-launch", Button).disabled is False


@pytest.mark.asyncio
async def test_wizard_gates_buttons_when_podman_and_peers_missing(tmp_path, monkeypatch):
    cfg, _ = _config(tmp_path)

    def fake_doctor(config_dir=None):
        return A.DoctorResult(
            ok=False, rc=2,
            lines=["  [MISS]  podman   not found",
                   "  [MISS]  peers version   not found",
                   "Summary: 0 ok, 0 warn, 2 miss."])

    monkeypatch.setattr(wizard_mod.actions, "doctor_preflight", fake_doctor)
    monkeypatch.setattr(wizard_mod.actions, "run_verb", _Capture())

    screen = LaunchWizardScreen(config_dir=cfg)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await _settle(pilot)
        # neither host nor container available -> Launch disabled.
        assert screen.query_one("#wiz-launch", Button).disabled is True
        # the container switch is disabled (podman missing).
        assert screen.query_one("#wiz-container", Switch).disabled is True


# --------------------------------------------------------------------------- #
# wizard: submit captures new+start argv (NO real launch) + explicit cwd        #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_wizard_submit_captures_new_and_start_argv(tmp_path, monkeypatch):
    cfg, _ = _config(tmp_path)
    target = tmp_path / "newproj"  # does not exist yet -> cwd is its parent.

    def fake_doctor(config_dir=None):
        return A.DoctorResult(ok=True, rc=0, lines=["Summary: ok"])

    cap = _Capture()
    monkeypatch.setattr(wizard_mod.actions, "doctor_preflight", fake_doctor)
    monkeypatch.setattr(wizard_mod.actions, "run_verb", cap)

    screen = LaunchWizardScreen(config_dir=cfg)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await _settle(pilot)
        # fill the form.
        screen.query_one("#wiz-path", Input).value = str(target)
        screen.query_one("#wiz-max-ticks", Input).value = "12"
        screen.query_one("#wiz-max-runtime", Input).value = "4h"
        screen.query_one("#wiz-plan", Input).value = str(tmp_path / "PLAN.md")
        screen.query_one("#wiz-driver", Select).value = "orchestrator"
        screen.query_one("#wiz-lang", Select).value = "python"
        # select two modes (host run -> container switch left off).
        sel = screen.query_one("#wiz-modes", SelectionList)
        # the modes-list worker is monkeypatched to canned; seed selection
        # deterministically regardless.
        sel.clear_options()
        from textual.widgets.selection_list import Selection
        sel.add_options([Selection("audit", "audit"),
                         Selection("implement", "implement")])
        sel.select(sel.get_option_at_index(0))
        sel.select(sel.get_option_at_index(1))
        await pilot.pause()

        # the modes list 'run_verb' was monkeypatched too -> no real launch yet.
        # submit.
        screen.query_one("#wiz-launch", Button).press()
        await _settle(pilot)

        # exactly two run_verb calls beyond the (captured) modes-list call:
        # one for `new`, one for `start`.
        new_calls = [c for c in cap.calls if "new" in c["argv"]]
        start_calls = [c for c in cap.calls if "start" in c["argv"]]
        assert len(new_calls) == 1, cap.calls
        assert len(start_calls) == 1, cap.calls
        new = new_calls[0]
        start = start_calls[0]

        # ---- new argv matches the form ---------------------------------- #
        na = new["argv"]
        assert na[na.index("new") + 1] == str(target)
        assert na[na.index("--modes") + 1] == "audit,implement"
        assert na[na.index("--driver") + 1] == "orchestrator"
        assert na[na.index("--lang") + 1] == "python"
        assert na[na.index("--plan") + 1] == str(tmp_path / "PLAN.md")
        assert "--container" not in na  # host run
        # config-dir threaded.
        assert na[na.index("--config-dir") + 1] == str(cfg)

        # ---- the new run uses an EXPLICIT cwd = the target's PARENT ------ #
        # (target dir does not exist yet) — and NEVER the inherited TUI cwd.
        import os
        assert new["cwd"] == str(target.parent)
        assert new["cwd"] != os.getcwd()
        # a generous timeout for the new --plan preflight.
        assert new["timeout"] >= 90

        # ---- start argv matches the form -------------------------------- #
        sa = start["argv"]
        assert sa[sa.index("start") + 1] == "newproj"
        assert sa[sa.index("--max-ticks") + 1] == "12"
        assert sa[sa.index("--max-runtime") + 1] == "4h"
        assert "--container" not in sa


@pytest.mark.asyncio
async def test_wizard_relative_target_resolves_to_absolute_cwd(tmp_path, monkeypatch):
    """SAD path: a RELATIVE / bare target must NOT make ``new`` run in the TUI's
    own cwd (the explicit-cwd invariant). The wizard resolves the target to an
    ABSOLUTE path (mirroring ``cmd_new``'s bare-name resolution), so the derived
    cwd is absolute and never ``.``/``os.getcwd()``."""
    cfg, _ = _config(tmp_path)
    # a bare project name (no separators) — historically resolved to ``.``.
    monkeypatch.setenv("PEERS_PROJECTS_ROOT", str(tmp_path / "proots"))

    monkeypatch.setattr(wizard_mod.actions, "doctor_preflight",
                        lambda config_dir=None: A.DoctorResult(
                            ok=True, rc=0, lines=["Summary: ok"]))
    cap = _Capture()
    monkeypatch.setattr(wizard_mod.actions, "run_verb", cap)

    screen = LaunchWizardScreen(config_dir=cfg)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await _settle(pilot)
        screen.query_one("#wiz-path", Input).value = "myproj"  # bare/relative
        await pilot.pause()
        screen.query_one("#wiz-launch", Button).press()
        await _settle(pilot)

        new = [c for c in cap.calls if "new" in c["argv"]][0]
        import os
        from pathlib import Path as _P
        # the derived cwd is ABSOLUTE and is NOT the TUI's own cwd / ``.``.
        assert _P(new["cwd"]).is_absolute(), new["cwd"]
        assert new["cwd"] != os.getcwd()
        assert new["cwd"] not in (".", "")
        # the cwd is the resolved target's PARENT (under PEERS_PROJECTS_ROOT).
        assert new["cwd"] == str((tmp_path / "proots").resolve())
        # and the argv path the verb gets is the SAME absolute target (they agree).
        na = new["argv"]
        assert na[na.index("new") + 1] == str((tmp_path / "proots" / "myproj").resolve())


@pytest.mark.asyncio
async def test_wizard_container_flag_flows_to_both_argvs(tmp_path, monkeypatch):
    cfg, _ = _config(tmp_path)
    target = tmp_path / "cproj"

    monkeypatch.setattr(wizard_mod.actions, "doctor_preflight",
                        lambda config_dir=None: A.DoctorResult(
                            ok=True, rc=0, lines=["Summary: ok"]))
    cap = _Capture()
    monkeypatch.setattr(wizard_mod.actions, "run_verb", cap)

    screen = LaunchWizardScreen(config_dir=cfg)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await _settle(pilot)
        screen.query_one("#wiz-path", Input).value = str(target)
        screen.query_one("#wiz-container", Switch).value = True  # container
        await pilot.pause()
        screen.query_one("#wiz-launch", Button).press()
        await _settle(pilot)
        new = [c for c in cap.calls if "new" in c["argv"]][0]["argv"]
        start = [c for c in cap.calls if "start" in c["argv"]][0]["argv"]
        assert "--container" in new
        assert "--container" in start


@pytest.mark.asyncio
async def test_wizard_has_no_dead_peers_field(tmp_path, monkeypatch):
    """The ``#wiz-peers`` Input was a silent no-op (``build_argvs`` never read it
    and there is no ``--peers`` flag). It must be gone so the write surface has
    no misleading dead control."""
    cfg, _ = _config(tmp_path)
    monkeypatch.setattr(wizard_mod.actions, "doctor_preflight",
                        lambda config_dir=None: A.DoctorResult(
                            ok=True, rc=0, lines=["Summary: ok"]))
    monkeypatch.setattr(wizard_mod.actions, "run_verb", _Capture())
    screen = LaunchWizardScreen(config_dir=cfg)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await _settle(pilot)
        assert not screen.query("#wiz-peers")


@pytest.mark.asyncio
async def test_wizard_template_widens_new_timeout(tmp_path, monkeypatch):
    """A ``--template`` (cold repo clone) + the 60s preflight can exceed 120s, so
    the ``new`` run gets a wider timeout when template (or container) is set."""
    cfg, _ = _config(tmp_path)
    target = tmp_path / "tproj"
    monkeypatch.setattr(wizard_mod.actions, "doctor_preflight",
                        lambda config_dir=None: A.DoctorResult(
                            ok=True, rc=0, lines=["Summary: ok"]))
    cap = _Capture()
    monkeypatch.setattr(wizard_mod.actions, "run_verb", cap)
    screen = LaunchWizardScreen(config_dir=cfg)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await _settle(pilot)
        screen.query_one("#wiz-path", Input).value = str(target)
        screen.query_one("#wiz-template", Select).value = "internal testing"
        await pilot.pause()
        screen.query_one("#wiz-launch", Button).press()
        await _settle(pilot)
        new = [c for c in cap.calls if "new" in c["argv"]][0]
        # cold clone + preflight needs more than the default 120s.
        assert new["timeout"] >= 300


@pytest.mark.asyncio
async def test_wizard_requires_path(tmp_path, monkeypatch):
    cfg, _ = _config(tmp_path)
    monkeypatch.setattr(wizard_mod.actions, "doctor_preflight",
                        lambda config_dir=None: A.DoctorResult(
                            ok=True, rc=0, lines=["Summary: ok"]))
    cap = _Capture()
    monkeypatch.setattr(wizard_mod.actions, "run_verb", cap)
    screen = LaunchWizardScreen(config_dir=cfg)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await _settle(pilot)
        # no path filled -> Launch is a no-op (no new/start argv captured).
        screen.query_one("#wiz-launch", Button).press()
        await _settle(pilot)
        assert not any("new" in c["argv"] for c in cap.calls)
        assert not any("start" in c["argv"] for c in cap.calls)


# --------------------------------------------------------------------------- #
# intervention: stop -> confirm -> build_stop_argv invoked                      #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_stop_modal_confirm_invokes_stop_verb(tmp_path, monkeypatch):
    cfg, _ = _config(tmp_path)
    cap = _Capture(stdout="stopped")
    monkeypatch.setattr(intervene_mod.actions, "run_verb", cap)

    screen = StopScreen(project_name="proj", config_dir=cfg, grace_s=10.0)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await _settle(pilot)
        # the displayed command equals the argv stop builder would produce.
        expected = A.build_stop_argv("proj", grace_s=10.0, config_dir=str(cfg))
        from textual.widgets import Static
        shown = str(screen.query_one("#intervene-cmd", Static).render())
        assert shown == "$ " + " ".join(expected)
        # stop is NOT type-to-confirm -> confirm enabled immediately.
        btn = screen.query_one("#intervene-confirm-btn", Button)
        assert btn.disabled is False
        btn.press()
        await _settle(pilot)
        assert len(cap.calls) == 1
        assert cap.calls[0]["argv"] == expected


@pytest.mark.asyncio
async def test_stop_modal_surfaces_fail_closed_text(tmp_path, monkeypatch):
    cfg, _ = _config(tmp_path)
    # stop legitimately fail-closes: non-zero rc + a RuntimeError on stderr.
    cap = _Capture(rc=1, stdout="", stderr="RuntimeError: podman not available")
    monkeypatch.setattr(intervene_mod.actions, "run_verb", cap)
    screen = StopScreen(project_name="proj", config_dir=cfg)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await _settle(pilot)
        screen.query_one("#intervene-confirm-btn", Button).press()
        await _settle(pilot)
        from textual.widgets import Static
        result = str(screen.query_one("#intervene-result", Static).render())
        # the fail-closed RuntimeError text is surfaced, not swallowed.
        assert "podman not available" in result
        assert "rc=1" in result


# --------------------------------------------------------------------------- #
# intervention: ack-block type-to-confirm gates the confirm button              #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_ack_block_type_to_confirm_gates_button(tmp_path, monkeypatch):
    cfg, _ = _config(tmp_path)
    cap = _Capture(stdout="acked")
    monkeypatch.setattr(intervene_mod.actions, "run_verb", cap)

    screen = AckBlockScreen(project_name="proj", config_dir=cfg)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await _settle(pilot)
        # contract-touching -> the danger banner is present.
        assert screen.query("#intervene-warn")
        step = screen.query_one("#intervene-step", Input)
        reason = screen.query_one("#intervene-reason", Input)
        confirm = screen.query_one("#intervene-confirm", Input)
        btn = screen.query_one("#intervene-confirm-btn", Button)

        # fill step + reason; the confirm token is now the STEP id.
        step.value = "STEP-3"
        reason.value = "external dep missing"
        await pilot.pause()
        # confirm is DISABLED until the exact step id is typed.
        assert btn.disabled is True
        confirm.value = "STEP-"   # partial -> still disabled.
        await pilot.pause()
        assert btn.disabled is True
        confirm.value = "STEP-3"  # exact match -> enabled.
        await pilot.pause()
        assert btn.disabled is False

        # the displayed command equals the ack-block argv.
        from textual.widgets import Static
        expected = A.build_ack_block_argv(
            project_name="proj", step_id="STEP-3",
            reason="external dep missing", config_dir=str(cfg))
        shown = str(screen.query_one("#intervene-cmd", Static).render())
        assert shown == "$ " + " ".join(expected)

        btn.press()
        await _settle(pilot)
        assert len(cap.calls) == 1
        assert cap.calls[0]["argv"] == expected


# --------------------------------------------------------------------------- #
# intervention: amend type-to-confirm requires the PROJECT name                  #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_amend_type_to_confirm_requires_project_name(tmp_path, monkeypatch):
    cfg, _ = _config(tmp_path)
    cap = _Capture(stdout="amended")
    monkeypatch.setattr(intervene_mod.actions, "run_verb", cap)

    screen = AmendScreen(project_name="proj", config_dir=cfg)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await _settle(pilot)
        acceptance = screen.query_one("#intervene-acceptance", Input)
        reason = screen.query_one("#intervene-reason", Input)
        confirm = screen.query_one("#intervene-confirm", Input)
        btn = screen.query_one("#intervene-confirm-btn", Button)

        acceptance.value = "pytest -q"
        reason.value = "re-pin acceptance"
        await pilot.pause()
        assert btn.disabled is True            # nothing typed yet
        confirm.value = "wrong-name"
        await pilot.pause()
        assert btn.disabled is True            # wrong project name
        confirm.value = "proj"                  # exact project name
        await pilot.pause()
        assert btn.disabled is False

        expected = A.build_amend_argv(
            project_name="proj", acceptance="pytest -q",
            reason="re-pin acceptance", config_dir=str(cfg))
        btn.press()
        await _settle(pilot)
        assert len(cap.calls) == 1
        assert cap.calls[0]["argv"] == expected


# --------------------------------------------------------------------------- #
# Fix 5: contract_touching ⇒ requires_type_confirm (enforced via __init_subclass__)
# --------------------------------------------------------------------------- #
def test_all_contract_touching_screens_require_type_confirm():
    """Every existing contract-touching modal also gates with type-to-confirm —
    the invariant the base docstring promises (ack-block + amend)."""
    contract_screens = [AckBlockScreen, AmendScreen]
    for cls in contract_screens:
        assert cls.contract_touching is True, cls.__name__
        assert cls.requires_type_confirm is True, (
            f"{cls.__name__} is contract_touching but missing type-confirm gate")
    # control: a non-contract op is NOT auto-gated.
    assert ResumeScreen.contract_touching is False
    assert ResumeScreen.requires_type_confirm is False


def test_new_contract_touching_subclass_auto_inherits_type_confirm():
    """Defense-in-depth (Fix 5): a NEW contract-touching modal that FORGETS to
    set ``requires_type_confirm`` still gets the gate via ``__init_subclass__``."""
    class _FutureContractModal(_InterveneScreen):
        contract_touching = True
        # deliberately does NOT set requires_type_confirm.

        def _verb_title(self) -> str:
            return "future"

        def _describe(self) -> str:
            return "future contract-touching op"

        def _build_argv(self) -> list[str]:
            return []

    assert _FutureContractModal.requires_type_confirm is True


def test_non_contract_subclass_does_not_auto_gate():
    """Sad/edge: a non-contract subclass that omits the flag stays ungated
    (the auto-default only fires for contract_touching classes)."""
    class _PlainModal(_InterveneScreen):
        def _verb_title(self) -> str:
            return "plain"

        def _describe(self) -> str:
            return "plain op"

        def _build_argv(self) -> list[str]:
            return []

    assert _PlainModal.contract_touching is False
    assert _PlainModal.requires_type_confirm is False


def test_explicit_type_confirm_on_contract_subclass_is_respected():
    """Edge: an explicit ``requires_type_confirm`` on the subclass body is left
    untouched (we only DEFAULT it, never override an explicit declaration)."""
    class _ExplicitOff(_InterveneScreen):
        contract_touching = True
        requires_type_confirm = False  # explicit on this class body

        def _verb_title(self) -> str:
            return "x"

        def _describe(self) -> str:
            return "x"

        def _build_argv(self) -> list[str]:
            return []

    assert _ExplicitOff.requires_type_confirm is False


# --------------------------------------------------------------------------- #
# app wiring: `n` opens the wizard; `s` needs a selected run                    #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_app_n_key_opens_wizard(tmp_path, monkeypatch):
    cfg, _ = _config(tmp_path)
    monkeypatch.setattr(wizard_mod.actions, "doctor_preflight",
                        lambda config_dir=None: A.DoctorResult(
                            ok=True, rc=0, lines=["Summary: ok"]))
    monkeypatch.setattr(wizard_mod.actions, "run_verb", _Capture())
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await _settle(pilot)
        await pilot.press("n")
        await _settle(pilot)
        assert isinstance(app.screen, LaunchWizardScreen)


@pytest.mark.asyncio
async def test_app_s_key_opens_stop_modal_for_selected_run(tmp_path, monkeypatch):
    cfg, _ = _config(tmp_path)
    monkeypatch.setattr(intervene_mod.actions, "run_verb", _Capture())
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await _settle(pilot)
        # the fixture seeds one running project -> it auto-selects.
        assert app._active_name == "proj"
        await pilot.press("s")
        await _settle(pilot)
        assert isinstance(app.screen, StopScreen)


# --------------------------------------------------------------------------- #
# vim keys inert while a modal Input is focused                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_vim_keys_inert_while_modal_input_focused(tmp_path, monkeypatch):
    cfg, _ = _config(tmp_path)
    monkeypatch.setattr(intervene_mod.actions, "run_verb", _Capture())
    screen = AmendScreen(project_name="proj", config_dir=cfg)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await _settle(pilot)
        screen.query_one("#intervene-acceptance", Input).focus()
        await pilot.pause()
        # with a modal Input focused, the single-letter actions are inert.
        assert app.check_action("stop_run", ()) is False
        assert app.check_action("amend_run", ()) is False
        assert app.check_action("new_run", ()) is False
        # arrows stay live.
        assert app.check_action("focus_next_panel", ()) is True


# --------------------------------------------------------------------------- #
# screenshot artifact (to /tmp, NOT committed)                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_wizard_screenshot_artifact(tmp_path, monkeypatch):
    cfg, _ = _config(tmp_path)
    monkeypatch.setattr(wizard_mod.actions, "doctor_preflight",
                        lambda config_dir=None: A.DoctorResult(
                            ok=True, rc=0,
                            lines=["  [OK]    podman   5.8.2",
                                   "Summary: 8 ok, 0 warn, 0 miss."]))
    monkeypatch.setattr(
        wizard_mod.actions, "run_verb",
        _Capture(stdout="NAME  VER\naudit  v1  builtin  x\n"
                        "implement  v1  builtin  y\n"))
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await _settle(pilot)
        await pilot.press("n")
        await _settle(pilot)
        assert isinstance(app.screen, LaunchWizardScreen)
        app.screen.query_one("#wiz-path", Input).value = str(tmp_path / "demo")
        await pilot.pause()
        out = app.save_screenshot("/tmp/peers-tui-wizard.svg")
        assert out
    import os
    assert os.path.exists("/tmp/peers-tui-wizard.svg")


# --------------------------------------------------------------------------- #
# EDGE: degenerate confirm-token / capability boundaries                        #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_ack_block_empty_step_token_never_confirmable_edge(tmp_path, monkeypatch):
    """EDGE: ack-block's type-to-confirm token IS the step id. With the step
    field left EMPTY the token resolves to None, so typing anything into the
    confirm box can never enable confirm (the ``if not token`` floor) — and the
    built argv stays empty, so there is nothing to run."""
    cfg, _ = _config(tmp_path)
    monkeypatch.setattr(intervene_mod.actions, "run_verb", _Capture())
    screen = AckBlockScreen(project_name="proj", config_dir=cfg)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await _settle(pilot)
        step = screen.query_one("#intervene-step", Input)
        confirm = screen.query_one("#intervene-confirm", Input)
        btn = screen.query_one("#intervene-confirm-btn", Button)
        assert step.value == ""                 # step deliberately empty
        confirm.value = "STEP-3"                 # would match IF a step were set
        await pilot.pause()
        assert btn.disabled is True              # empty token -> never confirmable
        assert screen._build_argv() == []        # missing step/reason -> no argv


@pytest.mark.asyncio
async def test_wizard_host_missing_forces_container_switch_edge(tmp_path, monkeypatch):
    """EDGE: host ``peers`` missing but podman present — the wizard forces the
    container path ON (switch value True + disabled) yet keeps Launch enabled,
    because the run is still launchable via the container. This single-missing
    branch is distinct from the both-missing case (which disables Launch)."""
    cfg, _ = _config(tmp_path)

    def fake_doctor(config_dir=None):
        return A.DoctorResult(
            ok=False, rc=2,
            lines=["  [MISS]  peers version   not found",
                   "  [OK]    podman           5.8.2",
                   "Summary: 1 ok, 0 warn, 1 miss."])

    monkeypatch.setattr(wizard_mod.actions, "doctor_preflight", fake_doctor)
    monkeypatch.setattr(wizard_mod.actions, "run_verb", _Capture())
    screen = LaunchWizardScreen(config_dir=cfg)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await _settle(pilot)
        switch = screen.query_one("#wiz-container", Switch)
        assert switch.value is True             # forced into the container path
        assert switch.disabled is True          # and locked there (host gone)
        # launch is NOT disabled: the container path is viable.
        assert screen.query_one("#wiz-launch", Button).disabled is False
