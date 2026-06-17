"""End-to-end graphify integration (real containers). Proves the caged chain:
build -> serve over MCP -> the 7 code-nav tools, and the api-key auth gate
(401 without the key, accepted with Authorization: Bearer or X-API-Key).

Skips if podman or the graphify-sandbox image is unavailable.
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from peers.graphify_sidecar import build_graph, new_api_key, serve_cmd

IMAGE = "graphify-sandbox:pinned"


def _image_present_named(image: str) -> bool:
    if not shutil.which("podman"):
        return False
    return subprocess.run(["podman", "image", "exists", image]).returncode == 0


def _image_present() -> bool:
    return _image_present_named(IMAGE)


pytestmark = pytest.mark.skipif(
    not _image_present(),
    reason="podman or graphify-sandbox:pinned image not available")


@pytest.fixture(scope="module")
def graph(tmp_path_factory) -> Path:
    # build a real graph from a small real package (this repo's peers_ctl)
    repo = Path(__file__).resolve().parent.parent.parent / "src" / "peers_ctl"
    out = tmp_path_factory.mktemp("gout")
    g = build_graph(repo, out)
    assert g is not None and g.exists(), "caged graph build failed"
    return g


def test_caged_build_produces_nonempty_graph(graph):
    # the build is offline + caged; read via the same subuid the cage wrote with
    raw = subprocess.run(["podman", "unshare", "cat", str(graph)],
                         capture_output=True, text=True)
    data = json.loads(raw.stdout)
    assert len(data.get("nodes", [])) > 0


def _serve(graph: Path, *, transport: str, port: int = 0, api_key: str = ""):
    """Start a caged graphify.serve; returns the Popen. For http, binds a port."""
    import os
    out_dir = graph.parent
    # keep-id + host uid so serve can read the host-owned graph.json
    cmd = ["podman", "run", "--rm", "-i",
           "--read-only", "--cap-drop=ALL",
           "--security-opt=no-new-privileges:true",
           "--userns=keep-id", "--user", f"{os.getuid()}:{os.getgid()}",
           "--tmpfs", "/tmp:rw", "-v", f"{out_dir}:/work/out:ro"]
    if transport == "http":
        cmd += ["-p", f"127.0.0.1:{port}:8080", "--entrypoint", "python3", IMAGE,
                "-m", "graphify.serve", "/work/out/graph.json",
                "--transport", "http", "--host", "0.0.0.0", "--port", "8080"]
        if api_key:
            cmd += ["--api-key", api_key]
    else:
        cmd += ["--network=none", "--entrypoint", "python3", IMAGE,
                "-m", "graphify.serve", "/work/out/graph.json",
                "--transport", "stdio"]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1)


def test_serve_stdio_exposes_code_nav_tools(graph):
    p = _serve(graph, transport="stdio")
    out = []
    t = threading.Thread(
        target=lambda: out.extend(ln.strip() for ln in p.stdout if ln.strip()),
        daemon=True)
    t.start()

    def send(o):
        p.stdin.write(json.dumps(o) + "\n")
        p.stdin.flush()
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "t", "version": "0"}}})
        time.sleep(2)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        time.sleep(1)
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        time.sleep(3)
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
    tools = set()
    for line in out:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("id") == 2:
            tools = {t["name"] for t in m.get("result", {}).get("tools", [])}
    for expect in ("query_graph", "get_neighbors", "shortest_path", "get_node",
                   "god_nodes", "graph_stats"):
        assert expect in tools, f"missing tool {expect}; got {tools}"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _post(url, headers):
    """POST a (deliberately minimal) MCP body. Returns a CLEAN 401 only when the
    api-key middleware rejects BEFORE the MCP layer. When auth passes, the MCP
    streamable-HTTP handler resets/closes on the minimal body — we map that to a
    non-401 sentinel (599): 'auth passed, reached the MCP layer'."""
    req = urllib.request.Request(
        url, data=b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream", **headers},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 599  # connected, auth passed, MCP transport closed on minimal body


def _wait_up(port: int) -> bool:
    """Wait until the HTTP app actually RESPONDS, not just until the TCP port
    accepts. graphify-serve accepts connections before the ASGI app (and its
    api-key middleware) is ready; a POST in that window is reset, which the
    test would misread as 'auth passed'. An unauthenticated POST returning a
    real HTTP status (401 once the middleware is live) means the gate is up."""
    url = f"http://127.0.0.1:{port}/mcp"
    body = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
    for _ in range(60):
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json",
                         "Accept": "application/json, text/event-stream"},
                method="POST")
            urllib.request.urlopen(req, timeout=1).close()
            return True
        except urllib.error.HTTPError:
            return True  # a real HTTP status (e.g. 401) => app + gate are live
        except Exception:
            time.sleep(0.5)
    return False


def test_http_api_key_gate(graph):
    KEY = "s3cret-test-key"
    port = _free_port()
    p = _serve(graph, transport="http", port=port, api_key=KEY)
    base = f"http://127.0.0.1:{port}/mcp"
    try:
        assert _wait_up(port), "serve http did not come up"
        time.sleep(1.0)
        # REJECT: no key / wrong key -> clean 401 (middleware short-circuits)
        assert _post(base, {}) == 401
        assert _post(base, {"Authorization": "Bearer wrong"}) == 401
        # ACCEPT: right key via Bearer OR X-API-Key -> past auth (not 401)
        assert _post(base, {"Authorization": f"Bearer {KEY}"}) != 401
        assert _post(base, {"X-API-Key": KEY}) != 401
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


def test_serve_cmd_host_mode_gate_end_to_end(graph):
    """The REAL serve_cmd helper (host mode, key via the inherited
    GRAPHIFY_API_KEY env) brings up a caged graphify MCP whose api-key gate
    rejects an unauthenticated POST (401) and admits the Bearer / X-API-Key
    (not 401). Proves the production argv AND the secret-not-in-argv env path
    end-to-end against a real container."""
    import os
    key = new_api_key()
    port = _free_port()
    name = f"graphify-mcp-test-{port}"
    cmd = serve_cmd(graph, name=name, publish=f"127.0.0.1:{port}:8080",
                    bind_host="0.0.0.0")
    # the secret reaches the container only via inherited env, never argv
    assert "--api-key" not in cmd
    assert not any(key in part for part in cmd)
    env = dict(os.environ, GRAPHIFY_API_KEY=key)
    run = subprocess.run(cmd, env=env, capture_output=True, text=True)
    assert run.returncode == 0, f"serve_cmd run failed: {run.stderr}"
    base = f"http://127.0.0.1:{port}/mcp"
    try:
        assert _wait_up(port), "serve_cmd http did not come up"
        assert _post(base, {}) == 401                                  # rejected
        assert _post(base, {"Authorization": f"Bearer {key}"}) != 401  # admitted
        assert _post(base, {"X-API-Key": key}) != 401                  # admitted
    finally:
        subprocess.run(["podman", "stop", "-t", "2", name],
                       capture_output=True, timeout=20)


def test_ensure_graphify_serve_host_lifecycle_e2e(tmp_path, monkeypatch):
    """The REAL control-plane host lifecycle (runner._ensure_graphify_serve_host):
    build the caged graph -> start the published serve sidecar (key via env) ->
    return (endpoint, key) whose gate rejects no-key (401) and admits the key;
    then _stop_graphify_best_effort reaps it. Proves the production wiring,
    not a mock."""
    from peers_ctl import runner
    from peers_ctl.store import Project
    monkeypatch.setattr(runner, "GRAPHIFY_DISABLED", False, raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text(
        "def a(x):\n    return b(x)\n\n\ndef b(x):\n    return x + 1\n")
    (tmp_path / ".peers").mkdir()
    (tmp_path / ".peers" / "config.yaml").write_text("graphify_mcp: true\n")
    project = Project(name="gx-smoke", path=str(tmp_path))

    got = runner._ensure_graphify_serve_host(project)
    assert got is not None, "host lifecycle returned None (build/serve failed)"
    endpoint, key = got
    try:
        assert endpoint.startswith("http://127.0.0.1:")
        port = int(endpoint.split(":")[2].split("/")[0])
        assert _wait_up(port), "serve sidecar did not come up"
        assert _post(endpoint, {}) == 401                                 # gated
        assert _post(endpoint, {"Authorization": f"Bearer {key}"}) != 401  # in
    finally:
        runner._stop_graphify_best_effort(project)
    name = runner._graphify_container_name(project)
    alive = subprocess.run(
        ["podman", "ps", "--filter", f"name={name}", "--format", "{{.Names}}"],
        capture_output=True, text=True).stdout
    assert name not in alive, f"graphify sidecar {name} was not reaped"


def test_container_mode_netns_join_e2e(tmp_path, monkeypatch):
    """The REAL container-mode lifecycle: graphify-serve JOINS the egress-proxy
    netns and is reachable + api-key-gated FROM WITHIN that shared netns at the
    distinct port 8645 (NOT the auth-proxy's 8080). Proves
    _ensure_graphify_serve_container + the netns-join end to end. Needs the
    egress-proxy image too."""
    import os
    from peers_ctl import runner
    from peers_ctl.store import Project
    if not _image_present_named("peers-egress-proxy:dev"):
        import pytest
        pytest.skip("peers-egress-proxy:dev image not available")
    monkeypatch.setattr(runner, "GRAPHIFY_DISABLED", False, raising=False)
    monkeypatch.setattr(runner, "EGRESS_PROXY_DISABLED", False, raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("def f(x):\n    return x\n")
    (tmp_path / ".peers").mkdir()
    # A valid peers list is required: _ensure_egress_proxy_running reads it to
    # compute the egress allow-list (openrouter hosts).
    (tmp_path / ".peers" / "config.yaml").write_text(
        "graphify_mcp: true\n"
        "peers:\n"
        "  - name: claude\n"
        "    tool: claude\n"
        '    argv: ["claude", "-p", "{PROMPT}"]\n')
    project = Project(name="gx-netns-smoke", path=str(tmp_path))
    proxy = runner._proxy_container_name(project)
    probe = (
        "import urllib.request,urllib.error,os,time\n"
        "def post(h):\n"
        " req=urllib.request.Request('http://127.0.0.1:8645/mcp',"
        "data=b'{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\","
        "\"params\":{}}',headers={'Content-Type':'application/json',"
        "'Accept':'application/json, text/event-stream',**h},method='POST')\n"
        " try:\n  return urllib.request.urlopen(req,timeout=3).status\n"
        " except urllib.error.HTTPError as e:\n  return e.code\n"
        " except Exception:\n  return 0\n"
        "for _ in range(40):\n"
        " if post({})==401: break\n"
        " time.sleep(0.5)\n"
        "print('NOKEY',post({}))\n"
        "print('KEY',post({'Authorization':'Bearer '+os.environ['GXK']}))\n"
    )
    try:
        runner._ensure_egress_proxy_running(project)  # head of the netns chain
        got = runner._ensure_graphify_serve_container(project)
        assert got is not None, "container lifecycle returned None"
        endpoint, key = got
        assert endpoint == "http://127.0.0.1:8645/mcp"  # distinct from 8080
        r = subprocess.run(
            ["podman", "run", "--rm", f"--network=container:{proxy}",
             f"--userns=container:{proxy}", "-e", "GXK",
             "--entrypoint", "python3", IMAGE, "-c", probe],
            env={**os.environ, "GXK": key},
            capture_output=True, text=True, timeout=150)
        out = r.stdout
        assert "NOKEY 401" in out, f"no-key not gated in netns: {out!r} {r.stderr[-300:]!r}"
        assert "KEY 200" in out, f"key not admitted in netns: {out!r}"
    finally:
        runner._stop_graphify_best_effort(project)
        runner._stop_egress_proxy_best_effort(project)
