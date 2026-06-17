"""Shared fixtures for the research-mode unit suite (Tasks 2–7 reuse these).

Not a test module (underscore prefix) — imported as
``from tests.unit._research_helpers import ...``.

**Bar precondition (load-bearing, but inverted from develop):** research is a
KNOWLEDGE mode — it must work on a topic with NO repo and NO test bar at all.
So ``ResearchFrontend.prepare`` records the bar (for the audit trail) but does
NOT block on an absent one; what blocks a round is a missing/vacuous
``TOPIC.md`` brief. ``_topic`` writes a non-vacuous generic brief (``## Scope`` +
``## Questions``, NO ``## Frameworks`` security section).
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from peers.research.frontend import ResearchFrontend
from peers.research.ports import (
    CommitResult,
    CompletenessVerdict,
    DecomposeResult,
    ReportArtifact,
    Source,
    SweepResult,
)
from peers.spine.mode_run import ModeRun
from peers.spine.op_config import OpConfig


def _git(p: Path, *a: str) -> str:
    return subprocess.run(
        ["git", "-C", str(p), *a], capture_output=True, text=True, check=True,
    ).stdout


def _topic(p: Path, body: str | None = None) -> None:
    """Write a NON-vacuous generic TOPIC.md (## Scope + ## Questions; NO ## Frameworks)."""
    (p / "TOPIC.md").write_text(
        body
        or (
            "# Topic\n\n## Scope\n"
            "Whether asparagus can be cloned from cuttings in Alaska in early "
            "autumn, and what frost-window constraints apply to the rooting "
            "period.\n\n"
            "## Questions\n"
            "1. Does asparagus root from cuttings at all?\n"
            "2. What is the minimum soil temperature for rooting?\n"
        )
    )


def _run(tmp_path: Path, *, with_topic: bool = True) -> ModeRun:
    if with_topic:
        _topic(tmp_path)
    return ModeRun(
        tool=tmp_path,
        op_config=OpConfig.from_dict({"mode": "research"}),
        ledger_path=tmp_path / "run.jsonl",
        mode_run="r1",
    )


def _attested_repo(p: Path, peer: str = "claude") -> str:
    # TWO commits — `base` is REQUIRED: attest_commits no-ops on a falsy since_sha.
    from peers import attest

    _git(p, "init", "-q")
    _git(p, "config", "user.email", "t@t")
    _git(p, "config", "user.name", "t")
    _git(p, "config", "commit.gpgsign", "false")
    (p / "a.py").write_text("a")
    _git(p, "add", "a.py")
    _git(p, "commit", "-q", "-m", "a")
    base = _git(p, "rev-parse", "HEAD").strip()
    (p / "b.py").write_text("b")
    _git(p, "add", "b.py")
    _git(p, "commit", "-q", "-m", "b")
    sha = _git(p, "rev-parse", "HEAD").strip()
    attest.attest_commits(p, peer, base, sha)  # HEAD attested to `peer`
    return sha


def _repo_with_commit(p: Path) -> str:
    # a REAL commit but NO peers-attest note (the negative e2e).
    _git(p, "init", "-q")
    _git(p, "config", "user.email", "t@t")
    _git(p, "config", "user.name", "t")
    _git(p, "config", "commit.gpgsign", "false")
    (p / "a.py").write_text("a")
    _git(p, "add", "a.py")
    _git(p, "commit", "-q", "-m", "a")
    return _git(p, "rev-parse", "HEAD").strip()


def _write_report(p: Path, name: str = "RESEARCH.md",
                  body: str = "# Report\n\n## Gaps\nx\n") -> tuple[Path, str]:
    """Write a report file to disk and return (path, sha256-hex) so a 'file' witness re-derives."""
    path = p / name
    path.write_text(body)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _src(url: str, origin: str, content: str = "content", *,
         failure: str | None = None) -> Source:
    """A Source as the fetcher port would return it. content_hash is over `content`."""
    return Source(
        url=url,
        resolved_origin=origin,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        retrieval_time="2026-06-10T00:00:00Z",
        access_failure=failure,
    )


# ---- Trivial port fakes derived straight from the Task-1 Protocol signatures ----
class _NullDecomposer:
    def decompose(self, topic, repo):
        return DecomposeResult(sub_questions=[])


class _FixedDecomposer:
    def __init__(self, qs):
        self.qs = qs

    def decompose(self, topic, repo):
        return DecomposeResult(sub_questions=list(self.qs))


class _NullSweeper:
    def sweep(self, sub_question, repo, modalities):
        return SweepResult(sources=[], code_locations=[])


class _FixedSweeper:
    """Returns a fixed SweepResult per call; `once=True` -> only the first round yields sources."""

    def __init__(self, result, once=False):
        self.r = result
        self.once = once
        self.n = 0

    def sweep(self, sub_question, repo, modalities):
        self.n += 1
        if not self.once or self.n == 1:
            return self.r
        return SweepResult(sources=[], code_locations=[])


class _AlwaysSynth:
    """Writes the report to disk and reports a real (path, sha256) the gate can re-hash."""

    def __init__(self, repo):
        self.repo = repo

    def synthesize(self, claims, gaps, repo):
        path, digest = _write_report(self.repo)
        return ReportArtifact(
            path=str(path),
            content_hash=digest,
            confirmed_ids=[c.id for c in claims if c.status == "confirmed"],
        )


class _NullSynth:
    def synthesize(self, claims, gaps, repo):
        return None


# A CompletenessCritic fake: reports whether the round did real work or was
# finder-exhausted. Empty construction defaults to a benign finder-exhausted
# verdict (NO IndexError).
class _FixedCritic:
    def __init__(self, *verdicts):
        self.v = list(verdicts) or [
            CompletenessVerdict(state="finder-exhausted", not_checked=[]),
        ]
        self.n = 0

    def assess(self, claims, gaps, modalities_run, modalities_enabled):
        v = self.v[min(self.n, len(self.v) - 1)]
        self.n += 1
        return v


class _FixedCommitter:
    def __init__(self, result):
        self.r = result

    def implement(self, report, repo):
        return self.r


# ---- Composite frontends used in Tasks 5 & 6 ----
def _research_fe_that_confirms_once(tmp_path, sha):
    """A ResearchFrontend that confirms exactly one corroborated claim in round 1
    (sweeper once=True returns TWO origin-independent sources for the sub-question),
    then sweeps nothing — critic reports work-done then finder-exhausted."""
    return ResearchFrontend(
        decomposer=_FixedDecomposer(["q1"]),
        sweeper=_FixedSweeper(
            SweepResult(
                sources=[
                    _src("https://a.example/x", "a.example"),
                    _src("https://b.example/y", "b.example"),
                ],
                code_locations=[],
            ),
            once=True,
        ),
        synthesizer=_AlwaysSynth(tmp_path),
        committer=_FixedCommitter(
            CommitResult(ok=True, head_sha=sha, branch="research/x"),
        ),
        critic=_FixedCritic(
            CompletenessVerdict(state="work-done", not_checked=[]),
            CompletenessVerdict(state="finder-exhausted", not_checked=[]),
        ),
        modalities=["web"],
        run_tests=lambda cmd: None,
        refuter_factory=lambda c: (lambda i: False),
        k=2,
    )


def _research_fe_finder_exhausted(tmp_path):
    """Same shape but the sweeper ALWAYS returns ZERO sources (no witness can be
    corroborated) and the critic always reports finder-exhausted — every round is a
    dry-round and the loop stops on stop-on-dry with NO confirmed-work."""
    _topic(tmp_path)
    return ResearchFrontend(
        decomposer=_FixedDecomposer(["q1"]),
        sweeper=_FixedSweeper(SweepResult(sources=[], code_locations=[])),
        synthesizer=_AlwaysSynth(tmp_path),
        committer=_FixedCommitter(
            CommitResult(ok=True, head_sha="0" * 40, branch="research/x"),
        ),
        critic=_FixedCritic(
            CompletenessVerdict(state="finder-exhausted", not_checked=["web"]),
        ),
        modalities=["web"],
        run_tests=lambda cmd: None,
        refuter_factory=lambda c: (lambda i: False),
        k=2,
    )
