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
            specs.append(PeerSpec(
                name=name, tool=tool,
                argv=tuple(argv), prompt_mode=prompt_mode,
                role=role,
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
        specs.append(PeerSpec(
            name=name, tool=name,
            argv=tuple(argv), prompt_mode=prompt_mode,
        ))
    return specs


def peer_names(specs: list[PeerSpec]) -> list[str]:
    return [s.name for s in specs]
