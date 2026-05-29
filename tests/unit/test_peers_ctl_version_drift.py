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


def test_enforce_drift_refuses_warn_for_audit_mode(monkeypatch):
    """audit-mode must refuse minor/patch drift (not just warn).

    Phase-1 motivation: v12 was bootstrapped with `peers-ctl new --container
    --modes=audit,thorough` while peers:dev was 1.5.0 and host was 1.6.0.
    `peers init` inside the container wrote `.peers/config.yaml` from the
    OLD 1.5.0 template, which lacks the stream-json claude argv → tick 11
    print-mode idle-timeout. Audit-mode integrity requires aligned versions.
    """
    import peers_ctl.runner as runner_mod
    monkeypatch.setattr(
        runner_mod, "check_container_version_drift",
        lambda: ("warn", "container peers=1.5.0, host peers=1.6.0"),
    )
    try:
        runner_mod.enforce_container_drift_for_modes(["audit", "thorough"])
    except RuntimeError as e:
        assert "audit" in str(e).lower() or "refuse" in str(e).lower()
        assert "1.5.0" in str(e) and "1.6.0" in str(e)
        return
    raise AssertionError("audit-mode warn-level drift should raise RuntimeError")


def test_enforce_drift_passes_warn_for_non_audit(monkeypatch):
    """Non-audit modes still see warn (printed by caller), not error."""
    import peers_ctl.runner as runner_mod
    monkeypatch.setattr(
        runner_mod, "check_container_version_drift",
        lambda: ("warn", "container peers=1.5.0, host peers=1.6.0"),
    )
    level, msg = runner_mod.enforce_container_drift_for_modes(["implement"])
    assert level == "warn"
    assert "1.5.0" in msg


def test_enforce_drift_always_errors_on_error(monkeypatch):
    """Even non-audit modes refuse on error-level drift (major mismatch)."""
    import peers_ctl.runner as runner_mod
    monkeypatch.setattr(
        runner_mod, "check_container_version_drift",
        lambda: ("error", "container peers=0.8.0, host peers=1.6.0; major-version drift is unsafe."),
    )
    try:
        runner_mod.enforce_container_drift_for_modes(["implement"])
    except RuntimeError as e:
        assert "major" in str(e).lower() or "unsafe" in str(e).lower()
        return
    raise AssertionError("error-level drift should always raise RuntimeError")


def test_enforce_drift_bypass_via_env(monkeypatch):
    """PEERS_CTL_ALLOW_DRIFT=1 lets operators override the refuse (audit-mode escape valve)."""
    import peers_ctl.runner as runner_mod
    monkeypatch.setattr(
        runner_mod, "check_container_version_drift",
        lambda: ("warn", "container peers=1.5.0, host peers=1.6.0"),
    )
    monkeypatch.setenv("PEERS_CTL_ALLOW_DRIFT", "1")
    # Bypass should let audit-mode through with the original warn level.
    level, msg = runner_mod.enforce_container_drift_for_modes(["audit"])
    assert level == "warn"


def test_enforce_drift_ok_passes_through(monkeypatch):
    """ok-level drift returns ok regardless of modes."""
    import peers_ctl.runner as runner_mod
    monkeypatch.setattr(
        runner_mod, "check_container_version_drift",
        lambda: ("ok", ""),
    )
    level, msg = runner_mod.enforce_container_drift_for_modes(["audit"])
    assert level == "ok"


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
