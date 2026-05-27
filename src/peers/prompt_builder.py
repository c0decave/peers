"""Deterministic prompt assembly from goal state and inbox."""
from __future__ import annotations

from peers.goals import Goal
from peers.goal_engine import GoalResult


SELF_REVIEW_BLOCK = """
SELF-REVIEW OBLIGATION (non-negotiable):
Before handing off, re-read the full diff of your turn and list bugs,
missing tests, doc drift, or anything fragile you spotted. Fix what
you can in follow-up commits within this same turn; flag what you
can't for the next peer. Your final commit's body MUST contain a
section starting with `## Self-Review`. Its trailers MUST include
`Self-Review: pass` and `Peer-Status: handoff`. Always also add a
`Peer: <your-name>` trailer so the other side knows who committed.
""".strip()


THOROUGH_TESTING_BLOCK = """
THOROUGH TESTING (default expectation):
Each non-trivial change you ship must come with tests that cover
all three case classes — happy path, edge cases, and sad path —
not just "the function returns something". Concretely:

  - happy: the obvious correct input → correct output;
  - edge: empty input, single element, max size, unicode, negative
    numbers, off-by-one boundaries, simultaneous events, missing
    file, permission denied, etc.;
  - sad: invalid input, malformed data, exceptions, timeouts,
    cancellation, partial writes, concurrent mutation, resource
    exhaustion.

When you add a feature, also actively HUNT for adjacent bugs (read
nearby code; ask "what would break if this got called with X?"
where X is something the spec doesn't promise won't happen). Don't
just defend against what the test happens to send — defend against
what a real caller can do.

Don't write `assert True` tests; the substrate's anti-cheating
revert will catch test-only diffs.
""".strip()


BUG_HUNT_BLOCK = """
BUG-HUNT PROTOCOL (non-negotiable before declaring done):

Two rounds, each: every peer reads the OTHER peer's diff since
`peers-baseline` and actively hunts for defects. File each finding as
its OWN commit so the substrate can count it.

Severity ladder (`crit` and `high` and `med` block completion; `low` /
`info` are advisory):

  - crit : data loss, RCE, crash on common input, security hole.
  - high : broken feature, wrong output, race, deadlock, memory leak.
  - med  : degraded UX, perf cliff, missing error handling on a real
           edge case.
  - low  : style nit, redundant code, minor naming issue.
  - info : observation / suggestion only.

Commit schema for FILING a bug (BUG-NNN is an ID you pick; must be
unique within the project; use BUG-001, BUG-002, ... as a default):

    BUG-NNN: <short title>

    ## Bug-Report
    {
      "id": "BUG-NNN",
      "severity": "high",          // crit|high|med|low|info
      "fix_by": "<other-peer>",    // optional but recommended
      "location": "src/x.py:42",   // optional
      "description": "<2-4 sentences: what is wrong, how to reproduce, what should happen>"
    }

    Peer: <your-name>
    Bug-Report: BUG-NNN

Commit schema for RESOLVING a bug (only `fix_by` peer should resolve;
make the actual code change in the SAME commit):

    Resolve BUG-NNN: <short note>

    ## Bug-Resolution
    {
      "resolves": "BUG-NNN",
      "status": "fixed",           // fixed|wontfix|duplicate|invalid
      "note": "<1-2 sentences>"
    }

    Peer: <your-name>
    Bug-Resolves: BUG-NNN

Verify the gate with: `python3 -m peers.bug_hunt summary`.

Rules:
- One bug per commit (so the substrate counts cleanly).
- File LOW/INFO bugs too — they don't block but help track follow-ups.
- Don't file bugs against the other peer's review comments / non-code
  commits; only against actual product code.
- `wontfix` keeps the bug open in the counter — use only with
  documentation of the trade-off and the OTHER peer's agreement.
""".strip()


CONVENTIONS = """
CONVENTIONS:
- Use git for all changes; commits with trailers are the message bus.
- Trailers go at the bottom of the commit body, one per line.
- Reviews of the other peer's work go in a commit body too, with the
  trailer `Peer-Review-Of: <sha>` and a `## Review` body section.
""".strip()


HYBRID_COMM_BLOCK = """
FILE-MESSAGE PROTOCOL (this project uses `comm: hybrid`):
- Code changes still go via git commits with trailers as above.
- For longer status notes, design rationale, or review request bodies
  that don't fit a commit message, write a markdown file:
      .peers/comms/<your-name>-to-<recipient>/NNNN-<short-topic>.md
  where NNNN is a 4-digit zero-padded sequence (start at 0001 if the
  directory is empty; otherwise next number). The file MUST begin with
  a YAML frontmatter block exactly like:

      ---
      from: <your-name>
      to: <recipient>
      ts: <ISO 8601 UTC>
      topic: <short-topic>
      ---

      <free-form markdown body>

- The substrate moves files to `.peers/comms/archive/` once they have
  been ingested. Never edit files in `archive/`.
""".strip()


SOFT_REVIEW_FORMAT_BLOCK = """
SOFT-REVIEW JSON FORMAT (strict):
Each pending soft review must be answered as a separate commit whose
BODY contains a `## Review` section followed by exactly one JSON
object with this shape:

    {
      "pass": true,                    // or false
      "notes": "<one or two sentences>"
    }

Optionally include "issues": ["..."] when pass=false. Do NOT wrap the
JSON in code fences, do NOT add prose between the `{` and `}` — the
substrate extracts the first balanced JSON object from the body and
will silently ignore the review if it cannot parse it.

The commit's TRAILERS must include:

    Peer-Review-Of: <goal-id>
    Peer: <your-name>

Example commit body:

    Review of docs-complete

    ## Review
    {
      "pass": true,
      "notes": "All public functions now have docstrings; examples in README.md compile."
    }

    Peer-Review-Of: docs-complete
    Peer: claude
""".strip()


def build_prompt(
    peer: str,
    other: str,
    goals: list[Goal],
    results: dict[str, GoalResult],
    inbox: list[str],
    stuck: bool,
    warnings: list[str] | None = None,
    soft_reviews_pending: list[Goal] | None = None,
    comm_variant: str = "git",
    all_peer_names: list[str] | None = None,
) -> str:
    parts: list[str] = []
    if all_peer_names and len(all_peer_names) > 2:
        roster = ", ".join(p for p in all_peer_names if p != peer)
        parts.append(
            f"You are peer '{peer}'. Your counterparts in this loop "
            f"are: {roster}."
        )
    else:
        parts.append(f"You are peer '{peer}'. Your counterpart is '{other}'.")
    parts.append("")

    if goals:
        parts.append("GOAL STATUS:")
        for g in goals:
            r = results.get(g.id)
            if r is None:
                parts.append(f"  - {g.id}: (not yet evaluated)")
            else:
                tail = f" — {r.diagnostic}" if r.diagnostic else ""
                parts.append(f"  - {g.id}: {r.state}{tail}")
        parts.append("")

    open_ids = [
        g.id for g in goals
        if (r := results.get(g.id)) is not None and r.state == "fail"
    ]
    if open_ids:
        parts.append("OPEN GOALS (focus here):")
        for gid in open_ids:
            parts.append(f"  - {gid}")
        parts.append("")

    if stuck:
        parts.append(
            "STUCK: previous attempts at the same strategy have not moved "
            "the goals. Propose something fundamentally different in this "
            "turn — do not just iterate on the last approach."
        )
        parts.append("")

    if warnings:
        parts.append("WARNINGS (substrate flagged these — address them):")
        for w in warnings:
            parts.append(f"  - {w}")
        parts.append("")

    if soft_reviews_pending:
        parts.append(
            "SOFT REVIEWS REQUESTED (please answer in this turn):"
        )
        for sg in soft_reviews_pending:
            parts.append(f"  - {sg.id}")
            for line in (sg.prompt or "").rstrip().splitlines():
                parts.append(f"      {line}")
        parts.append("")
        parts.append(SOFT_REVIEW_FORMAT_BLOCK)
        parts.append("")

    if inbox:
        parts.append("INBOX (messages from your peer):")
        for msg in inbox:
            parts.append(f"  - {msg}")
        parts.append("")

    parts.append("TASK: Take ONE concrete step toward the open goals.")
    parts.append("")
    parts.append(THOROUGH_TESTING_BLOCK)
    parts.append("")
    parts.append(BUG_HUNT_BLOCK)
    parts.append("")
    parts.append(SELF_REVIEW_BLOCK)
    parts.append("")
    parts.append(CONVENTIONS)
    if comm_variant == "hybrid":
        parts.append("")
        parts.append(HYBRID_COMM_BLOCK)

    return "\n".join(parts)
