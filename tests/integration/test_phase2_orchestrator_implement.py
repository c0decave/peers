"""Phase 2 integration test — implement-mode orchestrator + all hard gates.

Verifies the implement-mode templates + Schicht-1 hard gates work together
end-to-end without requiring actual peer execution (claude/codex OAuth is
out of scope for tests). Uses a mock "fake convergence" scenario.

Closes Tasks 2.1–2.9: every Phase-2 deliverable is exercised here at
least once at the substrate boundary.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from peers_ctl.contracts import write_frozen_contracts


def _peers_ctl(
    *args: str,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "peers_ctl", *args]
    return subprocess.run(
        cmd, capture_output=True, text=True, env=env,
        cwd=str(cwd) if cwd else None,
    )


def _peers_run_check(
    check_name: str, project_dir: Path,
) -> subprocess.CompletedProcess[str]:
    """Invoke `peers -C <project_dir> run-check <name>`.

    Note: `-C/--target` is a parent flag and MUST come before the
    subcommand, otherwise argparse rejects it as unrecognised.
    """
    return subprocess.run(
        [
            sys.executable, "-m", "peers",
            "-C", str(project_dir),
            "run-check", check_name,
        ],
        capture_output=True, text=True,
    )


def _make_env(projects_root: Path, config_home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PEERS_PROJECTS_ROOT"] = str(projects_root)
    env["XDG_CONFIG_HOME"] = str(config_home)
    return env


def test_implement_mode_listed_in_modes(tmp_path):
    """`peers-ctl modes list` includes implement as builtin."""
    env = _make_env(tmp_path / "projects", tmp_path / "config")
    res = _peers_ctl("modes", "list", env=env)
    assert res.returncode == 0, f"stderr={res.stderr}"
    # Header columns + one row per mode; implement must be in there.
    assert "implement" in res.stdout
    # And it should be the builtin copy unless XDG_CONFIG_HOME shadowed it.
    lines = [line for line in res.stdout.splitlines() if "implement" in line]
    assert any("builtin" in line for line in lines), res.stdout


def test_implement_mode_yaml_loadable_by_modes_module():
    """`peers.modes.discover()` can load the implement mode."""
    from peers.modes import discover
    modes = discover()
    assert "implement" in modes
    impl = modes["implement"]
    assert impl.version >= 1
    assert impl.source == "builtin"
    # Sanity: mode dir layout matches what the run-check dispatcher expects.
    assert (impl.path / "mode.yaml").is_file()
    assert (impl.path / "goals.yaml").is_file()
    assert (impl.path / "checks").is_dir()


def test_implement_goals_yaml_loadable_by_goal_engine():
    """The goals.yaml parses + has 10+ hard gates wired with cmd/pass_when."""
    impl_dir = (
        Path(__file__).resolve().parent.parent.parent
        / "src" / "peers" / "templates" / "modes" / "implement"
    )
    data = yaml.safe_load((impl_dir / "goals.yaml").read_text())
    goals = data["goals"]
    hard_goals = [
        g for g in goals
        if g.get("type") == "hard" or g.get("kind") == "hard"
    ]
    assert len(hard_goals) >= 10, (
        f"expected 10+ hard gates, got {len(hard_goals)}"
    )
    # Spot-check the implement-specific gates are wired with cmd+pass_when.
    spot_check_ids = {
        "plan-checklist-empty",
        "acceptance-pass",
        "contracts-unchanged",
    }
    seen = set()
    for g in goals:
        if g["id"] in spot_check_ids:
            seen.add(g["id"])
            assert "cmd" in g, f"goal {g['id']} missing cmd"
            assert "pass_when" in g, f"goal {g['id']} missing pass_when"
    assert seen == spot_check_ids, (
        f"missing wired goals: {spot_check_ids - seen}"
    )


def test_run_check_resolves_implement_gates(tmp_path):
    """Each implement check is dispatchable via `peers run-check`.

    The checks expect a PLAN.md / .peers/ layout that the tmp_path lacks,
    so each one is expected to FAIL (rc=1) with a domain-specific
    diagnostic — NOT to fail with "no such check" (which would mean
    the dispatcher couldn't find the script in the implement mode).
    """
    implement_checks = [
        "plan_checklist_empty",
        "acceptance_pass",
        "e2e_pass",
        "plan_step_traceable",
        "plan_original_preserved",
        "coverage_3class_delta",
        "contracts_unchanged",
    ]
    for check in implement_checks:
        res = _peers_run_check(check, tmp_path)
        # rc 0 or 1 are valid dispatch outcomes (rc=2 = argparse failure,
        # which would mean our invocation shape is wrong).
        assert res.returncode in (0, 1), (
            f"check {check} dispatch failed: rc={res.returncode}, "
            f"stderr={res.stderr!r}"
        )
        # Dispatcher's not-found message would land in stderr; the check's
        # own diagnostics land in stdout. If we see "no such check" in
        # stderr it means peers.modes.discover() didn't find the check
        # in the implement mode.
        assert "no such check" not in res.stderr, (
            f"check {check} not discovered by run-check dispatcher: "
            f"stderr={res.stderr!r}"
        )


def test_fake_convergence_all_gates_pass(tmp_path):
    """End-to-end: all PLAN.md steps checked, commit SHAs valid, contracts
    intact, acceptance passes, happy+edge+sad tests present.

    All seven implement-mode hard gates evaluate green (rc=0).
    """
    # Init a git repo so plan-step-traceable + coverage-3class-delta
    # can resolve commit SHAs.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "t@t.t"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "T"],
        check=True,
    )

    # Create src + tests with happy/edge/sad classified names so
    # coverage_3class_delta can find all three classes via KIND_RE.
    src = tmp_path / "src" / "auth.py"
    src.parent.mkdir(parents=True)
    src.write_text("def auth(token): return token == 'valid'\n")
    test = tmp_path / "tests" / "test_auth.py"
    test.parent.mkdir(parents=True)
    test.write_text(
        "from src.auth import auth\n"
        "def test_auth_happy_valid(): assert auth('valid')\n"
        "def test_auth_edge_empty(): assert not auth('')\n"
        "def test_auth_sad_invalid(): assert not auth('xxx')\n"
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "."], check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "step-1 auth"],
        check=True,
    )
    sha_full = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    sha = sha_full[:7]

    # PLAN.md with the step checked off + trailing (SHA) annotation and
    # touches that intersect the commit's changed files.
    plan = tmp_path / "PLAN.md"
    plan.write_text(
        "# IntegrationFeature\n"
        "## Meta\n"
        "surfaces: [cli]\n"
        "acceptance: false\n"
        "## Steps\n"
        f"- [x] [STEP-1] add auth ({sha})\n"
        "  - touches: src/auth.py, tests/test_auth.py\n"
    )

    # Frozen contracts: build the real init-time layout via the same
    # library `peers-ctl new --modes=implement` calls, so the fixture can
    # never drift from the contract format the gates enforce. (A prior
    # hand-rolled fixture silently went stale when BUG-178 added the
    # hash-chained contracts.log that verify_contracts now requires.)
    plan_dir = tmp_path / ".peers"
    write_frozen_contracts(
        plan_dir,
        acceptance="exit 0",  # passes => acceptance_pass green
        e2e=None,  # no e2e.sh => e2e_pass skips (rc 0)
        # Original == current PLAN.md (no steps dropped between init and
        # the convergence claim) => plan_original_preserved green.
        plan_md_content=plan.read_text(),
    )

    # Run each implement-mode gate.
    implement_checks = [
        "plan_checklist_empty",
        "acceptance_pass",
        "e2e_pass",  # skipped (no e2e.sh) -- still rc=0
        "plan_step_traceable",
        "plan_original_preserved",
        "coverage_3class_delta",
        "contracts_unchanged",
    ]
    results: dict[str, tuple[int, str]] = {}
    for check in implement_checks:
        res = _peers_run_check(check, tmp_path)
        results[check] = (res.returncode, res.stdout + res.stderr)

    failures = {c: out for c, (rc, out) in results.items() if rc != 0}
    assert not failures, (
        "fake convergence scenario failed gates: "
        + json.dumps(failures, indent=2)
    )
