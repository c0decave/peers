"""Test implement-mode templates load + register all gates (Task 2.8)."""
from __future__ import annotations

from pathlib import Path

import yaml


# Find the implement mode dir (tests/unit/<file> → repo root → src/peers/...)
IMPLEMENT_DIR = (
    Path(__file__).parent.parent.parent
    / "src"
    / "peers"
    / "templates"
    / "modes"
    / "implement"
)


def test_mode_yaml_exists_and_parses():
    p = IMPLEMENT_DIR / "mode.yaml"
    assert p.is_file()
    data = yaml.safe_load(p.read_text())
    assert data["name"] == "implement"
    # version may be int (existing modes use `1`) or string ("v1"); accept either
    version = data.get("version")
    assert version is not None
    assert str(version).lstrip("v").isdigit() or str(version).startswith("v")
    assert "description" in data


def test_goals_yaml_exists_and_parses():
    p = IMPLEMENT_DIR / "goals.yaml"
    assert p.is_file()
    data = yaml.safe_load(p.read_text())
    assert "goals" in data
    assert isinstance(data["goals"], list)


def test_required_gates_all_listed():
    """All 10 Schicht-1 hard gates must be registered."""
    p = IMPLEMENT_DIR / "goals.yaml"
    data = yaml.safe_load(p.read_text())
    ids = {g["id"] for g in data["goals"]}

    required = {
        "plan-checklist-empty",
        "acceptance-pass",
        "e2e-pass",
        "plan-step-traceable",
        "plan-original-preserved",
        "coverage-3class-delta",
        "contracts-unchanged",
        "no-prior-regression",
        "lint-clean",
        "tests-pass",
    }
    missing = required - ids
    assert not missing, f"missing required gates: {missing}"


def test_all_implement_check_files_referenced():
    """Every check file in implement/checks/ should be referenced by goals.yaml.

    Hard gates reference checks via `cmd:` (`peers run-check <name>`).
    Soft gates (Task 5.5 cleanliness scanners) reference them in the
    `prompt:` text (the reviewer is told which `run-check` to invoke);
    we accept either surface as proof the check is registered.
    """
    checks_dir = IMPLEMENT_DIR / "checks"
    check_files = {
        p.stem for p in checks_dir.glob("*.py") if p.stem != "__init__"
    }

    data = yaml.safe_load((IMPLEMENT_DIR / "goals.yaml").read_text())
    haystack = " ".join(
        str(g.get("cmd", "")) + " " + str(g.get("prompt", ""))
        for g in data["goals"]
    )

    for check in check_files:
        # check_name may use underscores in file, hyphens in id; either is fine
        check_id_underscore = check
        check_id_hyphen = check.replace("_", "-")
        assert (
            check_id_underscore in haystack or check_id_hyphen in haystack
        ), f"check file {check}.py not referenced in goals.yaml"


def test_budget_is_12h_default():
    p = IMPLEMENT_DIR / "goals.yaml"
    data = yaml.safe_load(p.read_text())
    if "budget" in data:
        assert data["budget"].get("max_runtime_s") == 43200


def test_all_hard_gates_have_pass_when():
    p = IMPLEMENT_DIR / "goals.yaml"
    data = yaml.safe_load(p.read_text())
    for g in data["goals"]:
        # Accept either `kind: hard` or the existing audit convention `type: hard`.
        if g.get("kind") == "hard" or g.get("type") == "hard":
            assert "pass_when" in g, f"hard gate {g['id']} missing pass_when"
            assert "cmd" in g, f"hard gate {g['id']} missing cmd"
