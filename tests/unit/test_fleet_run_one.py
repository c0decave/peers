"""Stage-7 fleet DAEMON — the per-run child launcher ``run_one`` (TDD).

``python -m peers.fleet.run_one --spec <json>`` is what each slot spawns: it
deserialises a fleet run spec, resolves a mode frontend from a fail-closed
registry, and ``run_isolated()``s it (lease its own worktree + ``drive``). The
``drive`` step needs live LLM peers, so it is exercised here with a NO-OP frontend
+ the dir-copy fake provider (the WIRING is what we prove); the registry is EMPTY
by default so every un-wired real mode FAILS CLOSED (never a silent degraded run).
happy / sad / edge each.
"""
from __future__ import annotations

import json

import pytest

from peers.fleet import run_one
from tests.unit._isolation_helpers import FakeWorktreeProvider, _init_repo


def _repo(tmp_path, name="tool"):
    p = tmp_path / name
    p.mkdir()
    _init_repo(p)
    return p


def _spec_json(tmp_path, **over):
    repo = over.pop("repo", None) or _repo(tmp_path)
    d = {
        "run_id": "a",
        "tool": str(repo),
        "mode": "develop",
        "op_config": {"mode": "develop", "budget": {"max_rounds": 1}},
        "base_sha": "0" * 40,
        "branch": "peers/run/a",
    }
    d.update(over)
    return json.dumps(d), repo


class _NoopFrontend:
    """A driveable frontend that does one trivial round (no LLM)."""

    def prepare(self, run):
        pass

    def run(self, run):
        run.ledger.append(event="round", status="ok", mode_run=run.mode_run)

    def interpret(self, run):
        return {"ok": True}


# ---- happy ---------------------------------------------------------------
def test_parse_spec_round_trips(tmp_path):
    spec_json, repo = _spec_json(tmp_path)
    spec = run_one.parse_spec(spec_json)
    assert spec["run_id"] == "a" and spec["tool"] == str(repo)
    assert spec["mode"] == "develop" and spec["base_sha"] == "0" * 40
    assert spec["op_config"]["mode"] == "develop"


def test_main_drives_a_registered_frontend(tmp_path):
    spec_json, repo = _spec_json(tmp_path)
    provider = FakeWorktreeProvider(tmp_path / "leases")
    rc = run_one.main(["--spec", spec_json],
                      factory=lambda spec: _NoopFrontend(), provider=provider)
    assert rc == 0
    assert provider.leased == ["a"] and provider.released == ["a"]  # leased + torn down


# ---- sad -----------------------------------------------------------------
def test_unregistered_mode_fails_closed(tmp_path):
    # the DEFAULT registry is empty -> a real mode is not wired -> fail closed,
    # NEVER a silent degraded run.
    spec_json, _ = _spec_json(tmp_path)
    rc = run_one.main(["--spec", spec_json])               # default factory
    assert rc == 2


def test_bad_json_fails_closed(tmp_path):
    rc = run_one.main(["--spec", "{not json"])
    assert rc == 2


def test_missing_required_field_fails_closed(tmp_path):
    spec_json, _ = _spec_json(tmp_path)
    bad = json.loads(spec_json)
    del bad["run_id"]
    rc = run_one.main(["--spec", json.dumps(bad)])
    assert rc == 2


def test_frontend_error_is_fail_closed_but_releases(tmp_path):
    spec_json, _ = _spec_json(tmp_path)
    provider = FakeWorktreeProvider(tmp_path / "leases")

    class _Boom:
        def prepare(self, run):
            raise RuntimeError("frontend exploded")

        def run(self, run):
            pass

        def interpret(self, run):
            return {}

    rc = run_one.main(["--spec", spec_json],
                      factory=lambda spec: _Boom(), provider=provider)
    assert rc == 1
    assert provider.released == ["a"]                      # run_isolated tore down anyway


# ---- edge ----------------------------------------------------------------
def test_missing_spec_arg_is_argparse_error(tmp_path):
    with pytest.raises(SystemExit):
        run_one.main([])


def test_default_factory_raises_for_unregistered_mode(tmp_path):
    with pytest.raises(run_one.UnsupportedFleetMode):
        run_one.default_factory({"mode": "develop"})


def test_register_frontend_builder_makes_a_mode_resolvable(tmp_path):
    run_one.register_frontend_builder("__test_mode__", lambda spec: _NoopFrontend())
    try:
        fe = run_one.default_factory({"mode": "__test_mode__"})
        assert isinstance(fe, _NoopFrontend)
    finally:
        run_one._FRONTEND_BUILDERS.pop("__test_mode__", None)
