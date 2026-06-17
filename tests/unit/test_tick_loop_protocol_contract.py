"""Contract tests: the narrow Protocol surfaces in ``peers.tick_loop`` must
stay in lock-step with the real implementations they describe.

Motivation: ``TickLoop._finalize_tick`` threads a ``rate_limited``
flag through four collaborators -- it calls

    turn_manager.advance(success=success, rate_limited=rate_limited)        # tick_loop.py:432
    driver._record_tick_accounting(..., rate_limited=rate_limited)          # tick_loop.py:434
    driver._update_peer_health(..., rate_limited=rate_limited)              # tick_loop.py:440
    driver._update_convergence_counter(state, success, rate_limited)        # tick_loop.py:473

When the ``rate_limited`` plumbing landed (the "v17 finding"), the
``_record_tick_accounting`` and ``_update_convergence_counter`` Protocol
signatures were updated -- but ``_TurnManager.advance`` and
``_update_peer_health`` were missed. The real ``TurnManager.advance`` /
``DriverPeerHealthMixin._update_peer_health`` *do* accept ``rate_limited``,
so production never crashed; but the type contract was wrong (mypy:
"Unexpected keyword argument 'rate_limited'") and any NEW collaborator
written to the *declared* Protocol would omit the parameter and raise
``TypeError`` the first time a peer is rate-limited.

These tests pin the contract so the drift cannot silently recur.
"""

from __future__ import annotations

import inspect

import pytest

from peers.driver_peer_health import DriverPeerHealthMixin
from peers.driver_tick_hooks import DriverTickHooksMixin
from peers.tick_loop import TickLoopDriver, _TurnManager
from peers.turn_manager import TurnManager

# (label, declared Protocol method, concrete implementation it stands for).
# Every entry is a call site in TickLoop that forwards ``rate_limited``.
_RATE_LIMITED_CONTRACT = [
    ("turn_manager.advance", _TurnManager.advance, TurnManager.advance),
    (
        "driver._record_tick_accounting",
        TickLoopDriver._record_tick_accounting,
        DriverTickHooksMixin._record_tick_accounting,
    ),
    (
        "driver._update_peer_health",
        TickLoopDriver._update_peer_health,
        DriverPeerHealthMixin._update_peer_health,
    ),
    (
        "driver._update_convergence_counter",
        TickLoopDriver._update_convergence_counter,
        DriverTickHooksMixin._update_convergence_counter,
    ),
]


@pytest.mark.parametrize(
    "label, protocol_method",
    [(lbl, proto) for lbl, proto, _impl in _RATE_LIMITED_CONTRACT],
)
def test_protocol_declares_rate_limited(label, protocol_method):
    """happy: every Protocol method the tick loop forwards ``rate_limited``
    to must declare a ``rate_limited`` parameter."""
    params = inspect.signature(protocol_method).parameters
    assert "rate_limited" in params, (
        f"{label}: tick_loop forwards rate_limited= but the Protocol "
        f"signature does not declare it (declares {list(params)})"
    )


@pytest.mark.parametrize(
    "label, protocol_method, impl_method",
    _RATE_LIMITED_CONTRACT,
)
def test_protocol_and_impl_agree_on_rate_limited_default(
    label, protocol_method, impl_method
):
    """edge: the Protocol and the concrete implementation must agree that
    ``rate_limited`` exists AND is optional (``bool = False``). Catches drift
    in either direction -- a Protocol that adds it while the impl drops it,
    or an impl that makes it required while callers omit it."""
    proto_params = inspect.signature(protocol_method).parameters
    impl_params = inspect.signature(impl_method).parameters
    assert "rate_limited" in impl_params, (
        f"{label}: concrete impl no longer accepts rate_limited"
    )
    assert "rate_limited" in proto_params, (
        f"{label}: Protocol no longer declares rate_limited"
    )
    # Both sides must keep it optional so existing positional callers and the
    # keyword call sites continue to type-check.
    assert impl_params["rate_limited"].default is False
    assert proto_params["rate_limited"].default is False


def test_turn_manager_advance_accepts_success_keyword():
    """edge: the call site uses ``advance(success=..., rate_limited=...)`` --
    both must be bindable by keyword on the real TurnManager."""
    sig = inspect.signature(TurnManager.advance)
    bound = sig.bind(object(), success=True, rate_limited=False)
    assert bound.arguments["success"] is True
    assert bound.arguments["rate_limited"] is False


def test_turn_manager_conforming_to_old_protocol_breaks_call_site():
    """sad: a turn manager implementing only the *previously declared*
    Protocol surface (``advance(self, *, success)``) is insufficient -- it
    raises TypeError the moment the tick loop forwards ``rate_limited``.
    This is the concrete runtime consequence the contract test guards
    against, so the Protocol MUST advertise ``rate_limited``."""

    class _OldContractTurnManager:
        def advance(self, *, success: bool) -> None:  # missing rate_limited
            pass

    stub = _OldContractTurnManager()
    with pytest.raises(TypeError):
        # Exactly how TickLoop._finalize_tick calls it (tick_loop.py:432).
        stub.advance(success=True, rate_limited=False)
