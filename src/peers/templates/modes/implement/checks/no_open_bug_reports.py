#!/usr/bin/env python3
"""Exit 1 if the bug-hunt ledger has any OPEN blocking bug-report.

Convergence anchor for the bug-hunt protocol. Each peer files findings as
commits carrying a ``Bug-Report: BUG-NNN`` trailer (with a JSON severity)
and closes them with ``Bug-Resolves: BUG-NNN``. ``peers.bug_hunt`` already
reconstructs the full ledger from git history and knows which bugs are still
open at severity >= med (``BugSummary.open_blocking_count``).

The gap this gate closes: nothing consumed that ledger at convergence. The
per-tick counter (`count_new_blocking_or_flag_bug_reports`) only feeds the
consecutive-clean-ticks streak, and the post-convergence skeptic's own
freshly-filed bug could slip straight through to a "complete" terminal exit.
In the calc greenfield run the final skeptic filed BUG-006 (a med-severity
OverflowError leak) as the very last commit and the run terminated anyway,
shipping a known, documented, unresolved bug.

This gate makes the ledger authoritative: convergence is impossible while any
blocking bug-report is open. A bug must be either resolved
(``Bug-Resolves:`` with status ``fixed``/``deferred``) or downgraded below
``med`` before the loop may declare success.

Pass (exit 0) when ``open_blocking_count == 0``.
Fail (exit 1), listing each open blocking bug, otherwise.
"""
from __future__ import annotations

import sys
from pathlib import Path

from peers import bug_hunt


def main(project_dir: str = ".") -> int:
    """Fail if the bug-hunt ledger has any open blocking (>= med) bug."""
    repo = Path(project_dir).resolve()
    try:
        summary = bug_hunt.summarize(repo)
    except Exception as e:
        # fail closed. A corrupt or unreadable ledger means
        # the gate cannot verify cleanliness; returning 0 here would
        # let any open blocking bug slip through convergence.
        print(
            f"no-open-bug-reports FAIL: ledger unavailable ({e!r}); "
            "cannot verify convergence",
            file=sys.stderr,
        )
        return 1

    if summary.open_blocking_count == 0:
        print("no-open-bug-reports: clean (no open blocking bug-reports)")
        return 0

    open_blocking = [
        rep
        for sev in bug_hunt.BLOCKING_SEVERITIES
        for rep in summary.open_by_severity.get(sev, [])
    ]
    print(
        f"no-open-bug-reports FAIL: {len(open_blocking)} open blocking "
        f"bug-report(s) at convergence:"
    )
    for rep in open_blocking:
        loc = getattr(rep, "location", "") or getattr(rep, "file", "") or "?"
        fix_by = getattr(rep, "fix_by", "") or "?"
        print(f"  {rep.id} [{rep.severity}] (fix_by={fix_by}) {loc}")
    print(
        "  hint: resolve each with a `Bug-Resolves: BUG-NNN` commit, or "
        "downgrade below `med` if it is genuinely not blocking"
    )
    return 1


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
