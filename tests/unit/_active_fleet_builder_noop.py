"""FLEET-S2 builder module (NOT a test module). Registers the LYING / no-op
develop frontend via the canonical ``install()`` hook, so the spawned ``run_one``
child wires it through ``PEERS_FLEET_BUILDERS=tests.unit._active_fleet_builder_noop``.
"""
from __future__ import annotations

from peers.fleet import run_one
from tests.unit._active_fleet_fixtures import make_noop_frontend


def install() -> None:
    run_one.register_frontend_builder("develop", make_noop_frontend)
