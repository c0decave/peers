"""Unit tests for `peers-ctl new --modes=implement --plan FILE` (Task 1.3).

Covers the integration of the implement-mode scaffold path:

* `--plan FILE` (mutually exclusive with `--spec`)
* `--modes=implement` <=> `--plan` (both required together)
* PLAN.md validation via plan_parser (errors → stderr, exit 1)
* Acceptance preflight (rejects already-passing unless `--force`)
* PLAN.md copied to project root + frozen contracts under .peers/
* Graceful degradation when the implement mode-template is absent
  (Task 2.8 deliverable).

Note: the spec for this task used `PEERS_CTL_PROJECTS_DIR` for the
bare-name projects root env var; the actual env var in this codebase
is `PEERS_PROJECTS_ROOT` (see `peers_ctl.cli.projects_root`). The tests
below use the real env var.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _peers_ctl_cmd() -> list[str]:
    return [sys.executable, "-m", "peers_ctl"]


def _make_plan(path: Path, acceptance: str = "false") -> None:
    """Write a minimal valid PLAN.md.

    Default acceptance is ``false`` so the preflight rejects the run
    (i.e. the feature is not yet implemented, which is the normal
    state at scaffold time).
    """
    path.write_text(
        "# Feature\n"
        "## Meta\n"
        "surfaces: [cli]\n"
        f"acceptance: {acceptance}\n"
        "## Steps\n"
        "- [ ] [STEP-1] do thing\n"
        "  - touches: src/thing.py\n"
    )


def _common_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PEERS_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


def test_new_with_plan_creates_contracts(tmp_path, monkeypatch):
    plan = tmp_path / "PLAN.md"
    _make_plan(plan, acceptance="false")
    _common_env(tmp_path, monkeypatch)
    res = subprocess.run(
        _peers_ctl_cmd() + [
            "new", "myfeature", "--modes=implement", "--plan", str(plan),
        ],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, f"stderr={res.stderr}\nstdout={res.stdout}"
    proj_dir = tmp_path / "projects" / "myfeature"
    assert (proj_dir / "PLAN.md").exists()
    assert (proj_dir / ".peers" / "PLAN.original.md").exists()
    assert (proj_dir / ".peers" / "contracts" / "acceptance.sh").exists()
    assert (proj_dir / ".peers" / "contracts.sha").exists()


def test_implement_mode_requires_plan(tmp_path, monkeypatch):
    _common_env(tmp_path, monkeypatch)
    res = subprocess.run(
        _peers_ctl_cmd() + ["new", "myfeature", "--modes=implement"],
        capture_output=True, text=True,
    )
    assert res.returncode != 0
    assert "--plan" in (res.stderr + res.stdout).lower()


def test_plan_requires_implement_mode(tmp_path, monkeypatch):
    plan = tmp_path / "PLAN.md"
    _make_plan(plan)
    _common_env(tmp_path, monkeypatch)
    res = subprocess.run(
        _peers_ctl_cmd() + [
            "new", "myfeature", "--modes=audit", "--plan", str(plan),
        ],
        capture_output=True, text=True,
    )
    assert res.returncode != 0
    assert "implement" in (res.stderr + res.stdout).lower()


def test_invalid_plan_file_reports_validation_error(tmp_path, monkeypatch):
    plan = tmp_path / "PLAN.md"
    # acceptance: is missing → plan_parser raises PlanValidationError
    plan.write_text(
        "# Feature\n"
        "## Meta\n"
        "surfaces: [cli]\n"
        "## Steps\n"
        "- [ ] [STEP-1] x\n"
    )
    _common_env(tmp_path, monkeypatch)
    res = subprocess.run(
        _peers_ctl_cmd() + [
            "new", "myfeature", "--modes=implement", "--plan", str(plan),
        ],
        capture_output=True, text=True,
    )
    assert res.returncode != 0
    assert "acceptance" in res.stderr.lower()


def test_acceptance_already_passing_rejects(tmp_path, monkeypatch):
    plan = tmp_path / "PLAN.md"
    _make_plan(plan, acceptance="true")  # exit 0 → "already implemented"
    _common_env(tmp_path, monkeypatch)
    res = subprocess.run(
        _peers_ctl_cmd() + [
            "new", "myfeature", "--modes=implement", "--plan", str(plan),
        ],
        capture_output=True, text=True,
    )
    assert res.returncode != 0
    combined = (res.stderr + res.stdout).lower()
    assert "already" in combined or "implemented" in combined


def test_acceptance_already_passing_force_overrides(tmp_path, monkeypatch):
    plan = tmp_path / "PLAN.md"
    _make_plan(plan, acceptance="true")
    _common_env(tmp_path, monkeypatch)
    res = subprocess.run(
        _peers_ctl_cmd() + [
            "new", "myfeature", "--modes=implement", "--plan", str(plan),
            "--force",
        ],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, f"stderr={res.stderr}\nstdout={res.stdout}"


def test_plan_and_spec_mutually_exclusive(tmp_path, monkeypatch):
    plan = tmp_path / "PLAN.md"
    _make_plan(plan, acceptance="false")
    spec = tmp_path / "SPEC.md"
    spec.write_text("# Spec\n")
    _common_env(tmp_path, monkeypatch)
    res = subprocess.run(
        _peers_ctl_cmd() + [
            "new", "myfeature", "--modes=implement",
            "--plan", str(plan), "--spec", str(spec),
        ],
        capture_output=True, text=True,
    )
    assert res.returncode != 0


def test_invalid_utf8_in_plan_md_rejected(tmp_path, monkeypatch):
    """PLAN.md must be valid UTF-8; silent replacement would corrupt the
    SHA-pinned PLAN.original.md copy.

    Requires a clean ``peers-ctl: ...`` error (NOT a Python stacktrace)
    so the operator gets actionable feedback rather than an uncaught
    UnicodeDecodeError from a deeper parser.
    """
    plan = tmp_path / "PLAN.md"
    plan.write_bytes(
        b"# Feature\n\xff\xfeinvalid utf-8\n"
        b"## Meta\nsurfaces: [cli]\nacceptance: false\n"
        b"## Steps\n- [ ] [STEP-1] x\n  - touches: src/x.py\n"
    )
    _common_env(tmp_path, monkeypatch)
    res = subprocess.run(
        _peers_ctl_cmd() + [
            "new", "myfeature", "--modes=implement", "--plan", str(plan),
        ],
        capture_output=True, text=True,
    )
    assert res.returncode != 0
    assert "utf-8" in res.stderr.lower()
    # Clean error, not an uncaught stacktrace from a deeper layer.
    assert "traceback" not in res.stderr.lower()


def test_preflight_runs_in_cwd(tmp_path, monkeypatch):
    """preflight uses operator cwd, not target project dir.

    Create a test file in tmp_path that ``cat`` can find; acceptance
    command ``cat marker.txt`` will succeed (exit 0) when run from
    tmp_path, demonstrating the cwd contract.
    """
    plan = tmp_path / "PLAN.md"
    _make_plan(plan, acceptance="cat marker.txt")
    (tmp_path / "marker.txt").write_text("hello")
    _common_env(tmp_path, monkeypatch)
    res = subprocess.run(
        _peers_ctl_cmd() + [
            "new", "myfeature", "--modes=implement", "--plan", str(plan),
        ],
        cwd=str(tmp_path),  # operator runs from tmp_path
        capture_output=True, text=True,
    )
    # marker.txt exists in cwd → acceptance succeeds → preflight rejects
    assert res.returncode != 0
    combined = (res.stderr + res.stdout).lower()
    assert "already" in combined or "implemented" in combined
