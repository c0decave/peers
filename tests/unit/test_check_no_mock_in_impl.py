"""Test no-mock-in-impl soft cleanliness gate (Task 5.5.5).

Scan ``src/*.py`` for imports of `unittest.mock`, `pytest_mock`, or
`mock`. Warn if any are present. Tests/ paths are not scanned.
"""
from __future__ import annotations

from pathlib import Path

from peers.templates.modes.implement.checks import no_mock_in_impl


def _setup(
    tmp_path: Path,
    src_files: dict[str, str] | None = None,
    tests_files: dict[str, str] | None = None,
) -> Path:
    if src_files:
        src = tmp_path / "src"
        src.mkdir(exist_ok=True)
        for name, body in src_files.items():
            p = src / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
    if tests_files:
        tests = tmp_path / "tests"
        tests.mkdir(exist_ok=True)
        for name, body in tests_files.items():
            p = tests / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
    return tmp_path


def test_no_mocks_passes(tmp_path, capsys):
    _setup(tmp_path, {"a.py": "import os\n\ndef f():\n    return 1\n"})
    rc = no_mock_in_impl.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out


def test_unittest_mock_import_warns(tmp_path, capsys):
    _setup(tmp_path, {"a.py": "from unittest.mock import MagicMock\n"})
    rc = no_mock_in_impl.main(str(tmp_path))
    assert rc == 0  # soft
    out = capsys.readouterr().out
    assert "WARN" in out or "warn" in out.lower()
    assert "a.py" in out


def test_pytest_mock_import_warns(tmp_path, capsys):
    _setup(tmp_path, {"a.py": "import pytest_mock\n"})
    rc = no_mock_in_impl.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "WARN" in out or "warn" in out.lower()
    assert "pytest_mock" in out


def test_plain_mock_import_warns(tmp_path, capsys):
    _setup(tmp_path, {"a.py": "import mock\n"})
    rc = no_mock_in_impl.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "WARN" in out or "warn" in out.lower()


def test_mocks_in_tests_are_ignored(tmp_path, capsys):
    """Imports under tests/ never count -- mocks belong there."""
    _setup(
        tmp_path,
        src_files={"a.py": "def f():\n    return 1\n"},
        tests_files={"test_a.py": "from unittest.mock import MagicMock\n"},
    )
    rc = no_mock_in_impl.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out
