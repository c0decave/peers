from __future__ import annotations

import hashlib
import json
from pathlib import Path

from peers.goals import _GOALS_YAML_MAX_BYTES
from peers.safe_io import read_bytes_no_symlink


_AUTO_SKEPTIC_PROMPT_PREFIX = """\
=== POST-CONVERGENCE SKEPTIC RE-AUDIT ===

Der substrate hat soeben convergence-reached gemeldet: N consecutive
clean ticks ohne neue blocking/shallow Bug-Reports. **Das ist verdächtig.**
Dieser tick ist eine KRITISCHE WIEDERHOLUNG der eigenen Konvergenz:

- Hat der vorherige Audit wirklich alle relevanten Failure-Modes geprüft,
  oder hat sich das peer-Paar in seiner eigenen Zusammenfassung beruhigt?
- Welche src-Files wurden seit peers-baseline NIE oder zu oberflächlich
  reviewt? Pick mindestens DREI und audit-e sie gewissenhaft jetzt.
- Welche neuen src-Files entstanden während diesem run (z.B. recon.py, additions)? Wurden DIE selbst auditiert oder nur
  passiv mit-gefixt?
- Welche soft-reviews wurden mit "passed" abgesegnet, hätten aber bei
  ehrlicher Prüfung ein FAIL verdient?
- Welche Bug-Reports wurden weak-fix'd oder shallow-fix'd geschlossen?

Wenn du in dieser Re-Audit-Phase MINDESTENS EINEN echten neuen Bug
findest (crit/high/med, mit Repro), file ihn — der `consecutive_clean_
ticks`-Counter resettet sich automatisch und der Loop läuft weiter.
Wenn nach gewissenhafter Suche WIRKLICH nichts da ist, dokumentiere
konkret welche 5+ Failure-Modes du in diesem Pass explizit ausgeschlossen
hast (pro Modul). Pauschal-"sauber" gilt nicht.

Erst NACH diesem Skeptiker-Tick wird der run als terminal-success
beendet. Ein einziger Tick — also ehrlicher als üblich.

=== END SKEPTIC HEADER ===
"""


def _hash_goals_yaml(path: Path) -> str:
    data = read_bytes_no_symlink(path, max_bytes=_GOALS_YAML_MAX_BYTES + 1)
    if len(data) > _GOALS_YAML_MAX_BYTES:
        raise ValueError(
            f"goals.yaml too large (max {_GOALS_YAML_MAX_BYTES} bytes)"
        )
    return hashlib.sha256(data).hexdigest()


def _extract_first_json_object(body: str) -> dict | None:
    """Scan `body` for the first balanced `{...}` block and return its
    decoded JSON dict, or None if no parseable object is found.

    Replaces the legacy single-nesting regex
    `\\{[^{}]*(?:\\{[^{}]*\\}[^{}]*)*\\}` which rejected soft-review
    JSON containing nested objects. Tolerates:
    - braces inside string literals (skipped via a tiny string-aware
      state machine);
    - multiple consecutive `{...}` blocks (first parseable wins);
    - prose before/after the block.
    """
    in_string = False
    escape = False
    depth = 0
    start = -1
    for i, ch in enumerate(body):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                snippet = body[start:i + 1]
                try:
                    val = json.loads(snippet)
                except json.JSONDecodeError:
                    start = -1
                    continue
                if isinstance(val, dict):
                    return val
                start = -1
    return None


# ---------------------------------------------------------------------------
# Task 4.1 — implement-mode Phase 0 state machine.
#
# Phase 0 is a 3-tick prep prelude that runs ONLY in implement-mode,
# before the normal tick loop produces implementation commits:
#
#   tick 0 → "recon"         — peer writes RECON.md
#   tick 1 → "alignment"     — peer writes PLAN.aligned.md
#   tick 2 → "architecture"  — peer writes ARCHITECTURE.intended.md
#   tick 3+→ "implementation"— normal per-step implementation ticks
#
# Every other mode (audit/security/thorough/unknown custom modes) stays
# in "implementation" from tick 0 — strict backward compatibility for
# pre-implement-mode users.
#
# Tasks 4.2 – 4.4 wire real Phase 0 prompts on top of this scaffold; the
# driver currently just records the phase string in state.json and logs
# a tick-start marker so operators can see which phase is active. The
# normal prompt path is unchanged (existing behaviour — we do NOT
# yet alter peer prompts based on phase).
_PHASE_0_TICKS = (
    "recon",         # tick 0
    "alignment",     # tick 1
    "architecture",  # tick 2
)
PHASE_IMPLEMENTATION = "implementation"


def _resolve_phase(mode_name: str, tick_number: int) -> str:
    """Return the phase string for the upcoming tick.

    Pure function — depends only on `mode_name` (string match against
    the literal "implement") and `tick_number` (0-indexed count of
    completed ticks; i.e. the index of the tick about to fire).

    Unknown modes (anything other than "implement") always return
    "implementation" — Phase 0 is intentionally exclusive to first-
    party implement-mode to avoid surprising custom-mode authors.
    """
    if mode_name != "implement":
        return PHASE_IMPLEMENTATION
    if 0 <= tick_number < len(_PHASE_0_TICKS):
        return _PHASE_0_TICKS[tick_number]
    return PHASE_IMPLEMENTATION


# Task 6.2 — blind-review tick orchestration.
#
# During implement-mode's "implementation" phase, the implementer and
# reviewer roles alternate on a per-tick basis so each tick has a
# well-defined "blind reader" of the previous tick's code-diff.
#
#   tick 3 → implementer  (writes IMPLEMENTATION_NOTES.md)
#   tick 4 → reviewer     (writes REVIEW_NOTES.md without peeking)
#   tick 5 → implementer
#   tick 6 → reviewer
#   ...
#
# The driver does not change the existing peer rotation (A → B → A →
# B...) — peers are simply told what role they hold for the upcoming
# tick via a prompt overlay (`blind_review_implementer.md` /
# `blind_review_reviewer.md`).
#
# All non-implement modes (audit / security / thorough / custom user
# modes) and all Phase 0 ticks (recon / alignment / architecture)
# return "normal" — they are unaffected by this overlay.
def _resolve_peer_role(mode_name: str, phase: str, tick: int) -> str:
    """Return 'implementer' | 'reviewer' | 'normal' for the upcoming tick.

    For implement-mode in the "implementation" phase (tick 3 onward),
    alternates: even offsets from tick 3 are implementer, odd are
    reviewer. Every other (mode, phase) combination returns "normal"
    — strict backward-compat for non-implement modes and for the
    Phase 0 prelude ticks.
    """
    if mode_name != "implement":
        return "normal"
    if phase != PHASE_IMPLEMENTATION:
        return "normal"
    return "implementer" if (tick - 3) % 2 == 0 else "reviewer"


def _should_checkpoint(
    peer_dir: Path,
    *,
    prev_phase: str | None,
    curr_phase: str,
) -> bool:
    """Return True iff the driver should pause for operator review.

    Task 4.5: when `peers-ctl start <project> --checkpoint` is used,
    the CLI writes a `.peers/checkpoint_requested` marker before
    launching the loop. The driver checks this marker on every tick
    and, when the phase transitions from "architecture" (Phase 0
    tick 2) to "implementation" (the first real impl tick), exits
    cleanly so the operator can review RECON.md + PLAN.aligned.md +
    ARCHITECTURE.intended.md before any code lands.

    Pure-ish helper: only filesystem side-effect is `is_file()` on
    the marker. Phase comparison is exact-string — this is the v1
    contract (architecture → implementation is the sole trigger).
    """
    if prev_phase != "architecture" or curr_phase != PHASE_IMPLEMENTATION:
        return False
    marker = peer_dir / "checkpoint_requested"
    return marker.is_file()


def _load_phase_prompt(mode_name: str, phase: str) -> str | None:
    """Return the text of a Phase 0 prompt template, or None if missing.

    Resolves to `src/peers/templates/modes/<mode_name>/prompts/<phase>.md`
    inside the installed `peers` package — i.e. discovered via the
    package's own __file__, NOT the caller's cwd. The lookup is
    intentionally narrow: any mode/phase combination without a shipped
    template returns None, which signals "no overlay; use the default
    prompt" to the caller.

    Used by the driver to overlay Phase 0 instructions (Tasks 4.2-4.4)
    on the per-tick prompt when implement-mode is active during ticks
    0/1/2 (recon → alignment → architecture).
    """
    # Reject obviously bogus / path-traversing inputs early so the
    # template lookup never escapes the templates/ tree.
    if not mode_name or not phase:
        return None
    if "/" in mode_name or "/" in phase or ".." in mode_name or ".." in phase:
        return None
    pkg_root = Path(__file__).resolve().parent
    prompt_path = (
        pkg_root / "templates" / "modes" / mode_name / "prompts"
        / f"{phase}.md"
    )
    if not prompt_path.is_file():
        return None
    try:
        return prompt_path.read_text(encoding="utf-8")
    except OSError:
        return None


def _detect_mode_name(peer_dir: Path) -> str:
    """Best-effort lookup of the active mode from `.peers/modes-applied.txt`.

    Returns "implement" only when implement-mode is the SOLE active mode
    (v1 of implement-mode is documented as standalone — not composable
    with audit/security/thorough). Returns "" on missing file, parse
    failure, or composed mode-stacks containing implement alongside
    others. The empty string flows through `_resolve_phase` as
    "implementation" — i.e. Phase 0 is silently skipped in those cases.

    Format (one mode per line, written by `peers init --modes`):
        2026-05-26T12:34:56+00:00  implement     v1  sha256=...
    """
    trail = peer_dir / "modes-applied.txt"
    try:
        text = trail.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    names: list[str] = []
    for line in text.splitlines():
        # The mode name is the 2nd whitespace-separated token (timestamp
        # is the 1st). Skip blank / malformed lines.
        parts = line.split()
        if len(parts) >= 2:
            names.append(parts[1])
    if names == ["implement"]:
        return "implement"
    if "hunt-open-ended" in names:
        return "hunt-open-ended"
    return ""


def _format_tick_status(*, success: bool, classification: str) -> str:
    """Operator-readable tick-end status label.

    Returns one of:
      `handoff`     — peer made a valid handoff commit
                      (`Peer-Status: handoff` + `Self-Review: pass`).
      `no-handoff`  — peer process exited cleanly (rc=0, no error)
                      but did NOT produce a valid handoff commit
                      (no new commit, missing trailers, history
                      rewritten, etc).
      <classification> — peer subprocess errored; classification is
                         `process-fail` / `api-error` /
                         `idle-timeout` / `absolute-timeout`.

    Replaces the older `fail({classification})` formatting (2026-05-26
    operator-UX fix): `fail(success)` was self-contradictory at a
    glance — the run did NOT fail, the peer simply didn't commit.
    """
    if success:
        return "handoff"
    if classification == "success":
        return "no-handoff"
    return classification
