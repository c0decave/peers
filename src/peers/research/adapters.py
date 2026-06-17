"""STEP-7 — the real research Synthesizer adapter (thin; the SOLE report writer).

``ReportSynthesizer`` is the production :class:`peers.research.ports.Synthesizer`:
given the round's confirmed claims it renders a markdown body (via an INJECTED
``write_report`` renderer so the unit test stays deterministic — no live LLM),
writes it to ``repo/RESEARCH.md`` through the no-symlink atomic writer, runs the
generic :func:`peers.research.checks.report_cited.check_report` gate on the
written file, and on pass returns a :class:`ReportArtifact` whose
``content_hash`` is re-derived FROM DISK (so the spine ``witness-ledgered`` gate
re-hashes the exact bytes). On a failing report (uncited / missing gaps /
completeness lie) it returns ``None`` — a dry round upstream, never a crash.

The Synthesizer is the SOLE writer of the report file. The companion real
``Committer`` adapter (``git add RESEARCH.md`` + commit on a ``research/<slug>``
branch, never modifying content) is the same thin shape as develop's
``Implementer`` but takes a :class:`ReportArtifact`; its live commit is
integration validation, not a deterministic unit acceptance, so it is not
wired here.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from collections.abc import Callable as _C
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from peers.agent_invoke import extract_json_array, extract_json_object
from peers.research.checks.report_cited import DOC_NAME, check_report
from peers.research.ports import (
    CommitResult,
    CompletenessVerdict,
    DecomposeResult,
    ReportArtifact,
    Source,
    SweepResult,
    Witness,
)
from peers.safe_io import atomic_write_text_in_dir_no_symlink, read_bytes_no_symlink

#: Cap the committer's drift re-read (defense-in-depth atop the no-symlink read).
_REPORT_MAX_BYTES = 16 * 1024 * 1024

_STOPWORDS = frozenset({
    "does", "work", "works", "this", "that", "with", "from", "what", "when",
    "where", "which", "your", "have", "will", "into", "them", "then", "than",
    "about", "would", "could", "should", "between", "using", "used", "make",
})

#: A one-shot agent runner ``run_agent(prompt) -> text`` (see
#: :func:`peers.agent_invoke.agent_runner_from_spec`).
RunAgent = Callable[[str], str]


#: Concrete peers research-apparatus paths/filenames. A sub-question that names
#: one of these is interrogating the research machinery itself, not the topic —
#: a degenerate, trivially-true meta-question (the documented driver of the
#: q6/q7 vacuous "confirmed"; see docs/audits/2026-06-15-research-confirmation-
#: seed-vacuity.md option C). These are concrete tokens an external-topic
#: question would never contain, so the false-positive risk is near zero.
_APPARATUS_MARKERS: tuple[str, ...] = (
    ".peers",
    "seed_urls",
    "config.yaml",
    "topic.md",
    "research.md",
    "run.jsonl",
)


def _is_apparatus_question(sub_question: str) -> bool:
    """True iff a sub-question references the peers research apparatus itself
    (case-insensitive). Defense-in-depth over the relevance gate + §5.2
    single-origin rule: drop the degenerate self-referential class at the
    source so it never enters the sweep/confirm pipeline."""
    low = sub_question.lower()
    return any(marker in low for marker in _APPARATUS_MARKERS)


class LLMDecomposer:
    """A production :class:`peers.research.ports.Decomposer`: asks an injected
    agent to break a topic into focused sub-questions.

    Fail-closed: a runner error, non-JSON, or malformed output yields an EMPTY
    DecomposeResult (an honest dry round), never fabricated sub-questions, and
    :meth:`decompose` never raises into the spine ``drive`` loop.

    Self-referential apparatus sub-questions (those naming ``.peers``,
    ``seed_urls``, ``config.yaml``, or the report artifacts) are dropped: they
    are degenerate meta-questions that vacuously "confirm" and never describe
    the research topic."""

    def __init__(self, *, run_agent: RunAgent, max_questions: int = 8) -> None:
        if max_questions < 1:
            raise ValueError("max_questions must be >= 1")
        self.run_agent = run_agent
        self.max_questions = max_questions

    def _build_prompt(self, topic: str, repo: Path) -> str:
        return (
            f"Break this research topic into focused, independently-answerable "
            f"sub-questions (grounding repo: {repo}):\n\n{topic}\n\n"
            "Return ONLY a JSON array of sub-question strings (no prose). Each "
            "must be a concrete question evidence could corroborate. If the "
            "topic is too vague to decompose, return []."
        )

    def decompose(self, topic, repo) -> DecomposeResult:
        try:
            raw = self.run_agent(self._build_prompt(str(topic), Path(repo)))
        except Exception:  # noqa: BLE001 — adapter boundary: never raise into drive()
            return DecomposeResult(sub_questions=[])
        parsed = extract_json_array(raw)
        if parsed is None:
            return DecomposeResult(sub_questions=[])
        out: list[str] = []
        for q in parsed:
            if len(out) >= self.max_questions:
                break
            if isinstance(q, str) and q.strip() and not _is_apparatus_question(q):
                out.append(q.strip())
        return DecomposeResult(sub_questions=out)


class LLMClaimRefuter:
    """A production research claim refuter (the verify_claim ``refuter_factory``).

    ResearchFrontend defaults to a refute-everything stub (fail-closed but inert
    — no claim survives). This asks an injected agent whether a claim is genuinely
    supported by its evidence; the claim survives only on a clear "not refuted".

    Fail-closed: a runner error, non-JSON, an ambiguous reply, or a
    missing/non-bool ``refuted`` key all count as REFUTED (True)."""

    def __init__(self, *, run_agent: RunAgent) -> None:
        self.run_agent = run_agent

    #: Per-vote refutation angles so the k votes are not byte-identical prompts
    #: (RC-05): each vote attacks the claim from a distinct lens.
    _ANGLES = (
        "Does the evidence actually say this, or is it an over-reading?",
        "Is there a more likely alternative explanation the evidence fits?",
        "Are the witnesses independent and on-topic, or circular/irrelevant?",
    )

    def refuter_factory(self, claim):
        base = (
            "Try to REFUTE this research claim against its evidence. Is it "
            "genuinely supported, or unsupported / overstated / wrong?\n"
            f"- claim: {getattr(claim, 'text', '')}\n"
            f"- witnesses: {len(getattr(claim, 'witnesses', []) or [])}\n"
        )
        tail = (
            '\nRespond with ONLY {"refuted": true|false} (refuted=true means NOT '
            "supported). If unsure, refuted=true."
        )

        def _refute(vote_index: int) -> bool:
            angle = self._ANGLES[vote_index % len(self._ANGLES)]
            prompt = f"{base}- focus: {angle}{tail}"
            try:
                raw = self.run_agent(prompt)
            except Exception:  # noqa: BLE001 — fail-closed: unverifiable -> refuted
                return True
            obj = extract_json_object(raw)
            if not isinstance(obj, dict) or not isinstance(obj.get("refuted"), bool):
                return True
            return obj["refuted"]

        return _refute


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


#: The single ``resolved_origin`` shared by ALL code-location witnesses of a
#: sweep. The repo-under-study is ONE source under the §5.2 anti-self-confirmation
#: rule, so code read across >=2 files must count as ONE origin — not one-per-file.
#: A per-file origin would let a codebase-only claim self-confirm by being echoed
#: across the repo's own files (the BUG-527/528 origin-over-count family), which
#: also contradicts the documented "codebase-only round ends honestly dry" design
#: and the report's >=2-distinct-URL citation floor. The ``uri`` still carries the
#: precise ``file:line`` (so evidence is not lost) — only the independence key is
#: collapsed.
_CODEBASE_ORIGIN = "codebase"

#: Relevance gate (deterministic): a fetched web source corroborates a claim only
#: if its CONTENT actually supports the sub-question, not merely because it was
#: fetched. Without this, ``make_seed_url_search`` returns the SAME seed set for
#: every sub-question (web_fetch.py), so every surviving question is auto-confirmed
#: by the static seeds and the claim ledger's >=2-origin rule is vacuous (the
#: seed-origin vacuity, docs/audits/2026-06-15-research-confirmation-seed-vacuity.md).
#: A source supports the claim iff a meaningful FRACTION of the sub-question's
#: salient tokens appear in the body, OR at least one DISTINCTIVE (long) token does
#: (the exact symbol/term the question is about). The k-vote adversarial-verify
#: remains the second gate; this is a NECESSARY (not sufficient) corroboration
#: filter. Conservative thresholds keep real on-topic sources (favour recall: a
#: false-negative would drop a genuine corroborator).
_RELEVANCE_MIN_RATIO = 0.34
_RELEVANCE_DISTINCTIVE_LEN = 8
_OFFTOPIC_REASON = "off-topic: body lacks the sub-question's salient tokens"


def _source_supports(tokens: list[str], body: bytes) -> bool:
    """True iff a fetched ``body`` SUPPORTS a sub-question whose salient identifier
    ``tokens`` are given (see :data:`_RELEVANCE_MIN_RATIO`). Fail-closed when there
    are no salient tokens to match on (nothing corroborates from nothing)."""
    if not tokens:
        return False
    text = body.decode("utf-8", "replace").lower()
    present = [t for t in tokens if t in text]
    if not present:
        return False
    if any(len(t) >= _RELEVANCE_DISTINCTIVE_LEN for t in present):
        return True
    return len(present) / len(tokens) >= _RELEVANCE_MIN_RATIO


class CodebaseSweeper:
    """A production :class:`peers.research.ports.Sweeper`.

    The ``codebase`` modality is real + deterministic + network-free: it
    ``git grep``s the repo for the sub-question's identifier tokens and emits a
    ``code-location`` :class:`Witness` per hit whose ``content_hash`` is sha256
    of the actual matched line (re-derivable from disk). The ``web`` modality
    runs ONLY when an injected ``web_search``+``fetch`` pair is supplied (it is
    skipped honestly otherwise); a fetched body's ``content_hash`` is sha256 of
    the real bytes, and a failed fetch is RECORDED (``access_failure``), never
    silently dropped and never a usable witness. No hash is ever fabricated."""

    def __init__(
        self,
        *,
        web_search: _C[[str], "list[str]"] | None = None,
        fetch: _C[[str], "tuple[bytes, str] | None"] | None = None,
        clock: _C[[], str] | None = None,
        max_hits: int = 20,
    ) -> None:
        self.web_search = web_search
        self.fetch = fetch
        self.clock = clock or _utc_now_iso
        self.max_hits = max_hits

    @staticmethod
    def _tokens(sub_question: str) -> list[str]:
        seen: list[str] = []
        for m in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", str(sub_question)):
            t = m.lower()
            if t not in _STOPWORDS and t not in seen:
                seen.append(t)
        return seen

    def _grep(self, repo: Path, tokens: list[str]) -> list[Witness]:
        if not tokens:
            return []
        # -z: NUL-delimit path/lineno so a filename containing ':' is parsed
        # unambiguously (HS-R3). Capture bytes + decode errors='replace' so a
        # non-UTF-8 matched line degrades gracefully instead of raising
        # UnicodeDecodeError and aborting the run (HS-R2).
        args = ["git", "-C", str(repo), "grep", "-z", "-n", "-I", "-i", "-F"]
        for t in tokens:
            args += ["-e", t]
        try:
            r = subprocess.run(args, capture_output=True, check=False)
        except (OSError, subprocess.SubprocessError):
            return []
        text = (r.stdout or b"").decode("utf-8", "replace")
        out: list[Witness] = []
        seen: set[tuple[str, str]] = set()
        for record in text.split("\n"):
            if len(out) >= self.max_hits:
                break
            if not record:
                continue
            fields = record.split("\x00")
            if len(fields) < 3:
                continue
            relpath, line_no = fields[0], fields[1]
            content = "\x00".join(fields[2:])  # content holds no NUL, but be safe
            key = (relpath, line_no)
            if key in seen:
                continue
            seen.add(key)
            out.append(Witness(
                kind="code-location",
                uri=f"{relpath}:{line_no}",
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                resolved_origin=_CODEBASE_ORIGIN,   # §5.2: the repo is ONE source
            ))
        return out

    def sweep(self, sub_question, repo, modalities) -> SweepResult:
        repo = Path(repo)
        modalities = list(modalities or [])
        code_locations: list[Witness] = []
        sources: list[Source] = []
        tokens = self._tokens(str(sub_question))
        if "codebase" in modalities:
            code_locations = self._grep(repo, tokens)
        if "web" in modalities and self.web_search and self.fetch:
            try:
                urls = list(self.web_search(str(sub_question)))
            except Exception:  # noqa: BLE001 — searcher boundary: no urls -> no web evidence
                urls = []
            for url in urls[: self.max_hits]:
                stamp = self.clock()
                try:
                    res = self.fetch(url)
                except Exception:  # noqa: BLE001 — a fetch crash is a recorded failure
                    res = None
                if res is None:
                    sources.append(Source(
                        url=url, resolved_origin=url, content_hash="",
                        retrieval_time=stamp, access_failure="fetch failed"))
                    continue
                body, origin = res
                # Relevance gate: a fetched source corroborates the claim ONLY if
                # its content supports the sub-question. An off-topic body is still
                # RECORDED (audit trail) but marked access_failure so it yields no
                # usable witness (frontend.py projection + _modality_yielded both
                # already exclude access_failure sources) — defeating the
                # seed-origin vacuity without losing the record of what was fetched.
                supports = _source_supports(tokens, body)
                sources.append(Source(
                    url=url, resolved_origin=origin,
                    content_hash=hashlib.sha256(body).hexdigest(),
                    retrieval_time=stamp,
                    access_failure=None if supports else _OFFTOPIC_REASON))
        return SweepResult(sources=sources, code_locations=code_locations)


class DeterministicCompletenessCritic:
    """A production :class:`peers.research.ports.CompletenessCritic` that judges
    a round purely on modality COVERAGE — deterministic, so it cannot lie.

    ``finder-exhausted`` (a dry round that does NOT advance stop-on-dry) when any
    enabled modality was not run this round; otherwise ``work-done``. (Whether a
    confirmed claim exists is checked separately by the frontend.)"""

    def assess(self, claims, gaps, modalities_run, modalities_enabled):
        run = set(modalities_run or [])
        not_checked = [m for m in (modalities_enabled or []) if m not in run]
        state = "finder-exhausted" if not_checked else "work-done"
        return CompletenessVerdict(state=state, not_checked=not_checked)


class ReportCommitter:
    """A production :class:`peers.research.ports.Committer`: commits the report
    the Synthesizer wrote, WITHOUT modifying its content, and attests it.

    Honesty seam: re-verifies the on-disk sha256 against
    ``ReportArtifact.content_hash`` (the synthesizer is the sole writer — any
    drift fails CLOSED), stages and commits ONLY the report file (never ``-A``),
    and attributes the commit via the substrate ``peers-attest`` note."""

    def __init__(
        self, *, attest_peer: str | None = "research",
        commit_message: str = "research: commit synthesized report",
    ) -> None:
        self.attest_peer = attest_peer
        self.commit_message = commit_message

    def implement(self, report, repo):
        repo = Path(repo)
        path = Path(report.path)
        if not path.is_file():
            return CommitResult(ok=False, reason=f"report file missing: {path}")
        # HS-R4: read the report through the hardened no-symlink reader (the same
        # one the spine file-witness gate uses) — the committer is the trust
        # boundary that commits + attests, so it must not follow a symlinked
        # report.path it was handed.
        try:
            actual = hashlib.sha256(
                read_bytes_no_symlink(path, max_bytes=_REPORT_MAX_BYTES)).hexdigest()
        except OSError as e:
            return CommitResult(ok=False, reason=f"cannot read report: {e}")
        if actual != report.content_hash:
            return CommitResult(ok=False,
                                reason="report content drifted since synthesis")
        try:
            rel = str(path.resolve().relative_to(repo.resolve()))
        except ValueError:
            return CommitResult(ok=False, reason="report is not under the repo")

        def g(*a, check=True):
            return subprocess.run(["git", "-C", str(repo), *a],
                                  capture_output=True, text=True, check=check)

        try:
            base = g("rev-parse", "HEAD").stdout.strip()
            branch = g("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            g("add", "--", rel)
            if g("diff", "--cached", "--quiet", "--", rel, check=False).returncode == 0:
                return CommitResult(ok=False, branch=branch,
                                    reason="report unchanged; nothing to commit")
            # RC-01: commit ONLY the report path — a bare `git commit` would
            # sweep the entire staged index (a pre-staged unrelated file would
            # ride along in the attested commit). The pathspec commit ignores
            # other staged entries.
            g("commit", "-q", "-m", self.commit_message, "--", rel)
            sha = g("rev-parse", "HEAD").stdout.strip()
        except subprocess.CalledProcessError as e:
            return CommitResult(ok=False, reason=f"git failed: {e.stderr or e}")
        if self.attest_peer:
            from peers.attest import attest_commits
            attest_commits(repo, self.attest_peer, base, sha)
        return CommitResult(ok=True, head_sha=sha, branch=branch)


class ReportSynthesizer:
    """The real :class:`peers.research.ports.Synthesizer` adapter.

    ``write_report(claims, gaps) -> str`` is injected (the deterministic seam):
    in production it is the LLM renderer; in the unit suite it is a fixed-body
    lambda. :meth:`synthesize` is the SOLE writer of ``repo/RESEARCH.md``.
    """

    def __init__(self, write_report: Callable[[list, list], str]) -> None:
        self.write_report = write_report

    def synthesize(self, claims, gaps, repo) -> ReportArtifact | None:
        """Render → write → gate. Returns a re-derivable :class:`ReportArtifact`
        on a passing cited report, ``None`` on a failing one.

        The report is written first (the synthesizer is the sole writer) and the
        gate runs on the FILE, so the ``content_hash`` is sha256 of the exact
        on-disk bytes — never the in-memory body — guaranteeing the downstream
        ``file`` witness re-hashes to the same value post-commit.
        """
        # Materialise ONCE up front: the Synthesizer Protocol does not promise
        # ``claims`` is a re-iterable list, and we read it twice (render +
        # confirmed_ids). A one-shot generator would otherwise be exhausted by the
        # render step, silently dropping every confirmed id.
        claims = list(claims)
        gaps = list(gaps)
        path = Path(repo) / DOC_NAME
        try:
            body = self.write_report(claims, gaps)
            atomic_write_text_in_dir_no_symlink(path, body)
            ok, _problems = check_report(path)
        except Exception:
            # The injected renderer is the untrusted LLM boundary: a non-encodable
            # body (a lone surrogate — what bytes.decode('utf-8','surrogateescape')
            # yields for arbitrary LLM/HTTP bytes), a non-str body, or a raising
            # render/write is NOT evidence of a report. Fail CLOSED to None (a dry
            # round upstream); never let it propagate out of drive() and crash the
            # run. Mirrors check_report()/verify()'s fail-closed posture and the
            # file_sha re-hash guard in frontend.run().
            return None
        if not ok:
            return None
        content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        confirmed_ids = [c.id for c in claims if getattr(c, "status", "") == "confirmed"]
        return ReportArtifact(
            path=str(path), content_hash=content_hash, confirmed_ids=confirmed_ids)


__all__ = [
    "LLMDecomposer",
    "CodebaseSweeper",
    "DeterministicCompletenessCritic",
    "LLMClaimRefuter",
    "ReportCommitter",
    "ReportSynthesizer",
]
