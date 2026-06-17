"""Thin public facade for the orchestrator runtime.

The runtime implementation lives in :mod:`peers._driver_orchestrator_impl`.
This module intentionally aliases that implementation module so existing
imports and test monkeypatches against ``peers.driver_orchestrator`` keep
affecting the actual runtime globals.
"""
from __future__ import annotations

import sys as _sys
from typing import TYPE_CHECKING

from peers import _driver_orchestrator_impl as _impl

if TYPE_CHECKING:
    # At runtime this module object is replaced by ``_impl`` (below), which
    # mypy cannot follow. Re-export the public symbols so static imports
    # against this facade (e.g. ``from peers.driver_orchestrator import
    # OrchestratorDriver``) resolve for the type checker.
    from peers._driver_orchestrator_impl import (
        OrchestratorDriver as OrchestratorDriver,
    )


_sys.modules[__name__] = _impl
