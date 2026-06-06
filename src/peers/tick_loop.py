"""Main peer tick-loop skeleton.

The loop still delegates domain decisions back to OrchestratorDriver; this
module owns the per-tick control-flow order so later extractions can peel
off smaller responsibilities without moving the whole driver at once.
"""
from __future__ import annotations

from dataclasses import dataclass
import sys
import time
from typing import Any, Protocol, Sequence

from peers.model_provider import build_peer_argv


class _Comm(Protocol):
    def head_sha(self) -> str:
        ...


class _HealthInvoker(Protocol):
    def invoke(
        self,
        argv: Sequence[str],
        *,
        prompt: str,
        idle_timeout_s: int,
        absolute_max_runtime_s: int,
        prompt_mode: str,
        error_patterns: Sequence[str],
        halt_patterns: Sequence[str],
        buf_cap_bytes: int,
        extra_env: dict[str, str] | None = None,
    ) -> Any:
        ...


class _PeerSpec(Protocol):
    argv: Sequence[str]
    prompt_mode: str
    tool: str


class _TurnManager(Protocol):
    def advance(self, *, success: bool) -> None:
        ...


class TickLoopDriver(Protocol):
    """Adapter surface that TickLoop needs from OrchestratorDriver."""

    comm: _Comm
    health: _HealthInvoker
    idle_timeout_s: int
    absolute_max_runtime_s: int
    error_patterns: Sequence[str]
    halt_patterns: Sequence[str]
    buf_cap_bytes: int
    _head_before_invoke: str | None

    def _verify_peer_dir_identity(self) -> None:
        ...

    def _pre_tick_exit(
        self, state: dict[str, Any], max_ticks: int | None, ticks: int,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        ...

    def _maybe_checkpoint_exit(
        self, state: dict[str, Any], ticks: int,
    ) -> dict[str, Any] | None:
        ...

    def _prepare_tick_prompt(
        self,
        state: dict[str, Any],
        turn_manager: _TurnManager,
        results: dict[str, Any],
    ) -> tuple[str, _PeerSpec, str]:
        ...

    def _write_prompt_log(self, tick: int, peer: str, prompt: str) -> None:
        ...

    def _handle_pattern_match_and_halt(
        self,
        state: dict[str, Any],
        ticks: int,
        upcoming_tick: int,
        peer: str,
        run: Any,
    ) -> dict[str, Any] | None:
        ...

    def _write_peer_output_logs(self, tick: int, peer: str, run: Any) -> None:
        ...

    def _post_run(self, state: dict[str, Any], peer: str, run: Any) -> bool:
        ...

    def _apply_anti_cheating_outcome(
        self, state: dict[str, Any], peer: str, success: bool,
    ) -> bool:
        ...

    def _apply_dry_run_reset(
        self, state: dict[str, Any], success: bool,
    ) -> bool:
        ...

    def _record_tick_accounting(
        self, state: dict[str, Any], success: bool, tick_dt: int,
    ) -> None:
        ...

    def _account_tokens_usd(
        self, state: dict[str, Any], tool: str, run: Any,
    ) -> tuple[int, float]:
        ...

    def _update_peer_health(
        self, state: dict[str, Any], peer: str, success: bool,
    ) -> None:
        ...

    def _dirty_worktree(self, state: dict[str, Any]) -> bool:
        ...

    def _detect_tampering(self, state: dict[str, Any]) -> None:
        ...

    def _maybe_halt(self, state: dict[str, Any]) -> None:
        ...

    def _append_warnings_history(
        self, state: dict[str, Any], warnings: list[str],
    ) -> None:
        ...

    def _append_run_log(
        self,
        state: dict[str, Any],
        peer: str,
        run: Any,
        success: bool,
        **kwargs: Any,
    ) -> None:
        ...

    def _save_state(self, state: dict[str, Any]) -> None:
        ...

    def _emit_tick_end(
        self,
        state: dict[str, Any],
        peer: str,
        run: Any,
        success: bool,
        tick_dt: int,
        head_after_sha: str | None,
    ) -> None:
        ...

    def _update_convergence_counter(self, state: dict[str, Any]) -> None:
        ...


_REQUIRED_DRIVER_ATTRS = (
    "comm",
    "health",
    "idle_timeout_s",
    "absolute_max_runtime_s",
    "error_patterns",
    "halt_patterns",
    "buf_cap_bytes",
    "_head_before_invoke",
    "_verify_peer_dir_identity",
    "_pre_tick_exit",
    "_maybe_checkpoint_exit",
    "_prepare_tick_prompt",
    "_write_prompt_log",
    "_handle_pattern_match_and_halt",
    "_write_peer_output_logs",
    "_post_run",
    "_apply_anti_cheating_outcome",
    "_apply_dry_run_reset",
    "_record_tick_accounting",
    "_account_tokens_usd",
    "_update_peer_health",
    "_dirty_worktree",
    "_detect_tampering",
    "_attest_tick_commits",
    "_maybe_halt",
    "_append_warnings_history",
    "_append_run_log",
    "_save_state",
    "_emit_tick_end",
    "_update_convergence_counter",
)


_REQUIRED_RUN_RESULT_ATTRS = (
    "classification",
    "exit_code",
    "stdout",
    "stderr",
    "duration_ms",
    "output_digest",
    "matched_error_pattern",
    "matched_error_snippet",
    "matched_error_source",
    "truncated",
    "halt_required",
    "jsonl_liveness_fallback_used",
    "jsonl_liveness_fallbacks",
)


@dataclass
class _TickInvocation:
    peer: str
    spec: _PeerSpec
    prompt: str
    upcoming_tick: int
    run: Any
    tick_dt: int


class TickLoop:
    """Run peer ticks by coordinating the orchestrator hook methods."""

    def __init__(self, driver: TickLoopDriver) -> None:
        self._validate_driver_contract(driver)
        self.driver = driver

    def run(
        self,
        state: dict[str, Any],
        turn_manager: _TurnManager,
        max_ticks: int | None,
        ticks: int,
    ) -> dict[str, Any]:
        self._validate_turn_manager(turn_manager)
        while True:
            early_exit, results = self._prepare_next_tick(
                state, turn_manager, max_ticks, ticks,
            )
            if early_exit is not None:
                return early_exit

            invocation = self._invoke_peer(state, turn_manager, results)
            halt_exit = self._handle_halt_if_needed(
                state, ticks, invocation,
            )
            if halt_exit is not None:
                return halt_exit

            self._finalize_tick(state, turn_manager, invocation)
            ticks += 1

    def _prepare_next_tick(
        self,
        state: dict[str, Any],
        turn_manager: _TurnManager,
        max_ticks: int | None,
        ticks: int,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        driver = self.driver
        driver._verify_peer_dir_identity()
        early_exit, results = driver._pre_tick_exit(state, max_ticks, ticks)
        if early_exit is not None:
            return early_exit, results
        checkpoint_exit = driver._maybe_checkpoint_exit(state, ticks)
        if checkpoint_exit is not None:
            return checkpoint_exit, results
        return None, results

    def _invoke_peer(
        self,
        state: dict[str, Any],
        turn_manager: _TurnManager,
        results: dict[str, Any],
    ) -> _TickInvocation:
        driver = self.driver
        peer, spec, prompt = driver._prepare_tick_prompt(
            state, turn_manager, results,
        )
        self._validate_peer_plan(peer, spec, prompt)
        upcoming_tick = state["iteration"] + 1
        driver._write_prompt_log(upcoming_tick, peer, prompt)
        print(
            f"peers: tick {upcoming_tick} peer={peer} starting...",
            file=sys.stderr, flush=True,
        )

        tick_t0 = time.monotonic()
        driver._head_before_invoke = driver.comm.head_sha()
        peer_argv, extra_env = build_peer_argv(spec)
        # Expose the current peer name to the peer's subprocess (and thus to
        # any git hooks it triggers) so peer attribution does not depend on
        # the git author identity — which is frequently a single shared
        # container user that cannot distinguish the two peers. The
        # reviewer-only-checkoff hook reads PEERS_PEER_NAME.
        extra_env = {**(extra_env or {}), "PEERS_PEER_NAME": str(peer)}
        invoke_kwargs: dict[str, Any] = {
            "prompt": prompt,
            "idle_timeout_s": driver.idle_timeout_s,
            "absolute_max_runtime_s": driver.absolute_max_runtime_s,
            "prompt_mode": spec.prompt_mode,
            "error_patterns": driver.error_patterns,
            "halt_patterns": driver.halt_patterns,
            "buf_cap_bytes": driver.buf_cap_bytes,
            # Option C: lets health_guard classify a halt from the tool's
            # structured status channel (claude stream-json result envelope),
            # not just the echo-prone free-text halt_patterns.
            "tool": spec.tool,
        }
        if extra_env:
            invoke_kwargs["extra_env"] = extra_env
        run = driver.health.invoke(peer_argv, **invoke_kwargs)
        driver._verify_peer_dir_identity()
        self._validate_run_result(run)
        tick_dt = int(time.monotonic() - tick_t0)
        return _TickInvocation(
            peer=peer,
            spec=spec,
            prompt=prompt,
            upcoming_tick=upcoming_tick,
            run=run,
            tick_dt=tick_dt,
        )

    def _handle_halt_if_needed(
        self,
        state: dict[str, Any],
        ticks: int,
        invocation: _TickInvocation,
    ) -> dict[str, Any] | None:
        return self.driver._handle_pattern_match_and_halt(
            state,
            ticks,
            invocation.upcoming_tick,
            invocation.peer,
            invocation.run,
        )

    def _finalize_tick(
        self,
        state: dict[str, Any],
        turn_manager: _TurnManager,
        invocation: _TickInvocation,
    ) -> None:
        driver = self.driver
        peer = invocation.peer
        run = invocation.run
        driver._write_peer_output_logs(
            invocation.upcoming_tick, peer, run,
        )
        if run.truncated:
            state.setdefault("warnings", []).append(
                f"healthguard: peer {peer!r}'s output exceeded the "
                "2 MiB per-stream cap; head/tail kept, middle "
                "truncated. Consider quieter prompting or a higher "
                "cap if signal is being lost."
            )

        success = driver._post_run(state, peer, run)
        success = driver._apply_anti_cheating_outcome(state, peer, success)
        success = driver._apply_dry_run_reset(state, success)

        turn_manager.advance(success=success)
        driver._record_tick_accounting(state, success, invocation.tick_dt, peer=peer)

        tokens_this_tick, usd_this_tick = driver._account_tokens_usd(
            state, invocation.spec.tool, run,
        )

        driver._update_peer_health(state, peer, success)
        state["dirty_worktree"] = driver._dirty_worktree(state)
        if success:
            driver._detect_tampering(state)
        driver._maybe_halt(state)

        new_warnings = list(state.get("warnings", []))
        driver._append_warnings_history(state, new_warnings)
        head_after_sha = driver.comm.head_sha()
        # attribute this tick's commits to the running peer by the
        # observed HEAD-delta (agent-unforgeable), before any later goal check
        # reads the attestation. No live agent runs here.
        driver._attest_tick_commits(
            peer, driver._head_before_invoke, head_after_sha,
        )
        driver._append_run_log(
            state, peer, run, success,
            tokens_this_tick=tokens_this_tick,
            usd_this_tick=usd_this_tick,
            head_before=driver._head_before_invoke,
            head_after=head_after_sha,
            warnings_emitted=new_warnings,
        )
        driver._save_state(state)
        driver._emit_tick_end(
            state, peer, run, success, invocation.tick_dt, head_after_sha,
        )
        driver._update_convergence_counter(state)

    @staticmethod
    def _validate_driver_contract(driver: object) -> None:
        missing = [
            name for name in _REQUIRED_DRIVER_ATTRS
            if not hasattr(driver, name)
        ]
        if missing:
            raise TypeError(
                "TickLoop driver missing required attributes: "
                + ", ".join(missing),
            )
        comm = getattr(driver, "comm")
        if not callable(getattr(comm, "head_sha", None)):
            raise TypeError("TickLoop driver.comm must provide head_sha()")
        health = getattr(driver, "health")
        if not callable(getattr(health, "invoke", None)):
            raise TypeError("TickLoop driver.health must provide invoke()")

    @staticmethod
    def _validate_turn_manager(turn_manager: object) -> None:
        if not callable(getattr(turn_manager, "advance", None)):
            raise TypeError("TickLoop turn_manager must provide advance()")

    @staticmethod
    def _validate_peer_plan(peer: str, spec: _PeerSpec, prompt: str) -> None:
        if not isinstance(peer, str) or not peer:
            raise TypeError("TickLoop peer plan must return a non-empty peer")
        if not isinstance(prompt, str):
            raise TypeError("TickLoop peer plan must return a string prompt")
        missing = [
            name for name in ("argv", "prompt_mode", "tool")
            if not hasattr(spec, name)
        ]
        if missing:
            raise TypeError(
                "TickLoop peer spec missing required attributes: "
                + ", ".join(missing),
            )

    @staticmethod
    def _validate_run_result(run: object) -> None:
        missing = [
            name for name in _REQUIRED_RUN_RESULT_ATTRS
            if not hasattr(run, name)
        ]
        if missing:
            raise TypeError(
                "health.invoke returned malformed RunResult; missing: "
                + ", ".join(missing),
            )
        for name in ("classification", "stdout", "stderr"):
            if not isinstance(getattr(run, name), str):
                raise TypeError(f"RunResult.{name} must be a string")
        if not isinstance(getattr(run, "truncated"), bool):
            raise TypeError("RunResult.truncated must be a bool")
        if not isinstance(getattr(run, "halt_required"), bool):
            raise TypeError("RunResult.halt_required must be a bool")
