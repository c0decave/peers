#!/usr/bin/env python3
"""Soft gate: verify TEST_SKEPSIS.md contains concrete claims per test.

Schicht-3 soft gate for implement-mode (Task 8.1). Periodic gate (every
3rd tick) -- the reviewer peer is expected to write ``TEST_SKEPSIS.md``
at the project root with one entry per recently-added test, each making
a *concrete* claim that the test would actually catch a regression:

    - tests/unit/test_foo.py::test_happy - if I remove line 42 from
      src/foo.py the test catches it because <concrete reason>.

The gate parses every list entry (``- ...`` or ``* ...``) in
``TEST_SKEPSIS.md`` and flags entries that look like boilerplate:

* ``looks fine`` / ``looks good`` / ``passes`` / ``it works`` -- the
  reviewer skimmed without thinking.
* no ``src/...`` reference AND no ``line N`` reference -- the entry
  does not actually claim which line the test pins.
* substantive prose too short (< 10 words after the entry header).

Soft semantics
--------------
The check always exits 0. Findings are printed to stdout for the
reviewer; the loop does not block on them. The companion soft goal in
``implement/goals.yaml`` asks the reviewer peer to run this script and
decide whether each flagged entry needs to be rewritten.

Pass surface
------------
* File absent or empty -- treated as off-tick / new project.
* All entries have a concrete src-line reference AND > 10 words of
  failure-mode prose AND do NOT contain boilerplate phrases.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


_BOILERPLATE_PHRASES = (
    "looks fine",
    "looks good",
    "looks ok",
    "looks okay",
    "passes",
    "it works",
    "seems fine",
    "seems good",
    "lgtm",
)

# A list entry starts with `-` or `*` at any indent. We do not require
# numbered lists -- the prompt asks for bullets.
_LIST_ENTRY_RE = re.compile(r"^\s*[-*]\s+(?P<body>.+)$")

# Acceptable line-reference shapes:
#   src/foo.py
#   src/foo/bar.py:42
#   line 42
#   lines 42-50
_SRC_REF_RE = re.compile(r"\bsrc/[\w./_-]+\.\w+")
_LINE_REF_RE = re.compile(r"\bline[s]?\s+\d+")


def _collect_entries(text: str) -> list[tuple[int, str]]:
    """Return list of (1-based lineno, body) for each bullet entry.

    Continuation lines (indented non-bullet lines following a bullet)
    are joined onto the preceding entry so a multi-line entry counts
    as one body.
    """
    entries: list[tuple[int, str]] = []
    current_lineno: int | None = None
    current_parts: list[str] = []

    def _flush() -> None:
        nonlocal current_lineno, current_parts
        if current_lineno is not None and current_parts:
            entries.append((current_lineno, " ".join(current_parts).strip()))
        current_lineno = None
        current_parts = []

    for idx, raw in enumerate(text.splitlines(), start=1):
        m = _LIST_ENTRY_RE.match(raw)
        if m:
            _flush()
            current_lineno = idx
            current_parts = [m.group("body").strip()]
        elif raw.strip() and raw.startswith((" ", "\t")) and current_lineno:
            # continuation of previous bullet
            current_parts.append(raw.strip())
        else:
            _flush()
    _flush()
    return entries


def _entry_snippet(body: str, max_chars: int = 60) -> str:
    """First N chars of the entry body, for human-readable findings."""
    flat = " ".join(body.split())
    if len(flat) <= max_chars:
        return flat
    return flat[:max_chars].rstrip() + "..."


def _entry_findings(lineno: int, body: str) -> list[str]:
    findings: list[str] = []
    snippet = _entry_snippet(body)
    low = body.lower()
    for phrase in _BOILERPLATE_PHRASES:
        if phrase in low:
            findings.append(
                f"line {lineno} [{snippet}]: boilerplate phrase "
                f"{phrase!r} -- rewrite with concrete failure-mode claim"
            )
            return findings  # one finding per entry is enough
    has_src_ref = _SRC_REF_RE.search(body) is not None
    has_line_ref = _LINE_REF_RE.search(body) is not None
    if not (has_src_ref or has_line_ref):
        findings.append(
            f"line {lineno} [{snippet}]: no `src/...` or `line N` "
            "reference -- entry does not pin a concrete line"
        )
    word_count = len(body.split())
    if word_count < 10:
        findings.append(
            f"line {lineno} [{snippet}]: only {word_count} words -- "
            "substantive failure reasoning expected (>=10)"
        )
    return findings


def main(project_dir: str = ".") -> int:
    """Soft scan: warn on boilerplate or unspecific TEST_SKEPSIS.md entries."""
    project_root = Path(project_dir).resolve()
    skepsis_path = project_root / "TEST_SKEPSIS.md"
    if not skepsis_path.is_file():
        print("test-skeptic-review: clean (no TEST_SKEPSIS.md -- off-tick)")
        return 0
    text = skepsis_path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        print("test-skeptic-review: clean (TEST_SKEPSIS.md empty -- off-tick)")
        return 0
    entries = _collect_entries(text)
    if not entries:
        print(
            "test-skeptic-review: clean (TEST_SKEPSIS.md present but no "
            "bullet entries yet)"
        )
        return 0
    all_findings: list[str] = []
    for lineno, body in entries:
        all_findings.extend(_entry_findings(lineno, body))
    if all_findings:
        print(
            f"test-skeptic-review WARN: {len(all_findings)} issue(s) "
            f"in {len(entries)} entry/ies:"
        )
        for f in all_findings:
            print(f"  {f}")
        print(
            "  hint: each entry should name a src line and explain "
            "concretely which assertion would now fire"
        )
        return 0  # soft
    print(
        f"test-skeptic-review: clean ({len(entries)} entry/ies, "
        "all with concrete claims)"
    )
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
