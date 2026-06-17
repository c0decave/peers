#!/usr/bin/env python3
"""Generic ``report-cited`` gate — a ``findings_anchored_and_cited`` honesty
check with the security frame lifted OUT.

The research mode (Stage 2) is a generic KNOWLEDGE mode on the spine, so its
report honesty contract drops the ATT&CK/CAPEC/CWE framework anchor (a
non-security topic like "cloning plants in Alaska" has no such ids) but KEEPS
the three frame-independent floors:

  1. CITED — at least ``MIN_CITATIONS`` distinct primary-source URLs
     (``http(s)://...``). Claims must be checkable. Lowered to 2 because a
     generic report corroborates a load-bearing claim with TWO origin-
     independent witnesses (mirrors the claim ledger's ≥2 rule).
  2. HONEST GAPS — a non-empty ``## Gaps`` (or ``## Limitations``) section with
     >= ``MIN_GAPS_CHARS`` of body. The deliverable must state what it did NOT
     cover. Lowered to 40 chars for the relaxed generic frame.
  3. NO BARE COMPLETENESS CLAIM — the doc must not assert it is "exhaustive" /
     "100% complete" / "all known" / "fully comprehensive". Research is never
     complete; the gaps section is the honest alternative.

DROPPED (vs the parent): ``_ANCHOR_RE`` / ``MIN_ANCHORS`` (the security-only
framework anchor).

Unlike the parent's ``main(root)`` (which appends ``RESEARCH.md`` to a root dir),
``check_report`` takes the report FILE path directly and returns
``(ok, problems)`` so the :class:`peers.research.adapters.ReportSynthesizer`
can gate its own freshly-written file. Fail-CLOSED: a missing / unreadable /
symlinked file returns ``(False, [...])``. ``main()`` is the CLI shim that
resolves ``<root>/RESEARCH.md`` and prints the single-line idiom.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

DOC_NAME = "RESEARCH.md"
_DOC_MAX_BYTES = 4 * 1024 * 1024  # 4 MiB cap (same as the parent gate)
# Relaxed generic floors (the security frame is lifted out): two independent
# citations + a real gaps paragraph clear the bar.
MIN_CITATIONS = 2
MIN_GAPS_CHARS = 40

_URL_RE = re.compile(r"https?://[^\s)>\]}\"']+")
# Bare completeness claims that are dishonest by construction in a research
# collection (the gaps section is the honest alternative).
_OVERCLAIM_RES = (
    re.compile(r"\bexhaustive\b", re.IGNORECASE),
    re.compile(r"100\s*%\s*(?:complete|coverage|comprehensive)", re.IGNORECASE),
    re.compile(r"\b(?:all|every)\s+known\s+\w+", re.IGNORECASE),
    re.compile(r"\bfully\s+comprehensive\b", re.IGNORECASE),
)
_GAPS_HEADINGS = ("gaps", "limitations", "not covered", "uncovered", "coverage gaps")
_NEGATION_WINDOW_CHARS = 80
# --- Negation / qualification matcher: ALLOWLIST gap, robust by construction ---
# A completeness adjective is treated as NEGATED/qualified (honest, not flagged)
# ONLY when a negation MARKER reaches it through a CLOSED allow-set of tokens:
#   * degree / comparative adverbs  ("not FULLY/YET/AS/SO/ENTIRELY exhaustive"),
#   * distributed-negation conjuncts "<word> or|nor"
#     ("not complete OR exhaustive", "neither complete NOR exhaustive").
# Anything else between the marker and the adjective — a verb, an article, a
# noun, punctuation, a clause boundary — BREAKS the connection, so the adjective
# is flagged. This is the INVERSE of the prior design, which let a marker span an
# arbitrary `(?:[\w'-]+\s+){0,5}` window and then clawed it back with an
# ever-growing BLOCKLIST of clause boundaries (BUG-729..734, each one more
# subordinator). That blocklist could never be complete: a matrix-clause negation
# with a <=5-word gap ("We did not confirm the dataset stays exhaustive") still
# leaked — with no subordinator at all. The allowlist closes the whole
# false-negative class by construction: a content word, which every embedded
# positive clause must contain, can never enter the gap. BUG-738 adds the
# mirror-image guard: idioms whose negation token flips positive ("nothing if
# not exhaustive", "by no means non-exhaustive", rhetorical "Is this not
# exhaustive?") are not honest negations.
#
# Contract direction is UNCHANGED and intentional — CONSERVATIVE-BY-DEFAULT and
# FAIL-SAFE: an UNRECOGNISED honest idiom ("hardly exhaustive", "anything but
# exhaustive") is OVER-flagged rather than waved through, fixed by rephrasing or
# moving the disclaimer into `## Gaps`. Such exotic-grammar over-flags are
# info/low, not blocking defects.
#
# SCOPE OF THE GUARANTEE. This matcher is a BEST-EFFORT, FAIL-SAFE heuristic over
# an OPEN grammatical class — NOT a complete decision procedure. It flags every
# bare overclaim on the pinned contract corpus (tests/unit/test_overclaim_matcher_
# parity.py), including the finite negation-flip idioms found in BUG-738/739, but
# natural-language negation/affirmation is unbounded. Newly found realistic false
# negatives remain real defects; contrived fringe idioms may be info/low, but must
# still be pinned if they are accepted as known limitations.
#
# Completeness-honesty is DEFENSE-IN-DEPTH, never this regex alone: a report that
# slips an exotic overclaim past the matcher must still clear the CITED floor (>=
# MIN_CITATIONS origin-independent URLs), the HONEST GAPS floor (a real `## Gaps`
# section), and human review. Those backstops reduce blast radius; they do not make
# a realistic matcher false negative acceptable.
_NEGATION_MARKER = r"""
    (?:
        \b(?:not|never|no|without|neither)\s+
      | \b(?:is|are|was|were|do|does|did)\s+not\s+
      | \b(?:isn['’]?t|aren['’]?t|wasn['’]?t|weren['’]?t|can['’]?t|cannot)\s+
      | \bfar\s+from\s+
      | \bnon[-\s]+
    )
"""
# A CLOSED set of degree/comparative adverbs (plus the perfect-aspect copula
# "been") that may sit between the marker and the adjective without breaking the
# negation. Predicate-head verbs (remains/became/stays/proved/...) are
# deliberately ABSENT — they are exactly the signal of a NEW positive clause, so
# "not lost and REMAINS exhaustive" stays flagged.
# "all" (added in BUG-740 so "not at all exhaustive" reads as the honest degree
# phrase) is the one token here that is ALSO a universal QUANTIFIER. That stays
# false-negative-SAFE: a quantifier "all" is always followed by a noun ("all
# sources …"), a content word that breaks the allowlist gap, so it can never
# bridge a marker to a genuine matrix-/new-clause overclaim — "Not all gaps are
# closed, yet the catalogue is exhaustive." stays flagged. The BUG-740 sad-
# direction locks in tests/unit/test_overclaim_matcher_parity.py pin this.
_DEGREE_ADVERBS = r"""
    (?:\b(?:
        fully | quite | very | so | as | too | that | yet | once | truly
      | really | genuinely | entirely | wholly | totally | completely
      | comprehensively | thoroughly | particularly | especially | remotely
      | nearly | almost | overly | perfectly | sufficiently | necessarily
      | always | all | ever | even | terribly | remarkably | exactly | been
    )\b\s+)*
"""
# Distributed-negation conjunct(s): one word joined by or/nor ("not complete OR
# exhaustive", "not broad OR 100% complete", "neither complete NOR exhaustive").
# "and"/"but" are NOT here — "not complete BUT exhaustive" / "not lost AND remains
# exhaustive" start a new positive claim and must stay flagged.
_DISTRIB_CONJUNCTS = r"(?:[\w'’-]+\s+(?:or|nor)\s+)*"
# A control/raising verb + infinitival "to be": "cannot claim TO BE 100% complete",
# "does not appear TO BE exhaustive". The MANDATORY "to be" anchor is what keeps
# this safe — a new finite clause ("the scan WAS exhaustive", "the dataset STAYS
# exhaustive") has no "to be", so it still BLOCKS; only a same-subject infinitival
# complement (no new subject) passes. Without it the lone honest "to be" idiom
# would be over-flagged.
_RAISING_TO_BE = r"(?:(?:[\w'’-]+\s+)?to\s+be\s+)?"
# At most ONE further non-degree word may sit in the gap — the copular/perception
# slot of a SINGLE predicate: "is not CONSIDERED exhaustive", "does not SEEM
# exhaustive", "did not PROVE exhaustive". This is FALSE-NEGATIVE-SAFE by the
# token-count invariant: a new finite clause that could positively assert the
# adjective needs a subject AND a verb (>= 2 tokens, e.g. "the scan was"), so one
# token can never carry one. It restores the honest single-verb forms the prior
# window accepted, without reopening the multi-word leak.
_ONE_LINKING_WORD = r"(?:[\w'’-]+\s+)?"
# "not ONLY/JUST/MERELY exhaustive" ASSERTS exhaustiveness (focus particle); the
# marker must not be immediately followed by one, or the overclaim slips through.
_FOCUS_PARTICLE_GUARD = r"(?!(?:only|just|merely)\b)"
# 'without'/'no'/'never' are negation MARKERS, but the fixed emphatic-
# AFFIRMATION idioms "without question/doubt/fail/exception" and "no doubt/
# question" are POSITIVE intensifiers (= "undoubtedly"). The single affirmation
# lexeme then fills _ONE_LINKING_WORD (or, for the litotes "never fails to be
# exhaustive", the _RAISING_TO_BE word slot), so the allowlist gap closes onto
# the completeness adjective and a genuine UNQUALIFIED overclaim is wrongly read
# as negated. The marker must not be IMMEDIATELY followed by an affirmation
# lexeme. This is a CLOSED, finite idiom inventory (inflection-tolerant via \w*),
# unlike the OPEN subordinator class the prior blocklist chased,
# so it does not reopen that enumeration trap. FAIL-SAFE: it only ADDS flags.
_AFFIRMATION_IDIOM_GUARD = r"(?!(?:question|doubt|fail|exception|dispute)\w*\b)"
_NEGATED_OVERCLAIM_PREFIX_RE = re.compile(
    _NEGATION_MARKER
    + _FOCUS_PARTICLE_GUARD
    + _AFFIRMATION_IDIOM_GUARD
    + _DISTRIB_CONJUNCTS
    + _RAISING_TO_BE
    + _ONE_LINKING_WORD
    + _DEGREE_ADVERBS
    + r"\Z",
    re.IGNORECASE | re.VERBOSE,
)
# Negation-flip idioms that look like the honest negation shapes above but
# semantically assert the adjective. Veto them after the ordinary negation
# matcher succeeds so honest "non-exhaustive" / "not entirely exhaustive" forms
# keep their clean path.
_AFFIRMING_NEGATION_FLIP_PREFIX = (
    r"(?:"
    r"\bnothing\s+if\s+not\s+" + _DEGREE_ADVERBS
    + r"|\bby\s+no\s+means\s+non[-\s]+" + _DEGREE_ADVERBS
    + r"|\bin\s+no\s+(?:way|sense)\s+non[-\s]+" + _DEGREE_ADVERBS
    + r"|\bby\s+no\s+stretch\s+non[-\s]+" + _DEGREE_ADVERBS
    + r"|\bnot\s+at\s+all\s+non[-\s]+" + _DEGREE_ADVERBS
    + r"|\bnot\s+non[-\s]+" + _DEGREE_ADVERBS
    # BUG-741 (harvest adversarial review): two STRUCTURAL affirmation flips that
    # the prior CLOSED enumerations missed. (a) double-negation litotes "never
    # not <adj>" = "always <adj>". (b) the open emphatic-affirmation idiom
    # "without/no <intensifier-noun> <adj>" ("without reservation/hesitation/
    # qualification/… exhaustive", "no question exhaustive") — a bare 'without'/
    # 'no' marker that bridges a SINGLE word onto the completeness adjective is
    # affirmation, not negation. Matching the STRUCTURE (one bridging word) rather
    # than enumerating the noun closes the whole open class at once. The honest
    # aspectual linkers are excluded by lookahead so genuine disclaimers stay
    # clean: 'without being/having <adj>' and 'no longer/more <adj>'. The single-
    # word + \Z anchoring keeps multi-token honest forms ('without claiming to be
    # exhaustive', 'no single source is exhaustive') on their existing paths.
    + r"|\bnever\s+not\s+" + _DEGREE_ADVERBS
    + r"|\bwithout\s+(?!being\b|having\b)[\w'’-]+\s+" + _DEGREE_ADVERBS
    + r"|\bno\s+(?!longer\b|more\b)[\w'’-]+\s+" + _DEGREE_ADVERBS
    + r"|(?:^|[.!?]\s+|\n)\s*"
    r"(?:is|are|was|were|do|does|did|can|could|should|would|has|have|had)"
    r"\b[^\n.!?]{0,80}\bnot\s+" + _DEGREE_ADVERBS
    + r")\Z"
)
_AFFIRMING_NEGATION_FLIP_PREFIX_RE = re.compile(
    _AFFIRMING_NEGATION_FLIP_PREFIX,
    re.IGNORECASE | re.VERBOSE,
)


def _split_sections(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", text))
    for i, m in enumerate(matches):
        name = m.group(1).strip().lower()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[name] = text[start:end].strip()
    return sections


def _is_negated_overclaim(text: str, start: int) -> bool:
    """True iff the completeness adjective at ``start`` is negated/qualified.

    A negation marker counts only when it reaches the adjective through the
    closed allow-set encoded in :data:`_NEGATED_OVERCLAIM_PREFIX_RE` (degree
    adverbs + ``or``/``nor`` distributed conjuncts). Any intervening content word
    — the hallmark of a new or subordinate clause — breaks the match, so a
    matrix-clause negation can never mask an embedded positive overclaim. The
    pattern is anchored at the end of the prefix (``\\Z`` == just before the
    adjective), so ``.search`` accepts only a marker whose entire gap to the
    adjective is allow-tokens.
    """
    prefix = text[max(0, start - _NEGATION_WINDOW_CHARS):start]
    if not _NEGATED_OVERCLAIM_PREFIX_RE.search(prefix):
        return False
    return not bool(_AFFIRMING_NEGATION_FLIP_PREFIX_RE.search(prefix))


def _overclaim_matches(text: str) -> list[str]:
    overclaims: list[str] = []
    for pat in _OVERCLAIM_RES:
        for m in pat.finditer(text):
            if not _is_negated_overclaim(text, m.start()):
                overclaims.append(m.group(0))
    return overclaims


def check_report(path: Path | str) -> tuple[bool, list[str]]:
    """Return ``(ok, problems)`` for the report at ``path``.

    Fail-CLOSED file layer (runs BEFORE any content check; defense-in-depth):

      * a SYMLINK is refused without being followed — even when its target is a
        report that would otherwise pass (CWE-59: a same-user race must not be
        able to redirect the gate to attacker-staged content);
      * a MISSING path or a NON-regular file (directory, FIFO, ...) is refused;
      * a readable regular file that the no-follow reader still rejects (e.g. a
        HARD-linked target, ``st_nlink > 1``) is refused via ``OSError``.

    Each of these short-circuits to ``(False, [<one path-scoped problem>])``
    before the floors run. An EMPTY regular file is NOT a file-layer rejection:
    it is read successfully and then fails on the content floors. A readable
    regular file is gated on the three relaxed floors (citations, honest gaps,
    no completeness lie). ``problems`` is empty iff ``ok`` is ``True`` —
    ``check_report(_GOOD) == (True, [])``. (Contract pinned by the fail-closed
    file-handling tests in tests/unit/test_research_adapter_synthesize.py.)
    """
    doc = Path(path)
    if not doc.is_file() or doc.is_symlink():
        return False, [f"{doc} missing or symlink"]
    try:
        from peers.safe_io import read_bytes_no_symlink

        raw = read_bytes_no_symlink(doc, max_bytes=_DOC_MAX_BYTES)
    except OSError as e:
        return False, [f"cannot read {doc}: {e}"]
    text = raw.decode("utf-8", errors="ignore")

    problems: list[str] = []

    citations = set(_URL_RE.findall(text))
    if len(citations) < MIN_CITATIONS:
        problems.append(
            f"only {len(citations)} distinct primary-source URL(s) "
            f"(need >= {MIN_CITATIONS})"
        )

    sections = _split_sections(text)
    gaps_body = None
    for heading, body in sections.items():
        if any(heading == h or heading.startswith(h) for h in _GAPS_HEADINGS):
            gaps_body = body
            break
    if gaps_body is None:
        problems.append(
            "missing `## Gaps` section — the report must state what it did NOT "
            "cover / verify"
        )
    elif len(gaps_body) < MIN_GAPS_CHARS:
        problems.append(
            f"`## Gaps` body {len(gaps_body)} chars (< {MIN_GAPS_CHARS}) — "
            "looks vacuous"
        )

    overclaims = _overclaim_matches(text)
    if overclaims:
        problems.append(
            f"bare completeness claim(s) {sorted(set(overclaims))} — research "
            "is never complete; remove or qualify against `## Gaps`"
        )

    return (not problems), problems


def main(root: str = ".") -> int:
    """CLI shim: gate ``<root>/RESEARCH.md`` with the single-line FAIL idiom."""
    doc = Path(root) / DOC_NAME
    ok, problems = check_report(doc)
    if not ok:
        print(f"report-cited FAIL: {doc} honesty contract:")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"report-cited: clean ({doc})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
