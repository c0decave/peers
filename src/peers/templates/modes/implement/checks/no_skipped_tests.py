#!/usr/bin/env python3
"""Exit 1 if ``tests/`` contains unsigned skip markers.

Schicht-5 cleanliness gate for implement-mode (Task 5.4). The cheapest
way to make a failing test "pass" is to skip it; this gate forbids
that vocabulary in ``tests/`` unless every occurrence carries a
reviewer-signed escape (same two-key pattern as ``no-shortcut-markers``
in Task 5.1).

Forbidden vocabulary
--------------------
Scanned in ``tests/`` ``.py`` files only:

* ``@pytest.mark.skip`` (with or without ``()`` / arguments)
* ``@pytest.mark.skipif`` (same)
* ``@unittest.skip`` / ``@unittest.skipIf`` / ``@unittest.skipUnless``
* ``pytest.skip(...)`` call inside a function body
* ``xit`` / ``xdescribe`` (textual, for JS-style port compat)

The decorator detection uses :mod:`ast` and matches on the dotted
attribute path so ``from pytest import mark`` style aliases are not
recognised -- callers must use the canonical ``pytest.mark.skip``
form for the gate to see them.

Escape via reviewer sign-off
----------------------------
Two ingredients are required, both at the same ``file:line``:

1. A ``# SKIP-REASON: <text>`` comment on the line *immediately
   before* the skip marker (decorator or call) explaining why.
2. ``.peers/justifications.log`` has a reviewer-signed entry for
   ``tests/<relpath>:<lineno>`` where ``<lineno>`` is the line of
   the skip marker itself (not the SKIP-REASON line).

Missing either half is a violation. We reuse the
:func:`peers_ctl.justifications.is_justified` lookup from Task 5.2
so a single ``justifications.log`` covers both shortcut-marker and
skipped-test escapes.

Exit codes
----------
* ``0`` -- ``tests/`` is clean, or every skip is annotated+signed,
  or no ``tests/`` directory exists.
* ``1`` -- at least one unsigned skip; stdout lists each violation
  as ``<relpath>:<lineno>: <kind>``.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

from peers_ctl.justifications import is_justified


_SKIP_REASON_TAG = "# SKIP-REASON:"

# Textual fallback markers (xit / xdescribe) -- the gate scans these
# line-by-line because they're free-form calls, not standard
# decorators we can easily AST-match.
_TEXTUAL_SKIP_TOKENS = ("xit(", "xdescribe(", "xit ", "xdescribe ")


def _decorator_path(dec: ast.expr) -> str:
    """Return the dotted source form of a decorator expression.

    For ``@pytest.mark.skip`` => ``"pytest.mark.skip"``; for
    ``@pytest.mark.skip(reason="x")`` => ``"pytest.mark.skip"`` (we
    strip the call form so the matcher is identical with/without
    arguments).
    """
    if isinstance(dec, ast.Call):
        return _decorator_path(dec.func)
    if isinstance(dec, ast.Attribute):
        return f"{_decorator_path(dec.value)}.{dec.attr}"
    if isinstance(dec, ast.Name):
        return dec.id
    # Anything else (subscripts, lambdas) -- not a skip marker.
    return ""


_SKIP_DECORATOR_PATHS = frozenset({
    "pytest.mark.skip",
    "pytest.mark.skipif",
    "unittest.skip",
    "unittest.skipIf",
    "unittest.skipUnless",
})


def _decorator_is_skip(dec: ast.expr) -> tuple[bool, str]:
    """Return ``(is_skip, kind_label)`` for ``dec``."""
    path = _decorator_path(dec)
    if path in _SKIP_DECORATOR_PATHS:
        return (True, path)
    return (False, "")


def _is_pytest_skip_call(node: ast.Call) -> bool:
    """True if ``node`` is a literal ``pytest.skip(...)`` call."""
    return _decorator_path(node.func) == "pytest.skip"


def _line_has_skip_reason(text_lines: list[str], lineno: int) -> bool:
    """True if the line *above* ``lineno`` contains ``# SKIP-REASON:``.

    ``lineno`` is 1-based (AST convention). We look at lineno-1 in
    1-based terms, i.e. the line immediately preceding the marker.
    A SKIP-REASON sitting any further back is not accepted -- the
    reason must be glued to its marker so the relationship is
    unambiguous.
    """
    prev_idx = lineno - 2  # convert to 0-based, step one above
    if prev_idx < 0 or prev_idx >= len(text_lines):
        return False
    return _SKIP_REASON_TAG in text_lines[prev_idx]


def _scan_python_file(
    path: Path, relpath: str, plan_dir: Path
) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    text_lines = text.splitlines()

    violations: list[str] = []

    # ---- AST-based scan: decorators + pytest.skip() calls. ----
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        tree = None

    seen_lines: set[tuple[int, str]] = set()

    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                for dec in node.decorator_list:
                    is_skip, kind = _decorator_is_skip(dec)
                    if not is_skip:
                        continue
                    lineno = dec.lineno
                    key = (lineno, kind)
                    if key in seen_lines:
                        continue
                    seen_lines.add(key)
                    if _line_has_skip_reason(text_lines, lineno):
                        signed, _signer = is_justified(
                            plan_dir, relpath, lineno
                        )
                        if signed:
                            continue
                        violations.append(
                            f"{relpath}:{lineno}: "
                            f"SKIP-REASON-but-unsigned: {kind}"
                        )
                        continue
                    violations.append(
                        f"{relpath}:{lineno}: {kind}: "
                        f"target={node.name}"
                    )
            if isinstance(node, ast.Call) and _is_pytest_skip_call(node):
                lineno = node.lineno
                key = (lineno, "pytest.skip")
                if key in seen_lines:
                    continue
                seen_lines.add(key)
                if _line_has_skip_reason(text_lines, lineno):
                    signed, _signer = is_justified(
                        plan_dir, relpath, lineno
                    )
                    if signed:
                        continue
                    violations.append(
                        f"{relpath}:{lineno}: "
                        f"SKIP-REASON-but-unsigned: pytest.skip"
                    )
                    continue
                violations.append(
                    f"{relpath}:{lineno}: pytest.skip: runtime-skip"
                )

    # ---- Textual scan: xit / xdescribe (JS-style port markers). ----
    for idx, line in enumerate(text_lines):
        lineno = idx + 1
        hit_token: str | None = None
        for tok in _TEXTUAL_SKIP_TOKENS:
            if tok in line:
                hit_token = tok.rstrip("( ")
                break
        if hit_token is None:
            continue
        key = (lineno, hit_token)
        if key in seen_lines:
            continue
        seen_lines.add(key)
        if _line_has_skip_reason(text_lines, lineno):
            signed, _signer = is_justified(plan_dir, relpath, lineno)
            if signed:
                continue
            violations.append(
                f"{relpath}:{lineno}: "
                f"SKIP-REASON-but-unsigned: {hit_token}"
            )
            continue
        violations.append(f"{relpath}:{lineno}: {hit_token}: textual-skip")

    return violations


def _iter_test_files(tests_root: Path) -> list[Path]:
    return sorted(p for p in tests_root.rglob("*.py") if p.is_file())


def main(project_dir: str = ".") -> int:
    """Forbid @pytest.mark.skip / @unittest.skip / xit / xdescribe in tests/ unless # SKIP-REASON: + reviewer signoff."""
    project_root = Path(project_dir).resolve()
    tests_root = project_root / "tests"
    plan_dir = project_root / ".peers"

    if not tests_root.is_dir():
        print("no-skipped-tests: clean (no tests/ to scan)")
        return 0

    all_violations: list[str] = []
    files = _iter_test_files(tests_root)
    for path in files:
        rel = path.relative_to(project_root).as_posix()
        all_violations.extend(_scan_python_file(path, rel, plan_dir))

    if all_violations:
        print(
            f"no-skipped-tests FAIL: {len(all_violations)} "
            f"violation(s) in {len(files)} file(s):"
        )
        for v in all_violations:
            print(f"  {v}")
        print(
            "  hint: each skip needs a `# SKIP-REASON: <text>` comment "
            "on the line above AND a reviewer-signed entry in "
            ".peers/justifications.log for the skip marker line"
        )
        return 1

    print(f"no-skipped-tests: clean ({len(files)} file(s) scanned)")
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
