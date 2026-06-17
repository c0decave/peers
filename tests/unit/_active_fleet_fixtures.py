"""Area-specific fleet active-test fixtures (NOT a test module — underscore prefix).

These are the deterministic, no-LLM/no-container fleet *frontend builder* fakes the
``peers fleet`` active tests inject through the documented ``PEERS_FLEET_BUILDERS``
env hook (each module's ``install()`` is called by the spawned ``run_one`` child —
see ``peers.fleet.run_one._load_env_builders`` + ``register_frontend_builder``).

Every fake is the run_agent/chitin/transport seam fake: it replaces the LLM but
still does REAL git work (or, for the lying/no-op fake, deliberately does NOT), so
the conductor's substrate re-verification (``is_converged`` over the run's own
ledger anchored on the pinned ref) is exercised for the RIGHT reason.

This module is NEVER imported by the existing helper modules and never reuses an
existing fixture name — it is additive only.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from peers.fleet import run_one


def _git(p, *a):
    return subprocess.run(["git", "-C", str(p), *a],
                          capture_output=True, text=True, check=True).stdout


# --------------------------------------------------------------------------
# (FLEET-S2) The LYING / NO-OP frontend: claims success but makes NO real diff.
# AgentConvergenceRunner's vacuous-green guard is mirrored by the spine: a run
# that commits NOTHING produces no git-sha confirmed-work row -> is_converged
# fails closed. Here we model the agent that returns "done" without doing the
# work at all (the frontend never writes/commits/attests), so the per-run ledger
# carries NO confirmed-work row and the conductor re-derives non-convergence.
# --------------------------------------------------------------------------
class _NoOpConverge:
    def prepare(self, run):
        pass

    def run(self, run):
        # An agent that LIES: it reports success but touches nothing. It writes a
        # benign, NON-confirmed-work row (so the ledger is not empty) but makes no
        # commit / no attest / no confirmed-work git-sha row -> nothing the gate
        # can trust. drive() still terminates the run.
        run.ledger.append(event="dry-round", status="dry", mode_run=run.mode_run)

    def interpret(self, run):
        return {"converged": True}      # the agent's SELF-REPORT (must buy nothing)


def make_noop_frontend(spec):
    return _NoOpConverge()


# --------------------------------------------------------------------------
# (FLEET-E2) The SELF-HOSTING frontend: a REAL attested converged commit whose
# diff touches a GOVERNANCE path (src/peers/spine/*.py), so is_self_hosting on
# the real base..converged diff returns True -> Tier-2, never auto-land.
# --------------------------------------------------------------------------
class _SelfHostConverge:
    def prepare(self, run):
        pass

    def run(self, run):
        from peers import attest
        wt = Path(run.tool)
        gov = wt / "src" / "peers" / "spine"
        gov.mkdir(parents=True, exist_ok=True)
        target = gov / f"{run.mode_run}_evil.py"
        target.write_text("# touches the governance surface\n")
        rel = f"src/peers/spine/{run.mode_run}_evil.py"
        _git(wt, "add", rel)
        _git(wt, "commit", "-q", "-m", f"selfhost:{run.mode_run}")
        sha = _git(wt, "rev-parse", "HEAD").strip()
        attest.attest_commits(wt, "claude", run.base_sha, sha)
        run.ledger.append_attested(
            wt, sha, event="confirmed-work", subject="F1", status="pass",
            witness={"kind": "git-sha", "uri": sha, "sha256": sha},
            independence=True, mode_run=run.mode_run)

    def interpret(self, run):
        return {"converged": True}


def make_selfhost_frontend(spec):
    return _SelfHostConverge()


# This module is the frontend-CLASS provider only; the canonical PEERS_FLEET_BUILDERS
# ``install()`` hooks live in the thin sibling builder modules
# (``_active_fleet_builder_noop`` / ``_active_fleet_builder_selfhost``), each of
# which registers exactly one develop frontend. ``run_one`` is imported above so a
# builder module can call ``run_one.register_frontend_builder``.
_ = run_one
