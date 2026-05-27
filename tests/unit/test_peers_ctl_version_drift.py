"""Container image version drift checks for `peers-ctl start --container`."""
from __future__ import annotations

from types import SimpleNamespace

from peers_ctl.runner import check_container_version_drift


def test_drift_check_returns_ok_when_versions_match(monkeypatch):
    monkeypatch.setattr("peers_ctl.runner._image_peers_version", lambda: "1.4.0")
    monkeypatch.setattr("peers_ctl.runner._host_peers_version", lambda: "1.4.0")

    level, msg = check_container_version_drift()

    assert level == "ok"
    assert msg == ""


def test_drift_check_warns_on_minor_mismatch(monkeypatch):
    monkeypatch.setattr("peers_ctl.runner._image_peers_version", lambda: "1.3.0")
    monkeypatch.setattr("peers_ctl.runner._host_peers_version", lambda: "1.4.0")

    level, msg = check_container_version_drift()

    assert level == "warn"
    assert "1.3.0" in msg and "1.4.0" in msg


def test_drift_check_errors_on_major_mismatch(monkeypatch):
    monkeypatch.setattr("peers_ctl.runner._image_peers_version", lambda: "0.8.0")
    monkeypatch.setattr("peers_ctl.runner._host_peers_version", lambda: "1.4.0")

    level, msg = check_container_version_drift()

    assert level == "error"
    assert "make build" in msg


def test_drift_check_skips_when_image_query_fails(monkeypatch):
    monkeypatch.setattr("peers_ctl.runner._image_peers_version", lambda: None)
    monkeypatch.setattr("peers_ctl.runner._host_peers_version", lambda: "1.4.0")

    level, msg = check_container_version_drift()

    assert level == "skipped"
    assert msg == ""


def test_image_version_query_uses_explicit_network(monkeypatch):
    import peers_ctl.runner as runner_mod

    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="peers 1.4.0")

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(runner_mod, "PODMAN_NETWORK", "")

    assert runner_mod._image_peers_version() == "1.4.0"
    assert "--network=none" in calls[-1]

    monkeypatch.setattr(runner_mod, "PODMAN_NETWORK", "host")

    assert runner_mod._image_peers_version() == "1.4.0"
    assert "--network=host" in calls[-1]
