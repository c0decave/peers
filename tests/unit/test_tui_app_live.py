"""Headless pilots for Wave-1b Unit H: the Live-Stream window + keybindings + help.

Skipif-guarded (the default ``.[dev]`` CI has no Textual). The pilots drive
``PeersTuiApp.run_test()`` and assert:

  * the Live panel shows decoded, colored lines from a FAKE stream source (a
    fixture script emitting claude session-jsonl lines) — injected via the app's
    ``_stream_source_factory`` test hook so no real ``peers-ctl peek`` is needed;
  * closing the Live window KILLS the streaming subprocess (the process is gone);
  * `?` opens the modal HelpScreen;
  * the full keymap bindings are registered on the app;
  * a non-claude peer renders the honest tick-level hint.

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

if not textual_missing:
    import sys

    import yaml

    from peers_ctl.tui.app import (
        _LIVE_EOF_RELAUNCH_BACKOFF_S as _LIVE_BACKOFF_S,
    )
    from peers_ctl.tui.app import PeersTuiApp
    from peers_ctl.tui.panels.live import LivePanel
    from peers_ctl.tui.screens.help import HelpScreen


# --------------------------------------------------------------------------- #
# fixtures                                                                     #
# --------------------------------------------------------------------------- #
def _populated_config(tmp_path, *, peer="claude", peer_tool="claude"):
    """A config dir + a project whose state.json names an active peer."""
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    proj = tmp_path / "proj"
    (proj / ".peers" / "log" / "peers").mkdir(parents=True, exist_ok=True)
    (proj / ".peers" / "state.json").write_text(json.dumps({
        "iteration": 5, "mode": "develop",
        "peer_order": [peer], "turn_index": 0,
        "goals_status": {"tests-pass": {"state": "pass", "duration_ms": 1}},
        "peers": {peer: {"state": "healthy"}},
        "budget": {"spent_runtime_s": 90, "spent_tokens": 100, "spent_usd": 0.0,
                   "max_runtime_s": 3600},
    }))
    (proj / ".peers" / "config.yaml").write_text(
        f"peers:\n  - name: {peer}\n    tool: {peer_tool}\n")
    (cfg / "projects.yaml").write_text(yaml.safe_dump(
        {"projects": [{"name": "proj", "path": str(proj),
                       "state": "running", "pid": 9}]}))
    return cfg, proj


#: a fixture stream script: emit N claude session-jsonl TEXT events then exit.
_CLAUDE_EVENTS_SCRIPT = (
    "import sys, json\n"
    "for i in range(3):\n"
    "    print(json.dumps({'type':'assistant',"
    "'timestamp':'2026-06-11T10:00:0%dZ' % i,"
    "'message':{'content':[{'type':'text','text':'live line %d' % i}]}}))\n"
    "    sys.stdout.flush()\n"
)

#: a fixture stream script that stays alive (so close() has a live proc to kill).
_FOREVER_SCRIPT = (
    "import sys, json, time\n"
    "print(json.dumps({'type':'assistant','timestamp':'2026-06-11T10:00:00Z',"
    "'message':{'content':[{'type':'text','text':'alive'}]}}))\n"
    "sys.stdout.flush()\n"
    "time.sleep(60)\n"
)


def _fake_source(script):
    """Build a ``_stream_source_factory`` that runs ``script`` as a claude source."""
    def factory(name, path, peer, tool):  # noqa: ARG001
        return ([sys.executable, "-u", "-c", script], None, True)
    return factory


async def _settle(pilot, n=8):
    await pilot.pause()
    for _ in range(n):
        await pilot.pause(0.3)


# --------------------------------------------------------------------------- #
# happy: the Live panel shows decoded lines from a fake stream source           #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_live_panel_shows_decoded_lines(tmp_path):
    cfg, _ = _populated_config(tmp_path)
    app = PeersTuiApp(config_dir=cfg)
    app._stream_source_factory = _fake_source(_CLAUDE_EVENTS_SCRIPT)
    async with app.run_test() as pilot:
        await _settle(pilot)
        live = app.query_one(LivePanel)
        from textual.widgets import Label
        body_text = " ".join(str(lbl.render()) for lbl in live.query(Label))
        assert "live line 0" in body_text
        assert "live line 2" in body_text
        # claude rows decode to TEXT and color as .live-text.
        assert len(live.query(".live-text")) >= 1
        # header announces genuine liveness for a claude peer.
        header = str(app.query_one("#live-header").render())
        assert "peer claude" in header


@pytest.mark.asyncio
async def test_live_header_idle_timer_and_runtime(tmp_path):
    cfg, _ = _populated_config(tmp_path)
    app = PeersTuiApp(config_dir=cfg)
    app._stream_source_factory = _fake_source(_CLAUDE_EVENTS_SCRIPT)
    async with app.run_test() as pilot:
        await _settle(pilot)
        header = str(app.query_one("#live-header").render())
        # runtime is rendered from budget.spent_runtime_s (90s -> "1m30s").
        assert "runtime" in header
        # idle-timer present (working / idle Ns).
        assert ("working" in header) or ("idle" in header)


# --------------------------------------------------------------------------- #
# honesty: a codex/opencode peer renders the tick-level hint                     #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_live_non_claude_peer_shows_tick_level_hint(tmp_path):
    cfg, proj = _populated_config(tmp_path, peer="gpt", peer_tool="codex")
    # seed a completed per-tick stdout log so the tail source resolves.
    (proj / ".peers" / "log" / "peers" / "tick-00005-gpt.stdout.log").write_text(
        json.dumps({"type": "turn.completed"}) + "\n")
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await _settle(pilot)
        hint = str(app.query_one("#live-hint").render())
        assert "tick-level" in hint
        assert "Wave 2" in hint


# --------------------------------------------------------------------------- #
# Wave-2 §5.1: the unified live tee is preferred for ALL peers                  #
# --------------------------------------------------------------------------- #
def test_live_source_prefers_tee_for_codex(tmp_path):
    """happy: when a tick-*-<peer>.stream.jsonl exists, `_live_source` tails IT
    (genuinely live) for codex — not the tick-level stdout log."""
    cfg, proj = _populated_config(tmp_path, peer="gpt", peer_tool="codex")
    peers_log = proj / ".peers" / "log" / "peers"
    (peers_log / "tick-00006-gpt.stdout.log").write_text("old tick log\n")
    tee = peers_log / "tick-00006-gpt.stream.jsonl"
    tee.write_text('{"type":"assistant"}\n')
    app = PeersTuiApp(config_dir=cfg)
    argv, _cwd, live = app._live_source("proj", str(proj), "gpt", "codex")
    assert argv is not None and argv[0] == "tail"
    assert str(tee) in argv          # tails the TEE, not the stdout log
    assert live is True              # genuinely live for codex now


def test_live_source_tee_preferred_over_claude_peek(tmp_path):
    """happy: a claude peer with a tee file tails the tee, not `peers-ctl peek`."""
    cfg, proj = _populated_config(tmp_path, peer="claude", peer_tool="claude")
    tee = proj / ".peers" / "log" / "peers" / "tick-00007-claude.stream.jsonl"
    tee.write_text('{"type":"assistant"}\n')
    app = PeersTuiApp(config_dir=cfg)
    argv, _cwd, live = app._live_source("proj", str(proj), "claude", "claude")
    assert argv[0] == "tail"
    assert str(tee) in argv
    assert "peek" not in argv
    assert live is True


def test_live_source_falls_back_to_peek_without_tee(tmp_path):
    """fallback: no tee + claude -> the legacy `peers-ctl peek` source."""
    cfg, proj = _populated_config(tmp_path, peer="claude", peer_tool="claude")
    app = PeersTuiApp(config_dir=cfg)
    argv, _cwd, live = app._live_source("proj", str(proj), "claude", "claude")
    assert "peek" in argv
    assert live is True


# --------------------------------------------------------------------------- #
# edge: closing the Live window KILLS the streaming subprocess (no leak)         #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_closing_live_kills_subprocess(tmp_path):
    cfg, _ = _populated_config(tmp_path)
    app = PeersTuiApp(config_dir=cfg)
    app._stream_source_factory = _fake_source(_FOREVER_SCRIPT)
    async with app.run_test() as pilot:
        await _settle(pilot)
        # the stream subprocess must be live now.
        assert app._stream is not None
        assert app._stream.is_running()
        pid = app._stream.pid
        assert pid is not None
        # hide the Live panel (toggle 'p' off) -> the stream is stopped+killed.
        app._toggle_by_key("p")
        await pilot.pause()
        await pilot.pause()
        assert app._stream is None
        # the OS process is gone.
        import os
        gone = False
        for _ in range(40):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                gone = True
                break
            await pilot.pause(0.1)
        assert gone, f"stream pid {pid} still alive after closing Live window"


@pytest.mark.asyncio
async def test_app_exit_kills_subprocess(tmp_path):
    cfg, _ = _populated_config(tmp_path)
    app = PeersTuiApp(config_dir=cfg)
    app._stream_source_factory = _fake_source(_FOREVER_SCRIPT)
    pid = None
    async with app.run_test() as pilot:
        await _settle(pilot)
        assert app._stream is not None and app._stream.is_running()
        pid = app._stream.pid
    # after the app context exits (on_unmount), the subprocess is killed.
    assert pid is not None
    import os
    import time
    gone = False
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            gone = True
            break
        time.sleep(0.1)
    assert gone, f"stream pid {pid} leaked past app exit"


@pytest.mark.asyncio
async def test_live_relaunches_after_natural_eof(tmp_path):
    """Fix #3: when the stream EOFs on its own (the followed source exits /
    rotated away) the guard must NOT keep the dead handle — a subsequent
    evaluate (same run+peer key) relaunches it so the peer doesn't go silent."""
    cfg, _ = _populated_config(tmp_path)
    app = PeersTuiApp(config_dir=cfg)
    # this source emits 3 lines then EXITS -> the handle EOFs on its own.
    app._stream_source_factory = _fake_source(_CLAUDE_EVENTS_SCRIPT)
    async with app.run_test() as pilot:
        await _settle(pilot)
        assert app._stream is not None
        old_pid = app._stream.pid
        old_key = app._stream_key
        assert old_key is not None
        # wait for the short script's process to exit on its own (natural EOF).
        eofed = False
        for _ in range(60):
            if not app._stream.is_running():
                eofed = True
                break
            await pilot.pause(0.1)
        assert eofed, "fake stream never EOF'd on its own"
        # swap to a long-lived source so the RELAUNCHED handle stays running
        # long enough to observe (the run+peer key is unchanged on purpose).
        app._stream_source_factory = _fake_source(_FOREVER_SCRIPT)
        # the key is unchanged, but the handle is dead -> re-evaluate must fall
        # through the guard and relaunch a fresh, RUNNING handle.
        app._evaluate_live_stream()
        await pilot.pause()
        assert app._stream is not None
        assert app._stream_key == old_key  # same run+peer, deliberately
        assert app._stream.is_running(), "dead handle was not relaunched"
        assert app._stream.pid != old_pid, "expected a fresh subprocess"


# --------------------------------------------------------------------------- #
# Fix 3: a codex peer WITH a tee renders the live hint and does NOT flip       #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_codex_tee_renders_live_and_does_not_flip(tmp_path):
    """Fix #3: a codex peer with a live tee is genuinely live. The stream key
    must store the source's ACTUAL liveness (src_live), so the initial paint AND
    the 0.4s pump-loop paint both render the 'live' hint — not a flip back to the
    'Wave 2 tick-level' hint within 0.4s."""
    cfg, proj = _populated_config(tmp_path, peer="gpt", peer_tool="codex")
    tee = proj / ".peers" / "log" / "peers" / "tick-00005-gpt.stream.jsonl"
    tee.write_text(json.dumps({"type": "assistant"}) + "\n")
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await _settle(pilot)
        # the stream key's 4th element (liveness) reflects the TEE source = True.
        assert app._stream_key is not None
        assert app._stream_key[3] is True, app._stream_key
        hint = str(app.query_one("#live-hint").render())
        assert "live (codex)" in hint, hint
        assert "Wave 2" not in hint
        # force a pump (the loop repaints the header from key.live every 0.4s) and
        # assert the hint did NOT flip to the tick-level / Wave-2 message.
        app._pump_live()
        await pilot.pause()
        hint2 = str(app.query_one("#live-hint").render())
        assert "live (codex)" in hint2, hint2
        assert "Wave 2" not in hint2


# --------------------------------------------------------------------------- #
# Fix 4: relaunch backoff for the EOF-prone peek source                         #
# --------------------------------------------------------------------------- #
def test_is_eof_prone_source_classifies_peek_only():
    """Fix #4 guard (textual-free): only the legacy `peek` argv is EOF-prone;
    a `tail -F` source (tee/tick-log) is not throttled by the backoff."""
    assert PeersTuiApp._is_eof_prone_source(
        [sys.executable, "-m", "peers_ctl", "peek", "proj"]) is True
    assert PeersTuiApp._is_eof_prone_source(
        ["tail", "-n", "+1", "-F", "/x/tick-1-p.stream.jsonl"]) is False
    assert PeersTuiApp._is_eof_prone_source(None) is False
    assert PeersTuiApp._is_eof_prone_source([]) is False


@pytest.mark.asyncio
async def test_peek_eof_relaunch_is_backed_off(tmp_path):
    """Fix #4: the legacy claude `peek` source can EOF on its own. A same-key
    re-evaluate within the backoff window must NOT re-spawn it (churn). A
    `peek`-shaped fake source that exits immediately drives the path.

    Deterministic: we pin ``_stream_eof_at`` to 'just now' (inside the 3s window)
    so the assertion never races real wall-clock against the backoff."""
    import time as _time

    cfg, _ = _populated_config(tmp_path)
    app = PeersTuiApp(config_dir=cfg)

    # a fake source whose argv CONTAINS "peek" (so it's classified EOF-prone) and
    # which exits immediately (natural EOF).
    def peek_like_source(name, path, peer, tool):  # noqa: ARG001
        return ([sys.executable, "-u", "-c", "pass", "peek"], None, True)

    app._stream_source_factory = peek_like_source
    async with app.run_test() as pilot:
        await _settle(pilot)
        # wait for the immediate-exit source to EOF.
        for _ in range(40):
            if app._stream is None or not app._stream.is_running():
                break
            await pilot.pause(0.1)
        dead_pid = app._stream.pid if app._stream else None
        # pin the EOF timestamp to 'now' so the next evaluate is firmly INSIDE the
        # backoff window regardless of test-scheduling jitter.
        app._stream_eof_at = _time.monotonic()
        app._evaluate_live_stream()
        await pilot.pause()
        # within the backoff window -> NOT re-spawned (same dead handle kept).
        pid_now = app._stream.pid if app._stream else None
        assert pid_now == dead_pid, (
            "EOF-prone peek was re-spawned within the backoff window (churn)")
        # and OUTSIDE the window (timestamp pushed into the past) -> it DOES
        # relaunch, so the peer doesn't go permanently silent.
        app._stream_eof_at = _time.monotonic() - (_LIVE_BACKOFF_S + 1.0)
        app._stream_source_factory = _fake_source(_FOREVER_SCRIPT)
        app._evaluate_live_stream()
        await pilot.pause()
        assert app._stream is not None and app._stream.is_running()
        assert app._stream.pid != dead_pid, (
            "peek must relaunch once the backoff window elapses")


@pytest.mark.asyncio
async def test_tail_eof_relaunch_not_backed_off(tmp_path):
    """Fix #4 (sad/edge): the `tail -F` path (a NON-peek source) is NOT throttled
    — a same-key relaunch happens promptly so a rotated tee re-attaches at once."""
    cfg, _ = _populated_config(tmp_path)
    app = PeersTuiApp(config_dir=cfg)
    # a non-peek fake source that exits immediately (simulates tail dying).
    app._stream_source_factory = _fake_source(_CLAUDE_EVENTS_SCRIPT)
    async with app.run_test() as pilot:
        await _settle(pilot)
        for _ in range(40):
            if app._stream is None or not app._stream.is_running():
                break
            await pilot.pause(0.1)
        old_pid = app._stream.pid if app._stream else None
        # swap to a long-lived non-peek source; same key, dead handle -> relaunch
        # immediately (no backoff for non-peek sources).
        app._stream_source_factory = _fake_source(_FOREVER_SCRIPT)
        app._evaluate_live_stream()
        await pilot.pause()
        assert app._stream is not None and app._stream.is_running()
        assert app._stream.pid != old_pid, "non-peek source should relaunch at once"
        assert app._stream_eof_at is None, "non-peek path must not arm the backoff"


# --------------------------------------------------------------------------- #
# help screen + keymap                                                          #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_question_mark_opens_help_screen(tmp_path):
    cfg, _ = _populated_config(tmp_path)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await _settle(pilot, n=3)
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        # the help lists groups + at least one binding row (query the modal
        # screen, which is on top of the screen stack).
        title = str(app.screen.query_one("#help-title").render())
        assert "keybindings" in title.lower()
        assert len(app.screen.query(".help-row")) >= 10
        # dismiss it.
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, HelpScreen)


@pytest.mark.asyncio
async def test_keymap_bindings_registered(tmp_path):
    cfg, _ = _populated_config(tmp_path)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await _settle(pilot, n=2)
        # collect the bound keys from the app's BINDINGS.
        keys = set()
        for b in app.BINDINGS:
            spec = b[0] if isinstance(b, tuple) else b.key
            for k in str(spec).split(","):
                keys.add(k.strip())
        # the full keymap from the plan must be present.
        for k in ("p", "g", "t", "b", "d", "n", "s", "r", "a", "m",
                  "question_mark", "q", "f1", "o", "space", "x",
                  "1", "9", "j", "k", "tab"):
            assert k in keys, f"binding {k!r} missing from keymap"


@pytest.mark.asyncio
async def test_vim_keys_inert_when_input_focused(tmp_path):
    # edge: with a text Input focused, the single-letter actions must be inert
    # (so typing into a form never fires an app action).
    cfg, _ = _populated_config(tmp_path)
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await _settle(pilot, n=2)
        from textual.widgets import Input
        inp = Input(id="probe-input")
        await app.mount(inp)
        inp.focus()
        await pilot.pause()
        # check_action returns False for letter actions while the input is focused.
        assert app.check_action("live", ()) is False
        assert app.check_action("focus_gates", ()) is False
        # arrow/function keys stay live (they don't collide with typing).
        assert app.check_action("focus_next_panel", ()) is True


# --------------------------------------------------------------------------- #
# sad: no active peer renders an empty-state (no crash, no stream)               #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_live_empty_state_when_no_peer(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()  # no .peers -> no state, no current_peer
    (cfg / "projects.yaml").write_text(yaml.safe_dump(
        {"projects": [{"name": "proj", "path": str(proj),
                       "state": "fresh", "pid": None}]}))
    app = PeersTuiApp(config_dir=cfg)
    async with app.run_test() as pilot:
        await _settle(pilot)
        assert app.query(LivePanel)  # the panel is mounted
        empty = str(app.query_one("#live-empty").render())
        assert "no live peer" in empty or "loading" in empty
        # no stream spawned without a peer.
        assert app._stream is None


# --------------------------------------------------------------------------- #
# screenshot artifact (to /tmp, NOT committed)                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_screenshot_artifact(tmp_path):
    cfg, _ = _populated_config(tmp_path)
    app = PeersTuiApp(config_dir=cfg)
    app._stream_source_factory = _fake_source(_CLAUDE_EVENTS_SCRIPT)
    async with app.run_test() as pilot:
        await _settle(pilot)
        out = app.save_screenshot("/tmp/peers-tui-live.svg")
        assert out
    import os
    assert os.path.exists("/tmp/peers-tui-live.svg")
