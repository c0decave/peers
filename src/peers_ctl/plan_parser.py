"""PLAN.md schema parser + validator for implement-mode.

See docs/plans/2026-05-26-implement-mode-implementation.md Task 1.1.

A PLAN.md is a tiny markdown dialect with four well-known sections:

    # <Feature Name>

    ## Meta
    surfaces: [cli, web, lib]
    acceptance: pytest tests/acceptance/
    e2e: playwright test e2e/        # required when 'web' or 'gui'

    ## Architecture
    - Component A: handles X

    ## Input Domains
    - user_id: int 1..2**31

    ## Steps
    - [ ] [STEP-1] Add auth middleware
      - touches: src/middleware/auth.py, tests/test_auth.py
      - rationale: required by STEP-2
    - [ ] [STEP-2] Add session store
      - touches: src/session/store.py
      - depends: [STEP-1]

In addition to the `[ ]` / `[x]` checkbox markers, step lines may carry
one of the named state markers `[PARTIAL]`, `[BLOCKED]`, or
`[BLOCKED-ACK]` (Task 7.1 escape valves). They map to Step.state values
`partial`, `blocked`, and `blocked-ack` respectively.

Every step must declare `touches:` so the reviewer-only-checkoff
enforcement (pre-commit hook + post-hoc gate) has something to anchor
on. Two opt-outs exist for steps that legitimately touch no source
files: `trivial_step: true` (Task 5.5) and `pure_documentation: true`
(Issue I4).

The parser is intentionally line-oriented (no full markdown library)
and rejects anything ambiguous via PlanValidationError.
"""
from __future__ import annotations

import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path


_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$")
# Step lines accept either a one-char checkbox (`[ ]` / `[x]` / `[X]`) or one
# of the named state markers `[PARTIAL]` / `[BLOCKED]` / `[BLOCKED-ACK]`.
# The marker maps to Step.state via _STATE_BY_MARK below.
_STEP_RE = re.compile(
    r"^-\s*\[(?P<mark>[ xX]|PARTIAL|BLOCKED|BLOCKED-ACK)\]\s*"
    r"\[(?P<id>STEP-\d+)\]\s*(?P<text>.+?)\s*$"
)
# Closed vocabulary mapping checkbox marker -> Step.state value. Anything
# else is a parser bug (the regex would not have matched).
_STATE_BY_MARK: dict[str, str] = {
    " ": "open",
    "x": "done",
    "X": "done",
    "PARTIAL": "partial",
    "BLOCKED": "blocked",
    "BLOCKED-ACK": "blocked-ack",
}
_SUBKEY_RE = re.compile(r"^\s+-\s*(?P<key>[a-z_][a-z0-9_]*)\s*:\s*(?P<val>.*?)\s*$")
_META_KV_RE = re.compile(r"^(?P<key>[a-z_][a-z0-9_]*)\s*:\s*(?P<val>.*?)\s*$")
# closed vocabulary for `surfaces:` so typos don't silently
# bypass the e2e requirement (`web`/`gui` trigger an e2e: contract).
_VALID_SURFACES = frozenset({"cli", "web", "lib", "gui"})
_BULLET_RE = re.compile(r"^-\s+(?P<text>.+?)\s*$")
_LIST_INLINE_RE = re.compile(r"^\[(?P<inner>.*)\]$")
# Trailing `(SHA)` annotation on a step's text line. The SHA is a hex
# string 7..40 chars long; anything outside that range is treated as
# ordinary parenthesised prose and left in the text untouched.
_STEP_SHA_RE = re.compile(r"^(?P<text>.+?)\s*\((?P<sha>[0-9a-f]{7,40})\)\s*$")


class PlanValidationError(ValueError):
    """Raised when a PLAN.md fails schema or semantic validation."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or "plan validation failed")


@dataclass
class Step:
    id: str
    text: str
    touches: list[str] = field(default_factory=list)
    depends: list[str] = field(default_factory=list)
    rationale: str = ""
    state: str = "open"
    trivial: bool = False
    pure_refactor: bool = False
    # `pure_documentation: true` exempts the step from the touches:
    # declaration requirement enforced by `_validate_touches_declared`
    # (Issue I4 -- without this opt-out, doc-only steps would have to
    # invent a touches: file just to satisfy the validator).
    pure_documentation: bool = False
    # Trailing `(SHA)` annotation on the step line, captured so that the
    # `plan-step-traceable` gate (and future delivery-report tooling) can
    # cross-check completed steps against real git commits. None when no
    # annotation is present.
    commit_sha: str | None = None


@dataclass
class Plan:
    name: str
    surfaces: list[str]
    acceptance: str
    e2e: str | None = None
    mutation_testing: bool = False
    convergence_n: int = 5
    honesty_audit_peer: str | None = None
    architecture: list[str] = field(default_factory=list)
    input_domains: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)


def parse_plan(path: Path) -> Plan:
    """Parse a PLAN.md file at ``path`` into a validated ``Plan``.

    Raises PlanValidationError on any schema or semantic problem.
    """
    text = Path(path).read_text(encoding="utf-8")
    lines = text.splitlines()

    name = _extract_title(lines)
    sections = _split_sections(lines)
    if "Meta" not in sections:
        raise PlanValidationError("missing required `## Meta` section")
    if "Steps" not in sections:
        raise PlanValidationError("missing required `## Steps` section")

    meta = _parse_meta(sections["Meta"])
    architecture = _parse_bullet_list(sections.get("Architecture", []))
    input_domains = _parse_bullet_list(sections.get("Input Domains", []))
    steps = _parse_steps(sections["Steps"])

    plan = Plan(
        name=name,
        surfaces=meta["surfaces"],
        acceptance=meta["acceptance"],
        e2e=meta.get("e2e"),
        mutation_testing=meta.get("mutation_testing", False),
        convergence_n=meta.get("convergence_n", 5),
        honesty_audit_peer=meta.get("honesty_audit_peer"),
        architecture=architecture,
        input_domains=input_domains,
        steps=steps,
    )

    _validate_e2e(plan)
    _validate_step_ids(plan.steps)
    _validate_depends(plan.steps)
    _validate_touches_declared(plan.steps)
    return plan


# --- section splitting ----------------------------------------------------


def _extract_title(lines: list[str]) -> str:
    for line in lines:
        if not line.strip():
            continue
        m = _TITLE_RE.match(line)
        if not m:
            raise PlanValidationError(
                "PLAN.md must start with `# <Feature Name>`"
            )
        return m.group(1)
    raise PlanValidationError("PLAN.md is empty")


def _split_sections(lines: list[str]) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines:
        if line.startswith("#") and not line.startswith("##"):
            # The top-level title — ends any prior section.
            current = None
            continue
        m = _HEADING_RE.match(line)
        if m:
            name = m.group(1).strip()
            if name in sections:
                raise PlanValidationError(
                    f"duplicate section: ## {name}"
                )
            sections[name] = []
            current = name
            continue
        if current is not None:
            sections[current].append(line)
    return sections


# --- meta -----------------------------------------------------------------


def _parse_meta(body: list[str]) -> dict[str, object]:
    out: dict[str, object] = {}
    for raw in body:
        line = raw.rstrip()
        # Strip inline `# comment` tails (only outside list brackets is
        # the safe case; we accept the common "key: val   # note" form).
        stripped = _strip_inline_comment(line).strip()
        if not stripped:
            continue
        m = _META_KV_RE.match(stripped)
        if not m:
            continue
        key = m.group("key")
        val = m.group("val").strip()
        if key == "surfaces":
            out["surfaces"] = _parse_inline_list(val, key="surfaces")
        elif key == "mutation_testing":
            out["mutation_testing"] = _parse_bool(val, key="mutation_testing")
        elif key == "convergence_n":
            out["convergence_n"] = _parse_int(val, key="convergence_n")
        else:
            out[key] = val
    if "surfaces" not in out:
        raise PlanValidationError("Meta is missing required key `surfaces`")
    # enforce a closed vocabulary on `surfaces` so a typo
    # such as `[weeb]` or an empty `[]` cannot bypass the e2e contract.
    surfaces = out["surfaces"]
    if not isinstance(surfaces, list) or not surfaces:
        raise PlanValidationError(
            "Meta key `surfaces` must be a non-empty list — pick from "
            f"{sorted(_VALID_SURFACES)}"
        )
    bad = [s for s in surfaces if s not in _VALID_SURFACES]
    if bad:
        raise PlanValidationError(
            f"Meta key `surfaces` has unknown surface(s) {bad!r}; "
            f"valid surfaces are {sorted(_VALID_SURFACES)}"
        )
    if "acceptance" not in out or not str(out.get("acceptance", "")).strip():
        raise PlanValidationError("Meta is missing required key `acceptance`")
    return out


def _strip_inline_comment(line: str) -> str:
    # Drop a `#` and everything after it if it's not inside `[...]`.
    depth = 0
    for i, ch in enumerate(line):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        elif ch == "#" and depth == 0:
            return line[:i]
    return line


def _parse_inline_list(val: str, *, key: str) -> list[str]:
    val = val.strip()
    m = _LIST_INLINE_RE.match(val)
    if not m:
        raise PlanValidationError(
            f"Meta key `{key}` must be a `[a, b, c]` list, got {val!r}"
        )
    inner = m.group("inner").strip()
    if not inner:
        return []
    return [item.strip() for item in inner.split(",") if item.strip()]


def _parse_bool(val: str, *, key: str) -> bool:
    low = val.strip().lower()
    if low in ("true", "yes", "on", "1"):
        return True
    if low in ("false", "no", "off", "0", ""):
        return False
    raise PlanValidationError(
        f"Meta key `{key}` must be a boolean, got {val!r}"
    )


def _parse_int(val: str, *, key: str) -> int:
    try:
        return int(val.strip())
    except ValueError as e:
        raise PlanValidationError(
            f"Meta key `{key}` must be an integer, got {val!r}"
        ) from e


# --- bullet sections ------------------------------------------------------


def _parse_bullet_list(body: list[str]) -> list[str]:
    out: list[str] = []
    for raw in body:
        line = raw.rstrip()
        if not line.strip():
            continue
        m = _BULLET_RE.match(line)
        if m:
            out.append(m.group("text").strip())
    return out


# --- steps ----------------------------------------------------------------


def _parse_steps(body: list[str]) -> list[Step]:
    steps: list[Step] = []
    current: Step | None = None
    for raw in body:
        line = raw.rstrip()
        if not line.strip():
            continue
        m_step = _STEP_RE.match(line)
        if m_step:
            state = _STATE_BY_MARK[m_step.group("mark")]
            raw_text = m_step.group("text").strip()
            commit_sha: str | None = None
            m_sha = _STEP_SHA_RE.match(raw_text)
            if m_sha:
                raw_text = m_sha.group("text").strip()
                commit_sha = m_sha.group("sha")
            current = Step(
                id=m_step.group("id"),
                text=raw_text,
                state=state,
                commit_sha=commit_sha,
            )
            steps.append(current)
            continue
        m_sub = _SUBKEY_RE.match(line)
        if m_sub and current is not None:
            key = m_sub.group("key")
            val = m_sub.group("val").strip()
            if key == "touches":
                current.touches = _parse_csv_or_list(val)
            elif key == "depends":
                current.depends = _parse_csv_or_list(val)
            elif key == "rationale":
                current.rationale = val
            elif key == "trivial" or key == "trivial_step":
                # `trivial_step` is the canonical name used by the
                # Task 5.5 cleanliness gates (min-impl-size-per-step);
                # `trivial` is the original short form. Both populate
                # the same Step.trivial flag.
                current.trivial = _parse_bool(val, key=key)
            elif key == "pure_refactor":
                current.pure_refactor = _parse_bool(val, key="pure_refactor")
            elif key == "pure_documentation":
                current.pure_documentation = _parse_bool(
                    val, key="pure_documentation"
                )
            # Unknown sub-keys are ignored deliberately; future fields
            # should land here before validation tightens.
    return steps


def _parse_csv_or_list(val: str) -> list[str]:
    val = val.strip()
    if not val:
        return []
    m = _LIST_INLINE_RE.match(val)
    if m:
        inner = m.group("inner").strip()
        if not inner:
            return []
        return [item.strip() for item in inner.split(",") if item.strip()]
    return [item.strip() for item in val.split(",") if item.strip()]


# --- semantic validators --------------------------------------------------


def _validate_e2e(plan: Plan) -> None:
    needs_e2e = any(s in plan.surfaces for s in ("web", "gui"))
    if needs_e2e and not plan.e2e:
        raise PlanValidationError(
            "surfaces include `web` or `gui`; Meta key `e2e:` is required"
        )


def _validate_step_ids(steps: list[Step]) -> None:
    if not steps:
        raise PlanValidationError("plan has no steps")
    seen: set[str] = set()
    for idx, step in enumerate(steps, start=1):
        expected = f"STEP-{idx}"
        if step.id in seen:
            raise PlanValidationError(
                f"duplicate step id: {step.id}"
            )
        seen.add(step.id)
        if step.id != expected:
            raise PlanValidationError(
                f"step ids must be sequential; expected {expected}, "
                f"got {step.id}"
            )


def _validate_touches_declared(steps: list[Step]) -> None:
    """Every step must declare `touches:` unless explicitly exempt.

    Issue I4 closure: the reviewer-only-checkoff enforcement (pre-commit
    hook + the post-hoc `checkoff_by_other_peer` gate) both skip steps
    without a `touches:` list -- omitting `touches:` was a silent
    escape from the rule. Validating up front means a peer cannot
    bypass enforcement by writing a vague step; they must either name
    the files (`touches: ...`), declare the step trivially small
    (`trivial_step: true`), or declare it documentation-only
    (`pure_documentation: true`).
    """
    missing: list[str] = []
    for s in steps:
        if s.touches:
            continue
        if s.trivial or s.pure_documentation:
            continue
        missing.append(s.id)
    if missing:
        raise PlanValidationError(
            "steps missing `touches:` declaration (use `trivial_step: "
            "true` or `pure_documentation: true` to exempt): "
            + ", ".join(missing)
        )


def _validate_depends(steps: list[Step]) -> None:
    known = {s.id for s in steps}
    for step in steps:
        for dep in step.depends:
            if dep not in known:
                raise PlanValidationError(
                    f"step {step.id} depends on unknown step {dep}"
                )
            if dep == step.id:
                raise PlanValidationError(
                    f"step {step.id} cannot depend on itself (cycle)"
                )
    # Kahn's algorithm: each node's indegree is its number of deps;
    # we remove nodes whose deps are all satisfied. If any remain,
    # there is a cycle somewhere in `depends`.
    indegree: dict[str, int] = {s.id: len(s.depends) for s in steps}
    dependents: dict[str, list[str]] = defaultdict(list)
    for s in steps:
        for dep in s.depends:
            dependents[dep].append(s.id)
    queue: deque[str] = deque(sid for sid, n in indegree.items() if n == 0)
    visited = 0
    while queue:
        sid = queue.popleft()
        visited += 1
        for child in dependents.get(sid, []):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if visited != len(steps):
        remaining = sorted(sid for sid, n in indegree.items() if n > 0)
        raise PlanValidationError(
            f"depends graph has a cycle involving: {', '.join(remaining)}"
        )
