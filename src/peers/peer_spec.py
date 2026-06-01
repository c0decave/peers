"""PeerSpec — per-peer configuration loaded from config.yaml.

Supports two on-disk shapes for backward compat:

1. New:

   peers:
     - name: claude
       tool: claude
       argv: ["claude", "-p", "{PROMPT}"]
       prompt_mode: argv-substitute

2. Legacy (Phase 1/2):

   tools:
     claude:
       argv: ["claude", "-p", "{PROMPT}"]
       prompt_mode: argv-substitute
     codex:
       ...

The legacy shape is auto-promoted to the new shape at load time:
`name == tool == <map key>`, peer order is `[claude, codex]`.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any


VALID_PROMPT_MODES = ("stdin", "argv-substitute")

# Item 13: peer-role topology. See PeerSpec.role docstring for semantics.
VALID_PEER_ROLES = ("default", "recovery", "witness", "debater")

# `tool` selects which token/USD parser to use in the driver and which
# default invocation conventions apply. Two are recognised today;
# anything else falls through to a no-op token parser.
KNOWN_TOOLS = ("claude", "codex")

VALID_PROVIDERS = ("anthropic", "openai", "openrouter")
VALID_REASONING_BY_TOOL = {
    "claude": ("low", "medium", "high", "xhigh", "max"),
    "codex": ("minimal", "low", "medium", "high", "xhigh"),
}
VALID_PROVIDERS_BY_TOOL = {
    "claude": ("anthropic", "openrouter"),
    "codex": ("openai", "openrouter"),
}

# Peer names land in filesystem path components (`.peers/comms/<name>-to-...`),
# tmux session/window names, and prompt text. Restrict to a tame token to
# avoid path traversal, shell injection in derived strings, and ambiguous
# tmux targets.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,31}$")

# M4 + L2: names the substrate reserves for itself. Allowing a peer to
# claim these would let it impersonate substrate commits (peers-init,
# peers-substrate write commits with these Peer: trailers) or collide
# with substrate-managed directories under .peers/.
RESERVED_PEER_NAMES = frozenset({
    "peers-substrate",   # author of anti-cheating revert commits
    "peers-init",        # author of post-init .gitignore commit
    "archive",           # collides with .peers/comms/archive/
    "comms", "log", "logs", "checks", "hooks", "queue",
})


def is_valid_peer_name(name: str) -> bool:
    return (
        isinstance(name, str)
        and bool(_NAME_RE.match(name))
        and name not in RESERVED_PEER_NAMES
    )


@dataclass(frozen=True)
class PeerSpec:
    name: str
    tool: str
    argv: tuple[str, ...]
    prompt_mode: str = "stdin"
    # Item 13: n>2 peer topologies. `role` selects substrate behavior:
    #   "default"  — regular peer in rotation (legacy)
    #   "recovery" — only activated when all default peers are degraded
    #   "witness"  — commits annotations only, never claims handoff
    #                (prompt-template variation; substrate is role-agnostic
    #                aside from skip-from-rotation rule below)
    #   "debater"  — takes the opposing position (prompt-template variation)
    # The substrate distinguishes only "default" vs "recovery" today;
    # witness/debater are pure prompt patterns living in the mode template.
    role: str = "default"
    model: str | None = None
    reasoning: str | None = None
    provider: str | None = None


@dataclass(frozen=True)
class PeerFieldOverride:
    key: str | None
    value: str


def parse_peer_field_overrides(
    values: list[str] | tuple[str, ...] | None,
    *,
    flag_name: str,
) -> tuple[PeerFieldOverride, ...]:
    """Parse ``--peer-*`` values.

    A bare value applies to all peers. ``name=value`` or ``tool=value``
    applies to matching peer entries when the config is patched.
    """
    overrides: list[PeerFieldOverride] = []
    for raw in values or ():
        if not isinstance(raw, str):
            raise ValueError(f"{flag_name} values must be strings")
        text = raw.strip()
        if not text:
            raise ValueError(f"{flag_name} value must not be empty")
        key: str | None
        value: str
        if "=" in text:
            key, value = text.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                raise ValueError(f"{flag_name} key must not be empty")
            if not value:
                raise ValueError(
                    f"{flag_name} value for {key!r} must not be empty"
                )
        else:
            key = None
            value = text
        overrides.append(PeerFieldOverride(key=key, value=value))
    return tuple(overrides)


def _optional_nonempty_string(
    entry: dict[str, Any],
    field: str,
    loc: str,
) -> str | None:
    value = entry.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{loc}.{field} must be a non-empty string")
    return value.strip()


def _validate_semantic_fields(
    *,
    loc: str,
    tool: str,
    model: str | None,
    reasoning: str | None,
    provider: str | None,
) -> tuple[str | None, str | None, str | None]:
    if reasoning is not None:
        reasoning = reasoning.lower()
    if provider is not None:
        provider = provider.lower()
    has_semantic = any(v is not None for v in (model, reasoning, provider))
    if has_semantic and tool not in KNOWN_TOOLS:
        raise ValueError(
            f"{loc}.tool {tool!r} has no model/reasoning/provider "
            "translation; use argv directly"
        )
    if reasoning is not None:
        allowed_reasoning = VALID_REASONING_BY_TOOL[tool]
        if reasoning not in allowed_reasoning:
            raise ValueError(
                f"{loc}.reasoning must be one of {allowed_reasoning} "
                f"for tool {tool!r}, got {reasoning!r}"
            )
    if provider is not None:
        if provider not in VALID_PROVIDERS:
            raise ValueError(
                f"{loc}.provider must be one of {VALID_PROVIDERS}, "
                f"got {provider!r}"
            )
        allowed_providers = VALID_PROVIDERS_BY_TOOL[tool]
        if provider not in allowed_providers:
            raise ValueError(
                f"{loc}.provider {provider!r} is not valid for tool "
                f"{tool!r}; allowed: {allowed_providers}"
            )
    return model, reasoning, provider


def _canonical_override_value(field: str, value: str) -> str:
    if field in ("provider", "reasoning"):
        return value.lower()
    return value


def apply_peer_field_overrides(
    cfg: dict[str, Any],
    *,
    peer_model: list[str] | tuple[str, ...] | None = None,
    peer_reasoning: list[str] | tuple[str, ...] | None = None,
    peer_provider: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Return a config copy with scaffold-time peer fields applied.

    The target key in ``name=value`` may match either a peer ``name`` or a
    peer ``tool``. If both match, the union is updated.
    """
    overrides_by_field = {
        "model": parse_peer_field_overrides(
            peer_model, flag_name="--peer-model",
        ),
        "reasoning": parse_peer_field_overrides(
            peer_reasoning, flag_name="--peer-reasoning",
        ),
        "provider": parse_peer_field_overrides(
            peer_provider, flag_name="--peer-provider",
        ),
    }
    if not any(overrides_by_field.values()):
        return copy.deepcopy(cfg)

    out = copy.deepcopy(cfg)
    raw_peers = out.get("peers")
    raw_tools = out.get("tools")
    if raw_peers is not None and raw_tools is not None:
        raise ValueError(
            "config has both `peers:` (new) and `tools:` (legacy) — "
            "use only one"
        )
    if isinstance(raw_peers, list):
        entries = [e for e in raw_peers if isinstance(e, dict)]

        def matches(entry: dict[str, Any], key: str) -> bool:
            name = entry.get("name")
            tool = entry.get("tool", name)
            return name == key or tool == key
    elif isinstance(raw_tools, dict):
        entries = [e for e in raw_tools.values() if isinstance(e, dict)]

        def matches(entry: dict[str, Any], key: str) -> bool:
            for tool_name, tool_entry in raw_tools.items():
                if tool_entry is entry:
                    return tool_name == key
            return False
    else:
        raise ValueError(
            "config has neither `peers:` (new) nor `tools:` (legacy)"
        )

    for field, overrides in overrides_by_field.items():
        for override in overrides:
            if override.key is None:
                targets = entries
            else:
                targets = [entry for entry in entries
                           if matches(entry, override.key)]
                if not targets:
                    raise ValueError(
                        f"{field} override target {override.key!r} "
                        "matches no peer name or tool"
                    )
            for entry in targets:
                entry[field] = _canonical_override_value(field, override.value)

    # Reuse the normal loader so scaffold-time validation and runtime
    # validation stay identical.
    load_peer_specs(out)
    return out


def load_peer_specs(cfg: dict[str, Any]) -> list[PeerSpec]:
    """Returns peer specs in declared order.

    Raises ValueError with a precise message on any malformed entry.
    """
    raw_peers = cfg.get("peers")
    raw_tools = cfg.get("tools")

    if raw_peers is not None and raw_tools is not None:
        raise ValueError(
            "config has both `peers:` (new) and `tools:` (legacy) — "
            "use only one"
        )

    if raw_peers is None and raw_tools is None:
        raise ValueError(
            "config has neither `peers:` (new) nor `tools:` (legacy)"
        )

    specs: list[PeerSpec] = []
    if raw_peers is not None:
        if not isinstance(raw_peers, list) or not raw_peers:
            raise ValueError("`peers` must be a non-empty list")
        seen: set[str] = set()
        for i, entry in enumerate(raw_peers):
            if not isinstance(entry, dict):
                raise ValueError(f"peers[{i}] must be a mapping")
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError(f"peers[{i}].name must be a non-empty string")
            if not _NAME_RE.match(name):
                raise ValueError(
                    f"peers[{i}].name {name!r} must match "
                    "[A-Za-z0-9][A-Za-z0-9_-]{0,31} (avoids path "
                    "traversal, shell metachars, tmux ambiguity)"
                )
            if name in RESERVED_PEER_NAMES:
                raise ValueError(
                    f"peers[{i}].name {name!r} is reserved by the "
                    "substrate; pick another name"
                )
            if name in seen:
                raise ValueError(f"peers[{i}].name {name!r} is duplicated")
            seen.add(name)
            tool = entry.get("tool", name)
            if not isinstance(tool, str) or not tool:
                raise ValueError(
                    f"peers[{i}].tool must be a non-empty string"
                )
            argv = entry.get("argv")
            if not isinstance(argv, list) or not argv:
                raise ValueError(
                    f"peers[{i}].argv must be a non-empty list of strings"
                )
            if not all(isinstance(a, str) for a in argv):
                raise ValueError(
                    f"peers[{i}].argv entries must all be strings"
                )
            prompt_mode = entry.get("prompt_mode", "stdin")
            if prompt_mode not in VALID_PROMPT_MODES:
                raise ValueError(
                    f"peers[{i}].prompt_mode must be one of "
                    f"{VALID_PROMPT_MODES}, got {prompt_mode!r}"
                )
            role = entry.get("role", "default")
            if role not in VALID_PEER_ROLES:
                raise ValueError(
                    f"peers[{i}].role must be one of "
                    f"{VALID_PEER_ROLES}, got {role!r}"
                )
            model = _optional_nonempty_string(entry, "model", f"peers[{i}]")
            reasoning = _optional_nonempty_string(
                entry, "reasoning", f"peers[{i}]",
            )
            provider = _optional_nonempty_string(
                entry, "provider", f"peers[{i}]",
            )
            model, reasoning, provider = _validate_semantic_fields(
                loc=f"peers[{i}]",
                tool=tool,
                model=model,
                reasoning=reasoning,
                provider=provider,
            )
            specs.append(PeerSpec(
                name=name, tool=tool,
                argv=tuple(argv), prompt_mode=prompt_mode,
                role=role,
                model=model, reasoning=reasoning, provider=provider,
            ))
        return specs

    # Legacy `tools:` map.
    if not isinstance(raw_tools, dict) or not raw_tools:
        raise ValueError("`tools` must be a non-empty mapping")
    for name in raw_tools:
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise ValueError(
                f"tools.{name!r} key must match "
                "[A-Za-z0-9][A-Za-z0-9_-]{0,31}"
            )
        if name in RESERVED_PEER_NAMES:
            raise ValueError(
                f"tools.{name!r} is reserved by the substrate; "
                "pick another name"
            )
        spec = raw_tools[name]
        if not isinstance(spec, dict):
            raise ValueError(f"tools.{name} must be a mapping")
        argv = spec.get("argv")
        if not isinstance(argv, list) or not argv:
            raise ValueError(
                f"tools.{name}.argv must be a non-empty list of strings"
            )
        if not all(isinstance(a, str) for a in argv):
            raise ValueError(
                f"tools.{name}.argv entries must all be strings"
            )
        prompt_mode = spec.get("prompt_mode", "stdin")
        if prompt_mode not in VALID_PROMPT_MODES:
            raise ValueError(
                f"tools.{name}.prompt_mode must be one of "
                f"{VALID_PROMPT_MODES}, got {prompt_mode!r}"
            )
        model = _optional_nonempty_string(spec, "model", f"tools.{name}")
        reasoning = _optional_nonempty_string(
            spec, "reasoning", f"tools.{name}",
        )
        provider = _optional_nonempty_string(
            spec, "provider", f"tools.{name}",
        )
        model, reasoning, provider = _validate_semantic_fields(
            loc=f"tools.{name}",
            tool=name,
            model=model,
            reasoning=reasoning,
            provider=provider,
        )
        specs.append(PeerSpec(
            name=name, tool=name,
            argv=tuple(argv), prompt_mode=prompt_mode,
            model=model, reasoning=reasoning, provider=provider,
        ))
    return specs


def peer_names(specs: list[PeerSpec]) -> list[str]:
    return [s.name for s in specs]
