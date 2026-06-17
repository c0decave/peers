"""Regression: the soft cleanliness scanners must discover the implementation
package in a FLAT layout (no ``src/``), e.g. ``scene3dx/`` or ``shell3d/``.

Previously all three hardcoded ``project_root / "src"`` and printed
``clean (no src/ to scan)`` for any project not using the src-layout, providing
ZERO coverage of the actual implementation (surfaced dogfooding peers implement
on scene3d-exploit, whose package is ``scene3dx/``). They now scan ``src/`` when
present, else every top-level package dir (a dir with ``__init__.py``) that is
not a tests/tooling/vendor dir.
"""
from __future__ import annotations

from pathlib import Path

from peers.templates.modes.implement.checks import (
    no_commented_code,
    no_mock_in_impl,
    no_stub_returns,
)


def _flat_pkg(tmp_path: Path, filename: str, body: str) -> Path:
    """A flat-layout project: package scene3dx/ (no src/) + a tests/ dir that
    must never be scanned."""
    pkg = tmp_path / "scene3dx"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / filename).write_text(body)
    (tmp_path / "tests").mkdir()
    return tmp_path


def test_no_mock_in_impl_scans_flat_package(tmp_path, capsys):
    _flat_pkg(tmp_path, "m.py", "import unittest.mock\n")
    rc = no_mock_in_impl.main(str(tmp_path))
    out = capsys.readouterr().out
    assert rc == 0
    assert "no src/ to scan" not in out
    assert "unittest.mock" in out and "scene3dx/m.py" in out


def test_no_stub_returns_scans_flat_package(tmp_path, capsys):
    _flat_pkg(tmp_path, "s.py", "def stub():\n    return None\n")
    rc = no_stub_returns.main(str(tmp_path))
    out = capsys.readouterr().out
    assert rc == 0
    assert "no src/ to scan" not in out
    assert "scene3dx/s.py" in out


def test_no_commented_code_scans_flat_package(tmp_path, capsys):
    body = "def f():\n    return 1\n# a = compute()\n# b = a + 1\n# c = b + 2\n# d = c + 3\n"
    _flat_pkg(tmp_path, "c.py", body)
    rc = no_commented_code.main(str(tmp_path))
    out = capsys.readouterr().out
    assert rc == 0
    assert "no src/ to scan" not in out
    assert "scene3dx/c.py" in out


def test_src_layout_still_scanned(tmp_path, capsys):
    """Back-compat: a conventional src/ layout is still scanned."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "m.py").write_text("import unittest.mock\n")
    rc = no_mock_in_impl.main(str(tmp_path))
    out = capsys.readouterr().out
    assert rc == 0
    assert "unittest.mock" in out and "src/m.py" in out


def test_tests_dir_not_scanned_in_flat_layout(tmp_path, capsys):
    """A clean flat package + a mock import under tests/ -> not flagged
    (tests are allowed to mock; only the impl package is scanned)."""
    _flat_pkg(tmp_path, "clean.py", "def f():\n    return 1 + 1\n")
    (tmp_path / "tests" / "t.py").write_text("import unittest.mock\n")
    rc = no_mock_in_impl.main(str(tmp_path))
    out = capsys.readouterr().out
    assert rc == 0
    assert "unittest.mock" not in out


def test_denylisted_toplevel_package_is_excluded(tmp_path, capsys):
    """A denylisted top-level package (e.g. vendor/) is excluded even with an
    __init__.py — only the real impl package is scanned. Pins the denylist so
    removing an entry would fail a test."""
    _flat_pkg(tmp_path, "clean.py", "def f():\n    return 1 + 1\n")
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "__init__.py").write_text("")
    (vendor / "dep.py").write_text("import unittest.mock\n")
    rc = no_mock_in_impl.main(str(tmp_path))
    out = capsys.readouterr().out
    assert rc == 0
    assert "unittest.mock" not in out  # vendor/ must not be scanned


def test_no_package_at_all_is_clean(tmp_path, capsys):
    """A project with neither src/ nor any package dir -> clean (nothing to scan),
    still exit 0 (soft)."""
    (tmp_path / "README.md").write_text("nothing here")
    rc = no_mock_in_impl.main(str(tmp_path))
    assert rc == 0
