"""Per-peer MCP launch-flag injection for the opt-in graphify graph server."""
import json

from peers.graphify_mcp import (
    GRAPHIFY_API_KEY_ENV,
    GRAPHIFY_ENDPOINT_ENV,
    graphify_mcp_flags,
    graphify_runtime,
)
from peers.model_provider import apply_graphify_mcp

EP = "http://127.0.0.1:8765/mcp"
KEY = "SEKRET"
_ON = {GRAPHIFY_ENDPOINT_ENV: EP, GRAPHIFY_API_KEY_ENV: KEY}


def _claude_mcp_config_values(argv):
    """Return values Claude's variadic --mcp-config option would consume."""
    i = argv.index("--mcp-config")
    values = []
    for arg in argv[i + 1:]:
        if arg.startswith("-"):
            break
        values.append(arg)
    return values


def test_claude_mcp_config_references_key_via_env():
    flags = graphify_mcp_flags("claude", "http://graphify-mcp:8080/mcp")
    assert flags[0] == "--mcp-config"
    cfg = json.loads(flags[1])
    srv = cfg["mcpServers"]["graphify"]
    assert srv["type"] == "http"
    assert srv["url"] == "http://graphify-mcp:8080/mcp"
    # key referenced via claude's ${VAR} env expansion, NEVER inlined (ps-safe)
    assert srv["headers"]["Authorization"] == "Bearer ${GRAPHIFY_API_KEY}"
    assert "SEKRET" not in flags[1]


def test_codex_gets_config_overrides_with_env_var_indirection():
    flags = graphify_mcp_flags("codex", "http://graphify-mcp:8080/mcp")
    joined = " ".join(flags)
    assert '-c' in flags
    # codex reads the key from env GRAPHIFY_API_KEY, never inlines the secret
    assert 'mcp_servers.graphify.url="http://graphify-mcp:8080/mcp"' in joined
    assert 'mcp_servers.graphify.bearer_token_env_var="GRAPHIFY_API_KEY"' in joined


def test_unknown_tool_returns_empty():
    assert graphify_mcp_flags("opencode-future", "http://x") == []
    assert graphify_mcp_flags("", "http://x") == []


# --- env signal + per-peer splicing (the driver-side enable seam) ---


def test_graphify_runtime_requires_both_env_vars():
    assert graphify_runtime(_ON) == (EP, KEY)
    assert graphify_runtime({GRAPHIFY_ENDPOINT_ENV: EP}) is None
    assert graphify_runtime({GRAPHIFY_API_KEY_ENV: KEY}) is None
    assert graphify_runtime({}) is None
    # empty values are treated as unset (fail-open)
    assert graphify_runtime(
        {GRAPHIFY_ENDPOINT_ENV: EP, GRAPHIFY_API_KEY_ENV: ""}
    ) is None


def test_graphify_runtime_accepts_whitespace_padded_env_values():
    # HAPPY: the enable signal survives surrounding whitespace — the control
    # plane may export values with stray padding; both are .strip()ed and the
    # stripped (endpoint, key) tuple is returned (never the padded raw value).
    out = graphify_runtime(
        {GRAPHIFY_ENDPOINT_ENV: f"  {EP}  ", GRAPHIFY_API_KEY_ENV: "  k  "})
    assert out == (EP, "k")


def test_apply_graphify_disabled_is_identity():
    argv = ("claude", "--print", "{PROMPT}")
    out_argv, out_env = apply_graphify_mcp(argv, {"A": "1"}, "claude", env={})
    assert out_argv == argv
    assert out_env == {"A": "1"}


def test_apply_graphify_claude_inserts_mcp_config_before_prompt():
    argv = ("claude", "--print", "{PROMPT}")
    out_argv, out_env = apply_graphify_mcp(argv, {}, "claude", env=_ON)
    assert "--mcp-config" in out_argv
    assert out_argv.index("--mcp-config") < out_argv.index("{PROMPT}")
    cfg = json.loads(out_argv[out_argv.index("--mcp-config") + 1])
    srv = cfg["mcpServers"]["graphify"]
    assert srv["url"] == EP
    # key referenced via env expansion (NOT inlined); the value is in extra_env
    assert srv["headers"]["Authorization"] == "Bearer ${GRAPHIFY_API_KEY}"
    assert KEY not in out_argv[out_argv.index("--mcp-config") + 1]
    assert out_env[GRAPHIFY_API_KEY_ENV] == KEY


def test_apply_graphify_claude_does_not_feed_prompt_to_mcp_config_BUG_522():
    # Claude's --mcp-config is variadic. The next positional argument after
    # the JSON config must not be the huge prompt, or Claude treats it as
    # another config path/string and can fail with ENAMETOOLONG.
    argv = (
        "claude", "-p", "--dangerously-skip-permissions",
        "--output-format", "stream-json", "--verbose", "{PROMPT}",
    )
    out_argv, _out_env = apply_graphify_mcp(argv, {}, "claude", env=_ON)

    values = _claude_mcp_config_values(out_argv)
    assert len(values) == 1
    assert "{PROMPT}" not in values


def test_apply_graphify_claude_bare_prompt_keeps_prompt_out_of_mcp_config_BUG_524():
    # Edge: a custom Claude argv can rely on the positional [prompt] without
    # any option before it. The graphify insertion still must not put the
    # prompt immediately after variadic --mcp-config.
    argv = ("claude", "{PROMPT}")
    out_argv, _out_env = apply_graphify_mcp(argv, {}, "claude", env=_ON)

    values = _claude_mcp_config_values(out_argv)
    assert len(values) == 1
    assert "{PROMPT}" not in values


def test_apply_graphify_codex_inserts_config_and_exports_key():
    argv = ("codex", "exec", "{PROMPT}")
    out_argv, out_env = apply_graphify_mcp(argv, {}, "codex", env=_ON)
    joined = " ".join(out_argv)
    assert f'mcp_servers.graphify.url="{EP}"' in joined
    assert 'mcp_servers.graphify.bearer_token_env_var="GRAPHIFY_API_KEY"' in joined
    assert KEY not in joined  # secret never enters codex argv
    assert out_env[GRAPHIFY_API_KEY_ENV] == KEY  # codex reads it from env


def test_apply_graphify_unknown_tool_is_identity():
    argv = ("opencode", "run", "{PROMPT}")
    out_argv, out_env = apply_graphify_mcp(argv, {"X": "y"}, "opencode", env=_ON)
    assert out_argv == argv
    assert out_env == {"X": "y"}
