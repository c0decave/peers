#!/usr/bin/env python3
"""Reject test skips and xfails without a solid justification.

Scans test_*.py for `@pytest.mark.skip`, `@pytest.mark.xfail`, and
module-level `pytestmark = pytest.mark.skip(...)` markers. Each marker
must carry a `reason=` keyword whose value is non-empty, at least
MIN_REASON_LEN chars long, and not entirely composed of generic stop
words ("TODO", "FIXME", "broken", "flaky", "later", "skip", "wip"...).

`@pytest.mark.skipif(condition, reason=...)` is treated leniently: the
condition itself documents WHY, so any non-empty reason is accepted.

Rationale: it is too easy to silence a real failure by slapping
`@pytest.mark.skip(reason="TODO")` on a test. This check forces the
operator to either fix the test or explain in detail why the skip is
load-bearing — i.e., why production correctness is preserved despite
the test no longer running.
"""
from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

MIN_REASON_LEN = 20
GENERIC_WORDS = {
    "todo", "fixme", "broken", "flaky", "later", "skip", "skipped",
    "wip", "tbd", "xfail", "test", "tests", "na",
    "fail", "fails", "failing",
    "tmp", "temp", "temporary",
}


@dataclass
class _Finding:
    file: Path
    name: str  # test function name, or "<module>" for module-level
    marker: str  # "skip", "xfail", "skipif"
    problem: str  # human-readable description
    line: int


def _is_pytest_mark(node: ast.expr, names: set[str]) -> str | None:
    """Returns the marker name (e.g. "skip") if `node` resolves to
    `pytest.mark.<name>` for some `<name>` in `names`, else None."""
    if isinstance(node, ast.Call):
        return _is_pytest_mark(node.func, names)
    if isinstance(node, ast.Attribute):
        if node.attr in names:
            inner = node.value
            if (isinstance(inner, ast.Attribute) and inner.attr == "mark"
                    and isinstance(inner.value, ast.Name)
                    and inner.value.id == "pytest"):
                return node.attr
    return None


def _extract_reason(call: ast.Call) -> str | None:
    """Returns the value of `reason=` kwarg as str if present, else None."""
    for kw in call.keywords:
        if kw.arg == "reason" and isinstance(kw.value, ast.Constant) \
                and isinstance(kw.value.value, str):
            return kw.value.value
    return None


def _validate_reason(
    marker: str, reason: str | None,
) -> str | None:
    """Returns problem string if reason is inadequate, else None."""
    if reason is None:
        return "no reason given (use reason=\"…\")"
    stripped = reason.strip()
    if not stripped:
        return "empty reason"
    if marker == "skipif":
        # The condition argument documents WHY; any non-empty reason ok.
        return None
    if len(stripped) < MIN_REASON_LEN:
        return f"reason too short (<{MIN_REASON_LEN} chars): {stripped!r}"
    words = re.findall(r"[a-z]+", stripped.lower())
    if words and all(w in GENERIC_WORDS for w in words):
        return f"generic reason: {stripped!r}"
    return None


def _scan_decorator(
    dec: ast.expr,
) -> tuple[str, str | None] | None:
    """Inspects one decorator. Returns (marker, problem) if it's a
    skip/xfail/skipif marker, where problem is None for valid, else a
    description. Returns None if it isn't a skip-family marker at all."""
    marker = _is_pytest_mark(dec, {"skip", "xfail", "skipif"})
    if marker is None:
        return None
    if isinstance(dec, ast.Call):
        reason = _extract_reason(dec)
        return marker, _validate_reason(marker, reason)
    # bare `@pytest.mark.skip` without call → no reason possible
    if marker in {"skip", "xfail"}:
        return marker, "no reason given (bare decorator without args)"
    return marker, None


def _scan_function(
    func: ast.FunctionDef | ast.AsyncFunctionDef, file: Path,
) -> list[_Finding]:
    if not func.name.startswith("test_"):
        return []
    findings: list[_Finding] = []
    for dec in func.decorator_list:
        res = _scan_decorator(dec)
        if res is None:
            continue
        marker, problem = res
        if problem is not None:
            findings.append(_Finding(
                file=file, name=func.name, marker=marker,
                problem=problem, line=dec.lineno,
            ))
    return findings


def _scan_pytestmark_value(
    val: ast.expr, file: Path, line: int,
) -> list[_Finding]:
    findings: list[_Finding] = []
    # `pytestmark = pytest.mark.skip(...)` (single)
    res = _scan_decorator(val)
    if res is not None:
        marker, problem = res
        if problem is not None:
            findings.append(_Finding(
                file=file, name="<module-level pytestmark>",
                marker=marker, problem=problem, line=line,
            ))
        return findings
    # `pytestmark = [pytest.mark.skip(...), ...]`
    if isinstance(val, (ast.List, ast.Tuple)):
        for item in val.elts:
            findings.extend(_scan_pytestmark_value(item, file, line))
    return findings


def _scan_module(file: Path) -> list[_Finding]:
    try:
        tree = ast.parse(file.read_text(errors="ignore"))
    except SyntaxError as e:
        print(
            f"tests_no_unjustified_skip_or_fail: "
            f"warning: {file}: syntax error: {e}",
            file=sys.stderr,
        )
        return []
    findings: list[_Finding] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "pytestmark":
                    findings.extend(
                        _scan_pytestmark_value(node.value, file, node.lineno),
                    )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            findings.extend(_scan_function(node, file))
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    findings.extend(_scan_function(sub, file))
    return findings


def main(repo: str = ".") -> int:
    root = Path(repo)
    tests_dir = root / "tests"
    if not tests_dir.is_dir():
        print("tests_no_unjustified_skip_or_fail: clean (no tests/ dir)")
        return 0
    all_findings: list[_Finding] = []
    for py_file in sorted(tests_dir.rglob("test_*.py")):
        all_findings.extend(_scan_module(py_file))
    if not all_findings:
        print("tests_no_unjustified_skip_or_fail: clean")
        return 0
    print(
        f"tests_no_unjustified_skip_or_fail FAIL: "
        f"{len(all_findings)} unjustified skip/xfail marker(s):",
    )
    for f in all_findings:
        rel = f.file.relative_to(root) if f.file.is_relative_to(root) else f.file
        print(f"  {rel}:{f.line}: {f.name} [{f.marker}]: {f.problem}")
    print(
        "\nFix: add a reason that explains WHY the test is skipped/xfailed and\n"
        f"why this does not hide a real bug. Min {MIN_REASON_LEN} chars,\n"
        "must contain non-generic words. For pytest.mark.skipif, any non-empty\n"
        "reason suffices because the condition documents the WHY.",
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
