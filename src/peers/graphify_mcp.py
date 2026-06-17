"""Per-peer MCP launch-flag injection for the opt-in graphify graph server.

Wires the caged graphify MCP sidecar into a peer's launch. Only the active
peers (claude, codex) are supported; unknown tools get nothing (graceful — the
run continues without graph tools). The secret never appears in argv: both
claude and codex read GRAPHIFY_API_KEY from the peer subprocess environment.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping

# The env var the peer container exposes the bearer token under (codex reads it).
GRAPHIFY_API_KEY_ENV = "GRAPHIFY_API_KEY"
# The endpoint URL the control plane publishes once the caged serve sidecar is
# up. Its presence (with the key) is the driver-side fail-open enable signal.
GRAPHIFY_ENDPOINT_ENV = "GRAPHIFY_MCP_ENDPOINT"


def graphify_runtime(
    env: Mapping[str, str] | None = None,
) -> tuple[str, str] | None:
    """Return ``(endpoint, api_key)`` from the env signal, else ``None``.

    Both ``GRAPHIFY_MCP_ENDPOINT`` and ``GRAPHIFY_API_KEY`` must be present and
    non-empty. The control plane sets them only after the caged sidecar
    actually started, so their presence IS the enable signal — absent (or the
    sidecar failed to come up) means the driver runs byte-identically to a
    no-graphify run.
    """
    env = os.environ if env is None else env
    endpoint = (env.get(GRAPHIFY_ENDPOINT_ENV) or "").strip()
    api_key = (env.get(GRAPHIFY_API_KEY_ENV) or "").strip()
    if endpoint and api_key:
        return endpoint, api_key
    return None


def graphify_mcp_flags(tool: str, endpoint: str) -> list[str]:
    """Extra launch flags that attach the graphify MCP server to ``tool``.

    The bearer token is referenced from the ``GRAPHIFY_API_KEY`` env -- claude
    expands ``${GRAPHIFY_API_KEY}`` in the --mcp-config header, codex reads it
    via ``bearer_token_env_var`` -- so the secret NEVER enters the argv
    (ps-invisible). The caller must put GRAPHIFY_API_KEY in the peer subprocess
    env (see :func:`apply_graphify_mcp`). Returns an empty list for tools
    without a known wiring, so a caller can unconditionally splice the result.
    """
    if tool == "claude":
        cfg = {"mcpServers": {"graphify": {
            "type": "http",
            "url": endpoint,
            "headers": {"Authorization": f"Bearer ${{{GRAPHIFY_API_KEY_ENV}}}"},
        }}}
        return ["--mcp-config", json.dumps(cfg)]
    if tool == "codex":
        return [
            "-c", f"mcp_servers.graphify.url={json.dumps(endpoint)}",
            "-c", f'mcp_servers.graphify.bearer_token_env_var="{GRAPHIFY_API_KEY_ENV}"',
        ]
    return []
