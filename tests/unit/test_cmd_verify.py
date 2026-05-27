"""Tests for the `peers verify` subcommand.

Covers:
- All-passing hard goals → exit 0, VERIFY.md present and labelled PASS.
- A failing hard goal → exit 1, VERIFY.md labelled FAIL.
- Extra verify.commands picked up and run, fail/pass independently.
- Empty-config case (no goals, no verify.commands) → exit 0 with note.
- Bad config error paths.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from peers.cli import cmd_verify


def _write_project(
    target: Path,
    goals_yaml: str,
    config_extras: str = "",
) -> None:
    peer_dir = target / ".peers"
    peer_dir.mkdir(parents=True)
    (peer_dir / "config.yaml").write_text(
        textwrap.dedent(
            """\
            driver: orchestrator
            comm: git
            peers:
              - name: a
                tool: claude
                argv: ["true"]
                prompt_mode: argv-substitute
              - name: b
                tool: codex
                argv: ["true"]
                prompt_mode: argv-substitute
            health:
              idle_timeout_s: 60
            """
        ) + config_extras
    )
    (peer_dir / "goals.yaml").write_text(goals_yaml)


def test_verify_all_pass(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    _write_project(tmp_path, textwrap.dedent("""\
        goals:
          - id: trivial
            type: hard
            cmd: "true"
            pass_when: "exit_code == 0"
    """))
    rc = cmd_verify(tmp_path)
    assert rc == 0
    out = capsys.readouterr().out
    assert "PASS" in out
    assert "trivial" in out
    md = (tmp_path / ".peers" / "VERIFY.md").read_text()
    assert "**Result:** PASS" in md
    assert "| `trivial` | pass" in md


def test_verify_with_failing_hard_goal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    _write_project(tmp_path, textwrap.dedent("""\
        goals:
          - id: always-fails
            type: hard
            cmd: "false"
            pass_when: "exit_code == 0"
    """))
    rc = cmd_verify(tmp_path)
    assert rc == 1
    md = (tmp_path / ".peers" / "VERIFY.md").read_text()
    assert "**Result:** FAIL" in md
    assert "always-fails" in md
    assert "| `always-fails` | fail" in md


def test_verify_runs_extra_commands(tmp_path: Path):
    extras = textwrap.dedent("""\
        verify:
          timeout_s: 10
          commands:
            - name: smoke-true
              cmd: "true"
            - name: smoke-false
              cmd: "false"
    """)
    _write_project(tmp_path, textwrap.dedent("""\
        goals:
          - id: g
            type: hard
            cmd: "true"
            pass_when: "exit_code == 0"
    """), config_extras=extras)
    rc = cmd_verify(tmp_path)
    # smoke-false fails → overall fail
    assert rc == 1
    md = (tmp_path / ".peers" / "VERIFY.md").read_text()
    assert "smoke-true" in md and "smoke-false" in md
    # smoke-true is pass; smoke-false is fail
    assert "| `smoke-true` | pass" in md
    assert "| `smoke-false` | fail" in md


def test_verify_extra_command_with_stdout_check(tmp_path: Path):
    extras = textwrap.dedent("""\
        verify:
          commands:
            - name: echo-hi
              cmd: "echo hi"
    """)
    _write_project(tmp_path, "goals: []", config_extras=extras)
    rc = cmd_verify(tmp_path)
    assert rc == 0
    md = (tmp_path / ".peers" / "VERIFY.md").read_text()
    assert "| `echo-hi` | pass" in md


def test_verify_no_goals_no_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    _write_project(tmp_path, "goals: []")
    rc = cmd_verify(tmp_path)
    assert rc == 0
    md = (tmp_path / ".peers" / "VERIFY.md").read_text()
    assert "PASS" in md
    assert "nothing to check" in md


def test_verify_missing_config_returns_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    rc = cmd_verify(tmp_path)
    assert rc == 1
    err = capsys.readouterr().err
    assert "missing" in err


def test_verify_extra_command_timeout(tmp_path: Path):
    """A verify command that exceeds its timeout is reported as fail
    with a `timeout` diagnostic, and verify returns non-zero."""
    extras = textwrap.dedent("""\
        verify:
          commands:
            - name: hang
              cmd: "sleep 2"
              timeout_s: 1
    """)
    _write_project(tmp_path, "goals: []", config_extras=extras)
    rc = cmd_verify(tmp_path)
    assert rc == 1
    md = (tmp_path / ".peers" / "VERIFY.md").read_text()
    assert "hang" in md
    assert "timeout" in md.lower()


def test_verify_hard_goals_inherit_goals_timeout(tmp_path: Path):
    extras = textwrap.dedent("""\
        goals:
          timeout_s: 1
    """)
    _write_project(tmp_path, textwrap.dedent("""\
        goals:
          - id: slow-hard
            type: hard
            cmd: "sleep 2"
            pass_when: "exit_code == 0"
    """), config_extras=extras)

    rc = cmd_verify(tmp_path)

    assert rc == 1
    md = (tmp_path / ".peers" / "VERIFY.md").read_text()
    assert "slow-hard" in md
    assert "timeout" in md.lower()


def test_verify_rejects_non_mapping_config(tmp_path: Path, capsys):
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir()
    (peer_dir / "config.yaml").write_text("- not-a-mapping\n")
    (peer_dir / "goals.yaml").write_text("goals: []\n")

    rc = cmd_verify(tmp_path)

    assert rc == 1
    assert "top-level value must be a mapping" in capsys.readouterr().err


def test_verify_rejects_bool_default_timeout(tmp_path: Path, capsys):
    extras = textwrap.dedent("""\
        verify:
          timeout_s: true
    """)
    _write_project(tmp_path, "goals: []", config_extras=extras)

    rc = cmd_verify(tmp_path)

    assert rc == 1
    err = capsys.readouterr().err
    assert "verify.timeout_s" in err
    assert "bool" in err


def test_verify_command_bad_timeout_is_reported_not_traceback(tmp_path: Path):
    extras = textwrap.dedent("""\
        verify:
          commands:
            - name: bad-timeout
              cmd: "true"
              timeout_s: soon
    """)
    _write_project(tmp_path, "goals: []", config_extras=extras)

    rc = cmd_verify(tmp_path)

    assert rc == 1
    md = (tmp_path / ".peers" / "VERIFY.md").read_text()
    assert "bad-timeout" in md
    assert "verify.commands.bad-timeout.timeout_s" in md


def test_verify_refuses_symlinked_output(tmp_path: Path, capsys):
    _write_project(tmp_path, "goals: []")
    target = tmp_path / "outside.txt"
    target.write_text("keep me")
    (tmp_path / ".peers" / "VERIFY.md").symlink_to(target)

    rc = cmd_verify(tmp_path)

    assert rc == 1
    assert "refusing to write" in capsys.readouterr().err
    assert target.read_text() == "keep me"


def test_verify_refuses_symlinked_peers_dir(tmp_path: Path, capsys):
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (tmp_path / ".peers").symlink_to(decoy)

    rc = cmd_verify(tmp_path)

    assert rc == 1
    assert "refusing to operate" in capsys.readouterr().err
