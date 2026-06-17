"""Tests for the type-only ``_DriverHost`` base that lets mypy resolve the
cross-mixin attribute/method surface ``OrchestratorDriver`` wires together.

The host base exists purely so ``mypy src/`` stops reporting ~135
``attr-defined`` errors of the form ``"DriverTickHooksMixin" has no
attribute "repo"``: each mixin calls ``self.<x>`` where ``<x>`` is provided
by the composing host, which mypy can't see when it type-checks a mixin in
isolation. The whole declaration lives under ``if TYPE_CHECKING`` so it
contributes ZERO runtime members — the composed class must stay
byte-identical at runtime. These tests guard exactly that contract.
"""
from __future__ import annotations

import pytest

from peers.driver_gate_pipeline import DriverGatePipelineMixin
from peers.driver_host import _DriverHost
from peers.driver_lifecycle import DriverLifecycleMixin
from peers.driver_observability import DriverObservabilityMixin
from peers.driver_peer_health import DriverPeerHealthMixin
from peers.driver_soft_reviews import DriverSoftReviewsMixin
from peers.driver_tick_hooks import DriverTickHooksMixin
from peers._driver_orchestrator_impl import OrchestratorDriver


ALL_MIXINS = [
    DriverGatePipelineMixin,
    DriverTickHooksMixin,
    DriverSoftReviewsMixin,
    DriverPeerHealthMixin,
    DriverObservabilityMixin,
    DriverLifecycleMixin,
]

# The host surface the mixins reference on ``self`` (a sample spanning data
# attributes and cross-mixin methods). Declaring these is the whole point of
# the base; none of them may exist as a *runtime* member.
DECLARED_SURFACE = [
    "repo",
    "peer_dir",
    "goals",
    "peers_by_name",
    "mode_name",
    "_save_state",
    "_attest_tick_commits",
    "_all_green_including_soft",
    "_evaluate_gates_for_tick",
    "_soft_reviews_pending",
]


def _runtime_members(cls: type) -> list[str]:
    return [n for n in vars(cls) if not (n.startswith("__") and n.endswith("__"))]


# --- happy: the composition is actually wired -------------------------------

def test_happy_orchestrator_inherits_driver_host() -> None:
    # If this regresses, mypy loses the host surface and the ~135
    # attr-defined errors return.
    assert issubclass(OrchestratorDriver, _DriverHost)


def test_happy_every_mixin_inherits_driver_host() -> None:
    for mixin in ALL_MIXINS:
        assert issubclass(mixin, _DriverHost), mixin.__name__


# --- edge: the TYPE_CHECKING block contributes nothing at runtime -----------

def test_edge_driver_host_is_runtime_empty() -> None:
    # The boundary that makes this safe: at runtime the class body is empty,
    # so inserting it into the MRO cannot change behavior.
    assert _runtime_members(_DriverHost) == []


def test_edge_driver_host_is_plain_object_subclass() -> None:
    assert _DriverHost.__bases__ == (object,)
    # An empty instance has no surprise instance dict entries either.
    assert vars(_DriverHost()) == {}


# --- sad: declarations must not shadow the host's real attributes -----------

@pytest.mark.parametrize("name", DECLARED_SURFACE)
def test_sad_declared_surface_is_not_a_runtime_attribute(name: str) -> None:
    # The failure mode this guards: if a declaration leaked to runtime (e.g.
    # ``repo: Path = None`` or a real method body), the host's value set in
    # ``__init__`` could be shadowed by a bogus class-level default. Proving a
    # bare host instance raises AttributeError proves the names are type-only.
    host = _DriverHost()
    with pytest.raises(AttributeError):
        getattr(host, name)
    assert name not in vars(_DriverHost)
