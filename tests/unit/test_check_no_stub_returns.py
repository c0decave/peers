"""Test no-stub-returns soft cleanliness gate (Task 5.5.1).

Soft gate -- emits findings to stdout but never blocks the loop. The
exit code is always 0; the reviewer reads stdout and decides whether
the listed functions look like real implementations or just stubs.
"""
from __future__ import annotations

from pathlib import Path

from peers.templates.modes.implement.checks import no_stub_returns


def _setup(tmp_path: Path, src_files: dict[str, str]) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    for name, body in src_files.items():
        (src / name).write_text(body)
    return tmp_path


def test_clean_function_passes(tmp_path, capsys):
    """A function that does real work is not flagged."""
    _setup(tmp_path, {"a.py": "def f(x):\n    return x + 1\n"})
    rc = no_stub_returns.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out


def test_return_none_warns(tmp_path, capsys):
    """A function whose only body line is `return None` is a stub."""
    _setup(tmp_path, {"a.py": "def f():\n    return None\n"})
    rc = no_stub_returns.main(str(tmp_path))
    assert rc == 0  # soft -- exit 0 even with findings
    out = capsys.readouterr().out
    assert "WARN" in out or "warn" in out.lower()
    assert "f" in out


def test_return_empty_dict_warns(tmp_path, capsys):
    """A function whose only body line is `return {}` is a stub."""
    _setup(tmp_path, {"a.py": "def f():\n    return {}\n"})
    rc = no_stub_returns.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "f" in out


def test_return_empty_list_or_string_warns(tmp_path, capsys):
    """Empty list / empty string returns are also stubs."""
    _setup(
        tmp_path,
        {
            "a.py": "def f():\n    return []\n",
            "b.py": 'def g():\n    return ""\n',
        },
    )
    rc = no_stub_returns.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "f" in out and "g" in out


def test_docstring_plus_return_none_still_warns(tmp_path, capsys):
    """A docstring + lone `return None` still counts as a stub."""
    _setup(
        tmp_path,
        {"a.py": 'def f():\n    """does nothing"""\n    return None\n'},
    )
    rc = no_stub_returns.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "f" in out
