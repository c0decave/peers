"""Caged graphify graph-build + sidecar lifecycle — fail-OPEN throughout.

peers must run byte-identically without graphify; therefore every step here
returns a falsy value + logs on any failure and NEVER raises into the loop.
"""
import os
from pathlib import Path

from peers.graphify_sidecar import (
    build_graph,
    build_graph_cmd,
    new_api_key,
    serve_cmd,
)


def test_build_graph_cmd_is_caged():
    cmd = build_graph_cmd(Path("/repo"), Path("/out"), image="graphify-sandbox:pinned")
    j = " ".join(cmd)
    # the supply-chain cage: no network, read-only, all caps dropped, non-root
    assert "--network=none" in cmd
    assert "--read-only" in cmd
    assert "--cap-drop=ALL" in cmd
    assert "--security-opt=no-new-privileges:true" in cmd
    # host-uid mapping -> non-root AND host-readable output (no :U chown)
    assert "--userns=keep-id" in cmd
    assert f"{os.getuid()}:{os.getgid()}" in cmd
    # else graphify writes graphify-out/ into the read-only code mount
    assert "GRAPHIFY_OUT=/work/out" in cmd
    # code mounted read-only, output writable (plain rw, not :U)
    assert "/repo:/work/code:ro" in j
    assert "/out:/work/out:rw" in j
    assert "graphify-sandbox:pinned" in cmd
    # offline AST update (never `install` — that injects agent hooks)
    assert "update" in cmd
    assert "install" not in cmd


def test_build_graph_returns_none_on_nonzero(tmp_path):
    class R:  # runner that reports failure
        returncode = 1
        stderr = "boom"
    out = build_graph(tmp_path / "repo", tmp_path / "out",
                      runner=lambda *a, **k: R())
    assert out is None


def test_build_graph_fail_open_on_exception(tmp_path):
    # the cardinal invariant: a raising runner must NOT propagate — return None
    def boom(*a, **k):
        raise OSError("podman not found")
    out = build_graph(tmp_path / "repo", tmp_path / "out", runner=boom)
    assert out is None


def test_build_graph_returns_graph_path_on_success(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "graph.json").write_text("{}")

    class R:
        returncode = 0
        stderr = ""
    got = build_graph(tmp_path / "repo", out_dir, runner=lambda *a, **k: R())
    assert got == out_dir / "graph.json"


# --- serve sidecar argv (the caged graphify.serve over MCP/HTTP) ---


def test_serve_cmd_is_caged_and_key_via_inherited_env():
    cmd = serve_cmd(Path("/out/graph.json"), name="graphify-mcp_proj")
    j = " ".join(cmd)
    assert cmd[:4] == ["podman", "run", "-d", "--rm"]
    assert "--name" in cmd and "graphify-mcp_proj" in cmd
    # same supply-chain cage as the build
    for flag in ("--read-only", "--cap-drop=ALL",
                 "--security-opt=no-new-privileges:true"):
        assert flag in cmd
    assert f"{os.getuid()}:{os.getgid()}" in cmd
    # KEY via INHERITED env (-e GRAPHIFY_API_KEY, no value) -> never in argv/ps
    i = cmd.index("GRAPHIFY_API_KEY")
    assert cmd[i - 1] == "-e"
    assert "--api-key" not in cmd
    assert not any("GRAPHIFY_API_KEY=" in a for a in cmd)
    # serve the graph over http
    assert "graphify.serve" in j
    assert "/work/out/graph.json" in cmd
    assert "--transport" in cmd and "http" in cmd
    # graph dir mounted READ-ONLY
    assert "/out:/work/out:ro" in j
    # never `install` (hook injection); serve only reads
    assert "install" not in cmd


def test_serve_cmd_host_mode_publishes_loopback():
    cmd = serve_cmd(Path("/o/graph.json"), name="g",
                    publish="127.0.0.1:55555:8080", bind_host="0.0.0.0")
    assert "-p" in cmd
    assert "127.0.0.1:55555:8080" in cmd
    assert "0.0.0.0" in cmd  # binds all so the published port reaches it
    # host mode does not join another netns
    assert not any(a.startswith("--network=container:") for a in cmd)


def test_serve_cmd_container_mode_joins_netns_chain_without_publish():
    cmd = serve_cmd(Path("/o/graph.json"), name="g",
                    network="container:peers-egress-proxy_x",
                    userns="container:peers-egress-proxy_x",
                    bind_host="127.0.0.1")
    assert "--network=container:peers-egress-proxy_x" in cmd
    assert "--userns=container:peers-egress-proxy_x" in cmd
    assert "-p" not in cmd  # shares the chain's loopback, never publishes
    assert "127.0.0.1" in cmd


def test_new_api_key_is_unique_and_urlsafe():
    import string
    k1, k2 = new_api_key(), new_api_key()
    assert k1 != k2 and len(k1) >= 32
    assert set(k1) <= set(string.ascii_letters + string.digits + "-_")


def test_build_graph_refuses_symlinked_out_dir(tmp_path):
    """BUG-509: a symlinked out_dir is refused TOCTOU-safely -> fail-open None,
    and the caged build never runs against the symlink target."""
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    calls = {"n": 0}

    def runner(*a, **k):
        calls["n"] += 1

        class R:
            returncode = 0
            stderr = ""

        return R()

    assert build_graph(tmp_path / "repo", link, runner=runner) is None
    assert calls["n"] == 0  # never reached the build


def test_build_graph_refuses_symlinked_peers_ancestor_BUG_514(tmp_path):
    """BUG-514 follow-up: a symlinked ANCESTOR under the repo (e.g. a swapped
    .peers) must not be followed when creating out_dir. _ensure_private_dir's
    mkdir(parents=True) followed it; the root-relative no-follow create refuses
    it -> fail-open None, the build never runs, nothing lands in the attacker dir."""
    import pytest

    repo = tmp_path / "repo"
    repo.mkdir()
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    try:
        (repo / ".peers").symlink_to(attacker, target_is_directory=True)
    except OSError as e:
        pytest.skip(f"symlinks unavailable: {e}")
    calls = {"n": 0}

    def runner(*a, **k):
        calls["n"] += 1

        class R:
            returncode = 0
            stderr = ""

        return R()

    out_dir = repo / ".peers" / "graphify"
    assert build_graph(repo, out_dir, runner=runner) is None
    assert calls["n"] == 0  # refused before the build
    assert not (attacker / "graphify").exists()  # ancestor symlink not followed
