"""STEP-2 — ``ResearchFrontend``: the second ``ModeFrontend`` (after develop).

This module currently implements the seam (``__init__``) and ``prepare`` only;
``run`` / ``interpret`` land in later steps (STEP-3..STEP-6) so this file is an
honestly-partial frontend, not a stubbed one.

``prepare`` runs the generic intake, records the bar via ``direction.infer_bar``
(the ``ledger=`` kwarg is REQUIRED to emit the ``bar-inferred`` row), and stashes
the brief body into ``self._topic_text``. Unlike develop, an *absent* bar does
NOT block: research is a KNOWLEDGE mode that must work on a topic with no repo
and no test bar at all. What blocks a research round is a missing / vacuous
``TOPIC.md`` brief, recorded in ``self._blocked``.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

from peers.research.claim_ledger import (
    CONFIRMED,
    CONTESTED,
    SINGLE_SOURCE,
    UNVERIFIED_GAP,
    classify_claim,
    independent_origins,
)
from peers.research.intake import _DOC_MAX_BYTES, DOC_NAME, require_topic
from peers.research.ports import (
    Claim,
    Committer,
    CompletenessCritic,
    Decomposer,
    SweepResult,
    Sweeper,
    Synthesizer,
    Witness,
)
from peers.research.source_cache import SourceCache
from peers.safe_io import read_bytes_no_symlink
from peers.spine.adversarial_verify import verify_claim
from peers.spine.direction import Bar, infer_bar
from peers.spine.gates import resolves_to_commit
from peers.spine.mode_run import ModeRun


def _default_refuter_factory(claim: Claim) -> Callable[[int], bool]:
    """Fail-CLOSED verify seam: refute every vote, so a claim is corroborated
    only by an explicitly non-refuting factory. Mirrors develop's
    ``_default_refuter_factory``."""
    return lambda i: True


class ResearchFrontend:
    """A :class:`peers.spine.mode_run.ModeFrontend` for research mode.

    Capabilities are injected as runtime-checkable ports so the orchestration is
    deterministic in tests: ``decomposer`` (DECOMPOSE), ``sweeper`` (SWEEP),
    ``synthesizer`` (SYNTHESIZE), ``committer`` (commit the report), ``critic``
    (the completeness / stop-on-dry guard). ``modalities`` are the sweep lanes
    enabled this run; ``run_tests`` feeds ``direction.infer_bar`` in
    :meth:`prepare`; ``k`` is the verify vote count; ``refuter_factory(claim)``
    yields that claim's per-vote refuter (defaults to the fail-closed
    refute-everything factory).
    """

    def __init__(
        self,
        decomposer: Decomposer,
        sweeper: Sweeper,
        synthesizer: Synthesizer,
        committer: Committer | None,
        critic: CompletenessCritic,
        *,
        modalities: list[str],
        run_tests: Callable[[str], "tuple[int, str] | None"],
        k: int = 2,
        refuter_factory: Callable[[Claim], Callable[[int], bool]] | None = None,
    ) -> None:
        self.decomposer = decomposer
        self.sweeper = sweeper
        self.synthesizer = synthesizer
        self.committer = committer
        self.critic = critic
        self.modalities = modalities
        self.run_tests = run_tests
        self.k = k
        self.refuter_factory = refuter_factory or _default_refuter_factory
        self.bar: Bar | None = None
        self._blocked = False
        self._topic_ok = False
        self._topic_problems: list[str] = []
        self._topic_text = ""
        # Round-local accumulators (reset at the TOP of every run() round — see
        # the load-bearing per-round-reset invariant). Initialised here so an
        # interpret()/inspection before the first round never AttributeErrors.
        self._round_idx = 0
        self._cache: SourceCache | None = None
        self._modalities_run: list[str] = []
        self._round_claims: list[Claim] = []
        self._confirmed: list[Claim] = []
        self._gaps: list[Claim] = []

    # ---- ModeFrontend seam -------------------------------------------------
    def prepare(self, run: ModeRun) -> None:
        """Run the intake, record the bar (NEVER blocking on an absent one), and
        stash the brief body.

        A missing / vacuous ``TOPIC.md`` sets ``self._blocked = True`` (an honest
        dry run with no brief to decompose). ``infer_bar`` is still called — its
        ``bar-inferred`` row is part of the audit trail and surfaces a
        ``weak`` / ``present`` signal when a grounding repo IS present — but the
        bar kind does NOT gate research.
        """
        self._topic_ok, self._topic_problems = require_topic(run.tool)
        self.bar = infer_bar(
            run.tool, self.run_tests, ledger=run.ledger, mode_run=run.mode_run)
        self._blocked = not self._topic_ok
        if self._topic_ok:
            # Re-use the SAME no-symlink read require_topic uses so prepare and
            # the intake agree on the source of truth for the brief body.
            self._topic_text = read_bytes_no_symlink(
                run.tool / DOC_NAME, max_bytes=_DOC_MAX_BYTES,
            ).decode("utf-8", "ignore")
        else:
            self._topic_text = ""

    # ---- round orchestration ----------------------------------------------
    @staticmethod
    def _modality_yielded(modality: str, sr: SweepResult) -> bool:
        """Whether ``modality`` produced a usable (non-``access_failure``-only)
        result in this sweep.

        TEMPORARY attribution heuristic (per the Stage-2 plan): the sweeper is
        invoked once across all enabled modalities and returns one aggregate
        :class:`SweepResult` with no per-modality breakdown, so until the real
        adapter reports per-modality coverage we attribute fetched URLs to
        ``web`` and read code/doc locations to ``codebase``; any other modality
        counts if EITHER kind of usable evidence appeared. A modality whose only
        result was an access failure / nothing is NOT counted — that gap is the
        signal a finder-exhausted critic acts on (it must not be aliased to the
        enabled set).
        """
        has_sources = any(s.access_failure is None for s in sr.sources)
        has_locations = bool(sr.code_locations)
        if modality == "web":
            return has_sources
        if modality == "codebase":
            return has_locations
        return has_sources or has_locations

    def run(self, run: ModeRun) -> None:
        """Perform one research round: DECOMPOSE → SWEEP (multi-modal fan-out)
        into the source cache.

        A blocked run (missing/vacuous brief) is an honest ``dry-round``. An
        unblocked round decomposes ``self._topic_text`` into sub-questions, then
        for each sub-question SWEEPs across the enabled modalities via the
        injected :class:`Sweeper`, persisting every fetched ``Source`` to the
        round's source cache and recording a ``sweep`` row (source counts + the
        round-local ``modalities_run``, which reflects a skipped modality rather
        than aliasing the enabled set). Each sub-question yields exactly ONE
        candidate :class:`Claim` (load-bearing, stable id) projected from THAT
        sub-question's gathered evidence.

        Each candidate claim is then adversarially VERIFIED (k-vote
        ``verify_claim``) and the survivors CLASSIFIED by their
        origin-independent witness count (STEP-4): a ``confirmed`` claim (≥2
        distinct origins) joins ``self._confirmed``, anything weaker is an
        honest gap. Finally (STEP-5) the COMPLETENESS CRITIC guards a SYNTHESIZE
        → commit step: when the round produced ≥1 ``confirmed`` claim AND the
        critic reports ``work-done``, the :class:`Synthesizer` writes a cited
        report and the :class:`Committer` commits it, recording an attested,
        file-witnessed ``confirmed-work`` unit (+ a ``landing`` row); every other
        path is an honest ``dry-round`` that does not reset stop-on-dry.
        """
        if self._blocked:
            run.ledger.append(event="dry-round", status="dry",
                              mode_run=run.mode_run)
            return

        # Reset the round-local accumulators at the TOP of every round so
        # confirmed-work derives ONLY from THIS round's sweep, never residue:
        # without this a finder-exhausted round could re-emit a prior round's
        # confirmed claims (resetting stop-on-dry) and modalities_run would grow
        # monotonically until it spuriously equalled the enabled set.
        self._confirmed = []
        self._gaps = []
        self._modalities_run = []

        self._round_idx += 1
        round_idx = self._round_idx
        self._cache = SourceCache(run.tool / "sources.jsonl")

        dr = self.decomposer.decompose(self._topic_text, run.tool)
        round_claims: list[Claim] = []
        for idx, sub_question in enumerate(dr.sub_questions):
            sr = self.sweeper.sweep(sub_question, run.tool, self.modalities)
            for s in sr.sources:
                self._cache.add(s)
            for modality in self.modalities:
                if (modality not in self._modalities_run
                        and self._modality_yielded(modality, sr)):
                    self._modalities_run.append(modality)
            # Load-bearing claim projection: exactly ONE candidate claim per
            # sub-question, witnessed by ONLY that sub-question's evidence
            # (cross-claim origins never confirm — STEP-4 classifies).
            # skip access-failed fetches. A failed fetch still carries
            # a resolved_origin (e.g. a post-DNS timeout) + content_hash, but
            # §5.2 says "a failed fetch yields no usable witness". Witness has no
            # access_failure field, so the failure bit is lost at projection;
            # without this filter a real+failed distinct-origin sweep would
            # count 2 origins -> classify_claim -> confirmed, greening the loop
            # on a fetch that FAILED. Tighten-only, like BUG-527. The failed
            # source is still recorded in the cache above (failures are never
            # silently dropped) — it just never corroborates a claim. This also
            # restores consistency with _modality_yielded, which already
            # excludes access-failed sources from coverage.
            witnesses = [
                Witness(kind="fetched-source", uri=s.url,
                        content_hash=s.content_hash,
                        resolved_origin=s.resolved_origin)
                for s in sr.sources
                if s.access_failure is None
            ] + list(sr.code_locations)
            round_claims.append(Claim(
                id=f"{run.mode_run}-r{round_idx}-q{idx}",
                text=sub_question, status="", witnesses=witnesses,
                load_bearing=True))
            run.ledger.append(
                event="sweep", status="ok", subject=sub_question,
                witness={"kind": "sweep", "sources": len(sr.sources),
                         "code_locations": len(sr.code_locations),
                         "modalities": list(self._modalities_run)},
                mode_run=run.mode_run)

        self._round_claims = round_claims

        # VERIFY + classify (STEP-4). Each candidate claim faces k adversarial
        # refuters via verify_claim (fail-closed: only an explicit non-refuting
        # factory clears it). A killed claim is DROPPED — verify_claim already
        # wrote its `gate` fail row, and no `claim` row is emitted for it, so a
        # refuted claim can never reach _confirmed/_gaps or green the loop. A
        # survivor is classified by the origin-independent witness count
        # (≥2 distinct resolved origins → confirmed; 1 → single-source; 0 →
        # unverified-gap) and recorded as a `claim` row carrying that status +
        # the origin count. Only a `confirmed` claim joins self._confirmed (the
        # eligible-for-confirmed-work set STEP-5 synthesizes); everything else is
        # an honest gap. Origins are counted per-claim, so cross-claim origins
        # never confirm (one claim per sub-question, witnesses from THAT
        # sub-question's sweep only).
        for c in round_claims:
            survived = verify_claim(
                c.id, refuter=self.refuter_factory(c), k=self.k,
                ledger=run.ledger, mode_run=run.mode_run)
            if not survived:
                continue
            c.status = classify_claim(c)
            run.ledger.append(
                event="claim", status="ok", subject=c.id,
                witness={"kind": "claim", "status": c.status,
                         "independent_origins": independent_origins(c.witnesses)},
                mode_run=run.mode_run)
            if c.status == CONFIRMED:
                self._confirmed.append(c)
            else:
                self._gaps.append(c)

        # STEP-5: SYNTHESIZE → commit → confirmed-work, guarded by the
        # COMPLETENESS CRITIC. The critic is the stop-on-dry guard: it is asked
        # whether THIS round did real work or was merely finder-exhausted (a
        # modality skipped / a source unread). A round only proceeds to a
        # confirmed unit when it produced ≥1 `confirmed` claim AND the critic
        # reports `work-done`; anything else is an honest dry-round that records
        # what was not_checked so the audit trail names the finder-exhaustion.
        verdict = self.critic.assess(self._confirmed + self._gaps, self._gaps,
                                     self._modalities_run, self.modalities)
        if not (self._confirmed and verdict.state == "work-done"):
            run.ledger.append(
                event="dry-round", status="dry",
                witness={"kind": "dry", "not_checked": list(verdict.not_checked)},
                mode_run=run.mode_run)
            return

        # The Synthesizer is the SOLE writer of the report file and returns its
        # on-disk sha256; the Committer ONLY `git add`s + commits the
        # already-written path, never modifies content. A None report (the
        # synthesizer declined) or an absent committer is fail-closed to a
        # dry-round (defense in depth: an unpublishable round must never green).
        report = self.synthesizer.synthesize(self._confirmed, self._gaps, run.tool)
        if report is None or self.committer is None:
            run.ledger.append(event="dry-round", status="dry",
                              mode_run=run.mode_run)
            return

        result = self.committer.implement(report, run.tool)
        head_sha = result.head_sha
        report_path = Path(report.path)
        try:
            file_sha = hashlib.sha256(report_path.read_bytes()).hexdigest()
        except OSError:
            file_sha = None
        # Three independent conditions must ALL hold for a gate-passing confirm:
        # the commit succeeded, the file on disk STILL re-hashes to the
        # synthesizer's reported hash (a Committer that re-wrote/normalised the
        # file would invalidate the `file` witness — this is why "Synthesizer
        # writes, Committer only commits" is load-bearing), and the returned
        # head_sha resolves to a real commit in the repo (a fabricated sha never
        # greens the loop — `resolves_to_commit` is the rejector). Any miss is a
        # dry-round.
        if not (result.ok and head_sha is not None
                and file_sha == report.content_hash
                and resolves_to_commit(run.tool, head_sha)):
            run.ledger.append(event="dry-round", status="dry",
                              mode_run=run.mode_run)
            return

        # A REAL corroborated unit: record confirmed-work via append_attested so
        # the `author` is the SUBSTRATE-attested peer of head_sha (never caller
        # content), with a `file` witness the spine `witness-ledgered` gate
        # re-hashes from disk. The subject is the first confirmed claim id (guard
        # against an empty confirmed_ids list — drive() does NOT catch
        # IndexError).
        subj = report.confirmed_ids[0] if report.confirmed_ids else None
        run.ledger.append_attested(
            run.tool, head_sha, event="confirmed-work", subject=subj,
            status="pass",
            witness={"kind": "file", "uri": str(report_path), "sha256": file_sha},
            independence=True, mode_run=run.mode_run)
        # The landing is a plain append — publication is not a substrate-attested
        # authorship event; the `url` witness is recorded, not gate-checked.
        run.ledger.append(
            event="landing", status="ok", subject=result.branch,
            witness={"kind": "url", "uri": result.branch, "landing": "branch-pr"},
            mode_run=run.mode_run)
        # Stage 5: research propagates its report FILE (re-hashable from disk),
        # not a git-sha -- mirroring its confirmed-work `file` witness above.
        # Written via append_attested(repo, result.head_sha, ...) so the author
        # is the substrate-attested peer of the committed head_sha (no re-attest);
        # `file_sha`/`report_path` are the SAME values the confirmed-work witness
        # used (file_sha is non-None on this confirmed path -- the confirm guard
        # already excluded the file_sha=None OSError case), so the row re-derives.
        # Only when the run is isolated on its own branch (legacy emits landing
        # only).
        if run.branch is not None:
            run.ledger.append_attested(
                run.tool, head_sha, event="propagation", subject=run.branch,
                status="ok",
                witness={"kind": "file", "uri": str(report_path), "sha256": file_sha,
                         "artifact": run.branch},
                independence=True, mode_run=run.mode_run)

    # ---- ModeFrontend seam: summary -----------------------------------------
    def interpret(self, run: ModeRun) -> dict:
        """Summarise the run off the ledger — :func:`drive`'s return value.

        Reads the rows ONCE and counts, purely from what was ledgered (the
        summary never recomputes classification — origin-independence is already
        recorded in each ``claim`` row's witness):

        - ``confirmed`` — number of ``confirmed-work`` units the run landed;
        - ``single_source`` — ``claim`` rows the ledger classified
          ``single-source`` (corroborated by exactly one origin);
        - ``gaps`` — ``claim`` rows classified ``unverified-gap`` or
          ``contested`` (the honest gaps routed away from confirmed-work);
        - ``rounds`` — work rounds, i.e. ``dry-round`` + ``confirmed-work`` rows
          (the per-round terminal markers; the ``run-start`` / ``stop`` /
          ``sweep`` / ``gate`` / ``claim`` / ``landing`` rows are not rounds);
        - ``landing`` — the LAST published branch subject, or ``None`` when the
          run never landed a report.
        """
        rows = run.ledger.read()
        confirmed = single_source = gaps = rounds = 0
        landing = None
        for r in rows:
            if r.event == "confirmed-work":
                confirmed += 1
                rounds += 1
            elif r.event == "dry-round":
                rounds += 1
            elif r.event == "claim":
                status = (r.witness or {}).get("status")
                if status == SINGLE_SOURCE:
                    single_source += 1
                elif status in (UNVERIFIED_GAP, CONTESTED):
                    gaps += 1
            elif r.event == "landing":
                landing = r.subject
        return {"confirmed": confirmed, "single_source": single_source,
                "gaps": gaps, "rounds": rounds, "landing": landing}
