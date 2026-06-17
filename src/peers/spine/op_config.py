"""STEP-3 — op-config: the operator's run intake.

The operator chooses *how* a run behaves (mode, landing, budget, stop-on-dry
threshold) but **never** the goal — the goal emerges from the tool's own bar
(decision §1.4). So the schema is a small, closed allow-list and an explicit
deny-list: any attempt to smuggle a `goal` / `charter` or a gate-relaxation key
(`disable_gate`, `trust_marker`, `proof_oracle`, …) is rejected. Defense in
depth: the deny-list catches the dangerous keys with a pointed message even if
the allow-list is ever widened.

`load_op_config` records the validated config as the FIRST ledger row — a
`run-start` entry whose witness is `{"kind": "op-config", …}` — so every run is
anchored to the intake the operator approved.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from peers.spine.ledger import LedgerEntry, RunLedger

#: The modes a run may request. Stage 0 ships no mode *logic* — these are the
#: labels Stage 1+ frontends bind to. Every entry MUST have a real frontend/builder
#: behind it (an allowed-but-unrunnable label is a dishonest surface): the fleet
#: artifact map (``peers.fleet.program.MODE_ARTIFACTS``) is kept in lock-step and
#: ``tests/unit/test_fleet_program.py::test_mode_artifact_map_is_complete`` pins it.
#:
#: Internal MODE_STACK overlays that run *as* one of the listed op-modes via a
#: private frontend are intentionally NOT listed here: adding such a label would
#: surface a mode with no public builder (a dishonest, incomplete surface). Only
#: standalone, publicly-buildable modes belong in this tuple.
ALLOWED_MODES = (
    "develop",
    "find-bugs:reproduce",
    "research",
    "bring-up",
)

#: Top-level keys the intake accepts. Anything else is rejected (allow-list).
_ALLOWED_KEYS = frozenset(
    {"mode", "landing", "evolve_features", "budget", "dry_n"},
)

#: Keys that are NEVER acceptable — a goal/charter the operator may not set, or
#: any gate-relaxation knob. Rejected with a pointed message even though the
#: allow-list above would already reject them (layered control).
_DENY_KEYS = frozenset({
    "goal", "charter", "objective", "task",
    "disable_gate", "skip_gate", "relax_gate", "gate", "gates",
    "trust_marker", "proof_oracle", "oracle",
    "allow_unattested", "override", "no_verify",
})

_ALLOWED_BUDGET_KEYS = frozenset({"max_rounds", "max_tokens"})


@dataclass
class Budget:
    """Run budget. ``max_rounds`` bounds the drive loop; ``max_tokens`` is an
    advisory ceiling the modes consume later (``None`` = unbounded here)."""

    max_rounds: int = 12
    max_tokens: int | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Budget":
        if not isinstance(d, dict):
            raise ValueError("budget must be a mapping")
        unknown = set(d) - _ALLOWED_BUDGET_KEYS
        if unknown:
            raise ValueError(f"unknown budget key(s): {sorted(unknown)}")
        max_rounds = d.get("max_rounds", 12)
        max_tokens = d.get("max_tokens", None)
        if not isinstance(max_rounds, int) or isinstance(max_rounds, bool) \
                or max_rounds < 1:
            raise ValueError("budget.max_rounds must be an int >= 1")
        if max_tokens is not None and (
                not isinstance(max_tokens, int) or isinstance(max_tokens, bool)
                or max_tokens < 1):
            raise ValueError("budget.max_tokens must be a positive int or None")
        return cls(max_rounds=max_rounds, max_tokens=max_tokens)


@dataclass
class OpConfig:
    """A validated operator intake. Build it ONLY via :meth:`from_dict`, which
    enforces the allow-list + deny-list."""

    mode: str
    landing: str = "branch-pr"
    evolve_features: bool = False
    budget: Budget = field(default_factory=Budget)
    dry_n: int = 3

    @classmethod
    def from_dict(cls, d: dict) -> "OpConfig":
        if not isinstance(d, dict):
            raise ValueError("op-config must be a mapping")

        # Layer 1: explicit deny-list (pointed message; survives allow-list widening).
        denied = set(d) & _DENY_KEYS
        if denied:
            raise ValueError(
                "op-config may not set goal/charter or any gate-relaxation key; "
                f"rejected: {sorted(denied)}",
            )
        # Layer 2: allow-list — anything unrecognised is rejected.
        unknown = set(d) - _ALLOWED_KEYS
        if unknown:
            raise ValueError(f"unknown op-config key(s): {sorted(unknown)}")

        if "mode" not in d:
            raise ValueError("op-config requires a 'mode'")
        mode = d["mode"]
        if mode not in ALLOWED_MODES:
            raise ValueError(
                f"unknown mode {mode!r}; allowed: {sorted(ALLOWED_MODES)}",
            )

        landing = d.get("landing", "branch-pr")
        if not isinstance(landing, str) or not landing:
            raise ValueError("landing must be a non-empty string")

        evolve = d.get("evolve_features", False)
        if not isinstance(evolve, bool):
            raise ValueError("evolve_features must be a bool")

        budget = Budget.from_dict(d.get("budget", {}))

        dry_n = d.get("dry_n", 3)
        if not isinstance(dry_n, int) or isinstance(dry_n, bool) or dry_n < 1:
            raise ValueError("dry_n must be an int >= 1")

        return cls(mode=mode, landing=landing, evolve_features=evolve,
                   budget=budget, dry_n=dry_n)

    def to_dict(self) -> dict:
        """The canonical, JSON-serialisable form (used for the witness digest)."""
        return {
            "mode": self.mode,
            "landing": self.landing,
            "evolve_features": self.evolve_features,
            "budget": {"max_rounds": self.budget.max_rounds,
                       "max_tokens": self.budget.max_tokens},
            "dry_n": self.dry_n,
        }

    def digest(self) -> str:
        """sha256 over the canonical config — anchors the run-start witness to
        exactly the intake the operator approved."""
        canon = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def load_op_config(cfg: OpConfig, ledger: RunLedger, *,
                   mode_run: str) -> LedgerEntry:
    """Append the op-config as the FIRST ledger row (``run-start``).

    Fails closed if the ledger already has rows — the op-config must anchor the
    run, never be slipped in mid-stream. The witness records the mode and a
    digest of the full canonical config.
    """
    if ledger.read():
        raise ValueError("op-config must be the first ledger row (ledger non-empty)")
    witness = {
        "kind": "op-config",
        "mode": cfg.mode,
        "landing": cfg.landing,
        "dry_n": cfg.dry_n,
        "sha256": cfg.digest(),
    }
    return ledger.append(event="run-start", status="ok", witness=witness,
                         mode_run=mode_run)
