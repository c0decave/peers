"""BUG-274 helper: hot-reload the in-memory goal list when the
mutation guard accepts a paired ``goals.yaml`` + source commit.

Split out of :mod:`peers.driver_tick_hooks` for the module line-budget
(facade test caps that file at 800 lines).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


_MISSING = object()


def _reload_diagnostic(exc: Exception) -> str:
    return (
        "goals.yaml changed with paired source edit but could not "
        f"reload: {exc}"
    )


def _restore_attr(obj: Any, name: str, value: Any) -> None:
    """Restore ``obj.name`` to a snapshotted value, or delete it if the
    attribute did not exist before (the ``_MISSING`` sentinel)."""
    if obj is None:
        return
    if value is _MISSING:
        try:
            delattr(obj, name)
        except AttributeError:
            pass
    else:
        setattr(obj, name, value)


def _reload_driver_goals(
    driver: Any, gfile: Path, accepted_hash: str | None = None,
) -> str | None:
    """Rebind ``driver.goals`` and ``driver.engine.goals`` (plus the
    pipelined async runner's goal list, if present) to whatever the
    on-disk ``gfile`` currently parses to.

    Returns ``None`` on success. On load failure, returns a diagnostic
    string and leaves the in-memory state untouched so the mutation
    guard can fail closed without advancing its hash snapshot.

    BUG-501 (v22 harvest): the publish is TRANSACTIONAL. Every reference
    rebound below is snapshotted first; a failure AFTER a successful parse
    restores the old driver/engine/runner/cache/snapshot references before
    returning a diagnostic, so a partial update can never leave the driver
    in a mixed (some-new, some-old) goal state. When ``accepted_hash`` is
    supplied, the snapshot is advanced only after every reference is rebound.

    The engine's verdict cache is cleared because a goal's cmd may have
    changed; a memoized PASS keyed only by (tree, HEAD) would otherwise
    serve the OLD cmd's verdict for a fresh evaluation of the NEW cmd.
    """
    from peers.goals import load_goals

    try:
        fresh = load_goals(gfile)
    except (OSError, ValueError) as exc:
        return _reload_diagnostic(exc)

    engine = getattr(driver, "engine", None)
    runner = getattr(driver, "async_runner", None)
    cache = getattr(engine, "_cache", None) if engine is not None else None
    cache_dict: dict[Any, Any] | None = cache if isinstance(cache, dict) else None

    # snapshot every reference we are about to rebind so a publish
    # failure can roll back to the old state (no mixed-state corruption).
    driver_goals_before = getattr(driver, "goals", _MISSING)
    engine_goals_before = (
        getattr(engine, "goals", _MISSING) if engine is not None else _MISSING
    )
    runner_goals_before = (
        getattr(runner, "goals", _MISSING) if runner is not None else _MISSING
    )
    runner_expensive_before = (
        getattr(runner, "expensive_ids", _MISSING)
        if runner is not None else _MISSING
    )
    snapshot_before = getattr(driver, "_goal_hash_snapshot", _MISSING)
    cache_before = dict(cache_dict) if cache_dict is not None else None

    try:
        driver.goals = fresh
        if engine is not None:
            engine.goals = fresh
        if runner is not None:
            runner.goals = list(fresh)
            if engine is not None:
                runner.expensive_ids = set(engine.expensive_ids())
        if cache_dict is not None:
            cache_dict.clear()
        if accepted_hash is not None:
            driver._goal_hash_snapshot = accepted_hash
        # BUG-504 (v22 harvest): after publishing the new goal set, drop any
        # in-flight async-gate verdicts computed against the OLD goal set, so a
        # future submitted at the end of the previous tick cannot be served
        # back as a fresh same-SHA result. No-op if the runner lacks it.
        if runner is not None:
            invalidate = getattr(runner, "invalidate_in_flight", None)
            if callable(invalidate):
                invalidate()
    except Exception as exc:
        # BUG-501 rollback: restore every reference + the cache, fail closed.
        if cache_dict is not None and cache_before is not None:
            try:
                cache_dict.clear()
                cache_dict.update(cache_before)
            except Exception:
                pass
        for obj, name, value in (
            (driver, "goals", driver_goals_before),
            (engine, "goals", engine_goals_before),
            (runner, "goals", runner_goals_before),
            (runner, "expensive_ids", runner_expensive_before),
            (driver, "_goal_hash_snapshot", snapshot_before),
        ):
            try:
                _restore_attr(obj, name, value)
            except Exception:
                pass
        return _reload_diagnostic(exc)

    return None
