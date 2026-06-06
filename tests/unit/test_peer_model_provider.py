from __future__ import annotations

import pytest

from peers.model_provider import (
    OPENROUTER_API_KEY_ENV,
    OPENROUTER_CLAUDE_BASE_URL,
    OPENROUTER_CODEX_BASE_URL,
    build_peer_argv,
    validate_peer_runtime_env,
)
from peers.peer_spec import PeerSpec, apply_peer_field_overrides, load_peer_specs


def _cfg(peer: dict) -> dict:
    return {"peers": [peer]}


def test_load_peer_specs_accepts_model_reasoning_provider() -> None:
    specs = load_peer_specs(_cfg({
        "name": "codex",
        "tool": "codex",
        "argv": ["codex", "exec", "{PROMPT}"],
        "prompt_mode": "argv-substitute",
        "model": "openai/gpt-5.1",
        "reasoning": "XHIGH",
        "provider": "OpenRouter",
    }))

    assert specs[0].model == "openai/gpt-5.1"
    assert specs[0].reasoning == "xhigh"
    assert specs[0].provider == "openrouter"


@pytest.mark.parametrize("field", ["model", "reasoning", "provider"])
def test_load_peer_specs_rejects_empty_semantic_fields(field: str) -> None:
    peer = {
        "name": "claude",
        "tool": "claude",
        "argv": ["claude", "-p", "{PROMPT}"],
        field: " ",
    }

    with pytest.raises(ValueError, match=field):
        load_peer_specs(_cfg(peer))


def test_load_peer_specs_rejects_invalid_reasoning_for_tool() -> None:
    with pytest.raises(ValueError, match="reasoning"):
        load_peer_specs(_cfg({
            "name": "claude",
            "tool": "claude",
            "argv": ["claude", "-p", "{PROMPT}"],
            "reasoning": "minimal",
        }))


def test_load_peer_specs_accepts_opencode_model_and_variant() -> None:
    """opencode is a first-class tool: `model` is opencode's `provider/model`
    string (validated by opencode, not peers) and `reasoning` maps to
    `--variant`. No separate `provider:` field — it lives in the model."""
    specs = load_peer_specs(_cfg({
        "name": "opencode",
        "tool": "opencode",
        "argv": ["opencode", "run", "{PROMPT}"],
        "prompt_mode": "argv-substitute",
        "model": "ollama/qwen2.5",
        "reasoning": "HIGH",
    }))

    assert specs[0].tool == "opencode"
    assert specs[0].model == "ollama/qwen2.5"
    assert specs[0].reasoning == "high"
    assert specs[0].provider is None


def test_load_peer_specs_rejects_provider_field_for_opencode() -> None:
    """opencode encodes the provider in `model` (provider/model); a separate
    `provider:` field is rejected with a guiding message."""
    with pytest.raises(ValueError, match="opencode"):
        load_peer_specs(_cfg({
            "name": "opencode",
            "tool": "opencode",
            "argv": ["opencode", "run", "{PROMPT}"],
            "model": "ollama/qwen2.5",
            "provider": "ollama",
        }))


def test_build_peer_argv_translates_opencode_model_and_variant() -> None:
    spec = PeerSpec(
        name="opencode",
        tool="opencode",
        argv=("opencode", "run", "--format", "json",
              "--dangerously-skip-permissions", "{PROMPT}"),
        prompt_mode="argv-substitute",
        model="ollama/qwen2.5",
        reasoning="high",
    )

    argv, env = build_peer_argv(spec)

    assert env == {}
    assert argv[-1] == "{PROMPT}"
    # `-m <model>` and `--variant <reasoning>` injected before the prompt.
    assert argv[argv.index("-m") + 1] == "ollama/qwen2.5"
    assert argv[argv.index("--variant") + 1] == "high"


def test_build_peer_argv_respects_explicit_opencode_model() -> None:
    """An explicit -m in the argv wins; the builder does not add a second."""
    spec = PeerSpec(
        name="opencode",
        tool="opencode",
        argv=("opencode", "run", "-m", "anthropic/claude-x", "{PROMPT}"),
        prompt_mode="argv-substitute",
        model="ollama/qwen2.5",
    )

    argv, env = build_peer_argv(spec)

    assert argv.count("-m") == 1
    assert "anthropic/claude-x" in argv
    assert "ollama/qwen2.5" not in argv


def test_load_peer_specs_rejects_implausible_provider_for_tool() -> None:
    with pytest.raises(ValueError, match="provider"):
        load_peer_specs(_cfg({
            "name": "claude",
            "tool": "claude",
            "argv": ["claude", "-p", "{PROMPT}"],
            "provider": "openai",
        }))


def test_build_peer_argv_translates_claude_model_reasoning_and_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    spec = PeerSpec(
        name="claude",
        tool="claude",
        argv=("claude", "-p", "{PROMPT}"),
        prompt_mode="argv-substitute",
        model="anthropic/claude-opus-4.8",
        reasoning="high",
        provider="openrouter",
    )

    argv, env = build_peer_argv(spec)

    assert argv == (
        "claude", "-p",
        "--model", "anthropic/claude-opus-4.8",
        "--effort", "high",
        "{PROMPT}",
    )
    assert env == {
        "ANTHROPIC_BASE_URL": OPENROUTER_CLAUDE_BASE_URL,
        "ANTHROPIC_AUTH_TOKEN": "sk-or-test",
        "ANTHROPIC_API_KEY": "",
    }


def test_build_peer_argv_respects_explicit_claude_flags() -> None:
    spec = PeerSpec(
        name="claude",
        tool="claude",
        argv=("claude", "-p", "--model", "manual", "--effort=low", "{PROMPT}"),
        prompt_mode="argv-substitute",
        model="semantic",
        reasoning="high",
    )

    argv, env = build_peer_argv(spec)

    assert argv == spec.argv
    assert env == {}


def test_build_peer_argv_translates_codex_openrouter() -> None:
    spec = PeerSpec(
        name="codex",
        tool="codex",
        argv=("codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "{PROMPT}"),
        prompt_mode="argv-substitute",
        model="~openai/gpt-latest",
        reasoning="xhigh",
        provider="openrouter",
    )

    argv, env = build_peer_argv(spec)

    assert env == {}
    assert argv[:1] == ("codex",)
    assert argv[-2:] == ("--dangerously-bypass-approvals-and-sandbox", "{PROMPT}")
    config_values = [
        argv[i + 1] for i, value in enumerate(argv)
        if value == "-c"
    ]
    assert 'model_provider="openrouter"' in config_values
    assert 'model_providers.openrouter.name="openrouter"' in config_values
    assert (
        f'model_providers.openrouter.base_url="{OPENROUTER_CODEX_BASE_URL}"'
        in config_values
    )
    assert 'model_providers.openrouter.env_key="OPENROUTER_API_KEY"' in config_values
    assert 'model="~openai/gpt-latest"' in config_values
    assert 'model_reasoning_effort="xhigh"' in config_values


def test_build_peer_argv_respects_explicit_codex_config() -> None:
    spec = PeerSpec(
        name="codex",
        tool="codex",
        argv=(
            "codex", "exec",
            "-c", 'model="manual"',
            "-c", 'model_reasoning_effort="low"',
            "--model", "manual-flag",
            "{PROMPT}",
        ),
        prompt_mode="argv-substitute",
        model="semantic",
        reasoning="high",
    )

    argv, _env = build_peer_argv(spec)

    assert argv.count("-c") == 2
    assert 'model="semantic"' not in argv
    assert 'model_reasoning_effort="high"' not in argv


def test_build_peer_argv_respects_explicit_codex_provider_config() -> None:
    spec = PeerSpec(
        name="codex",
        tool="codex",
        argv=(
            "codex", "exec",
            "-c", 'model_provider="custom"',
            "{PROMPT}",
        ),
        prompt_mode="argv-substitute",
        model="semantic",
        provider="openrouter",
    )

    argv, _env = build_peer_argv(spec)

    assert 'model_provider="custom"' in argv
    assert 'model_provider="openrouter"' not in argv
    assert not any(
        isinstance(value, str)
        and value.startswith("model_providers.openrouter.")
        for value in argv
    )
    assert 'model="semantic"' in argv


def test_build_peer_argv_does_not_substring_match_codex_provider() -> None:
    spec = PeerSpec(
        name="codex",
        tool="codex",
        argv=(
            "codex", "exec",
            "-c", 'model_provider="not-openrouter"',
            "{PROMPT}",
        ),
        prompt_mode="argv-substitute",
        provider="openrouter",
    )

    argv, _env = build_peer_argv(spec)

    assert 'model_provider="not-openrouter"' in argv
    assert 'model_provider="openrouter"' not in argv
    assert not any(
        isinstance(value, str)
        and value.startswith("model_providers.openrouter.")
        for value in argv
    )


def test_build_peer_argv_accepts_explicit_codex_openrouter_provider() -> None:
    spec = PeerSpec(
        name="codex",
        tool="codex",
        argv=(
            "codex", "exec",
            '--config=model_provider="openrouter"',
            "{PROMPT}",
        ),
        prompt_mode="argv-substitute",
        provider="openrouter",
    )

    argv, _env = build_peer_argv(spec)

    assert '--config=model_provider="openrouter"' in argv
    assert 'model_provider="openrouter"' not in argv
    assert 'model_providers.openrouter.env_key="OPENROUTER_API_KEY"' in argv


def test_build_peer_argv_keeps_conflicting_codex_provider_explicit() -> None:
    spec = PeerSpec(
        name="codex",
        tool="codex",
        argv=(
            "codex", "exec",
            "-c", 'model_provider="openrouter"',
            "-c", 'model_provider="custom"',
            "{PROMPT}",
        ),
        prompt_mode="argv-substitute",
        provider="openrouter",
    )

    argv, _env = build_peer_argv(spec)

    assert argv.count("-c") == 2
    assert not any(
        isinstance(value, str)
        and value.startswith("model_providers.openrouter.")
        for value in argv
    )


def test_build_peer_argv_respects_spaced_codex_config_assignments() -> None:
    spec = PeerSpec(
        name="codex",
        tool="codex",
        argv=(
            "codex", "exec",
            "-c", 'model_provider = "custom"',
            "-c", 'model = "manual"',
            "-c", 'model_reasoning_effort = "low"',
            "{PROMPT}",
        ),
        prompt_mode="argv-substitute",
        model="semantic",
        reasoning="high",
        provider="openrouter",
    )

    argv, _env = build_peer_argv(spec)

    assert argv.count("-c") == 3
    assert 'model_provider="openrouter"' not in argv
    assert 'model="semantic"' not in argv
    assert 'model_reasoning_effort="high"' not in argv
    assert not any(
        isinstance(value, str)
        and value.startswith("model_providers.openrouter.")
        for value in argv
    )


def test_build_peer_argv_adds_openrouter_fields_for_spaced_codex_provider(
) -> None:
    spec = PeerSpec(
        name="codex",
        tool="codex",
        argv=(
            "codex", "exec",
            "--config", 'model_provider = "openrouter"',
            "{PROMPT}",
        ),
        prompt_mode="argv-substitute",
        provider="openrouter",
    )

    argv, _env = build_peer_argv(spec)

    assert 'model_provider="openrouter"' not in argv
    assert 'model_providers.openrouter.env_key="OPENROUTER_API_KEY"' in argv


def test_build_peer_argv_without_semantic_fields_keeps_unknown_tool() -> None:
    spec = PeerSpec(
        name="other",
        tool="other",
        argv=("other", "{PROMPT}"),
    )

    argv, env = build_peer_argv(spec)

    assert argv == spec.argv
    assert env == {}


def test_build_peer_argv_unknown_tool_with_semantic_fields_errors() -> None:
    spec = PeerSpec(
        name="other",
        tool="other",
        argv=("other", "{PROMPT}"),
        model="m",
    )

    with pytest.raises(ValueError, match="no model/reasoning/provider"):
        build_peer_argv(spec)


def test_validate_peer_runtime_env_requires_openrouter_key() -> None:
    spec = PeerSpec(
        name="claude",
        tool="claude",
        argv=("claude", "{PROMPT}"),
        provider="openrouter",
    )

    with pytest.raises(ValueError, match=OPENROUTER_API_KEY_ENV):
        validate_peer_runtime_env([spec], env={})

    validate_peer_runtime_env([spec], env={OPENROUTER_API_KEY_ENV: "sk-or"})


def test_validate_peer_runtime_env_respects_codex_custom_env_key() -> None:
    spec = PeerSpec(
        name="codex",
        tool="codex",
        argv=(
            "codex", "exec",
            "-c", 'model_provider = "openrouter"',
            "-c", 'model_providers.openrouter.env_key = "CUSTOM_OR_KEY"',
            "{PROMPT}",
        ),
        prompt_mode="argv-substitute",
        provider="openrouter",
    )

    with pytest.raises(ValueError, match="CUSTOM_OR_KEY"):
        validate_peer_runtime_env([spec], env={OPENROUTER_API_KEY_ENV: "sk-or"})

    validate_peer_runtime_env([spec], env={"CUSTOM_OR_KEY": "sk-custom"})


def test_validate_peer_runtime_env_respects_explicit_codex_custom_provider() -> None:
    spec = PeerSpec(
        name="codex",
        tool="codex",
        argv=(
            "codex", "exec",
            "-c", 'model_provider = "custom"',
            "{PROMPT}",
        ),
        prompt_mode="argv-substitute",
        provider="openrouter",
    )

    validate_peer_runtime_env([spec], env={})


def test_apply_peer_field_overrides_targets_name_or_tool() -> None:
    cfg = {
        "peers": [
            {"name": "claude", "tool": "claude", "argv": ["claude"]},
            {"name": "claude-2", "tool": "claude", "argv": ["claude"]},
            {"name": "codex", "tool": "codex", "argv": ["codex"]},
        ],
    }

    out = apply_peer_field_overrides(
        cfg,
        peer_model=["claude=opus", "codex=gpt-5.1"],
        peer_reasoning=["high"],
        peer_provider=["codex=openrouter"],
    )

    peers = {p["name"]: p for p in out["peers"]}
    assert peers["claude"]["model"] == "opus"
    assert peers["claude-2"]["model"] == "opus"
    assert peers["codex"]["model"] == "gpt-5.1"
    assert all(p["reasoning"] == "high" for p in out["peers"])
    assert peers["codex"]["provider"] == "openrouter"


def test_apply_peer_field_overrides_supports_legacy_tools_shape() -> None:
    cfg = {
        "tools": {
            "claude": {"argv": ["claude"]},
            "codex": {"argv": ["codex"]},
        },
    }

    out = apply_peer_field_overrides(
        cfg,
        peer_model=["codex=~openai/gpt-latest"],
        peer_reasoning=["high"],
        peer_provider=["codex=openrouter"],
    )

    assert out["tools"]["claude"]["reasoning"] == "high"
    assert out["tools"]["codex"]["model"] == "~openai/gpt-latest"
    assert out["tools"]["codex"]["reasoning"] == "high"
    assert out["tools"]["codex"]["provider"] == "openrouter"


def test_apply_peer_field_overrides_canonicalizes_enum_values() -> None:
    cfg = {
        "peers": [
            {"name": "codex", "tool": "codex", "argv": ["codex"]},
        ],
    }

    out = apply_peer_field_overrides(
        cfg,
        peer_model=["MiXeD/Case"],
        peer_reasoning=["XHIGH"],
        peer_provider=["OpenRouter"],
    )

    assert out["peers"][0]["model"] == "MiXeD/Case"
    assert out["peers"][0]["reasoning"] == "xhigh"
    assert out["peers"][0]["provider"] == "openrouter"
