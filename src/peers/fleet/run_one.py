"""``python -m peers.fleet.run_one --spec <json>`` — the per-run child the fleet
SlotRunner spawns for each slot.

It deserialises a self-contained fleet run spec, resolves a mode frontend from a
**fail-closed registry**, and ``run_isolated()``s it: leases the run's own
isolated worktree+branch (at the parent-chosen ``base_sha``) and ``drive()``s the
frontend, writing the per-run ledger the conductor later re-verifies.

The registry seam is the live-integration boundary. It is EMPTY by default, so an
un-wired mode FAILS CLOSED (clear message, non-zero exit) — NEVER a silent
degraded run that the convergence gate might then trust. Per-mode builders (which
assemble each mode's components exactly like ``make_bring_up_frontend`` does for
bring-up — corpus adapters / oracles / readers, and need live LLM peers) are
registered via :func:`register_frontend_builder`; that wiring is the documented
follow-up (``docs/plans/2026-06-13-fleet-daemon-design.md`` §cross-tool seam /
further-live).
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

from peers.spine.op_config import OpConfig

#: mode -> builder(spec_dict) -> ModeFrontend. Empty by default (fail-closed).
_FRONTEND_BUILDERS: dict[str, Callable[[dict], object]] = {}

_REQUIRED_FIELDS = ("run_id", "tool", "mode", "op_config", "base_sha")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class UnsupportedFleetMode(Exception):
    """A mode with no registered fleet frontend builder (fail-closed)."""


def register_frontend_builder(mode: str, builder: Callable[[dict], object]) -> None:
    """Register ``builder`` as the fleet frontend factory for ``mode``."""
    _FRONTEND_BUILDERS[mode] = builder


def _load_env_builders(spec_arg: str | None = None) -> None:
    """Load per-mode frontend builders named in ``PEERS_FLEET_BUILDERS`` (or the
    given ``spec_arg``), comma-separated import paths. Each module is imported and,
    if it defines ``install()``, that is called (the plugin contract — so a module
    may register without an import side effect). Fail-soft: a module that won't
    import/install simply leaves its mode unregistered (then fail-closed)."""
    names = spec_arg if spec_arg is not None else os.environ.get(
        "PEERS_FLEET_BUILDERS", "")
    for name in [n.strip() for n in (names or "").split(",") if n.strip()]:
        # Snapshot the registry so a module that registers at IMPORT time (against
        # the install()-only contract) and then fails is fully ROLLED BACK — a
        # half-registered mode would otherwise defeat the fail-closed guarantee.
        before = dict(_FRONTEND_BUILDERS)
        try:
            mod = importlib.import_module(name)
            install = getattr(mod, "install", None)
            if callable(install):
                install()
        except Exception as e:                   # noqa: BLE001 — fail-soft plugin load
            _FRONTEND_BUILDERS.clear()
            _FRONTEND_BUILDERS.update(before)
            print(f"run_one: builder {name!r} failed to load: {e}", file=sys.stderr)


def default_factory(spec: dict):
    """Resolve a frontend for ``spec['mode']`` from the registry, fail-closed."""
    mode = spec.get("mode")
    builder = _FRONTEND_BUILDERS.get(mode) if isinstance(mode, str) else None
    if builder is None:
        raise UnsupportedFleetMode(
            f"mode {mode!r} is not wired for fleet execution; register a frontend "
            f"builder via peers.fleet.run_one.register_frontend_builder")
    return builder(spec)


def parse_spec(spec_json: str) -> dict:
    """Parse + validate a serialised fleet run spec. Raises :class:`ValueError`
    on bad JSON, a non-mapping, or a missing required field (fail-closed)."""
    try:
        spec = json.loads(spec_json)
    except (ValueError, TypeError) as e:
        raise ValueError(f"spec is not valid JSON: {e}") from e
    if not isinstance(spec, dict):
        raise ValueError("spec must be a JSON object")
    for field in _REQUIRED_FIELDS:
        if field not in spec:
            raise ValueError(f"spec missing required field {field!r}")
    if not isinstance(spec["op_config"], dict):
        raise ValueError("spec op_config must be a mapping")
    # Defense-in-depth value validation (the program already validates run_id, but
    # run_id flows into a git ref name + a dir path here, and a bad base/tool would
    # only fail deep inside the lease wasting resources):
    run_id = spec["run_id"]
    if (not isinstance(run_id, str) or not run_id or run_id in (".", "..")
            or "/" in run_id or "\\" in run_id or "\x00" in run_id):
        raise ValueError(f"spec run_id must be a safe single path component, got {run_id!r}")
    if not isinstance(spec["base_sha"], str) or not _SHA_RE.match(spec["base_sha"]):
        raise ValueError(f"spec base_sha must be a 40-hex git sha, got {spec['base_sha']!r}")
    tool = spec["tool"]
    if not isinstance(tool, str) or not tool or not Path(tool).is_dir():
        raise ValueError(f"spec tool must be an existing directory, got {tool!r}")
    return spec


def _persist_fleet_run(repo: Path, run_id: str, run, ws) -> None:
    """Copy the per-run ledger out of the (about-to-be-torn-down) worktree to a
    STABLE location, and pin the converged commit under ``refs/peers/fleet/<id>``
    so it survives ``git worktree remove`` + ``git branch -D`` — the conductor
    re-verifies convergence + diffs/lands it AFTER teardown, so both must persist.
    Worktrees share the parent ODB, so the ref keeps the commit reachable."""
    from peers.spine.propagate import _converged_commit

    stable = repo / ".peers" / "fleet-runs" / run_id
    stable.mkdir(parents=True, exist_ok=True)
    if Path(run.ledger_path).exists():
        shutil.copy2(run.ledger_path, stable / "run.jsonl")
    # HONEST-01: pin the run's REAL branch tip (substrate-observed via rev-parse,
    # NOT the agent-writable ledger's _converged_commit claim) under
    # refs/peers/fleet/<id>, surviving `git branch -D`. The conductor anchors
    # attest-reachability on this ref, so a forged peers-attest note on a dangling
    # commit (not on the real tip) is rejected post-teardown too.
    real_tip = None
    if ws.branch:
        r = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
             f"{ws.branch}^{{commit}}"],
            capture_output=True, text=True, timeout=120, check=False)
        if r.returncode == 0 and len(r.stdout.strip()) == 40:
            real_tip = r.stdout.strip()
    # The pinned ref is the SOLE post-teardown reachability anchor, so record it
    # ONLY when the update-ref actually SUCCEEDED (adversarial-review CRITICAL:
    # silently ignoring update-ref's rc records a phantom ref that does not exist,
    # so the conductor would anchor on a non-existent ref). On any pin failure the
    # ref is None -> run_for cannot anchor -> the run fails closed at re-verify.
    ref = f"refs/peers/fleet/{run_id}"
    pinned = False
    if real_tip:
        up = subprocess.run(
            ["git", "-C", str(repo), "update-ref", ref, real_tip],
            capture_output=True, text=True, timeout=120, check=False)
        pinned = up.returncode == 0
    converged = None
    try:
        converged = _converged_commit(run.ledger.read())
    except (OSError, ValueError):
        converged = None
    record = {
        "run_id": run_id, "branch": ws.branch, "base_sha": ws.base_sha,
        "converged_commit": converged, "real_tip": real_tip,
        "ledger_path": str(stable / "run.jsonl"),
        "ref": ref if pinned else None,
    }
    (stable / "record.json").write_text(json.dumps(record))


def _lease_drive_persist(spec: dict, op_config, frontend, provider) -> None:
    """Lease the run's isolated worktree, ``drive`` the frontend, then persist the
    ledger + converged commit BEFORE the lease ``finally`` tears the worktree down
    (the spine acquire-at-lease / release-in-finally pattern). A frontend error
    propagates THROUGH (the lease still releases); persistence is skipped (no
    converged artifact to keep)."""
    from peers.spine.mode_run import ModeRun, drive

    repo = Path(spec["tool"])
    run_id = spec["run_id"]
    with provider.lease(repo, run_id, base=spec["base_sha"]) as ws:
        run = ModeRun(
            tool=ws.worktree_path, op_config=op_config,
            ledger_path=ws.worktree_path / ".peers" / "run.jsonl",
            mode_run=run_id, branch=ws.branch, base_sha=ws.base_sha)
        drive(run, frontend)
        _persist_fleet_run(repo, run_id, run, ws)


def main(argv=None, *, factory=None, provider=None) -> int:
    """Drive one fleet run to termination. Returns 0 on a clean drive, 2 on a
    spec/registry (caller) error, 1 on a run-time failure (the worktree is still
    torn down — the lease releases on every exit)."""
    parser = argparse.ArgumentParser(prog="peers.fleet.run_one")
    parser.add_argument("--spec", required=True,
                        help="serialised fleet run spec (JSON)")
    args = parser.parse_args(argv)

    try:
        spec = parse_spec(args.spec)
    except ValueError as e:
        print(f"run_one: {e}", file=sys.stderr)
        return 2

    if factory is None:
        _load_env_builders()                     # per-mode plugins (production path)
        factory = default_factory
    try:
        frontend = factory(spec)
    except UnsupportedFleetMode as e:
        print(f"run_one: {e}", file=sys.stderr)
        return 2

    try:
        op_config = OpConfig.from_dict(spec["op_config"])
    except ValueError as e:
        print(f"run_one: bad op_config: {e}", file=sys.stderr)
        return 2

    if provider is None:
        from peers.spine.worktree import GitWorktreeProvider
        provider = GitWorktreeProvider()

    try:
        _lease_drive_persist(spec, op_config, frontend, provider)
    except Exception as e:                       # noqa: BLE001 — child boundary: any
        # run failure is reported fail-closed (the conductor re-verifies the
        # ledger and records 'failed'); the lease already released the worktree.
        print(f"run_one: run {spec['run_id']!r} failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":                        # pragma: no cover
    # Delegate to the CANONICAL module: run as `python -m peers.fleet.run_one`
    # this file is imported as ``__main__``, but a builder module loaded via
    # PEERS_FLEET_BUILDERS imports ``peers.fleet.run_one`` (a SECOND instance with
    # its own registry). Run main() off the canonical instance so the registry the
    # builder populates is the one default_factory reads.
    from peers.fleet import run_one as _canonical

    raise SystemExit(_canonical.main())
