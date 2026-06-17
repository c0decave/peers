"""A MISBEHAVING fleet builder for the rollback test (NOT a test module): it
registers at import time (violating the install()-only contract) AND its
install() raises. _load_env_builders must roll back so the mode stays
fail-closed."""
from __future__ import annotations

from peers.fleet import run_one


def _bad_frontend(spec):
    return object()


# Contract violation on purpose: register at import time.
run_one.register_frontend_builder("develop", _bad_frontend)


def install() -> None:
    raise RuntimeError("install exploded")
