#!/usr/bin/env python3
"""Exit 1 if implementer/reviewer blind-review artefacts are missing or
explicitly mismatched.

Schicht-6 honesty gate for implement-mode (Task 6.1). The "blind review"
mechanism makes the reviewer-peer describe the implementation *without*
reading the implementer-peer's notes; the two independent descriptions
are then compared (by a human / a downstream tick) for divergence.

This gate enforces only the **structural** half of that contract -- the
behavioural half ("reviewer must not read IMPLEMENTATION_NOTES.md") lives
in the Phase 0 prompt templates, which is the right place because no
post-hoc filesystem check can prove a peer didn't peek. Here we only
verify that the two artefacts exist, are non-trivial, and that no
explicit mismatch marker has been filed by either peer.

Inputs (at project root)
------------------------
* ``IMPLEMENTATION_NOTES.md`` -- implementer-peer's description of what
  was built. Required, must contain at least ``_MIN_WORDS`` words after
  whitespace tokenisation.
* ``REVIEW_NOTES.md`` -- reviewer-peer's description of what the
  implementation appears to be. Required, same word-count threshold.
* ``CONCERNS.md`` -- optional. If present and a line *begins* with the
  marker ``[BLIND-REVIEW-MISMATCH]`` (after optional Markdown list/heading/
  blockquote prefixes and an optional backtick), the gate fails. This is the
  explicit escape valve for either peer to record "I read the other side's
  notes and they don't match what's actually in the tree". A marker that only
  appears *mid-prose* (e.g. a Concern that documents the protocol: "if codex
  disagrees, file ``[BLIND-REVIEW-MISMATCH]``") is a reference, not a filing,
  and does NOT trip the gate -- the reviewer prompt instructs filing it as a
  line.

Why no content semantics
------------------------
A soft convergence ratio (shared bag-of-words overlap) is tempting but
unreliable -- two correct descriptions of the same change in different
vocabulary would fail it, while two boilerplate descriptions would
trivially pass it. We deliberately stop at structural checks; the
honesty signal comes from the **independence** of the two writers,
which is a prompt-template invariant, not a textual one.

Exit codes
----------
* ``0`` -- both files present, both above the word threshold, no
  line-leading ``[BLIND-REVIEW-MISMATCH]`` marker in ``CONCERNS.md``.
* ``1`` -- any of: file missing, file too short, mismatch marker
  filed. Stdout names the failing condition.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_IMPL_NOTES_NAME = "IMPLEMENTATION_NOTES.md"
_REVIEW_NOTES_NAME = "REVIEW_NOTES.md"
_CONCERNS_NAME = "CONCERNS.md"
_MISMATCH_MARKER = "[BLIND-REVIEW-MISMATCH]"
_MIN_WORDS = 20

# A *filed* mismatch is the marker at the START of a line, FOLLOWED BY a
# description of the divergence — what the reviewer prompt instructs. A marker
# that appears mid-prose, or wrapped in inline code with nothing after it but
# punctuation ("...if codex disagrees, file `[BLIND-REVIEW-MISMATCH]`."), is a
# REFERENCE, not a filing, and must not trip this hard honesty gate.
#
# Detection is per-line (the `^`/`$` anchors + line-local whitespace classes,
# so nothing matches across newlines — an earlier global inline-code strip
# `\`[^\`]*\`.sub("")` was a false-NEGATIVE: it erased a genuine filing that was
# itself backticked, or that sat between two stray backticks on other lines).
# A line fires iff, after optional Markdown line prefixes (list/heading/quote/
# number) and an optional ``\``` / ``**`` / ``__`` wrapper, the marker is present
# and is followed EITHER by end-of-line or by a token containing a word char (a
# real description). Trailing punctuation only (the reference case) does NOT fire.
_WRAP = r"(?:\*\*|__|`)?"
_FILED_MISMATCH_RE = re.compile(
    r"^[ \t>]*(?:[-*+][ \t]+|#{1,6}[ \t]+|\d+\.[ \t]+)*"
    + _WRAP + re.escape(_MISMATCH_MARKER) + _WRAP
    + r"[ \t]*(?:$|\S*[A-Za-z0-9])",
    re.MULTILINE,
)


def _word_count(text: str) -> int:
    """Count whitespace-separated tokens. Markdown formatting characters
    are not stripped -- a peer writing ``**done**`` legitimately spent a
    word on emphasis."""
    return len(text.split())


def main(project_dir: str = ".") -> int:
    """Verify implementer and reviewer independently described the
    implementation. Structural-only enforcement (see module docstring)."""
    project_root = Path(project_dir).resolve()
    impl_path = project_root / _IMPL_NOTES_NAME
    review_path = project_root / _REVIEW_NOTES_NAME
    concerns_path = project_root / _CONCERNS_NAME

    failures: list[str] = []

    # ---- Existence checks. ----
    if not impl_path.is_file():
        failures.append(f"{_IMPL_NOTES_NAME} missing at project root")
    if not review_path.is_file():
        failures.append(f"{_REVIEW_NOTES_NAME} missing at project root")

    # ---- Word-count checks (only if the file actually exists). ----
    if impl_path.is_file():
        impl_words = _word_count(impl_path.read_text(encoding="utf-8"))
        if impl_words < _MIN_WORDS:
            failures.append(
                f"{_IMPL_NOTES_NAME} trivial ({impl_words} words, "
                f"need >= {_MIN_WORDS})"
            )
    if review_path.is_file():
        review_words = _word_count(review_path.read_text(encoding="utf-8"))
        if review_words < _MIN_WORDS:
            failures.append(
                f"{_REVIEW_NOTES_NAME} trivial ({review_words} words, "
                f"need >= {_MIN_WORDS})"
            )

    # ---- Explicit mismatch marker (a FILED line, not a prose/backtick reference). ----
    if concerns_path.is_file():
        concerns_text = concerns_path.read_text(encoding="utf-8")
        if _FILED_MISMATCH_RE.search(concerns_text):
            failures.append(
                f"{_CONCERNS_NAME} contains {_MISMATCH_MARKER} "
                f"-- reviewer/implementer recorded a mismatch"
            )

    if failures:
        print(f"blind-review FAIL: {len(failures)} issue(s):")
        for f in failures:
            print(f"  {f}")
        print(
            "  hint: implementer writes IMPLEMENTATION_NOTES.md, "
            "reviewer writes REVIEW_NOTES.md independently (without "
            "reading the impl notes); both must be >= "
            f"{_MIN_WORDS} words. Either peer may file "
            f"{_MISMATCH_MARKER} in {_CONCERNS_NAME} to fail the gate."
        )
        return 1

    print(
        f"blind-review: clean ({_IMPL_NOTES_NAME} + {_REVIEW_NOTES_NAME} "
        f"both present and >= {_MIN_WORDS} words, no mismatch marker)"
    )
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
