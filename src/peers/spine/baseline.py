"""Stage-4 — the characterization-baseline BUILDER (P6).

``direction.infer_bar`` only *detects* a bar (``present``/``weak``/``absent``).
This module turns a ``weak``/``absent`` bar from a dead end into a BUILD step:
when a tool has little/no test signal, peers first AUTHORS executable
characterization observations (via the injected :class:`BaselineAuthor`), RUNS
them through the injected ``RunTests`` runner, and — **only on a real green** —
upgrades the effective bar to a trustworthy ``present`` with provenance
``"built"`` and a ``file`` witness re-hashed from the written artifact. A tool
whose behaviour cannot be pinned at all is an **honest stop**
(``uncharacterizable`` → bar stays ``absent``), never a guess.

This is explicitly STRONGER than the ``no_regression`` snapshot, which only
protects already-green tests: the snapshot just-passes on an EMPTY baseline, so
a test-less tool gets a meaningless bar. The ``reused`` path delegates to that
snapshot ONLY for a ``weak`` bar whose runner RE-RUNS GREEN (a flaky-but-passing
tool whose tests already exist); the ``built`` path MANUFACTURES observations
for the genuinely test-less weak/absent case.

Everything is reached through injected ports (the :class:`BaselineAuthor`
Protocol + the existing ``direction.RunTests`` callable + an injectable snapshot
delegate), so the orchestration is deterministically unit-testable with fakes —
no live subprocess/LLM in any unit test.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from peers import safe_io
from peers.spine.direction import Bar, RunTests, _classify, infer_bar
from peers.spine.gates import _git_resolve_commit
from peers.spine.ledger import RunLedger

#: Outcome sentinels for :class:`BaselineResult`.
OUTCOME_BUILT = "built"
OUTCOME_REUSED = "reused"
OUTCOME_UNCHARACTERIZABLE = "uncharacterizable"


@dataclass
class CandidateBaseline:
    """A written candidate characterization-test artifact: the ``path`` of the
    file the author materialised and the ``command`` that runs it."""

    path: str
    command: str


@dataclass
class BaselineResult:
    """The result of a baseline build. ``outcome`` is one of the ``OUTCOME_*``
    sentinels; ``bar`` is the (possibly upgraded) bar; ``witness`` is the ``file``
    witness over the written artifact (``built`` only); ``artifact_path`` is the
    written file (``built`` only). Scalar defaults only — never a mutable default."""

    outcome: str
    bar: Bar
    witness: dict | None = None
    artifact_path: str | None = None


@runtime_checkable
class BaselineAuthor(Protocol):
    """The injected port that AUTHORS a candidate characterization test. Returns
    ``None`` when it cannot author one for ``bar`` — the honest-stop input the
    builder maps to ``uncharacterizable``."""

    def author(self, repo: Path, bar: Bar) -> CandidateBaseline | None:
        ...


def _default_snapshot(repo: Path) -> str | None:
    """Return a REUSE message iff a GREEN regression baseline is already pinned on
    disk (``.peers/passing-baseline.txt``, non-empty), else ``None`` (the
    empty-baseline case the builder's ``built`` path exists for).

    full-depth-analysis #9: the prior call keyed on the wrong literal
    (``("no_regression",)``) while ``needs_baseline_snapshot`` gates on the goal-id
    ``no-prior-regression`` — so it ALWAYS returned ``None`` and the reused path was
    dead. ``ensure_baseline_snapshot`` was also a SEED-IF-ABSENT helper whose return
    does not mean "a baseline is pinned"; the correct query is whether the pinned
    baseline file actually exists, which is what this now checks."""
    from peers.regression_baseline import _BASELINE_NAME
    bl = Path(repo) / ".peers" / _BASELINE_NAME
    try:
        if bl.is_file() and bl.read_text(encoding="utf-8").strip():
            return f"reusing pinned regression baseline ({bl})"
    except OSError:
        return None
    return None


def _log_baseline_row(
    ledger: RunLedger,
    result: BaselineResult,
    repo: Path | str,
    mode_run: str | None,
) -> None:
    """Append exactly one ``baseline-built`` row for ``result`` — called by EVERY
    return path in :func:`build_baseline` when a ledger is present, so no return
    is left unlogged.

    NEVER ``independence=True``: a built baseline is authored AND greened in the
    same run (no out-of-band second signal), so it is not a cross-agent
    independence claim. Its trust rests on the ``file`` witness + the attested
    author, not on the ``independence`` flag.
    """
    if result.outcome == OUTCOME_BUILT:
        head_sha = _git_resolve_commit(Path(repo), "HEAD")
        if head_sha is not None:
            # A real attested, witnessed unit. Do NOT use resolves_to_commit(repo,
            # "HEAD") — it is ALWAYS False (requires the resolved 40-hex to equal
            # the literal "head"), which would silently force the fallback.
            ledger.append_attested(
                repo, head_sha, event="baseline-built", status="built",
                witness=result.witness, independence=False, mode_run=mode_run,
            )
        else:
            ledger.append(event="baseline-built", status="built",
                          witness=result.witness, independence=False,
                          mode_run=mode_run)
        return
    if result.outcome == OUTCOME_REUSED:
        ledger.append(event="baseline-built", status="reused",
                      independence=False, mode_run=mode_run)
        return
    # OUTCOME_UNCHARACTERIZABLE (no candidate, red/None run, or OSError re-hash).
    ledger.append(event="baseline-built", status="uncharacterizable",
                  independence=False, mode_run=mode_run)


def build_baseline(
    repo: Path | str,
    run_tests: RunTests,
    *,
    author: BaselineAuthor,
    bar: Bar,
    ledger: RunLedger | None = None,
    mode_run: str | None = None,
    snapshot: Callable[[Path], "str | None"] | None = None,
) -> BaselineResult:
    """The P6 builder. Given a ``weak``/``absent`` ``bar``:

    * the ``reused`` path: if ``bar`` is ``weak`` and its runner RE-RUNS GREEN and
      the snapshot machinery pins something (non-None message) → delegate to the
      snapshot (outcome ``reused``, provenance ``"reused"``, no new file authored);
    * the ``built`` path: else ask ``author`` for a :class:`CandidateBaseline`; a
      ``None`` candidate → ``uncharacterizable``; else RUN the candidate via
      ``run_tests`` and upgrade to ``present``/``"built"`` ONLY on a real green
      (exit 0), emitting a ``file`` witness re-hashed from disk; a red/None run →
      ``uncharacterizable``.

    Fail-closed: an ``OSError`` re-hashing the written artifact (a normaliser /
    symlink swap racing the file) → ``uncharacterizable``, never an escaping
    exception (``drive()`` does not catch frontend exceptions).
    """
    repo_path = Path(repo)
    snap = snapshot if snapshot is not None else _default_snapshot

    def _finish(result: BaselineResult) -> BaselineResult:
        if ledger is not None:
            _log_baseline_row(ledger, result, repo_path, mode_run)
        return result

    # ---- reused path: a WEAK bar whose runner re-runs GREEN delegates to the
    # snapshot (flaky-but-passing tests that ALREADY exist) -------------------
    if bar.kind == "weak":
        rerun = _classify(bar.command or "", run_tests(bar.command or ""))
        if rerun.kind == "present":
            msg = snap(repo_path)
            if msg is not None:
                return _finish(BaselineResult(
                    OUTCOME_REUSED,
                    bar=Bar("present", bar.command, exit_code=0,
                            provenance="reused"),
                ))
            # snapshot pinned NOTHING (empty baseline) -> FALL THROUGH to author.

    # ---- built path: AUTHOR observations for the genuinely test-less case ----
    candidate = author.author(repo_path, bar)
    if candidate is None:
        return _finish(BaselineResult(
            OUTCOME_UNCHARACTERIZABLE, bar=Bar("absent", bar.command)))

    classified = _classify(candidate.command, run_tests(candidate.command))
    if classified.kind != "present":
        return _finish(BaselineResult(
            OUTCOME_UNCHARACTERIZABLE, bar=Bar("absent", candidate.command),
            witness=None))

    # a REAL green — re-hash the written artifact from disk (fail-closed).
    try:
        digest = hashlib.sha256(
            safe_io.read_bytes_no_symlink(Path(candidate.path))).hexdigest()
    except OSError:
        return _finish(BaselineResult(
            OUTCOME_UNCHARACTERIZABLE, bar=Bar("absent", candidate.command),
            witness=None))

    witness = {"kind": "file", "uri": candidate.path, "sha256": digest}
    return _finish(BaselineResult(
        OUTCOME_BUILT,
        bar=Bar("present", candidate.command, exit_code=0,
                output=classified.output, provenance="built"),
        witness=witness, artifact_path=candidate.path))


def ensure_bar(
    repo: Path | str,
    run_tests: RunTests,
    *,
    author: BaselineAuthor,
    ledger: RunLedger | None = None,
    mode_run: str | None = None,
) -> Bar:
    """Run the ``infer_bar`` DETECTOR and, on a ``weak``/``absent`` bar, invoke
    :func:`build_baseline` and return the (possibly upgraded) bar. A ``present``
    bar is returned untouched (the builder is never invoked on it).

    Never lets an ``IndexError``/``AttributeError``/``OSError`` escape — it runs
    inside ``prepare()``/``run()`` and ``drive()`` does not catch those."""
    bar = infer_bar(repo, run_tests, ledger=ledger, mode_run=mode_run)
    if bar.kind in ("weak", "absent"):
        res = build_baseline(repo, run_tests, author=author, bar=bar,
                             ledger=ledger, mode_run=mode_run)
        return res.bar
    return bar
