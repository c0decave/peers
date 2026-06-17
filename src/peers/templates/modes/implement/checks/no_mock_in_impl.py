#!/usr/bin/env python3
"""Soft cleanliness gate: warn on mock-library imports under src/.

Schicht-5 soft gate for implement-mode (Task 5.5.5). AST-scans ``src/``
for imports of ``unittest.mock``, ``pytest_mock``, or the standalone
``mock`` package. Tests/ paths are never scanned -- mocks belong there.

Production code that imports a mocking library is a smell: usually
either left-over test scaffolding or a faux dependency that should be
behind a real interface.

Soft semantics: always exit 0. Findings are printed for the reviewer
to triage via the companion ``soft`` goal.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path


_MOCK_MODULE_PREFIXES = (
    "unittest.mock",
    "pytest_mock",
    "mock",
)


def _matches_mock(modname: str) -> bool:
    """True if a fully-qualified module name is (or starts with) a mock module.

    ``unittest.mock`` and ``unittest.mock.MagicMock`` both match
    ``unittest.mock``. Bare ``mock`` matches ``mock`` but
    ``mockingbird`` (which merely shares a prefix) does not.
    """
    for pat in _MOCK_MODULE_PREFIXES:
        if modname == pat or modname.startswith(pat + "."):
            return True
    return False


def _scan_imports(tree: ast.Module, relpath: str) -> list[str]:
    findings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _matches_mock(alias.name):
                    findings.append(
                        f"{relpath}:{node.lineno}: import {alias.name}"
                    )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            # Relative imports (level > 0) can't reach unittest.mock etc.
            if node.level > 0 or not mod:
                continue
            if _matches_mock(mod):
                names = ", ".join(a.name for a in node.names)
                findings.append(
                    f"{relpath}:{node.lineno}: from {mod} import {names}"
                )
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
    return _scan_imports(tree, relpath)


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
    """Soft scan: warn on impl-package imports of unittest.mock / pytest_mock / mock."""
    project_root = Path(project_dir).resolve()
    roots = _impl_roots(project_root)
    if not roots:
        print("no-mock-in-impl: clean (no implementation package to scan)")
        return 0
    findings: list[str] = []
    files = sorted(p for r in roots for p in r.rglob("*.py") if p.is_file())
    for path in files:
        rel = path.relative_to(project_root).as_posix()
        findings.extend(_scan_python_file(path, rel))
    if findings:
        print(
            f"no-mock-in-impl WARN: {len(findings)} mock-library "
            f"import(s) in the implementation:"
        )
        for f in findings:
            print(f"  {f}")
        print(
            "  hint: move mock-using code to tests/, or replace the "
            "mock with a real interface"
        )
        return 0  # soft
    print(f"no-mock-in-impl: clean ({len(files)} file(s) scanned)")
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
