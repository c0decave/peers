"""Implement-mode acceptance / honesty check modules.

Each sibling module in this package is a standalone gate script invoked by an
implement-mode goal's check command. The package init intentionally carries no
logic — submodules are imported directly (``from ...implement.checks import
no_skipped_tests``).
"""
from __future__ import annotations
