#!/usr/bin/env python3
"""Hard goal: required `##`-level sections present + non-empty.

SPEC.md must have:        ## Threat Model, ## Invariants, ## API
ARCHITECTURE.md must have: ## Components, ## Data Flow
DESIGN.md must have:      ## Decisions, ## Tradeoffs

Each section must contain at least MIN_SECTION_BYTES bytes of content
between its `## Name` heading and the next `##` or EOF — otherwise a
peer can pass the check by inserting empty headings.

Fail-CLOSED.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REQUIRED: dict[str, list[str]] = {
    "SPEC.md": ["Threat Model", "Invariants", "API"],
    "ARCHITECTURE.md": ["Components", "Data Flow"],
    "DESIGN.md": ["Decisions", "Tradeoffs"],
}
MIN_SECTION_BYTES = 50


def _extract_sections(text: str) -> dict[str, str]:
    """Returns {section_name: body_text} for every `##` heading.

    Only second-level (##) headings count; `#` and `###` are ignored
    for simplicity (peers can always nest deeper inside a `##`).
    """
    sections: dict[str, str] = {}
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", text, re.MULTILINE))
    for i, m in enumerate(matches):
        name = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        sections[name] = body
    return sections


def main(repo: str = ".") -> int:
    root = Path(repo)
    problems: list[str] = []
    for fname, required_sections in REQUIRED.items():
        p = root / fname
        if not p.is_file() or p.is_symlink():
            problems.append(
                f"  {fname}: missing or symlink "
                f"(run description_files_present first)",
            )
            continue
        try:
            text = p.read_text(errors="ignore")
        except OSError as e:
            problems.append(f"  {fname}: unreadable: {e}")
            continue
        present = _extract_sections(text)
        for sec in required_sections:
            if sec not in present:
                problems.append(
                    f"  {fname}: missing `## {sec}` section",
                )
                continue
            body = present[sec]
            if len(body) < MIN_SECTION_BYTES:
                problems.append(
                    f"  {fname}: `## {sec}` body too short "
                    f"({len(body)} < {MIN_SECTION_BYTES} chars)",
                )
    if problems:
        print("description_sections_present FAIL:")
        for p in problems:
            print(p)
        return 1
    print(
        "description_sections_present: clean "
        "(all required sections present + non-empty)",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
