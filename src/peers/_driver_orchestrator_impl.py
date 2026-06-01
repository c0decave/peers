"""External orchestrator driver: a tight Python while-loop."""
from __future__ import annotations

import fcntl
import os
import signal
import subprocess as subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from peers.anti_cheat_guard import (
    AntiCheatGuard as AntiCheatGuard,
    _TEST_ONLY_PATH_RE as _TEST_ONLY_PATH_RE,
    is_test_only_commit as is_test_only_commit,
)
from peers.budget_accountant import (
    BudgetCheck as BudgetCheck,
    _CONFIGURABLE_BUDGET_LIMITS as _CONFIGURABLE_BUDGET_LIMITS,
    _TOKEN_PARSERS as _TOKEN_PARSERS,
    _apply_config_budget as _apply_config_budget,
    apply_operator_budget_overrides as apply_operator_budget_overrides,
    _parse_claude_json_envelope as _parse_claude_json_envelope,
    _parse_claude_tokens as _parse_claude_tokens,
    _parse_codex_tokens as _parse_codex_tokens,
    _warn_once as _warn_once,
    account_tokens_usd as account_tokens_usd,
    record_tick_accounting as record_tick_accounting,
)
from peers.bug_hunt import (
    count_new_blocking_or_flag_bug_reports
    as count_new_blocking_or_flag_bug_reports,
)
from peers.comm_layer import GitCommLayer, HybridCommLayer
from peers.driver_helpers import (
    PHASE_IMPLEMENTATION as PHASE_IMPLEMENTATION,
    _AUTO_SKEPTIC_PROMPT_PREFIX as _AUTO_SKEPTIC_PROMPT_PREFIX,
    _detect_mode_name,
    _extract_first_json_object as _extract_first_json_object,
    _format_tick_status as _format_tick_status,
    _hash_goals_yaml as _hash_goals_yaml,
    _load_phase_prompt as _load_phase_prompt,
    _resolve_peer_role as _resolve_peer_role,
    _resolve_phase as _resolve_phase,
    _should_checkpoint as _should_checkpoint,
)
from peers.driver_lifecycle import DriverLifecycleMixin
from peers.driver_observability import DriverObservabilityMixin
from peers.driver_peer_health import DriverPeerHealthMixin
from peers.driver_soft_reviews import DriverSoftReviewsMixin
from peers.driver_tick_hooks import DriverTickHooksMixin
from peers.goal_engine import GoalEngine, GoalResult as GoalResult
from peers.goals import Goal, _GOALS_YAML_MAX_BYTES as _GOALS_YAML_MAX_BYTES
from peers.health_guard import HealthGuard, RunResult as RunResult
from peers.peer_spec import PeerSpec
from peers.prompt_builder import build_prompt as build_prompt
from peers.recon import run_recon as _run_recon  # noqa: F401
from peers.regression_baseline import ensure_baseline_snapshot
from peers.safe_io import (
    _ensure_private_dir,
    _open_private_nested_dir_fd_no_symlink
    as _open_private_nested_dir_fd_no_symlink,
    _write_text_in_private_nested_dir_no_symlink
    as _write_text_in_private_nested_dir_no_symlink,
    append_text_in_dir_no_symlink as append_text_in_dir_no_symlink,
    open_text_no_symlink,
    read_bytes_no_symlink as read_bytes_no_symlink,
    read_text_no_symlink as read_text_no_symlink,
    write_text_no_symlink as write_text_no_symlink,
)
from peers.skeptic_engine import (
    PHASE_B_SKEPTIC_GATES as PHASE_B_SKEPTIC_GATES,
    SkepticEngine as SkepticEngine,
    _resolve_convergence_state as _resolve_convergence_state,
)
from peers.state_store import (
    StateStore,
    current_peer_name as current_peer_name,
    release_run_lock,
)
from peers.tick_loop import TickLoop
from peers.turn_manager import TurnManager, sweep_legacy_handoff_msg


class OrchestratorDriver(
    DriverTickHooksMixin,
    DriverSoftReviewsMixin,
    DriverPeerHealthMixin,
    DriverObservabilityMixin,
    DriverLifecycleMixin,
):
    """Wires substrate parts together into a tick loop."""

    def __init__(
        self,
        repo: Path,
        peer_dir: Path,
        goals: list[Goal],
        peer_specs: list[PeerSpec],
        state_store: StateStore | None = None,
        idle_timeout_s: int = 15 * 60,
        absolute_max_runtime_s: int = 2 * 3600,
        cfg_budget: dict[str, Any] | None = None,
        error_patterns: Sequence[str] | None = None,
        halt_patterns: Sequence[str] | None = None,
        dry_run: bool = False,
        comm_variant: str = "git",
        buf_cap_bytes: int = 2 * 1024 * 1024,
        goals_timeout_s: int = 120,
        verbose: bool = False,
        recon_enabled: bool = True,
        auto_skeptic_enabled: bool = True,
    ) -> None:
        if len(peer_specs) < 2:
            raise ValueError(
                f"need at least 2 peers, got {len(peer_specs)}"
            )
        self.repo = Path(repo)
        self.peer_dir = Path(peer_dir)
        self.goals = goals
        self.peer_specs = peer_specs
        self.peer_names = [p.name for p in peer_specs]
        self.peers_by_name: dict[str, PeerSpec] = {
            p.name: p for p in peer_specs
        }
        self.state_store = state_store or StateStore(
            self.peer_dir / "state.json",
            peer_order=self.peer_names,
        )
        self.health = HealthGuard(self.repo)
        self.engine = GoalEngine(goals, cwd=self.repo,
                                 timeout_s=goals_timeout_s)
        if comm_variant == "hybrid":
            self.comm = HybridCommLayer(self.repo, self.peer_dir)
        elif comm_variant == "git":
            self.comm = GitCommLayer(self.repo)
        else:
            raise ValueError(
                f"unknown comm_variant {comm_variant!r}; expected "
                "'git' or 'hybrid'"
            )
        self.comm_variant = comm_variant
        self.idle_timeout_s = idle_timeout_s
        self.absolute_max_runtime_s = absolute_max_runtime_s
        self.error_patterns = list(error_patterns or [])
        # halt_patterns trigger an immediate peer-unavailable
        # exit instead of degraded-on-retry. Intended for AUTH/QUOTA.
        self.halt_patterns = list(halt_patterns or [])
        self.cfg_budget = cfg_budget or {}
        self.dry_run = dry_run
        self.buf_cap_bytes = int(buf_cap_bytes)
        # end-of-tick stdout/stderr echo to substrate stderr.
        self.verbose = bool(verbose)
        # Recon pre-tick hook: substrate-only project digest written to
        # .peers/recon.md before the loop starts. Default on; disable
        # via --without-recon for runs where the digest is hand-prepared
        # or unwanted.
        self.recon_enabled = bool(recon_enabled)
        # when convergence-reached is about to fire, run ONE
        # extra "skeptic re-audit" tick first. If that tick stays clean
        # → really terminal. If it files a new blocking bug → counter
        # resets, loop continues. Default on; disable via
        # --without-post-convergence-skeptic for runs where false-
        # convergence is acceptable.
        self.auto_skeptic_enabled = bool(auto_skeptic_enabled)
        self._head_before_invoke: str | None = None
        self._peer_dir_identity: tuple[int, int] | None = None
        # H1: snapshot the expected goals.yaml hash ONCE at driver
        # init, in memory. The per-tick mutation check compares the
        # live file hash against this snapshot — so a peer that
        # rewrites both goals.yaml AND goals.sha256 in lockstep can
        # no longer fool the lock.
        self._goal_hash_snapshot: str | None = self._read_goal_hash_snapshot()
        # Task 4.1: detect active mode for Phase 0 state machine. Empty
        # string ("") flows through `_resolve_phase` as "implementation"
        # — i.e. backward-compat for every non-implement mode (audit,
        # security, thorough, custom user modes, or runs without a
        # `modes-applied.txt` audit trail at all).
        self.mode_name: str = _detect_mode_name(self.peer_dir)









    def run(self, max_ticks: int | None = None) -> dict[str, Any]:
        # File lock: refuse to run if another peers process is already
        # active against the same .peers/ dir. Prevents two peer loops
        # from clobbering state.json or racing for git on the target.
        lock_path = self.peer_dir / "run.lock"
        if self.peer_dir.is_symlink():
            raise RuntimeError(
                f"{self.peer_dir} is a symlink "
                f"({os.readlink(self.peer_dir)!r}); refusing to operate. "
                "Remove it manually to continue."
            )
        _ensure_private_dir(self.peer_dir)
        self._verify_no_control_symlinks()
        # Open without truncating first: contenders must not erase the
        # currently-running PID before they know they own the flock.
        self._peer_dir_identity = self._capture_peer_dir_identity()
        lock_fp = open_text_no_symlink(lock_path, "a")
        try:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_fp.close()
            return {"reason": "lock-held", "state": None}
        lock_fp.seek(0)
        lock_fp.truncate(0)
        lock_fp.write(f"{os.getpid()}\n")
        lock_fp.flush()
        sweep_legacy_handoff_msg(self.repo)

        # route SIGTERM through the same KeyboardInterrupt path
        # the rest of the loop already handles. Without this, a
        # `peers-ctl stop` (which sends SIGTERM) would terminate the
        # process immediately, skipping state.save() and leaving the
        # run.lock file behind.
        def _sigterm_handler(signum, frame):
            raise KeyboardInterrupt
        prev_term = signal.signal(signal.SIGTERM, _sigterm_handler)

        try:
            state = self.state_store.load()
            self._sync_peer_order(state)
            _apply_config_budget(
                state, self.cfg_budget,
                peer_tools=[s.tool for s in self.peer_specs],
            )
            # The config overlay above re-clobbers caps from config.yaml on
            # every start. Re-apply any explicit operator override (e.g.
            # `peers-ctl start --max-runtime 12h`) ON TOP so it actually
            # takes effect instead of being silently reset to the config
            # default.
            apply_operator_budget_overrides(state, self.repo)
            tm = TurnManager.from_state(state)
            ticks = 0

            # Seed the no-prior-regression baseline ONCE, before any peer
            # modifies code. Without this the gate fails forever (missing
            # baseline → exit 1) and sticks the run at the convergence wall.
            baseline_msg = ensure_baseline_snapshot(
                self.repo, self.peer_dir, [g.id for g in self.goals],
            )
            if baseline_msg is not None:
                print(f"peers: {baseline_msg}", file=sys.stderr, flush=True)

            if self.recon_enabled:
                self._run_recon_step()

            try:
                result = self._loop(state, tm, max_ticks, ticks)
                # step 2a: write stop-reason sentinel so
                # peers-ctl reconcile distinguishes clean self-termination
                # from a hard crash.
                self._write_stop_reason(result.get("reason", "unknown"))
                return result
            except KeyboardInterrupt:
                try:
                    self._save_state(state)
                except Exception as e:
                    print(f"peers: warning, failed to persist state on "
                          f"interrupt: {e}", file=sys.stderr)
                # Surface the interrupt in runs.jsonl too — useful in
                # post-mortems to distinguish "operator stopped" from
                # "completed cleanly".
                self._append_exit_event(
                    "interrupted", state.get("budget", {}).get(
                        "spent_iterations", 0,
                    ),
                )
                self._write_stop_reason("interrupted")
                raise
            except Exception as e:
                # (CRITICAL): any non-KeyboardInterrupt exception
                # from _loop would previously skip state.save and lose
                # everything since the last successful save. Persist
                # best-effort, then re-raise so the caller sees it.
                try:
                    self._save_state(state)
                except Exception as save_err:
                    print(
                        f"peers: warning, failed to persist state "
                        f"after exception {type(e).__name__}: "
                        f"{save_err}",
                        file=sys.stderr,
                    )
                self._write_stop_reason(f"error:{type(e).__name__}")
                raise
        finally:
            try:
                signal.signal(signal.SIGTERM, prev_term)
            except Exception:
                pass
            try:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
                lock_fp.close()
            except Exception:
                pass
            release_run_lock(self.peer_dir)

    def _loop(self, state: dict[str, Any], tm: TurnManager,
              max_ticks: int | None, ticks: int) -> dict[str, Any]:
        return TickLoop(self).run(state, tm, max_ticks, ticks)
