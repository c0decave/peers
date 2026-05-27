from __future__ import annotations

from pathlib import Path


def test_peers_ctl_new_modes_audit_security(tmp_path, monkeypatch):
    from peers_ctl.cli import cmd_new
    monkeypatch.setenv("PEERS_PROJECTS_ROOT", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    rc = cmd_new(Path("svc"), modes=["audit", "security"],
                 config_dir=tmp_path / "ctl")
    assert rc == 0
    proj = tmp_path / "svc"
    assert (proj / ".peers" / "checks" / "vuln_scan.py").is_file()
    assert (proj / ".peers" / "checks" / "coverage_3class.py").is_file()


def test_peers_ctl_new_modes_unknown_errors(tmp_path, monkeypatch, capsys):
    from peers_ctl.cli import cmd_new
    monkeypatch.setenv("PEERS_PROJECTS_ROOT", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    rc = cmd_new(Path("svc"), modes=["bogus"],
                 config_dir=tmp_path / "ctl")
    assert rc != 0
    err = capsys.readouterr().err
    assert "bogus" in err


def test_peers_ctl_new_modes_empty_string_errors(tmp_path, monkeypatch):
    """--modes ',,,' should error (parsed to empty list), not silent
    no-op. Otherwise users pass a typo and get an unconfigured project."""
    import subprocess
    import sys
    monkeypatch.setenv("PEERS_PROJECTS_ROOT", str(tmp_path / "proj"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    r = subprocess.run(
        [sys.executable, "-m", "peers_ctl.cli", "--config-dir",
         str(tmp_path / "cfg" / "peers-ctl"),
         "new", "svc", "--modes", ", , ,"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "empty list" in (r.stderr + r.stdout).lower()
