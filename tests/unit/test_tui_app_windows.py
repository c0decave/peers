"""Headless pilot tests for the Wave-1b TUI loop-layer windows (Unit G).

These run only under the optional ``[tui]`` extra (Textual + textual-window) — the
default ``.[dev]`` CI has no Textual, so the whole module is skipif-guarded. The
pilots drive ``PeersTuiApp.run_test()`` against a tmp config dir holding a fake
``projects.yaml`` + a project ``.peers/`` with state.json / runs.jsonl /
bugs.jsonl / PLAN.md and assert: each new panel renders the seeded data (and
colors it via the ``.state-*`` CSS classes, incl. a RED ``.state-fail`` and the
attestation badge for a forgery mismatch); toggling a window hides/shows it; a
pop-out creates a ``textual_window.Window``; selecting a tick drives the Diff
panel. One pilot writes a screenshot SVG to /tmp for visual review.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess

import pytest

textual_missing = importlib.util.find_spec("textual") is None
pytestmark = pytest.mark.skipif(
    textual_missing,
    reason="textual extra (peers[tui]) not installed; TUI pilot needs the optional GUI dependency",
)

# Gate the textual-dependent imports so this module imports cleanly (and merely
# SKIPS) when the optional [tui] extra is absent.
if not textual_missing:
    import yaml
    from textual_window import Window

    from peers_ctl.tui.app import PeersTuiApp
    from peers_ctl.tui.panels.budget import BudgetPanel
    from peers_ctl.tui.panels.bugs import BugsPanel
    from peers_ctl.tui.panels.diff import DiffPanel
    from peers_ctl.tui.panels.gates import GatesPanel
    from peers_ctl.tui.panels.log import LogPanel
    from peers_ctl.tui.panels.review import ReviewPanel
    from peers_ctl.tui.panels.tasks import TasksPanel
    from peers_ctl.tui.panels.ticks import TicksPanel


# --------------------------------------------------------------------------- #
# fixtures                                                                     #
# --------------------------------------------------------------------------- #
def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _seed_repo_with_commit(proj):
    """Make the project dir a git repo with one real commit (Peer: trailer)."""
    _git(proj, "init", "-q")
    _git(proj, "config", "user.email", "t@t")
    _git(proj, "config", "user.name", "t")
    _git(proj, "config", "commit.gpgsign", "false")
    (proj / "f.txt").write_text("hello\nworld\n")
    _git(proj, "add", "f.txt")
    _git(proj, "commit", "-q", "-m", "do work\n\nPeer: claude")
    return subprocess.run(["git", "-C", str(proj), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def _populated_config(tmp_path, *, with_repo=True):
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    proj = tmp_path / "proj"
    (proj / ".peers" / "log").mkdir(parents=True, exist_ok=True)
    head = _seed_repo_with_commit(proj) if with_repo else "0" * 40
    (proj / ".peers" / "state.json").write_text(json.dumps({
        "iteration": 5, "mode": "develop",
        "peer_order": ["claude", "claude-2"], "turn_index": 0,
        "goals_status": {
            "tests-pass": {"state": "pass", "duration_ms": 3},
            "no-shortcut": {"state": "fail", "duration_ms": 2, "diagnostic": "hit"},
        },
        "stuck_counter": {"no-shortcut": 2},
        "peers": {"claude": {"state": "healthy"},
                  "claude-2": {"state": "degraded", "consecutive_fails": 2.0}},
        "budget": {"spent_runtime_s": 120, "spent_tokens": 5000, "spent_usd": 0.0,
                   "max_runtime_s": 3600, "max_tokens": 6000,
                   "consecutive_failures": 1,
                   "wasted_runtime_per_tick": [
                       {"iteration": 3, "peer": "claude-2", "duration_s": 40}]},
        "warnings_history": [
            {"ts": "2026-06-11T10:00:00+00:00", "iter": 4, "w": "no-shortcut markers"}],
    }))
    (proj / ".peers" / "PLAN.md").write_text(
        "- [x] [STEP-1] scaffold\n- [ ] [STEP-2] implement\n")
    (proj / ".peers" / "bugs.jsonl").write_text(
        json.dumps({"id": "BUG-1", "severity": "high", "title": "leak",
                    "status": "open"}) + "\n")
    # one real tick whose head_after is the actual commit -> Diff can resolve it.
    (proj / ".peers" / "log" / "runs.jsonl").write_text(
        json.dumps({"ts": "2026-06-11T10:01:00+00:00", "iteration": 5,
                    "peer": "claude", "classification": "handoff",
                    "success": True, "tokens_this_tick": 1200,
                    "head_before": "0" * 40, "head_after": head}) + "\n")
    (cfg / "projects.yaml").write_text(yaml.safe_dump(
        {"projects": [{"name": "proj", "path": str(proj),
                       "state": "running", "pid": 9}]}))
    return cfg, head


async def _settle(pilot, n=6):
    await pilot.pause()
    for _ in range(n):
        await pilot.pause(0.3)


# --------------------------------------------------------------------------- #
# happy: each new panel renders from the seeded snapshot                        #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_tasks_panel_renders_plan_and_bugs(tmp_path):
    cfg, _ = _populated_config(tmp_path)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await _settle(pilot)
        tasks = app.query_one(TasksPanel)
        text = str(tasks.query_one("#tasks-plan-head").render())
        assert "1/2" in text  # one of two PLAN steps done
        # one done (green) + one open (yellow) step row.
        assert len(tasks.query(".state-pass")) >= 1
        assert len(tasks.query(".state-pending")) >= 1


@pytest.mark.asyncio
async def test_budget_panel_colors_and_failures(tmp_path):
    cfg, _ = _populated_config(tmp_path)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await _settle(pilot)
        budget = app.query_one(BudgetPanel)
        # consecutive_failures > 0 reads red.
        fails = budget.query_one("#budget-failures")
        assert fails.has_class("state-fail")


@pytest.mark.asyncio
async def test_ticks_panel_renders_and_selection_drives_diff(tmp_path):
    cfg, head = _populated_config(tmp_path)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await _settle(pilot)
        ticks = app.query_one(TicksPanel)
        from peers_ctl.tui.panels.ticks import TickRow
        rows = list(ticks.query(TickRow))
        assert len(rows) == 1
        # the tick selection should have driven the Diff sha to the real commit.
        assert app._diff_sha == head
        # let the diff worker resolve + paint.
        for _ in range(5):
            await pilot.pause(0.3)
        diff = app.query_one(DiffPanel)
        body_text = " ".join(str(lbl.render()) for lbl in diff.query("Label"))
        assert "f.txt" in body_text  # the commit's diff rendered


@pytest.mark.asyncio
async def test_bugs_panel_highlights_blocking(tmp_path):
    cfg, _ = _populated_config(tmp_path)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        # bugs starts hidden -> toggle it on.
        await _settle(pilot)
        app._toggle_by_key("6")
        await pilot.pause()
        bugs = app.query_one(BugsPanel)
        assert bugs.display
        # an OPEN high-severity bug reads red (.state-fail); header alert set.
        assert len(bugs.query(".state-fail")) >= 1
        assert bugs.query_one("#bugs-header").has_class("state-alert")


@pytest.mark.asyncio
async def test_review_panel_attestation_badge_mismatch(tmp_path):
    # edge: a peers-attest note that DISAGREES with the Peer: trailer is the
    # forgery signal -> the badge reads RED (.state-fail).
    cfg, head = _populated_config(tmp_path)
    proj = tmp_path / "proj"
    # attest the (root) commit to a DIFFERENT peer than the trailer ("claude") ->
    # a substrate-attested mismatch = the forgery signal. The commit is a root
    # (no parent) so we write the peers-attest note directly rather than via
    # attest_commits (which needs a since_sha range).
    _git(proj, "notes", "--ref=peers-attest", "add", "-f", "-m", "codex", head)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await _settle(pilot)
        app._toggle_by_key("7")  # show Konsens
        await pilot.pause()
        for _ in range(4):
            await pilot.pause(0.3)
        review = app.query_one(ReviewPanel)
        badges = review.query(".review-badge")
        texts = [str(b.render()) for b in badges]
        assert any("MISMATCH" in t for t in texts), texts
        assert len(review.query(".state-fail")) >= 1


@pytest.mark.asyncio
async def test_log_panel_renders_warning(tmp_path):
    cfg, _ = _populated_config(tmp_path)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await _settle(pilot)
        app._toggle_by_key("8")  # show Log
        await pilot.pause()
        log = app.query_one(LogPanel)
        body_text = " ".join(str(lbl.render()) for lbl in log.query("Label"))
        assert "no-shortcut markers" in body_text


# --------------------------------------------------------------------------- #
# window toggling                                                              #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_toggle_hides_and_shows_panel(tmp_path):
    cfg, _ = _populated_config(tmp_path)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await _settle(pilot)
        gates = app.query_one(GatesPanel)
        assert gates.display  # visible by default
        app._toggle_by_key("1")  # hide
        await pilot.pause()
        assert not gates.display
        app._toggle_by_key("1")  # show again
        await pilot.pause()
        assert gates.display


# --------------------------------------------------------------------------- #
# pop-out: creates a textual_window.Window; closing removes it                  #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_popout_creates_window_and_close_removes_it(tmp_path):
    cfg, _ = _populated_config(tmp_path)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await _settle(pilot)
        assert list(app.query(Window)) == []  # none floating yet
        app.query_one(GatesPanel).focus()
        await pilot.pause()
        app.action_popout()
        await pilot.pause()
        await pilot.pause()
        wins = list(app.query(Window))
        assert len(wins) == 1
        assert wins[0].id == "win-gates-panel"
        # the tiled twin is hidden while the float is up.
        assert not app.query_one("#gates-panel").display
        # close it -> Window gone, tiled twin restored.
        app.action_close_window()
        await pilot.pause()
        await pilot.pause()
        assert list(app.query(Window)) == []
        assert app.query_one("#gates-panel").display


@pytest.mark.asyncio
async def test_popout_repaints_content(tmp_path):
    cfg, _ = _populated_config(tmp_path)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await _settle(pilot)
        app.query_one(BugsPanel)  # ensure mounted
        app._toggle_by_key("6")
        await pilot.pause()
        app.query_one(BugsPanel).focus()
        await pilot.pause()
        app.action_popout()
        await pilot.pause()
        await pilot.pause()
        # the floating twin holds a BugsPanel with the bug rendered.
        # NOTE: textual_window.Window does NOT set the underlying Textual DOM id
        # (it overrides `.id` as a property), so query by `.id` value, not `#id`.
        win = next(w for w in app.query(Window) if w.id == "win-bugs-panel")
        twin = win.query_one(BugsPanel)
        body_text = " ".join(str(lbl.render()) for lbl in twin.query("Label"))
        assert "BUG-1" in body_text


# --------------------------------------------------------------------------- #
# sad: a run with no state still renders empty-states (no crash)               #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_empty_run_renders_empty_states(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True)
    proj = tmp_path / "proj"  # no .peers at all
    proj.mkdir()
    (cfg / "projects.yaml").write_text(yaml.safe_dump(
        {"projects": [{"name": "proj", "path": str(proj),
                       "state": "fresh", "pid": None}]}))
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await _settle(pilot)
        tasks = app.query_one(TasksPanel)
        assert "no live state" in str(tasks.border_title)
        assert tasks.query(".empty-state")


# --------------------------------------------------------------------------- #
# screenshot artifact (to /tmp, NOT committed)                                 #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_screenshot_artifact(tmp_path):
    cfg, _ = _populated_config(tmp_path)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await _settle(pilot)
        # show a couple of the hidden windows so the shot is rich.
        app._toggle_by_key("6")  # bugs
        app._toggle_by_key("7")  # konsens
        await pilot.pause()
        for _ in range(3):
            await pilot.pause(0.3)
        out = app.save_screenshot("/tmp/peers-tui-windows.svg")
        assert out
    import os
    assert os.path.exists("/tmp/peers-tui-windows.svg")
