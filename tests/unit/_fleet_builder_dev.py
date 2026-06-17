"""A throwaway fleet frontend builder for the integration capstone (NOT a test
module — underscore prefix). Registers a ``develop`` builder whose frontend makes
ONE real attested git commit on the run's leased branch + writes a CONVERGED
per-run ledger (no LLM). Imported by the spawned ``run_one`` child via the
``PEERS_FLEET_BUILDERS`` env hook, so the capstone exercises the real
spawn -> lease -> drive -> converge -> persist path with deterministic content.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from peers.fleet import run_one


def _git(p, *a):
    return subprocess.run(["git", "-C", str(p), *a],
                          capture_output=True, text=True, check=True).stdout


class _GitCommitConverge:
    def prepare(self, run):
        pass

    def run(self, run):
        from peers import attest
        wt = Path(run.tool)
        fname = f"{run.mode_run}_fix.py"
        (wt / fname).write_text("fix\n")
        _git(wt, "add", fname)
        _git(wt, "commit", "-q", "-m", f"work:{run.mode_run}")
        sha = _git(wt, "rev-parse", "HEAD").strip()
        attest.attest_commits(wt, "claude", run.base_sha, sha)
        run.ledger.append_attested(
            wt, sha, event="confirmed-work", subject="F1", status="pass",
            witness={"kind": "git-sha", "uri": sha, "sha256": sha},
            independence=True, mode_run=run.mode_run)

    def interpret(self, run):
        return {"converged": True}


def make_dev_frontend(spec):
    return _GitCommitConverge()


def install() -> None:
    """The PEERS_FLEET_BUILDERS hook contract: ``run_one`` calls this after import
    so merely importing this module (e.g. for ``_GitCommitConverge``) does NOT
    pollute the global registry for other tests."""
    run_one.register_frontend_builder("develop", make_dev_frontend)
