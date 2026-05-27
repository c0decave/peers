"""Test no-empty-bodies check (Task 5.3)."""
from __future__ import annotations

from pathlib import Path

from peers.templates.modes.implement.checks import no_empty_bodies


def _setup(tmp_path: Path, src_files: dict[str, str]) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    for name, body in src_files.items():
        (src / name).write_text(body)
    return tmp_path


def test_clean_function_passes(tmp_path, capsys):
    _setup(tmp_path, {"a.py": "def f():\n    return 1\n"})
    rc = no_empty_bodies.main(str(tmp_path))
    assert rc == 0


def test_pass_body_fails(tmp_path, capsys):
    _setup(tmp_path, {"a.py": "def f():\n    pass\n"})
    rc = no_empty_bodies.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "f" in out


def test_ellipsis_body_fails(tmp_path, capsys):
    _setup(tmp_path, {"a.py": "def f():\n    ...\n"})
    rc = no_empty_bodies.main(str(tmp_path))
    assert rc == 1


def test_abstractmethod_pass_allowed(tmp_path, capsys):
    _setup(tmp_path, {"a.py": """from abc import ABC, abstractmethod
class Foo(ABC):
    @abstractmethod
    def bar(self):
        pass
"""})
    rc = no_empty_bodies.main(str(tmp_path))
    assert rc == 0


def test_protocol_ellipsis_allowed(tmp_path, capsys):
    _setup(tmp_path, {"a.py": """from typing import Protocol
class Foo(Protocol):
    def bar(self) -> int:
        ...
"""})
    rc = no_empty_bodies.main(str(tmp_path))
    assert rc == 0


def test_empty_class_fails(tmp_path, capsys):
    _setup(tmp_path, {"a.py": "class Foo:\n    pass\n"})
    rc = no_empty_bodies.main(str(tmp_path))
    assert rc == 1


def test_docstring_only_body_fails(tmp_path, capsys):
    """A docstring + nothing else is still effectively empty."""
    _setup(tmp_path, {"a.py": 'def f():\n    """just a docstring"""\n'})
    rc = no_empty_bodies.main(str(tmp_path))
    assert rc == 1
