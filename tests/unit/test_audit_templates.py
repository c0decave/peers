"""Audit scaffold templates for `peers init` / `peers-ctl new`."""
from __future__ import annotations

from pathlib import Path

import yaml

from peers_ctl.cli import cmd_new, main


AUDIT_CHECKS = (
    "coverage_3class.py",
    "scan_secrets.py",
    "deps_justified.py",
    "api_stable.py",
    "no_regression.py",
    "diff_size_per_resolve.py",
)


def test_audit_templates_creates_six_check_scripts(tmp_path, monkeypatch):
    monkeypatch.setenv("PEERS_PROJECTS_ROOT", str(tmp_path))

    rc = cmd_new(
        Path("test-audit"), audit_templates=True, config_dir=tmp_path / "ctl"
    )

    assert rc == 0
    checks = tmp_path / "test-audit" / ".peers" / "checks"
    for name in AUDIT_CHECKS:
        path = checks / name
        assert path.is_file(), f"missing {path}"
        assert path.stat().st_mode & 0o111, f"{path} not executable"


def test_audit_templates_cli_bare_name_uses_project_root_and_reports_wired_gates(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("PEERS_PROJECTS_ROOT", str(tmp_path / "projects"))

    rc = main([
        "--config-dir", str(tmp_path / "ctl"),
        "new", "myapp", "--audit-templates",
    ])

    assert rc == 0
    target = tmp_path / "projects" / "myapp"
    assert (target / ".peers" / "checks" / "coverage_3class.py").is_file()
    assert (target / ".peers" / "goals.yaml").is_file()
    out = capsys.readouterr().out
    assert "review pre-wired audit gates" in out
    assert "delete placeholder" not in out


def test_audit_templates_wires_goals_yaml_hard_gates(tmp_path, monkeypatch):
    monkeypatch.setenv("PEERS_PROJECTS_ROOT", str(tmp_path))
    cmd_new(Path("test-audit"), audit_templates=True, config_dir=tmp_path / "ctl")

    goals = yaml.safe_load(
        (tmp_path / "test-audit" / ".peers" / "goals.yaml").read_text()
    )
    hard_ids = {g["id"] for g in goals["goals"] if g["type"] == "hard"}

    expected = {
        "tdd-reproduces-bug", "no-secrets-committed", "deps-justified",
        "api-stable", "no-prior-regression", "diff-size-per-resolve",
        "bug-hunt-clean", "tests-pass", "lint-clean", "type-clean",
        "tests-cover-happy-edge-sad", "self-review-on-handoff",
    }
    assert expected <= hard_ids


def test_audit_templates_tests_pass_uses_exit_code_not_passed_regex(
    tmp_path, monkeypatch,
):
    """followup: pytest 9.0+ with -q
    suppresses the 'X passed' summary for all-pass runs, breaking the
    legacy regex-based pass_when. The template must use exit_code-based
    detection so the gate works under current pytest versions."""
    monkeypatch.setenv("PEERS_PROJECTS_ROOT", str(tmp_path))
    cmd_new(Path("test-audit"), audit_templates=True, config_dir=tmp_path / "ctl")

    goals = yaml.safe_load(
        (tmp_path / "test-audit" / ".peers" / "goals.yaml").read_text()
    )
    tests_pass = next(g for g in goals["goals"] if g["id"] == "tests-pass")

    # Don't suppress pytest's actual exit code with `|| true` (would force
    # exit_code to 0 regardless of test outcome).
    assert "|| true" not in tests_pass["cmd"], (
        f"tests-pass cmd masks pytest exit code: {tests_pass['cmd']!r}; "
        "remove `|| true` so pass_when 'exit_code == 0' is meaningful."
    )
    # Pass-condition must be exit-code-based, not stdout-regex (which is
    # version-fragile under pytest 9.0+ with -q).
    assert "exit_code" in tests_pass["pass_when"], (
        f"tests-pass pass_when relies on output regex: "
        f"{tests_pass['pass_when']!r}; switch to exit_code == 0 for "
        "robustness under pytest 9.0+ -q output suppression."
    )


def test_audit_templates_without_flag_uses_default_goals(tmp_path, monkeypatch):
    monkeypatch.setenv("PEERS_PROJECTS_ROOT", str(tmp_path))
    cmd_new(Path("test-default"), audit_templates=False, config_dir=tmp_path / "ctl")

    goals = yaml.safe_load(
        (tmp_path / "test-default" / ".peers" / "goals.yaml").read_text()
    )
    hard_ids = {g["id"] for g in goals["goals"] if g["type"] == "hard"}

    assert "tdd-reproduces-bug" not in hard_ids


def test_audit_templates_lang_js_lays_down_js_scripts(tmp_path, monkeypatch):
    monkeypatch.setenv("PEERS_PROJECTS_ROOT", str(tmp_path))

    rc = cmd_new(
        Path("test-js"), audit_templates=True, lang="js",
        config_dir=tmp_path / "ctl",
    )

    assert rc == 0
    checks = tmp_path / "test-js" / ".peers" / "checks"
    assert (checks / "coverage_3class.js").is_file()
    assert not (checks / "coverage_3class.py").exists()
    goals_text = (tmp_path / "test-js" / ".peers" / "goals.yaml").read_text()
    assert "coverage_3class.js" in goals_text


def test_audit_templates_lang_rust_lays_down_rust_scripts(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PEERS_PROJECTS_ROOT", str(tmp_path))

    rc = cmd_new(
        Path("test-rust"), audit_templates=True, lang="rust",
        config_dir=tmp_path / "ctl",
    )

    assert rc == 0
    checks = tmp_path / "test-rust" / ".peers" / "checks"
    assert (checks / "coverage_3class.sh").is_file()
    assert (checks / "coverage_3class.sh").stat().st_mode & 0o111
    assert not (checks / "coverage_3class.py").exists()
    goals_text = (tmp_path / "test-rust" / ".peers" / "goals.yaml").read_text()
    assert ".peers/checks/coverage_3class.sh" in goals_text
    assert "python3 .peers/checks/coverage_3class.py" not in goals_text


def test_audit_templates_lang_go_lays_down_go_scripts(tmp_path, monkeypatch):
    monkeypatch.setenv("PEERS_PROJECTS_ROOT", str(tmp_path))

    rc = cmd_new(
        Path("test-go"), audit_templates=True, lang="go",
        config_dir=tmp_path / "ctl",
    )

    assert rc == 0
    checks = tmp_path / "test-go" / ".peers" / "checks"
    assert (checks / "coverage_3class.sh").is_file()
    assert (checks / "no_regression.sh").is_file()
    assert not (checks / "coverage_3class.py").exists()
    goals_text = (tmp_path / "test-go" / ".peers" / "goals.yaml").read_text()
    assert ".peers/checks/no_regression.sh" in goals_text


def test_audit_templates_unknown_lang_falls_back_to_python(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("PEERS_PROJECTS_ROOT", str(tmp_path))

    rc = cmd_new(
        Path("test-x"), audit_templates=True, lang="cobol",
        config_dir=tmp_path / "ctl",
    )

    assert rc == 0
    assert "falling back to python" in capsys.readouterr().err
    checks = tmp_path / "test-x" / ".peers" / "checks"
    assert (checks / "coverage_3class.py").is_file()
