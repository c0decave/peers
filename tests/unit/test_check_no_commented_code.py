"""Test no-commented-code soft cleanliness gate (Task 5.5.2).

Heuristic: a block of >3 consecutive `# ` lines that look like
commented-out source (containing `=`, `(`, `def `, `import`, etc.) is
flagged. Prose comments and license headers are ignored.
"""
from __future__ import annotations

from pathlib import Path

from peers.templates.modes.implement.checks import no_commented_code


def _setup(tmp_path: Path, src_files: dict[str, str]) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    for name, body in src_files.items():
        (src / name).write_text(body)
    return tmp_path


def test_prose_comments_pass(tmp_path, capsys):
    """A block of plain English comments is not flagged."""
    body = (
        "# This module computes the daily rollup.\n"
        "# It reads from the source table and writes to the warehouse.\n"
        "# The schedule is controlled by the orchestrator upstream.\n"
        "# Owners: data-platform team.\n"
        "def f():\n"
        "    return 1\n"
    )
    _setup(tmp_path, {"a.py": body})
    rc = no_commented_code.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out


def test_commented_out_code_warns(tmp_path, capsys):
    """A block of >3 commented lines that look like code is flagged."""
    body = (
        "def f():\n"
        "    # x = compute(y)\n"
        "    # if x > 0:\n"
        "    #     return x * 2\n"
        "    # else:\n"
        "    #     return 0\n"
        "    return 1\n"
    )
    _setup(tmp_path, {"a.py": body})
    rc = no_commented_code.main(str(tmp_path))
    assert rc == 0  # soft
    out = capsys.readouterr().out
    assert "WARN" in out or "warn" in out.lower()
    assert "a.py" in out


def test_short_block_of_code_comments_passes(tmp_path, capsys):
    """Only blocks >3 lines are flagged; 2 or 3 lines slide."""
    body = (
        "def f():\n"
        "    # x = 1\n"
        "    # y = 2\n"
        "    return 3\n"
    )
    _setup(tmp_path, {"a.py": body})
    rc = no_commented_code.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out


def test_docstrings_ignored(tmp_path, capsys):
    """Triple-quoted docstrings are not comment blocks."""
    body = (
        'def f():\n'
        '    """\n'
        '    x = 1\n'
        '    y = 2\n'
        '    z = 3\n'
        '    w = 4\n'
        '    """\n'
        '    return 1\n'
    )
    _setup(tmp_path, {"a.py": body})
    rc = no_commented_code.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out


def test_license_header_ignored(tmp_path, capsys):
    """A long block of `#` license-style comments at the very top is not
    flagged even though it's long -- license headers are prose, not code.
    """
    body = (
        "# Copyright 2026 Example Inc.\n"
        "# Licensed under the Apache License, Version 2.0 (the License);\n"
        "# you may not use this file except in compliance with the License.\n"
        "# You may obtain a copy of the License at\n"
        "# http://www.apache.org/licenses/LICENSE-2.0\n"
        "# Unless required by applicable law or agreed to in writing,\n"
        "# software distributed under the License is distributed on an\n"
        "# AS IS BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND.\n"
        "def f():\n"
        "    return 1\n"
    )
    _setup(tmp_path, {"a.py": body})
    rc = no_commented_code.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out
