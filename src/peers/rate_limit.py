"""Transient rate-limit handling helpers (v17 internal testing operator finding).

A transient server rate-limit (HTTP 429/5xx/overloaded — explicitly "not your
usage limit") is classified ``rate-limited`` by ``health_guard`` (see
``structured_halt.classify_structured_transient``). The loop then rotates to the
other peer without degrading the rate-limited one; this module holds the small
explicit backoff applied between ticks so a run where EVERY peer is being
rate-limited at once does not hot-spin.
"""
from __future__ import annotations

#: Cap on the explicit inter-tick backoff (seconds). The dominant spacing is the
#: other peer's tick; this only guards against an all-peers-limited hot spin.
RATE_LIMIT_BACKOFF_CAP_S = 120


def rate_limit_backoff_s(streak: int) -> int:
    """Seconds to wait after ``streak`` consecutive rate-limited ticks.

    Exponential 15→30→60→120, capped at ``RATE_LIMIT_BACKOFF_CAP_S``; 0 when
    ``streak <= 0`` (no rate-limit, no wait).
    """
    if streak <= 0:
        return 0
    return min(15 * (2 ** (streak - 1)), RATE_LIMIT_BACKOFF_CAP_S)
