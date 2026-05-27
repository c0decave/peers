#!/usr/bin/env python3
"""Soft gate: compare ARCHITECTURE.actual.md against ARCHITECTURE.intended.md.

Schicht-6 soft gate for implement-mode (Task 8.2). Runs at convergence
and compares the *actual* architecture (regenerated from the live tree
at convergence) against the *intended* architecture (frozen at Phase 0
when the contracts were locked).

Heuristic
---------
Both files are split into their H2 sections (``## <heading>``). Two
divergence signals are computed:

* **Heading drift**: the symmetric difference between the two heading
  sets, normalised case-insensitively. >= 2 missing/added headings on
  either side is "structural divergence".
* **Body drift**: the unified-diff ratio (added+removed lines / total).
  >= 0.5 is "large body divergence".

Either signal triggers the gate -- UNLESS ``PLAN.md`` contains a
documented amendment. We accept an amendment in any of the following
shapes (case-insensitive, anywhere in PLAN.md):

* a heading containing ``architecture amendment``
* a heading containing ``architecture drift``
* a line starting with ``Architecture-Amendment:`` (footer-style)

Soft semantics
--------------
Always exits 0. Findings are printed to stdout; the reviewer reads
them and decides whether the drift needs a PLAN.md amendment or whether
the implementation should be steered back toward the intended shape.

Pass surface
------------
* Both files present, headings align (<= 1 sym-diff entry), body diff
  ratio < 0.5.
* Body / heading drift present BUT a documented amendment is found in
  PLAN.md.

Warn surface
------------
* ``ARCHITECTURE.intended.md`` missing (Phase 0 didn't run?).
* ``ARCHITECTURE.actual.md`` missing (convergence didn't regenerate?).
* Structural divergence without an amendment in PLAN.md.
"""
from __future__ import annotations

import difflib
import re
import sys
from pathlib import Path


_H2_RE = re.compile(r"^##\s+(?P<heading>.+?)\s*$")

_AMENDMENT_HEADING_PATTERNS = (
    "architecture amendment",
    "architecture drift",
    "architecture revision",
)
_AMENDMENT_FOOTER_RE = re.compile(
    r"^architecture[-_ ]amendment\s*:", re.IGNORECASE
)


def _extract_headings(text: str) -> list[str]:
    headings: list[str] = []
    for line in text.splitlines():
        m = _H2_RE.match(line)
        if m:
            headings.append(m.group("heading").strip().lower())
    return headings


def _diff_ratio(a: str, b: str) -> float:
    a_lines = a.splitlines()
    b_lines = b.splitlines()
    if not a_lines and not b_lines:
        return 0.0
    sm = difflib.SequenceMatcher(a=a_lines, b=b_lines)
    return 1.0 - sm.ratio()


def _has_amendment(plan_text: str) -> bool:
    for line in plan_text.splitlines():
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith("#"):
            # heading -- strip leading `#`s and whitespace
            heading_body = low.lstrip("#").strip()
            for pat in _AMENDMENT_HEADING_PATTERNS:
                if pat in heading_body:
                    return True
        if _AMENDMENT_FOOTER_RE.match(stripped):
            return True
    return False


def main(project_dir: str = ".") -> int:
    """Soft compare of actual vs intended architecture."""
    project_root = Path(project_dir).resolve()
    intended_path = project_root / "ARCHITECTURE.intended.md"
    actual_path = project_root / "ARCHITECTURE.actual.md"

    findings: list[str] = []
    intended_missing = not intended_path.is_file()
    actual_missing = not actual_path.is_file()
    if intended_missing:
        findings.append(
            "ARCHITECTURE.intended.md missing -- Phase 0 should have "
            "frozen it; the gate cannot compare against a baseline"
        )
    if actual_missing:
        findings.append(
            "ARCHITECTURE.actual.md missing -- convergence should "
            "regenerate it from the live tree"
        )

    if not intended_missing and not actual_missing:
        intended_text = intended_path.read_text(
            encoding="utf-8", errors="replace"
        )
        actual_text = actual_path.read_text(
            encoding="utf-8", errors="replace"
        )
        intended_headings = set(_extract_headings(intended_text))
        actual_headings = set(_extract_headings(actual_text))
        sym_diff = intended_headings.symmetric_difference(actual_headings)
        body_ratio = _diff_ratio(intended_text, actual_text)
        structural = len(sym_diff) >= 2
        large_body = body_ratio >= 0.5
        if structural or large_body:
            plan_path = project_root / "PLAN.md"
            amendment_ok = (
                plan_path.is_file()
                and _has_amendment(
                    plan_path.read_text(encoding="utf-8", errors="replace")
                )
            )
            if amendment_ok:
                print(
                    "architecture-coherent: clean (divergence present "
                    "but documented as amendment in PLAN.md; "
                    f"headings sym-diff={len(sym_diff)}, "
                    f"body diff ratio={body_ratio:.2f})"
                )
                return 0
            if structural:
                missing_in_actual = sorted(
                    intended_headings - actual_headings
                )
                missing_in_intended = sorted(
                    actual_headings - intended_headings
                )
                findings.append(
                    f"heading drift: {len(sym_diff)} divergence(s); "
                    f"missing in actual={missing_in_actual!r}, "
                    f"new in actual={missing_in_intended!r}"
                )
            if large_body:
                findings.append(
                    f"body drift: diff ratio {body_ratio:.2f} >= 0.50 "
                    "without an `Architecture-Amendment` in PLAN.md"
                )

    if findings:
        print(
            f"architecture-coherent WARN: {len(findings)} divergence "
            "signal(s):"
        )
        for f in findings:
            print(f"  {f}")
        print(
            "  hint: either steer ARCHITECTURE.actual back toward "
            "intended, or document the change in PLAN.md as an "
            "`## Architecture Amendment` section"
        )
        return 0  # soft
    print(
        "architecture-coherent: clean (headings align, body diff small)"
    )
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
