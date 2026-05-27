"""Detect whether a peer's CLI is OAuth-billed (subscription) or
API-key-billed (per-token).

Why: `budget.max_usd` as a HARD cap is meaningful only when the user
actually pays per-token (API-key mode). For OAuth-authenticated CLIs
the user pays a flat subscription; killing a loop on
`total_cost_usd >= max_usd` is a spurious failure — the field reports
"what this would cost on the API", not what the user spends.

Strategy: opportunistic, conservative.
- ANTHROPIC_API_KEY / OPENAI_API_KEY env vars are explicit "API mode".
- Auth files in ~/.claude/ and ~/.codex/ that carry OAuth markers are
  treated as OAuth.
- When ambiguous, return "unknown" — the caller's policy decides how
  to treat that (we default to WARN, not HARD, on unknown).

Public API: `detect_billing_mode(tool)` and `resolve_max_usd_mode(...)`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from peers.safe_io import read_bytes_no_symlink


BillingMode = Literal["api", "oauth", "unknown"]
MaxUsdMode = Literal["auto", "hard", "warn", "off"]
_AUTH_FILE_MAX_BYTES = 1 * 1024 * 1024


def _read_auth_json(path: Path) -> dict | None:
    try:
        raw = read_bytes_no_symlink(path, max_bytes=_AUTH_FILE_MAX_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _AUTH_FILE_MAX_BYTES:
        return None
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _is_regular_auth_file(path: Path) -> bool:
    try:
        raw = read_bytes_no_symlink(path, max_bytes=_AUTH_FILE_MAX_BYTES + 1)
    except OSError:
        return False
    return len(raw) <= _AUTH_FILE_MAX_BYTES


def detect_billing_mode(tool: str, home: Path | None = None) -> BillingMode:
    """Return "api" | "oauth" | "unknown" for the given peer tool.

    `home` is overridable for tests (default: `Path.home()`).
    """
    home = home if home is not None else Path.home()

    if tool == "claude":
        # Explicit env-var API key trumps any OAuth file.
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "api"
        # Claude Code stores OAuth credentials in ~/.claude/.credentials.json.
        cred = home / ".claude" / ".credentials.json"
        if _is_regular_auth_file(cred):
            return "oauth"
        # ~/.claude.json with user/subscription markers is a softer signal.
        cfg = home / ".claude.json"
        data = _read_auth_json(cfg)
        if data is not None:
            if isinstance(data, dict) and (
                "userID" in data
                or "hasOpusPlanDefault" in data
                or "passesEligibilityCache" in data
            ):
                return "oauth"
        return "unknown"

    if tool == "codex":
        if os.environ.get("OPENAI_API_KEY"):
            return "api"
        auth = home / ".codex" / "auth.json"
        if not auth.exists():
            return "unknown"
        data = _read_auth_json(auth)
        if data is None:
            return "unknown"
        # Canonical: codex auth.json carries `auth_mode`.
        mode = str(data.get("auth_mode", "")).lower()
        if mode in ("chatgpt", "oauth"):
            return "oauth"
        if mode in ("apikey", "api_key", "api-key"):
            return "api"
        # Fallback: presence of tokens without a populated API key.
        if data.get("tokens") and not data.get("OPENAI_API_KEY"):
            return "oauth"
        if data.get("OPENAI_API_KEY"):
            return "api"
        return "unknown"

    # Unknown tool: callers should not assume billing model.
    return "unknown"


def resolve_max_usd_mode(
    declared_mode: str | None,
    peer_tools: list[str],
    home: Path | None = None,
) -> tuple[MaxUsdMode, str]:
    """Decide effective `max_usd_mode` given the user's declaration and
    the actual peer tools.

    Returns (effective_mode, reason). `reason` is a short human-readable
    string for the log / `peers info` output.

    Decision table for declared_mode == "auto":
      - ANY peer in API mode → "hard"   (some real $ at risk)
      - ALL peers in OAuth mode → "warn" (no $ spent; warn on threshold)
      - Otherwise (any unknown, no api) → "warn" (conservative default)

    Explicit declared_mode ∈ {"hard","warn","off"} passes through.
    `None` or empty is treated as "auto".
    """
    declared = (declared_mode or "auto").lower()
    if declared in ("hard", "warn", "off"):
        return declared, f"explicit max_usd_mode={declared}"  # type: ignore[return-value]
    if declared != "auto":
        return "warn", f"unknown max_usd_mode={declared!r}, defaulting to warn"

    if not peer_tools:
        return "warn", "auto: no peer tools known"

    modes = [detect_billing_mode(t, home=home) for t in peer_tools]
    if "api" in modes:
        return "hard", "auto: at least one peer uses an API key"
    if all(m == "oauth" for m in modes):
        return "warn", "auto: all peers OAuth-billed"
    return "warn", (
        f"auto: peer billing mix={dict(zip(peer_tools, modes))}, "
        "defaulting to warn"
    )
