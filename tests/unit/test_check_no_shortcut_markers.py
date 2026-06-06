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


def test_skips_peer_template_check_implementations(tmp_path, capsys):
    """The peers repo's own policy templates name the markers they reject."""
    path = (
        tmp_path / "src" / "peers" / "templates" / "modes"
        / "implement" / "checks" / "no_shortcut_markers.py"
    )
    path.parent.mkdir(parents=True)
    path.write_text('MARKERS = ("TODO", "FIXME", "XXX", "HACK")\n')

    rc = no_shortcut_markers.main(str(tmp_path))

    assert rc == 0


def test_forged_justification_with_broken_chain_fails_closed_BUG_173(
    tmp_path, capsys,
):
    """A forged log entry whose hash is not chain-valid must fail the gate.

    The annotated line is *syntactically* covered by a log entry, but
    the chain prefix does not match sha256(prev + payload)[:16]. Without
    chain verification at gate entry, ``is_justified`` returns True and
    the violation is silently waived. The fix runs ``verify_log_chain``
    first and fails closed on tamper.
    """
    _setup(
        tmp_path,
        {"a.py": "def f():\n    pass  # TODO  # JUSTIFIED: forged\n"},
    )
    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir()
    # Hand-write a log entry with a deliberately wrong chain prefix.
    # `is_justified` would happily accept this; the chain check must not.
    (plan_dir / "justifications.log").write_text(
        "0000000000000000 src/a.py:2 attacker forged reason\n",
    )
    rc = no_shortcut_markers.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "chain" in out.lower() or "tamper" in out.lower()
