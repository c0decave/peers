"""Caged graphify graph-build + sidecar lifecycle — fail-OPEN throughout.

graphify is a young, single-maintainer tool whose installer injects agent hooks,
so it is never trusted: it runs only inside a supply-chain cage (no network,
read-only, all caps dropped, non-root, ephemeral, resource-capped). We only ever
use the offline `update` (build the graph) and `serve` (read it over MCP) — never
`install` (which would inject PreToolUse hooks).

peers must run byte-identically without graphify, so every function here returns
a falsy value + logs a warning on any failure and NEVER raises into the loop.
"""
from __future__ import annotations

import logging
import os
import secrets
import subprocess
from pathlib import Path

from peers.safe_io import _ensure_private_dir, ensure_private_dir_under_root

logger = logging.getLogger("peers.graphify")

GRAPHIFY_IMAGE = "graphify-sandbox:pinned"


def _cage_flags() -> list[str]:
    """Shared supply-chain cage. ``--userns=keep-id`` + the host uid/gid map the
    container user to the operator, so the graph output is host-readable (and the
    serve sidecar, run the same way, can read it) WITHOUT the ``:U`` chown that
    makes output host-unreadable — while staying non-root, capless, no-new-privs,
    read-only-rootfs, resource-capped."""
    return [
        "--rm",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--security-opt=label=disable",
        "--userns=keep-id",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--memory=4g", "--memory-swap=4g", "--pids-limit=1024", "--cpus=4",
        "--tmpfs", "/tmp:rw,size=1g,nosuid,nodev,noexec",
        "--tmpfs", "~:rw,size=128m,nosuid,nodev,mode=1777",
        "-e", "HOME=~",
        # else graphify writes graphify-out/ INTO the read-only code mount
        "-e", "GRAPHIFY_OUT=/work/out",
    ]


def build_graph_cmd(repo: Path, out_dir: Path,
                    image: str = GRAPHIFY_IMAGE) -> list[str]:
    """The caged, offline ``graphify update`` command: code mounted read-only,
    graph written host-readable to ``out_dir``. No network (pure AST extraction)."""
    return [
        "podman", "run", *_cage_flags(),
        "--network=none",
        "-v", f"{repo}:/work/code:ro",
        "-v", f"{out_dir}:/work/out:rw",
        image,
        "update", "/work/code",
    ]


def build_graph(repo: Path, out_dir: Path, *, image: str = GRAPHIFY_IMAGE,
                runner=subprocess.run, timeout_s: int = 300) -> Path | None:
    """Build/refresh the knowledge graph in the cage. Returns the graph.json
    path on success, ``None`` on any failure (fail-open — never raises)."""
    out_dir = Path(out_dir)
    repo = Path(repo)
    try:
        # BUG-509: create out_dir TOCTOU-safely AND refuse a symlinked
        # ANCESTOR under the repo. _ensure_private_dir only guards the leaf (its
        # mkdir(parents=True) would follow a swapped .peers); the root-relative
        # walk O_NOFOLLOWs every component BELOW the trusted repo root (which may
        # itself legitimately sit under a symlink like /tmp). Fail-open: a refusal
        # raises OSError, caught by the except below.
        try:
            rel_parts = out_dir.relative_to(repo).parts
        except ValueError:
            rel_parts = ()
        if rel_parts:
            ensure_private_dir_under_root(repo, rel_parts)
        else:
            _ensure_private_dir(out_dir)
        result = runner(
            build_graph_cmd(Path(repo), out_dir, image),
            capture_output=True, text=True, timeout=timeout_s,
        )
        if getattr(result, "returncode", 1) != 0:
            logger.warning(
                "graphify build_graph failed (rc=%s); continuing without graph: %s",
                getattr(result, "returncode", "?"),
                (getattr(result, "stderr", "") or "")[:200])
            return None
        graph = out_dir / "graph.json"
        if not graph.exists():
            logger.warning(
                "graphify build_graph produced no graph.json at %s; "
                "continuing without graph", graph)
            return None
        return graph
    except Exception as e:  # fail-open: a missing podman/image must not break a run
        logger.warning(
            "graphify build_graph error (continuing without graph): %s", e)
        return None


def new_api_key() -> str:
    """A fresh url-safe 256-bit key for one serve sidecar (per-run secret)."""
    return secrets.token_urlsafe(32)


def serve_cmd(
    graph_json: Path,
    *,
    name: str,
    image: str = GRAPHIFY_IMAGE,
    port: int = 8080,
    publish: str | None = None,
    network: str | None = None,
    userns: str = "keep-id",
    bind_host: str = "0.0.0.0",
) -> list[str]:
    """Caged ``podman run -d`` for ``graphify.serve`` over MCP/HTTP, api-key gated.

    The graph's directory is mounted READ-ONLY and serve binds
    ``{bind_host}:{port}`` inside the same supply-chain cage as the build
    (read-only rootfs, all caps dropped, no-new-privs, non-root, resource
    capped). The api key is taken from the ``GRAPHIFY_API_KEY`` env, which is
    INHERITED into the container (``-e GRAPHIFY_API_KEY`` with no value) from the
    podman process -- so the secret never appears in any argv (ps-invisible).
    The caller MUST set ``GRAPHIFY_API_KEY`` in the podman subprocess environment.

    Host mode:      ``publish="127.0.0.1:H:port"``, ``network=None``,
                    ``bind_host="0.0.0.0"`` so the host-published port reaches it.
    Container mode: ``network="container:<egress-proxy>"`` +
                    ``userns="container:<egress-proxy>"`` (join the proxy netns
                    chain like the auth-proxy), ``publish=None``,
                    ``bind_host="127.0.0.1"`` (peers share the chain's loopback).
    """
    graph_json = Path(graph_json)
    cmd = [
        "podman", "run", "-d", "--rm", "--name", name,
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--security-opt=label=disable",
        f"--userns={userns}",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--memory=2g", "--memory-swap=2g", "--pids-limit=512", "--cpus=2",
        "--tmpfs", "/tmp:rw,size=256m,nosuid,nodev,noexec",
        "--tmpfs", "~:rw,size=64m,nosuid,nodev,mode=1777",
        "-e", "HOME=~",
        # api key inherited from the podman env -> never in argv (ps-invisible)
        "-e", "GRAPHIFY_API_KEY",
        "-v", f"{graph_json.parent}:/work/out:ro",
    ]
    if network:
        cmd.append(f"--network={network}")
    if publish:
        cmd += ["-p", publish]
    cmd += [
        "--entrypoint", "python3", image,
        "-m", "graphify.serve", f"/work/out/{graph_json.name}",
        "--transport", "http",
        "--host", bind_host,
        "--port", str(port),
    ]
    return cmd
