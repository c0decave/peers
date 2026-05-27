#!/usr/bin/env python3
"""Exit 1 if `HONESTY_AUDIT.md` is missing, under-staffed, structurally
incomplete, or carries trivial answers.

Schicht-6 honesty gate for implement-mode (Task 6.4). The honesty audit
is the *final* convergence anchor: before the loop is allowed to declare
``complete``, both peers must answer the same three hard questions
about the implementation they just produced. The questions are
deliberately uncomfortable -- they ask each peer to surface what they
*know* is shaky, even if every other gate is green:

    * **Weakest part**       -- which part of the change is most
                                fragile, ugly, or under-thought?
    * **Likely uncaught bug** -- which failure mode would survive the
                                test suite as it stands?
    * **Skipped or shortcut** -- what did you not do that you would
                                have done with infinite budget, and
                                why was the skip safe enough?

This gate enforces only the **structural** half of that contract: that
every configured peer wrote a non-trivial answer to all three. The
behavioural half ("answers must be honest, not boilerplate") is human-
in-the-loop -- no post-hoc filesystem check can prove a peer wasn't
phoning it in. What we *can* prove is that something with a pulse was
written under each heading.

Schema (at project root)
------------------------
``HONESTY_AUDIT.md``::

    # Honesty Audit

    ## <peer-name>          # one H2 per peer (e.g. claude, codex, ...)

    ### Weakest part        # exactly these three H3 subsections per peer
    <prose, >5 words>
    ### Likely uncaught bug
    <prose, >5 words>
    ### Skipped or shortcut
    <prose, >5 words>

    ## <other-peer-name>
    ### Weakest part
    ...

Rules
-----
* The file must exist.
* At least **2** ``## <peer>`` H2 sections must be present (the two
  default peers are ``claude`` + ``codex``, but the gate is name-agnostic
  -- it counts any H2 as a peer section so swapping or adding peers
  works without code changes). Extra peer sections (e.g. ``## gemini``)
  are allowed and also validated.
* Each peer section must contain all three required H3 subsections
  ``Weakest part`` / ``Likely uncaught bug`` / ``Skipped or shortcut``,
  matched **case-insensitively** (``### Weakest Part`` is fine).
* Each H3 subsection's body must contain more than ``_MIN_WORDS``
  whitespace-separated tokens -- single-word evasions like ``none`` /
  ``n/a`` / ``nothing`` are rejected as trivial.

Exit codes
----------
* ``0`` -- file present, >= 2 peer sections, every peer section
  complete with non-trivial content under all three subsections.
* ``1`` -- any of: file missing, < 2 peer sections, any peer missing a
  required subsection, any subsection body too short.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_AUDIT_NAME = "HONESTY_AUDIT.md"

# H2 line opening a per-peer block: `## <name>`. The name capture is
# greedy-to-EOL so multi-word peer labels work, though canonical names
# are single tokens (claude / codex / gemini / ...).
_PEER_HEADER_RE = re.compile(r"^##\s+(\S.*?)\s*$")

# H3 line opening a per-question subsection: `### Weakest part`.
_SUB_HEADER_RE = re.compile(r"^###\s+(\S.*?)\s*$")

# Three required subsection headings, normalised to lowercase for the
# case-insensitive match. The trailing tuple is the canonical (cased)
# label used in failure messages.
_REQUIRED_SUBSECTIONS: tuple[tuple[str, str], ...] = (
    ("weakest part", "Weakest part"),
    ("likely uncaught bug", "Likely uncaught bug"),
    ("skipped or shortcut", "Skipped or shortcut"),
)

# Minimum *exceeded* word count -- bodies must contain strictly more
# than this many whitespace-separated tokens. 5 catches `none`,
# `n/a`, `nothing to report`, and similar short evasions while still
# accepting a real one-sentence answer.
_MIN_WORDS = 5

# Minimum number of peer sections the gate accepts. With fewer than
# two, the "two independent perspectives" point of the audit collapses.
_MIN_PEERS = 2


def _word_count(text: str) -> int:
    """Count whitespace-separated tokens. Markdown formatting characters
    are kept -- a peer writing ``**fragile**`` legitimately spent a
    word on emphasis."""
    return len(text.split())


def _parse_audit(text: str) -> dict[str, dict[str, str]]:
    """Split HONESTY_AUDIT.md into ``{peer_name: {sub_name_lower: body}}``.

    * Peer order is preserved by dict insertion order.
    * Subsection bodies are the raw text between an H3 line and the
      next H2/H3 line (or EOF), with leading/trailing whitespace
      stripped. Empty bodies map to the empty string -- the caller
      decides whether that's a failure (it is).
    """
    peers: dict[str, dict[str, str]] = {}
    current_peer: str | None = None
    current_sub: str | None = None
    body_buf: list[str] = []

    def _flush_subsection() -> None:
        if current_peer is not None and current_sub is not None:
            peers[current_peer][current_sub] = "\n".join(body_buf).strip()

    for raw_line in text.splitlines():
        peer_match = _PEER_HEADER_RE.match(raw_line)
        sub_match = _SUB_HEADER_RE.match(raw_line)

        if peer_match:
            _flush_subsection()
            current_peer = peer_match.group(1).strip()
            current_sub = None
            body_buf = []
            # Initialise per-peer dict idempotently (a peer header
            # appearing twice would just merge -- that's fine; the
            # last subsection bodies win).
            peers.setdefault(current_peer, {})
            continue

        if sub_match:
            _flush_subsection()
            current_sub = sub_match.group(1).strip().lower()
            body_buf = []
            continue

        if current_sub is not None:
            body_buf.append(raw_line)

    _flush_subsection()
    return peers


def main(project_dir: str = ".") -> int:
    """Verify HONESTY_AUDIT.md exists with both peers' 3-question audit.

    See module docstring for the schema and the full rule set.
    """
    project_root = Path(project_dir).resolve()
    audit_path = project_root / _AUDIT_NAME

    if not audit_path.is_file():
        print(f"honesty-audit FAIL: {_AUDIT_NAME} missing at project root.")
        print(
            "  hint: before declaring complete, each peer must answer "
            "three honesty questions (Weakest part / Likely uncaught "
            f"bug / Skipped or shortcut) in {_AUDIT_NAME}."
        )
        return 1

    text = audit_path.read_text(encoding="utf-8")
    peers = _parse_audit(text)

    failures: list[str] = []

    if len(peers) < _MIN_PEERS:
        failures.append(
            f"only {len(peers)} peer section(s) found "
            f"(need >= {_MIN_PEERS}; default peers are `## claude` + "
            "`## codex`)"
        )

    for peer_name, subs in peers.items():
        for sub_key, sub_label in _REQUIRED_SUBSECTIONS:
            if sub_key not in subs:
                failures.append(
                    f"peer `{peer_name}` missing `### {sub_label}` "
                    "subsection"
                )
                continue
            body = subs[sub_key]
            words = _word_count(body)
            if words <= _MIN_WORDS:
                failures.append(
                    f"peer `{peer_name}` `### {sub_label}` body is "
                    f"trivial ({words} word(s), need > {_MIN_WORDS})"
                )

    if failures:
        print(f"honesty-audit FAIL: {len(failures)} issue(s):")
        for f in failures:
            print(f"  {f}")
        print(
            "  hint: each peer's H2 section (`## <peer>`) must contain "
            "all three H3 subsections (`### Weakest part`, "
            "`### Likely uncaught bug`, `### Skipped or shortcut`), "
            f"each with > {_MIN_WORDS} words of substantive prose. "
            "Header casing is flexible; bodies are not."
        )
        return 1

    print(
        f"honesty-audit: clean ({len(peers)} peer section(s), all with "
        "3 substantive subsections)."
    )
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
