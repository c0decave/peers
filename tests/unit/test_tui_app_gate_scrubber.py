"""Headless pilots for Wave-2 §5.2: the Gates window gate-history scrubber.

Skipif-guarded (the default ``.[dev]`` CI has no Textual). The pilots seed a
``runs.jsonl`` carrying per-tick ``gates`` snapshots and drive
``PeersTuiApp.run_test()`` to assert:

  * the live view (current ``state.json`` gates) is the DEFAULT;
  * pressing ``[`` enters history at the newest snapshot and shows the historical
    gate stand + the time display (tick + absolute ts + relative);
  * pressing ``[`` again steps further back; ``]`` steps forward and past the
    newest snapshot returns to live; ``\\`` jumps back to live;
  * a project WITHOUT gate snapshots (pre-Wave-2 runs.jsonl) leaves the scrubber
    disabled (no history) — the keys are no-ops, the live view stays.

There is also a default-python (textual-free) unit test for the scrubber state
machine that does NOT need a mounted panel.
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

if not textual_missing:
    import yaml

    from peers_ctl.tui.app import PeersTuiApp
    from peers_ctl.tui.panels.gates import GatesPanel


# --------------------------------------------------------------------------- #
# fixtures                                                                     #
# --------------------------------------------------------------------------- #
def _seed(tmp_path, *, with_snapshots=True):
    """A config dir + a project whose runs.jsonl carries (or omits) gate
    snapshots, plus a live state.json with current gates."""
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    proj = tmp_path / "proj"
    log = proj / ".peers" / "log"
    log.mkdir(parents=True, exist_ok=True)
    # Live state.json: tests currently PASS (the live view).
    (proj / ".peers" / "state.json").write_text(json.dumps({
        "iteration": 3, "mode": "develop",
        "peer_order": ["claude"], "turn_index": 0,
        "goals_status": {"tests": {"state": "pass", "duration_ms": 1}},
        "peers": {"claude": {"state": "healthy"}},
        "budget": {"spent_runtime_s": 90, "spent_tokens": 100, "spent_usd": 0.0,
                   "max_runtime_s": 3600},
    }))
    lines = []
    if with_snapshots:
        # tick 1: tests FAIL; tick 2: tests PASS. Distinct from the live state.
        lines.append(json.dumps({
            "ts": "2026-06-11T00:00:00+00:00", "iteration": 1, "peer": "claude",
            "classification": "success", "success": False,
            "gates": {"hard": {"tests": "fail"}, "soft": {"review": "0/2"}},
        }))
        lines.append(json.dumps({
            "ts": "2026-06-11T00:00:30+00:00", "iteration": 2, "peer": "claude",
            "classification": "success", "success": True,
            "gates": {"hard": {"tests": "pass"}, "soft": {"review": "2/2"}},
        }))
    else:
        # pre-Wave-2: a tick line WITHOUT any `gates` field.
        lines.append(json.dumps({
            "ts": "2026-06-11T00:00:00+00:00", "iteration": 1, "peer": "claude",
            "classification": "success", "success": True,
        }))
    (log / "runs.jsonl").write_text("\n".join(lines) + "\n")
    (cfg / "projects.yaml").write_text(yaml.safe_dump(
        {"projects": [{"name": "proj", "path": str(proj),
                       "state": "running", "pid": 9}]}))
    return cfg, proj


async def _settle(pilot, n=6):
    await pilot.pause()
    for _ in range(n):
        await pilot.pause(0.2)


def _gates(app):
    return app.query_one(GatesPanel)


def _header(app):
    return str(app.query_one("#gates-header").render())


def _body_text(app):
    body = app.query_one("#gates-body")
    return "\n".join(str(c.render()) for c in body.children)


# --------------------------------------------------------------------------- #
# happy: default = live; [ enters history; time display shown                  #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_live_is_default_then_scrub_back_shows_history(tmp_path):
    cfg, _ = _seed(tmp_path, with_snapshots=True)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await _settle(pilot)
        gates = _gates(app)
        # Default = LIVE: not scrubbing; live view shows tests PASS.
        assert gates.scrubbing is False
        assert "pass" in _body_text(app)
        # Focus the Gates window, then scrub one tick back -> newest snapshot
        # (tick 2: tests pass, review reached).
        app.set_focus(gates)
        await pilot.press("left_square_bracket")
        await pilot.pause()
        assert gates.scrubbing is True
        header = _header(app)
        assert "HISTORY" in header
        assert "tick 2" in header
        # absolute ts + relative ("vor ...") time display in the header.
        assert "2026-06-11" in header
        assert "vor" in header
        # Step further back -> tick 1 (tests FAIL).
        await pilot.press("left_square_bracket")
        await pilot.pause()
        assert "tick 1" in _header(app)
        assert "fail" in _body_text(app)


@pytest.mark.asyncio
async def test_scrub_forward_returns_to_live(tmp_path):
    cfg, _ = _seed(tmp_path, with_snapshots=True)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await _settle(pilot)
        gates = _gates(app)
        app.set_focus(gates)
        await pilot.press("left_square_bracket")  # enter history (newest)
        await pilot.pause()
        assert gates.scrubbing is True
        # Stepping forward past the newest snapshot returns to LIVE.
        await pilot.press("right_square_bracket")
        await pilot.pause()
        assert gates.scrubbing is False
        assert "HISTORY" not in _header(app)


@pytest.mark.asyncio
async def test_backslash_jumps_back_to_live(tmp_path):
    cfg, _ = _seed(tmp_path, with_snapshots=True)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await _settle(pilot)
        gates = _gates(app)
        app.set_focus(gates)
        await pilot.press("left_square_bracket")
        await pilot.press("left_square_bracket")  # tick 1
        await pilot.pause()
        assert gates.scrubbing is True
        await pilot.press("backslash")  # back to live
        await pilot.pause()
        assert gates.scrubbing is False
        assert "HISTORY" not in _header(app)


# --------------------------------------------------------------------------- #
# sad: no snapshots -> scrubber disabled, keys are no-ops                       #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_no_history_scrubber_is_noop(tmp_path):
    cfg, _ = _seed(tmp_path, with_snapshots=False)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await _settle(pilot)
        gates = _gates(app)
        app.set_focus(gates)
        await pilot.press("left_square_bracket")  # no history -> no-op
        await pilot.pause()
        assert gates.scrubbing is False
        assert "HISTORY" not in _header(app)


# --------------------------------------------------------------------------- #
# edge (textual-free): the scrub state machine without a mounted panel          #
# --------------------------------------------------------------------------- #
def test_scrub_state_machine_clamps_and_wraps_to_live():
    from peers_ctl.tui.snapshots import GateSnapshotRow

    panel = GatesPanel()
    rows = [
        GateSnapshotRow(iteration=1, ts="t1",
                        gates={"hard": {"a": "fail"}}, green=0, total=1, gap_s=None),
        GateSnapshotRow(iteration=2, ts="t2",
                        gates={"hard": {"a": "pass"}}, green=1, total=1, gap_s=10.0),
    ]
    panel.set_history(rows)
    assert panel.scrubbing is False
    # back -> newest (index 1); back again -> oldest (index 0); back -> no-op.
    assert panel.scrub_back() is True and panel._scrub_index == 1
    assert panel.scrub_back() is True and panel._scrub_index == 0
    assert panel.scrub_back() is False and panel._scrub_index == 0
    # forward -> index 1; forward past newest -> live (None).
    assert panel.scrub_forward() is True and panel._scrub_index == 1
    assert panel.scrub_forward() is True and panel._scrub_index is None
    # forward while live -> no-op.
    assert panel.scrub_forward() is False
    # scrub_live from live -> no-op; from history -> True.
    assert panel.scrub_live() is False
    panel.scrub_back()
    assert panel.scrub_live() is True and panel.scrubbing is False


def test_background_poll_does_not_yank_operator_off_inspected_tick():
    """Fix #5 (poll-doesn't-yank): while the operator is scrubbing a PAST tick,
    a background poll (``render_gates`` with fresh live data + the same history)
    must NOT pull them back to live — ``scrubbing`` stays True and the displayed
    historical tick (the scrub index) is preserved.

    Textual-free: ``render_gates``'s ``_render_history`` repaint touches the
    mounted widgets via ``query_one`` (which raises on an unmounted panel and is
    swallowed), so the SCRUB STATE MACHINE still runs and the index is the only
    thing under test here."""
    from peers_ctl.tui.snapshots import GateSnapshotRow, GateView

    panel = GatesPanel()
    history = [
        GateSnapshotRow(iteration=1, ts="t1", gates={"hard": {"a": "fail"}},
                        green=0, total=1, gap_s=None),
        GateSnapshotRow(iteration=2, ts="t2", gates={"hard": {"a": "pass"}},
                        green=1, total=1, gap_s=10.0),
        GateSnapshotRow(iteration=3, ts="t3", gates={"hard": {"a": "fail"}},
                        green=0, total=1, gap_s=10.0),
    ]
    # Seed live + history, then scrub back twice -> inspecting tick 2 (index 1).
    panel.render_gates([GateView(
        id="a", kind="hard", state="pass", stuck=0, duration_ms=1,
        diagnostic="", cached=False, consensus=None)], None, history)
    assert panel.scrubbing is False
    panel.scrub_back()                 # -> newest (index 2)
    panel.scrub_back()                 # -> index 1 (the inspected tick)
    assert panel.scrubbing is True
    assert panel._scrub_index == 1

    # A background poll arrives: NEW live gates (now failing) + a GROWN history
    # (a 4th tick appended). This must NOT move the operator.
    grown_history = history + [
        GateSnapshotRow(iteration=4, ts="t4", gates={"hard": {"a": "pass"}},
                        green=1, total=1, gap_s=10.0),
    ]
    panel.render_gates([GateView(
        id="a", kind="hard", state="fail", stuck=2, duration_ms=5,
        diagnostic="now red", cached=False, consensus=None)], None, grown_history)

    # Still scrubbing, still on the SAME historical tick (index 1, iteration 2) —
    # the poll updated the live snapshot underneath but did not yank the view.
    assert panel.scrubbing is True
    assert panel._scrub_index == 1
    assert panel._history[panel._scrub_index].iteration == 2
    # The fresh live data WAS stored (so a later return-to-live repaints it).
    assert panel._live_gates[0].state == "fail"


def test_scrub_index_reclamped_when_history_shrinks():
    from peers_ctl.tui.snapshots import GateSnapshotRow

    panel = GatesPanel()
    panel.set_history([
        GateSnapshotRow(iteration=i, ts=f"t{i}", gates={"hard": {"a": "pass"}},
                        green=1, total=1, gap_s=None)
        for i in range(5)
    ])
    panel.scrub_back()  # index 4 (newest)
    assert panel._scrub_index == 4
    # History shrinks to 2 rows -> index re-clamped to the new last (1).
    panel.set_history([
        GateSnapshotRow(iteration=i, ts=f"t{i}", gates={"hard": {"a": "pass"}},
                        green=1, total=1, gap_s=None)
        for i in range(2)
    ])
    assert panel._scrub_index == 1
    # History empties -> scrub index falls back to live (None).
    panel.set_history([])
    assert panel._scrub_index is None
