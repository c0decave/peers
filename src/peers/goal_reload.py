"""BUG-274 helper: hot-reload the in-memory goal list when the
mutation guard accepts a paired ``goals.yaml`` + source commit.

Split out of :mod:`peers.driver_tick_hooks` for the module line-budget
(facade test caps that file at 800 lines).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def _reload_driver_goals(
    driver: Any, gfile: Path, accepted_hash: str | None = None,
) -> str | None:
    """Rebind ``driver.goals`` and ``driver.engine.goals`` (plus the
    pipelined async runner's goal list, if present) to whatever the
    on-disk ``gfile`` currently parses to.

    Returns ``None`` on success. On load failure, returns a diagnostic
    string and leaves the in-memory state untouched so the mutation
    guard can fail closed without advancing its hash snapshot. When
    ``accepted_hash`` is supplied, the driver's snapshot is advanced
    only after all in-memory goal references have been rebound.

    The engine's verdict cache is cleared because a goal's cmd may
    have changed; a memoized PASS keyed only by (tree, HEAD) would
    otherwise serve the OLD cmd's verdict for a fresh evaluation of
    the NEW cmd.
    """
    from peers.goals import load_goals

    try:
        fresh = load_goals(gfile)
    except (OSError, ValueError) as exc:
        return (
            "goals.yaml changed with paired source edit but could not "
            f"reload: {exc}"
        )
    driver.goals = fresh
    engine = getattr(driver, "engine", None)
    if engine is not None:
        engine.goals = fresh
        cache = getattr(engine, "_cache", None)
        if isinstance(cache, dict):
            cache.clear()
    runner = getattr(driver, "async_runner", None)
    if runner is not None:
        runner.goals = list(fresh)
        if engine is not None:
            runner.expensive_ids = set(engine.expensive_ids())
    if accepted_hash is not None:
        driver._goal_hash_snapshot = accepted_hash
    return None
