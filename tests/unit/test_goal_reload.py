"""`_reload_driver_goals` — transactional hot-reload (v22 harvest).

Covers the success path, the parse-failure fail-closed path, the BUG-501
rollback (a publish-step exception after a good parse restores ALL the old
references — no mixed state), and the BUG-504 in-flight invalidation hook.
"""
from __future__ import annotations

import types

from peers import goal_reload


class _Engine:
    def __init__(self, goals, expensive):
        self.goals = goals
        self._cache = {"memo": 1}
        self._expensive = expensive
        self.raise_expensive = False

    def expensive_ids(self):
        if self.raise_expensive:
            raise RuntimeError("expensive_ids boom")
        return self._expensive


class _Runner:
    def __init__(self, goals, expensive):
        self.goals = goals
        self.expensive_ids = expensive
        self.invalidated = 0

    def invalidate_in_flight(self):
        self.invalidated += 1


def _driver(goals, expensive):
    eng = _Engine(list(goals), set(expensive))
    run = _Runner(list(goals), set(expensive))
    drv = types.SimpleNamespace(
        goals=list(goals), engine=eng, async_runner=run,
        _goal_hash_snapshot="OLDHASH",
    )
    return drv, eng, run


def test_reload_success_rebinds_and_invalidates(tmp_path, monkeypatch):
    fresh = ["g-new"]
    monkeypatch.setattr("peers.goals.load_goals", lambda _f: list(fresh))
    drv, eng, run = _driver(["g-old"], {"e-old"})

    out = goal_reload._reload_driver_goals(
        drv, tmp_path / "goals.yaml", "NEWHASH")

    assert out is None
    assert drv.goals == fresh and eng.goals == fresh and run.goals == fresh
    assert eng._cache == {}                 # cmd may have changed -> cache cleared
    assert drv._goal_hash_snapshot == "NEWHASH"
    assert run.invalidated == 1             # BUG-504 hook fired


def test_reload_parse_failure_leaves_state_untouched(tmp_path, monkeypatch):
    def boom(_f):
        raise ValueError("bad yaml")

    monkeypatch.setattr("peers.goals.load_goals", boom)
    drv, eng, run = _driver(["g-old"], {"e-old"})

    out = goal_reload._reload_driver_goals(
        drv, tmp_path / "goals.yaml", "NEWHASH")

    assert out is not None and "could not" in out
    assert drv.goals == ["g-old"] and eng.goals == ["g-old"]
    assert drv._goal_hash_snapshot == "OLDHASH"
    assert run.invalidated == 0


def test_reload_publish_failure_rolls_back(tmp_path, monkeypatch):
    """BUG-501: a publish-step exception AFTER a good parse restores ALL the
    old references (driver/engine/runner/cache/snapshot) and fails closed."""
    monkeypatch.setattr("peers.goals.load_goals", lambda _f: ["g-new"])
    drv, eng, run = _driver(["g-old"], {"e-old"})
    cache_ref = eng._cache
    eng.raise_expensive = True              # blow up mid-publish

    out = goal_reload._reload_driver_goals(
        drv, tmp_path / "goals.yaml", "NEWHASH")

    assert out is not None and "could not" in out
    # every rebound reference rolled back to its OLD value
    assert drv.goals == ["g-old"]
    assert eng.goals == ["g-old"]
    assert run.goals == ["g-old"]
    assert run.expensive_ids == {"e-old"}
    assert drv._goal_hash_snapshot == "OLDHASH"   # snapshot NOT advanced
    assert eng._cache == {"memo": 1} and eng._cache is cache_ref  # cache restored
    assert run.invalidated == 0             # never reached the invalidate step


def test_reload_with_no_engine_or_runner_still_works_edge(tmp_path, monkeypatch):
    """EDGE: a degenerate driver that has neither ``engine`` nor ``async_runner``
    (the optional collaborators) must still rebind ``driver.goals`` and advance
    the snapshot — every ``getattr(..., None)`` guard holds, no AttributeError."""
    monkeypatch.setattr("peers.goals.load_goals", lambda _f: ["g-new"])
    drv = types.SimpleNamespace(goals=["g-old"])   # no engine, no async_runner

    out = goal_reload._reload_driver_goals(drv, tmp_path / "goals.yaml", "NEWHASH")

    assert out is None
    assert drv.goals == ["g-new"]
    assert drv._goal_hash_snapshot == "NEWHASH"
