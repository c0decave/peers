"""peers.modes.discover() — locate mode directories under templates/modes
(built-in) and ~/.config/peers/modes (user)."""
from __future__ import annotations


def test_discover_returns_dict_keyed_by_mode_name():
    from peers.modes import discover
    result = discover()
    assert isinstance(result, dict)


def test_discover_finds_no_modes_when_templates_dir_empty(tmp_path, monkeypatch):
    """When no built-in modes exist and no user dir is set, return {}."""
    from peers import modes as modes_mod
    empty = tmp_path / "no-modes"
    empty.mkdir()
    monkeypatch.setattr(modes_mod, "_builtin_modes_dir", lambda: empty)
    monkeypatch.setattr(modes_mod, "_user_modes_dir", lambda: tmp_path / "user-empty")
    assert modes_mod.discover() == {}


def test_discover_finds_a_minimal_builtin_mode(tmp_path, monkeypatch):
    """A mode dir with a mode.yaml + goals.yaml is found and parsed."""
    from peers import modes as modes_mod
    root = tmp_path / "builtin"
    mode_dir = root / "demo"
    mode_dir.mkdir(parents=True)
    (mode_dir / "mode.yaml").write_text(
        "name: demo\nversion: 1\ndescription: test\n"
    )
    (mode_dir / "goals.yaml").write_text("goals: []\n")
    monkeypatch.setattr(modes_mod, "_builtin_modes_dir", lambda: root)
    monkeypatch.setattr(modes_mod, "_user_modes_dir", lambda: tmp_path / "no-user")
    result = modes_mod.discover()
    assert "demo" in result
    assert result["demo"].name == "demo"
    assert result["demo"].version == 1
    assert result["demo"].source == "builtin"
    assert result["demo"].path == mode_dir


def test_discover_user_mode_overrides_builtin(tmp_path, monkeypatch):
    """User-supplied mode with same name as builtin wins (override path)."""
    from peers import modes as modes_mod
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user"
    for root in (builtin_root, user_root):
        m = root / "demo"
        m.mkdir(parents=True)
        (m / "mode.yaml").write_text(
            f"name: demo\nversion: 1\ndescription: from {root.name}\n"
        )
        (m / "goals.yaml").write_text("goals: []\n")
    monkeypatch.setattr(modes_mod, "_builtin_modes_dir", lambda: builtin_root)
    monkeypatch.setattr(modes_mod, "_user_modes_dir", lambda: user_root)
    result = modes_mod.discover()
    assert result["demo"].source == "user"
    assert "from user" in result["demo"].description


def test_discover_skips_invalid_mode_yaml(tmp_path, monkeypatch, capsys):
    """A mode dir without `name:` in mode.yaml is skipped with a stderr warning."""
    from peers import modes as modes_mod
    root = tmp_path / "builtin"
    bad = root / "bad"
    bad.mkdir(parents=True)
    (bad / "mode.yaml").write_text("description: oops\n")
    (bad / "goals.yaml").write_text("goals: []\n")
    monkeypatch.setattr(modes_mod, "_builtin_modes_dir", lambda: root)
    monkeypatch.setattr(modes_mod, "_user_modes_dir", lambda: tmp_path / "no-user")
    result = modes_mod.discover()
    assert "bad" not in result
    err = capsys.readouterr().err
    assert "bad" in err and "name" in err.lower() and "skipped" in err.lower()


def test_discover_skips_non_mapping_mode_yaml(tmp_path, monkeypatch, capsys):
    """mode.yaml whose top-level is a list (not a mapping) is skipped
    with a warning instead of crashing."""
    from peers import modes as modes_mod
    root = tmp_path / "builtin"
    bad = root / "weird"
    bad.mkdir(parents=True)
    (bad / "mode.yaml").write_text("- not\n- a\n- mapping\n")
    (bad / "goals.yaml").write_text("goals: []\n")
    monkeypatch.setattr(modes_mod, "_builtin_modes_dir", lambda: root)
    monkeypatch.setattr(modes_mod, "_user_modes_dir", lambda: tmp_path / "no-user")
    result = modes_mod.discover()
    assert "weird" not in result
    err = capsys.readouterr().err
    assert "weird" in err and "mapping" in err.lower()
