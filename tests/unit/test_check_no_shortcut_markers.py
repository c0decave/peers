"""Test no-shortcut-markers check (Task 5.1)."""
from __future__ import annotations

from pathlib import Path

from peers.templates.modes.implement.checks import no_shortcut_markers


def _setup(tmp_path: Path, src_files: dict[str, str]) -> Path:
    """Create src/ with given files, return project_dir."""
    src = tmp_path / "src"
    src.mkdir()
    for name, body in src_files.items():
        f = src / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    return tmp_path


def test_clean_src_passes(tmp_path, capsys):
    _setup(tmp_path, {"a.py": "def f():\n    return 1\n"})
    rc = no_shortcut_markers.main(str(tmp_path))
    assert rc == 0


def test_todo_marker_fails(tmp_path, capsys):
    _setup(tmp_path, {"a.py": "def f():\n    # TODO: fix this\n    return 1\n"})
    rc = no_shortcut_markers.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "TODO" in out
    assert "a.py" in out


def test_fixme_marker_fails(tmp_path, capsys):
    _setup(tmp_path, {"a.py": "def f():\n    return 1  # FIXME later\n"})
    rc = no_shortcut_markers.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "FIXME" in out


def test_xxx_hack_placeholder_stub_fail(tmp_path, capsys):
    _setup(tmp_path, {
        "a.py": "# XXX: temp\n",
        "b.py": "# HACK around bug\n",
        "c.py": "# PLACEHOLDER for X\n",
        "d.py": "x = 'STUB'\n",
    })
    rc = no_shortcut_markers.main(str(tmp_path))
    assert rc == 1


def test_not_implemented_in_concrete_class_fails(tmp_path, capsys):
    _setup(tmp_path, {"a.py": """class Foo:
    def bar(self):
        raise NotImplementedError("subclass me")
"""})
    rc = no_shortcut_markers.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "NotImplementedError" in out


def test_not_implemented_in_abstract_class_allowed(tmp_path, capsys):
    _setup(tmp_path, {"a.py": """from abc import ABC, abstractmethod
class Foo(ABC):
    @abstractmethod
    def bar(self):
        raise NotImplementedError
"""})
    rc = no_shortcut_markers.main(str(tmp_path))
    assert rc == 0


def test_not_implemented_in_protocol_allowed(tmp_path, capsys):
    _setup(tmp_path, {"a.py": """from typing import Protocol
class Foo(Protocol):
    def bar(self):
        raise NotImplementedError
"""})
    rc = no_shortcut_markers.main(str(tmp_path))
    assert rc == 0


def test_justified_marker_passes_with_signed_entry(tmp_path, capsys):
    from peers_ctl.justifications import append_justification
    _setup(tmp_path, {"a.py": "def f():\n    pass  # TODO  # JUSTIFIED: waits on issue 42\n"})
    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir()
    # Line 2 has TODO -- justify it
    append_justification(plan_dir, "src/a.py", 2, "waits on issue 42", "codex@p.local")
    rc = no_shortcut_markers.main(str(tmp_path))
    assert rc == 0


def test_justified_marker_without_signoff_fails(tmp_path, capsys):
    _setup(tmp_path, {"a.py": "def f():\n    pass  # TODO  # JUSTIFIED: handwave\n"})
    # No justifications.log written
    rc = no_shortcut_markers.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "JUSTIFIED" in out or "unsigned" in out.lower() or "TODO" in out


def test_skips_tests_directory(tmp_path, capsys):
    """tests/ paths are not scanned -- only src/ matters for this gate."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def x(): pass\n")  # clean
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("# TODO: write tests\n")  # has TODO but in tests/
    rc = no_shortcut_markers.main(str(tmp_path))
    assert rc == 0  # tests/ TODO is fine
