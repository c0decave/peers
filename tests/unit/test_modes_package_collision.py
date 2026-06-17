"""STEP-1 (Concern 1): `src/peers/modes.py` must become
`src/peers/modes/__init__.py` so the Stage-3 engine sub-packages
(`find_bugs_reproduce/` and siblings) can live under `peers/modes/`
without Python shadowing the public modes surface.

Why this is load-bearing: Python cannot resolve both `peers/modes.py`
and `peers/modes/__init__.py` in the same directory — a `modes/`
package directory shadows the `modes.py` module, so the moment STEP-2
creates `peers/modes/find_bugs_reproduce/`, every `from peers.modes
import discover` would import an (empty) package `__init__.py` and break.
The fix is to fold the module body into `modes/__init__.py`, preserving
the public surface byte-for-byte except the one `__file__`-relative path
(`_builtin_modes_dir`) that must climb an extra parent now that the file
sits one directory deeper.

Three case classes:

  * happy — the full public surface imports and `discover()` /
    `normalize_lang()` still behave;
  * edge  — `_builtin_modes_dir()` still resolves to an existing
    `templates/modes` directory despite `__file__` moving one level
    deeper (the regression this conversion is most likely to introduce);
  * sad   — the legacy `modes.py` module file is gone (so there is no
    module/package shadow), and `resolve()` still raises on an unknown
    mode (a preserved error path, not silently swallowed).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml


def _modes_module():
    import peers.modes as m

    return m


def _peers_pkg_dir() -> Path:
    import peers

    return Path(peers.__file__).resolve().parent


def _make_mode(
    root: Path,
    name: str,
    *,
    goals: list[dict] | None = None,
    checks: dict[str, str] | None = None,
    lang_checks: dict[str, dict[str, str]] | None = None,
):
    """Build a real on-disk mode dir and return its ``Mode`` object.

    ``merge()`` reads ``goals.yaml`` + ``checks/`` (and ``checks/lang_*/``)
    straight off ``Mode.path``, so the only honest way to exercise it is a
    real directory tree — no mock substitutes for the filesystem walk.
    """
    from peers.modes import Mode

    mdir = root / name
    mdir.mkdir(parents=True, exist_ok=True)
    if goals is not None:
        (mdir / "goals.yaml").write_text(yaml.safe_dump({"goals": goals}))
    if checks:
        cdir = mdir / "checks"
        cdir.mkdir(exist_ok=True)
        for fname, content in checks.items():
            (cdir / fname).write_text(content)
    if lang_checks:
        cdir = mdir / "checks"
        cdir.mkdir(exist_ok=True)
        for lang, files in lang_checks.items():
            ldir = cdir / f"lang_{lang}"
            ldir.mkdir(exist_ok=True)
            for fname, content in files.items():
                (ldir / fname).write_text(content)
    return Mode(name=name, version=1, path=mdir)


# --- happy ---------------------------------------------------------------


def test_public_surface_is_importable():
    """Every name peers/peers_ctl import from peers.modes still resolves."""
    from peers.modes import (  # noqa: F401
        Mode,
        _builtin_modes_dir,
        discover,
        merge,
        normalize_lang,
        resolve,
    )

    # Mode is still the dataclass with the documented fields.
    fields = Mode("x", 1).__dataclass_fields__  # type: ignore[attr-defined]
    for name in ("name", "version", "description", "requires", "path", "source"):
        assert name in fields, f"Mode lost field {name!r}"


def test_normalize_lang_behavior_preserved():
    """The pure-function behavior must survive the module->package move."""
    from peers.modes import normalize_lang

    assert normalize_lang(None) == "python"
    assert normalize_lang("") == "python"
    assert normalize_lang("Python") == "python"
    assert normalize_lang("JavaScript") == "js"
    assert normalize_lang("TS") == "js"
    assert normalize_lang("golang") == "go"
    # Unknown token is lowercased and passed through unchanged.
    assert normalize_lang("Cobol") == "cobol"


def test_discover_still_finds_builtin_modes():
    """discover() must still enumerate the shipped builtin modes."""
    from peers.modes import discover

    modes = discover()
    assert isinstance(modes, dict)
    # `audit` is a long-standing builtin; its absence means the
    # templates/modes path resolution broke during the move.
    assert "audit" in modes, f"builtin modes missing; got {sorted(modes)}"
    assert modes["audit"].source == "builtin"


# --- edge ----------------------------------------------------------------


def test_modes_is_a_package_not_a_module():
    """After STEP-1 `peers.modes` must be a package so sub-packages fit."""
    m = _modes_module()
    assert hasattr(m, "__path__"), "peers.modes is still a plain module"
    f = Path(m.__file__).resolve()
    assert f.name == "__init__.py", f"expected modes/__init__.py, got {f}"
    assert f.parent.name == "modes", f"package dir is not named modes/: {f}"


def test_builtin_modes_dir_resolves_after_deeper_file():
    """`_builtin_modes_dir()` must still point at the real templates/modes.

    This is the regression the move is most likely to introduce: the
    function uses `Path(__file__).parent`, and `__file__` moves from
    `peers/modes.py` to `peers/modes/__init__.py`, one level deeper. The
    parent-walk must be corrected so the returned path is the package's
    `templates/modes`, not the now-nonexistent `peers/modes/templates/modes`.
    """
    from peers.modes import _builtin_modes_dir

    d = _builtin_modes_dir()
    assert d.is_dir(), f"_builtin_modes_dir() does not exist: {d}"
    assert d.parts[-2:] == ("templates", "modes"), f"unexpected tail: {d}"
    # It must be the package's own templates/modes, i.e. NOT nested under
    # the modes package directory (the classic off-by-one-parent bug).
    assert "modes/templates" not in d.as_posix(), (
        f"_builtin_modes_dir resolved under the modes package: {d}"
    )
    assert (d / "audit").is_dir(), f"audit builtin missing under {d}"


# --- sad -----------------------------------------------------------------


def test_legacy_modes_module_file_is_gone():
    """A leftover `peers/modes.py` next to the package is an import landmine.

    With both present, CPython resolves the package and silently drops the
    module — so a stale `modes.py` carrying the real surface would be
    shadowed and its later edits would have no effect. The conversion must
    DELETE the module file, not leave it beside the package.
    """
    legacy = _peers_pkg_dir() / "modes.py"
    assert not legacy.exists(), (
        f"legacy module {legacy} still exists beside the modes/ package — "
        "it shadows/conflicts with peers/modes/__init__.py"
    )


def test_resolve_rejects_unknown_mode():
    """resolve() must still fail closed on an unknown mode name."""
    from peers.modes import resolve

    with pytest.raises(ValueError):
        resolve(["this-mode-does-not-exist-zzz"])


def test_resolve_empty_request_is_empty():
    """Edge of the resolve contract: no requested modes -> no resolution."""
    from peers.modes import resolve

    assert resolve([]) == []


# --- merge(): the third pillar of the preserved surface -------------------
#
# STEP-1 promises the WHOLE modes public surface survives the module->package
# move. `discover`/`resolve`/`normalize_lang`/`Mode`/`_builtin_modes_dir` are
# pinned above; `merge()` was the one exported entry point with no test in
# this file. These are characterization tests against the *existing* merge()
# behavior (the package move must not perturb it) and they exercise every
# branch a real caller hits: goal union, structural dedup, both collision
# raises, lang-subdir selection, and the lang-fallback warning.


def test_merge_combines_modes_into_valid_goal_stack(tmp_path):
    """happy: distinct goals from several modes union; checks collect."""
    from peers.modes import merge

    a = _make_mode(
        tmp_path, "alpha",
        goals=[{"id": "g_a", "kind": "soft"}],
        checks={"check_a.py": "print('a')\n"},
    )
    b = _make_mode(
        tmp_path, "beta",
        goals=[{"id": "g_b", "kind": "hard"}],
        checks={"check_b.py": "print('b')\n"},
    )
    merged, checks = merge([a, b], lang="python")

    assert {g["id"] for g in merged["goals"]} == {"g_a", "g_b"}
    assert {p.name for p in checks} == {"check_a.py", "check_b.py"}


def test_merge_reads_lang_subdir_checks_when_present(tmp_path):
    """happy: a checks/lang_<lang>/ subdir REPLACES the mode's top-level checks."""
    from peers.modes import merge

    m = _make_mode(
        tmp_path, "poly",
        goals=[{"id": "g", "kind": "soft"}],
        checks={"py_default.py": "# python\n"},
        lang_checks={"js": {"js_only.py": "// js\n"}},
    )
    _merged, checks = merge([m], lang="js")

    names = {p.name for p in checks}
    assert names == {"js_only.py"}, f"lang_js must replace top-level, got {names}"


def test_merge_empty_resolved_returns_empty_goal_set(tmp_path):
    """edge: merge([]) is the empty identity — no goals, no checks, no crash."""
    from peers.modes import merge

    merged, checks = merge([], lang="python")

    assert merged == {"goals": []}
    assert checks == []


def test_merge_deduplicates_identical_duplicate_goal(tmp_path):
    """edge: a byte-identical goal id across two modes dedups silently."""
    from peers.modes import merge

    goal = {"id": "shared", "kind": "hard", "weight": 2}
    a = _make_mode(tmp_path, "a", goals=[goal])
    b = _make_mode(tmp_path, "b", goals=[dict(goal)])
    merged, _checks = merge([a, b], lang="python")

    shared = [g for g in merged["goals"] if g["id"] == "shared"]
    assert len(shared) == 1, "structurally identical goal id must dedup to one"


def test_merge_rejects_conflicting_goal_ids(tmp_path):
    """sad: same goal id with different bodies -> ValueError naming both modes."""
    from peers.modes import merge

    a = _make_mode(tmp_path, "a", goals=[{"id": "dup", "weight": 1}])
    b = _make_mode(tmp_path, "b", goals=[{"id": "dup", "weight": 9}])

    with pytest.raises(ValueError, match="dup"):
        merge([a, b], lang="python")


def test_merge_rejects_conflicting_check_files(tmp_path):
    """sad: same check filename with different bytes -> ValueError."""
    from peers.modes import merge

    a = _make_mode(
        tmp_path, "a", goals=[{"id": "ga"}], checks={"shared.py": "print(1)\n"}
    )
    b = _make_mode(
        tmp_path, "b", goals=[{"id": "gb"}], checks={"shared.py": "print(2)\n"}
    )

    with pytest.raises(ValueError, match="shared"):
        merge([a, b], lang="python")


def test_merge_falls_back_to_python_on_missing_lang_subdir(tmp_path, capsys):
    """sad: a mode with lang_*/ subdirs but not the requested one warns + uses top-level."""
    from peers.modes import merge

    m = _make_mode(
        tmp_path, "poly",
        goals=[{"id": "g"}],
        checks={"top.py": "# top\n"},
        lang_checks={"go": {"go_only.py": "// go\n"}},
    )
    _merged, checks = merge([m], lang="js")

    assert {p.name for p in checks} == {"top.py"}, "missing lang_js must fall back"
    err = capsys.readouterr().err
    assert "falling back to python" in err
    assert "poly" in err


def test_merge_skips_non_string_goal_id(tmp_path):
    """sad: a non-string/unhashable goal id is skipped, not raised.

    The full-depth-analysis #14 fix (a list/unhashable ``id`` must not raise a
    raw TypeError out of ``peers init``) lives in ``modes.py`` on ``main``; the
    Stage-3 module->package move must PRESERVE it (this tree lands atop main).
    """
    from peers.modes import merge

    m = _make_mode(tmp_path, "m", goals=[{"id": ["a", "b"]}, {"id": "real"}])

    doc, _checks = merge([m])  # must NOT raise TypeError on the unhashable id

    goals = doc.get("goals", [])
    ids = {g.get("id") for g in goals if isinstance(g, dict) and isinstance(g.get("id"), str)}
    assert "real" in ids                       # the good goal survives
    assert ["a", "b"] not in [g.get("id") for g in goals]  # the bad one skipped
