"""Persistent JSON-backed state for a peers loop.

Schema v2:

    state.json = {
      "schema_version": 2,
      "iteration": int,
      "peer_order":  ["claude", "codex"],   # n>=2 order
      "turn_index":  int,                   # 0..len(peer_order)-1
      "budget": { ... },
      "goals_status": { ... },
      "stuck_counter": { ... },
      "peers": {                            # renamed from "tools"
        "<name>": {
          "state": "healthy" | "degraded" | "halted",
          "consecutive_fails": int,
          "recent_fails": int,
          "recent_runs": [bool, ...],
          "last_run": { ... } | absent,
        },
        ...
      },
      "warnings": [...],
      ...
    }

Migration: v1 state (with `whose_turn` and `tools.{claude,codex}`) is
auto-detected and rewritten to v2 in-memory; the migrated state is
saved on the next StateStore.save(). A `state.json.pre-migration`
backup is written the first time a v1 state is migrated.
"""
from __future__ import annotations

import copy
import fcntl
import json
from pathlib import Path
from typing import Any, Iterable

from peers.safe_io import (
    atomic_write_text_in_dir_no_symlink,
    open_text_no_symlink,
    read_bytes_no_symlink,
)

SCHEMA_VERSION = 2
_STATE_FILE_MAX_BYTES = 5 * 1024 * 1024

_VALID_PEER_STATES = ("healthy", "degraded", "halted", "unavailable")

# Default peers when no `peer_order` is provided (e.g. first load against
# a brand-new .peers/ before a config has been parsed). The driver is
# responsible for normalising peer_order to match the active config.
DEFAULT_PEER_ORDER = ["claude", "codex"]


def _default_peer_health() -> dict[str, Any]:
    return {
        "state": "healthy",
        "consecutive_fails": 0,
        "recent_fails": 0,
        "recent_runs": [],
    }


def _build_default_state(peer_order: Iterable[str] | None = None
                         ) -> dict[str, Any]:
    order = list(peer_order) if peer_order is not None else list(
        DEFAULT_PEER_ORDER
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "iteration": 0,
        "peer_order": order,
        "turn_index": 0,
        "budget": {
            "max_iterations": 200,
            "max_runtime_s": 6 * 3600,
            "max_consecutive_failures": 5,
            "spent_iterations": 0,
            "spent_runtime_s": 0,
            "wasted_runtime_s": 0,
            "consecutive_failures": 0,
            "spent_tokens": 0,
            "spent_usd": 0.0,
            "max_tokens": None,
            "max_usd": None,
        },
        "goals_status": {},
        "stuck_counter": {},
        # thorough mode. Counts consecutive ticks that landed
        # WITHOUT a new crit/high/med Bug-Report or weak-fix/shallow-fix
        # flag-bug. Read by `convergence_reached.py` hard gate; default
        # 0 (additive field, backward-compatible — no schema bump).
        "consecutive_clean_ticks": 0,
        "peers": {name: _default_peer_health() for name in order},
    }


# Public default (n=2 claude+codex). Tests and external readers see the
# new shape directly.
DEFAULT_STATE: dict[str, Any] = _build_default_state()


def _migrate_v1(loaded: dict[str, Any]) -> dict[str, Any]:
    """Convert legacy v1 state to v2 in-memory.

    v1 markers: top-level `whose_turn` (str), `tools` map keyed by
    canonical peer names. v2 uses `turn_index`+`peer_order`, `peers`
    map keyed by configured peer names.
    """
    out = copy.deepcopy(loaded)
    legacy_tools = out.pop("tools", None)
    whose = out.pop("whose_turn", None)
    if legacy_tools is not None and isinstance(legacy_tools, dict):
        order = list(legacy_tools.keys())
    else:
        order = list(DEFAULT_PEER_ORDER)
    # Stable ordering: prefer the canonical claude/codex order if present.
    canonical = [n for n in DEFAULT_PEER_ORDER if n in order]
    extras = [n for n in order if n not in DEFAULT_PEER_ORDER]
    order = canonical + extras
    out["peer_order"] = order
    if isinstance(whose, str) and whose in order:
        out["turn_index"] = order.index(whose)
    else:
        out["turn_index"] = 0
    peers_map: dict[str, Any] = {}
    for name in order:
        legacy_entry = (legacy_tools or {}).get(name) or {}
        merged = _default_peer_health()
        # Preserve all known fields if present in legacy state.
        for k in (
            "state", "consecutive_fails", "recent_fails",
            "recent_runs", "last_run",
        ):
            if k in legacy_entry:
                merged[k] = legacy_entry[k]
        peers_map[name] = merged
    out["peers"] = peers_map
    # additive field; migrated v1 states get the default 0.
    out.setdefault("consecutive_clean_ticks", 0)
    out["schema_version"] = SCHEMA_VERSION
    return out


def _validate_state(state: dict[str, Any], path: Path) -> None:
    if state.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError(
            f"state file corrupt: {path}: schema_version must be "
            f"{SCHEMA_VERSION}, got {state.get('schema_version')!r}"
        )
    order = state.get("peer_order")
    if not isinstance(order, list) or not order:
        raise RuntimeError(
            f"state file corrupt: {path}: peer_order must be a non-empty list"
        )
    if not all(isinstance(n, str) and n for n in order):
        raise RuntimeError(
            f"state file corrupt: {path}: peer_order entries must be "
            "non-empty strings"
        )
    # M3: duplicates would break TurnManager (rotation lands on the
    # same peer twice; "the other peer" becomes self).
    if len(set(order)) != len(order):
        raise RuntimeError(
            f"state file corrupt: {path}: peer_order has duplicate "
            f"entries: {order}"
        )
    ti = state.get("turn_index")
    if not isinstance(ti, int) or not (0 <= ti < len(order)):
        raise RuntimeError(
            f"state file corrupt: {path}: turn_index must be int in "
            f"[0, {len(order)}), got {ti!r}"
        )
    # convergence counter. Optional (default 0 from _build_
    # default_state); enforce shape when present so a corrupted /
    # hand-edited value can't crash the gate script.
    cct = state.get("consecutive_clean_ticks", 0)
    if not isinstance(cct, int) or isinstance(cct, bool) or cct < 0:
        raise RuntimeError(
            f"state file corrupt: {path}: consecutive_clean_ticks must "
            f"be a non-negative integer, got {cct!r}"
        )
    peers = state.get("peers", {})
    if not isinstance(peers, dict):
        raise RuntimeError(
            f"state file corrupt: {path}: peers must be a mapping"
        )
    for name in order:
        if name not in peers:
            raise RuntimeError(
                f"state file corrupt: {path}: peers.{name} is missing"
            )
        if not isinstance(peers[name], dict):
            raise RuntimeError(
                f"state file corrupt: {path}: peers.{name} must be a mapping"
            )
        ts = peers[name].get("state", "healthy")
        if ts not in _VALID_PEER_STATES:
            raise RuntimeError(
                f"state file corrupt: {path}: peers.{name}.state must be "
                f"one of {_VALID_PEER_STATES}, got {ts!r}"
            )


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of `base` with `over` merged on top.

    For nested dicts the merge recurses; otherwise `over` wins.
    """
    result = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = copy.deepcopy(v)
    return result


def _looks_like_v1(loaded: dict[str, Any]) -> bool:
    if loaded.get("schema_version") == SCHEMA_VERSION:
        return False
    if "whose_turn" in loaded:
        return True
    if "tools" in loaded and isinstance(loaded.get("tools"), dict):
        # Treat presence of tools without peers as v1.
        if "peers" not in loaded:
            return True
    return False


class StateStore:
    def __init__(self, path: Path,
                 peer_order: Iterable[str] | None = None) -> None:
        self.path = Path(path)
        # Optional config-derived peer order. When provided, missing-key
        # loads return a default state shaped to the configured peers.
        self._cfg_peer_order: list[str] | None = (
            list(peer_order) if peer_order is not None else None
        )

    def _empty_default(self) -> dict[str, Any]:
        if self._cfg_peer_order is not None:
            return _build_default_state(self._cfg_peer_order)
        return copy.deepcopy(DEFAULT_STATE)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty_default()
        try:
            data = read_bytes_no_symlink(
                self.path, max_bytes=_STATE_FILE_MAX_BYTES + 1
            )
            if len(data) > _STATE_FILE_MAX_BYTES:
                raise RuntimeError(
                    f"state file too large: {self.path}: "
                    f"max {_STATE_FILE_MAX_BYTES} bytes"
                )
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as e:
                raise RuntimeError(
                    f"state file corrupt: {self.path}: invalid UTF-8: {e}"
                ) from e
            loaded = json.loads(text)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"state file corrupt: {self.path}: {e}"
            ) from e
        if not isinstance(loaded, dict):
            raise RuntimeError(
                f"state file corrupt: {self.path}: top-level value is not "
                f"an object ({type(loaded).__name__})"
            )

        # Migration: v1 → v2.
        migrated = False
        if _looks_like_v1(loaded):
            backup = self.path.with_suffix(self.path.suffix
                                           + ".pre-migration")
            if backup.is_symlink():
                raise RuntimeError(
                    f"state file {self.path}: refusing to migrate "
                    f"with symlinked backup path ({backup}). Remove it "
                    f"manually and retry."
                )
            if not backup.exists():
                try:
                    # Same parent-no-follow durable write as save():
                    # refuse a symlinked/swapped parent before writing the
                    # backup, not just a symlinked leaf (the is_symlink check
                    # above only covers the leaf).
                    atomic_write_text_in_dir_no_symlink(
                        backup,
                        json.dumps(loaded, indent=2, sort_keys=True),
                    )
                except OSError as e:
                    # Refuse to migrate without a backup. Without it,
                    # a bad migration would have NO rollback path
                    # for the user.
                    raise RuntimeError(
                        f"state file {self.path}: refusing to migrate "
                        f"v1 → v2 without a backup ({backup}): {e}. "
                        f"Check disk space / filesystem permissions."
                    ) from e
            loaded = _migrate_v1(loaded)
            migrated = True

        base = self._empty_default()
        # The defaults' peer_order may not match the loaded order; we want
        # the loaded order to win. Merge non-destructively.
        state = _deep_merge(base, loaded)

        # If the merge left `peer_order` empty (shouldn't, given defaults),
        # restore from defaults.
        order = state.get("peer_order")
        if not order:
            state["peer_order"] = base["peer_order"]
            state["turn_index"] = 0
            order = state["peer_order"]

        # Ensure a peers-health entry exists for every configured peer.
        peers = state.get("peers")
        if (
            isinstance(order, list)
            and all(isinstance(name, str) and name for name in order)
            and isinstance(peers, dict)
        ):
            for name in order:
                if name not in peers:
                    peers[name] = _default_peer_health()

        _validate_state(state, self.path)
        # Tag once so callers can know if a backup was just written.
        if migrated:
            state.setdefault("_migrated_from_v1", True)
        return state

    def save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Strip ephemeral marker keys from on-disk persistence.
        to_write = {k: v for k, v in state.items()
                    if not k.startswith("_")}
        to_write.setdefault("schema_version", SCHEMA_VERSION)
        # M6: validate before writing. Refuse to persist corrupt
        # state — otherwise the user can't run peers at all next
        # time without manually editing the file.
        _validate_state(to_write, self.path)
        # BUG-118/119/114: the whole tmp-write → atomic replace → dir-fsync
        # runs relative to a no-follow (dev/ino-rechecked) parent dir_fd, so a
        # symlinked/swapped `.peers/` parent is refused BEFORE any state bytes
        # are written (not just at the durability fsync), and the temp open
        # delays truncation past the nlink check.
        atomic_write_text_in_dir_no_symlink(
            self.path, json.dumps(to_write, indent=2, sort_keys=True),
        )


# --- helpers shared with TurnManager / driver -----------------------------

def release_run_lock(peers_dir: Path) -> None:
    """Remove a stale or just-released ``.peers/run.lock`` file.

    The active lock is the kernel flock held by the running process; once
    that process exits and unlocks, leaving the file behind only confuses
    operators. Refuse to unlink symlinks so a peer cannot trick substrate
    cleanup into deleting an arbitrary path.
    """
    lock = Path(peers_dir) / "run.lock"
    try:
        if lock.is_symlink():
            return
        with open_text_no_symlink(lock, "a") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            try:
                lock.unlink()
            finally:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
    except FileNotFoundError:
        return
    except OSError:
        return

def current_peer_name(state: dict[str, Any]) -> str:
    return state["peer_order"][state["turn_index"]]


def other_peer_names(state: dict[str, Any]) -> list[str]:
    """All peers except the current one, in declared order."""
    cur = current_peer_name(state)
    return [n for n in state["peer_order"] if n != cur]
