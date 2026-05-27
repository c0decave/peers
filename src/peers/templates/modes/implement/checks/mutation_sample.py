#!/usr/bin/env python3
"""Opt-in soft gate: stub placeholder for future mutation-testing integration.

Schicht-6 opt-in gate for implement-mode (Task 8.4). When PLAN.md
declares ``mutation_testing: true`` in its Meta section, the project
operator is asking for a mutation-testing sweep (e.g. mutmut /
cosmic-ray) to surface tests that pass against mutated source but
should not.

Real mutmut / cosmic-ray integration is intentionally deferred to v2 --
those tools are heavy (10x-100x test-suite runtime) and project-specific
in their configuration. This gate is the structural placeholder: when
the operator opts in, the gate emits a clear "requested but not yet
implemented" notice rather than silently passing.

Opt-in mechanism
----------------
* PLAN.md meta key ``mutation_testing: true``.
* Otherwise the gate exits 0 with ``skipped (opt-in not enabled)``.

Soft semantics
--------------
Always exits 0. When opted in, prints a single-line WARN noting that
the real implementation is pending; reviewer acts on it by either
running mutmut/cosmic-ray themselves or filing a deferral note.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


_PLAN_NAME = "PLAN.md"

_META_KEY_RE = re.compile(
    r"^\s*mutation_testing\s*:\s*(?P<val>.+?)\s*$",
    re.IGNORECASE,
)

_TRUE_TOKENS = ("true", "yes", "on", "1")


def _read_mutation_flag(plan_path: Path) -> bool:
    """Return True iff PLAN.md Meta declares mutation_testing as truthy."""
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
            # Trim inline comment.
            if "#" in val:
                val = val.split("#", 1)[0].strip()
            return val.lower() in _TRUE_TOKENS
    return False


def main(project_dir: str = ".") -> int:
    """Soft scan: stub for future mutmut / cosmic-ray integration."""
    project_root = Path(project_dir).resolve()
    plan_path = project_root / _PLAN_NAME

    if not _read_mutation_flag(plan_path):
        print(
            "mutation-sample: skipped (opt-in not enabled -- set "
            "`mutation_testing: true` in PLAN.md Meta to activate)"
        )
        return 0

    print(
        "mutation-sample WARN: mutation testing requested but not yet "
        "implemented in this version of the gate"
    )
    print(
        "  hint: real mutmut / cosmic-ray integration is deferred to v2; "
        "for now run the tool of your choice manually and record findings "
        "in DELIVERY.md or CONCERNS.md"
    )
    return 0  # soft


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
