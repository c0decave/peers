"""Translate semantic per-peer model/provider fields to CLI argv/env."""
from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable, Sequence

from peers.graphify_mcp import (
    GRAPHIFY_API_KEY_ENV,
    graphify_mcp_flags,
    graphify_runtime,
)

OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
OPENROUTER_CLAUDE_BASE_URL = "https://openrouter.ai/api"
OPENROUTER_CODEX_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_EXTRA_HOST_RE = r"^openrouter\.ai$"

_Translator = Callable[
    [tuple[str, ...], str | None, str | None, str | None],
    tuple[tuple[str, ...], dict[str, str]],
]
_CONFIG_ASSIGNMENT_RE = re.compile(
    r"^\s*([A-Za-z0-9_.-]+)\s*=\s*(.*?)\s*$"
)


def build_peer_argv(
    spec: Any,
    base_argv: Sequence[str] | None = None,
) -> tuple[tuple[str, ...], dict[str, str]]:
    """Return ``(argv, extra_env)`` for a peer invocation.

    ``argv`` is the existing escape hatch. Semantic fields only add the
    corresponding switch when the switch is not already present.
    """
    argv = tuple(base_argv if base_argv is not None else spec.argv)
    model = getattr(spec, "model", None)
    reasoning = getattr(spec, "reasoning", None)
    provider = getattr(spec, "provider", None)
    if not any((model, reasoning, provider)):
        return argv, {}

    tool = getattr(spec, "tool", None)
    translator = _TOOL_TRANSLATORS.get(tool) if isinstance(tool, str) else None
    if translator is not None:
        return translator(argv, model, reasoning, provider)
    raise ValueError(
        f"tool {tool!r} has no model/reasoning/provider translation; "
        "use argv directly"
    )


def apply_graphify_mcp(
    argv: Sequence[str],
    extra_env: Mapping[str, str],
    tool: str | None,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[tuple[str, ...], dict[str, str]]:
    """Splice the opt-in graphify MCP server into a peer launch.

    Keyed off the env signal (:func:`graphify_runtime`): when graphify is off
    -- or its caged sidecar never came up -- the argv and env are returned
    unchanged, so the launch is byte-identical to a no-graphify run. For a
    known tool the per-tool flags (:func:`graphify_mcp_flags`) are inserted in
    a tool-safe location: Claude's variadic ``--mcp-config`` must be followed
    by another option rather than the positional prompt, while codex config
    flags go after the ``codex`` subcommand. Both tools read the bearer token
    from ``GRAPHIFY_API_KEY`` (added to ``extra_env``) so the secret never
    enters argv.
    """
    base_argv = tuple(argv)
    base_env = dict(extra_env)
    runtime = graphify_runtime(env)
    if runtime is None:
        return base_argv, base_env
    endpoint, api_key = runtime
    flags = graphify_mcp_flags(tool or "", endpoint)
    if not flags:
        return base_argv, base_env
    # Both tools read the key from GRAPHIFY_API_KEY in the env (claude expands
    # ${GRAPHIFY_API_KEY} in the --mcp-config header; codex via
    # bearer_token_env_var), so the secret never enters argv.
    new_env = {**base_env, GRAPHIFY_API_KEY_ENV: api_key}
    if tool == "claude":
        return _insert_claude_mcp_config(base_argv, flags), new_env
    if tool == "codex":
        return _insert_codex_config(base_argv, flags), new_env
    return _insert_before_prompt(base_argv, flags), new_env


def validate_peer_runtime_env(
    specs: Sequence[Any],
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    """Validate environment required by semantic provider settings."""
    env_map = os.environ if env is None else env
    missing: dict[str, list[str]] = {}
    for spec in specs:
        peer_name = getattr(spec, "name", "<unknown>")
        for env_key in _required_env_keys_for_spec(spec):
            if not (env_map.get(env_key) or "").strip():
                missing.setdefault(env_key, []).append(peer_name)
    if missing:
        details = "; ".join(
            f"{env_key} ({', '.join(peer_names)})"
            for env_key, peer_names in sorted(missing.items())
        )
        raise ValueError(
            "missing required environment for peer provider settings: "
            f"{details}"
        )


def required_peer_runtime_env_keys(specs: Sequence[Any]) -> tuple[str, ...]:
    """Return provider env var names needed by these peer specs."""
    keys: list[str] = []
    seen: set[str] = set()
    for spec in specs:
        for env_key in _required_env_keys_for_spec(spec):
            if env_key in seen:
                continue
            seen.add(env_key)
            keys.append(env_key)
    return tuple(keys)


def _has_switch(argv: Sequence[str], switch: str) -> bool:
    prefix = switch + "="
    return any(arg == switch or arg.startswith(prefix) for arg in argv)


def _insert_before_prompt(
    argv: Sequence[str],
    additions: Sequence[str],
) -> tuple[str, ...]:
    out = list(argv)
    idx = next(
        (i for i, arg in enumerate(out) if "{PROMPT}" in arg),
        len(out),
    )
    out[idx:idx] = additions
    return tuple(out)


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _codex_config_arg(key: str, value: str) -> tuple[str, str]:
    return ("-c", f"{key}={_toml_string(value)}")


def _codex_config_values(argv: Sequence[str]) -> tuple[str, ...]:
    values: list[str] = []
    for i, arg in enumerate(argv):
        if arg in ("-c", "--config") and i + 1 < len(argv):
            values.append(argv[i + 1])
        elif arg.startswith("-c=") or arg.startswith("--config="):
            values.append(arg.split("=", 1)[1])
    return tuple(values)


def _split_codex_config_value(value: str) -> tuple[str, str] | None:
    match = _CONFIG_ASSIGNMENT_RE.match(value)
    if not match:
        return None
    return match.group(1), match.group(2)


def _codex_config_present(argv: Sequence[str], key: str) -> bool:
    for value in _codex_config_values(argv):
        assignment = _split_codex_config_value(value)
        if assignment is not None and assignment[0] == key:
            return True
    return False


def _codex_model_present(argv: Sequence[str]) -> bool:
    return (
        _has_switch(argv, "--model")
        or _has_switch(argv, "-m")
        or _codex_config_present(argv, "model")
    )


def _parse_codex_config_scalar(raw_value: str) -> Any:
    try:
        return tomllib.loads(f"value = {raw_value}")["value"]
    except tomllib.TOMLDecodeError:
        return raw_value.strip("\"'")


def _codex_config_value_equals(
    argv: Sequence[str],
    key: str,
    expected: str,
) -> bool:
    found = False
    for value in _codex_config_values(argv):
        assignment = _split_codex_config_value(value)
        if assignment is None or assignment[0] != key:
            continue
        found = True
        if _parse_codex_config_scalar(assignment[1]) != expected:
            return False
    return found


def _codex_config_string_values(
    argv: Sequence[str],
    key: str,
) -> tuple[str, ...]:
    values: list[str] = []
    for value in _codex_config_values(argv):
        assignment = _split_codex_config_value(value)
        if assignment is None or assignment[0] != key:
            continue
        parsed_value = _parse_codex_config_scalar(assignment[1])
        if isinstance(parsed_value, str):
            values.append(parsed_value)
    return tuple(values)


def _required_env_keys_for_spec(spec: Any) -> tuple[str, ...]:
    if getattr(spec, "provider", None) != "openrouter":
        return ()
    if getattr(spec, "tool", None) == "claude":
        return (OPENROUTER_API_KEY_ENV,)
    if getattr(spec, "tool", None) == "codex":
        argv, _env = build_peer_argv(spec)
        if not _codex_config_value_equals(
            argv, "model_provider", "openrouter",
        ):
            return ()
        env_keys = _codex_config_string_values(
            argv, "model_providers.openrouter.env_key",
        )
        return (env_keys[-1] if env_keys else OPENROUTER_API_KEY_ENV,)
    return (OPENROUTER_API_KEY_ENV,)


def _insert_codex_config(
    argv: Sequence[str],
    additions: Sequence[str],
) -> tuple[str, ...]:
    out = list(argv)
    if out and Path(out[0]).name == "codex":
        out[1:1] = additions
        return tuple(out)
    return _insert_before_prompt(out, additions)


def _insert_claude_mcp_config(
    argv: Sequence[str],
    additions: Sequence[str],
) -> tuple[str, ...]:
    out = list(argv)
    prompt_idx = next(
        (i for i, arg in enumerate(out) if "{PROMPT}" in arg),
        len(out),
    )
    # Claude's --mcp-config is variadic (<configs...>). If the JSON config is
    # inserted immediately before the prompt, Claude consumes the prompt as a
    # second config and tries to open it as a filename. Put the config before
    # the first existing option so the option boundary terminates the variadic
    # list.
    for idx in range(1, prompt_idx):
        if out[idx].startswith("-"):
            out[idx:idx] = additions
            return tuple(out)
    if prompt_idx < len(out):
        # No pre-prompt option exists to terminate Claude's variadic
        # --mcp-config list. Claude accepts options after its positional
        # prompt, and placing the config there keeps the prompt out of
        # the variadic values.
        out[prompt_idx + 1:prompt_idx + 1] = additions
        return tuple(out)
    out.extend(additions)
    return tuple(out)


def _build_claude_argv(
    argv: tuple[str, ...],
    model: str | None,
    reasoning: str | None,
    provider: str | None,
) -> tuple[tuple[str, ...], dict[str, str]]:
    additions: list[str] = []
    if model and not _has_switch(argv, "--model"):
        additions += ["--model", model]
    if reasoning and not _has_switch(argv, "--effort"):
        additions += ["--effort", reasoning]
    extra_env: dict[str, str] = {}
    if provider == "openrouter":
        extra_env["ANTHROPIC_BASE_URL"] = OPENROUTER_CLAUDE_BASE_URL
        extra_env["ANTHROPIC_AUTH_TOKEN"] = os.environ.get(
            OPENROUTER_API_KEY_ENV, ""
        )
        extra_env["ANTHROPIC_API_KEY"] = ""
    return _insert_before_prompt(argv, additions), extra_env


def _build_codex_argv(
    argv: tuple[str, ...],
    model: str | None,
    reasoning: str | None,
    provider: str | None,
) -> tuple[tuple[str, ...], dict[str, str]]:
    additions: list[str] = []
    if provider == "openrouter":
        has_model_provider = _codex_config_present(argv, "model_provider")
        model_provider_is_openrouter = _codex_config_value_equals(
            argv, "model_provider", "openrouter",
        )
        should_add_provider_fields = (
            not has_model_provider or model_provider_is_openrouter
        )
        if not has_model_provider:
            additions += list(_codex_config_arg("model_provider", "openrouter"))
        if should_add_provider_fields:
            provider_fields = (
                ("model_providers.openrouter.name", "openrouter"),
                ("model_providers.openrouter.base_url", OPENROUTER_CODEX_BASE_URL),
                ("model_providers.openrouter.env_key", OPENROUTER_API_KEY_ENV),
            )
            for key, value in provider_fields:
                if not _codex_config_present(argv, key):
                    additions += list(_codex_config_arg(key, value))
    if model and not _codex_model_present(argv):
        additions += list(_codex_config_arg("model", model))
    if reasoning and not _codex_config_present(
        argv, "model_reasoning_effort",
    ):
        additions += list(_codex_config_arg(
            "model_reasoning_effort", reasoning,
        ))
    return _insert_codex_config(argv, additions), {}


def _build_opencode_argv(
    argv: tuple[str, ...],
    model: str | None,
    reasoning: str | None,
    provider: str | None,
) -> tuple[tuple[str, ...], dict[str, str]]:
    # opencode encodes the provider in `model` as `<provider>/<model>` and
    # takes the reasoning effort via `--variant`. No env/provider plumbing
    # (opencode resolves providers from its own config). An explicit -m /
    # --variant already in the argv wins.
    additions: list[str] = []
    if model and not _has_switch(argv, "-m") and not _has_switch(argv, "--model"):
        additions += ["-m", model]
    if reasoning and not _has_switch(argv, "--variant"):
        additions += ["--variant", reasoning]
    return _insert_before_prompt(argv, additions), {}


_TOOL_TRANSLATORS: dict[str, _Translator] = {
    "claude": _build_claude_argv,
    "codex": _build_codex_argv,
    "opencode": _build_opencode_argv,
}
