"""Item 12: peers-ctl replay — offline tick history review.

`replay_project(name, options)` walks a project's
`.peers/log/runs.jsonl` and prints per-tick detail so a reviewer can
re-trace what happened without re-running the loop.

Read-only. No LLM calls, no container starts, no git mutations: the
only git call is an OPTIONAL `git diff <head_before>..<head_after>`
when ``--show-diffs`` is set, and even that is mocked here so the
tests don't depend on a real repo with the seeded SHAs.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from peers_ctl.replay import ReplayOptions, replay_project


# --- helpers ------------------------------------------------------------

def _seed_runs(proj: Path, entries: list[dict]) -> None:
    """Write the supplied entries to .peers/log/runs.jsonl, one per line."""
    log_dir = proj / ".peers" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "runs.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n"
    )


def _setup_env(tmp_path: Path) -> dict[str, str]:
    """Point PEERS_PROJECTS_ROOT + XDG_CONFIG_HOME inside tmp_path so the
    test never touches the operator's real registry."""
    env = os.environ.copy()
    env["PEERS_PROJECTS_ROOT"] = str(tmp_path / "projects")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    return env


def _opts(**overrides) -> ReplayOptions:
    """Default ReplayOptions with a fresh StringIO buffer."""
    base = dict(
        show_prompts=False,
        show_diffs=False,
        from_tick=None,
        to_tick=None,
        out=io.StringIO(),
    )
    base.update(overrides)
    return ReplayOptions(**base)


def _seed_project(tmp_path: Path, name: str = "demo",
                  entries: list[dict] | None = None) -> Path:
    """Make a fake project tree under PEERS_PROJECTS_ROOT."""
    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    proj = root / name
    proj.mkdir(parents=True, exist_ok=True)
    if entries is not None:
        _seed_runs(proj, entries)
    return proj


# Use monkeypatch fixture to set env for all tests below.
@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """Each test gets its own PEERS_PROJECTS_ROOT / XDG_CONFIG_HOME so
    we don't read the operator's registry."""
    monkeypatch.setenv("PEERS_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


# --- tests --------------------------------------------------------------

def test_replay_prints_per_tick_header(tmp_path: Path) -> None:
    """A seeded runs.jsonl renders human-readable per-tick blocks."""
    _seed_project(tmp_path, "demo", [
        {"iteration": 1, "peer": "claude", "classification": "success",
         "duration_ms": 1200, "success": True,
         "head_before": "aaa111", "head_after": "bbb222",
         "tokens_this_tick": 1500, "usd_this_tick": 0.0150,
         "soft_fail_reason": None},
        {"iteration": 2, "peer": "codex", "classification": "no-handoff",
         "duration_ms": 800, "success": False,
         "head_before": "bbb222", "head_after": "bbb222",
         "tokens_this_tick": 900, "usd_this_tick": 0.0090,
         "soft_fail_reason": "did not commit"},
    ])
    opts = _opts()
    rc = replay_project("demo", opts)
    assert rc == 0, f"expected exit 0, got {rc}"
    text = opts.out.getvalue()
    # Per-tick fields surface (both ticks)
    assert "iteration: 1" in text
    assert "iteration: 2" in text
    assert "peer: claude" in text
    assert "peer: codex" in text
    assert "classification: success" in text
    assert "classification: no-handoff" in text
    # Head transitions
    assert "aaa111" in text
    assert "bbb222" in text
    # Duration
    assert "1200" in text or "1.2" in text
    # Tokens / USD
    assert "1500" in text
    assert "0.015" in text or "0.0150" in text
    # Soft fail reason for tick 2
    assert "did not commit" in text
    # Block separator
    assert "---" in text


def test_replay_range_filter_from_and_to(tmp_path: Path) -> None:
    """--from-tick / --to-tick clip the rendered range inclusively."""
    _seed_project(tmp_path, "demo", [
        {"iteration": i, "peer": "claude", "classification": "success",
         "duration_ms": 100, "success": True,
         "head_before": f"h{i:03d}a", "head_after": f"h{i:03d}b"}
        for i in range(1, 11)  # iterations 1..10
    ])
    opts = _opts(from_tick=3, to_tick=5)
    rc = replay_project("demo", opts)
    assert rc == 0
    text = opts.out.getvalue()
    assert "iteration: 3" in text
    assert "iteration: 4" in text
    assert "iteration: 5" in text
    # Outside the range stays out
    assert "iteration: 1" not in text
    assert "iteration: 2" not in text
    assert "iteration: 6" not in text
    assert "iteration: 10" not in text


def test_replay_missing_runs_returns_exit_1(tmp_path: Path) -> None:
    """If the project directory has no runs.jsonl, exit 1 with a
    diagnostic on the output stream."""
    proj = tmp_path / "projects" / "demo"
    proj.mkdir(parents=True)
    # No .peers/log/runs.jsonl on purpose.
    opts = _opts()
    rc = replay_project("demo", opts)
    assert rc == 1
    text = opts.out.getvalue()
    assert "runs.jsonl" in text or "no tick history" in text


def test_replay_unknown_project_returns_exit_1(tmp_path: Path) -> None:
    """An unknown project name returns exit 1 with a diagnostic."""
    opts = _opts()
    rc = replay_project("does-not-exist", opts)
    assert rc == 1
    text = opts.out.getvalue()
    assert "does-not-exist" in text or "no such project" in text


def test_replay_show_diffs_invokes_git(tmp_path: Path) -> None:
    """With --show-diffs, replay invokes `git -C <project> diff
    <head_before>..<head_after>` for each tick that has both
    head_before AND head_after AND they differ."""
    _seed_project(tmp_path, "demo", [
        {"iteration": 1, "peer": "claude", "classification": "success",
         "duration_ms": 100, "success": True,
         "head_before": "aaa111", "head_after": "bbb222"},
        # tick 2 has identical SHAs — no diff invocation expected
        {"iteration": 2, "peer": "claude", "classification": "no-handoff",
         "duration_ms": 100, "success": False,
         "head_before": "bbb222", "head_after": "bbb222"},
    ])
    opts = _opts(show_diffs=True)

    fake = subprocess.CompletedProcess(
        args=["git"], returncode=0,
        stdout="--- a/foo\n+++ b/foo\n@@ +1 @@\n+hello\n",
        stderr="",
    )
    with patch("peers_ctl.replay.subprocess.run", return_value=fake) as run:
        rc = replay_project("demo", opts)
    assert rc == 0
    # Exactly one git diff invocation (for tick 1; tick 2 is a no-op)
    assert run.call_count == 1
    argv = run.call_args.args[0]
    assert argv[0] == "git"
    assert "-C" in argv
    assert "diff" in argv
    # SHA range argument is present
    assert any("aaa111..bbb222" in a for a in argv)
    # The diff text leaks into the output stream
    text = opts.out.getvalue()
    assert "hello" in text


def test_replay_show_prompts_notes_when_missing(tmp_path: Path) -> None:
    """--show-prompts looks in .peers/log/prompts/iter-N for prompt
    text; when missing it notes that on the output."""
    _seed_project(tmp_path, "demo", [
        {"iteration": 1, "peer": "claude", "classification": "success",
         "duration_ms": 100, "success": True,
         "head_before": "x", "head_after": "y"},
    ])
    opts = _opts(show_prompts=True)
    rc = replay_project("demo", opts)
    assert rc == 0
    text = opts.out.getvalue()
    # Either "no prompt" diagnostic or "not found" notice surfaces.
    assert "prompt" in text.lower()


def test_replay_show_prompts_refuses_symlinked_file_BUG_176(
    tmp_path: Path,
) -> None:
    """BUG-176: a symlink inside iter-N/ used to be printed via read_text,
    disclosing any same-user-readable host file (e.g. ~/.ssh/id_rsa) when
    the operator runs `peers-ctl replay --show-prompts`. The renderer
    must skip symlinked entries with a clear diagnostic instead.
    """
    proj = _seed_project(tmp_path, "demo", [
        {"iteration": 1, "peer": "claude", "classification": "success",
         "duration_ms": 100, "success": True,
         "head_before": "x", "head_after": "y"},
    ])
    prompts = proj / ".peers" / "log" / "prompts" / "iter-1"
    prompts.mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET_HOST_FILE_CONTENT\n")
    (prompts / "evil.txt").symlink_to(secret)

    opts = _opts(show_prompts=True)
    rc = replay_project("demo", opts)
    assert rc == 0
    text = opts.out.getvalue()
    assert "SECRET_HOST_FILE_CONTENT" not in text
    # A skip/refuse diagnostic should explain why nothing was printed.
    assert "symlink" in text.lower() or "skip" in text.lower()


def test_replay_show_prompts_refuses_symlinked_iter_dir_BUG_176(
    tmp_path: Path,
) -> None:
    """The iter-N directory itself may be a symlink. listdir would
    happily walk into the attacker's directory; the renderer must refuse.
    """
    proj = _seed_project(tmp_path, "demo", [
        {"iteration": 1, "peer": "claude", "classification": "success",
         "duration_ms": 100, "success": True,
         "head_before": "x", "head_after": "y"},
    ])
    prompts_root = proj / ".peers" / "log" / "prompts"
    prompts_root.mkdir(parents=True)
    attacker_dir = tmp_path / "attacker_prompts"
    attacker_dir.mkdir()
    (attacker_dir / "leak.txt").write_text("SECRET_ATTACKER_CONTENT\n")
    (prompts_root / "iter-1").symlink_to(attacker_dir)

    opts = _opts(show_prompts=True)
    rc = replay_project("demo", opts)
    assert rc == 0
    text = opts.out.getvalue()
    assert "SECRET_ATTACKER_CONTENT" not in text


def test_replay_show_prompts_reads_existing(tmp_path: Path) -> None:
    """If a prompt file exists for iter-N, its contents appear in the
    output."""
    proj = _seed_project(tmp_path, "demo", [
        {"iteration": 1, "peer": "claude", "classification": "success",
         "duration_ms": 100, "success": True,
         "head_before": "x", "head_after": "y"},
    ])
    prompts = proj / ".peers" / "log" / "prompts" / "iter-1"
    prompts.mkdir(parents=True)
    (prompts / "claude.txt").write_text("You are a peer.\nDo the thing.\n")

    opts = _opts(show_prompts=True)
    rc = replay_project("demo", opts)
    assert rc == 0
    text = opts.out.getvalue()
    assert "You are a peer." in text
    assert "Do the thing." in text


def test_replay_skips_exit_event_line(tmp_path: Path) -> None:
    """Synthetic `event: exit` lines (driver_observability) are not
    real ticks and must not be rendered as ticks."""
    _seed_project(tmp_path, "demo", [
        {"iteration": 1, "peer": "claude", "classification": "success",
         "duration_ms": 100, "success": True,
         "head_before": "a", "head_after": "b"},
        {"event": "exit", "reason": "complete", "ticks_in_run": 1,
         "ts": "2026-05-28T00:00:00Z"},
    ])
    opts = _opts()
    rc = replay_project("demo", opts)
    assert rc == 0
    text = opts.out.getvalue()
    # The exit event surfaces as a footer, not as a tick block.
    assert "iteration: 1" in text
    # No "iteration:" entry should appear for the exit line, but the
    # exit reason should still show somewhere as a trailing summary.
    assert "complete" in text


def test_replay_invalid_project_name_returns_exit_2(tmp_path: Path) -> None:
    """An invalid project name short-circuits with exit 2 before any
    filesystem lookup."""
    opts = _opts()
    rc = replay_project("../escape", opts)
    assert rc == 2
    text = opts.out.getvalue()
    assert "invalid" in text.lower() or "project name" in text.lower()


def test_replay_edge_boundary_duration_infinity_renders_dash_BUG_304(
    tmp_path: Path,
) -> None:
    """BUG-304 reproducer: a runs.jsonl entry whose ``duration_ms`` is
    ``+Infinity`` (which Python's json.loads accepts when the producer
    used ``allow_nan=True``) must render as the same ``-`` fallback that
    NaN / None / negative durations get, NOT crash the whole replay with
    OverflowError from ``int(float('inf'))``. Boundary case — replay
    should be defensive about runs.jsonl payloads it didn't write."""
    log_dir = tmp_path / "projects" / "demo" / ".peers" / "log"
    log_dir.mkdir(parents=True)
    # Python's json module emits / accepts the literal token 'Infinity'
    # in non-strict mode. Write it directly so we don't depend on the
    # producer's serializer flags.
    (log_dir / "runs.jsonl").write_text(
        '{"iteration": 1, "peer": "claude", "classification": "success", '
        '"duration_ms": Infinity, "success": true, '
        '"head_before": "aaa", "head_after": "bbb"}\n'
    )
    opts = _opts()
    rc = replay_project("demo", opts)
    assert rc == 0, f"expected clean replay, got {rc}: {opts.out.getvalue()}"
    text = opts.out.getvalue()
    assert "iteration: 1" in text
    # The duration line must surface as the unknown-marker, not crash
    # and not print a bogus 'inf'-derived integer.
    assert "duration: -" in text


# --- BUG-179: replay --show-diffs must validate head SHAs --------------------

def test_replay_show_diffs_refuses_option_like_head_BUG_179(
    tmp_path: Path,
) -> None:
    """BUG-179: _render_diff passes head_before/head_after directly to
    ``git diff <head_before>..<head_after>``. A runs.jsonl entry under
    project control can name ``--upload-pack=/tmp/x`` or any other
    option-like string; without validation the supposedly read-only
    replay subprocesses arbitrary git option parsing. The renderer must
    reject anything that isn't a valid hex object id before invoking
    git.
    """
    _seed_project(tmp_path, "demo", [
        {"iteration": 1, "peer": "claude", "classification": "success",
         "duration_ms": 100, "success": True,
         "head_before": "--upload-pack=/tmp/evil",
         "head_after": "bbb222"},
    ])
    opts = _opts(show_diffs=True)
    # On the buggy code, _render_diff calls subprocess.run with the
    # attacker-controlled value as a positional arg; patch to capture.
    with patch("peers_ctl.replay.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout="", stderr="",
        )
        rc = replay_project("demo", opts)
    assert rc == 0
    # After the fix the renderer skips the tick with a diagnostic and
    # NEVER invokes git for the malformed revision pair.
    assert run.call_count == 0, (
        f"git was invoked with attacker-controlled revision: "
        f"{run.call_args_list}"
    )
    text = opts.out.getvalue()
    assert "diff:" in text
    # A clear "rejected"/"invalid" diagnostic should mention the issue.
    lowered = text.lower()
    assert "invalid" in lowered or "skipped" in lowered or "rejected" in lowered


def test_replay_show_diffs_refuses_non_hex_head_BUG_179(
    tmp_path: Path,
) -> None:
    """BUG-179: random non-hex strings in head_before/head_after are
    also rejected — git can be steered into expensive ref lookups
    against the project-controlled string. Only full/abbreviated hex
    object IDs (4..64 chars) are accepted."""
    _seed_project(tmp_path, "demo", [
        {"iteration": 1, "peer": "claude", "classification": "success",
         "duration_ms": 100, "success": True,
         "head_before": "refs/heads/main",
         "head_after": "bbb222"},
    ])
    opts = _opts(show_diffs=True)
    with patch("peers_ctl.replay.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout="", stderr="",
        )
        rc = replay_project("demo", opts)
    assert rc == 0
    assert run.call_count == 0


def test_replay_show_diffs_accepts_short_hex_BUG_179(
    tmp_path: Path,
) -> None:
    """BUG-179 happy path: 4..64-char hex strings are still accepted
    (covers normal abbreviated and full SHAs after the validation
    tightens)."""
    _seed_project(tmp_path, "demo", [
        {"iteration": 1, "peer": "claude", "classification": "success",
         "duration_ms": 100, "success": True,
         "head_before": "abcd",
         "head_after": "1234567890abcdef1234567890abcdef12345678"},
    ])
    opts = _opts(show_diffs=True)
    with patch("peers_ctl.replay.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout="", stderr="",
        )
        rc = replay_project("demo", opts)
    assert rc == 0
    assert run.call_count == 1
