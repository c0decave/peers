"""Headless pilot tests for the Wave-1b TUI app skeleton (Unit F).

These run only under the optional ``[tui]`` extra (Textual) — the default
``.[dev]`` CI has no Textual, so the whole module is skipif-guarded. The pilots
drive ``PeersTuiApp.run_test()`` against a tmp config dir holding a fake
``projects.yaml`` + a project ``.peers/state.json`` and assert the panels render
the seeded data (and color it via the ``.state-*`` CSS classes), that selecting a
fleet row switches the active run, and that empty/malformed inputs degrade safely.
One pilot writes a screenshot SVG to /tmp for visual review.
"""
from __future__ import annotations

import importlib.util
import json

import pytest

textual_missing = importlib.util.find_spec("textual") is None
pytestmark = pytest.mark.skipif(
    textual_missing,
    reason="textual extra (peers[tui]) not installed; TUI pilot needs the optional GUI dependency",
)

# Gate the textual-dependent imports so this module imports cleanly (and merely
# SKIPS) when the optional [tui] extra is absent — otherwise a module-top import
# would raise ModuleNotFoundError at COLLECTION time and abort the whole
# default-CI ``tests/unit`` run before the skipif marker can take effect.
if not textual_missing:
    import yaml

    from peers_ctl.tui.app import PeersTuiApp
    from peers_ctl.tui.panels.fleet import FleetPanel, FleetRow
    from peers_ctl.tui.panels.gates import GatesPanel
    from peers_ctl.tui.panels.peers import PeersPanel


# --------------------------------------------------------------------------- #
# fixtures: a tmp config dir with a fake registry + state.json                 #
# --------------------------------------------------------------------------- #
def _write_registry(config_dir, projects):
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "projects.yaml").write_text(
        yaml.safe_dump({"projects": projects}, sort_keys=False)
    )


def _seed_state(project_dir, state):
    (project_dir / ".peers").mkdir(parents=True, exist_ok=True)
    (project_dir / ".peers" / "state.json").write_text(json.dumps(state))


def _populated_config(tmp_path):
    """Two projects; proj-alpha has live state (gates+peers+budget), proj-beta fresh."""
    cfg = tmp_path / "config"
    alpha = tmp_path / "proj-alpha"
    beta = tmp_path / "proj-beta"
    beta.mkdir(parents=True, exist_ok=True)
    _seed_state(alpha, {
        "iteration": 9,
        "mode": "develop",
        "peer_order": ["claude", "claude-2"],
        "turn_index": 0,
        "goals_status": {
            "tests-pass": {"state": "pass", "duration_ms": 12},
            "no-shortcut": {"state": "fail", "duration_ms": 3, "diagnostic": "marker found"},
        },
        "stuck_counter": {"no-shortcut": 2},
        "soft_status": {"architecture": {"consensus_count": 1}},
        "peers": {
            "claude": {"state": "healthy", "consecutive_fails": 0.0,
                       "recent_runs": [True, True, 0.5]},
            "claude-2": {"state": "degraded", "consecutive_fails": 2.0,
                         "recent_runs": [False, True]},
        },
        "budget": {"spent_runtime_s": 120, "spent_tokens": 5000, "spent_usd": 0.0},
    })
    _write_registry(cfg, [
        {"name": "proj-alpha", "path": str(alpha), "state": "running", "pid": 4242},
        {"name": "proj-beta", "path": str(beta), "state": "fresh", "pid": None},
    ])
    return cfg


# --------------------------------------------------------------------------- #
# happy: populated fleet renders, gates/peers colored, selection switches run  #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_fleet_lists_seeded_projects(tmp_path):
    cfg = _populated_config(tmp_path)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        fleet = app.query_one(FleetPanel)
        rows = list(fleet.query(FleetRow))
        names = {r.entry.name for r in rows}
        assert names == {"proj-alpha", "proj-beta"}


@pytest.mark.asyncio
async def test_gates_panel_shows_states_with_css_classes(tmp_path):
    cfg = _populated_config(tmp_path)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        # let the fleet paint + the first snapshot worker complete + paint.
        await pilot.pause()
        for _ in range(5):
            await pilot.pause(0.3)
        gates = app.query_one(GatesPanel)
        # a failing+stuck gate reads RED (.state-fail) with stuck as an ADDITIVE
        # emphasis marker (.gate-stuck) that does not override the color; the
        # passing gate reads green (.state-pass).
        fail_nodes = gates.query(".state-fail")
        stuck_nodes = gates.query(".gate-stuck")
        pass_nodes = gates.query(".state-pass")
        assert len(fail_nodes) >= 1, "expected the failing gate to read red (.state-fail)"
        assert len(stuck_nodes) >= 1, "expected the stuck gate to carry the .gate-stuck marker"
        assert len(pass_nodes) >= 1, "expected the passing gate to be marked"


@pytest.mark.asyncio
async def test_peers_panel_shows_health(tmp_path):
    cfg = _populated_config(tmp_path)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(5):
            await pilot.pause(0.3)
        peers = app.query_one(PeersPanel)
        # the current peer (claude, turn_index 0) reads cyan; the degraded one yellow.
        assert len(peers.query(".state-current")) >= 1
        assert len(peers.query(".state-degraded")) >= 1


@pytest.mark.asyncio
async def test_selecting_row_updates_active_run(tmp_path):
    cfg = _populated_config(tmp_path)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(3):
            await pilot.pause(0.3)
        # first row (proj-alpha, has state) is active by default.
        assert app._active_name == "proj-alpha"
        # move selection to proj-beta -> active run switches.
        app.action_fleet_down()
        await pilot.pause()
        for _ in range(3):
            await pilot.pause(0.3)
        assert app._active_name == "proj-beta"
        # proj-beta has no state.json -> cockpit shows the "no live state" panels.
        gates = app.query_one(GatesPanel)
        assert "no live state" in str(gates.border_title)


# --------------------------------------------------------------------------- #
# sad: empty registry -> friendly empty-state, no crash                        #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_empty_registry_shows_empty_state(tmp_path):
    cfg = tmp_path / "empty-config"  # no projects.yaml at all
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(3):
            await pilot.pause(0.2)
        fleet = app.query_one(FleetPanel)
        assert list(fleet.query(FleetRow)) == []
        empty = fleet.query_one("#fleet-empty")
        assert empty.display is True
        assert app._active_name is None  # nothing to select -> no crash


# --------------------------------------------------------------------------- #
# edge: malformed state.json -> panels show safe defaults, no crash            #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_malformed_state_renders_safe_defaults(tmp_path):
    cfg = tmp_path / "config"
    broken = tmp_path / "proj-broken"
    (broken / ".peers").mkdir(parents=True)
    (broken / ".peers" / "state.json").write_text("{ this is not json")
    _write_registry(cfg, [
        {"name": "proj-broken", "path": str(broken), "state": "running", "pid": 7},
    ])
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(4):
            await pilot.pause(0.3)
        # the fleet still lists it; the cockpit shows "no live state" (state unreadable).
        fleet = app.query_one(FleetPanel)
        assert {r.entry.name for r in fleet.query(FleetRow)} == {"proj-broken"}
        gates = app.query_one(GatesPanel)
        peers = app.query_one(PeersPanel)
        assert "no live state" in str(gates.border_title)
        # empty-state placeholders present, no crash.
        assert gates.query(".empty-state")
        assert peers.query(".empty-state")


# --------------------------------------------------------------------------- #
# screenshot artifact (to /tmp, NOT committed)                                 #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_screenshot_artifact(tmp_path):
    cfg = _populated_config(tmp_path)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(5):
            await pilot.pause(0.3)
        out = app.save_screenshot("/tmp/peers-tui-skeleton.svg")
        assert out
    import os
    assert os.path.exists("/tmp/peers-tui-skeleton.svg")
