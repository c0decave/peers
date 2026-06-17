"""STEP-1 — the develop-mode ports: the seam every later task depends on.

develop reaches its three capabilities — discovering findings (AUDIT), turning
survivors into a frozen implement contract (AUTHOR), and converging that
contract to a real commit (IMPLEMENT) — through **injected Protocols**, exactly
as the Stage-0 spine injected its test-runner (``direction.infer_bar``) and its
refuter (``adversarial_verify.verify_claim``). Keeping these as
``runtime_checkable`` Protocols (not base classes) means the LLM/subprocess
adapters stay thin and swappable, and the orchestration in
:mod:`peers.develop.frontend` is unit-testable with trivial fakes.

The :class:`Finding` here is the *develop* finding — deliberately distinct from
:class:`peers.bug_hunt.BugReport`; a real :class:`Auditor` adapter may map one
to the other, but the orchestration depends only on this shape.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class Finding:
    """One audit finding develop may act on.

    ``id`` is a stable handle used as the verify subject and the ledger
    ``subject``; ``fail_first`` is the acceptance hint AUTHOR turns into a
    fail-first test (develop never edits freehand — every change is gated by an
    authored, frozen, fail-first contract).
    """

    id: str
    dimension: str
    severity: str
    location: str
    summary: str
    fix: str
    fail_first: str


@dataclass
class AuthoredContract:
    """A parser-valid implement contract AUTHOR produced from survivors.

    ``plan_md`` is a full PLAN.md body (``parse_plan`` must accept it);
    ``acceptance``/``e2e`` are the contract scripts frozen by
    ``write_frozen_contracts``; ``findings`` are the source finding ids (the
    first becomes the confirmed-work ledger subject).
    """

    plan_md: str
    acceptance: str
    findings: list[str] = field(default_factory=list)
    e2e: str | None = None


@dataclass
class ImplementResult:
    """The outcome of IMPLEMENTing a contract.

    ``ok`` is convergence; ``head_sha`` is the resulting commit (only a sha that
    resolves to a real 40-hex commit becomes a witnessed confirmed-work — see
    :mod:`peers.develop.frontend`); ``branch`` is the branch-PR landing target;
    ``reason`` explains a non-converged result.
    """

    ok: bool
    head_sha: str | None = None
    branch: str | None = None
    reason: str = ""


@runtime_checkable
class Auditor(Protocol):
    """Discovers findings over a repo for the requested dimensions."""

    def audit(self, repo: Path, dimensions: list[str]) -> list[Finding]:
        ...


@runtime_checkable
class Author(Protocol):
    """Turns surviving findings into a frozen-able implement contract, or
    ``None`` when it cannot produce a parser-valid contract (the finding then
    stays a report — a dry round)."""

    def author(self, findings: list[Finding], repo: Path) -> AuthoredContract | None:
        ...


@runtime_checkable
class Implementer(Protocol):
    """Converges a contract to a real commit (or reports why it did not)."""

    def implement(self, contract: AuthoredContract, repo: Path) -> ImplementResult:
        ...
