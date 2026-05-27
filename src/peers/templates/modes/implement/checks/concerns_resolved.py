#!/usr/bin/env python3
"""Exit 1 if `CONCERNS.md` has unresolved entries -- or if no concerns
have been filed at all (pessimism-quota gate).

Schicht-6 honesty gate for implement-mode (Task 6.3). `CONCERNS.md` is
the structured pessimism log peers maintain across the run. Each entry
is one ``## Concern N -- <summary>`` H2 block carrying a ``status:``
line. This check enforces two complementary properties:

1. **Resolution discipline.** Every concern entry must carry a status
   value drawn from the closed vocabulary

       * ``open``                                       -- unresolved
       * ``addressed (commit: <sha>)``                  -- resolved
       * ``[USER-ACK] (reason: <prose>)``               -- waived

   Any ``open`` (or otherwise non-matching) status fails the gate. A
   concern with no ``status:`` line at all also fails. The
   ``(commit: ...)`` / ``(reason: ...)`` parenthesised remainder is
   required syntactically -- ``addressed`` without a commit ref or
   ``[USER-ACK]`` without a reason are rejected, because the whole
   point of those statuses is to anchor the resolution to evidence.

2. **Pessimism quota.** An empty ``CONCERNS.md`` (or no file at all)
   is itself a failure: across a multi-tick implement-mode run, zero
   filed concerns is overwhelmingly evidence of rubber-stamping, not
   of a flawless implementation. The cure is to file something -- even
   a ``[USER-ACK]``'d concern counts. This gate's purpose is the
   convergence anchor; running it in non-convergence contexts is a
   misuse of the gate, not a reason to weaken the rule.

Also emits a *warning* (still exit 0 if statuses are otherwise clean)
when every concern was raised at the same tick: a healthy run drips
concerns in across multiple ticks, not all at once at the end as
box-ticking.

Inputs
------
* ``project_dir/CONCERNS.md`` -- absent ⇔ empty for this gate's
  purposes.

Exit codes
----------
* ``0`` -- log is non-empty and every concern entry is resolved
  (addressed-with-commit or user-ack'd-with-reason).
* ``1`` -- log empty, or any concern unresolved / malformed /
  status-less.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_CONCERNS_NAME = "CONCERNS.md"

# H2 header introducing one concern entry, e.g.
#   ## Concern 1 -- token refresh race
# Both ASCII `-` and em-dash `—` are accepted as the summary separator.
_CONCERN_HEADER_RE = re.compile(r"^##\s+(Concern\s+\S+)(?:\s+[-—]\s+(.+))?\s*$")

# `- status: <value>` line inside a concern block. Leading dash optional,
# casing of the key insensitive, value captured verbatim through EOL.
_STATUS_LINE_RE = re.compile(r"^\s*-?\s*status\s*:\s*(.+?)\s*$", re.IGNORECASE)

# Status value patterns. `addressed` requires a non-empty `commit: ...`
# parenthesised remainder; `[USER-ACK]` requires a non-empty
# `reason: ...` remainder. The remainders are intentionally permissive
# about whitespace and content so peers aren't fighting the parser.
_ADDRESSED_RE = re.compile(
    r"^addressed\s*\(\s*commit\s*:\s*\S+.*\)\s*$", re.IGNORECASE
)
_USER_ACK_RE = re.compile(
    r"^\[USER-ACK\]\s*\(\s*reason\s*:\s*.+\)\s*$", re.IGNORECASE
)
_OPEN_RE = re.compile(r"^open\s*$", re.IGNORECASE)


def _parse_concerns(text: str) -> list[dict]:
    """Split CONCERNS.md into a list of concern dicts.

    Each entry: ``{"header": str, "summary": str | None,
    "raised_tick": str | None, "status_raw": str | None,
    "lines": list[str]}``. Order is preserved (concerns appear in the
    same order they were filed).
    """
    concerns: list[dict] = []
    current: dict | None = None

    for raw_line in text.splitlines():
        header_match = _CONCERN_HEADER_RE.match(raw_line)
        if header_match:
            if current is not None:
                concerns.append(current)
            current = {
                "header": header_match.group(1),
                "summary": header_match.group(2),
                "raised_tick": None,
                "status_raw": None,
                "lines": [],
            }
            continue

        if current is None:
            # Prose before the first concern (or just the title) -- ignore.
            continue

        current["lines"].append(raw_line)

        status_match = _STATUS_LINE_RE.match(raw_line)
        if status_match and current["status_raw"] is None:
            current["status_raw"] = status_match.group(1).strip()

        # Capture `raised-tick:` for the same-tick warning. Tolerant of
        # any non-empty value -- we never numerically compare ticks.
        m = re.match(r"^\s*-?\s*raised-tick\s*:\s*(\S+)\s*$", raw_line, re.IGNORECASE)
        if m and current["raised_tick"] is None:
            current["raised_tick"] = m.group(1)

    if current is not None:
        concerns.append(current)

    return concerns


def _classify_status(status_raw: str | None) -> str:
    """Return one of ``"open"``, ``"addressed"``, ``"user-ack"``,
    ``"missing"``, ``"invalid"``."""
    if status_raw is None:
        return "missing"
    if _OPEN_RE.match(status_raw):
        return "open"
    if _ADDRESSED_RE.match(status_raw):
        return "addressed"
    if _USER_ACK_RE.match(status_raw):
        return "user-ack"
    return "invalid"


def main(project_dir: str = ".") -> int:
    """Verify CONCERNS.md state.

    Pass when ``CONCERNS.md`` is non-empty and every concern entry is
    resolved (addressed-with-commit or user-ack'd-with-reason). See
    module docstring for the full rule set.
    """
    project_root = Path(project_dir).resolve()
    concerns_path = project_root / _CONCERNS_NAME

    # Treat missing file as empty -- the pessimism gate cares about
    # *content*, not file presence.
    if not concerns_path.is_file():
        text = ""
    else:
        text = concerns_path.read_text(encoding="utf-8")

    concerns = _parse_concerns(text)

    if not concerns:
        print(
            f"concerns-resolved FAIL: {_CONCERNS_NAME} is empty -- "
            "pessimism quota not met (no concerns filed across the "
            "run = rubber-stamping suspected)."
        )
        print(
            "  hint: file at least one concern entry, even if its "
            "status is `[USER-ACK] (reason: ...)`."
        )
        return 1

    failures: list[str] = []
    for c in concerns:
        kind = _classify_status(c["status_raw"])
        label = c["header"] + (f" -- {c['summary']}" if c["summary"] else "")
        if kind == "open":
            failures.append(f"{label}: status `open` (unresolved)")
        elif kind == "missing":
            failures.append(f"{label}: no `status:` line found")
        elif kind == "invalid":
            failures.append(
                f"{label}: invalid status `{c['status_raw']}` "
                "(expected `open` | `addressed (commit: <sha>)` | "
                "`[USER-ACK] (reason: ...)`)"
            )
        # `addressed` and `user-ack` are pass states; no failure appended.

    if failures:
        print(f"concerns-resolved FAIL: {len(failures)} unresolved concern(s):")
        for f in failures:
            print(f"  {f}")
        print(
            "  hint: each concern needs `status: addressed (commit: <sha>)` "
            "after the fix lands, or `status: [USER-ACK] (reason: ...)` "
            "when the user has explicitly accepted the residual risk."
        )
        return 1

    # All concerns resolved. Also flag the "everything filed at the
    # same tick" anti-pattern -- still pass.
    ticks = {c["raised_tick"] for c in concerns if c["raised_tick"]}
    if len(concerns) > 1 and len(ticks) == 1:
        only_tick = next(iter(ticks))
        print(
            f"concerns-resolved WARN: all {len(concerns)} concerns were "
            f"raised at the same tick ({only_tick}); a healthy run "
            "spreads pessimism across ticks rather than batching it."
        )

    print(
        f"concerns-resolved: clean ({len(concerns)} concern(s), all "
        "addressed-with-commit or [USER-ACK])."
    )
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
