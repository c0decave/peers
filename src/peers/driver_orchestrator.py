"""Thin public facade for the orchestrator runtime.

The runtime implementation lives in :mod:`peers._driver_orchestrator_impl`.
This module intentionally aliases that implementation module so existing
imports and test monkeypatches against ``peers.driver_orchestrator`` keep
affecting the actual runtime globals.
"""
from __future__ import annotations

import sys as _sys

from peers import _driver_orchestrator_impl as _impl


_sys.modules[__name__] = _impl
