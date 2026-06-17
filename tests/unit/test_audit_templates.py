"""Audit scaffold templates for `peers init` / `peers-ctl new`."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

from peers.templates.modes.audit.checks import deps_justified
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


def test_audit_templates_lint_type_preserve_tool_exit_codes_BUG_259(
    tmp_path, monkeypatch,
):
    """BUG-259: missing ruff/mypy must fail closed, not pass because the
    shell output lacks the word "error"."""
    monkeypatch.setenv("PEERS_PROJECTS_ROOT", str(tmp_path))
    cmd_new(Path("test-audit"), audit_templates=True, config_dir=tmp_path / "ctl")

    goals = yaml.safe_load(
        (tmp_path / "test-audit" / ".peers" / "goals.yaml").read_text()
    )

    for gate_id in ("lint-clean", "type-clean"):
        gate = next(g for g in goals["goals"] if g["id"] == gate_id)
        assert "|| true" not in gate["cmd"], (
            f"{gate_id} masks missing-tool or finding exit codes: "
            f"{gate['cmd']!r}"
        )
        assert gate["pass_when"] == "exit_code == 0"


def test_audit_templates_host_init_ignores_stale_path_peers_BUG_260(
    tmp_path, monkeypatch,
):
    """BUG-260 sad path: a stale installed `peers` executable on
    PATH must not supply old bundled templates during host scaffolding."""
    monkeypatch.setenv("PEERS_PROJECTS_ROOT", str(tmp_path))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_peers = bin_dir / "peers"
    fake_peers.write_text(
        "#!/bin/sh\n"
        "printf 'stale peers executable should not run\\n' >&2\n"
        "exit 42\n",
        encoding="utf-8",
    )
    fake_peers.chmod(0o755)
    monkeypatch.setenv(
        "PATH",
        f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
    )

    rc = cmd_new(Path("test-audit"), audit_templates=True,
                 config_dir=tmp_path / "ctl")

    assert rc == 0
    goals = yaml.safe_load(
        (tmp_path / "test-audit" / ".peers" / "goals.yaml").read_text()
    )
    lint = next(g for g in goals["goals"] if g["id"] == "lint-clean")
    assert lint["cmd"] == "ruff check ."
    assert lint["pass_when"] == "exit_code == 0"


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


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)


def _deps_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.test")
    _git(repo, "config", "user.name", "Test User")
    (repo / "pyproject.toml").write_text(
        "[project]\nname = \"demo\"\nversion = \"0.1.0\"\n",
        encoding="utf-8",
    )
    _commit_all(repo, "baseline")
    _git(repo, "tag", "peers-baseline")
    return repo


def test_deps_justified_ignores_pyproject_tool_config_BUG_510(tmp_path):
    """BUG-510: tool config in pyproject is not a dependency addition."""
    repo = _deps_repo(tmp_path)
    (repo / "pyproject.toml").write_text(
        "[project]\nname = \"demo\"\nversion = \"0.1.0\"\n\n"
        "[tool.mypy]\nexclude = \"^src/templates/\"\n\n"
        "[[tool.mypy.overrides]]\n"
        "module = \"yaml\"\nignore_missing_imports = true\n",
        encoding="utf-8",
    )
    _commit_all(repo, "add mypy config")

    assert deps_justified.changed_dep_lines(str(repo)) == []
    assert deps_justified.main(str(repo)) == 0


def test_deps_justified_still_requires_real_pyproject_dependencies(tmp_path):
    repo = _deps_repo(tmp_path)
    (repo / "pyproject.toml").write_text(
        "[project]\nname = \"demo\"\nversion = \"0.1.0\"\n"
        "dependencies = [\"requests>=2.32\", \"PyYAML>=6.0\"]\n",
        encoding="utf-8",
    )
    _commit_all(repo, "add deps")

    assert deps_justified.changed_dep_lines(str(repo)) == [
        "requests>=2.32",
        "PyYAML>=6.0",
    ]
    assert deps_justified.main(str(repo)) == 1

    _git(
        repo, "commit", "--allow-empty", "-m", "justify deps",
        "-m", "Dependency-Justification: requests needed for HTTP tests\n"
              "Dependency-Justification: pyyaml exercised by config tests",
    )
    assert deps_justified.main(str(repo)) == 0


def test_deps_justified_handles_no_newline_diff_markers(tmp_path):
    repo = _deps_repo(tmp_path)
    (repo / "pyproject.toml").write_text(
        "[tool.demo]\nvalue = \"old\"",
        encoding="utf-8",
    )
    _commit_all(repo, "replace baseline without trailing newline")
    _git(repo, "tag", "-f", "peers-baseline")
    (repo / "pyproject.toml").write_text(
        "[project]\nname = \"demo\"\n"
        "dependencies = [\"requests>=2.32\"]\n",
        encoding="utf-8",
    )
    _commit_all(repo, "add real dependency")

    assert deps_justified.changed_dep_lines(str(repo)) == ["requests>=2.32"]
    assert deps_justified.main(str(repo)) == 1
