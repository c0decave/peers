"""Tests for OAuth detection + max_usd_mode resolution.

Covers detect_billing_mode for claude/codex against synthetic auth
files, and resolve_max_usd_mode's auto-mode decision table.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from peers.billing import (
    detect_billing_mode,
    resolve_max_usd_mode,
)


# ---------------- detect_billing_mode --------------------------------


def test_claude_api_via_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert detect_billing_mode("claude", home=tmp_path) == "api"


def test_claude_oauth_via_credentials_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_text(
        '{"oauth_token": "x"}'
    )
    assert detect_billing_mode("claude", home=tmp_path) == "oauth"


def test_claude_oauth_via_claude_json_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / ".claude.json").write_text(json.dumps({
        "userID": "u-123", "hasOpusPlanDefault": True,
    }))
    assert detect_billing_mode("claude", home=tmp_path) == "oauth"


def test_claude_unknown_when_no_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert detect_billing_mode("claude", home=tmp_path) == "unknown"


def test_claude_ignores_symlinked_credentials_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / ".claude").mkdir()
    bait = tmp_path / "credentials.json"
    bait.write_text('{"oauth_token": "x"}')
    (tmp_path / ".claude" / ".credentials.json").symlink_to(bait)

    assert detect_billing_mode("claude", home=tmp_path) == "unknown"


def test_codex_oauth_via_chatgpt_auth_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text(json.dumps({
        "auth_mode": "chatgpt", "tokens": {"access": "x"},
    }))
    assert detect_billing_mode("codex", home=tmp_path) == "oauth"


def test_codex_api_via_auth_mode_apikey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text(json.dumps({
        "auth_mode": "ApiKey", "OPENAI_API_KEY": "sk-x",
    }))
    assert detect_billing_mode("codex", home=tmp_path) == "api"


def test_codex_api_via_env_takes_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text(json.dumps({
        "auth_mode": "chatgpt",  # would otherwise say oauth
    }))
    assert detect_billing_mode("codex", home=tmp_path) == "api"


def test_codex_unknown_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert detect_billing_mode("codex", home=tmp_path) == "unknown"


def test_unknown_tool_returns_unknown(tmp_path: Path):
    assert detect_billing_mode("notatool", home=tmp_path) == "unknown"


def test_corrupt_auth_json_returns_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text("{ not json")
    assert detect_billing_mode("codex", home=tmp_path) == "unknown"


def test_codex_ignores_symlinked_auth_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / ".codex").mkdir()
    bait = tmp_path / "auth.json"
    bait.write_text(json.dumps({"auth_mode": "chatgpt"}))
    (tmp_path / ".codex" / "auth.json").symlink_to(bait)

    assert detect_billing_mode("codex", home=tmp_path) == "unknown"


def test_codex_ignores_oversized_auth_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text(
        '{"auth_mode":"chatgpt","padding":"' + ("x" * (1024 * 1024 + 1)) + '"}'
    )

    assert detect_billing_mode("codex", home=tmp_path) == "unknown"


# ---------------- resolve_max_usd_mode -------------------------------


def test_resolve_explicit_modes_pass_through(tmp_path: Path):
    for m in ("hard", "warn", "off"):
        mode, _ = resolve_max_usd_mode(m, ["claude"], home=tmp_path)
        assert mode == m


def test_resolve_auto_with_api_peer_picks_hard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    mode, reason = resolve_max_usd_mode("auto", ["claude"], home=tmp_path)
    assert mode == "hard"
    assert "API key" in reason


def test_resolve_auto_with_all_oauth_picks_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_text("{}")
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text(
        json.dumps({"auth_mode": "chatgpt"})
    )
    mode, reason = resolve_max_usd_mode(
        "auto", ["claude", "codex"], home=tmp_path,
    )
    assert mode == "warn"
    assert "OAuth" in reason


def test_resolve_auto_with_mixed_unknown_defaults_to_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """One OAuth + one unknown peer → warn (we don't escalate to hard
    on unknowns; conservative default)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_text("{}")
    # No codex auth file → unknown
    mode, _ = resolve_max_usd_mode(
        "auto", ["claude", "codex"], home=tmp_path,
    )
    assert mode == "warn"


def test_resolve_garbage_mode_falls_back_to_warn(tmp_path: Path):
    mode, reason = resolve_max_usd_mode("nope", ["claude"], home=tmp_path)
    assert mode == "warn"
    assert "nope" in reason


def test_resolve_none_treated_as_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    mode, _ = resolve_max_usd_mode(None, ["claude"], home=tmp_path)
    assert mode == "hard"


# ---------------- opencode (Option C / first-class peer) -------------------

def test_opencode_billing_is_unknown_and_resolves_to_warn(tmp_path: Path):
    """opencode's per-call billing depends on the model (local=free, opencode
    zen=subscription, BYOK cloud=metered) and cannot be inferred from the tool
    name, so detection is "unknown" → resolve_max_usd_mode defaults to "warn".
    Locks the contract: a future change must not silently hard-kill an
    opencode run on max_usd (mirrors the OAuth/subscription policy)."""
    assert detect_billing_mode("opencode", home=tmp_path) == "unknown"
    mode, _reason = resolve_max_usd_mode("auto", ["opencode"], home=tmp_path)
    assert mode == "warn"


def test_opencode_mixed_with_api_peer_still_hard(tmp_path: Path, monkeypatch):
    """If another peer is genuinely API-billed, the run still hard-caps —
    opencode's "unknown" does not weaken a real per-token risk."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    mode, _ = resolve_max_usd_mode("auto", ["opencode", "claude"], home=tmp_path)
    assert mode == "hard"
