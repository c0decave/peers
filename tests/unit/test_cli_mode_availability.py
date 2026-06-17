"""The operator-runnable find-bugs / bring-up commands must DEGRADE CLEANLY
when their backing mode package is not present in this build (e.g. a trimmed
distribution that ships the CLI but not the optional engine), rather than
crashing with a ModuleNotFoundError traceback.

These tests are build-agnostic: they probe a stdlib module for the "present"
case and a guaranteed-absent name for the "absent" case, so they pass whether
or not the optional engine packages are installed.
"""
from __future__ import annotations

from peers import cli


def test_mode_pkg_available_true_for_present_module():
    assert cli._mode_pkg_available("json") is True


def test_mode_pkg_available_false_for_absent_module():
    assert cli._mode_pkg_available("peers.modes.__definitely_absent__") is False


def test_cmd_bring_up_degrades_cleanly_when_mode_absent(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_mode_pkg_available", lambda _m: False)
    rc = cli.cmd_bring_up("ignored-manifest.yaml")
    assert rc == 2
    assert "not available" in capsys.readouterr().err.lower()


def test_cmd_find_bugs_degrades_cleanly_when_mode_absent(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_mode_pkg_available", lambda _m: False)
    rc = cli.cmd_find_bugs("ignored-repo", input_path="seed", fuzz_binary=None)
    assert rc == 2
    assert "not available" in capsys.readouterr().err.lower()
