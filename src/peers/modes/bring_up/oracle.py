"""Phase-3 — layered oracle adapters + the adjudicator.

Each oracle turns an :class:`~peers.modes.bring_up.runner.Observation` into a
:class:`Verdict`; :func:`adjudicate` fuses the layered verdicts into a
:class:`Judgment` that classifies a failure. The classification is the honesty
seam: a crash/error is unambiguously a *tool-bug*; a verdict that disagrees with
the corpus ground-truth is *needs-adjudication* (the loop's root-cause step
decides tool-bug vs corpus-error — it is never silently 'fixed' into the tool);
an unreadable ground-truth is a *corpus-error*.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .models import Case
from .runner import Observation

#: Default substrings that mark a tool-internal crash regardless of exit code.
_DEFAULT_SANITIZER_PATTERNS = (
    "AddressSanitizer", "LeakSanitizer", "UndefinedBehaviorSanitizer",
    "ThreadSanitizer", "Segmentation fault", "core dumped", "panic:",
)
#: Failure-class priority when several layers fail in one sweep (lower = wins).
_PRIORITY = {"tool-bug": 0, "corpus-error": 1, "needs-adjudication": 2}


@dataclass(frozen=True)
class Verdict:
    passed: bool
    oracle: str
    signature: str
    category: str = ""   # on failure: tool-bug | corpus-error | needs-adjudication


@dataclass(frozen=True)
class Judgment:
    passed: bool
    bug_kind: str        # none | tool-bug | corpus-error | needs-adjudication
    signature: str
    failing_oracle: str | None


class RuntimeOracle:
    """Fails on a crash/error/timeout/sanitizer trip — all unambiguous tool-bugs."""

    def __init__(self, config: dict) -> None:
        self._success_rc = config.get("success_rc", 0)
        self._patterns = tuple(
            config.get("sanitizer_patterns", _DEFAULT_SANITIZER_PATTERNS))

    def judge(self, case: Case, observation: Observation, *, work=None) -> Verdict:
        if observation.timed_out:
            return Verdict(False, "runtime", "timeout", "tool-bug")
        text = observation.stdout + "\n" + observation.stderr
        for pat in self._patterns:
            if pat in text:
                return Verdict(False, "runtime", f"sanitizer:{pat}", "tool-bug")
        if observation.rc != self._success_rc:
            return Verdict(False, "runtime", f"rc={observation.rc}", "tool-bug")
        return Verdict(True, "runtime", "ok", "")


class DifferentialOracle:
    """Compares the tool's own verdict against the corpus ground-truth.

    Both reads are injected (real wiring: a sqlite/status read + an exploit-corpus
    enrichment read). An unreadable ground-truth is a corpus-error; an unreadable
    tool verdict is a tool-bug; a mismatch is needs-adjudication (root-cause decides).
    """

    def __init__(self, config: dict, *,
                 tool_verdict: Callable[..., str | None],
                 expected_verdict: Callable[[Case], str | None]) -> None:
        self._config = config
        self._tool_verdict = tool_verdict
        self._expected_verdict = expected_verdict

    def judge(self, case: Case, observation: Observation, *, work=None) -> Verdict:
        try:
            expected = self._expected_verdict(case)
        except Exception as exc:  # noqa: BLE001
            # A read FAILURE is transient/retryable, NOT a definitive corpus-error:
            # corpus-error is terminal (the loop excludes it forever), so a flaky
            # DB/nvd blip must never silently drop a case from the harness. Route it
            # to needs-adjudication (re-swept; escalated to 'stuck' if it persists).
            return Verdict(False, "differential",
                           f"ground-truth-error:{type(exc).__name__}",
                           "needs-adjudication")
        if expected is None:
            # positively absent ground-truth (the reader's deliberate 'no row'
            # signal) -> excludable corpus-error, surfaced in the summary.
            return Verdict(False, "differential", "ground-truth-missing", "corpus-error")
        try:
            actual = self._tool_verdict(case, observation, work)
        except Exception as exc:  # noqa: BLE001 — tool didn't produce a verdict
            return Verdict(False, "differential",
                           f"tool-verdict-error:{type(exc).__name__}", "tool-bug")
        if actual is None:
            return Verdict(False, "differential", "tool-verdict-unreadable", "tool-bug")
        if actual == expected:
            return Verdict(True, "differential", f"{actual}=={expected}", "")
        return Verdict(False, "differential",
                       f"{actual}!=expected:{expected}", "needs-adjudication")


class TestSuiteOracle:
    """The corpus case is a test; it passes iff its per-case run exited 0."""

    __test__ = False  # not a pytest test class despite the 'Test' name prefix

    def __init__(self, config: dict) -> None:
        self._success_rc = config.get("success_rc", 0)

    def judge(self, case: Case, observation: Observation, *, work=None) -> Verdict:
        if observation.rc == self._success_rc and not observation.timed_out:
            return Verdict(True, "test-suite", "passed", "")
        sig = "timeout" if observation.timed_out else f"test-failed rc={observation.rc}"
        return Verdict(False, "test-suite", sig, "tool-bug")


def adjudicate(case: Case, verdicts: list[Verdict]) -> Judgment:
    """Fuse layered verdicts into a classified :class:`Judgment`."""
    failing = [v for v in verdicts if not v.passed]
    if not failing:
        return Judgment(True, "none", "all-green", None)
    worst = min(failing, key=lambda v: _PRIORITY.get(v.category, 3))
    return Judgment(False, worst.category or "needs-adjudication",
                    worst.signature, worst.oracle)
