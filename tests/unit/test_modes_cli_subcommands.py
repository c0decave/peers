"""Tests for `peers-ctl modes list` + `peers-ctl modes show <name>`.

Thin read-only subcommands over `peers.modes.discover()`; we exercise
the CLI by subprocessing into `python -m peers_ctl.cli`, mirroring the
other peers-ctl CLI tests.
"""
from __future__ import annotations

import subprocess
import sys


def _peers_ctl(*args: str, env_extra: dict[str, str] | None = None,
               ) -> subprocess.CompletedProcess:
    import os
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "peers_ctl.cli", *args],
        capture_output=True, text=True, env=env,
    )


def test_modes_list_shows_audit_and_security(tmp_path, monkeypatch):
    monkeypatch.setenv("PEERS_MODES_DIR", str(tmp_path / "user-empty"))
    r = _peers_ctl("modes", "list",
                   env_extra={"PEERS_MODES_DIR": str(tmp_path / "user-empty")})
    assert r.returncode == 0, r.stderr
    assert "audit" in r.stdout
    assert "security" in r.stdout


def test_modes_show_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("PEERS_MODES_DIR", str(tmp_path / "user-empty"))
    r = _peers_ctl("modes", "show", "audit",
                   env_extra={"PEERS_MODES_DIR": str(tmp_path / "user-empty")})
    assert r.returncode == 0, r.stderr
    assert "name: audit" in r.stdout
    assert "goals:" in r.stdout


def test_modes_show_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("PEERS_MODES_DIR", str(tmp_path / "user-empty"))
    r = _peers_ctl("modes", "show", "bogus",
                   env_extra={"PEERS_MODES_DIR": str(tmp_path / "user-empty")})
    assert r.returncode != 0
    assert "bogus" in (r.stderr + r.stdout)
