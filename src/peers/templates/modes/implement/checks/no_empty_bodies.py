#!/usr/bin/env python3
"""Exit 1 if ``src/`` contains function/method/class bodies that are empty.

Schicht-5 cleanliness gate for implement-mode (Task 5.3). A function
or class whose body is *just* ``pass`` / ``...`` / a lone docstring is
a structural shortcut: the implementer declared a name but never wrote
the behaviour. The acceptance script and ``no-shortcut-markers`` gate
both miss this failure mode -- the body parses, no forbidden vocabulary
appears, and the symbol exists -- so we close the hole with an AST
scan.

What counts as "empty"
----------------------
A body is empty when, after stripping a single leading docstring
expression, the only remaining statement is one of:

* ``pass``
* ``...`` (an :class:`ast.Constant` wrapping :data:`Ellipsis`)

A body that *only* contains a docstring (with no following statement)
is also empty -- documentation alone is not implementation.

Anything else (a return, an assignment, a raise, another function
call, even a chained ``if False: pass``) is treated as a real body
and not flagged.

Exemptions
----------
The whole point of ``pass`` / ``...`` in Python is to mark abstract /
protocol surfaces. We exempt:

* Functions decorated with ``@abstractmethod`` (or
  ``@abc.abstractmethod``).
* Any function / class defined inside a class body whose bases
  textually match ``Protocol`` / ``typing.Protocol`` / ``ABC`` /
  ``abc.ABC`` / ``ABCMeta`` / ``abc.ABCMeta`` (or use
  ``metaclass=ABCMeta`` via the keyword form).

The base-class match is purely textual on the unparsed AST node, so
``Foo(Protocol)`` and ``Foo(typing.Protocol)`` both work. Aliases
(``from typing import Protocol as P``; ``class X(P):``) are not
recognised -- this matches the pattern in :mod:`no_shortcut_markers`.

Exit codes
----------
* ``0`` -- ``src/`` is clean, or no ``src/`` directory exists.
* ``1`` -- at least one empty body; stdout lists violations as
  ``<relpath>:<lineno>: <symbol-name>: <kind>``.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path


_ABSTRACT_BASE_PATTERNS = (
    "ABC",
    "abc.ABC",
    "ABCMeta",
    "abc.ABCMeta",
    "Protocol",
    "typing.Protocol",
)

_ABSTRACTMETHOD_DECORATORS = (
    "abstractmethod",
    "abc.abstractmethod",
    "abstractclassmethod",
    "abc.abstractclassmethod",
    "abstractstaticmethod",
    "abc.abstractstaticmethod",
    "abstractproperty",
    "abc.abstractproperty",
)


def _is_abstract_base(node: ast.ClassDef) -> bool:
    """True if ``node`` derives from a known abstract / protocol base."""
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
    """True if ``node`` carries ``@abstractmethod`` (any form)."""
    for dec in node.decorator_list:
        text = ast.unparse(dec)
        # Strip call form ``@abstractmethod()`` to just the name.
        if text.endswith(")"):
            # Cheap: just look at the substring before the first '('.
            text = text.split("(", 1)[0]
        for pat in _ABSTRACTMETHOD_DECORATORS:
            if text == pat or text.endswith("." + pat):
                return True
    return False


def _is_docstring_expr(stmt: ast.stmt) -> bool:
    """True if ``stmt`` is a bare string literal (docstring slot)."""
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def _is_ellipsis_expr(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and stmt.value.value is Ellipsis
    )


def _body_is_empty(body: list[ast.stmt]) -> str | None:
    """Return a short kind label if ``body`` is empty, else ``None``.

    Kinds: ``"pass"``, ``"ellipsis"``, ``"docstring-only"``.
    """
    if not body:
        # Syntactically impossible in valid Python, but be defensive.
        return "empty"
    # Allow one leading docstring, then check what's left.
    rest = body
    leading_docstring = False
    if _is_docstring_expr(body[0]):
        leading_docstring = True
        rest = body[1:]
    if not rest:
        return "docstring-only" if leading_docstring else "empty"
    if len(rest) == 1:
        only = rest[0]
        if isinstance(only, ast.Pass):
            return "pass"
        if _is_ellipsis_expr(only):
            return "ellipsis"
    return None


def _scan_tree(
    tree: ast.Module, relpath: str
) -> list[str]:
    """Walk ``tree`` and return violation lines for empty bodies."""
    violations: list[str] = []

    # Track which class definitions are abstract / protocol bases so
    # we can exempt their direct member functions and (nested) classes.
    # Map id(node) -> True for any FunctionDef/AsyncFunctionDef/ClassDef
    # that is an immediate child of an exempt class body.
    exempt_ids: set[int] = set()

    def _mark_exempt_members(cls: ast.ClassDef) -> None:
        for stmt in cls.body:
            if isinstance(
                stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                exempt_ids.add(id(stmt))

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and _is_abstract_base(node):
            _mark_exempt_members(node)

    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue

        # Skip exempted members (direct children of Protocol/ABC classes).
        if id(node) in exempt_ids:
            continue

        # Skip @abstractmethod-decorated functions.
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) and _has_abstractmethod_decorator(node):
            continue

        kind = _body_is_empty(node.body)
        if kind is None:
            continue

        sym_kind = (
            "class"
            if isinstance(node, ast.ClassDef)
            else (
                "async-function"
                if isinstance(node, ast.AsyncFunctionDef)
                else "function"
            )
        )
        violations.append(
            f"{relpath}:{node.lineno}: {node.name}: "
            f"{sym_kind} body is {kind}"
        )

    return violations


def _scan_python_file(path: Path, relpath: str) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        # An unparseable file cannot have its bodies inspected; we
        # leave it to the project's lint gate to flag it.
        return []
    return _scan_tree(tree, relpath)


def _iter_src_files(src_root: Path) -> list[Path]:
    return sorted(p for p in src_root.rglob("*.py") if p.is_file())


def main(project_dir: str = ".") -> int:
    """AST scan src/ for empty function/class bodies. Exempt Protocol/ABC abstracts."""
    project_root = Path(project_dir).resolve()
    src_root = project_root / "src"

    if not src_root.is_dir():
        print("no-empty-bodies: clean (no src/ to scan)")
        return 0

    all_violations: list[str] = []
    files = _iter_src_files(src_root)
    for path in files:
        rel = path.relative_to(project_root).as_posix()
        all_violations.extend(_scan_python_file(path, rel))

    if all_violations:
        print(
            f"no-empty-bodies FAIL: {len(all_violations)} "
            f"violation(s) in {len(files)} file(s):"
        )
        for v in all_violations:
            print(f"  {v}")
        print(
            "  hint: implement the body, or mark the surface as abstract "
            "(@abstractmethod, Protocol base, ABC base)"
        )
        return 1

    print(f"no-empty-bodies: clean ({len(files)} file(s) scanned)")
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
