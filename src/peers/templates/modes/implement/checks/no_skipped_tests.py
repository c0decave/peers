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
* ``@pytest.mark.xfail`` (same)
* module-level ``pytestmark = pytest.mark.skip/xfail/...``
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
import hashlib
import re
import sys
from pathlib import Path

from peers_ctl.justifications import (
    JustificationError,
    is_justified,
    verify_log_chain,
)


_SKIP_REASON_TAG = "# SKIP-REASON:"

# Textual fallback markers (xit / xdescribe) -- the gate scans these
# line-by-line because they're free-form JS-style calls, not standard
# decorators we can easily AST-match. BUG-011 (eco-run): match the call as a
# WHOLE WORD (`\bxit\b\s*\(`), not a substring -- the old `"xit(" in line`
# false-flagged every `sys.exit(`, `os._exit(`, or `...xit(` identifier.
_TEXTUAL_SKIP_RE = re.compile(r"\b(xit|xdescribe)\b\s*\(")


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
    "pytest.mark.xfail",
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


# Baseline of skip signatures present at run-start. Skips whose signature
# is recorded here are "grandfathered" — inherited / pre-baseline skips that
# must not block a fresh implement-mode run (mirrors no_regression's
# passing-baseline.txt). NEW skips added after the snapshot are still flagged.
_BASELINE_NAME = "skip-baseline.txt"


def _skip_signature(
    relpath: str, kind: str, target: str | None, marker_line: str,
    context: str,
) -> str:
    """Line-number-INDEPENDENT identity for a skip marker.

    A baseline keyed on ``file:lineno`` would silently un-grandfather a
    pre-existing skip the moment any earlier line in the file shifts. We
    key instead on (file, kind, enclosing target, the marker's own source
    text, AND a hash of the guarded test's source) so the identity survives
    intra-run line drift but does NOT survive a peer repurposing a baselined
    skip's identity to hide DIFFERENT (newly failing) code under the same
    file+decorator+name (adversarial-review HIGH-2). ``context`` is the source
    of the enclosing test for AST markers, or "" for module/textual markers.
    """
    tgt = "" if target is None else target
    ctx = hashlib.sha256((context or "").encode("utf-8")).hexdigest()[:16]
    return f"{relpath}|{kind}|{tgt}|{marker_line.strip()}|{ctx}"


def _node_source(text: str, node: ast.AST) -> str:
    seg = ast.get_source_segment(text, node)
    return seg if seg is not None else ""


def _enclosing_def_source(
    defs: list[ast.AST], text: str, lineno: int
) -> str:
    """Source of the innermost function/class def whose span (decorators
    included) contains ``lineno`` — the test a ``pytest.skip()`` call guards."""
    best: ast.AST | None = None
    best_start = -1
    for node in defs:
        decs = getattr(node, "decorator_list", [])
        start = min([node.lineno] + [d.lineno for d in decs])
        end = getattr(node, "end_lineno", None) or node.lineno
        if start <= lineno <= end and start > best_start:
            best, best_start = node, start
    return _node_source(text, best) if best is not None else ""


def _pytestmark_skip_marks(value: ast.expr) -> list[tuple[int, str]]:
    """Return ``(lineno, kind)`` for every skip mark in a ``pytestmark``
    value (a bare mark or a list/tuple of them)."""
    is_skip, kind = _decorator_is_skip(value)
    if is_skip:
        return [(value.lineno, kind)]
    out: list[tuple[int, str]] = []
    if isinstance(value, (ast.List, ast.Tuple)):
        for item in value.elts:
            out.extend(_pytestmark_skip_marks(item))
    return out


def _iter_file_skips(
    text: str, text_lines: list[str]
) -> list[tuple[int, str, str | None, str]]:
    """Return ``(lineno, kind, target, context)`` for every skip marker.

    ``target`` is the enclosing function/class name for decorators,
    ``"runtime-skip"`` for ``pytest.skip()`` calls,
    ``"<module-level pytestmark>"`` for module marks, and ``None`` for
    textual xit/xdescribe markers (which carry no AST target). ``context`` is
    the source of the guarded test (for AST markers) or ``""`` (module/textual)
    — it binds the grandfather to the test's body. Deduped by ``(lineno,
    kind)``, AST markers taking precedence over textual ones on the same line.
    """
    skips: list[tuple[int, str, str | None, str]] = []
    seen: set[tuple[int, str]] = set()

    def _add(lineno: int, kind: str, target: str | None, context: str) -> None:
        key = (lineno, kind)
        if key in seen:
            return
        seen.add(key)
        skips.append((lineno, kind, target, context))

    # ---- AST-based scan: decorators + pytest.skip() + pytestmark. ----
    try:
        tree: ast.AST | None = ast.parse(text)
    except SyntaxError:
        tree = None

    if tree is not None:
        defs = [
            n for n in ast.walk(tree)
            if isinstance(
                n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            )
        ]
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                for dec in node.decorator_list:
                    is_skip, kind = _decorator_is_skip(dec)
                    if is_skip:
                        _add(dec.lineno, kind, node.name,
                             _node_source(text, node))
            if isinstance(node, ast.Call) and _is_pytest_skip_call(node):
                _add(node.lineno, "pytest.skip", "runtime-skip",
                     _enclosing_def_source(defs, text, node.lineno))
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign)
                    else [node.target]
                )
                if any(isinstance(t, ast.Name) and t.id == "pytestmark"
                       for t in targets):
                    for lineno, kind in _pytestmark_skip_marks(node.value):
                        _add(lineno, kind, "<module-level pytestmark>", "")

    # ---- Textual scan: xit / xdescribe (JS-style port markers). ----
    for idx, line in enumerate(text_lines):
        m = _TEXTUAL_SKIP_RE.search(line)
        if m:
            _add(idx + 1, m.group(1), None, "")

    return skips


def _format_violation(relpath: str, lineno: int, kind: str,
                      target: str | None) -> str:
    if target is None:
        return f"{relpath}:{lineno}: {kind}: textual-skip"
    return f"{relpath}:{lineno}: {kind}: target={target}"


def _scan_python_file(
    path: Path, relpath: str, plan_dir: Path, baseline: frozenset[str],
) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    text_lines = text.splitlines()

    violations: list[str] = []
    for lineno, kind, target, context in _iter_file_skips(text, text_lines):
        marker_line = (
            text_lines[lineno - 1] if 1 <= lineno <= len(text_lines) else ""
        )
        # Grandfathered: present in tests/ at run-start. Bypass all policy.
        sig = _skip_signature(relpath, kind, target, marker_line, context)
        if sig in baseline:
            continue
        if _line_has_skip_reason(text_lines, lineno):
            signed, _signer = is_justified(plan_dir, relpath, lineno)
            if signed:
                continue
            violations.append(
                f"{relpath}:{lineno}: SKIP-REASON-but-unsigned: {kind}"
            )
            continue
        violations.append(_format_violation(relpath, lineno, kind, target))

    return violations


def _iter_test_files(tests_root: Path) -> list[Path]:
    return sorted(p for p in tests_root.rglob("*.py") if p.is_file())


def _collect_signatures(project_root: Path, tests_root: Path) -> set[str]:
    """Every skip signature currently present under ``tests/`` — the set we
    write to the baseline at run-start."""
    sigs: set[str] = set()
    for path in _iter_test_files(tests_root):
        rel = path.relative_to(project_root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        text_lines = text.splitlines()
        for lineno, kind, target, context in _iter_file_skips(text, text_lines):
            marker_line = (
                text_lines[lineno - 1]
                if 1 <= lineno <= len(text_lines) else ""
            )
            sigs.add(
                _skip_signature(rel, kind, target, marker_line, context)
            )
    return sigs


def _load_baseline(plan_dir: Path) -> frozenset[str]:
    """Load grandfathered skip signatures. A missing/unreadable baseline =>
    empty set => today's behaviour (nothing grandfathered).

    NB (FU-1, docs/plans/2026-06-07-justification-baseline-integrity-followups):
    this baseline has no forgery resistance — same trust level as
    no_regression's passing-baseline.txt. A peer that can write .peers/ could
    grandfather a new skip by editing it. Subsystem-wide fix (anchor the
    run-start hash outside peer-writable .peers/) is tracked separately."""
    try:
        raw = (plan_dir / _BASELINE_NAME).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return frozenset()
    return frozenset(ln for ln in raw.splitlines() if ln.strip())


def main(project_dir: str = ".", snapshot: bool = False) -> int:
    """Forbid @pytest.mark.skip / @unittest.skip / xit / xdescribe in tests/ unless # SKIP-REASON: + reviewer signoff (skips present at run-start are grandfathered via --snapshot)."""
    project_root = Path(project_dir).resolve()
    tests_root = project_root / "tests"
    plan_dir = project_root / ".peers"

    if not tests_root.is_dir():
        if snapshot:
            print("no-skipped-tests: snapshot skipped (no tests/ to scan)")
            return 0
        print("no-skipped-tests: clean (no tests/ to scan)")
        return 0

    # --snapshot: record the skips present right now as the grandfather set
    # and exit. Run ONCE at run-start, before peers touch any code. No chain
    # verification here — we are only recording the pre-existing state.
    if snapshot:
        sigs = _collect_signatures(project_root, tests_root)
        plan_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / _BASELINE_NAME).write_text(
            "".join(f"{s}\n" for s in sorted(sigs)), encoding="utf-8",
        )
        print(
            "no-skipped-tests: snapshot wrote "
            f"{len(sigs)} baseline skip signature(s) to "
            f"{plan_dir / _BASELINE_NAME}"
        )
        return 0

    # verify chain before consulting the log. is_justified() is
    # a pure lookup and accepts forged entries with a bogus chain prefix.
    try:
        verify_log_chain(plan_dir)
    except JustificationError as e:
        print(f"no-skipped-tests FAIL: justifications log chain broken: {e}")
        return 1

    baseline = _load_baseline(plan_dir)
    all_violations: list[str] = []
    files = _iter_test_files(tests_root)
    for path in files:
        rel = path.relative_to(project_root).as_posix()
        all_violations.extend(_scan_python_file(path, rel, plan_dir, baseline))

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

    grandfathered = f" ({len(baseline)} grandfathered)" if baseline else ""
    print(
        f"no-skipped-tests: clean ({len(files)} file(s) scanned)"
        f"{grandfathered}"
    )
    return 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    do_snapshot = "--snapshot" in argv
    positional = [a for a in argv if not a.startswith("-")]
    arg = positional[0] if positional else "."
    sys.exit(main(arg, snapshot=do_snapshot))
