#!/usr/bin/env python3
"""Opt-in soft gate: scan PLAN.md + DELIVERY.md for weasel phrases.

Schicht-6 opt-in gate for implement-mode (Task 8.4). Honest engineering
writing names what is true; weasel phrases ("should work", "appears to",
"probably", "I think") name what the writer wants to be true but did
not verify. Such phrases in convergence artefacts (PLAN.md checkoff
prose, DELIVERY.md justifications) are a useful smell: the writer
either knows and is hedging, or does not know.

The gate scans ``PLAN.md`` and ``DELIVERY.md`` for a closed vocabulary
of forbidden substrings and reports each hit with ``file:line``.

Forbidden vocabulary (case-insensitive substring match):

    "should work", "appears to", "probably", "i think",
    "seems to", "we believe", "i believe", "looks like it",
    "might work", "hopefully"

Operational semantics
---------------------
This gate is registered as ``type: soft`` in ``goals.yaml``. The
GoalEngine routes soft goals through a per-peer JSON review and
**ignores the check's exit code entirely** -- so this script always
exits 0, treating every finding as a warning. A hard-fail variant
would require dual-registration in ``goals.yaml`` as ``type: hard``
(out of scope for v1).

Pass surface
------------
* Neither file present -- nothing to scan.
* No forbidden substring matches in either file.

Warn surface
------------
* Any forbidden substring match -- one finding line per occurrence.
"""
from __future__ import annotations

import sys
from pathlib import Path


_TARGETS: tuple[str, ...] = ("PLAN.md", "DELIVERY.md")

# Closed vocabulary of forbidden substrings. Match is case-insensitive
# substring; multi-word phrases preserve internal whitespace.
_WEASEL_PHRASES: tuple[str, ...] = (
    "should work",
    "appears to",
    "probably",
    "i think",
    "seems to",
    "we believe",
    "i believe",
    "looks like it",
    "might work",
    "hopefully",
)


def _scan_file(path: Path) -> list[tuple[str, int, str, str]]:
    """Return [(filename, lineno, phrase, snippet)] for each weasel hit."""
    hits: list[tuple[str, int, str, str]] = []
    if not path.is_file():
        return hits
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return hits
    for lineno, raw in enumerate(text.splitlines(), start=1):
        low = raw.lower()
        for phrase in _WEASEL_PHRASES:
            if phrase in low:
                snippet = raw.strip()
                if len(snippet) > 80:
                    snippet = snippet[:80].rstrip() + "..."
                hits.append((path.name, lineno, phrase, snippet))
    return hits


def main(project_dir: str = ".") -> int:
    """Soft scan: flag weasel phrases in PLAN.md + DELIVERY.md.

    Always exits 0 -- this gate is soft by registration; see the module
    docstring for the rationale.
    """
    project_root = Path(project_dir).resolve()

    all_hits: list[tuple[str, int, str, str]] = []
    for name in _TARGETS:
        all_hits.extend(_scan_file(project_root / name))

    if not all_hits:
        print(
            "weasel-scan: clean (no forbidden phrases in "
            f"{'/'.join(_TARGETS)})"
        )
        return 0

    print(
        f"weasel-scan WARN: {len(all_hits)} weasel-phrase hit(s):"
    )
    for fname, lineno, phrase, snippet in all_hits:
        print(f"  {fname}:{lineno}: {phrase!r} -- {snippet}")
    print(
        "  hint: replace hedging with what was actually verified, or "
        "name the unknown explicitly (`status: open -- needs check`)"
    )
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
