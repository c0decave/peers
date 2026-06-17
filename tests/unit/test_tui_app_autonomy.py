"""Headless pilots for the Unit J autonomy windows + layout persistence.

Runs only under the optional ``[tui]`` extra (Textual) — skipif-guarded so the
default ``.[dev]`` CI (no Textual) merely SKIPS. Asserts the 5 forward-looking
autonomy panels render their HONEST empty-state without crashing, that the
Spine-Gates / ledger RE-DERIVE convergence from a real minimal attested ledger
(never a stored independence flag), that the Eskalations-Banner shows RED when a
HALTED.md exists and is quiet otherwise, and that the layout (visible-panel set)
round-trips through the app (toggle → quit → relaunch → persisted).

One pilot writes ``/tmp/peers-tui-cockpit.svg`` (autonomy windows open in their
empty-state) and another ``/tmp/peers-tui-escalation.svg`` (HALTED.md present →
the red banner) for visual review.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess

import pytest

textual_missing = importlib.util.find_spec("textual") is None
pytestmark = pytest.mark.skipif(
    textual_missing,
    reason="textual extra (peers[tui]) not installed; TUI pilot needs the optional GUI dependency",
)

if not textual_missing:
    import yaml

    from peers_ctl.tui import layout as layout_mod
    from peers_ctl.tui.app import PANEL_SPECS, PeersTuiApp
    from peers_ctl.tui.panels.autonomy import (
        AutonomyFeedPanel,
        AutonomyLedgerPanel,
        EscalationBannerPanel,
        PropagationsPanel,
        SpineGatesPanel,
    )


# --------------------------------------------------------------------------- #
# fixtures                                                                      #
# --------------------------------------------------------------------------- #
def _write_registry(config_dir, projects):
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "projects.yaml").write_text(
        yaml.safe_dump({"projects": projects}, sort_keys=False)
    )


def _seed_state(project_dir, state):
    (project_dir / ".peers").mkdir(parents=True, exist_ok=True)
    (project_dir / ".peers" / "state.json").write_text(json.dumps(state))


def _one_project(tmp_path):
    """A single fresh project + a config dir pointing the layout at tmp (isolated)."""
    cfg = tmp_path / "config"
    proj = tmp_path / "proj"
    _seed_state(proj, {"iteration": 1, "mode": "develop",
                       "peer_order": ["claude"], "turn_index": 0})
    _write_registry(cfg, [
        {"name": "proj", "path": str(proj), "state": "running", "pid": 1},
    ])
    return cfg, proj


# ---- a real minimal attested spine ledger (re-derivation, not a stub) ------ #
def _git(p, *args):
    subprocess.run(["git", "-C", str(p), *args], check=True,
                   capture_output=True, text=True)


def _init_repo(p):
    p.mkdir(parents=True, exist_ok=True)
    _git(p, "init", "-q")
    _git(p, "config", "user.email", "t@t")
    _git(p, "config", "user.name", "t")
    _git(p, "config", "commit.gpgsign", "false")


def _file_witness(d, content="ok"):
    wp = d / "evidence.txt"
    wp.write_text(content)
    return {"kind": "file", "uri": str(wp),
            "sha256": hashlib.sha256(content.encode()).hexdigest()}


def _attested_repo(p, peer="claude"):
    from peers import attest
    _init_repo(p)
    (p / "a.py").write_text("a")
    _git(p, "add", "a.py")
    _git(p, "commit", "-q", "-m", "a")
    base = subprocess.run(["git", "-C", str(p), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    (p / "b.py").write_text("b")
    _git(p, "add", "b.py")
    _git(p, "commit", "-q", "-m", "b")
    sha = subprocess.run(["git", "-C", str(p), "rev-parse", "HEAD"],
                         check=True, capture_output=True, text=True).stdout.strip()
    attest.attest_commits(p, peer, base, sha)
    return sha


def _seed_converged_ledger(proj):
    """Write a real, hash-valid, attested ``.peers/run.jsonl`` that CONVERGES."""
    from peers.spine.ledger import RunLedger
    from peers.spine.op_config import OpConfig, load_op_config
    sha = _attested_repo(proj, "claude")
    led = RunLedger(proj / ".peers" / "run.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="r1")
    led.append(event="bar-inferred", status="pass")
    led.append_attested(proj, sha, event="confirmed-work", subject="u1",
                        status="pass", witness=_file_witness(proj),
                        independence=True)


# --------------------------------------------------------------------------- #
# 1. the 5 autonomy panels render their HONEST empty-state without crashing    #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_autonomy_panels_empty_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfg, _proj = _one_project(tmp_path)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(5):
            await pilot.pause(0.3)
        # every autonomy panel exists and shows the forward-looking empty-state.
        for cls, body_id in (
            (AutonomyLedgerPanel, "#autoledger-body"),
            (SpineGatesPanel, "#spinegates-body"),
            (PropagationsPanel, "#propagations-body"),
            (AutonomyFeedPanel, "#autofeed-body"),
        ):
            panel = app.query_one(cls)
            empties = panel.query(".empty-state")
            assert len(empties) >= 1, f"{cls.__name__} should show an empty-state"
        # the escalation banner is QUIET (no HALTED/CONCERNS) -> dim, no red.
        banner = app.query_one(EscalationBannerPanel)
        assert len(banner.query(".escalation-banner")) == 0
        assert len(banner.query(".state-dim")) >= 1


# --------------------------------------------------------------------------- #
# 2. Spine-Gates / Ledger RE-DERIVE convergence from a real attested ledger     #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_spine_gates_rederive_converged(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfg, proj = _one_project(tmp_path)
    _seed_converged_ledger(proj)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(6):
            await pilot.pause(0.3)
        gates = app.query_one(SpineGatesPanel)
        # NOT the empty-state any more — real re-derived gates render.
        assert len(gates.query(".empty-state")) == 0
        # the re-derived CONVERGED verdict reads green (.state-converged).
        assert len(gates.query(".state-converged")) >= 1
        # at least the authorship-attested + witness-ledgered gates pass (green).
        assert len(gates.query(".spinegate-row.state-pass")) >= 2
        # the ledger shows the integrity badge ✓ (verify() True).
        ledger = app.query_one(AutonomyLedgerPanel)
        header = ledger.query_one("#autoledger-header")
        assert "verified" in str(header.render()).lower()
        assert len(ledger.query(".autoledger-event")) >= 1
        # Fix #1 inverse: on a VALID (verified True) ledger, a pass event still
        # colors normally — the neutralization only kicks in when verify() fails.
        assert len(ledger.query(".autoledger-event.state-pass")) >= 1, (
            "a confirmed-work/pass event on a valid ledger still colors green"
        )


@pytest.mark.asyncio
async def test_spine_gates_forged_independence_not_converged(tmp_path, monkeypatch):
    # HONESTY SEAM: a hand-forged ledger line (independence:true, broken chain)
    # must NOT show CONVERGED — the panel renders the RE-DERIVED verdict only.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfg, proj = _one_project(tmp_path)
    forged = {
        "v": 1, "prev": None, "event": "confirmed-work", "mode_run": "r1",
        "author": None, "subject": "forged", "status": "pass", "witness": None,
        "independence": True, "entry_sha": "deadbeef" * 8,
    }
    (proj / ".peers" / "run.jsonl").write_text(json.dumps(forged) + "\n")
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(6):
            await pilot.pause(0.3)
        gates = app.query_one(SpineGatesPanel)
        # the forged independence flag must NOT light convergence.
        assert len(gates.query(".state-converged")) == 0
        # the ledger integrity badge surfaces the tamper (verify() False -> red).
        ledger = app.query_one(AutonomyLedgerPanel)
        header = ledger.query_one("#autoledger-header")
        assert "fail" in str(header.render()).lower()
        # HONESTY HARDENING (Fix #1): on a tampered ledger (verified False) the
        # per-event status color is NEUTRALIZED — the forged "status: pass" row
        # must NOT render green (.state-pass / no positive class); every event
        # row is dimmed (.state-dim) so nothing reassuring shows next to the
        # loud red integrity badge.
        events = ledger.query(".autoledger-event")
        assert len(events) >= 1, "the forged event row should still render"
        for ev_row in events:
            classes = set(ev_row.classes)
            assert "state-pass" not in classes, (
                "a tampered ledger must not show a green (state-pass) event row"
            )
            assert "state-fail" not in classes, (
                "a tampered ledger neutralizes status color entirely (dim only)"
            )
            assert "state-dim" in classes, (
                "every event row on a tampered ledger is dimmed (state-dim)"
            )


# --------------------------------------------------------------------------- #
# 3. Eskalations-Banner: RED when HALTED.md exists, quiet otherwise             #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_escalation_banner_red_when_halted(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfg, proj = _one_project(tmp_path)
    (proj / ".peers" / "HALTED.md").write_text(
        "# HALTED\nmax ticks reached — operator review required\n")
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(5):
            await pilot.pause(0.3)
        banner = app.query_one(EscalationBannerPanel)
        # the loud red banner is present.
        red = banner.query(".escalation-banner")
        assert len(red) >= 1
        header = banner.query_one("#escalation-header")
        assert "eskaliert" in str(header.render()).lower()
        # the excerpt surfaces the HALTED.md content.
        assert len(banner.query(".escalation-excerpt")) >= 1


# --------------------------------------------------------------------------- #
# 4. layout persistence round-trips through the app                            #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_layout_round_trips_through_app(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfg, _proj = _one_project(tmp_path)
    layout_path = layout_mod.default_layout_path(config_dir=cfg)

    # launch 1: toggle a hidden panel ON (bugs-panel, "6") + an autonomy window
    # ON (escalation, f9), then quit -> layout saved.
    app1 = PeersTuiApp(config_dir=cfg)
    async with app1.run_test() as pilot:
        await pilot.pause()
        assert app1.query_one("#bugs-panel").display is False
        assert app1.query_one("#escalation-panel").display is False
        app1._toggle_panel("bugs-panel")
        app1._toggle_panel("escalation-panel")
        await pilot.pause()
        assert app1.query_one("#bugs-panel").display is True
    # the toggle persisted to disk (saved on toggle + on quit).
    saved = json.loads(layout_path.read_text())
    assert saved["visible"]["bugs-panel"] is True
    assert saved["visible"]["escalation-panel"] is True

    # launch 2: a fresh app restores the persisted visibility.
    app2 = PeersTuiApp(config_dir=cfg)
    async with app2.run_test() as pilot:
        await pilot.pause()
        await pilot.pause(0.2)
        assert app2.query_one("#bugs-panel").display is True
        assert app2.query_one("#escalation-panel").display is True


@pytest.mark.asyncio
async def test_corrupt_layout_does_not_block_launch(tmp_path, monkeypatch):
    # a corrupt layout file must not crash the app — it degrades to the default
    # mission-control layout.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfg, _proj = _one_project(tmp_path)
    layout_path = layout_mod.default_layout_path(config_dir=cfg)
    layout_path.parent.mkdir(parents=True, exist_ok=True)
    layout_path.write_text("{ not json at all")
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        # default mission-control: gates visible, bugs hidden.
        assert app.query_one("#gates-panel").display is True
        assert app.query_one("#bugs-panel").display is False


# --------------------------------------------------------------------------- #
# 4b. DRIFT GUARD: layout.py panel ids stay in lockstep with app.PANEL_SPECS    #
# --------------------------------------------------------------------------- #
def test_layout_panel_ids_lockstep_with_app_specs():
    # The layout module (textual-free) and the app (textual) each carry the set
    # of cockpit panel ids independently. They MUST agree: a panel added to one
    # but not the other would silently break toggle persistence (an unknown id is
    # dropped on save) or leave a panel un-persistable. This test fails the moment
    # the two drift, forcing both to be updated together.
    layout_ids = set(layout_mod.known_panel_ids())
    app_ids = {spec.pid for spec in PANEL_SPECS}
    assert layout_ids == app_ids, (
        "layout.known_panel_ids() and app.PANEL_SPECS ids drifted: "
        f"only in layout={layout_ids - app_ids}, only in app={app_ids - layout_ids}"
    )
    # …and the DEFAULT-VISIBLE sets must agree too: a panel the app starts visible
    # must be visible in the layout default (and vice versa), or a fresh launch
    # and a persisted-default launch would disagree on what's on screen.
    layout_default = layout_mod.default_layout()["visible"]
    layout_visible = {pid for pid, on in layout_default.items() if on}
    app_visible = {spec.pid for spec in PANEL_SPECS if spec.visible_default}
    assert layout_visible == app_visible, (
        "default-visible sets drifted: "
        f"only in layout={layout_visible - app_visible}, "
        f"only in app={app_visible - layout_visible}"
    )


# --------------------------------------------------------------------------- #
# 5. screenshots for visual review                                             #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cockpit_screenshot(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfg, proj = _one_project(tmp_path)
    _seed_converged_ledger(proj)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(6):
            await pilot.pause(0.3)
        # open a couple of autonomy windows for the shot.
        app._toggle_panel("autonomy-ledger-panel")
        app._toggle_panel("spine-gates-panel")
        await pilot.pause()
        await pilot.pause(0.3)
        app.save_screenshot("/tmp/peers-tui-cockpit.svg")


@pytest.mark.asyncio
async def test_escalation_screenshot(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfg, proj = _one_project(tmp_path)
    (proj / ".peers" / "HALTED.md").write_text(
        "# HALTED\nthe system escalates: a blocking gate wedged for 5 ticks\n"
        "operator review required before resume\n")
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(5):
            await pilot.pause(0.3)
        app._toggle_panel("escalation-panel")
        await pilot.pause()
        await pilot.pause(0.3)
        app.save_screenshot("/tmp/peers-tui-escalation.svg")
