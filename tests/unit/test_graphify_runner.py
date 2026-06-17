"""Control-plane lifecycle for the opt-in caged graphify MCP sidecar (runner.py).

Mirrors the egress/auth proxy lifecycle: build the graph + start a caged serve
sidecar before the peer launch, stop it at teardown. Fail-OPEN throughout —
every failure returns a falsy value + logs, never raises into the start path.
"""
import types
from pathlib import Path

import pytest

from peers_ctl import runner
from peers_ctl.store import Project


def _project(tmp_path, *, graphify=None):
    if graphify is not None:
        (tmp_path / ".peers").mkdir(exist_ok=True)
        cfg = "graphify_mcp: true\n" if graphify else "driver: orchestrator\n"
        (tmp_path / ".peers" / "config.yaml").write_text(cfg)
    return Project(name="proj-x", path=str(tmp_path))


def test_graphify_enabled_reads_config(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "GRAPHIFY_DISABLED", False, raising=False)
    assert runner._graphify_enabled(_project(tmp_path, graphify=True)) is True
    assert runner._graphify_enabled(_project(tmp_path, graphify=False)) is False


def test_graphify_enabled_missing_config_is_false(tmp_path):
    assert runner._graphify_enabled(Project(name="p", path=str(tmp_path))) is False


def test_graphify_enabled_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "GRAPHIFY_DISABLED", True, raising=False)
    assert runner._graphify_enabled(_project(tmp_path, graphify=True)) is False


def test_graphify_container_name_distinct_prefix(tmp_path):
    name = runner._graphify_container_name(Project(name="myproj", path=str(tmp_path)))
    assert name.startswith("peers-graphify_")
    # distinct from the main + proxy names so `stop` can find all
    assert name != runner._container_name(Project(name="myproj", path=str(tmp_path)))


def test_ensure_serve_host_disabled_returns_none(tmp_path):
    assert runner._ensure_graphify_serve_host(_project(tmp_path, graphify=False)) is None


def test_ensure_serve_host_build_fail_is_fail_open(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "GRAPHIFY_DISABLED", False, raising=False)
    monkeypatch.setattr(runner, "build_graph", lambda *a, **k: None)
    assert runner._ensure_graphify_serve_host(_project(tmp_path, graphify=True)) is None


def test_ensure_serve_host_happy_passes_key_via_env_not_argv(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "GRAPHIFY_DISABLED", False, raising=False)
    monkeypatch.setattr(runner, "build_graph",
                        lambda repo, out, **k: Path(out) / "graph.json")
    monkeypatch.setattr(runner, "new_api_key", lambda: "KKK")
    monkeypatch.setattr(runner, "_free_loopback_port", lambda: 45678)
    monkeypatch.setattr(runner, "_cleanup_stale_container", lambda *a, **k: None)
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["env"] = kw.get("env")
        return types.SimpleNamespace(returncode=0, stderr="", stdout="cid")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    got = runner._ensure_graphify_serve_host(_project(tmp_path, graphify=True))
    assert got == ("http://127.0.0.1:45678/mcp", "KKK")
    # secret only via the podman subprocess env, never in argv/ps
    assert seen["env"]["GRAPHIFY_API_KEY"] == "KKK"
    assert "KKK" not in " ".join(seen["cmd"])
    assert "--api-key" not in seen["cmd"]
    assert any("127.0.0.1:45678:8080" in c for c in seen["cmd"])  # host publish


def test_ensure_serve_host_serve_fail_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "GRAPHIFY_DISABLED", False, raising=False)
    monkeypatch.setattr(runner, "build_graph",
                        lambda repo, out, **k: Path(out) / "graph.json")
    monkeypatch.setattr(runner, "_cleanup_stale_container", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_stop_graphify_best_effort", lambda *a, **k: None)
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(
                            returncode=125, stderr="boom", stdout=""))
    assert runner._ensure_graphify_serve_host(_project(tmp_path, graphify=True)) is None


def test_ensure_serve_host_fail_open_on_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "GRAPHIFY_DISABLED", False, raising=False)

    def boom(*a, **k):
        raise OSError("podman gone")

    monkeypatch.setattr(runner, "build_graph", boom)
    assert runner._ensure_graphify_serve_host(_project(tmp_path, graphify=True)) is None


def test_stop_graphify_best_effort_stops_when_running(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_container_running", lambda name: True)
    seen = {}
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda cmd, **k: seen.update(cmd=cmd) or
                        types.SimpleNamespace(returncode=0))
    runner._stop_graphify_best_effort(Project(name="p", path=str(tmp_path)))
    assert "stop" in seen["cmd"]


def test_stop_graphify_best_effort_noop_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_container_running", lambda name: False)
    called = {"run": False}
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda *a, **k: called.update(run=True))
    runner._stop_graphify_best_effort(Project(name="p", path=str(tmp_path)))
    assert called["run"] is False


def _enable_graphify_config(tmp_path):
    (tmp_path / ".peers").mkdir(exist_ok=True)
    (tmp_path / ".peers" / "config.yaml").write_text("graphify_mcp: true\n")


# --- container mode: join the egress-proxy netns chain (distinct port) ---


def test_peer_netns_head_is_egress_proxy_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "EGRESS_PROXY_DISABLED", False)
    p = Project(name="p", path=str(tmp_path))
    assert runner._peer_netns_head(p) == runner._proxy_container_name(p)


def test_peer_netns_head_is_auth_proxy_when_egress_off(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "EGRESS_PROXY_DISABLED", True)
    monkeypatch.setattr(runner, "_auth_proxy_enabled", lambda *a, **k: True)
    p = Project(name="p", path=str(tmp_path))
    assert runner._peer_netns_head(p) == runner._auth_proxy_container_name(p)


def test_peer_netns_head_none_when_peer_owns_netns(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "EGRESS_PROXY_DISABLED", True)
    monkeypatch.setattr(runner, "_auth_proxy_enabled", lambda *a, **k: False)
    assert runner._peer_netns_head(Project(name="p", path=str(tmp_path))) is None


def test_ensure_serve_container_joins_netns_with_distinct_port(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "GRAPHIFY_DISABLED", False, raising=False)
    monkeypatch.setattr(runner, "EGRESS_PROXY_DISABLED", False)
    monkeypatch.setattr(runner, "_container_running", lambda name: True)
    monkeypatch.setattr(runner, "_cleanup_stale_container", lambda *a, **k: None)
    monkeypatch.setattr(runner, "build_graph",
                        lambda repo, out, **k: Path(out) / "graph.json")
    monkeypatch.setattr(runner, "new_api_key", lambda: "CKEY")
    seen = {}
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda cmd, **kw: seen.update(cmd=cmd, env=kw.get("env"))
                        or types.SimpleNamespace(returncode=0, stderr="", stdout="x"))
    _enable_graphify_config(tmp_path)
    p = Project(name="p", path=str(tmp_path))
    endpoint, key = runner._ensure_graphify_serve_container(p)
    proxy = runner._proxy_container_name(p)
    assert key == "CKEY"
    # CRITICAL: distinct in-netns port, NOT the auth-proxy's 8080
    assert endpoint == "http://127.0.0.1:8645/mcp"
    assert "8080" not in endpoint
    assert f"--network=container:{proxy}" in seen["cmd"]
    assert f"--userns=container:{proxy}" in seen["cmd"]
    assert "-p" not in seen["cmd"]  # shares the chain loopback, never publishes
    assert "127.0.0.1" in seen["cmd"]  # binds the shared loopback
    assert seen["env"]["GRAPHIFY_API_KEY"] == "CKEY"  # key via env, not argv
    assert "CKEY" not in " ".join(seen["cmd"])


def test_ensure_serve_container_fail_open_when_no_netns_head(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "GRAPHIFY_DISABLED", False, raising=False)
    monkeypatch.setattr(runner, "EGRESS_PROXY_DISABLED", True)
    monkeypatch.setattr(runner, "_auth_proxy_enabled", lambda *a, **k: False)
    _enable_graphify_config(tmp_path)
    got = runner._ensure_graphify_serve_container(Project(name="p", path=str(tmp_path)))
    assert got is None


def test_container_runtime_flags_thread_graphify_env_only_when_set(tmp_path):
    p = Project(name="p", path=str(tmp_path))
    off = runner._peer_container_runtime_flags(p)
    on = runner._peer_container_runtime_flags(
        p, graphify=("http://127.0.0.1:8645/mcp", "K"))
    # off => no graphify env (byte-identical to today)
    assert "GRAPHIFY_MCP_ENDPOINT=http://127.0.0.1:8645/mcp" not in off
    # on => endpoint inlined (not secret) + key INHERITED (-e GRAPHIFY_API_KEY)
    assert "GRAPHIFY_MCP_ENDPOINT=http://127.0.0.1:8645/mcp" in on
    i = on.index("GRAPHIFY_API_KEY")
    assert on[i - 1] == "-e"
    assert "GRAPHIFY_API_KEY=K" not in " ".join(on)  # key never inlined


# --- fail-open hardening (review I2/I3): probe/spawn errors must not abort ---


def test_ensure_serve_host_fail_open_on_probe_error(tmp_path, monkeypatch):
    """A probe error that ISN'T OSError/YAMLError (e.g. from _graphify_enabled)
    must still fail-open, not abort the run."""
    monkeypatch.setattr(runner, "GRAPHIFY_DISABLED", False, raising=False)

    def boom(_project):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(runner, "_graphify_enabled", boom)
    assert runner._ensure_graphify_serve_host(
        Project(name="p", path=str(tmp_path))) is None


def test_ensure_serve_container_fail_open_on_probe_error(tmp_path, monkeypatch):
    """_container_running raising a PermissionError (podman socket perms) must
    fail-open in container mode, not propagate into the start path."""
    monkeypatch.setattr(runner, "GRAPHIFY_DISABLED", False, raising=False)
    monkeypatch.setattr(runner, "EGRESS_PROXY_DISABLED", False)
    _enable_graphify_config(tmp_path)

    def boom(_name):
        raise PermissionError("podman socket")

    monkeypatch.setattr(runner, "_container_running", boom)
    assert runner._ensure_graphify_serve_container(
        Project(name="p", path=str(tmp_path))) is None


def test_start_host_stops_graphify_on_spawn_failure(tmp_path, monkeypatch):
    """If the graphify sidecar started but the driver Popen fails, the sidecar
    must be torn down (not orphaned) and the error re-raised."""
    monkeypatch.setattr(runner, "_ensure_graphify_serve_host",
                        lambda _p: ("http://127.0.0.1:1/mcp", "K"))
    stops = []
    monkeypatch.setattr(runner, "_stop_graphify_best_effort",
                        lambda _p: stops.append(1))
    monkeypatch.setattr(runner, "open_text_in_dir_no_symlink",
                        lambda *a, **k: (tmp_path / "log.txt").open("a"))

    def popen_boom(*a, **k):
        raise OSError("cannot spawn driver")

    monkeypatch.setattr(runner.subprocess, "Popen", popen_boom)
    with pytest.raises(OSError):
        runner._start_project_host(
            types.SimpleNamespace(), Project(name="p", path=str(tmp_path)),
            tmp_path / "log.txt", None, None, ())
    assert stops == [1], "graphify sidecar must be stopped on spawn failure"


def test_shipped_template_defaults_graphify_off():
    """The shipped config.yaml template documents graphify_mcp and defaults it
    OFF, so a freshly-initialised project runs byte-identically (no graph)."""
    import yaml

    import peers
    tpl = Path(peers.__file__).parent / "templates" / "config.yaml"
    cfg = yaml.safe_load(tpl.read_text())
    assert "graphify_mcp" in cfg, "template must document the graphify_mcp flag"
    assert cfg["graphify_mcp"] is False, "graphify must default OFF"
