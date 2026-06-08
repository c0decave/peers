"""Tier-1 Part B: hard gates split into 'expensive' (pytest/coverage-backed,
run async on the frozen SHA) vs 'cheap' (run synchronously fresh each tick).
"""
from __future__ import annotations

from pathlib import Path

from peers.goals import Goal, load_goals
from peers.goal_engine import GoalEngine


def test_goal_expensive_defaults_false() -> None:
    g = Goal(id="x", type="hard", cmd="true", pass_when="exit_code == 0")
    assert g.expensive is False


def test_expensive_parsed_from_yaml(tmp_path: Path) -> None:
    p = tmp_path / "goals.yaml"
    p.write_text(
        "goals:\n"
        "  - id: tests-pass\n"
        "    type: hard\n"
        "    cmd: 'true'\n"
        "    pass_when: 'exit_code == 0'\n"
        "    expensive: true\n"
        "  - id: lint\n"
        "    type: hard\n"
        "    cmd: 'true'\n"
        "    pass_when: 'exit_code == 0'\n"
    )
    goals = {g.id: g for g in load_goals(p)}
    assert goals["tests-pass"].expensive is True
    assert goals["lint"].expensive is False


def test_audit_template_marks_pytest_gates_expensive() -> None:
    import peers
    tmpl = (Path(peers.__file__).parent
            / "templates" / "modes" / "audit" / "goals.yaml")
    goals = {g.id: g for g in load_goals(tmpl)}
    # The two full-pytest-suite gates are overlapped (expensive).
    assert goals["tests-pass"].expensive is True
    assert goals["no-prior-regression"].expensive is True
    # Fast/static gates stay cheap (run synchronously, fresh each tick).
    assert goals["lint-clean"].expensive is False
    assert goals["tests-no-unjustified-skip-or-fail"].expensive is False


def test_engine_partitions_expensive_and_cheap(tmp_path: Path) -> None:
    cheap = Goal(id="lint", type="hard", cmd="true", pass_when="exit_code == 0")
    exp = Goal(id="tests-pass", type="hard", cmd="true",
               pass_when="exit_code == 0", expensive=True)
    soft = Goal(id="rev", type="soft")  # soft gates are neither
    eng = GoalEngine([cheap, exp, soft], cwd=tmp_path)
    assert eng.expensive_ids() == {"tests-pass"}
    assert eng.cheap_ids() == {"lint"}
