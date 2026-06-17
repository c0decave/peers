"""Convergence and post-convergence skeptic state helpers."""
from __future__ import annotations

from pathlib import Path
from typing import Any


PHASE_B_SKEPTIC_GATES = (
    "blind-review", "honesty-audit", "concerns-resolved",
)
DEFAULT_PHASE_A_N = 5


def implement_phase_a_n(repo: Path, peer_dir: Path) -> int:
    """Return implement-mode Phase A threshold from frozen PLAN metadata.

    The live PLAN.md is peer-editable during checkoff. Prefer the frozen
    operator-supplied copy, falling back to live PLAN.md only for older
    scaffolds that lack .peers/PLAN.original.md.
    """
    from peers_ctl.plan_parser import PlanValidationError, parse_plan

    for path in (peer_dir / "PLAN.original.md", repo / "PLAN.md"):
        try:
            plan = parse_plan(path)
        except PlanValidationError:
            continue
        if plan.convergence_n >= 0:
            return plan.convergence_n
    return DEFAULT_PHASE_A_N


def _resolve_convergence_state(
    mode_name: str,
    current_phase: str,
    consecutive_clean: int,
    phase_a_n: int,
    phase_b_n: int,
    phase_b_extra_ticks: int,
) -> str:
    """Return the next implement-mode convergence phase."""
    if mode_name != "implement":
        return current_phase
    if current_phase == "complete":
        return "complete"
    if current_phase == "A":
        if consecutive_clean >= phase_a_n:
            return "B"
        return "A"
    if current_phase == "B":
        if phase_b_extra_ticks >= phase_b_n:
            return "complete"
        return "B"
    return "A"


class SkepticEngine:
    """Maintains implement-mode two-phase convergence counters."""

    def __init__(
        self,
        mode_name: str,
        phase_b_skeptic_gates: tuple[str, ...] = PHASE_B_SKEPTIC_GATES,
        phase_a_n: int = DEFAULT_PHASE_A_N,
        phase_b_n: int = 2,
    ) -> None:
        self.mode_name = mode_name
        self.phase_b_skeptic_gates = phase_b_skeptic_gates
        self.phase_a_n = phase_a_n
        self.phase_b_n = phase_b_n

    def update_two_phase_counters(
        self, state: dict[str, Any], results: dict[str, Any],
    ) -> None:
        """Update convergence_phase fields for implement mode only."""
        if self.mode_name != "implement":
            return
        all_hard_green = all(
            result.state == "pass" for result in results.values()
        ) if results else False
        if all_hard_green:
            state["consecutive_hard_green_ticks"] = state.get(
                "consecutive_hard_green_ticks", 0,
            ) + 1
        else:
            state["consecutive_hard_green_ticks"] = 0

        current_phase = state.get("convergence_phase", "A")
        if current_phase == "B":
            skeptic_ok = all(
                results.get(gid) is not None
                and results[gid].state == "pass"
                for gid in self.phase_b_skeptic_gates
            )
            if skeptic_ok:
                state["phase_b_extra_ticks"] = state.get(
                    "phase_b_extra_ticks", 0,
                ) + 1
            else:
                state["phase_b_extra_ticks"] = 0
        else:
            state.setdefault("phase_b_extra_ticks", 0)

        state["convergence_phase"] = _resolve_convergence_state(
            self.mode_name,
            current_phase,
            state["consecutive_hard_green_ticks"],
            phase_a_n=self.phase_a_n,
            phase_b_n=self.phase_b_n,
            phase_b_extra_ticks=state["phase_b_extra_ticks"],
        )
