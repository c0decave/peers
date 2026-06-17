"""The real, process-spawning SlotRunner — the one Stage-7 conductor boundary.

The conductor injects a SlotRunner and calls only three methods: ``observe()``
(``{slot: run_id|None}``), ``liveness(run_id)`` (``"live"|"wedged"|"done"``), and
``start(slot, spec)``. :class:`ProcessSlotRunner` implements that contract over OS
subprocesses: each ``start`` spawns one child (``python -m peers.fleet.run_one``)
that leases its OWN isolated worktree and ``drive()``s one run; the parent learns
the child's worktree/branch/ledger from the host-discoverable spine-runs registry
(``<tool>/.peers/spine-runs/<run_id>.json``) and chooses the lease base itself
(``git rev-parse HEAD`` of the tool at spawn — the registry record omits it).

Reaping is TWO-PHASE so the conductor always sees a finished run ONCE (to
transition it on its own ledger) before its slot frees: a child exit makes
``liveness`` return ``"done"`` (marking the slot ``done-reported``); the NEXT
``observe`` drops it. ``liveness`` is idle-based (rolling, NOT a wall-clock kill —
the "idle-timeout, not total-runtime" rule): a live child whose ledger/registry
has not advanced within ``idle_timeout_s`` is ``"wedged"`` and the conductor
restarts it (``start`` kills the stuck child first).

No LLM/container logic lives here; the child is the run. The ``launch`` boundary
is injected (default :func:`build_run_one_argv` + ``Popen``) so the daemon is
deterministically testable with a fake child.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from peers.fleet.program import ModeRunSpec
from peers.spine.op_config import OpConfig
from peers.spine.mode_run import ModeRun

_SPINE_RUNS_DIR = "spine-runs"


def build_run_one_argv(spec: ModeRunSpec, base_sha: str) -> list[str]:
    """The production child argv: ``python -m peers.fleet.run_one --spec <json>``.

    The spec is serialised to a self-contained JSON payload (run_id / tool / mode
    / op_config / base_sha) so the child reconstructs the ModeRun with no shared
    in-process state. Pure + deterministic (the actual ``Popen`` is exercised
    live)."""
    payload = {
        "run_id": spec.run_id,
        "tool": str(spec.tool),
        "mode": spec.mode,
        "op_config": spec.op_config.to_dict(),
        "base_sha": base_sha,
        "branch": spec.branch,
    }
    return [sys.executable, "-m", "peers.fleet.run_one", "--spec",
            json.dumps(payload)]


def _default_launch(spec: ModeRunSpec, base_sha: str) -> subprocess.Popen:
    """Spawn the production child in its OWN process group (so a wedged run's whole
    tree is killable) with cwd = the tool repo."""
    return subprocess.Popen(
        build_run_one_argv(spec, base_sha),
        cwd=str(spec.tool), start_new_session=True)


def _git_head(repo: Path) -> str:
    """``git rev-parse HEAD`` of ``repo`` — the parent-chosen lease base. Raises on
    failure (fail-closed: a run cannot start without a fork point)."""
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=120, check=True).stdout.strip()


def _terminate(proc: subprocess.Popen) -> None:
    """Kill ``proc``'s whole process group (SIGTERM, then SIGKILL after a grace),
    fail-soft on a race where it already exited."""
    if proc.poll() is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate() if sig == signal.SIGTERM else proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue
    # Both per-signal waits timed out (a slow/D-state child). Make a FINAL,
    # longer reap attempt so a SIGKILL'd-but-slow process is not abandoned as a
    # zombie (the prior code returned here without ever reaping it).
    try:
        proc.wait(timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        pass


@dataclass
class _SlotState:
    run_id: str
    proc: subprocess.Popen
    base_sha: str
    op_config: OpConfig
    started_at: float
    done_reported: bool = False


class ProcessSlotRunner:
    """Manage one OS subprocess per slot; satisfy the conductor's SlotRunner
    Protocol; reconstruct ModeRun objects from the spine-runs registry."""

    def __init__(self, pool, repos_by_id, *, idle_timeout_s=900, launch=None,
                 now=None, git_head=None):
        self._slots: dict[str, _SlotState | None] = {s: None for s in pool.slots}
        self._repos = dict(repos_by_id)
        self.idle_timeout_s = idle_timeout_s
        self._launch = launch or _default_launch
        self._now = now or time.time
        self._git_head = git_head or _git_head

    # ---- the conductor Protocol ---------------------------------------
    def start(self, slot, spec) -> None:
        """Launch ``spec`` on ``slot``. If the slot already holds a (wedged) run,
        its child is killed first (the conductor's restart path)."""
        if slot not in self._slots:
            raise KeyError(f"unknown slot {slot!r}")
        prev = self._slots[slot]
        if prev is not None:
            _terminate(prev.proc)
        repo = self._repos.get(spec.run_id, spec.tool)
        try:
            base_sha = self._git_head(Path(repo))
        except (subprocess.SubprocessError, OSError) as e:
            # A per-run repo failure (deleted/corrupt repo, timeout) raises a CLEAR
            # error. (The conductor has no per-run start-failure channel, so this
            # surfaces fail-safe as the daemon's 'aborted' terminal with a precise
            # cause rather than a cryptic CalledProcessError. Finer-grained per-run
            # failure is a documented follow-up — see the design doc.)
            raise RuntimeError(
                f"cannot resolve base for run {spec.run_id!r} in {repo}: {e}") from e
        proc = self._launch(spec, base_sha)
        self._slots[slot] = _SlotState(
            run_id=spec.run_id, proc=proc, base_sha=base_sha,
            op_config=spec.op_config, started_at=self._now())

    def observe(self) -> dict:
        """``{slot: run_id|None}``. A slot whose run was already reported ``done``
        is REAPED here (two-phase) so the conductor saw it once before it frees."""
        out: dict[str, str | None] = {}
        for slot, st in self._slots.items():
            if st is None:
                out[slot] = None
            elif st.done_reported:
                self._slots[slot] = None
                out[slot] = None
            else:
                out[slot] = st.run_id
        return out

    def liveness(self, run_id) -> str:
        """``"done"`` if the child exited (marks the slot done-reported),
        ``"wedged"`` if a live child has been idle past ``idle_timeout_s``, else
        ``"live"``. An unknown run is ``"done"`` (treated as gone — fail-safe)."""
        st = self._find(run_id)
        if st is None:
            return "done"
        if st.proc.poll() is not None:
            st.done_reported = True
            return "done"
        now = self._now()
        last = st.started_at
        for f in self._activity_files(run_id):
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            # Only fold in an mtime in the SAME time domain as now(): a mtime in
            # the future relative to now() is a clock-domain mismatch (an injected
            # fake clock vs real wall-clock file mtimes) — ignore it rather than
            # let it mask a genuinely-idle run (clock-mixing hazard).
            if mtime <= now:
                last = max(last, mtime)
        if self.idle_timeout_s is not None and now - last > self.idle_timeout_s:
            return "wedged"
        return "live"

    # ---- daemon helpers ----------------------------------------------
    def run_for(self, run_id) -> ModeRun | None:
        """Reconstruct the run's ModeRun. Prefers the STABLE persisted fleet-run
        (``<repo>/.peers/fleet-runs/<id>/``) — its ledger + converged commit
        survive the lease teardown, so the conductor can re-verify/diff/land a
        FINISHED run. Falls back to the live spine-runs registry (worktree) while
        the run is still in flight. ``None`` for an unknown/reaped run."""
        st = self._find(run_id)
        if st is None:
            return None
        persisted = self._read_fleet_run(run_id)
        if persisted is not None and persisted.get("ledger_path"):
            # HONEST-01: branch = the persisted ref (refs/peers/fleet/<id>, pinned
            # to the run's real tip) so is_converged anchors attest-reachability on
            # a SURVIVING, substrate-pinned ref — the original peers/run/<id> branch
            # is `git branch -D`'d at teardown. Do NOT fall back to that deleted
            # branch when the pin failed (persisted['ref'] is None): a None anchor
            # makes is_converged default to HEAD and fail CLOSED, rather than
            # anchoring on a non-existent ref.
            return ModeRun(
                tool=Path(self._repos.get(run_id, ".")), op_config=st.op_config,
                ledger_path=Path(persisted["ledger_path"]), mode_run=run_id,
                branch=persisted.get("ref"), base_sha=st.base_sha)
        rec = self._read_registry(run_id)
        if rec is None:
            return None
        wt = Path(rec["worktree_path"])
        return ModeRun(
            tool=wt, op_config=st.op_config,
            ledger_path=Path(rec.get("ledger_path", wt / ".peers" / "run.jsonl")),
            mode_run=run_id, branch=rec.get("branch"), base_sha=st.base_sha)

    def runs_by_id(self) -> dict:
        """Every live slot's run as a ModeRun (skips runs whose child has not yet
        written its registry record)."""
        out = {}
        for st in self._slots.values():
            if st is None:
                continue
            run = self.run_for(st.run_id)
            if run is not None:
                out[st.run_id] = run
        return out

    def shutdown(self) -> None:
        """Kill every live child (clean daemon exit / test teardown)."""
        for st in self._slots.values():
            if st is not None:
                _terminate(st.proc)

    def _proc_of(self, run_id) -> subprocess.Popen | None:
        st = self._find(run_id)
        return st.proc if st is not None else None

    # ---- internals ----------------------------------------------------
    def _find(self, run_id) -> _SlotState | None:
        for st in self._slots.values():
            if st is not None and st.run_id == run_id:
                return st
        return None

    def _registry_path(self, run_id) -> Path:
        repo = Path(self._repos.get(run_id, "."))
        return repo / ".peers" / _SPINE_RUNS_DIR / f"{run_id}.json"

    def _read_fleet_run(self, run_id) -> dict | None:
        """The STABLE persisted fleet-run record (survives worktree teardown)."""
        repo = Path(self._repos.get(run_id, "."))
        rec = repo / ".peers" / "fleet-runs" / run_id / "record.json"
        try:
            if rec.is_symlink() or not rec.exists():
                return None
            data = json.loads(rec.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _read_registry(self, run_id) -> dict | None:
        rec = self._registry_path(run_id)
        try:
            if rec.is_symlink() or not rec.exists():
                return None
            data = json.loads(rec.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _activity_files(self, run_id):
        """The files whose mtime marks run progress: the registry record + the
        leased worktree's run ledger (resolved via the record)."""
        files = []
        rec_path = self._registry_path(run_id)
        files.append(rec_path)
        rec = self._read_registry(run_id)
        if rec is not None and rec.get("ledger_path"):
            files.append(Path(rec["ledger_path"]))
        return files
