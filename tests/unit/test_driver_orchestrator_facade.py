"""Thin facade checks for the public driver_orchestrator module."""
from __future__ import annotations

from pathlib import Path


def test_driver_orchestrator_public_module_aliases_impl() -> None:
    import peers._driver_orchestrator_impl as impl
    import peers.driver_orchestrator as public

    assert public is impl


def test_driver_orchestrator_public_file_stays_thin() -> None:
    root = Path(__file__).resolve().parents[2]
    public_file = root / "src" / "peers" / "driver_orchestrator.py"

    assert len(public_file.read_text(encoding="utf-8").splitlines()) < 500


def test_driver_orchestrator_impl_stays_a_thin_coordinator() -> None:
    root = Path(__file__).resolve().parents[2]
    peers_dir = root / "src" / "peers"

    assert len(
        (peers_dir / "_driver_orchestrator_impl.py")
        .read_text(encoding="utf-8")
        .splitlines()
    ) < 500

    extracted = (
        "driver_helpers.py",
        "driver_lifecycle.py",
        "driver_observability.py",
        "driver_peer_health.py",
        "driver_soft_reviews.py",
        "driver_tick_hooks.py",
    )
    for filename in extracted:
        assert len(
            (peers_dir / filename).read_text(encoding="utf-8").splitlines()
        ) < 800
