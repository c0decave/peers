"""Tests for `peers init --driver=hooks --install` host-config patcher.

Covers _install_claude_settings (JSON, idempotency, drift, multi-section
preservation) and _install_codex_config (TOML, refusal on existing
unrelated [hooks] section, append-then-update path).
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from peers.cli import (
    _hook_install_marker,
    _install_claude_settings,
    _install_codex_config,
    _install_host_hooks,
    cmd_init,
)


# ---------------- claude settings.json -------------------------------


def test_claude_install_into_empty_file(tmp_path: Path):
    settings = tmp_path / "settings.json"
    marker = _hook_install_marker(tmp_path)
    status, backup = _install_claude_settings(
        settings, "peers -C /x tick --after claude", marker,
    )
    assert status == "installed"
    assert backup is None  # no prior file to back up
    data = json.loads(settings.read_text())
    stop = data["hooks"]["Stop"]
    assert len(stop) == 1
    assert stop[0]["hooks"][0]["command"].endswith(f"# {marker}")


def test_claude_install_is_idempotent(tmp_path: Path):
    settings = tmp_path / "settings.json"
    marker = _hook_install_marker(tmp_path)
    cmd = "peers -C /x tick --after claude"
    s1, _ = _install_claude_settings(settings, cmd, marker)
    s2, b2 = _install_claude_settings(settings, cmd, marker)
    assert s1 == "installed"
    assert s2 == "noop"
    assert b2 is None  # noop does not back up


def test_claude_install_updates_on_drift(tmp_path: Path):
    settings = tmp_path / "settings.json"
    marker = _hook_install_marker(tmp_path)
    _install_claude_settings(settings, "old-cmd", marker)
    status, backup = _install_claude_settings(
        settings, "peers -C /new tick --after claude", marker,
    )
    assert status == "updated"
    assert backup is not None and backup.exists()
    data = json.loads(settings.read_text())
    assert "old-cmd" not in json.dumps(data)
    assert "/new" in data["hooks"]["Stop"][-1]["hooks"][0]["command"]


def test_claude_install_skips_symlinked_settings(tmp_path: Path):
    settings = tmp_path / "settings.json"
    bait = tmp_path / "bait.json"
    bait.write_text("{}\n")
    settings.symlink_to(bait)
    marker = _hook_install_marker(tmp_path)

    status, backup = _install_claude_settings(
        settings, "peers -C /x tick --after claude", marker,
    )

    assert status == "skipped"
    assert backup is None
    assert bait.read_text() == "{}\n"


def test_claude_install_preserves_unrelated_entries(tmp_path: Path):
    """Other hooks blocks (PreCompact, SessionStart, user's own Stop
    entries) must survive the merge."""
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "model": "claude-opus-4-7",
        "hooks": {
            "Stop": [
                {"matcher": "", "hooks": [
                    {"type": "command", "command": "echo user-own"},
                ]},
            ],
            "PreCompact": [
                {"matcher": "", "hooks": [
                    {"type": "command", "command": "echo precompact"},
                ]},
            ],
        },
    }))
    marker = _hook_install_marker(tmp_path)
    status, backup = _install_claude_settings(
        settings, "peers -C /x tick --after claude", marker,
    )
    assert status == "installed"
    assert backup is not None
    data = json.loads(settings.read_text())
    assert data["model"] == "claude-opus-4-7"
    assert data["hooks"]["PreCompact"][0]["hooks"][0]["command"] == "echo precompact"
    # Both Stop entries (user's + ours) must be present
    commands = [h["command"]
                for e in data["hooks"]["Stop"] for h in e["hooks"]]
    assert "echo user-own" in commands
    assert any(marker in c for c in commands)


def test_claude_install_skips_corrupt_json(tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text("{ not valid json")
    marker = _hook_install_marker(tmp_path)
    status, _ = _install_claude_settings(settings, "cmd", marker)
    assert status == "skipped"
    # File untouched
    assert settings.read_text() == "{ not valid json"


def test_claude_install_skips_non_object_root(tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text('["a", "list, not an object"]')
    marker = _hook_install_marker(tmp_path)
    status, _ = _install_claude_settings(settings, "cmd", marker)
    assert status == "skipped"


# ---------------- codex config.toml ----------------------------------


def test_codex_install_into_empty_file(tmp_path: Path):
    config = tmp_path / "config.toml"
    marker = _hook_install_marker(tmp_path)
    status, backup = _install_codex_config(config, "peers tick", marker)
    assert status == "installed"
    assert backup is None
    text = config.read_text()
    assert "[hooks]" in text
    assert "on_stop" in text
    assert marker in text


def test_codex_install_escapes_toml_command_string(tmp_path: Path):
    config = tmp_path / "config.toml"
    marker = _hook_install_marker(tmp_path)
    command = 'peers -C "/tmp/with quote and\nnewline" tick'

    status, _ = _install_codex_config(config, command, marker)

    assert status == "installed"
    parsed = tomllib.loads(config.read_text())
    assert parsed["hooks"]["on_stop"] == command


def test_codex_install_appends_to_file_without_hooks(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text('[model]\nname = "gpt-5"\n')
    marker = _hook_install_marker(tmp_path)
    status, backup = _install_codex_config(config, "peers tick", marker)
    assert status == "installed"
    assert backup is not None
    text = config.read_text()
    assert "[model]" in text  # preserved
    assert "[hooks]" in text
    assert marker in text


def test_codex_install_idempotent(tmp_path: Path):
    config = tmp_path / "config.toml"
    marker = _hook_install_marker(tmp_path)
    _install_codex_config(config, "peers tick", marker)
    status, backup = _install_codex_config(config, "peers tick", marker)
    assert status == "noop"
    assert backup is None


def test_codex_install_updates_on_drift(tmp_path: Path):
    config = tmp_path / "config.toml"
    marker = _hook_install_marker(tmp_path)
    _install_codex_config(config, "old-cmd", marker)
    status, backup = _install_codex_config(config, "new-cmd", marker)
    assert status == "updated"
    assert backup is not None
    text = config.read_text()
    assert "old-cmd" not in text
    assert "new-cmd" in text


def test_codex_install_skips_symlinked_config(tmp_path: Path):
    config = tmp_path / "config.toml"
    bait = tmp_path / "bait.toml"
    bait.write_text("[hooks]\n")
    config.symlink_to(bait)
    marker = _hook_install_marker(tmp_path)

    status, backup = _install_codex_config(config, "peers tick", marker)

    assert status == "skipped"
    assert backup is None
    assert bait.read_text() == "[hooks]\n"


def test_codex_install_refuses_to_clobber_user_hooks(tmp_path: Path):
    """If user has their own [hooks] without our marker, we refuse to
    touch it — they have a custom on_stop we don't understand."""
    config = tmp_path / "config.toml"
    config.write_text(
        '[hooks]\non_stop = "my-own-command --foo"\n'
    )
    marker = _hook_install_marker(tmp_path)
    status, _ = _install_codex_config(config, "peers tick", marker)
    assert status == "skipped"
    assert "my-own-command" in config.read_text()


# ---------------- end-to-end through _install_host_hooks -------------


def test_install_host_hooks_writes_both_files(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    claude_settings = tmp_path / "claude_home" / "settings.json"
    codex_config = tmp_path / "codex_home" / "config.toml"
    rc = _install_host_hooks(
        project, claude_settings=claude_settings, codex_config=codex_config,
    )
    assert rc == 0
    assert claude_settings.exists()
    assert codex_config.exists()
    marker = _hook_install_marker(project)
    assert marker in claude_settings.read_text()
    assert marker in codex_config.read_text()


def test_install_host_hooks_reports_failure_when_both_skipped(
    tmp_path: Path,
):
    project = tmp_path / "project"
    project.mkdir()
    claude_settings = tmp_path / "settings.json"
    codex_config = tmp_path / "config.toml"
    claude_settings.write_text("{ not json")
    codex_config.write_text('[hooks]\non_stop = "x"\n')
    rc = _install_host_hooks(
        project, claude_settings=claude_settings, codex_config=codex_config,
    )
    assert rc == 1


# ---------------- cmd_init wiring -------------------------------------


def test_cmd_init_install_flag_ignored_without_hooks_driver(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
):
    rc = cmd_init(target=tmp_path, force=False, driver="orchestrator",
                  install_hooks=True)
    assert rc == 0
    captured = capsys.readouterr()
    assert "only applies with --driver=hooks" in captured.err


def test_cmd_init_with_hooks_install_creates_snippets_and_attempts_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """End-to-end: cmd_init with driver=hooks + install_hooks=True
    creates the .peers/hooks snippets AND attempts the host-side
    install. We redirect HOME via monkeypatch so we don't touch the
    user's actual settings."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    # Path.home() reads HOME on POSIX, so the monkeypatch above is
    # enough. Make sure imports inside cmd_init pick up the new HOME.
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    project = tmp_path / "project"
    project.mkdir()
    rc = cmd_init(target=project, force=False, driver="hooks",
                  install_hooks=True)
    assert rc == 0
    assert (project / ".peers" / "hooks" / "claude-stop-hook.json").exists()
    assert (fake_home / ".claude" / "settings.json").exists()
    assert (fake_home / ".codex" / "config.toml").exists()
    marker = _hook_install_marker(project)
    assert marker in (fake_home / ".claude" / "settings.json").read_text()
    assert marker in (fake_home / ".codex" / "config.toml").read_text()


def test_cmd_init_hooks_snippets_use_absolute_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.chdir(tmp_path)
    project = tmp_path / "project"
    project.mkdir()

    rc = cmd_init(target=Path("project"), force=False, driver="hooks")

    assert rc == 0
    expected = str(project.resolve())
    codex = project / ".peers" / "hooks" / "codex-config.toml"
    claude = project / ".peers" / "hooks" / "claude-stop-hook.json"
    assert expected in codex.read_text()
    data = json.loads(claude.read_text())
    command = data["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert expected in command
