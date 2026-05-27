from __future__ import annotations

from pathlib import Path

import pytest


def _make_mode(tmp_path: Path, name: str, requires: list[str] | None = None) -> Path:
    d = tmp_path / "builtin" / name
    d.mkdir(parents=True, exist_ok=True)
    req = f"\nrequires: {requires}\n" if requires else ""
    (d / "mode.yaml").write_text(f"name: {name}\nversion: 1{req}")
    (d / "goals.yaml").write_text("goals: []\n")
    return d


def test_resolve_single_mode(tmp_path, monkeypatch):
    from peers import modes as modes_mod
    _make_mode(tmp_path, "alpha")
    monkeypatch.setattr(modes_mod, "_builtin_modes_dir",
                        lambda: tmp_path / "builtin")
    monkeypatch.setattr(modes_mod, "_user_modes_dir",
                        lambda: tmp_path / "no-user")
    out = modes_mod.resolve(["alpha"])
    assert [m.name for m in out] == ["alpha"]


def test_resolve_pulls_in_requires(tmp_path, monkeypatch):
    """B requires A -> resolve([B]) returns [A, B] (deps first)."""
    from peers import modes as modes_mod
    _make_mode(tmp_path, "alpha")
    _make_mode(tmp_path, "beta", requires=["alpha"])
    monkeypatch.setattr(modes_mod, "_builtin_modes_dir",
                        lambda: tmp_path / "builtin")
    monkeypatch.setattr(modes_mod, "_user_modes_dir",
                        lambda: tmp_path / "no-user")
    out = modes_mod.resolve(["beta"])
    assert [m.name for m in out] == ["alpha", "beta"]


def test_resolve_dedups_when_dep_also_in_explicit_list(tmp_path, monkeypatch):
    from peers import modes as modes_mod
    _make_mode(tmp_path, "alpha")
    _make_mode(tmp_path, "beta", requires=["alpha"])
    monkeypatch.setattr(modes_mod, "_builtin_modes_dir",
                        lambda: tmp_path / "builtin")
    monkeypatch.setattr(modes_mod, "_user_modes_dir",
                        lambda: tmp_path / "no-user")
    out = modes_mod.resolve(["alpha", "beta"])
    assert [m.name for m in out] == ["alpha", "beta"]


def test_resolve_unknown_mode_raises_with_available_list(tmp_path, monkeypatch):
    from peers import modes as modes_mod
    _make_mode(tmp_path, "alpha")
    monkeypatch.setattr(modes_mod, "_builtin_modes_dir",
                        lambda: tmp_path / "builtin")
    monkeypatch.setattr(modes_mod, "_user_modes_dir",
                        lambda: tmp_path / "no-user")
    with pytest.raises(ValueError) as exc:
        modes_mod.resolve(["nonexistent"])
    assert "nonexistent" in str(exc.value)
    assert "alpha" in str(exc.value)  # the available list is shown


def test_resolve_cycle_raises(tmp_path, monkeypatch):
    from peers import modes as modes_mod
    _make_mode(tmp_path, "alpha", requires=["beta"])
    _make_mode(tmp_path, "beta", requires=["alpha"])
    monkeypatch.setattr(modes_mod, "_builtin_modes_dir",
                        lambda: tmp_path / "builtin")
    monkeypatch.setattr(modes_mod, "_user_modes_dir",
                        lambda: tmp_path / "no-user")
    with pytest.raises(ValueError, match="(?i)cycle|circular"):
        modes_mod.resolve(["alpha"])
