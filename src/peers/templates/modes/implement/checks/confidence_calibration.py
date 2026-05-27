#!/usr/bin/env python3
"""Opt-in soft gate: when enabled, every [x] step must declare confidence N/5.

Schicht-6 opt-in gate for implement-mode (Task 8.4). Self-reported
confidence is a useful pre-mortem signal: a peer who checks a step off
at ``confidence: 2/5`` is asking the reviewer to look harder than one
who self-reports 5/5. The opt-in keeps the noise off projects that
don't want this discipline; the gate makes the missing/low values
visible when projects do want it.

Required per-step structure when opt-in is on::

    - [x] [STEP-N] add foo (abc1234)
      - confidence: 4/5

Where ``N in {1..5}``. Confidence below 4 produces a soft warning
(``low-confidence checkoff, consider re-review``); a missing
declaration produces a soft warning (``no confidence: N/5 sub-attribute``).

Opt-in mechanism
----------------
* PLAN.md Meta key ``confidence_calibration: true``.
* Otherwise the gate exits 0 with ``skipped (opt-in not enabled)``.

Soft semantics
--------------
Always exits 0. Findings are advisory; the reviewer reads them and
either accepts the implementer's hedging or asks for a deeper second
look at the low-confidence steps.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


_PLAN_NAME = "PLAN.md"

_META_KEY_RE = re.compile(
    r"^\s*confidence_calibration\s*:\s*(?P<val>.+?)\s*$",
    re.IGNORECASE,
)

_TRUE_TOKENS = ("true", "yes", "on", "1")

# Step opener: `- [x] [STEP-N] ...` (case-insensitive on the X).
_STEP_RE = re.compile(
    r"^\s*-\s*\[[xX]\]\s*\[(?P<id>STEP-\d+)\]\s*(?P<text>.+?)\s*$"
)
# Sub-attribute opener for an existing step: `  - <key>: <value>`.
_SUB_RE = re.compile(
    r"^\s+-\s*(?P<key>[a-z_][a-z0-9_]*)\s*:\s*(?P<val>.+?)\s*$"
)
# Confidence value: N/5 where N in {1..5}.
_CONF_VAL_RE = re.compile(r"^\s*(?P<n>[1-5])\s*/\s*5\s*$")

_LOW_THRESHOLD = 4  # confidence < 4 -> soft warning


def _read_opt_in(plan_path: Path) -> bool:
    if not plan_path.is_file():
        return False
    try:
        text = plan_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    in_meta = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("##"):
            in_meta = stripped.lower() == "## meta"
            continue
        if not in_meta:
            continue
        m = _META_KEY_RE.match(line)
        if m:
            val = m.group("val").strip()
            if "#" in val:
                val = val.split("#", 1)[0].strip()
            return val.lower() in _TRUE_TOKENS
    return False


def _collect_checkoff_confidence(
    plan_path: Path,
) -> list[tuple[str, int | None]]:
    """Return [(step_id, confidence_or_None)] for each [x] step.

    Confidence is None when no `- confidence: N/5` sub-attribute is
    present under the step entry (or it is malformed).
    """
    out: list[tuple[str, int | None]] = []
    if not plan_path.is_file():
        return out
    try:
        text = plan_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out

    current_step: str | None = None
    current_confidence: int | None = None

    def _flush() -> None:
        nonlocal current_step, current_confidence
        if current_step is not None:
            out.append((current_step, current_confidence))
        current_step = None
        current_confidence = None

    for line in text.splitlines():
        m_step = _STEP_RE.match(line)
        if m_step:
            _flush()
            current_step = m_step.group("id")
            current_confidence = None
            continue
        # Sub-attribute under a step (indented bullet).
        m_sub = _SUB_RE.match(line)
        if m_sub and current_step is not None:
            if m_sub.group("key") == "confidence":
                m_val = _CONF_VAL_RE.match(m_sub.group("val"))
                if m_val:
                    current_confidence = int(m_val.group("n"))
            continue
        # A non-indented non-step line ends the current step's scope.
        if line.strip() and not line.startswith((" ", "\t")):
            _flush()
    _flush()
    return out


def main(project_dir: str = ".") -> int:
    """Soft scan: verify [x] steps carry `confidence: N/5` when opted-in."""
    project_root = Path(project_dir).resolve()
    plan_path = project_root / _PLAN_NAME

    if not _read_opt_in(plan_path):
        print(
            "confidence-calibration: skipped (opt-in not enabled -- "
            "set `confidence_calibration: true` in PLAN.md Meta to "
            "activate)"
        )
        return 0

    entries = _collect_checkoff_confidence(plan_path)
    if not entries:
        print(
            "confidence-calibration: clean (opt-in enabled but no "
            "[x] steps yet)"
        )
        return 0

    findings: list[str] = []
    for step_id, conf in entries:
        if conf is None:
            findings.append(
                f"{step_id}: no `- confidence: N/5` sub-attribute "
                "(opt-in requires one per checked step)"
            )
        elif conf < _LOW_THRESHOLD:
            findings.append(
                f"{step_id}: low-confidence checkoff "
                f"({conf}/5 < {_LOW_THRESHOLD}/5) -- consider re-review"
            )

    if findings:
        print(
            f"confidence-calibration WARN: {len(findings)} issue(s) "
            f"across {len(entries)} checked step(s):"
        )
        for f in findings:
            print(f"  {f}")
        print(
            "  hint: add `  - confidence: N/5` (N in {1..5}) under "
            "each [x] step; <4 invites the reviewer to look harder"
        )
        return 0  # soft

    print(
        f"confidence-calibration: clean ({len(entries)} checked step(s), "
        "all with confidence >= 4/5)"
    )
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
