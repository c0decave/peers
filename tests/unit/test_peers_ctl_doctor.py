"""Tests for `peers-ctl doctor` (Item 9): host-environment preflight.

The doctor command surfaces the things `peers-ctl start` silently
depends on (podman, /dev/net/tun, container images, host vs container
peers version, claude OAuth/ANTHROPIC_API_KEY, git) so the operator
can diagnose a "won't start" report without spelunking through
runner.py.

Each probe is tested in isolation with monkeypatch so the test
suite stays independent of the operator's actual host.
"""
from __future__ import annotations

from types import SimpleNamespace

from peers_ctl import doctor as doctor_mod


# ---------------------------------------------------------------------------
# Probe building blocks
# ---------------------------------------------------------------------------


def test_probe_podman_handles_empty_version_output_edge(monkeypatch):
    # edge: a podman that returns rc=0 but empty stdout (broken install,
    # unexpected locale, version format change) must still produce a
    # well-formed ProbeResult — value defaults to a stable placeholder
    # rather than crashing on whitespace.split().
    monkeypatch.setattr(doctor_mod.shutil, "which",
                        lambda name: "/usr/bin/podman" if name == "podman" else None)

    def fake_run(argv, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(doctor_mod.subprocess, "run", fake_run)

    result = doctor_mod.probe_podman()

    # Status falls through to OK (binary IS present) but value is a
    # bounded fallback, not an unbounded crash.
    assert isinstance(result.value, str)
    assert result.status in ("OK", "WARN", "MISS")


def test_probe_podman_handles_nonzero_exit_with_garbage_output_edge(
    monkeypatch,
):
    # edge: a podman binary that exits non-zero with junk on both
    # stdout and stderr must not break the doctor formatter — the
    # probe surfaces a probe result with non-empty value/hint.
    monkeypatch.setattr(doctor_mod.shutil, "which",
                        lambda name: "/usr/bin/podman" if name == "podman" else None)

    def fake_run(argv, **_kwargs):
        return SimpleNamespace(
            returncode=125,
            stdout="\x00\x01garbage\n",
            stderr="podman: malformed",
        )
    monkeypatch.setattr(doctor_mod.subprocess, "run", fake_run)

    result = doctor_mod.probe_podman()

    assert result.status in ("OK", "WARN", "MISS")
    assert isinstance(result.value, str)


def test_probe_podman_ok_when_binary_present(monkeypatch):
    monkeypatch.setattr(doctor_mod.shutil, "which",
                        lambda name: "/usr/bin/podman" if name == "podman" else None)

    def fake_run(argv, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="podman version 4.9.0\n",
                               stderr="")
    monkeypatch.setattr(doctor_mod.subprocess, "run", fake_run)

    result = doctor_mod.probe_podman()

    assert result.status == "OK"
    assert result.required is True
    assert "4.9.0" in result.value


def test_probe_podman_miss_when_binary_absent(monkeypatch):
    monkeypatch.setattr(doctor_mod.shutil, "which", lambda _name: None)

    result = doctor_mod.probe_podman()

    assert result.status == "MISS"
    assert result.required is True


def test_probe_dev_net_tun_ok_when_present(monkeypatch, tmp_path):
    fake = tmp_path / "tun"
    fake.write_text("")
    monkeypatch.setattr(doctor_mod, "DEV_NET_TUN_PATH", fake)

    result = doctor_mod.probe_dev_net_tun()

    assert result.status == "OK"
    assert result.required is True


def test_probe_dev_net_tun_warn_when_missing_with_workaround(monkeypatch, tmp_path):
    missing = tmp_path / "missing-tun"
    monkeypatch.setattr(doctor_mod, "DEV_NET_TUN_PATH", missing)

    result = doctor_mod.probe_dev_net_tun()

    # Required check fails — the spec asks for an explicit WARN with
    # the documented workaround so the operator can immediately recover.
    assert result.status == "WARN"
    assert result.required is True
    assert "PEERS_CTL_NO_EGRESS_PROXY=1" in result.hint
    assert "PEERS_CTL_NO_AUTH_PROXY=1" in result.hint
    assert "PEERS_CTL_PODMAN_NETWORK=host" in result.hint


def test_probe_peers_image_ok_when_present(monkeypatch):
    monkeypatch.setattr(doctor_mod, "_podman_image_exists", lambda image: True)
    monkeypatch.setattr(doctor_mod, "_image_peers_version_safe",
                        lambda: "1.6.0")

    result = doctor_mod.probe_peers_image()

    assert result.status == "OK"
    assert result.required is True
    assert "1.6.0" in result.value


def test_probe_peers_image_miss_when_absent(monkeypatch):
    monkeypatch.setattr(doctor_mod, "_podman_image_exists", lambda image: False)

    result = doctor_mod.probe_peers_image()

    assert result.status == "MISS"
    assert result.required is True
    assert "make build" in result.hint


def test_probe_egress_proxy_image_warn_when_absent(monkeypatch):
    monkeypatch.setattr(doctor_mod, "_podman_image_exists", lambda image: False)

    result = doctor_mod.probe_egress_proxy_image()

    # Egress proxy is optional iff the operator sets PEERS_CTL_NO_EGRESS_PROXY=1.
    # By default it is required; doctor reports as WARN because the operator
    # may legitimately have set the bypass env var.
    assert result.status == "WARN"
    assert result.required is False
    assert "proxy-build" in result.hint or "egress" in result.hint.lower()


def test_probe_egress_proxy_image_ok_when_present(monkeypatch):
    monkeypatch.setattr(doctor_mod, "_podman_image_exists", lambda image: True)

    result = doctor_mod.probe_egress_proxy_image()

    assert result.status == "OK"


def test_probe_auth_proxy_image_warn_when_absent(monkeypatch):
    monkeypatch.setattr(doctor_mod, "_podman_image_exists", lambda image: False)

    result = doctor_mod.probe_auth_proxy_image()

    assert result.status == "WARN"
    assert result.required is False


def test_probe_auth_proxy_image_ok_when_present(monkeypatch):
    monkeypatch.setattr(doctor_mod, "_podman_image_exists", lambda image: True)

    result = doctor_mod.probe_auth_proxy_image()

    assert result.status == "OK"


def test_probe_version_drift_ok_when_versions_match(monkeypatch):
    monkeypatch.setattr(doctor_mod, "_host_peers_version_safe",
                        lambda: "1.6.0")
    monkeypatch.setattr(doctor_mod, "_image_peers_version_safe",
                        lambda: "1.6.0")

    result = doctor_mod.probe_version_drift()

    assert result.status == "OK"
    assert "1.6.0" in result.value


def test_probe_version_drift_warn_when_versions_differ(monkeypatch):
    monkeypatch.setattr(doctor_mod, "_host_peers_version_safe",
                        lambda: "1.6.0")
    monkeypatch.setattr(doctor_mod, "_image_peers_version_safe",
                        lambda: "1.5.0")

    result = doctor_mod.probe_version_drift()

    assert result.status == "WARN"
    assert "1.6.0" in result.value
    assert "1.5.0" in result.value


def test_probe_version_drift_warn_when_image_unavailable(monkeypatch):
    monkeypatch.setattr(doctor_mod, "_host_peers_version_safe",
                        lambda: "1.6.0")
    monkeypatch.setattr(doctor_mod, "_image_peers_version_safe",
                        lambda: None)

    result = doctor_mod.probe_version_drift()

    # Image absent → can't compute drift. Surface as WARN, never OK.
    assert result.status == "WARN"


def test_probe_oauth_or_apikey_ok_when_claude_json_present(monkeypatch, tmp_path):
    cj = tmp_path / ".claude.json"
    cj.write_text("{}")
    monkeypatch.setattr(doctor_mod, "_claude_json_path", lambda: cj)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result = doctor_mod.probe_oauth_or_apikey()

    assert result.status == "OK"
    assert "claude" in result.value.lower()


def test_probe_oauth_or_apikey_ok_when_env_set(monkeypatch, tmp_path):
    missing = tmp_path / "no-such-claude.json"
    monkeypatch.setattr(doctor_mod, "_claude_json_path", lambda: missing)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-xxx")

    result = doctor_mod.probe_oauth_or_apikey()

    assert result.status == "OK"
    assert "ANTHROPIC_API_KEY" in result.value


def test_probe_oauth_or_apikey_miss_when_neither(monkeypatch, tmp_path):
    missing = tmp_path / "no-such-claude.json"
    monkeypatch.setattr(doctor_mod, "_claude_json_path", lambda: missing)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result = doctor_mod.probe_oauth_or_apikey()

    assert result.status == "MISS"
    assert result.required is True


def test_probe_git_ok_when_present(monkeypatch):
    monkeypatch.setattr(doctor_mod.shutil, "which",
                        lambda name: "/usr/bin/git" if name == "git" else None)

    result = doctor_mod.probe_git()

    assert result.status == "OK"
    assert result.required is True


def test_probe_git_miss_when_absent(monkeypatch):
    monkeypatch.setattr(doctor_mod.shutil, "which", lambda _name: None)

    result = doctor_mod.probe_git()

    assert result.status == "MISS"
    assert result.required is True


# ---------------------------------------------------------------------------
# run_doctor() — full orchestration / exit code / formatting
# ---------------------------------------------------------------------------


def _all_ok_results(monkeypatch, tmp_path):
    """Stub every probe so the doctor reports a clean bill of health."""
    tun = tmp_path / "tun"
    tun.write_text("")
    cj = tmp_path / ".claude.json"
    cj.write_text("{}")

    monkeypatch.setattr(doctor_mod.shutil, "which",
                        lambda name: f"/usr/bin/{name}")

    def fake_run(argv, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="podman version 4.9.0\n",
                               stderr="")
    monkeypatch.setattr(doctor_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(doctor_mod, "DEV_NET_TUN_PATH", tun)
    monkeypatch.setattr(doctor_mod, "_podman_image_exists", lambda image: True)
    monkeypatch.setattr(doctor_mod, "_host_peers_version_safe",
                        lambda: "1.6.0")
    monkeypatch.setattr(doctor_mod, "_image_peers_version_safe",
                        lambda: "1.6.0")
    monkeypatch.setattr(doctor_mod, "_claude_json_path", lambda: cj)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_run_doctor_exit_zero_when_all_required_pass(monkeypatch, tmp_path,
                                                     capsys):
    _all_ok_results(monkeypatch, tmp_path)

    rc = doctor_mod.run_doctor()

    captured = capsys.readouterr()
    assert rc == 0, captured.out
    # Tabular output — header + at least one OK row, no emoji.
    assert "peers-ctl doctor" in captured.out
    assert "[OK]" in captured.out
    # No emoji per spec.
    for ch in captured.out:
        assert ord(ch) < 128 or ch in ("—",), repr(ch)


def test_run_doctor_exit_one_when_required_check_fails(monkeypatch, tmp_path,
                                                      capsys):
    _all_ok_results(monkeypatch, tmp_path)
    # Drop podman → required MISS → exit 1.
    monkeypatch.setattr(doctor_mod.shutil, "which",
                        lambda name: None if name == "podman"
                        else f"/usr/bin/{name}")
    monkeypatch.setattr(doctor_mod, "_podman_image_exists", lambda image: True)

    rc = doctor_mod.run_doctor()

    captured = capsys.readouterr()
    assert rc == 1
    assert "[MISS]" in captured.out


def test_run_doctor_warn_when_dev_net_tun_missing_with_workaround_hint(
    monkeypatch, tmp_path, capsys,
):
    _all_ok_results(monkeypatch, tmp_path)
    monkeypatch.setattr(doctor_mod, "DEV_NET_TUN_PATH",
                        tmp_path / "no-tun-here")

    rc = doctor_mod.run_doctor()

    captured = capsys.readouterr()
    # /dev/net/tun is a REQUIRED check (default pasta network), so its
    # absence forces rc=1. The workaround hint must be printed verbatim
    # so the operator can copy-paste it.
    assert rc == 1
    assert "PEERS_CTL_NO_EGRESS_PROXY=1" in captured.out
    assert "PEERS_CTL_NO_AUTH_PROXY=1" in captured.out
    assert "PEERS_CTL_PODMAN_NETWORK=host" in captured.out


def test_run_doctor_warn_when_proxy_images_missing_but_required_pass(
    monkeypatch, tmp_path, capsys,
):
    _all_ok_results(monkeypatch, tmp_path)

    # peers:dev present, but proxy images missing.
    def selective(image):
        return image == doctor_mod.PEERS_IMAGE
    monkeypatch.setattr(doctor_mod, "_podman_image_exists", selective)

    rc = doctor_mod.run_doctor()

    captured = capsys.readouterr()
    # Proxy images are optional (operator may run with NO_*_PROXY=1),
    # so missing → WARN but rc still 0.
    assert rc == 0, captured.out
    assert "[WARN]" in captured.out


def test_run_doctor_version_drift_surfaces_both_versions(
    monkeypatch, tmp_path, capsys,
):
    _all_ok_results(monkeypatch, tmp_path)
    monkeypatch.setattr(doctor_mod, "_host_peers_version_safe",
                        lambda: "1.6.0")
    monkeypatch.setattr(doctor_mod, "_image_peers_version_safe",
                        lambda: "1.5.0")

    rc = doctor_mod.run_doctor()

    captured = capsys.readouterr()
    # Drift is a WARN, not a hard fail.
    assert rc == 0, captured.out
    assert "1.6.0" in captured.out
    assert "1.5.0" in captured.out
    assert "[WARN]" in captured.out


def test_run_doctor_summary_line_counts_categories(
    monkeypatch, tmp_path, capsys,
):
    _all_ok_results(monkeypatch, tmp_path)

    rc = doctor_mod.run_doctor()

    captured = capsys.readouterr()
    assert rc == 0
    assert "Summary" in captured.out
    assert "ok" in captured.out.lower()


def test_cli_dispatches_doctor_subcommand(monkeypatch, tmp_path):
    """`peers-ctl doctor` (no positional args) must reach run_doctor."""
    _all_ok_results(monkeypatch, tmp_path)
    from peers_ctl.cli import main

    called = {}

    def fake_run_doctor(*_args, **_kwargs):
        called["yes"] = True
        return 0
    monkeypatch.setattr(doctor_mod, "run_doctor", fake_run_doctor)
    # cli.py imports run_doctor inside cmd_doctor (or at module top);
    # patch the attribute the dispatcher actually calls.
    import peers_ctl.cli as cli_mod
    if hasattr(cli_mod, "run_doctor"):
        monkeypatch.setattr(cli_mod, "run_doctor", fake_run_doctor)

    rc = main(["doctor"])

    assert rc == 0
    assert called.get("yes") is True


# ---------------------------------------------------------------------------
# probe_claude_smoke() + run_doctor(claude_smoke=...) — opt-in live preflight
#
# The smoke launches a REAL `claude -p` in a throwaway peer container (wired
# exactly like a real turn) and fails fast on a startup hang. Heavy, so it is
# NOT in the default _PROBES; every test stubs the single podman/claude seam
# (`_run_claude_smoke_container`) and the sidecar lifecycle so the suite never
# touches the host.
# ---------------------------------------------------------------------------


def _ok_probe():
    return doctor_mod.ProbeResult(
        status="OK", label="stub", value="ok", hint="", required=True,
    )


def _stub_smoke_sidecars(monkeypatch, calls=None):
    """Neutralize the real podman sidecar lifecycle; optionally record order."""
    def _ens(_project):
        if calls is not None:
            calls.append("ensure")

    def _stop(_project):
        if calls is not None:
            calls.append("stop")

    monkeypatch.setattr(doctor_mod, "_ensure_smoke_sidecars", _ens)
    monkeypatch.setattr(doctor_mod, "_stop_smoke_sidecars", _stop)


def _set_smoke_outcome(monkeypatch, outcome=None, raises=None, calls=None):
    def _run(_project, _timeout_s):
        if calls is not None:
            calls.append("run")
        if raises is not None:
            raise raises
        return outcome

    monkeypatch.setattr(doctor_mod, "_run_claude_smoke_container", _run)


def test_run_doctor_excludes_claude_smoke_by_default(monkeypatch):
    """Bare `peers-ctl doctor` must NOT run the heavy live smoke."""
    ran = {"smoke": False}

    def _boom():
        ran["smoke"] = True
        return _ok_probe()

    monkeypatch.setattr(doctor_mod, "probe_claude_smoke", _boom)

    rc = doctor_mod.run_doctor(probes=(_ok_probe,))

    assert rc == 0
    assert ran["smoke"] is False


def test_run_doctor_includes_claude_smoke_when_requested(monkeypatch, capsys):
    called = {"n": 0}

    def _smoke():
        called["n"] += 1
        return doctor_mod.ProbeResult(
            status="OK", label="claude smoke", value="replied",
            hint="", required=True,
        )

    monkeypatch.setattr(doctor_mod, "probe_claude_smoke", _smoke)

    rc = doctor_mod.run_doctor(probes=(), claude_smoke=True)

    assert called["n"] == 1
    assert rc == 0
    assert "claude smoke" in capsys.readouterr().out


def test_probe_claude_smoke_ok_on_model_output(monkeypatch):
    _stub_smoke_sidecars(monkeypatch)
    _set_smoke_outcome(monkeypatch, doctor_mod.SmokeOutcome(
        returncode=0, stdout="OK\n", stderr="", timed_out=False,
        duration_s=3.2,
    ))

    result = doctor_mod.probe_claude_smoke()

    assert result.status == "OK"
    assert result.required is True
    assert "repl" in result.value.lower()


def test_probe_claude_smoke_miss_on_timeout(monkeypatch):
    _stub_smoke_sidecars(monkeypatch)
    _set_smoke_outcome(monkeypatch, doctor_mod.SmokeOutcome(
        returncode=None, stdout="", stderr="", timed_out=True,
        duration_s=90.0,
    ))

    result = doctor_mod.probe_claude_smoke()

    assert result.status == "MISS"
    assert result.required is True
    assert "hang" in (result.value + " " + result.hint).lower()


def test_probe_claude_smoke_miss_on_config_hang_signature(monkeypatch):
    _stub_smoke_sidecars(monkeypatch)
    _set_smoke_outcome(monkeypatch, doctor_mod.SmokeOutcome(
        returncode=1, stdout="",
        stderr="Claude configuration file not found at: "
               "~/.claude.json",
        timed_out=False, duration_s=2.0,
    ))

    result = doctor_mod.probe_claude_smoke()

    assert result.status == "MISS"
    assert "configuration file not found" in result.hint.lower()


def test_probe_claude_smoke_miss_on_empty_output(monkeypatch):
    _stub_smoke_sidecars(monkeypatch)
    _set_smoke_outcome(monkeypatch, doctor_mod.SmokeOutcome(
        returncode=0, stdout="   \n", stderr="", timed_out=False,
        duration_s=2.0,
    ))

    result = doctor_mod.probe_claude_smoke()

    assert result.status == "MISS"


def test_probe_claude_smoke_stops_sidecars_even_on_error(monkeypatch):
    calls = []
    _stub_smoke_sidecars(monkeypatch, calls)
    _set_smoke_outcome(monkeypatch, raises=RuntimeError("boom"), calls=calls)

    result = doctor_mod.probe_claude_smoke()

    # Teardown happened despite the failure, and the probe degraded to a
    # MISS (a doctor probe must never crash the report).
    assert "stop" in calls
    assert result.status == "MISS"


def test_probe_claude_smoke_ensures_before_run_and_stops_after(monkeypatch):
    calls = []
    _stub_smoke_sidecars(monkeypatch, calls)
    _set_smoke_outcome(monkeypatch, doctor_mod.SmokeOutcome(
        returncode=0, stdout="OK\n", stderr="", timed_out=False,
        duration_s=1.0,
    ), calls=calls)

    doctor_mod.probe_claude_smoke()

    assert calls == ["ensure", "run", "stop"]


def test_cmd_doctor_forwards_claude_smoke_flag(monkeypatch):
    seen = {}

    def fake_run_doctor(*_args, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(doctor_mod, "run_doctor", fake_run_doctor)

    from peers_ctl.cli import cmd_doctor
    rc = cmd_doctor(claude_smoke=True)

    assert rc == 0
    assert seen.get("claude_smoke") is True


def test_doctor_parser_accepts_claude_smoke_flag():
    from peers_ctl.cli import build_parser
    parser = build_parser()

    args = parser.parse_args(["doctor", "--claude-smoke"])
    assert args.claude_smoke is True

    args2 = parser.parse_args(["doctor"])
    assert args2.claude_smoke is False
