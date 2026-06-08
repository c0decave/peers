"""Tests for the implement-mode PLAN.md schema parser + validator.

See docs/plans/2026-05-26-implement-mode-implementation.md Task 1.1.
The parser walks a markdown file with `## Meta` / `## Architecture` /
`## Input Domains` / `## Steps` sections and produces a typed `Plan`
with `Step` entries. All validation failures raise PlanValidationError.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import peers_ctl.plan_parser as plan_parser_mod
from peers_ctl.plan_parser import (
    Plan,
    PlanValidationError,
    Step,
    parse_plan,
)


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "PLAN.md"
    p.write_text(text, encoding="utf-8")
    return p


MINIMAL = """\
# My Feature

## Meta
surfaces: [cli]
acceptance: pytest tests/acceptance/

## Steps
- [ ] [STEP-1] Do the thing
  - touches: src/thing.py
"""


def test_minimal_valid_plan(tmp_path: Path) -> None:
    plan = parse_plan(_write(tmp_path, MINIMAL))
    assert isinstance(plan, Plan)
    assert plan.name == "My Feature"
    assert plan.surfaces == ["cli"]
    assert plan.acceptance == "pytest tests/acceptance/"
    assert plan.e2e is None
    assert plan.mutation_testing is False
    assert plan.convergence_n == 5
    assert plan.honesty_audit_peer is None
    assert plan.architecture == []
    assert plan.input_domains == []
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert isinstance(step, Step)
    assert step.id == "STEP-1"
    assert step.text == "Do the thing"
    assert step.touches == ["src/thing.py"]
    assert step.depends == []
    assert step.rationale == ""
    assert step.state == "open"
    assert step.trivial is False
    assert step.pure_refactor is False


def test_rejects_symlinked_plan_BUG_254(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text(MINIMAL, encoding="utf-8")
    plan_path = tmp_path / "PLAN.md"
    plan_path.symlink_to(outside)

    with pytest.raises(PlanValidationError, match="symlink|symbolic|unsafe"):
        parse_plan(plan_path)


def test_rejects_invalid_utf8_plan_BUG_254(tmp_path: Path) -> None:
    plan_path = tmp_path / "PLAN.md"
    plan_path.write_bytes(b"\xff\xfe# not utf-8\n")

    with pytest.raises(PlanValidationError, match="UTF-8"):
        parse_plan(plan_path)


def test_rejects_oversized_plan_BUG_254(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path = _write(tmp_path, MINIMAL)
    monkeypatch.setattr(
        plan_parser_mod, "_PLAN_MAX_BYTES", len(MINIMAL.encode("utf-8")) - 1
    )

    with pytest.raises(PlanValidationError, match="too large"):
        parse_plan(plan_path)


def test_missing_acceptance_fails(tmp_path: Path) -> None:
    text = """\
# F

## Meta
surfaces: [cli]

## Steps
- [ ] [STEP-1] x
"""
    with pytest.raises(PlanValidationError, match="acceptance"):
        parse_plan(_write(tmp_path, text))


def test_missing_surfaces_fails(tmp_path: Path) -> None:
    text = """\
# F

## Meta
acceptance: pytest

## Steps
- [ ] [STEP-1] x
"""
    with pytest.raises(PlanValidationError, match="surfaces"):
        parse_plan(_write(tmp_path, text))


def test_web_surface_requires_e2e(tmp_path: Path) -> None:
    text = """\
# F

## Meta
surfaces: [web]
acceptance: pytest

## Steps
- [ ] [STEP-1] x
"""
    with pytest.raises(PlanValidationError, match="e2e"):
        parse_plan(_write(tmp_path, text))


def test_gui_surface_requires_e2e(tmp_path: Path) -> None:
    text = """\
# F

## Meta
surfaces: [gui]
acceptance: pytest

## Steps
- [ ] [STEP-1] x
"""
    with pytest.raises(PlanValidationError, match="e2e"):
        parse_plan(_write(tmp_path, text))


def test_empty_surfaces_list_fails_BUG_166(tmp_path: Path) -> None:
    """BUG-166: `surfaces: []` must be rejected — an empty list can
    bypass the e2e enforcement for UI projects."""
    text = """\
# F

## Meta
surfaces: []
acceptance: pytest

## Steps
- [ ] [STEP-1] x
  - touches: src/x.py
"""
    with pytest.raises(PlanValidationError, match="surfaces"):
        parse_plan(_write(tmp_path, text))


def test_unknown_surface_token_fails_BUG_166(tmp_path: Path) -> None:
    """BUG-166: a typo such as `weeb` (instead of `web`) must NOT be
    silently accepted — that would bypass the e2e contract."""
    text = """\
# F

## Meta
surfaces: [weeb]
acceptance: pytest

## Steps
- [ ] [STEP-1] x
  - touches: src/x.py
"""
    with pytest.raises(PlanValidationError, match="surface"):
        parse_plan(_write(tmp_path, text))


def test_unknown_surface_token_with_real_surface_fails_BUG_166(
    tmp_path: Path,
) -> None:
    """BUG-166: mixing a real surface with a typo also fails — every
    token must come from the closed vocabulary."""
    text = """\
# F

## Meta
surfaces: [cli, webb]
acceptance: pytest

## Steps
- [ ] [STEP-1] x
  - touches: src/x.py
"""
    with pytest.raises(PlanValidationError, match="surface"):
        parse_plan(_write(tmp_path, text))


def test_cli_surface_no_e2e_needed(tmp_path: Path) -> None:
    text = """\
# F

## Meta
surfaces: [cli, lib]
acceptance: pytest

## Steps
- [ ] [STEP-1] x
  - touches: src/x.py
"""
    plan = parse_plan(_write(tmp_path, text))
    assert plan.surfaces == ["cli", "lib"]
    assert plan.e2e is None


def test_step_ids_must_be_sequential(tmp_path: Path) -> None:
    text = """\
# F

## Meta
surfaces: [cli]
acceptance: pytest

## Steps
- [ ] [STEP-1] a
- [ ] [STEP-3] c
"""
    with pytest.raises(PlanValidationError, match="STEP-2"):
        parse_plan(_write(tmp_path, text))


def test_step_ids_no_duplicates(tmp_path: Path) -> None:
    text = """\
# F

## Meta
surfaces: [cli]
acceptance: pytest

## Steps
- [ ] [STEP-1] a
- [ ] [STEP-1] b
"""
    with pytest.raises(PlanValidationError, match="duplicate"):
        parse_plan(_write(tmp_path, text))


def test_depends_cycle_fails(tmp_path: Path) -> None:
    text = """\
# F

## Meta
surfaces: [cli]
acceptance: pytest

## Steps
- [ ] [STEP-1] a
  - depends: [STEP-2]
- [ ] [STEP-2] b
  - depends: [STEP-1]
"""
    with pytest.raises(PlanValidationError, match="cycle"):
        parse_plan(_write(tmp_path, text))


def test_depends_unknown_step_fails(tmp_path: Path) -> None:
    text = """\
# F

## Meta
surfaces: [cli]
acceptance: pytest

## Steps
- [ ] [STEP-1] a
  - depends: [STEP-99]
"""
    with pytest.raises(PlanValidationError, match="STEP-99"):
        parse_plan(_write(tmp_path, text))


def test_missing_touches_rejected(tmp_path: Path) -> None:
    """Issue I4: a step without `touches:` (and without an exemption)
    is rejected up front. Otherwise reviewer-only-checkoff enforcement
    silently no-ops on that step (the hook and post-hoc gate both
    skip touch-less entries)."""
    text = """\
# F

## Meta
surfaces: [cli]
acceptance: pytest

## Steps
- [ ] [STEP-1] just a thought
"""
    with pytest.raises(PlanValidationError, match="touches"):
        parse_plan(_write(tmp_path, text))


def test_trivial_step_exempt_from_touches(tmp_path: Path) -> None:
    """Issue I4: `trivial_step: true` opts a step out of the touches
    requirement (and out of the min-impl-size gate, Task 5.5)."""
    text = """\
# F

## Meta
surfaces: [cli]
acceptance: pytest

## Steps
- [ ] [STEP-1] one-liner config bump
  - trivial_step: true
"""
    plan = parse_plan(_write(tmp_path, text))
    assert plan.steps[0].touches == []
    assert plan.steps[0].trivial is True


def test_pure_documentation_exempt_from_touches(tmp_path: Path) -> None:
    """Issue I4: `pure_documentation: true` opts a doc-only step out of
    the touches requirement -- doc steps legitimately touch no source
    files and shouldn't have to invent a phony entry."""
    text = """\
# F

## Meta
surfaces: [cli]
acceptance: pytest

## Steps
- [ ] [STEP-1] update README with new flag
  - pure_documentation: true
"""
    plan = parse_plan(_write(tmp_path, text))
    assert plan.steps[0].touches == []
    assert plan.steps[0].pure_documentation is True


def test_full_example_with_architecture_and_input_domains(
    tmp_path: Path,
) -> None:
    text = """\
# Auth Feature

## Meta
surfaces: [cli, web, lib]
acceptance: pytest tests/acceptance/
e2e: playwright test e2e/
mutation_testing: true
convergence_n: 7
honesty_audit_peer: gemini

## Architecture
- Component A: handles X
- Component B: handles Y

## Input Domains
- user_id: int 1..2**31
- email: str non-empty

## Steps
- [ ] [STEP-1] Add auth middleware
  - touches: src/middleware/auth.py, tests/test_auth.py
  - rationale: required by STEP-2
- [ ] [STEP-2] Add session store
  - touches: src/session/store.py
  - depends: [STEP-1]
"""
    plan = parse_plan(_write(tmp_path, text))
    assert plan.name == "Auth Feature"
    assert plan.surfaces == ["cli", "web", "lib"]
    assert plan.acceptance == "pytest tests/acceptance/"
    assert plan.e2e == "playwright test e2e/"
    assert plan.mutation_testing is True
    assert plan.convergence_n == 7
    assert plan.honesty_audit_peer == "gemini"
    assert plan.architecture == [
        "Component A: handles X",
        "Component B: handles Y",
    ]
    assert plan.input_domains == [
        "user_id: int 1..2**31",
        "email: str non-empty",
    ]
    assert len(plan.steps) == 2
    s1, s2 = plan.steps
    assert s1.id == "STEP-1"
    assert s1.text == "Add auth middleware"
    assert s1.touches == [
        "src/middleware/auth.py",
        "tests/test_auth.py",
    ]
    assert s1.rationale == "required by STEP-2"
    assert s1.depends == []
    assert s2.id == "STEP-2"
    assert s2.text == "Add session store"
    assert s2.touches == ["src/session/store.py"]
    assert s2.depends == ["STEP-1"]


def test_duplicate_meta_section_rejected(tmp_path: Path) -> None:
    text = """\
# F

## Meta
surfaces: [cli]
acceptance: pytest

## Meta
surfaces: [web]
acceptance: pytest

## Steps
- [ ] [STEP-1] x
"""
    with pytest.raises(PlanValidationError, match="duplicate section: ## Meta"):
        parse_plan(_write(tmp_path, text))


def test_duplicate_steps_section_rejected(tmp_path: Path) -> None:
    text = """\
# F

## Meta
surfaces: [cli]
acceptance: pytest

## Steps
- [ ] [STEP-1] a

## Steps
- [ ] [STEP-2] b
"""
    with pytest.raises(PlanValidationError, match="duplicate section: ## Steps"):
        parse_plan(_write(tmp_path, text))


def test_checked_step_state_done(tmp_path: Path) -> None:
    text = """\
# F

## Meta
surfaces: [cli]
acceptance: pytest

## Steps
- [x] [STEP-1] foo
  - touches: src/foo.py
"""
    plan = parse_plan(_write(tmp_path, text))
    assert plan.steps[0].state == "done"


def test_unchecked_step_state_open(tmp_path: Path) -> None:
    text = """\
# F

## Meta
surfaces: [cli]
acceptance: pytest

## Steps
- [ ] [STEP-1] foo
  - touches: src/foo.py
"""
    plan = parse_plan(_write(tmp_path, text))
    assert plan.steps[0].state == "open"


def test_step_with_commit_sha_parsed(tmp_path: Path) -> None:
    """A trailing `(SHA)` annotation is split off into step.commit_sha."""
    text = """\
# F

## Meta
surfaces: [cli]
acceptance: pytest

## Steps
- [x] [STEP-1] add auth (a7f96c3)
  - touches: src/auth.py
- [x] [STEP-2] add session (abcdef1234567890abcdef1234567890abcdef12)
  - touches: src/session.py
"""
    plan = parse_plan(_write(tmp_path, text))
    assert plan.steps[0].text == "add auth"
    assert plan.steps[0].commit_sha == "a7f96c3"
    assert plan.steps[1].text == "add session"
    assert plan.steps[1].commit_sha == "abcdef1234567890abcdef1234567890abcdef12"


def test_step_without_commit_sha_parsed(tmp_path: Path) -> None:
    """No trailing `(SHA)` annotation leaves step.commit_sha as None."""
    text = """\
# F

## Meta
surfaces: [cli]
acceptance: pytest

## Steps
- [ ] [STEP-1] todo item
  - touches: src/todo.py
- [x] [STEP-2] done but no sha annotation
  - touches: src/done.py
"""
    plan = parse_plan(_write(tmp_path, text))
    assert plan.steps[0].text == "todo item"
    assert plan.steps[0].commit_sha is None
    assert plan.steps[1].text == "done but no sha annotation"
    assert plan.steps[1].commit_sha is None


def test_trivial_step_alias_populates_trivial(tmp_path: Path) -> None:
    """`trivial_step: true` is the Task-5.5 canonical alias for `trivial`."""
    text = """\
# F

## Meta
surfaces: [cli]
acceptance: pytest

## Steps
- [ ] [STEP-1] short alias
  - trivial: true
- [ ] [STEP-2] long alias
  - trivial_step: true
- [ ] [STEP-3] no flag
  - touches: src/x.py
"""
    plan = parse_plan(_write(tmp_path, text))
    assert plan.steps[0].trivial is True
    assert plan.steps[1].trivial is True
    assert plan.steps[2].trivial is False


def test_partial_step_state(tmp_path: Path) -> None:
    """`[PARTIAL]` marker maps to Step.state == 'partial' (Task 7.1)."""
    text = """\
# F

## Meta
surfaces: [cli]
acceptance: pytest

## Steps
- [PARTIAL] [STEP-1] partly done
  - touches: src/p.py
"""
    plan = parse_plan(_write(tmp_path, text))
    assert plan.steps[0].state == "partial"


def test_blocked_step_state(tmp_path: Path) -> None:
    """`[BLOCKED]` marker maps to Step.state == 'blocked' (Task 7.1)."""
    text = """\
# F

## Meta
surfaces: [cli]
acceptance: pytest

## Steps
- [BLOCKED] [STEP-1] needs API
  - touches: src/api.py
"""
    plan = parse_plan(_write(tmp_path, text))
    assert plan.steps[0].state == "blocked"


def test_blocked_ack_step_state(tmp_path: Path) -> None:
    """`[BLOCKED-ACK]` maps to Step.state == 'blocked-ack' (Task 7.1)."""
    text = """\
# F

## Meta
surfaces: [cli]
acceptance: pytest

## Steps
- [BLOCKED-ACK] [STEP-1] user acknowledged
  - touches: src/x.py
"""
    plan = parse_plan(_write(tmp_path, text))
    assert plan.steps[0].state == "blocked-ack"


def test_mixed_state_markers(tmp_path: Path) -> None:
    """All five state markers coexist in one plan (Task 7.1)."""
    text = """\
# F

## Meta
surfaces: [cli]
acceptance: pytest

## Steps
- [x] [STEP-1] done
  - touches: src/a.py
- [BLOCKED] [STEP-2] blocked
  - touches: src/b.py
- [BLOCKED-ACK] [STEP-3] ack'd
  - touches: src/c.py
- [PARTIAL] [STEP-4] partial
  - touches: src/d.py
- [ ] [STEP-5] open
  - touches: src/e.py
"""
    plan = parse_plan(_write(tmp_path, text))
    states = [s.state for s in plan.steps]
    assert states == ["done", "blocked", "blocked-ack", "partial", "open"]


def test_pure_refactor_subkey_populates_flag(tmp_path: Path) -> None:
    """`pure_refactor: true` exempts the step from test-to-code-ratio."""
    text = """\
# F

## Meta
surfaces: [cli]
acceptance: pytest

## Steps
- [ ] [STEP-1] refactor only
  - touches: src/refactor.py
  - pure_refactor: true
- [ ] [STEP-2] normal step
  - touches: src/normal.py
"""
    plan = parse_plan(_write(tmp_path, text))
    assert plan.steps[0].pure_refactor is True
    assert plan.steps[1].pure_refactor is False
