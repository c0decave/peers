"""Type-only declaration of the surface ``OrchestratorDriver`` provides to
its mixins.

The orchestrator is composed from several ``Driver*Mixin`` classes (see
``peers._driver_orchestrator_impl.OrchestratorDriver``). Each mixin freely
calls ``self.repo``, ``self._save_state(...)``, etc. — attributes and
methods that the *host* class wires up in ``__init__`` or that a *sibling*
mixin defines. When mypy type-checks a mixin in isolation it cannot see the
host, so every such access used to surface as ``"DriverTickHooksMixin" has
no attribute "repo"`` (~135 ``attr-defined`` errors across the six mixins).

``_DriverHost`` declares that shared surface once. Every mixin inherits it,
so mypy resolves the cross-mixin accesses; the real ``OrchestratorDriver``
supplies the actual values/implementations. The entire declaration lives
under ``if TYPE_CHECKING`` — so at runtime this class is an empty
``object`` subclass and inserting it into the MRO is byte-identical to the
previous behavior. See ``tests/unit/test_driver_host.py`` for the
runtime-empty / no-shadowing contract this relies on.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

    from peers.comm_layer import GitCommLayer, HybridCommLayer
    from peers.goal_engine import GoalEngine, GoalResult
    from peers.goals import Goal
    from peers.health_guard import RunResult
    from peers.peer_spec import PeerSpec
    from peers.state_store import StateStore


class _DriverHost:
    """See module docstring. Runtime-empty by construction."""

    if TYPE_CHECKING:
        # --- data attributes set by OrchestratorDriver.__init__ ---
        repo: Path
        peer_dir: Path
        goals: list[Goal]
        peer_specs: list[PeerSpec]
        peer_names: list[str]
        peers_by_name: dict[str, PeerSpec]
        state_store: StateStore
        engine: GoalEngine
        comm: GitCommLayer | HybridCommLayer
        comm_variant: str
        dry_run: bool
        verbose: bool
        mode_name: str
        auto_skeptic_enabled: bool
        _goal_hash_snapshot: str | None
        _head_before_invoke: str | None
        _peer_dir_identity: tuple[int, int] | None
        _peer_dir_identity_fd: int | None

        # --- methods supplied by sibling mixins (or the host) ---
        def _save_state(self, state: dict[str, Any]) -> None: ...

        def _verify_peer_dir_identity(self) -> None: ...

        def _attest_tick_commits(
            self, peer: str, head_before: str | None, head_after: str,
        ) -> None: ...

        def _record_results(
            self,
            state: dict[str, Any],
            results: dict[str, GoalResult],
            *,
            increment_repeat_failures: bool = True,
        ) -> None: ...

        def _all_green_including_soft(self, state: dict[str, Any]) -> bool: ...

        def _soft_reviews_pending(
            self, state: dict[str, Any], current_peer: str,
        ) -> list[Goal]: ...

        def _peer_review_trailer_is_soft_goal(
            self, goal_id: str | None,
        ) -> bool: ...

        def _record_soft_review_from_commit(
            self, state: dict[str, Any], commit: Any, reviewer: str,
        ) -> bool: ...

        def _evaluate_gates_for_tick(self) -> dict[str, GoalResult]: ...

        def _submit_gate_eval(self, sha: str) -> None: ...

        def _converged_after_fresh_recheck(
            self, state: dict[str, Any], results: dict[str, GoalResult],
        ) -> bool: ...

        def _append_exit_event(self, reason: str, ticks: int) -> None: ...

        def _write_peer_output_logs(
            self, tick_n: int, peer: str, run: RunResult,
        ) -> None: ...
