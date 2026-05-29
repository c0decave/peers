from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from peers.tick_loop import TickLoop


class _TurnManager:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def advance(self, *, success: bool) -> None:
        self.events.append(f"turn_advance:{success}")


class _Comm:
    def head_sha(self) -> str:
        return "abc1234"


class _Health:
    def __init__(self, events: list[str], run: Any) -> None:
        self.events = events
        self.run = run
        self.last_args: tuple[Any, ...] | None = None
        self.last_kwargs: dict[str, Any] | None = None

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        self.events.append("health_invoke")
        self.last_args = args
        self.last_kwargs = kwargs
        return self.run


def _run_result(**overrides: Any) -> SimpleNamespace:
    data = {
        "classification": "success",
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "duration_ms": 10,
        "output_digest": "",
        "matched_error_pattern": "",
        "matched_error_snippet": "",
        "matched_error_source": "",
        "truncated": False,
        "halt_required": False,
        "jsonl_liveness_fallback_used": False,
        "jsonl_liveness_fallbacks": 0,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


class _Driver:
    def __init__(
        self,
        run: Any | None = None,
        *,
        pre_exit_first: bool = False,
        checkpoint_exit: dict[str, Any] | None = None,
        halt_exit: dict[str, Any] | None = None,
        post_success: bool = True,
        anti_success: bool | None = None,
        dry_success: bool | None = None,
    ) -> None:
        self.events: list[str] = []
        self.comm = _Comm()
        self.health = _Health(
            self.events,
            run or _run_result(),
        )
        self.idle_timeout_s = 1
        self.absolute_max_runtime_s = 2
        self.error_patterns = ["ERR"]
        self.halt_patterns = ["HALT"]
        self.buf_cap_bytes = 1024
        self._head_before_invoke = None
        self._pre_tick_calls = 0
        self._pre_exit_first = pre_exit_first
        self._checkpoint_exit = checkpoint_exit
        self._halt_exit = halt_exit
        self._post_success = post_success
        self._anti_success = anti_success
        self._dry_success = dry_success

    def _verify_peer_dir_identity(self) -> None:
        self.events.append("verify")

    def _pre_tick_exit(
        self, state: dict[str, Any], max_ticks: int | None, ticks: int,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        self.events.append(f"pre:{ticks}")
        self._pre_tick_calls += 1
        if self._pre_exit_first:
            return {"reason": "pre-exit", "state": state}, {}
        if self._pre_tick_calls > 1:
            return {"reason": "done", "state": state}, {}
        return None, {"tests-pass": object()}

    def _maybe_checkpoint_exit(
        self, state: dict[str, Any], ticks: int,
    ) -> dict[str, Any] | None:
        self.events.append(f"checkpoint:{ticks}")
        return self._checkpoint_exit

    def _prepare_tick_prompt(
        self, state: dict[str, Any], turn_manager: Any, results: dict[str, Any],
    ) -> tuple[str, Any, str]:
        self.events.append("prepare")
        spec = SimpleNamespace(
            argv=("fake-peer",),
            prompt_mode="stdin",
            tool="claude",
        )
        return "claude", spec, "prompt"

    def _write_prompt_log(self, tick: int, peer: str, prompt: str) -> None:
        self.events.append(f"prompt_log:{tick}:{peer}:{prompt}")

    def _handle_pattern_match_and_halt(
        self, state: dict[str, Any], ticks: int, upcoming_tick: int,
        peer: str, run: Any,
    ) -> dict[str, Any] | None:
        self.events.append("halt_check")
        return self._halt_exit

    def _write_peer_output_logs(self, tick: int, peer: str, run: Any) -> None:
        self.events.append(f"peer_logs:{tick}:{peer}")

    def _post_run(
        self, state: dict[str, Any], peer: str, run: Any,
    ) -> bool:
        self.events.append("post_run")
        return self._post_success

    def _apply_anti_cheating_outcome(
        self, state: dict[str, Any], peer: str, success: bool,
    ) -> bool:
        self.events.append("anti_cheat")
        return success if self._anti_success is None else self._anti_success

    def _apply_dry_run_reset(
        self, state: dict[str, Any], success: bool,
    ) -> bool:
        self.events.append("dry_run")
        return success if self._dry_success is None else self._dry_success

    def _record_tick_accounting(
        self, state: dict[str, Any], success: bool, tick_dt: int,
        peer: str | None = None,
    ) -> None:
        self.events.append("accounting")
        state["iteration"] += 1

    def _account_tokens_usd(
        self, state: dict[str, Any], tool: str, run: Any,
    ) -> tuple[int, float]:
        self.events.append(f"tokens:{tool}")
        return 12, 0.5

    def _update_peer_health(
        self, state: dict[str, Any], peer: str, success: bool,
    ) -> None:
        self.events.append("peer_health")

    def _dirty_worktree(self, state: dict[str, Any]) -> bool:
        self.events.append("dirty")
        return False

    def _detect_tampering(self, state: dict[str, Any]) -> None:
        self.events.append("tamper")

    def _maybe_halt(self, state: dict[str, Any]) -> None:
        self.events.append("maybe_halt")

    def _append_warnings_history(
        self, state: dict[str, Any], warnings: list[str],
    ) -> None:
        self.events.append(f"warnings:{len(warnings)}")

    def _append_run_log(
        self, state: dict[str, Any], peer: str, run: Any, success: bool,
        **kwargs: Any,
    ) -> None:
        self.events.append(
            f"run_log:{kwargs['tokens_this_tick']}:{kwargs['usd_this_tick']}",
        )

    def _save_state(self, state: dict[str, Any]) -> None:
        self.events.append("save")

    def _emit_tick_end(
        self, state: dict[str, Any], peer: str, run: Any,
        success: bool, tick_dt: int, head_after_sha: str | None,
    ) -> None:
        self.events.append("emit")

    def _update_convergence_counter(self, state: dict[str, Any]) -> None:
        self.events.append("convergence")


def test_tick_loop_delegates_one_successful_tick_then_exits():
    state = {"iteration": 0}
    driver = _Driver()

    result = TickLoop(driver).run(state, _TurnManager(driver.events), None, 0)

    assert result["reason"] == "done"
    assert state["iteration"] == 1
    assert driver.events == [
        "verify",
        "pre:0",
        "checkpoint:0",
        "prepare",
        "prompt_log:1:claude:prompt",
        "health_invoke",
        "verify",
        "halt_check",
        "peer_logs:1:claude",
        "post_run",
        "anti_cheat",
        "dry_run",
        "turn_advance:True",
        "accounting",
        "tokens:claude",
        "peer_health",
        "dirty",
        "tamper",
        "maybe_halt",
        "warnings:0",
        "run_log:12:0.5",
        "save",
        "emit",
        "convergence",
        "verify",
        "pre:1",
    ]


def test_tick_loop_surfaces_truncated_output_warning():
    state = {"iteration": 0}
    run = _run_result(truncated=True)
    driver = _Driver(run=run)

    TickLoop(driver).run(state, _TurnManager(driver.events), None, 0)

    assert any("output exceeded" in w for w in state["warnings"])
    assert "warnings:1" in driver.events


def test_tick_loop_forwards_healthguard_invocation_arguments():
    state = {"iteration": 0}
    driver = _Driver()

    TickLoop(driver).run(state, _TurnManager(driver.events), None, 0)

    assert driver._head_before_invoke == "abc1234"
    assert driver.health.last_args == (("fake-peer",),)
    assert driver.health.last_kwargs == {
        "prompt": "prompt",
        "idle_timeout_s": 1,
        "absolute_max_runtime_s": 2,
        "prompt_mode": "stdin",
        "error_patterns": ["ERR"],
        "halt_patterns": ["HALT"],
        "buf_cap_bytes": 1024,
    }


def test_failed_tick_advances_false_and_skips_success_only_tamper_check():
    state = {"iteration": 0}
    driver = _Driver(post_success=False)

    result = TickLoop(driver).run(state, _TurnManager(driver.events), None, 0)

    assert result["reason"] == "done"
    assert state["iteration"] == 1
    assert "turn_advance:False" in driver.events
    assert "tamper" not in driver.events
    assert "run_log:12:0.5" in driver.events


def test_tick_loop_returns_pre_tick_exit_before_checkpoint_or_invoke():
    state = {"iteration": 0}
    driver = _Driver(pre_exit_first=True)

    result = TickLoop(driver).run(state, _TurnManager(driver.events), None, 0)

    assert result["reason"] == "pre-exit"
    assert driver.events == ["verify", "pre:0"]


def test_tick_loop_returns_checkpoint_exit_before_invocation():
    state = {"iteration": 0}
    driver = _Driver(checkpoint_exit={"reason": "checkpoint", "state": state})

    result = TickLoop(driver).run(state, _TurnManager(driver.events), None, 0)

    assert result["reason"] == "checkpoint"
    assert driver.events == ["verify", "pre:0", "checkpoint:0"]


def test_tick_loop_returns_halt_exit_before_finalizing_tick():
    state = {"iteration": 0}
    driver = _Driver(halt_exit={"reason": "peer-unavailable", "state": state})

    result = TickLoop(driver).run(state, _TurnManager(driver.events), None, 0)

    assert result["reason"] == "peer-unavailable"
    assert driver.events == [
        "verify",
        "pre:0",
        "checkpoint:0",
        "prepare",
        "prompt_log:1:claude:prompt",
        "health_invoke",
        "verify",
        "halt_check",
    ]


def test_tick_loop_rejects_incomplete_driver_contract():
    with pytest.raises(TypeError, match="missing required attributes"):
        TickLoop(object())


def test_tick_loop_rejects_turn_manager_without_advance():
    driver = _Driver()

    with pytest.raises(TypeError, match="turn_manager"):
        TickLoop(driver).run({"iteration": 0}, object(), None, 0)


def test_tick_loop_rejects_malformed_peer_plan():
    class BadPlanDriver(_Driver):
        def _prepare_tick_prompt(
            self,
            state: dict[str, Any],
            turn_manager: Any,
            results: dict[str, Any],
        ) -> tuple[str, Any, str]:
            self.events.append("prepare")
            return "claude", SimpleNamespace(argv=("fake-peer",)), "prompt"

    driver = BadPlanDriver()

    with pytest.raises(TypeError, match="peer spec missing"):
        TickLoop(driver).run({"iteration": 0}, _TurnManager(driver.events), None, 0)


def test_tick_loop_rejects_malformed_run_result_before_halt_handling():
    state = {"iteration": 0}
    driver = _Driver(run=SimpleNamespace(truncated=False))

    with pytest.raises(TypeError, match="malformed RunResult"):
        TickLoop(driver).run(state, _TurnManager(driver.events), None, 0)

    assert "health_invoke" in driver.events
    assert "halt_check" not in driver.events
