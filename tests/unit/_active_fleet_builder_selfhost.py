"""FLEET-E2 builder module (NOT a test module). Registers the self-hosting
develop frontend (a real attested converged commit whose diff touches the
governance surface) via the canonical ``install()`` hook, so the spawned
``run_one`` child wires it through
``PEERS_FLEET_BUILDERS=tests.unit._active_fleet_builder_selfhost``.
"""
from __future__ import annotations

from peers.fleet import run_one
from tests.unit._active_fleet_fixtures import make_selfhost_frontend


def install() -> None:
    run_one.register_frontend_builder("develop", make_selfhost_frontend)
