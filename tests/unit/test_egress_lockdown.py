"""Egress isolation: the proxy must (1) reach every operator-allowlisted host
(no silently-dropped last entry), (2) FORCE all container egress through
tinyproxy via an in-netns firewall lockdown (clearing HTTP_PROXY must not grant
direct internet), and (3) fail CLOSED if the lockdown cannot be installed.

Root cause:
  #1b  the entrypoint's `while read` dropped the final PEERS_EGRESS_EXTRA_HOSTS
       entry (no trailing newline) -> an allowlisted host 403'd.
  #2   the main container joins the proxy's netns, which has a working default
       route; nothing forced egress through tinyproxy, so `unset HTTP_PROXY;
       curl` reached arbitrary (non-allowlisted) hosts.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from peers_ctl import runner
from peers_ctl.store import Project

ENTRYPOINT = Path(__file__).resolve().parents[2] / "proxy" / "entrypoint.sh"
BASE_FILTER = Path(__file__).resolve().parents[2] / "proxy" / "filter-allow.txt"


def _run_entrypoint(
    tmp_path: Path,
    extra_hosts: str | None,
    *,
    firewall_rc: int = 0,
):
    """Run the real proxy entrypoint against temp paths with the egress tools
    stubbed onto PATH. Returns (CompletedProcess, runtime_filter_text,
    firewall_log_lines)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fw_log = tmp_path / "fw.log"
    # Stub iptables/ip6tables: log argv, exit firewall_rc.
    for name in ("iptables", "ip6tables"):
        stub = bindir / name
        stub.write_text(
            "#!/bin/sh\n"
            f'printf "%s %s\\n" "{name}" "$*" >> "{fw_log}"\n'
            f"exit {firewall_rc}\n"
        )
        stub.chmod(0o755)
    # Stub su-exec/tinyproxy: record that we reached the daemon launch.
    launch_log = tmp_path / "launched"
    suexec = bindir / "su-exec"
    suexec.write_text(
        "#!/bin/sh\n"
        f'echo "$@" > "{launch_log}"\n'
        "exit 0\n"
    )
    suexec.chmod(0o755)
    (bindir / "tinyproxy").write_text("#!/bin/sh\nexit 0\n")
    (bindir / "tinyproxy").chmod(0o755)

    runtime_filter = tmp_path / "tinyproxy-filter"
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["PEERS_EGRESS_BASE_FILTER"] = str(BASE_FILTER)
    env["PEERS_EGRESS_RUNTIME_FILTER"] = str(runtime_filter)
    if extra_hosts is not None:
        env["PEERS_EGRESS_EXTRA_HOSTS"] = extra_hosts
    else:
        env.pop("PEERS_EGRESS_EXTRA_HOSTS", None)

    proc = subprocess.run(
        ["sh", str(ENTRYPOINT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    text = runtime_filter.read_text() if runtime_filter.exists() else ""
    fw = fw_log.read_text().splitlines() if fw_log.exists() else []
    return proc, text, fw


# --------------------------------------------------------------------------
# #1b — allowlist propagation (happy / edge): no host is silently dropped
# --------------------------------------------------------------------------

def test_single_extra_host_is_not_dropped(tmp_path):
    """A single host (no trailing newline) must reach the runtime filter.
    Regression for the `while read` EOF bug that 403'd an allowlisted host."""
    proc, text, _ = _run_entrypoint(tmp_path, r"^raw\.githubusercontent\.com$")
    assert proc.returncode == 0, proc.stderr
    assert r"^raw\.githubusercontent\.com$" in text


def test_last_of_many_hosts_is_not_dropped(tmp_path):
    """The FINAL comma-joined entry must survive (the dropped one in the wild)."""
    hosts = r"^a\.test$,^b\.test$,^last\.example\.org$"
    _, text, _ = _run_entrypoint(tmp_path, hosts)
    for h in (r"^a\.test$", r"^b\.test$", r"^last\.example\.org$"):
        assert h in text, f"{h} missing from runtime filter"


def test_empty_extra_hosts_is_safe(tmp_path):
    """No extra hosts -> filter is just the baked-in base, no crash (sad path)."""
    proc, text, _ = _run_entrypoint(tmp_path, "")
    assert proc.returncode == 0, proc.stderr
    assert "api.anthropic.com" in text  # base allow-list present
    # no project-specific additions header when empty
    assert "Runtime project-specific" not in text


def test_blank_entries_between_hosts_are_skipped(tmp_path):
    """Empty fields (e.g. trailing comma) are skipped, real hosts kept (edge)."""
    _, text, _ = _run_entrypoint(tmp_path, r"^x\.test$,,^y\.test$,")
    assert r"^x\.test$" in text
    assert r"^y\.test$" in text


# --------------------------------------------------------------------------
# #2 — egress lockdown is installed, default-deny, fail-closed
# --------------------------------------------------------------------------

def test_lockdown_installs_default_deny_and_allow_rules(tmp_path):
    """The entrypoint must install, for BOTH IPv4 and IPv6: default-DROP on
    OUTPUT, an allow for loopback, and an allow for tinyproxy's uid."""
    _, _, fw = _run_entrypoint(tmp_path, r"^ok\.test$")
    joined = "\n".join(fw)
    for fam in ("iptables", "ip6tables"):
        assert f"{fam} -P OUTPUT DROP" in joined, f"{fam} missing default-deny"
        assert f"{fam} -A OUTPUT -o lo -j ACCEPT" in joined, f"{fam} loopback"
        assert (
            f"{fam} -A OUTPUT -m owner --uid-owner 100 -j ACCEPT" in joined
        ), f"{fam} proxy-uid allow"


def test_lockdown_runs_before_daemon_launch(tmp_path):
    """tinyproxy must only be launched after the firewall is up (ordering:
    a daemon that started before the lockdown would have a window of open
    egress for the joined container)."""
    proc, _, fw = _run_entrypoint(tmp_path, None)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "launched").exists(), "tinyproxy was never launched"
    assert fw, "no firewall rules installed before launch"


def test_fail_closed_when_firewall_cannot_be_installed(tmp_path):
    """If iptables fails (e.g. no CAP_NET_ADMIN), the entrypoint must REFUSE
    to start the proxy (non-zero exit) rather than run with open egress."""
    proc, _, _ = _run_entrypoint(tmp_path, r"^ok\.test$", firewall_rc=1)
    assert proc.returncode != 0, "entrypoint must fail closed on firewall error"
    assert not (tmp_path / "launched").exists(), (
        "tinyproxy must NOT launch when the lockdown failed"
    )


# --------------------------------------------------------------------------
# #2 wiring — proxy container is granted exactly the caps the lockdown needs
# --------------------------------------------------------------------------

def test_proxy_argv_grants_netadmin_and_privdrop_caps(tmp_path):
    project = Project(name="p", path=str(tmp_path))
    argv = runner._build_proxy_argv(project)
    assert "--cap-drop=ALL" in argv  # still drops everything by default
    assert "--cap-add=NET_ADMIN" in argv  # to install the firewall
    # su-exec drops root -> tinyproxy after the firewall is up
    assert "--cap-add=SETUID" in argv
    assert "--cap-add=SETGID" in argv


def test_proxy_argv_labels_egress_allow_digest(tmp_path, monkeypatch):
    """The launched proxy is stamped with the digest of the allow-list it was
    built for, so a later start can detect drift and recreate it (#1a)."""
    monkeypatch.setattr(runner, "_project_uses_openrouter", lambda _p: False)
    (tmp_path / ".peers").mkdir()
    (tmp_path / ".peers" / "config.yaml").write_text(
        "egress_allow:\n  - '^rfc-editor\\.org$'\n"
    )
    project = Project(name="p", path=str(tmp_path))
    argv = runner._build_proxy_argv(project)
    digest = runner._egress_allow_digest(runner._egress_extra_allow_hosts(project))
    assert f"--label=peers.egress_allow_digest={digest}" in argv


# --------------------------------------------------------------------------
# #1a — a changed egress_allow restarts the stale proxy
# --------------------------------------------------------------------------

def _proj_with_allow(tmp_path: Path, host: str) -> Project:
    (tmp_path / ".peers").mkdir(exist_ok=True)
    (tmp_path / ".peers" / "config.yaml").write_text(
        f"egress_allow:\n  - '{host}'\n"
    )
    return Project(name="p", path=str(tmp_path))


def test_running_proxy_with_matching_digest_is_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_project_uses_openrouter", lambda _p: False)
    project = _proj_with_allow(tmp_path, r"^ok\.test$")
    want = runner._egress_allow_digest(runner._egress_extra_allow_hosts(project))
    monkeypatch.setattr(runner, "_container_running", lambda _n: True)
    monkeypatch.setattr(runner, "_proxy_egress_digest", lambda _n: want)

    started = []
    monkeypatch.setattr(
        runner, "_run_proxy_container",
        lambda _p: started.append("run"),
    )
    stopped = []
    monkeypatch.setattr(
        runner, "_stop_egress_proxy_best_effort",
        lambda _p: stopped.append("stop"),
    )
    runner._ensure_egress_proxy_running(project)
    assert started == [], "matching proxy must be reused, not restarted"
    assert stopped == []


def test_running_proxy_with_stale_digest_is_recreated(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_project_uses_openrouter", lambda _p: False)
    project = _proj_with_allow(tmp_path, r"^new\.test$")
    monkeypatch.setattr(runner, "_container_running", lambda _n: True)
    # running proxy was built for a DIFFERENT (older) allow-list
    monkeypatch.setattr(runner, "_proxy_egress_digest", lambda _n: "stale-digest")

    order = []
    monkeypatch.setattr(
        runner, "_stop_egress_proxy_best_effort",
        lambda _p: order.append("stop"),
    )
    monkeypatch.setattr(
        runner, "_cleanup_stale_container", lambda _n: order.append("cleanup")
    )

    def _fake_run(_p):
        order.append("run")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runner, "_run_proxy_container", _fake_run)
    monkeypatch.setattr(runner, "_proxy_is_live", lambda _n: True)
    runner._ensure_egress_proxy_running(project)
    assert "stop" in order, "stale proxy must be stopped"
    assert order.index("stop") < order.index("run"), "stop before re-run"


def test_proxy_that_exits_after_start_fails_closed_with_clear_error(
    tmp_path, monkeypatch,
):
    """If the proxy starts but immediately exits (the entrypoint's fail-closed
    egress lockdown, e.g. no CAP_NET_ADMIN), the runner must raise a clear
    error rather than let the main container join a dead/absent netns."""
    monkeypatch.setattr(runner, "_project_uses_openrouter", lambda _p: False)
    project = _proj_with_allow(tmp_path, r"^ok\.test$")
    monkeypatch.setattr(runner, "_container_running", lambda _n: False)
    monkeypatch.setattr(runner, "_cleanup_stale_container", lambda _n: None)
    monkeypatch.setattr(
        runner, "_run_proxy_container",
        lambda _p: subprocess.CompletedProcess([], 0, "", ""),
    )
    # liveness probe: the proxy is NOT running shortly after start
    monkeypatch.setattr(runner, "_proxy_is_live", lambda _n: False)
    with pytest.raises(RuntimeError, match="egress lockdown|exited"):
        runner._ensure_egress_proxy_running(project)


# --------------------------------------------------------------------------
# end-to-end (opt-in): requires a working rootless podman + network egress
# --------------------------------------------------------------------------

_PODMAN = shutil.which("podman")


@pytest.mark.skipif(_PODMAN is None, reason="podman not available")
@pytest.mark.skipif(
    os.environ.get("PEERS_EGRESS_E2E") != "1",
    reason="set PEERS_EGRESS_E2E=1 to run the container egress lockdown e2e",
)
def test_e2e_lockdown_blocks_bypass_allows_proxied(tmp_path):
    """Full chain: a container joined to the locked-down proxy netns CANNOT
    reach the internet directly (the bypass), but CAN reach an allowlisted
    host through the proxy, and a non-allowlisted host 403s."""
    proxy_dir = Path(__file__).resolve().parents[2] / "proxy"
    tag = "peers-egress-proxy:e2e-test"
    name = "peers-egress-e2e-test"
    subprocess.run([_PODMAN, "rm", "-f", name], capture_output=True)
    build = subprocess.run(
        [_PODMAN, "build", "-f", str(proxy_dir / "Containerfile.proxy"),
         "-t", tag, str(proxy_dir)],
        capture_output=True, text=True, timeout=300,
    )
    assert build.returncode == 0, build.stderr
    try:
        up = subprocess.run(
            [_PODMAN, "run", "-d", "--rm", "--name", name,
             "--userns=keep-id", "--cap-drop=ALL", "--cap-add=NET_ADMIN",
             "--cap-add=SETUID", "--cap-add=SETGID",
             "--security-opt=no-new-privileges", "--read-only",
             "--tmpfs", "/tmp:rw,mode=1777",
             "--tmpfs", "/var/log/tinyproxy:rw,mode=1777",
             "--tmpfs", "/run/tinyproxy:rw,mode=1777",
             "-e", r"PEERS_EGRESS_EXTRA_HOSTS=^raw\.githubusercontent\.com$",
             tag],
            capture_output=True, text=True, timeout=60,
        )
        assert up.returncode == 0, up.stderr
        subprocess.run(["sleep", "2"], check=False)
        script = (
            'env -u HTTP_PROXY -u HTTPS_PROXY curl -sS -o /dev/null '
            '-w "BYPASS=%{http_code}\\n" --max-time 8 '
            'https://raw.githubusercontent.com/torvalds/linux/master/README; '
            'curl -sS -o /dev/null -w "PROXIED=%{http_code}\\n" --max-time 15 '
            '-x http://127.0.0.1:3128 '
            'https://raw.githubusercontent.com/torvalds/linux/master/README; '
            'curl -sS -o /dev/null -w "DENIED=%{http_code}\\n" --max-time 15 '
            '-x http://127.0.0.1:3128 https://cwe.mitre.org/; true'
        )
        out = subprocess.run(
            [_PODMAN, "run", "--rm", f"--network=container:{name}",
             "--user", "12345:12345", "--entrypoint", "sh",
             "docker.io/curlimages/curl:8.10.1", "-c", script],
            capture_output=True, text=True, timeout=90,
        )
        combined = out.stdout + out.stderr
        assert "BYPASS=000" in combined, f"bypass not blocked: {combined}"
        assert "PROXIED=200" in combined, f"allowlisted not reachable: {combined}"
        assert "DENIED=000" in combined, f"non-allowlisted not denied: {combined}"
    finally:
        subprocess.run([_PODMAN, "rm", "-f", name], capture_output=True)
