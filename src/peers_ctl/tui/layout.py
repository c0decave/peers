"""Pure layout persistence for the TUI cockpit (Wave 1b, Unit J).

Saves/restores *which panels are visible* + best-effort *floating-window
geometry* to ``<config_dir>/tui-layout.json`` so the operator's mission-control
arrangement survives a relaunch. This module is intentionally **textual-free**
(default-python testable): the Textual app imports it and applies the result.

Three pure entry points:
  * :func:`default_layout` — a fresh copy of the mission-control default.
  * :func:`load_layout` — read + merge-onto-defaults; **fail-soft** (missing /
    corrupt / oversized / wrong-shape / symlinked -> the default layout, never
    raises — a bad layout file must never block launch).
  * :func:`save_layout` — sanitize to the known schema + atomically persist via
    ``peers.safe_io`` (no-symlink, parent created); **fail-soft** (a quit must
    never crash because the layout couldn't be written).

Schema (versioned, forward/garbage-tolerant)::

    {"visible": {"<panel-id>": bool, ...},
     "windows": {"<panel-id>": {"x": int, "y": int, "w": int, "h": int}, ...}}

Unknown panel ids and unknown top-level keys are dropped on both read and write
(defense in depth: a forward/garbage file can't inject junk UI state). The file
is user-editable, so nothing in it is ever trusted — every value is type-checked
and falls back to the default on the slightest mismatch.
"""
from __future__ import annotations

import json
from pathlib import Path

from peers import safe_io

#: hard cap on the layout file (it is tiny — a handful of panels). An oversized
#: file is rejected (the truncated read would corrupt the JSON anyway) -> default.
_LAYOUT_MAX = 64 * 1024

#: the canonical default visibility of every known cockpit panel. This is the
#: single source of truth the app's ``PANEL_SPECS`` visibility mirrors; keep them
#: in lockstep. The mission-control set (Gates/Peers/Tasks/Ticks/Budget/Live) is
#: visible; everything else (loop extras + the forward-looking autonomy windows)
#: starts hidden and toggles on via the WindowBar.
_DEFAULT_VISIBLE: dict[str, bool] = {
    # mission-control set (visible by default)
    "gates-panel": True,
    "peers-panel": True,
    "tasks-panel": True,
    "ticks-panel": True,
    "budget-panel": True,
    "live-panel": True,
    # loop-layer extras (hidden by default)
    "bugs-panel": False,
    "review-panel": False,
    "log-panel": False,
    "diff-panel": False,
    # forward-looking autonomy windows (hidden by default)
    "autonomy-ledger-panel": False,
    "spine-gates-panel": False,
    "propagations-panel": False,
    "autonomy-feed-panel": False,
    "escalation-panel": False,
}

#: the geometry keys a persisted floating-window position carries.
_GEOM_KEYS = ("x", "y", "w", "h")


def known_panel_ids() -> tuple[str, ...]:
    """Every panel id the layout schema knows about (the app cross-checks this)."""
    return tuple(_DEFAULT_VISIBLE.keys())


def default_layout() -> dict:
    """A fresh (deeply-copied) mission-control default layout."""
    return {
        "visible": dict(_DEFAULT_VISIBLE),
        "windows": {},
    }


def default_layout_path(config_dir: Path | str | None = None) -> Path:
    """Resolve ``<config_dir>/tui-layout.json``.

    ``config_dir`` defaults to the same dir the rest of peers_ctl uses
    (``XDG_CONFIG_HOME/peers-ctl`` else ``~/.config/peers-ctl``) so the layout
    sits next to ``projects.yaml``.
    """
    if config_dir is not None:
        base = Path(config_dir)
    else:
        try:
            from peers_ctl.store import default_config_dir
            base = default_config_dir()
        except Exception:
            import os
            xdg = os.environ.get("XDG_CONFIG_HOME")
            base = Path(xdg) / "peers-ctl" if xdg else Path.home() / ".config" / "peers-ctl"
    return base / "tui-layout.json"


def _sanitize_visible(raw: object, defaults: dict) -> dict:
    """Keep only KNOWN panel ids with a BOOL value; everything else -> default."""
    out = dict(defaults.get("visible", _DEFAULT_VISIBLE))
    if isinstance(raw, dict):
        for pid, val in raw.items():
            if pid in out and isinstance(val, bool):
                out[pid] = val
    return out


def _sanitize_windows(raw: object) -> dict:
    """Keep only KNOWN panel ids whose geometry is 4 ints; drop the rest."""
    out: dict[str, dict] = {}
    if not isinstance(raw, dict):
        return out
    for pid, geom in raw.items():
        if pid not in _DEFAULT_VISIBLE:
            continue
        if not isinstance(geom, dict):
            continue
        if not all(isinstance(geom.get(k), int) and not isinstance(geom.get(k), bool)
                   for k in _GEOM_KEYS):
            continue
        out[pid] = {k: int(geom[k]) for k in _GEOM_KEYS}
    return out


def merge_with_defaults(loaded: object, defaults: dict) -> dict:
    """Merge a (possibly partial / garbage) loaded layout onto ``defaults``.

    Pure + total: a non-dict ``loaded`` -> a copy of ``defaults``. Otherwise the
    known schema is sanitized (unknown panels + keys dropped, bad value types
    fall back to the default), so the result is always a well-formed layout.
    """
    if not isinstance(loaded, dict):
        return {"visible": dict(defaults.get("visible", _DEFAULT_VISIBLE)),
                "windows": dict(defaults.get("windows", {}))}
    return {
        "visible": _sanitize_visible(loaded.get("visible"), defaults),
        "windows": _sanitize_windows(loaded.get("windows")),
    }


def load_layout(path: Path | str) -> dict:
    """Load + merge-onto-defaults a layout file. FAIL-SOFT -> default on anything.

    Missing / corrupt / non-object / oversized / symlinked -> :func:`default_layout`.
    """
    defaults = default_layout()
    p = Path(path)
    try:
        if not p.exists():
            return defaults
    except OSError:
        return defaults
    try:
        text = safe_io.read_text_no_symlink(p, max_bytes=_LAYOUT_MAX)
    except (OSError, ValueError):
        return defaults
    # oversize guard: read one extra byte's worth and reject if at/over the cap
    # (a truncated read would otherwise yield invalid JSON -> already handled,
    # but reject explicitly so a valid-JSON prefix can't slip through).
    if len(text.encode("utf-8", "ignore")) >= _LAYOUT_MAX:
        return defaults
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return defaults
    return merge_with_defaults(data, defaults)


def save_layout(path: Path | str, state: object) -> None:
    """Sanitize ``state`` to the known schema + atomically persist it. FAIL-SOFT.

    Creates the parent config dir if needed. Any error (un-writable path, a file
    where a dir should be, etc.) is swallowed — failing to persist the layout
    must never crash a quit.
    """
    p = Path(path)
    sanitized = merge_with_defaults(state, default_layout())
    text = json.dumps(sanitized, indent=2, sort_keys=True)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    try:
        safe_io.atomic_write_text_in_dir_no_symlink(p, text)
    except (OSError, ValueError):
        return
