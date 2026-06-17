"""STEP-7 — the generic cited-report gate + the thin ReportSynthesizer adapter.

The security frame is lifted OUT: ``report_cited`` keeps the citation floor
(``MIN_CITATIONS = 2``),
the ``## Gaps`` requirement (``MIN_GAPS_CHARS = 40``), and the completeness-claim
ban, but DROPS the ATT&CK/CAPEC/CWE ``_ANCHOR_RE`` so a generic, non-security
topic clears the bar. ``ReportSynthesizer`` is the SOLE writer of the report
file: it renders via the injected ``write_report`` renderer, writes to
``repo/RESEARCH.md``, runs the gate, and returns a :class:`ReportArtifact` whose
``content_hash`` re-hashes from that file — or ``None`` on a failing report (a
dry round upstream), never a crash.
"""
import hashlib
import os

from peers.research.adapters import ReportSynthesizer
from peers.research.checks.report_cited import check_report
from peers.research.ports import Claim, Witness


def _confirmed(cid, origin):
    return Claim(id=cid, text="t", status="confirmed",
                 witnesses=[Witness(kind="fetched-source", uri=f"https://{origin}/p",
                                    content_hash="h", resolved_origin=origin)],
                 load_bearing=True)


# Two DISTINCT cited URLs on SEPARATE tokens (no trailing-comma capture) and a
# `## Gaps` body >= 40 chars — matches the generic floors MIN_CITATIONS=2, MIN_GAPS_CHARS=40.
_GOOD = ("# Report\n\n"
         "asparagus roots from cuttings [C1] (https://a.example/x).\n"
         "corroborated independently [C1] (https://b.example/y).\n\n"
         "## Gaps\n"
         "Minimum soil temperature was single-sourced and is not yet confirmed here.\n")
_NO_GAPS = "# Report\n\nclaim (https://a.example/x).\n"
_OVERCLAIM = ("# Report\n\nThis is a 100% complete, exhaustive survey (https://a.example/x).\n\n"
              "## Gaps\nThis section is long enough to clear the forty-character gaps floor here.\n")


def test_report_cited_gate_passes_generic_no_framework(tmp_path):
    p = tmp_path / "RESEARCH.md"
    p.write_text(_GOOD)
    ok, problems = check_report(p)
    assert ok is True and problems == []        # NO ATT&CK/CAPEC/CWE anchor required


def test_report_cited_gate_rejects_missing_gaps_and_overclaim(tmp_path):
    p1 = tmp_path / "a.md"
    p1.write_text(_NO_GAPS)
    p2 = tmp_path / "b.md"
    p2.write_text(_OVERCLAIM)
    assert check_report(p1)[0] is False
    assert check_report(p2)[0] is False


def test_report_cited_allows_negated_completeness_disclaimer_bug_724(tmp_path):
    body = ("# Report\n\n"
            "claim (https://a.example/x).\n"
            "corroborated independently (https://b.example/y).\n\n"
            "## Gaps\n"
            "This review was not exhaustive and is not 100% complete; several "
            "ecosystem areas remain unverified here.\n")
    ok, problems = check_report(_write(tmp_path / "negated.md", body))
    assert ok is True
    assert problems == []


def test_report_cited_rejects_unqualified_overclaim_after_negated_one_bug_724(tmp_path):
    body = ("# Report\n\n"
            "claim (https://a.example/x).\n"
            "corroborated independently (https://b.example/y).\n\n"
            "## Gaps\n"
            "This review was not exhaustive and leaves several source families "
            "unverified.\n\n"
            "## Conclusion\n"
            "The final result is an exhaustive survey.\n")
    ok, problems = check_report(_write(tmp_path / "mixed.md", body))
    assert ok is False
    assert any("bare completeness claim" in pr for pr in problems)


def test_report_cited_rejects_unrelated_negation_before_overclaim_bug_729(tmp_path):
    body = ("# Report\n\n"
            "claim (https://a.example/x).\n"
            "corroborated independently (https://b.example/y).\n\n"
            "## Gaps\n"
            "The raw dataset was not lost and remains exhaustive, despite the "
            "known limits this section still records.\n")
    ok, problems = check_report(_write(tmp_path / "unrelated-negation.md", body))
    assert ok is False
    assert any("bare completeness claim" in pr for pr in problems)


def test_report_cited_allows_distributed_negation_over_coordination_bug_730(tmp_path):
    # happy: one 'not' scopes over a coordinated adjective run. "not complete or
    # exhaustive" means NOT exhaustive (De Morgan), so it is an honest disclaimer
    # and MUST clear the gate. The BUG-729 fix truncated at 'or' and wrongly
    # flagged it; the conjunction here introduces no new predicate, so the
    # negation distributes across it.
    body = ("# Report\n\n"
            "claim (https://a.example/x).\n"
            "corroborated independently (https://b.example/y).\n\n"
            "## Gaps\n"
            "This catalogue is not complete or exhaustive; several source "
            "families remain unverified here.\n")
    ok, problems = check_report(_write(tmp_path / "distributed.md", body))
    assert ok is True
    assert problems == []


def test_report_cited_distributed_negation_covers_percent_overclaim_edge_bug_730(tmp_path):
    # edge: distribution must also reach the '100% complete' overclaim form, and
    # survive an intervening adjective ('broad') before the conjunction.
    body = ("# Report\n\n"
            "claim (https://a.example/x).\n"
            "corroborated independently (https://b.example/y).\n\n"
            "## Gaps\n"
            "Coverage is not broad or 100% complete; major gaps persist in this "
            "draft and are recorded here.\n")
    ok, problems = check_report(_write(tmp_path / "percent.md", body))
    assert ok is True
    assert problems == []


def test_report_cited_new_clause_after_conjunction_still_flagged_bug_730(tmp_path):
    # sad: a coordinating conjunction that DOES introduce a new predicate
    # ('and remains exhaustive') must STILL be flagged — the BUG-730 fix must not
    # reopen the BUG-729 false-negative. The negation 'not lost' binds only the
    # first clause; the second clause positively asserts 'exhaustive'.
    body = ("# Report\n\n"
            "claim (https://a.example/x).\n"
            "corroborated independently (https://b.example/y).\n\n"
            "## Gaps\n"
            "The dataset was not lost and remains exhaustive across every "
            "source family here.\n")
    ok, problems = check_report(_write(tmp_path / "new-clause.md", body))
    assert ok is False
    assert any("bare completeness claim" in pr for pr in problems)


def test_report_cited_allows_modified_distributed_negation_bug_731(tmp_path):
    # happy: the adverb after the conjunction modifies the same negated
    # adjective phrase; "not complete or fully exhaustive" still means NOT
    # exhaustive and should not be treated as a new predicate.
    body = ("# Report\n\n"
            "claim (https://a.example/x).\n"
            "corroborated independently (https://b.example/y).\n\n"
            "## Gaps\n"
            "This catalogue is not complete or fully exhaustive; several source "
            "families remain unverified here.\n")
    ok, problems = check_report(_write(tmp_path / "modified-distributed.md", body))
    assert ok is True
    assert problems == []


def test_report_cited_allows_negation_adverbs_that_look_like_conjunctions_bug_731(tmp_path):
    # edge: "yet" and "so" can be adverbs attached to the negation rather than
    # clause boundaries. These common disclaimers must remain accepted.
    for phrase in ("not yet exhaustive", "not so exhaustive"):
        body = ("# Report\n\n"
                "claim (https://a.example/x).\n"
                "corroborated independently (https://b.example/y).\n\n"
                "## Gaps\n"
                f"This catalogue is {phrase}; several source families remain "
                "unverified here.\n")
        ok, problems = check_report(_write(tmp_path / f"{phrase}.md", body))
        assert ok is True
        assert problems == []


def test_report_cited_rejects_contrastive_positive_overclaim_bug_731(tmp_path):
    # sad: "but exhaustive" is a positive contrastive claim, not a distributed
    # negation. It must be flagged even when the previous adjective is negated.
    body = ("# Report\n\n"
            "claim (https://a.example/x).\n"
            "corroborated independently (https://b.example/y).\n\n"
            "## Gaps\n"
            "This catalogue is not complete but exhaustive across every source "
            "family reviewed here.\n")
    ok, problems = check_report(_write(tmp_path / "contrastive.md", body))
    assert ok is False
    assert any("bare completeness claim" in pr for pr in problems)


def test_report_cited_allows_negation_adverb_with_modifier_before_claim_bug_732(tmp_path):
    # edge: "not yet <modifier> exhaustive" — "yet" is a negation adverb bound to
    # "not", so it should NOT reset scope just because an adverb ("fully",
    # "quite", "comprehensively") sits between "yet" and "exhaustive". The
    # BUG-731 fix only preserved scope when "yet"/"so" DIRECTLY abutted the
    # overclaim (its `not tail and ...` guard), so these honest disclaimers were
    # still flagged. All three mean NOT exhaustive and must pass.
    for phrase in (
        "not yet fully exhaustive",
        "not yet quite exhaustive",
        "not yet comprehensively exhaustive",
    ):
        body = ("# Report\n\n"
                "claim (https://a.example/x).\n"
                "corroborated independently (https://b.example/y).\n\n"
                "## Gaps\n"
                f"This catalogue is {phrase}; several source families remain "
                "unverified here.\n")
        ok, problems = check_report(_write(tmp_path / "neg-adverb.md", body))
        assert ok is True, phrase
        assert problems == [], phrase


def test_report_cited_allows_neither_nor_distributed_negation_bug_732(tmp_path):
    # edge: "neither complete nor exhaustive" is a distributed negation
    # (== NOT exhaustive), the direct parallel of the BUG-730 honest disclaimer
    # "not complete or exhaustive". "neither" was missing from the negation-
    # marker set, so this honest phrasing was flagged as a bare completeness lie.
    body = ("# Report\n\n"
            "claim (https://a.example/x).\n"
            "corroborated independently (https://b.example/y).\n\n"
            "## Gaps\n"
            "This catalogue is neither complete nor exhaustive; several source "
            "families remain unverified here.\n")
    ok, problems = check_report(_write(tmp_path / "neither-nor.md", body))
    assert ok is True
    assert problems == []


def test_report_cited_rejects_contrastive_yet_without_negation_bug_732(tmp_path):
    # sad: "complete yet exhaustive" — with NO negation marker before "yet", the
    # word is contrastive ("but"), so "exhaustive" is a positive overclaim and
    # must still be flagged. Guards the BUG-732 relaxation against reopening a
    # false negative.
    body = ("# Report\n\n"
            "claim (https://a.example/x).\n"
            "corroborated independently (https://b.example/y).\n\n"
            "## Gaps\n"
            "This catalogue is complete yet exhaustive across every source "
            "family reviewed here.\n")
    ok, problems = check_report(_write(tmp_path / "contrastive-yet.md", body))
    assert ok is False
    assert any("bare completeness claim" in pr for pr in problems)


def test_report_cited_rejects_intensifier_so_overclaim_bug_732(tmp_path):
    # sad: "so exhaustive" as an intensifier (no preceding negation) is a
    # positive overclaim and must be flagged.
    body = ("# Report\n\n"
            "claim (https://a.example/x).\n"
            "corroborated independently (https://b.example/y).\n\n"
            "## Gaps\n"
            "This report is so exhaustive that nothing further remains to be "
            "examined here.\n")
    ok, problems = check_report(_write(tmp_path / "intensifier-so.md", body))
    assert ok is False
    assert any("bare completeness claim" in pr for pr in problems)


def test_report_cited_unrecognized_negation_idioms_flagged_failsafe_bug_732(tmp_path):
    # The gate recognises a BOUNDED set of negation/qualification patterns; any
    # unrecognised idiom is CONSERVATIVELY flagged. This is fail-safe BY DESIGN —
    # the gate never passes a dishonest completeness claim, and an over-flagged
    # honest report is fixed by rephrasing or moving the disclaimer into ## Gaps.
    # This test pins the documented contract so residual exotic-grammar over-
    # flags stay info/low, not blocking defects. (If a future change recognises
    # one of these, update the assertion — that is a deliberate scope decision.)
    for phrase in ("hardly exhaustive", "anything but exhaustive"):
        body = ("# Report\n\n"
                "claim (https://a.example/x).\n"
                "corroborated independently (https://b.example/y).\n\n"
                "## Gaps\n"
                f"This catalogue is {phrase}; coverage of several families "
                "remains open here.\n")
        ok, problems = check_report(_write(tmp_path / "failsafe.md", body))
        assert ok is False, phrase
        assert any("bare completeness claim" in pr for pr in problems), phrase


def test_report_cited_rejects_overclaim_in_subordinate_clause_bug_733(tmp_path):
    # sad / FALSE-NEGATIVE guard: a completeness claim embedded in a subordinate
    # clause is a DISHONEST overclaim and must be flagged. The bug: the
    # subordinating conjunction (until/since/...) lands inside the negation
    # window, so `not` (governing the matrix verb "stop"/"idled") wrongly scopes
    # over "exhaustive" in the new clause and the claim slipped through. A
    # subordinating conjunction must reset negation scope just like a
    # coordinating one that starts a new predicate.
    for fname, claimful in (
        ("until.md", "We did not stop until the scan was exhaustive across every "
                     "source family here."),
        ("since.md", "We have not idled since the survey became exhaustive over "
                     "the reviewed corpus here."),
        ("after.md", "We did not publish after the review became exhaustive over "
                     "the reviewed corpus here."),
        ("after-elided.md", "We did not publish after exhaustive review covered "
                            "every source family here."),
        ("before.md", "We did not publish before the review became exhaustive "
                      "over the reviewed corpus here."),
        ("when.md", "We did not publish when the review became exhaustive over "
                    "the reviewed corpus here."),
        ("once.md", "We did not ship once the catalogue was exhaustive across "
                    "the reviewed corpus here."),
        ("as.md", "We did not publish as the review became exhaustive over the "
                  "reviewed corpus here."),
    ):
        body = ("# Report\n\n"
                "claim (https://a.example/x).\n"
                "corroborated independently (https://b.example/y).\n\n"
                "## Gaps\n"
                f"{claimful}\n")
        ok, problems = check_report(_write(tmp_path / fname, body))
        assert ok is False, fname
        assert any("bare completeness claim" in pr for pr in problems), fname


def test_report_cited_allows_honest_disclaimer_with_subordinator_bug_733(tmp_path):
    # happy + edge: the BUG-733 fix adds subordinating conjunctions as scope
    # resets, but it must NOT start over-flagging HONEST disclaimers that merely
    # contain such a word. Two placements:
    #   - subordinator AFTER the negated overclaim ("not exhaustive although ...")
    #     — the subordinator is outside the negation prefix, so it is irrelevant;
    #   - subordinator BEFORE the negation ("Since ... this review is not
    #     exhaustive") — the reset lands before `not`, so the negation still
    #     governs "exhaustive". Both mean NOT exhaustive and must pass.
    for fname, disclaimer in (
        ("subord-after.md",
         "This review is not exhaustive although several promising leads remain "
         "open for future work here."),
        ("subord-before.md",
         "Since source access was limited this review is not exhaustive over the "
         "families surveyed here."),
        ("comparative-as.md",
         "This review is not as exhaustive as a full source-code audit; several "
         "families remain unverified here."),
        ("not-once.md",
         "This review was not once exhaustive across the changing corpus, and "
         "several families remain unverified here."),
    ):
        body = ("# Report\n\n"
                "claim (https://a.example/x).\n"
                "corroborated independently (https://b.example/y).\n\n"
                "## Gaps\n"
                f"{disclaimer}\n")
        ok, problems = check_report(_write(tmp_path / fname, body))
        assert ok is True, (fname, problems)
        assert problems == [], fname


def test_report_cited_matrix_negation_never_masks_embedded_overclaim_bug_735(tmp_path):
    # sad / FALSE-NEGATIVE CLASS CLOSURE: a matrix-clause negation must
    # NEVER reach across into an embedded POSITIVE completeness claim. BUG-729..
    # 734 each patched ONE clause boundary into a blocklist, but the underlying
    # arbitrary-word window `(?:[\w'-]+\s+){0,5}` still let `not` leap a <=5-word
    # gap. These all positively ASSERT exhaustiveness and must be flagged. The
    # first two carry NO subordinating conjunction at all (proving the boundary-
    # enumeration approach could never close the class); the rest use connectives
    # codex never enumerated (whenever/wherever). All have a <=5-word gap, so the
    # pre-fix matcher wrongly reported them clean.
    for fname, claimful in (
        ("rel-that.md",
         "We did not flag the scan that was exhaustive over the corpus here."),
        ("matrix-confirm.md",
         "We did not confirm the dataset stays exhaustive over the corpus here."),
        ("matrix-prove.md",
         "We did not prove the corpus is exhaustive across every family here."),
        ("matrix-say.md",
         "We did not say the audit was exhaustive over the corpus here."),
        ("subord-whenever.md",
         "We did not ship whenever the catalogue was exhaustive over the corpus here."),
        ("subord-wherever.md",
         "We did not pause wherever surveys stayed exhaustive over the corpus here."),
    ):
        body = ("# Report\n\n"
                "claim (https://a.example/x).\n"
                "corroborated independently (https://b.example/y).\n\n"
                "## Gaps\n"
                f"{claimful}\n")
        ok, problems = check_report(_write(tmp_path / fname, body))
        assert ok is False, fname
        assert any("bare completeness claim" in pr for pr in problems), fname


def test_report_cited_not_only_completeness_is_a_positive_overclaim_bug_735(tmp_path):
    # sad: "not ONLY exhaustive (but ...)" ASSERTS exhaustiveness — the focus
    # particle flips polarity. The allowlist matcher must keep flagging it (the
    # `(?!only|just|merely)` guard survives the redesign), or a dishonest
    # overclaim wrapped in "not only" would slip through.
    for phrase in ("not only exhaustive but fast",
                   "not just exhaustive but quick",
                   "not merely exhaustive but thorough"):
        body = ("# Report\n\n"
                "claim (https://a.example/x).\n"
                "corroborated independently (https://b.example/y).\n\n"
                "## Gaps\n"
                f"This catalogue is {phrase} across every family reviewed here.\n")
        ok, problems = check_report(_write(tmp_path / "not-only.md", body))
        assert ok is False, phrase
        assert any("bare completeness claim" in pr for pr in problems), phrase


def test_report_cited_extended_degree_adverbs_stay_honest_bug_735(tmp_path):
    # happy / edge: the redesign widens the recognised degree/comparative set so
    # common honest disclaimers are NOT over-flagged. Each means NOT exhaustive
    # and must pass. ("been" covers the perfect-aspect copula "has not been".)
    for phrase in ("not entirely exhaustive",
                   "not remotely exhaustive",
                   "not nearly exhaustive",
                   "not truly exhaustive",
                   "not yet entirely exhaustive"):
        body = ("# Report\n\n"
                "claim (https://a.example/x).\n"
                "corroborated independently (https://b.example/y).\n\n"
                "## Gaps\n"
                f"This catalogue is {phrase}; several families remain unverified here.\n")
        ok, problems = check_report(_write(tmp_path / "degree.md", body))
        assert ok is True, (phrase, problems)
        assert problems == [], phrase
    # perfect-aspect copula placement: "has not been exhaustive" == NOT exhaustive
    body = ("# Report\n\n"
            "claim (https://a.example/x).\n"
            "corroborated independently (https://b.example/y).\n\n"
            "## Gaps\n"
            "This survey has not been exhaustive; several families remain open here.\n")
    ok, problems = check_report(_write(tmp_path / "been.md", body))
    assert ok is True, problems
    assert problems == []
    # control/raising verb + infinitival "to be" (no NEW subject): the matrix
    # negation legitimately scopes the adjective -- "does not claim to be
    # exhaustive" == NOT exhaustive. Must NOT be over-flagged just because a verb
    # sits in the gap (this is the structural contrast to "the scan that WAS
    # exhaustive", which introduces a new finite clause and stays flagged).
    for phrase in ("does not claim to be exhaustive",
                   "cannot purport to be 100% complete",
                   "does not appear to be fully exhaustive"):
        body = ("# Report\n\n"
                "claim (https://a.example/x).\n"
                "corroborated independently (https://b.example/y).\n\n"
                "## Gaps\n"
                f"This catalogue {phrase}; several families remain unverified here.\n")
        ok, problems = check_report(_write(tmp_path / "to-be.md", body))
        assert ok is True, (phrase, problems)
        assert problems == [], phrase


def test_report_cited_single_copular_verb_negation_stays_honest_bug_735(tmp_path):
    # happy: a SINGLE copular/perception verb in the gap is the same predicate
    # under the negation — "is not considered exhaustive", "does not seem
    # exhaustive" all mean NOT exhaustive. The allowlist permits at most one such
    # word (false-negative-safe: a positive embedded clause needs subject+verb,
    # i.e. >=2 tokens), restoring honest forms the prior window accepted.
    for phrase in ("is not considered exhaustive",
                   "is not deemed exhaustive",
                   "does not seem exhaustive",
                   "does not appear exhaustive",
                   "did not prove exhaustive",
                   "is not considered fully exhaustive"):
        body = ("# Report\n\n"
                "claim (https://a.example/x).\n"
                "corroborated independently (https://b.example/y).\n\n"
                "## Gaps\n"
                f"This survey {phrase}; several families remain unverified here.\n")
        ok, problems = check_report(_write(tmp_path / "copular.md", body))
        assert ok is True, (phrase, problems)
        assert problems == [], phrase


def test_report_cited_two_word_gap_still_flagged_bug_735(tmp_path):
    # sad / boundary: the one-word relaxation must NOT regrow into the multi-word
    # leak. A TWO-word gap can already carry a fresh assertion (a subject+verb or
    # a connective+attributive), so it must stay FLAGGED — pins the <=1-token
    # ceiling that keeps the false-negative class closed.
    for fname, claimful in (
        ("deem2.md", "We did not deem the corpus exhaustive over the reviewed set here."),
        ("after2.md", "We did not rest after exhaustive scanning finished the corpus here."),
    ):
        body = ("# Report\n\n"
                "claim (https://a.example/x).\n"
                "corroborated independently (https://b.example/y).\n\n"
                "## Gaps\n"
                f"{claimful}\n")
        ok, problems = check_report(_write(tmp_path / fname, body))
        assert ok is False, fname
        assert any("bare completeness claim" in pr for pr in problems), fname


def test_report_cited_affirmation_idiom_overclaim_is_flagged_bug_736(tmp_path):
    # sad / FALSE-NEGATIVE CLASS: 'without'/'no'/'never' are negation
    # MARKERS, but in the fixed EMPHATIC-AFFIRMATION idioms "without
    # question/doubt/fail/exception" and "no doubt/question" they are POSITIVE
    # intensifiers (= "undoubtedly"). The single affirmation noun fills the
    # _ONE_LINKING_WORD slot, the allowlist gap closes onto the adjective, and the
    # pre-fix matcher reports these UNQUALIFIED overclaims clean. All must flag.
    for fname, claimful in (
        ("affirm-question.md",
         "The catalogue is without question exhaustive over every family here."),
        ("affirm-doubt.md",
         "This survey is without doubt exhaustive across the whole corpus here."),
        ("affirm-no-doubt.md",
         "This catalogue is no doubt exhaustive over every source reviewed here."),
        ("affirm-fail.md",
         "The scan is without fail exhaustive across the entire corpus here."),
        ("affirm-exception.md",
         "Coverage is without exception exhaustive over every family here."),
        ("affirm-100.md",
         "Our coverage is without question 100% complete across the set here."),
        ("affirm-comprehensive.md",
         "The catalogue is without doubt fully comprehensive over the set here."),
        ("affirm-allknown.md",
         "Without question all known vulnerabilities are listed in this report here."),
    ):
        body = ("# Report\n\n"
                "claim (https://a.example/x).\n"
                "corroborated independently (https://b.example/y).\n\n"
                "## Gaps\n"
                f"{claimful}\n")
        ok, problems = check_report(_write(tmp_path / fname, body))
        assert ok is False, fname
        assert any("bare completeness claim" in pr for pr in problems), fname


def test_report_cited_affirmation_litotes_overclaim_is_flagged_bug_736(tmp_path):
    # sad / litotes affirmation: "never fails to be" / "does not fail
    # to be" negate FAILURE, i.e. positively ASSERT the adjective. They leak via
    # the _RAISING_TO_BE "to be" path ("fails" is consumed before "to be"), so the
    # inflection-tolerant affirmation guard must cover "fails"/"fail" too.
    for fname, claimful in (
        ("litotes-never.md",
         "The scanner never fails to be exhaustive across the corpus here."),
        ("litotes-doesnot.md",
         "It does not fail to be exhaustive over every family reviewed here."),
    ):
        body = ("# Report\n\n"
                "claim (https://a.example/x).\n"
                "corroborated independently (https://b.example/y).\n\n"
                "## Gaps\n"
                f"{claimful}\n")
        ok, problems = check_report(_write(tmp_path / fname, body))
        assert ok is False, fname
        assert any("bare completeness claim" in pr for pr in problems), fname


def test_report_cited_negation_token_in_honest_disclaimer_stays_clean_bug_736(tmp_path):
    # happy / edge: the BUG-736 guard is SURGICAL — it rejects only the closed
    # affirmation-lexeme set immediately after a marker, so ordinary honest
    # disclaimers that merely START with a negation token stay accepted. "no
    # longer exhaustive" (was, is not now), "far from exhaustive", and
    # "non-exhaustive" all mean NOT exhaustive and must NOT be over-flagged.
    for phrase in ("no longer exhaustive",
                   "far from exhaustive",
                   "non-exhaustive",
                   "not entirely exhaustive",
                   "not at all exhaustive"):
        body = ("# Report\n\n"
                "claim (https://a.example/x).\n"
                "corroborated independently (https://b.example/y).\n\n"
                "## Gaps\n"
                f"This catalogue is {phrase}; several families remain open here.\n")
        ok, problems = check_report(_write(tmp_path / "honest.md", body))
        assert ok is True, (phrase, problems)
        assert problems == [], phrase


def test_report_cited_rejects_negation_flip_overclaims_bug_738(tmp_path):
    # sad: each phrase contains a negation token, but the full idiom positively
    # asserts exhaustiveness. The report-level gate must reject them even when
    # citations and the Gaps floor are otherwise satisfied.
    for fname, claimful in (
        ("nothing-if-not.md",
         "This review is nothing if not exhaustive."),
        ("no-means-non.md",
         "It is by no means non-exhaustive."),
        ("rhetorical-question.md",
         "Is this catalogue not exhaustive?"),
    ):
        body = ("# Report\n\n"
                "claim (https://a.example/x).\n"
                "corroborated independently (https://b.example/y).\n\n"
                "## Gaps\n"
                f"{claimful} Several source families remain open here.\n")
        ok, problems = check_report(_write(tmp_path / fname, body))
        assert ok is False, fname
        assert any("bare completeness claim" in pr for pr in problems), fname


def test_report_cited_rejects_litotes_negation_flip_overclaims_bug_739(tmp_path):
    # sad: these sibling double-negative litotes forms also positively assert
    # exhaustiveness. Before BUG-739 they reached the inner "non-" marker and
    # let a cited report with a real Gaps section pass as honest.
    for fname, claimful in (
        ("in-no-way-non.md", "It is in no way non-exhaustive."),
        ("by-no-stretch-non.md", "It is by no stretch non-exhaustive."),
        ("not-at-all-non.md", "The list is not at all non-exhaustive."),
        ("in-no-sense-non.md", "It is in no sense non-exhaustive."),
        ("not-non.md", "It is not non-exhaustive."),
    ):
        body = ("# Report\n\n"
                "claim (https://a.example/x).\n"
                "corroborated independently (https://b.example/y).\n\n"
                "## Gaps\n"
                f"{claimful} Several source families remain open here.\n")
        ok, problems = check_report(_write(tmp_path / fname, body))
        assert ok is False, fname
        assert any("bare completeness claim" in pr for pr in problems), fname


def test_adapter_writes_and_hashes_a_good_report(tmp_path):
    synth = ReportSynthesizer(write_report=lambda claims, gaps: _GOOD)
    art = synth.synthesize([_confirmed("C1", "a.example")], gaps=[], repo=tmp_path)
    assert art is not None
    on_disk = (tmp_path / "RESEARCH.md").read_bytes()
    assert art.content_hash == hashlib.sha256(on_disk).hexdigest()


def test_adapter_rejects_uncited_report(tmp_path):
    synth = ReportSynthesizer(write_report=lambda claims, gaps: _NO_GAPS)
    assert synth.synthesize([_confirmed("C1", "a.example")], gaps=[], repo=tmp_path) is None


def test_adapter_confirmed_ids_survive_a_generator_of_claims(tmp_path):
    # The Synthesizer Protocol does not promise `claims` is a re-iterable list.
    # A one-shot generator must NOT be exhausted by the render step and leave
    # confirmed_ids empty — the confirmed-work subject derives from confirmed_ids.
    synth = ReportSynthesizer(write_report=lambda claims, gaps: _GOOD)
    claims_gen = (c for c in [_confirmed("C1", "a.example"), _confirmed("C2", "b.example")])
    art = synth.synthesize(claims_gen, gaps=[], repo=tmp_path)
    assert art is not None
    assert art.confirmed_ids == ["C1", "C2"]


# --- fail-CLOSED: a hostile/garbage renderer is a dry round, NEVER a crash ---
# The renderer is the untrusted LLM boundary; the docstring promises "never a
# crash". A non-encodable body (a lone surrogate — exactly what
# bytes.decode('utf-8','surrogateescape') yields for arbitrary LLM/HTTP bytes),
# a non-str body, or a raising renderer must degrade to None (dry round
# upstream), not propagate out of drive() and crash the whole run.

def test_adapter_lone_surrogate_render_returns_none_not_crash(tmp_path):
    synth = ReportSynthesizer(write_report=lambda claims, gaps: "# R\né\udce9\n## Gaps\nx" * 5)
    assert synth.synthesize([_confirmed("C1", "a.example")], gaps=[], repo=tmp_path) is None


def test_adapter_non_str_render_returns_none_not_crash(tmp_path):
    synth = ReportSynthesizer(write_report=lambda claims, gaps: {"not": "a str"})
    assert synth.synthesize([_confirmed("C1", "a.example")], gaps=[], repo=tmp_path) is None


def test_adapter_raising_render_returns_none_not_crash(tmp_path):
    def _boom(claims, gaps):
        raise RuntimeError("LLM 500")
    synth = ReportSynthesizer(write_report=_boom)
    assert synth.synthesize([_confirmed("C1", "a.example")], gaps=[], repo=tmp_path) is None


# --- EDGE: citation/gaps boundaries + the empty-claims degenerate input ----
# The gate counts DISTINCT urls (set-dedup), so the same source repeated cannot
# pad the >=2 floor — an edge a happy "two different urls" case never probes.

def test_report_cited_duplicate_urls_count_once_edge(tmp_path):
    # one source cited TWICE is one distinct witness, not two -> below MIN_CITATIONS.
    dup = ("# R\n\nclaim (https://a.example/x).\nrestated (https://a.example/x).\n\n"
           "## Gaps\nMinimum soil temperature was single-sourced and not confirmed.\n")
    p = tmp_path / "dup.md"
    p.write_text(dup)
    ok, problems = check_report(p)
    assert ok is False
    assert any("distinct primary-source URL" in pr for pr in problems)


def test_report_cited_gaps_body_length_boundary_edge(tmp_path):
    # off-by-one on MIN_GAPS_CHARS=40: a 40-char gaps body clears, 39 is vacuous.
    head = "# R\n\nclaim (https://a.example/x) and (https://b.example/y).\n\n## Gaps\n"
    ok40, problems40 = check_report(_write(tmp_path / "g40.md", head + "x" * 40 + "\n"))
    ok39, problems39 = check_report(_write(tmp_path / "g39.md", head + "x" * 39 + "\n"))
    assert ok40 is True and problems40 == []
    assert ok39 is False and any("vacuous" in pr for pr in problems39)


def test_adapter_empty_claims_still_writes_when_body_good_edge(tmp_path):
    # degenerate input: zero claims. A good body still gates clean and the
    # artifact carries an EMPTY confirmed_ids list (not a crash, not None).
    synth = ReportSynthesizer(write_report=lambda claims, gaps: _GOOD)
    art = synth.synthesize([], gaps=[], repo=tmp_path)
    assert art is not None
    assert art.confirmed_ids == []


def _write(path, text):
    path.write_text(text)
    return path


# --- FAIL-CLOSED file handling: the security layer BEFORE any content check ---
# check_report()'s docstring promises a missing / non-regular / symlinked /
# unreadable RESEARCH.md returns ``(False, [...])`` — it must never follow a
# symlink (CWE-59), crash on a missing file, or read a hard-linked target. The
# content tests above all feed it a plain regular file, so this whole branch
# (report_cited.py: the is_file()/is_symlink() guard + the read OSError handler)
# was uncharacterised. These pin that defense-in-depth contract: happy (a real
# regular file is accepted by the file layer), edge (an empty regular file is
# READ then judged on content; a directory is refused), sad (missing, symlink-to-
# valid-target, and hard-linked paths all fail CLOSED). The matcher is untouched.

# Two distinct cited URLs + a >=40-char `## Gaps` body: content that PASSES, so a
# fail-closed verdict on a path holding it isolates the file layer from content.
_FILE_LAYER_GOOD = (
    "# Report\n\nclaim a (https://a.example/x).\ncorrob (https://b.example/y).\n\n"
    "## Gaps\nMinimum soil temperature was single-sourced and is not yet confirmed.\n"
)
# A file-layer rejection short-circuits to a SINGLE path-scoped problem before any
# content evaluation; a content failure yields URL/Gaps problems instead.
_CONTENT_TOKENS = ("URL", "Gaps", "vacuous", "completeness")


def test_report_cited_accepts_a_plain_regular_file_happy(tmp_path):
    # happy: a legitimate regular, non-symlink file passes the file layer and is
    # judged purely on content (here: clean). Guards against the fail-closed
    # checks false-positiving on an honest report.
    ok, problems = check_report(_write(tmp_path / "RESEARCH.md", _FILE_LAYER_GOOD))
    assert ok is True and problems == []


def test_report_cited_missing_file_fails_closed_sad(tmp_path):
    # sad: a path that does not exist must fail CLOSED (not raise) with one
    # file-level problem — never a green gate on an absent deliverable.
    p = tmp_path / "does-not-exist.md"
    ok, problems = check_report(p)
    assert ok is False
    assert len(problems) == 1 and str(p) in problems[0]


def test_report_cited_symlink_to_valid_target_fails_closed_cwe59_sad(tmp_path):
    # sad / CWE-59: a symlink is refused WITHOUT following it, even when its
    # target is a report that would otherwise pass. The real file passing while
    # the link fails proves the rejection is the symlink itself, not the content.
    real = _write(tmp_path / "real.md", _FILE_LAYER_GOOD)
    assert check_report(real) == (True, [])          # sanity: target is valid
    link = tmp_path / "link.md"
    os.symlink(real, link)
    ok, problems = check_report(link)
    assert ok is False
    assert len(problems) == 1 and str(link) in problems[0]


def test_report_cited_hard_linked_file_fails_closed_sad(tmp_path):
    # sad: a regular file with st_nlink > 1 is refused by the O_NOFOLLOW reader
    # (refusing to read hard-linked file), exercising the read-OSError branch.
    # Deterministic regardless of euid, unlike a chmod-000 permission test.
    src = _write(tmp_path / "src.md", _FILE_LAYER_GOOD)
    hard = tmp_path / "hard.md"
    os.link(src, hard)
    ok, problems = check_report(hard)
    assert ok is False
    assert len(problems) == 1 and "cannot read" in problems[0]


def test_report_cited_directory_path_fails_closed_edge(tmp_path):
    # edge: a directory is not a regular file, so the file layer refuses it with
    # a single path-scoped problem rather than trying to read it.
    d = tmp_path / "a-directory"
    d.mkdir()
    ok, problems = check_report(d)
    assert ok is False
    assert len(problems) == 1 and str(d) in problems[0]


def test_report_cited_empty_regular_file_is_read_then_content_judged_edge(tmp_path):
    # edge: a 0-byte regular file is NOT a file-layer rejection — it is READ
    # successfully and then fails on the content floors (no citations, no Gaps).
    # Distinguishes "could not read the file" from "read it, content is bad".
    ok, problems = check_report(_write(tmp_path / "empty.md", ""))
    assert ok is False
    assert any(tok in pr for pr in problems for tok in _CONTENT_TOKENS)
    assert not any("missing or symlink" in pr or "cannot read" in pr
                   for pr in problems)
