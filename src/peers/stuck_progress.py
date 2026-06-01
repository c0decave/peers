"""Convergence-wall stuck detection + progress-aware reset.

Split out of `driver_tick_hooks` to keep that module under the
decomposition line-budget. `driver_tick_hooks` re-exports these names,
so existing imports (`from peers.driver_tick_hooks import
compute_stuck_gate_halt_reason`) keep working.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from peers.safe_io import read_text_no_symlink

# Item 7: convergence-wall hard halt. v9-v12 all burned full budget
# struggling on tests-pass + no-prior-regression. After N consecutive
# red ticks on a watched gate, exit cleanly with stop-reason `stuck:<gate>`
# instead of letting the loop spin until max_runtime.
_DEFAULT_STUCK_HALT_AFTER = 5
_DEFAULT_STUCK_HALT_GATES = ("tests-pass", "no-prior-regression")


def compute_stuck_gate_halt_reason(state: dict[str, Any]) -> str | None:
    """Return `stuck:<gate>` if any watched gate stuck >= threshold ticks.

    Threshold default 5, override via state['config']['goals']['stuck_halt_after'].
    Watched-gate set defaults to (tests-pass, no-prior-regression), override
    via state['config']['goals']['stuck_halt_gates'] (list of goal ids).
    A threshold of 0 disables the halt entirely (legacy behavior).
    """
    cfg_goals = ((state.get("config") or {}).get("goals") or {})
    raw_n = cfg_goals.get("stuck_halt_after", _DEFAULT_STUCK_HALT_AFTER)
    try:
        threshold = int(raw_n)
    except (TypeError, ValueError):
        threshold = _DEFAULT_STUCK_HALT_AFTER
    if threshold <= 0:
        return None
    raw_gates = cfg_goals.get("stuck_halt_gates")
    if raw_gates:
        watched = tuple(str(g) for g in raw_gates)
    else:
        watched = _DEFAULT_STUCK_HALT_GATES
    stuck = state.get("stuck_counter") or {}
    # Pick the worst (highest count) watched gate that crossed threshold.
    worst_gate: str | None = None
    worst_count = -1
    for gate in watched:
        count = int(stuck.get(gate, 0))
        if count >= threshold and count > worst_count:
            worst_gate = gate
            worst_count = count
    if worst_gate is None:
        return None
    return f"stuck:{worst_gate}"


# Progress-aware stuck reset. The convergence-wall halt watches
# `tests-pass`, but in implement-mode the acceptance suite is red BY
# DESIGN until ~all PLAN steps are built (calc v2 diagnostic 2026-05-31:
# a multi-step build was killed `stuck:tests-pass` while genuinely
# progressing one step at a time). When the count of completed PLAN
# steps rises on a tick, the configured terminal gate's red streak is
# forgiven — so a run halts only on NO step progress for N ticks, not on
# mere feature-incompleteness. HEAD-advance is deliberately NOT the
# signal: a handoff commit advances HEAD every tick even when stuck.
_IMPLEMENT_PROGRESS_RESET_GATES = ("tests-pass",)
_DONE_STEP_RE = re.compile(r"(?m)^[ \t]*-[ \t]*\[[xX]\]")


def count_done_plan_steps(plan_path: Path) -> int:
    """Count checked `- [x]`/`- [X]` checklist lines in PLAN.md.

    A robust progress heuristic (NOT a correctness gate): a transiently
    malformed PLAN.md must not raise here, so on any read error we return
    0 (= no progress observed) rather than propagating. Only line-start
    checkboxes count; an inline `- [x]` mid-prose does not.
    """
    try:
        text = read_text_no_symlink(plan_path, max_bytes=1_000_000)
    except OSError:
        return 0
    return len(_DONE_STEP_RE.findall(text))


def _resolve_progress_reset_gates(
    state: dict[str, Any], mode_name: str,
) -> tuple[str, ...]:
    """Gates whose stuck streak is forgiven on PLAN-step progress.

    Config override `goals.stuck_progress_reset_gates` wins when present
    (an explicit empty list disables the behavior). Otherwise implement-
    mode defaults to (tests-pass,); every other mode defaults to () so
    audit/security/thorough behavior is unchanged (there a red tests-pass
    IS a genuine stuck signal).
    """
    cfg_goals = ((state.get("config") or {}).get("goals") or {})
    raw = cfg_goals.get("stuck_progress_reset_gates")
    if raw is not None:
        return tuple(str(g) for g in raw)
    if mode_name == "implement":
        return _IMPLEMENT_PROGRESS_RESET_GATES
    return ()


def reset_stuck_on_progress(
    state: dict[str, Any], plan_steps_done: int, mode_name: str,
) -> None:
    """Clear the watched gates' red streak when PLAN steps advance.

    Compares `plan_steps_done` to the baseline recorded last tick. On a
    strict increase, pops each configured gate from `stuck_counter`. The
    baseline is always refreshed (incl. on a drop, so re-completing a
    step counts as fresh progress). No-op — and no baseline written — for
    modes/configs with no reset gates, keeping their state.json clean.
    """
    reset_gates = _resolve_progress_reset_gates(state, mode_name)
    if not reset_gates:
        return
    prev = state.get("last_plan_steps_done")
    if prev is not None and plan_steps_done > prev:
        counter = state.get("stuck_counter") or {}
        for gate in reset_gates:
            counter.pop(gate, None)
    state["last_plan_steps_done"] = plan_steps_done
