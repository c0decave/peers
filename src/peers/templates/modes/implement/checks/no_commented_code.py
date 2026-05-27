#!/usr/bin/env python3
"""Soft cleanliness gate: warn on blocks of commented-out code.

Schicht-5 soft gate for implement-mode (Task 5.5.2). Scans ``src/``
for runs of >3 consecutive ``# ``-prefixed lines that look like
commented-out source (each line containing at least one of ``=``,
``(``, ``def``, ``import``, ``class``, ``return``, ``if``, ``for``,
``while``, ``import``, etc.). Pure prose blocks and the file's leading
license header are not flagged.

Heuristic
---------
For every ``.py`` file under ``src/``:

1. Collect runs of consecutive lines whose first non-whitespace token
   is ``#``.
2. If the file's *first* such run starts on a comment line that
   begins with ``# Copyright`` / ``# Licensed`` / ``# SPDX`` /
   ``# !`` (shebang continuations), drop it -- it's the license /
   header block.
3. For every remaining run of length > 3, count how many lines look
   "code-shaped" -- the comment body (after the leading ``#``) must
   either contain a Python operator (``=``, ``(``, ``[``, ``{``) or
   start with a Python keyword (``def``, ``class``, ``import``,
   ``from``, ``return``, ``if``, ``elif``, ``else``, ``for``,
   ``while``, ``try``, ``except``, ``with``, ``raise``).
4. If at least half of the run's lines are code-shaped, flag the
   whole block (file + starting line).

Soft semantics: always exit 0. Findings are printed for the reviewer
to triage via the companion ``soft`` goal in ``implement/goals.yaml``.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


_KEYWORD_PREFIXES = (
    "def ", "class ", "import ", "from ", "return ", "return(",
    "if ", "elif ", "else:", "for ", "while ", "try:", "except",
    "with ", "raise ", "yield ", "assert ", "global ", "nonlocal ",
    "async ", "await ",
)

_LICENSE_HEADER_PREFIXES = (
    "# copyright",
    "# licensed",
    "# spdx",
    "# !",
    "#!",
)

# Pattern: line whose first non-whitespace token is `#`. Captures the
# leading whitespace and the comment body for later inspection.
_COMMENT_LINE_RE = re.compile(r"^(?P<indent>\s*)#\s?(?P<body>.*)$")


def _is_comment_line(line: str) -> bool:
    if not line.strip():
        return False
    return _COMMENT_LINE_RE.match(line) is not None


def _looks_like_code(body: str) -> bool:
    stripped = body.strip()
    if not stripped:
        return False
    # Heuristic indicators that the comment body is source, not prose.
    if any(op in stripped for op in ("=", "(", "[", "{")):
        # Cheap false-positive filter: prose with parentheses
        # (e.g. "this is a note (see X)") is OK only if it does NOT
        # also contain an `=` or a leading keyword. We accept this
        # as a soft heuristic -- false positives are acceptable for
        # a soft gate; the reviewer makes the final call.
        return True
    low = stripped.lower()
    for kw in _KEYWORD_PREFIXES:
        if low.startswith(kw):
            return True
    return False


def _is_license_header_line(line: str) -> bool:
    low = line.lstrip().lower()
    for pat in _LICENSE_HEADER_PREFIXES:
        if low.startswith(pat):
            return True
    return False


def _scan_file(path: Path, relpath: str) -> list[str]:
    """Return findings of the form ``<relpath>:<lineno>: N comment lines``."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    lines = text.splitlines()

    findings: list[str] = []
    run_start: int | None = None  # 1-based
    run_lines: list[str] = []
    first_run = True

    def _maybe_flag() -> None:
        nonlocal run_start, run_lines, first_run
        if run_start is None or not run_lines:
            return
        # Drop the file-leading license header verbatim -- never flag it.
        is_header = first_run and any(
            _is_license_header_line(line) for line in run_lines[:3]
        )
        first_run = False
        if not is_header and len(run_lines) > 3:
            code_shaped = sum(
                1 for line in run_lines
                if _looks_like_code(
                    _COMMENT_LINE_RE.match(line).group("body")
                )
            )
            if code_shaped * 2 >= len(run_lines):
                findings.append(
                    f"{relpath}:{run_start}: {len(run_lines)} consecutive "
                    "comment lines look like commented-out code "
                    f"({code_shaped} code-shaped)"
                )
        run_start = None
        run_lines = []

    for idx, line in enumerate(lines, start=1):
        if _is_comment_line(line):
            if run_start is None:
                run_start = idx
            run_lines.append(line)
        else:
            _maybe_flag()
    _maybe_flag()
    return findings


def main(project_dir: str = ".") -> int:
    """Soft scan: warn on blocks of >3 commented-out-code lines."""
    project_root = Path(project_dir).resolve()
    src_root = project_root / "src"
    if not src_root.is_dir():
        print("no-commented-code: clean (no src/ to scan)")
        return 0
    findings: list[str] = []
    files = sorted(p for p in src_root.rglob("*.py") if p.is_file())
    for path in files:
        rel = path.relative_to(project_root).as_posix()
        findings.extend(_scan_file(path, rel))
    if findings:
        print(
            f"no-commented-code WARN: {len(findings)} comment block(s) "
            f"look like commented-out code:"
        )
        for f in findings:
            print(f"  {f}")
        print("  hint: delete dead code -- git history preserves it")
        return 0  # soft
    print(f"no-commented-code: clean ({len(files)} file(s) scanned)")
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
