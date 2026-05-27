from __future__ import annotations

import textwrap
from pathlib import Path

import pytest


def _seed_mode(root: Path, name: str, goals_yaml: str,
               checks: dict[str, str] | None = None,
               requires: list[str] | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    req = f"\nrequires: {requires}\n" if requires else ""
    (d / "mode.yaml").write_text(f"name: {name}\nversion: 1{req}")
    (d / "goals.yaml").write_text(goals_yaml)
    if checks:
        (d / "checks").mkdir(exist_ok=True)
        for fname, content in checks.items():
            (d / "checks" / fname).write_text(content)
    return d


def test_merge_dedups_structurally_identical_goal_ids(tmp_path, monkeypatch):
    from peers import modes as modes_mod
    root = tmp_path / "builtin"
    g = textwrap.dedent("""
        goals:
          - id: g1
            type: hard
            cmd: "true"
            pass_when: "exit_code == 0"
    """).lstrip()
    _seed_mode(root, "alpha", g)
    _seed_mode(root, "beta", g)
    monkeypatch.setattr(modes_mod, "_builtin_modes_dir", lambda: root)
    monkeypatch.setattr(modes_mod, "_user_modes_dir",
                        lambda: tmp_path / "no-user")
    resolved = modes_mod.resolve(["alpha", "beta"])
    merged_goals, _checks = modes_mod.merge(resolved)
    ids = [x["id"] for x in merged_goals["goals"]]
    assert ids == ["g1"]  # deduped


def test_merge_raises_on_conflicting_goal_definitions(tmp_path, monkeypatch):
    from peers import modes as modes_mod
    root = tmp_path / "builtin"
    _seed_mode(root, "alpha", textwrap.dedent("""
        goals:
          - id: g1
            type: hard
            cmd: "true"
            pass_when: "exit_code == 0"
    """))
    _seed_mode(root, "beta", textwrap.dedent("""
        goals:
          - id: g1
            type: hard
            cmd: "false"      # different!
            pass_when: "exit_code == 0"
    """))
    monkeypatch.setattr(modes_mod, "_builtin_modes_dir", lambda: root)
    monkeypatch.setattr(modes_mod, "_user_modes_dir",
                        lambda: tmp_path / "no-user")
    resolved = modes_mod.resolve(["alpha", "beta"])
    with pytest.raises(ValueError) as exc:
        modes_mod.merge(resolved)
    msg = str(exc.value)
    assert "g1" in msg and "alpha" in msg and "beta" in msg


def test_merge_collects_checks_filename_to_path_pairs(tmp_path, monkeypatch):
    from peers import modes as modes_mod
    root = tmp_path / "builtin"
    _seed_mode(root, "alpha", "goals: []\n",
               checks={"alpha_check.py": "print('a')"})
    _seed_mode(root, "beta", "goals: []\n",
               checks={"beta_check.py": "print('b')"})
    monkeypatch.setattr(modes_mod, "_builtin_modes_dir", lambda: root)
    monkeypatch.setattr(modes_mod, "_user_modes_dir",
                        lambda: tmp_path / "no-user")
    resolved = modes_mod.resolve(["alpha", "beta"])
    _goals, checks = modes_mod.merge(resolved)
    names = {p.name for p in checks}
    assert names == {"alpha_check.py", "beta_check.py"}


def test_merge_raises_on_conflicting_check_filenames(tmp_path, monkeypatch):
    from peers import modes as modes_mod
    root = tmp_path / "builtin"
    _seed_mode(root, "alpha", "goals: []\n",
               checks={"shared.py": "print('a')"})
    _seed_mode(root, "beta", "goals: []\n",
               checks={"shared.py": "print('b')"})  # different content
    monkeypatch.setattr(modes_mod, "_builtin_modes_dir", lambda: root)
    monkeypatch.setattr(modes_mod, "_user_modes_dir",
                        lambda: tmp_path / "no-user")
    resolved = modes_mod.resolve(["alpha", "beta"])
    with pytest.raises(ValueError) as exc:
        modes_mod.merge(resolved)
    msg = str(exc.value)
    assert "shared.py" in msg


def test_merge_same_check_content_in_two_modes_is_silent_dedup(tmp_path, monkeypatch):
    from peers import modes as modes_mod
    root = tmp_path / "builtin"
    _seed_mode(root, "alpha", "goals: []\n",
               checks={"shared.py": "same"})
    _seed_mode(root, "beta", "goals: []\n",
               checks={"shared.py": "same"})
    monkeypatch.setattr(modes_mod, "_builtin_modes_dir", lambda: root)
    monkeypatch.setattr(modes_mod, "_user_modes_dir",
                        lambda: tmp_path / "no-user")
    resolved = modes_mod.resolve(["alpha", "beta"])
    _goals, checks = modes_mod.merge(resolved)
    assert len(checks) == 1


def test_merge_uses_lang_specific_subdir(tmp_path, monkeypatch):
    """`lang=js` makes merge() pick checks/lang_js/ over top-level."""
    from peers import modes as modes_mod
    root = tmp_path / "builtin"
    d = root / "demo"
    (d / "checks" / "lang_js").mkdir(parents=True)
    (d / "mode.yaml").write_text("name: demo\nversion: 1\n")
    (d / "goals.yaml").write_text("goals: []\n")
    (d / "checks" / "py_top.py").write_text("# python")
    (d / "checks" / "lang_js" / "js_only.js").write_text("// js")
    monkeypatch.setattr(modes_mod, "_builtin_modes_dir", lambda: root)
    monkeypatch.setattr(modes_mod, "_user_modes_dir",
                        lambda: tmp_path / "no-user")
    resolved = modes_mod.resolve(["demo"])
    _g, checks = modes_mod.merge(resolved, lang="js")
    names = {p.name for p in checks}
    assert names == {"js_only.js"}, f"expected only js file, got {names}"


def test_merge_lang_agnostic_mode_uses_top_level_regardless(
    tmp_path, monkeypatch
):
    """A mode with no lang_*/ subdirs is language-agnostic — its top
    level is used regardless of --lang."""
    from peers import modes as modes_mod
    root = tmp_path / "builtin"
    d = root / "agno"
    (d / "checks").mkdir(parents=True)
    (d / "mode.yaml").write_text("name: agno\nversion: 1\n")
    (d / "goals.yaml").write_text("goals: []\n")
    (d / "checks" / "always.py").write_text("# always")
    monkeypatch.setattr(modes_mod, "_builtin_modes_dir", lambda: root)
    monkeypatch.setattr(modes_mod, "_user_modes_dir",
                        lambda: tmp_path / "no-user")
    resolved = modes_mod.resolve(["agno"])
    _g, checks = modes_mod.merge(resolved, lang="js")
    names = {p.name for p in checks}
    assert names == {"always.py"}


def test_merge_lang_python_silent_when_only_lang_dirs_are_others(
    tmp_path, monkeypatch, capsys,
):
    """followup: `--lang=python` against a mode whose only lang_*/
    subdirs are non-python (e.g. audit mode ships lang_go/, lang_js/,
    lang_rust/) must NOT emit a misleading 'falling back to python'
    warning — the top-level checks ARE Python, that's the canonical
    path, no downgrade happened."""
    from peers import modes as modes_mod
    root = tmp_path / "builtin"
    d = root / "demo"
    (d / "checks" / "lang_js").mkdir(parents=True)
    (d / "checks" / "lang_rust").mkdir()
    (d / "mode.yaml").write_text("name: demo\nversion: 1\n")
    (d / "goals.yaml").write_text("goals: []\n")
    (d / "checks" / "py_top.py").write_text("# python")
    (d / "checks" / "lang_js" / "js_only.js").write_text("// js")
    (d / "checks" / "lang_rust" / "rs_only.rs").write_text("// rust")
    monkeypatch.setattr(modes_mod, "_builtin_modes_dir", lambda: root)
    monkeypatch.setattr(modes_mod, "_user_modes_dir",
                        lambda: tmp_path / "no-user")
    resolved = modes_mod.resolve(["demo"])
    _g, checks = modes_mod.merge(resolved, lang="python")
    names = {p.name for p in checks}
    assert names == {"py_top.py"}  # top-level Python files used
    err = capsys.readouterr().err
    assert "falling back" not in err.lower(), (
        f"`--lang=python` should not emit a fallback warning when the "
        f"top-level checks are the canonical python implementation; "
        f"got stderr: {err!r}"
    )


def test_merge_unknown_lang_falls_back_with_warning(
    tmp_path, monkeypatch, capsys
):
    from peers import modes as modes_mod
    root = tmp_path / "builtin"
    d = root / "demo"
    (d / "checks" / "lang_js").mkdir(parents=True)
    (d / "mode.yaml").write_text("name: demo\nversion: 1\n")
    (d / "goals.yaml").write_text("goals: []\n")
    (d / "checks" / "py_top.py").write_text("# python")
    (d / "checks" / "lang_js" / "js_only.js").write_text("// js")
    monkeypatch.setattr(modes_mod, "_builtin_modes_dir", lambda: root)
    monkeypatch.setattr(modes_mod, "_user_modes_dir",
                        lambda: tmp_path / "no-user")
    resolved = modes_mod.resolve(["demo"])
    _g, checks = modes_mod.merge(resolved, lang="cobol")
    names = {p.name for p in checks}
    assert names == {"py_top.py"}  # fell back to top-level
    err = capsys.readouterr().err
    assert "lang_cobol" in err
    assert "falling back" in err.lower() or "fallback" in err.lower()
