"""STEP-4 — ModeRun record, ModeFrontend protocol, and the drive loop.

This is the seam every mode (develop / find-bugs / research, Stage 1+) plugs into.
A :class:`ModeRun` is the immutable binding of *what* a run operates on (the tool
path, the op-config, the ledger path, the run id). A :class:`ModeFrontend` is the
behavioural Protocol a mode implements: ``prepare`` (set up / infer the bar),
``run`` (one round of work), ``interpret`` (summarise).

:func:`drive` is a **real** loop — not glue deferred elsewhere. It logs the
op-config as the first row, prepares, then runs rounds until either stop-on-dry
fires (``stop(status='dry')``) or the budget cap is reached
(``stop(status='complete')``). A ``stop`` row is emitted on BOTH exit paths so a
driven ledger always ends with an explicit terminal status.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from peers.spine.ledger import RunLedger
from peers.spine.op_config import OpConfig, load_op_config
from peers.spine.stop_on_dry import should_stop


@dataclass
class ModeRun:
    """The binding for one run. ``ledger`` is opened lazily (and cached) at
    ``ledger_path`` so constructing a ModeRun touches no files."""

    tool: Path
    op_config: OpConfig
    ledger_path: Path
    mode_run: str
    branch: str | None = None                     # Stage 5: the run's isolated branch (None = legacy single-HEAD)
    base_sha: str | None = None                    # Stage 6: the run's recorded fork point (lease base; None = legacy)
    _ledger: RunLedger | None = field(default=None, init=False, repr=False)

    @property
    def ledger(self) -> RunLedger:
        if self._ledger is None:
            self._ledger = RunLedger(self.ledger_path)
        return self._ledger


@runtime_checkable
class ModeFrontend(Protocol):
    """What a mode must implement to be driven. Loop-agnostic: the spine calls
    these; the mode decides what each round does."""

    def prepare(self, run: ModeRun) -> None:
        """One-time setup before the rounds (e.g. infer the bar)."""
        ...

    def run(self, run: ModeRun) -> None:
        """Perform one round of work, appending its outcome row(s)."""
        ...

    def interpret(self, run: ModeRun) -> dict:
        """Summarise the run; the return value is drive()'s result."""
        ...


def drive(run: ModeRun, frontend: ModeFrontend) -> dict:
    """Drive one ModeRun to termination and return ``frontend.interpret(run)``.

    1. log the op-config (``run-start``),
    2. ``frontend.prepare(run)``,
    3. loop ``frontend.run(run)`` — after each round, stop with ``dry`` when
       :func:`should_stop` fires, or with ``complete`` once ``max_rounds`` rounds
       have run,
    4. return ``frontend.interpret(run)``.
    """
    load_op_config(run.op_config, run.ledger, mode_run=run.mode_run)
    frontend.prepare(run)

    max_rounds = run.op_config.budget.max_rounds
    dry_n = run.op_config.dry_n
    rounds = 0
    while True:
        frontend.run(run)
        rounds += 1
        try:
            rows = run.ledger.read()
        except (ValueError, OSError):
            # Fail closed: a corrupt/torn ledger must still terminate with an
            # explicit stop row rather than crash the driver (mirrors verify()'s
            # fail-closed posture). The append path tolerates a torn trailing
            # line (RunLedger._last_entry_sha), so the terminal row still lands.
            run.ledger.append(event="stop", status="aborted",
                              mode_run=run.mode_run)
            break
        if should_stop(rows, n=dry_n):
            run.ledger.append(event="stop", status="dry", mode_run=run.mode_run)
            break
        if rounds >= max_rounds:
            run.ledger.append(event="stop", status="complete",
                              mode_run=run.mode_run)
            break
    return frontend.interpret(run)


def run_isolated(repo, op_config, mode_run, frontend, provider, *, base=None) -> dict:
    """Lease an isolated worktree+branch for ``mode_run``, drive ``frontend``
    inside it, and tear the workspace down on EVERY exit (the spine's flock
    acquire-at-run / release-in-`finally` pattern, generalised to a worktree
    lease).

    The ``with`` block's ``__exit__`` runs ``provider.lease``'s ``finally``
    (worktree remove + lock release) even when ``drive()`` / the frontend raises
    a non-ledger error — ``drive()`` only catches ``ValueError``/``OSError`` from
    ``ledger.read()``, so a frontend ``RuntimeError`` propagates THROUGH it and
    out of the ``with``, which still releases. The crash-leak self-heal
    (prune-at-acquire) lives inside ``GitWorktreeProvider.lease``, so this
    inherits it transitively.
    """
    with provider.lease(repo, mode_run, base=base) as ws:
        run = ModeRun(tool=ws.worktree_path, op_config=op_config,
                      ledger_path=ws.worktree_path / ".peers" / "run.jsonl",
                      mode_run=mode_run, branch=ws.branch,
                      base_sha=ws.base_sha)         # Stage 6: thread the lease's recorded fork point
        return drive(run, frontend)
