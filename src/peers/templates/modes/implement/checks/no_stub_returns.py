#!/usr/bin/env python3
"""Soft cleanliness gate: warn on functions that only return a stub value.

Schicht-5 soft gate for implement-mode (Task 5.5.1). AST-scans ``src/``
for function / method bodies whose only behaviour is to return a stub
value -- ``return None``, ``return {}``, ``return []``, ``return ""``,
``return 0``, ``return False`` -- with no other statements (a leading
docstring is allowed).

These bodies typically appear when an implementer wires up a surface
but punts on the actual logic. The hard ``no-empty-bodies`` gate
(Task 5.3) catches ``pass`` / ``...`` / docstring-only bodies, but
not ``return None`` -- the body has a real statement, it just has no
behaviour. This soft gate fills that hole and emits findings for the
reviewer to triage.

Soft semantics
--------------
The check always exits 0. Findings are printed to stdout for the
reviewer; the loop does not block on them. The companion soft goal
in ``implement/goals.yaml`` asks the reviewer peer to run this script
and decide whether the listed stubs are intentional placeholders or
real omissions.

Exempt from the scan
--------------------
* Functions decorated with ``@abstractmethod`` (or ``@abc.abstractmethod``)
  -- abstract surfaces are *expected* to be stub-shaped.
* Members of a class whose bases textually match ``Protocol`` / ``ABC``
  -- same rationale as ``no_empty_bodies``.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path


_STUB_RETURN_LITERALS: tuple[object, ...] = (None, "", 0, False)
_STUB_RETURN_EMPTY_CONTAINERS = (ast.Dict, ast.List, ast.Set, ast.Tuple)

_ABSTRACT_BASE_PATTERNS = (
    "ABC", "abc.ABC", "ABCMeta", "abc.ABCMeta",
    "Protocol", "typing.Protocol",
)
_ABSTRACTMETHOD_DECORATORS = (
    "abstractmethod", "abc.abstractmethod",
    "abstractclassmethod", "abc.abstractclassmethod",
    "abstractstaticmethod", "abc.abstractstaticmethod",
    "abstractproperty", "abc.abstractproperty",
)


def _is_abstract_base(node: ast.ClassDef) -> bool:
    for base in node.bases:
        text = ast.unparse(base)
        for pat in _ABSTRACT_BASE_PATTERNS:
            if text == pat or text.endswith("." + pat):
                return True
    for kw in node.keywords:
        if kw.arg == "metaclass":
            text = ast.unparse(kw.value)
            for pat in _ABSTRACT_BASE_PATTERNS:
                if text == pat or text.endswith("." + pat):
                    return True
    return False


def _has_abstractmethod_decorator(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    for dec in node.decorator_list:
        text = ast.unparse(dec)
        if text.endswith(")"):
            text = text.split("(", 1)[0]
        for pat in _ABSTRACTMETHOD_DECORATORS:
            if text == pat or text.endswith("." + pat):
                return True
    return False


def _is_stub_return(stmt: ast.stmt) -> bool:
    """True if ``stmt`` is a ``return <stub-literal>`` statement."""
    if not isinstance(stmt, ast.Return):
        return False
    val = stmt.value
    if val is None:  # bare `return` == `return None`
        return True
    if isinstance(val, ast.Constant) and val.value in _STUB_RETURN_LITERALS:
        return True
    # `return {}`, `return []`, `return ()`, `return set()` shapes --
    # we only care about empty collection *literals*.
    if isinstance(val, _STUB_RETURN_EMPTY_CONTAINERS):
        if isinstance(val, ast.Dict):
            return not val.keys
        return not val.elts
    return False


def _is_docstring_expr(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def _body_is_stub_return_only(body: list[ast.stmt]) -> bool:
    if not body:
        return False
    rest = body[1:] if _is_docstring_expr(body[0]) else body
    return len(rest) == 1 and _is_stub_return(rest[0])


def _scan_tree(tree: ast.Module, relpath: str) -> list[str]:
    findings: list[str] = []
    exempt_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and _is_abstract_base(node):
            for stmt in node.body:
                if isinstance(
                    stmt,
                    (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                ):
                    exempt_ids.add(id(stmt))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if id(node) in exempt_ids:
            continue
        if _has_abstractmethod_decorator(node):
            continue
        if _body_is_stub_return_only(node.body):
            findings.append(f"{relpath}:{node.lineno}: {node.name}")
    return findings


def _scan_python_file(path: Path, relpath: str) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return []
    return _scan_tree(tree, relpath)


_NON_IMPL_DIRS = frozenset({
    "tests", "test", "testing", "docs", "doc", "examples", "example",
    "tools", "viewer", "schema", "vendor", "third_party", "node_modules",
    "build", "dist",
})


def _impl_roots(project_root: Path) -> list[Path]:
    """Implementation roots to scan. Conventional ``src/`` layout when present;
    otherwise every top-level *package* directory (one with ``__init__.py``)
    that is not a tests/tooling/vendor dir. This covers flat package layouts
    (e.g. ``scene3dx/`` / ``shell3d/``) that otherwise made the soft gate pass
    vacuously ("no src/ to scan") with zero coverage of the implementation.

    Limitation (advisory, soft gate): a flat *single-module* project (a top-level
    ``.py`` with no package dir) or a PEP-420 *namespace* package (no
    ``__init__.py``) is still not discovered and remains unscanned."""
    src = project_root / "src"
    if src.is_dir():
        return [src]
    roots: list[Path] = []
    for child in sorted(project_root.iterdir()):
        if (child.is_dir() and not child.name.startswith(".")
                and child.name not in _NON_IMPL_DIRS
                and (child / "__init__.py").is_file()):
            roots.append(child)
    return roots


def main(project_dir: str = ".") -> int:
    """Soft scan: warn on functions whose only body is a stub return."""
    project_root = Path(project_dir).resolve()
    roots = _impl_roots(project_root)
    if not roots:
        print("no-stub-returns: clean (no implementation package to scan)")
        return 0
    findings: list[str] = []
    files = sorted(p for r in roots for p in r.rglob("*.py") if p.is_file())
    for path in files:
        rel = path.relative_to(project_root).as_posix()
        findings.extend(_scan_python_file(path, rel))
    if findings:
        print(
            f"no-stub-returns WARN: {len(findings)} stub-only function(s) "
            f"in {len(files)} file(s):"
        )
        for f in findings:
            print(f"  {f}")
        print(
            "  hint: implement the body, or mark abstract "
            "(@abstractmethod / Protocol / ABC)"
        )
        return 0  # soft -- advisory only
    print(f"no-stub-returns: clean ({len(files)} file(s) scanned)")
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
