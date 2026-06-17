"""STEP-2 — the generic research TOPIC.md intake.

A generic, no-security-frame ``topic_present`` check. A research run needs a
written brief before it can DECOMPOSE, but research is a generic KNOWLEDGE mode
— it must accept a non-security topic ("cloning plants in Alaska"). So
:func:`require_topic` requires a non-vacuous ``## Scope`` and ``## Questions``
but does NOT require a ``## Frameworks`` section.

Fail-CLOSED and symlink-refusing: a missing / unreadable / symlinked
``TOPIC.md`` or any vacuous required section yields ``(False, [problems])``,
never a silent pass. The same 2 MiB cap and per-section char floor as the
security copy apply so a heading with an empty body does not slip through.
"""
from __future__ import annotations

import re
from pathlib import Path

from peers.safe_io import read_bytes_no_symlink

DOC_NAME = "TOPIC.md"
MIN_SECTION_CHARS = 60
_DOC_MAX_BYTES = 2 * 1024 * 1024  # 2 MiB cap; the brief should be small

# Each required section is matched by a group of accepted heading spellings
# (case-insensitive). The first spelling is the canonical one. NOTE: the
# security copy's ``Frameworks`` requirement is deliberately absent here.
REQUIRED_SECTIONS = (
    ("Scope", ("scope", "boundaries", "in scope")),
    ("Questions", ("questions", "question")),
)


def _split_sections(text: str) -> dict[str, str]:
    """Map each ``## <Heading>`` to its body text (until the next ``## ``)."""
    sections: dict[str, str] = {}
    matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", text))
    for i, m in enumerate(matches):
        name = m.group(1).strip().lower()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[name] = text[start:end].strip()
    return sections


def require_topic(repo: Path | str) -> tuple[bool, list[str]]:
    """Return ``(ok, problems)`` for the research brief at ``repo/TOPIC.md``.

    ``ok`` is True only when ``TOPIC.md`` is a real (non-symlink) file readable
    under the 2 MiB cap and carries non-vacuous ``## Scope`` and ``## Questions``
    sections. ``problems`` is an empty list on success, else one human-readable
    string per defect (fail-CLOSED: any error path returns ``(False, [...])``).
    """
    doc = Path(repo) / DOC_NAME
    if not doc.is_file() or doc.is_symlink():
        return False, [
            f"{DOC_NAME} missing or symlink at {doc} — research requires a "
            "real TOPIC.md with `## Scope` and `## Questions` sections",
        ]
    try:
        raw = read_bytes_no_symlink(doc, max_bytes=_DOC_MAX_BYTES)
    except OSError as e:
        return False, [f"cannot read {doc}: {e}"]

    text = raw.decode("utf-8", errors="ignore")
    sections = _split_sections(text)

    problems: list[str] = []
    for canonical, spellings in REQUIRED_SECTIONS:
        body = None
        for heading, content in sections.items():
            if any(heading == s or heading.startswith(s) for s in spellings):
                body = content
                break
        if body is None:
            problems.append(f"missing `## {canonical}` section")
        elif len(body) < MIN_SECTION_CHARS:
            problems.append(
                f"`## {canonical}` body {len(body)} chars "
                f"(< {MIN_SECTION_CHARS}) — looks vacuous",
            )

    return (not problems), problems
