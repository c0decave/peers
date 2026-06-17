"""STEP-7 — direction-inference: a minimal trustworthy-bar detector.

Full bar-inference is fuzzy; Stage 0 ships only the *detector*. It answers one
question — "does this tool expose a trustworthy bar?" — by (a) detecting a
runnable test command from well-known markers and (b) classifying the result of
an **injected** baseline run:

  - ``present`` — a runner was detected and the baseline exits 0,
  - ``weak``    — a runner was detected but the baseline is red/flaky,
  - ``absent``  — no runner, or the baseline produced no usable result.

The characterization-baseline *builder* (P6) is DELIVERED in
:mod:`peers.spine.baseline` (Stage 4): ``ensure_bar`` runs this detector and, on
a ``weak``/``absent`` bar, AUTHORS+greens characterization observations to upgrade
the bar to a trustworthy ``present`` (provenance ``"built"``), or stops honestly.
This module stays the PURE detector — ``infer_bar`` never builds. The runner
result is injected (``run_tests(cmd) -> (exit_code, output) | None``) so no heavy
suite runs in a unit test and the detector stays loop-agnostic. ``weak``/``absent``
is the caller's signal (Stage 1+) to build a baseline or stop — the detector is
deliberately fail-closed: anything it cannot positively classify as green is NOT
``present``.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from peers.spine.ledger import RunLedger

# Injected runner: returns (exit_code, output) or None when it could not run.
RunTests = Callable[[str], "tuple[int, str] | None"]


@dataclass
class Bar:
    """The detected bar. ``kind`` is ``present``/``weak``/``absent``;
    ``command`` is the detected test command (or ``None`` when no runner)."""

    kind: str
    command: str | None
    exit_code: int | None = None
    output: str | None = None
    #: HOW the bar was obtained: ``"detected"`` (this detector), ``"built"``
    #: (Stage-4 characterization-baseline builder authored+greened it), or
    #: ``"reused"`` (delegated to the regression snapshot). Last field so the
    #: existing positional ``Bar(kind, command, ...)`` calls are unaffected.
    provenance: str = "detected"


def _detect_runner(repo: Path) -> str | None:
    """Return a runnable test command for ``repo``, or ``None``.

    Deterministic priority: pytest (``pyproject.toml`` / ``pytest.ini``) →
    npm (``package.json`` with a ``scripts.test`` entry) → go (``go.mod``).
    The exact string matters only as the argument handed to the injected
    runner; detection granularity (which baseline gets run) is what counts.
    """
    if (repo / "pyproject.toml").is_file() or (repo / "pytest.ini").is_file():
        return "python3 -m pytest"

    pkg = repo / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            scripts = data.get("scripts")
            if isinstance(scripts, dict) and scripts.get("test"):
                return "npm test"

    if (repo / "go.mod").is_file():
        return "go test ./..."

    return None


def _classify(command: str, result: object) -> Bar:
    """Map an injected ``run_tests`` result to a :class:`Bar`.

    ``None`` or a malformed (non ``(int, …)``) result → ``absent`` (fail-closed:
    no trustworthy baseline). ``exit_code == 0`` → ``present``; non-zero →
    ``weak``.
    """
    if result is None or not isinstance(result, Sequence):
        return Bar("absent", command)
    try:
        exit_code = result[0]
        output = result[1] if len(result) > 1 else ""
    except (TypeError, IndexError, KeyError):
        return Bar("absent", command)           # garbage shape -> no bar
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        return Bar("absent", command)
    if exit_code == 0:
        return Bar("present", command, exit_code=exit_code, output=str(output))
    return Bar("weak", command, exit_code=exit_code, output=str(output))


def infer_bar(
    repo: Path | str,
    run_tests: RunTests,
    *,
    ledger: RunLedger | None = None,
    mode_run: str | None = None,
) -> Bar:
    """Detect a runner in ``repo`` and classify its injected baseline result.

    When ``ledger`` is given, records a ``bar-inferred`` row whose witness
    carries the detected command and the resulting bar kind.
    """
    command = _detect_runner(Path(repo))
    if command is None:
        bar = Bar("absent", None)
    else:
        bar = _classify(command, run_tests(command))

    if ledger is not None:
        ledger.append(
            event="bar-inferred",
            status="pass" if bar.kind == "present" else bar.kind,
            mode_run=mode_run,
            witness={"kind": "bar", "command": bar.command, "bar": bar.kind},
        )
    return bar
