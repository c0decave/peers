"""Shared fixtures for the Stage-7 fleet unit suite (STEPs 1-7 reuse).

Not a test module (underscore prefix) -- imported as
``from tests.unit._fleet_helpers import ...``. Reuses tests/unit/_isolation_helpers
for the real-git tmp_path repos.

STEP-1 lands the program builders + the injected ``SlotRunner`` fake. The
``FleetLedger``-backed ``_fleet_ledger`` builder is added when STEP-2 lands
``peers.fleet.fleet_ledger`` (importing it now would break the STEP-1 suite with
an ImportError on a not-yet-built module).
"""
from __future__ import annotations

from peers.fleet.program import ModeRunSpec, Program
from peers.fleet.fleet_ledger import FleetLedger
from peers.spine.op_config import OpConfig


def _spec(run_id, *, tool, mode="develop", depends_on=None, affinity=None,
          writable=True, landing="branch-pr", max_tokens=None,
          requires_artifact=None):
    """One ModeRunSpec with a real OpConfig (the validator checks the mode against
    op_config.ALLOWED_MODES + the artifact map). ``max_tokens`` sets a REAL
    op_config.budget.max_tokens so the ceiling tests can exercise the live
    projection path (no injected ``projected`` dict) -- without this every run
    projects 0 and the production ceiling is dead (blocker F5-1)."""
    d = {"mode": mode, "landing": landing}
    if max_tokens is not None:
        d["budget"] = {"max_tokens": max_tokens}
    return ModeRunSpec(
        tool=tool, mode=mode,
        op_config=OpConfig.from_dict(d),
        run_id=run_id, depends_on=list(depends_on or []),
        affinity=affinity, writable=writable, requires_artifact=requires_artifact)


def _program(*specs):
    return Program(runs=list(specs))


def _fleet_ledger(tmp_path, name="fleet.jsonl"):
    """A fleet-ledger over a tmp JSONL (the spine RunLedger primitives). For the
    pure-function tests (satisfy/scheduler/invalidate) the ledger is built by
    appending fleet rows directly; the conductor tests drive it through the tick."""
    return FleetLedger(tmp_path / name)


# ---- The injected SlotRunner fake (the one Stage-7 boundary) ----
class FakeSlotRunner:
    """Implements the SlotRunner Protocol WITHOUT containers/subprocess. `start`
    records (slot, run_id) into a scripted observed world; `observe` returns it;
    `liveness` returns a scripted verdict per run_id (default 'live'). A test can
    pre-seed the world (a run the ledger does NOT know about => divergence) and
    script liveness ('wedged' => restart, 'done' => terminal)."""

    def __init__(self, *, slots, world=None, liveness=None):
        self.slots = list(slots)                       # e.g. ["s0", "s1"]
        self.world = dict(world or {})                 # {slot: run_id|None}
        self._liveness = dict(liveness or {})          # {run_id: verdict}
        self.started = []                              # [(slot, run_id)] in order

    def start(self, slot, spec):
        self.started.append((slot, spec.run_id))
        self.world[slot] = spec.run_id

    def observe(self):
        # the ACTUAL world: every slot, mapped to the run on it (or None).
        return {s: self.world.get(s) for s in self.slots}

    def liveness(self, run_id):
        return self._liveness.get(run_id, "live")

    # test helpers (NOT part of the Protocol) ----
    def set_world(self, slot, run_id):
        self.world[slot] = run_id

    def set_liveness(self, run_id, verdict):
        self._liveness[run_id] = verdict
