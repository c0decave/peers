from __future__ import annotations

import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"


def _mypy_excludes() -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    raw = data.get("tool", {}).get("mypy", {}).get("exclude", [])
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return raw
    return []


def _excluded(path: str) -> bool:
    return any(re.search(pattern, path) for pattern in _mypy_excludes())


def _override_modules_with_ignore_missing() -> set[str]:
    """Module globs that have ``ignore_missing_imports = true`` set via a
    ``[[tool.mypy.overrides]]`` table."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    overrides = data.get("tool", {}).get("mypy", {}).get("overrides", [])
    mods: set[str] = set()
    for ov in overrides if isinstance(overrides, list) else []:
        if not ov.get("ignore_missing_imports"):
            continue
        mod = ov.get("module", [])
        for m in ([mod] if isinstance(mod, str) else mod):
            mods.add(m)
    return mods


def test_happy_mypy_config_declares_template_check_exclude() -> None:
    assert _mypy_excludes(), "type-clean needs a template-check exclude"


def test_edge_mypy_config_matches_hyphenated_mode_check_paths() -> None:
    # Generic hyphenated mode-dir examples: the exclude is a ``.*`` regex over
    # any modes/<dir>/checks/convergence_reached.py, so the concrete dir name is
    # irrelevant to what is under test (and naming a private mode here is not).
    assert _excluded(
        "src/peers/templates/modes/some-mode/checks/convergence_reached.py"
    )
    assert _excluded(
        "src/peers/templates/modes/another-mode/checks/convergence_reached.py"
    )


def test_sad_mypy_config_does_not_exclude_runtime_src_modules() -> None:
    assert not _excluded("src/auth_proxy/server.py")
    assert not _excluded("src/peers/cli.py")
    assert not _excluded("src/peers_ctl/cli.py")


def test_happy_textual_optional_dep_ignores_missing_imports() -> None:
    # The peers[tui] extra (Textual) is not installed in the .[dev] type-check
    # env; its imports must be ignore_missing_imports, not import-not-found noise.
    mods = _override_modules_with_ignore_missing()
    assert "textual.*" in mods
    assert "textual_window" in mods or "textual_window.*" in mods


def test_sad_override_does_not_silence_first_party_packages() -> None:
    # ignore_missing_imports must stay scoped to the optional GUI dep — silencing
    # peers/peers_ctl/auth_proxy would hide real first-party type breakage.
    mods = _override_modules_with_ignore_missing()
    for first_party in ("peers", "peers.*", "peers_ctl", "peers_ctl.*", "auth_proxy"):
        assert first_party not in mods
