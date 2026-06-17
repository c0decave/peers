"""STEP-1 — the research-mode ports: the seam every later task depends on.

research reaches its capabilities — breaking a topic into sub-questions
(DECOMPOSE), gathering fetched/read evidence across web + codebase + docs
(SWEEP), turning corroborated claims into a cited report (SYNTHESIZE),
committing it (COMMIT), and judging whether a round did real work or was
merely finder-exhausted (CRITIQUE) — through **injected Protocols**, exactly
as Stage-1 develop injected ``Auditor``/``Author``/``Implementer`` and the
Stage-0 spine injected its test-runner (``direction.infer_bar``) and refuter
(``adversarial_verify.verify_claim``). Keeping these as ``runtime_checkable``
Protocols (not base classes) means the LLM/web adapters stay thin and
swappable, and the orchestration in :mod:`peers.research.frontend` is
unit-testable with trivial fakes (no live web / no LLM in any unit test).

The verify seam is deliberately NOT a Protocol: a zero-method
``@runtime_checkable`` ``Verifier`` would be degenerate
(``isinstance(anything, X)`` is always ``True``). Adversarial-VERIFY is
injected as a plain typed constructor callable, :data:`RefuterFactory`,
mirroring :mod:`peers.develop.frontend`'s ``refuter_factory`` and the Stage-0
``verify_claim(refuter=...)`` shape. The frontend depends on **FIVE** Protocol
ports: :class:`Decomposer`, :class:`Sweeper`, :class:`Synthesizer`,
:class:`Committer`, :class:`CompletenessCritic`.

The honesty doctrine of the research spec maps 1:1 onto the spine's
no-self-greening closure: a claim counts only when corroborated by a FETCHED
source (URL + content hash) or READ code (``file:line``) — never by model
assertion — and a load-bearing ``confirmed`` claim requires **≥2
origin-independent witnesses** (see :mod:`peers.research.claim_ledger`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol, runtime_checkable


@dataclass
class Source:
    """One fetched/read evidence artifact, as the source cache records it.

    The §5.3 source-cache schema: ``url`` is what was fetched/read;
    ``resolved_origin`` is the canonical identity the fetcher resolved it to
    (independence is computed over this, NOT the raw URL — two URLs to the same
    origin count as one witness); ``content_hash`` is the sha256 of the fetched
    body (the witness re-derivation key); ``retrieval_time`` is an ISO-8601
    stamp; ``access_failure`` is a non-None reason string when the fetch failed
    (a failed fetch is recorded, never silently dropped, but yields no usable
    witness).
    """

    url: str
    resolved_origin: str
    content_hash: str
    retrieval_time: str
    access_failure: str | None = None


@dataclass
class Witness:
    """Links a claim to one piece of corroborating evidence.

    ``kind`` is ``"fetched-source"`` (a source-cache entry, ``uri`` = its URL)
    or ``"code-location"`` (read code, ``uri`` = ``file:line``);
    ``content_hash`` ties back to the source cache / the read bytes;
    ``resolved_origin`` is the independence key the claim ledger counts.
    """

    kind: str
    uri: str
    content_hash: str
    resolved_origin: str


@dataclass
class Claim:
    """One load-bearing assertion under investigation.

    ``id`` is a stable handle (``f"{mode_run}-r{round}-q{idx}"``) used as the
    verify subject and the ledger ``subject``; ``status`` is set by
    :func:`peers.research.claim_ledger.classify_claim` over ``witnesses``;
    ``load_bearing`` marks claims that must be corroborated or routed to gaps
    (research never asserts a load-bearing claim from model output).
    """

    id: str
    text: str
    status: str
    witnesses: list[Witness] = field(default_factory=list)
    load_bearing: bool = False


@dataclass
class ReportArtifact:
    """The synthesized, on-disk report the Committer commits.

    ``path`` is the written file; ``content_hash`` is the synthesizer's sha256
    of that file (re-derived from disk by the spine ``witness-ledgered`` gate —
    the synthesizer is the SOLE writer, so a Committer that re-writes content
    invalidates this); ``confirmed_ids`` are the confirmed claims it cites (the
    first becomes the confirmed-work ledger subject).
    """

    path: str
    content_hash: str
    confirmed_ids: list[str] = field(default_factory=list)


@dataclass
class DecomposeResult:
    """A topic broken into sub-questions (one candidate claim each)."""

    sub_questions: list[str] = field(default_factory=list)


@dataclass
class SweepResult:
    """Evidence gathered for one sub-question across the enabled modalities."""

    sources: list[Source] = field(default_factory=list)
    code_locations: list[Witness] = field(default_factory=list)


@dataclass
class CompletenessVerdict:
    """The completeness critic's read of a round.

    ``state`` is ``"work-done"`` (the round corroborated something real) or
    ``"finder-exhausted"`` (a modality skipped / a source unread — a dry round
    that does NOT advance the stop-on-dry counter); ``not_checked`` names the
    modalities/sources the critic found unexamined.
    """

    state: str
    not_checked: list[str] = field(default_factory=list)


@dataclass
class CommitResult:
    """The outcome of committing a synthesized report.

    ``ok`` is success; ``head_sha`` is the resulting commit (only a sha that
    resolves to a real 40-hex commit becomes a witnessed confirmed-work — see
    :func:`peers.spine.gates.resolves_to_commit`); ``branch`` is the branch-PR
    landing target; ``reason`` explains a non-ok result.
    """

    ok: bool
    head_sha: str | None = None
    branch: str | None = None
    reason: str = ""


@runtime_checkable
class Decomposer(Protocol):
    """Breaks a topic (+ optional grounding repo) into sub-questions."""

    def decompose(self, topic, repo) -> DecomposeResult:
        ...


@runtime_checkable
class Sweeper(Protocol):
    """Gathers evidence for one sub-question across the enabled modalities."""

    def sweep(self, sub_question, repo, modalities) -> SweepResult:
        ...


@runtime_checkable
class Synthesizer(Protocol):
    """Renders corroborated claims into a cited report on disk (the SOLE writer
    of the report file), or ``None`` when the report fails its own cited-report
    gate (a dry round upstream)."""

    def synthesize(self, claims, gaps, repo) -> ReportArtifact | None:
        ...


@runtime_checkable
class Committer(Protocol):
    """Commits the already-written report path (never modifying its content),
    returning the resulting commit. Mirrors develop's ``Implementer.implement``
    shape but takes a :class:`ReportArtifact`, not an ``AuthoredContract``."""

    def implement(self, report, repo) -> CommitResult:
        ...


@runtime_checkable
class CompletenessCritic(Protocol):
    """Judges whether a round did real work or was finder-exhausted (a modality
    skipped / a source unread) — the stop-on-dry guard."""

    def assess(self, claims, gaps, modalities_run, modalities_enabled) -> CompletenessVerdict:
        ...


#: The verify seam — a plain typed callable, NOT a Protocol. Given a candidate
#: claim it yields the per-vote refuter ``verify_claim`` calls; mirrors
#: ``peers.develop.frontend``'s ``refuter_factory`` and the Stage-0
#: ``verify_claim(refuter=...)`` shape. There is NO ``Verifier`` Protocol.
RefuterFactory = Callable[[Claim], Callable[[int], bool]]
