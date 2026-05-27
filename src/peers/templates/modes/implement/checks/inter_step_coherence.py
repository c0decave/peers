#!/usr/bin/env python3
"""Soft gate: verify STITCH.md tracks inter-step coherence reviews.

Schicht-6 soft gate for implement-mode (Task 8.3). Every N=3 completed
``[x]`` steps trigger a "stitch" check -- the reviewer peer writes an
H2 entry in ``STITCH.md`` at the project root explaining how the most
recent batch of steps fits together: are there duplications? dangling
connections? does the seam between the steps still hold?

Heuristic
---------
The gate parses ``PLAN.md`` to count checked (``state == "done"``)
steps. It then parses ``STITCH.md`` (if present) by H2 sections
(``## Stitch <N> -- ...``) and counts the substantive prose in each
section's body.

Pass surface
------------
* ``STITCH.md`` missing AND <= N=3 checked steps -- nothing to stitch
  yet.
* ``STITCH.md`` present, every H2 stitch section has >= 20 words of
  body prose.

Warn surface
------------
* >N=3 checked steps but no ``STITCH.md`` -- at least one stitch
  review should have been written by now.
* ``STITCH.md`` present but one or more sections have < 20 words.

Soft semantics
--------------
Always exits 0. Findings are printed to stdout; the reviewer reads
them and decides whether the next tick should include a stitch entry.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


_STITCH_INTERVAL = 3

_H2_RE = re.compile(r"^##\s+(?P<heading>.+?)\s*$")
# Plan step checkbox: `- [x] STEP-N: ...` or `- [X]`. Other markers
# (``[ ]`` / ``[PARTIAL]`` / ``[BLOCKED]`` / ``[BLOCKED-ACK]``) are not
# counted as "checked" for stitch purposes -- only fully-done steps.
_PLAN_DONE_RE = re.compile(r"^\s*-\s*\[[xX]\]\s+")


def _count_checked_steps(plan_path: Path) -> int:
    if not plan_path.is_file():
        return 0
    try:
        text = plan_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    count = 0
    for line in text.splitlines():
        if _PLAN_DONE_RE.match(line):
            count += 1
    return count


def _parse_h2_sections(text: str) -> list[tuple[str, str]]:
    """Return [(heading, body)] for each H2 section."""
    sections: list[tuple[str, str]] = []
    current_heading: str | None = None
    current_body: list[str] = []
    for line in text.splitlines():
        m = _H2_RE.match(line)
        if m:
            if current_heading is not None:
                sections.append(
                    (current_heading, "\n".join(current_body).strip())
                )
            current_heading = m.group("heading").strip()
            current_body = []
        else:
            if current_heading is not None:
                current_body.append(line)
    if current_heading is not None:
        sections.append(
            (current_heading, "\n".join(current_body).strip())
        )
    return sections


def _word_count(s: str) -> int:
    return len(s.split())


def main(project_dir: str = ".") -> int:
    """Soft scan: verify STITCH.md keeps pace with checked steps."""
    project_root = Path(project_dir).resolve()
    plan_path = project_root / "PLAN.md"
    stitch_path = project_root / "STITCH.md"

    if not plan_path.is_file():
        print("inter-step-coherence: clean (no PLAN.md -- nothing to stitch)")
        return 0

    checked = _count_checked_steps(plan_path)
    findings: list[str] = []

    if not stitch_path.is_file():
        if checked > _STITCH_INTERVAL:
            findings.append(
                f"{checked} checked step(s) but no STITCH.md -- a "
                f"stitch review every {_STITCH_INTERVAL} steps is "
                "expected"
            )
        else:
            print(
                f"inter-step-coherence: clean ({checked} checked step(s), "
                f"< {_STITCH_INTERVAL + 1} -- no stitch needed yet)"
            )
            return 0
    else:
        try:
            text = stitch_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        sections = _parse_h2_sections(text)
        if not sections and checked > _STITCH_INTERVAL:
            findings.append(
                "STITCH.md present but contains no `## Stitch <N>` H2 "
                "sections"
            )
        for heading, body in sections:
            wc = _word_count(body)
            if wc < 20:
                findings.append(
                    f"stitch section {heading!r}: only {wc} words "
                    "(>=20 expected -- explain duplications, dangling "
                    "connections, seams)"
                )

    if findings:
        print(
            f"inter-step-coherence WARN: {len(findings)} issue(s):"
        )
        for f in findings:
            print(f"  {f}")
        print(
            "  hint: append `## Stitch <N> -- STEP-A..STEP-B` to "
            "STITCH.md with substantive prose on how the recent batch "
            "fits together"
        )
        return 0  # soft

    print(
        f"inter-step-coherence: clean ({checked} checked step(s), "
        "STITCH.md keeps pace)"
    )
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
