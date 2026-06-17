"""Mode discovery + loading.

A "mode" is a reusable bundle of audit goals + check scripts. Modes live
either built-in (shipped with peers) or under the user's ~/.config dir.

  src/peers/templates/modes/<name>/      ← built-in
  ~/.config/peers/modes/<name>/          ← user-supplied (override built-in)

Each mode dir contains:
  mode.yaml     — {name, version, description, requires?: [other-mode]}
  goals.yaml    — hard + soft goals (peers schema)
  checks/       — optional, python check scripts referenced by goals
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from peers.safe_io import read_bytes_no_symlink, read_text_no_symlink


@dataclass
class Mode:
    name: str
    version: int
    description: str = ""
    requires: list[str] = field(default_factory=list)
    path: Path = Path()
    source: str = "builtin"     # "builtin" | "user"


def _builtin_modes_dir() -> Path:
    # This module is `peers/modes/__init__.py`, so `Path(__file__).parent`
    # is `peers/modes/`; the builtin templates live at `peers/templates/
    # modes/`, one directory up from here. (Before STEP-1 this code lived
    # in `peers/modes.py`, where a single `.parent` reached `peers/`; the
    # module->package move added a directory level, hence `.parent.parent`.)
    return (Path(__file__).parent.parent / "templates" / "modes").resolve()


def _user_modes_dir() -> Path:
    raw = os.environ.get("PEERS_MODES_DIR")
    if raw:
        return Path(raw).expanduser().resolve()
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return (Path(base).expanduser() / "peers" / "modes").resolve()


# Alias map → canonical language token used by `merge(lang=…)` when
# picking `checks/lang_<lang>/` subdirs. Kept private; the only public
# surface is normalize_lang().
_LANG_ALIASES: dict[str, str] = {
    "javascript": "js",
    "typescript": "js",
    "ts": "js",
    "golang": "go",
    "rs": "rust",
    "py": "python",
}


def normalize_lang(raw: str | None) -> str:
    """Return the canonical language token for ``raw``.

    Contract:

    * ``None`` or empty string → ``"python"`` (peers' default stack).
    * Input is lowercased before lookup, so ``"Python"`` / ``"JS"`` /
      ``"JavaScript"`` all resolve.
    * Known aliases (see ``_LANG_ALIASES``) are mapped to their
      canonical token: ``javascript`` / ``typescript`` / ``ts`` →
      ``js``; ``golang`` → ``go``; ``rs`` → ``rust``; ``py`` →
      ``python``.
    * Unknown tokens are returned lowercased but otherwise unchanged
      (e.g. ``"cobol"`` → ``"cobol"``); callers can detect the miss
      via the modes layer's lang-fallback warning.
    """
    lowered = (raw or "python").lower()
    return _LANG_ALIASES.get(lowered, lowered)


def _load_one(mode_dir: Path, source: str) -> Mode | None:
    meta_path = mode_dir / "mode.yaml"
    if not meta_path.is_file():
        return None
    try:
        meta_text = read_text_no_symlink(meta_path, max_bytes=64 * 1024)
        meta = yaml.safe_load(meta_text) or {}
    except (yaml.YAMLError, OSError) as e:
        print(f"peers.modes: {mode_dir.name}: invalid mode.yaml ({e}); "
              "skipped", file=sys.stderr)
        return None
    if not isinstance(meta, dict):
        print(f"peers.modes: {mode_dir.name}: mode.yaml top-level is not a "
              "mapping; skipped", file=sys.stderr)
        return None
    name = meta.get("name")
    if not isinstance(name, str) or not name:
        print(f"peers.modes: {mode_dir.name}: mode.yaml missing `name`; "
              "skipped", file=sys.stderr)
        return None
    version = meta.get("version", 1)
    try:
        version = int(version)
    except (TypeError, ValueError):
        version = 1
    requires = meta.get("requires") or []
    if not isinstance(requires, list):
        requires = []
    return Mode(
        name=name,
        version=version,
        description=str(meta.get("description", "")),
        requires=[str(x) for x in requires if x],
        path=mode_dir,
        source=source,
    )


def discover() -> dict[str, Mode]:
    """Find all modes; user overrides builtin on name collision."""
    out: dict[str, Mode] = {}
    bdir = _builtin_modes_dir()
    if bdir.is_dir():
        for child in sorted(bdir.iterdir()):
            if not child.is_dir():
                continue
            m = _load_one(child, source="builtin")
            if m is not None:
                out[m.name] = m
    udir = _user_modes_dir()
    if udir.is_dir():
        for child in sorted(udir.iterdir()):
            if not child.is_dir():
                continue
            m = _load_one(child, source="user")
            if m is not None:
                out[m.name] = m  # user wins
    return out


def resolve(requested: list[str]) -> list[Mode]:
    """Expand `requires:` transitively; return deps-first stable order.

    Raises ValueError on unknown mode or dependency cycle.
    """
    available = discover()
    if not requested:
        return []

    for name in requested:
        if name not in available:
            raise ValueError(
                f"unknown mode {name!r}; available: "
                f"{sorted(available)}"
            )

    visiting: set[str] = set()
    visited: set[str] = set()
    out: list[Mode] = []

    def _walk(name: str, chain: tuple[str, ...]) -> None:
        if name in visited:
            return
        if name in visiting:
            cycle = " -> ".join(chain + (name,))
            raise ValueError(f"cycle in mode requires: {cycle}")
        if name not in available:
            raise ValueError(
                f"mode {name!r} (required by {' -> '.join(chain)}) "
                f"not found; available: {sorted(available)}"
            )
        visiting.add(name)
        mode = available[name]
        for dep in mode.requires:
            _walk(dep, chain + (name,))
        visiting.discard(name)
        visited.add(name)
        out.append(mode)

    for name in requested:
        _walk(name, ())
    return out


def merge(
    resolved: list[Mode], lang: str = "python"
) -> tuple[dict, list[Path]]:
    """Merge multiple modes into (combined goals.yaml dict, check files).

    Goal-id collisions: silent dedup if structurally identical, raises
    ValueError naming both source modes otherwise.

    Check-file collisions: silent dedup if byte-identical, raises
    ValueError otherwise. Returns a list of Path objects to the
    canonical source check files (caller copies them to .peers/checks/).

    Language awareness (``lang``):

    For each mode's ``checks/`` directory:

    * If ``checks/lang_<lang>/`` exists, files from THAT subdir are
      used (replaces, doesn't union with, the top-level files of that
      mode). E.g. ``--lang=js`` + audit mode → ships
      ``checks/lang_js/*``.
    * If a mode has NO ``lang_*/`` subdirs at all, the mode is
      language-agnostic: its top-level checks are used regardless of
      ``lang`` (e.g. a security mode whose checks are not stack-
      specific).
    * If a mode has ``lang_*/`` subdirs but not ``lang_<lang>/``, fall
      back to that mode's top-level (= python defaults) and emit a
      stderr warning so the user knows the request was downgraded.
    """
    goals_by_id: dict[str, tuple[dict, str]] = {}     # id -> (goal, source_mode)
    for mode in resolved:
        gpath = mode.path / "goals.yaml"
        if not gpath.is_file():
            continue
        try:
            gtext = read_text_no_symlink(gpath, max_bytes=512 * 1024)
            doc = yaml.safe_load(gtext) or {}
        except yaml.YAMLError as e:
            raise ValueError(
                f"mode {mode.name!r}: invalid goals.yaml ({e})"
            ) from e
        for goal in (doc.get("goals") or []):
            if not isinstance(goal, dict) or "id" not in goal:
                continue
            gid = goal["id"]
            if not isinstance(gid, str) or not gid:
                continue                    # #14: a non-str/unhashable id must not
                                            # raise a raw TypeError out of `peers init`
            prior = goals_by_id.get(gid)
            if prior is None:
                goals_by_id[gid] = (goal, mode.name)
                continue
            if prior[0] == goal:
                continue  # structurally identical dedup
            raise ValueError(
                f"conflicting definition of goal {gid!r} in modes "
                f"[{prior[1]}, {mode.name}]"
            )

    merged_goals = {"goals": [g for g, _src in goals_by_id.values()]}

    check_by_name: dict[str, tuple[Path, bytes, str]] = {}
    for mode in resolved:
        cdir = mode.path / "checks"
        if not cdir.is_dir():
            continue
        # Detect lang-* subdirs so we can decide whether the mode is
        # language-agnostic (no lang_*/ at all → use top-level) or
        # whether the requested lang missed (lang_*/ exist but not
        # the requested one → warn, fall back to top-level).
        available_lang_dirs = sorted(
            p.name[len("lang_"):] for p in cdir.iterdir()
            if p.is_dir() and p.name.startswith("lang_")
        )
        lang_dir = cdir / f"lang_{lang}"
        if lang_dir.is_dir():
            source_dir = lang_dir
        else:
            # Mode HAS lang_*/ subdirs but not the requested one.
            # Suppress the warning when lang=="python" because the
            # top-level checks ARE the canonical Python implementation
            # (lang_python/ as a separate subdir is by convention not
            # shipped — python is the default lang, so top-level files
            # are already what `--lang=python` should get). The
            # "falling back to python" message there is misleading: we
            # didn't downgrade, we used the right files. For any other
            # lang the warning still fires — the user genuinely asked
            # for something this mode doesn't ship.
            if available_lang_dirs and lang != "python":
                print(
                    f"peers.modes: mode {mode.name!r} has no "
                    f"lang_{lang}/ checks "
                    f"(available: {available_lang_dirs}); "
                    "falling back to python",
                    file=sys.stderr,
                )
            source_dir = cdir
        for f in sorted(source_dir.iterdir()):
            # f.is_file() filters out the lang_*/ subdirs when
            # source_dir == cdir, and is a no-op when source_dir is
            # a lang_<lang>/ leaf (which contains only files).
            if not f.is_file():
                continue
            content = read_bytes_no_symlink(f, max_bytes=1024 * 1024)
            prior_check = check_by_name.get(f.name)
            if prior_check is None:
                check_by_name[f.name] = (f, content, mode.name)
                continue
            if prior_check[1] == content:
                continue
            raise ValueError(
                f"conflicting check file {f.name!r} in modes "
                f"[{prior_check[2]}, {mode.name}]"
            )
    checks = [p for p, _content, _src in check_by_name.values()]
    return merged_goals, checks
