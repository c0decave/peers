"""Unit J: pure layout persistence (textual-free, default-python).

``layout.py`` is a small pure module: it loads/saves the TUI window layout
(which panels are visible + best-effort floating-window geometry) to a JSON
file via ``peers.safe_io`` (no-symlink, size-capped) and merges a partial/old
file onto the default mission-control layout. Everything is FAIL-SOFT: a
missing / corrupt / oversized / wrong-shape file degrades to the default
layout and never raises — a bad layout file must never block launch.
"""
from __future__ import annotations

import json

from peers_ctl.tui import layout as L


# --------------------------------------------------------------------------- #
# defaults                                                                      #
# --------------------------------------------------------------------------- #
def test_default_layout_has_visible_set_and_is_mission_control():
    d = L.default_layout()
    # the default mission-control visible set (Gates/Peers/Tasks/Ticks/Budget/Live)
    assert isinstance(d, dict)
    assert "visible" in d and isinstance(d["visible"], dict)
    # the hidden windows (bugs/review/log/diff/autonomy) are absent or False.
    assert d["visible"].get("gates-panel") is True
    assert d["visible"].get("bugs-panel", False) is False
    # a fresh copy each call (no shared mutable default).
    d2 = L.default_layout()
    d["visible"]["gates-panel"] = False
    assert d2["visible"]["gates-panel"] is True


def test_default_layout_path_respects_config_dir(tmp_path):
    p = L.default_layout_path(config_dir=tmp_path)
    assert p == tmp_path / "tui-layout.json"


# --------------------------------------------------------------------------- #
# load — happy / sad / edge                                                    #
# --------------------------------------------------------------------------- #
def test_load_round_trip_happy(tmp_path):
    # happy: save then load -> the overrides survive (load merges onto defaults,
    # so every known panel is present, but the saved overrides + geometry win).
    p = tmp_path / "tui-layout.json"
    state = {"visible": {"gates-panel": True, "bugs-panel": True},
             "windows": {"diff-panel": {"x": 4, "y": 2, "w": 40, "h": 10}}}
    L.save_layout(p, state)
    loaded = L.load_layout(p)
    assert loaded["visible"]["bugs-panel"] is True       # override survives
    assert loaded["visible"]["gates-panel"] is True
    assert loaded["windows"]["diff-panel"] == {"x": 4, "y": 2, "w": 40, "h": 10}
    # and the merge filled in every known panel's default.
    assert set(loaded["visible"]) == set(L.known_panel_ids())


def test_load_missing_returns_defaults(tmp_path):
    # sad: a missing file -> the default layout (never raises).
    loaded = L.load_layout(tmp_path / "nope.json")
    assert loaded == L.default_layout()


def test_load_corrupt_returns_defaults(tmp_path):
    # sad: a corrupt (non-JSON) file -> the default layout.
    p = tmp_path / "tui-layout.json"
    p.write_text("{ not json at all")
    assert L.load_layout(p) == L.default_layout()


def test_load_non_object_returns_defaults(tmp_path):
    # sad: valid JSON but not an object (a list) -> the default layout.
    p = tmp_path / "tui-layout.json"
    p.write_text(json.dumps([1, 2, 3]))
    assert L.load_layout(p) == L.default_layout()


def test_load_oversized_returns_defaults(tmp_path):
    # edge/security: an oversized file is rejected (truncated read would corrupt
    # the JSON anyway) -> the default layout, never a partial parse.
    p = tmp_path / "tui-layout.json"
    # pad with whitespace so it's valid JSON were it not capped.
    p.write_text("{" + " " * (L._LAYOUT_MAX + 16) + '"visible": {}}')
    assert L.load_layout(p) == L.default_layout()


def test_load_symlinked_returns_defaults(tmp_path):
    # edge/security: a symlinked layout file is refused by safe_io -> defaults.
    import os
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"visible": {"gates-panel": False}}))
    link = tmp_path / "tui-layout.json"
    try:
        os.symlink(real, link)
    except OSError:
        import pytest
        pytest.skip("symlinks unsupported here")
    assert L.load_layout(link) == L.default_layout()


# --------------------------------------------------------------------------- #
# merge_with_defaults — happy / sad / edge                                     #
# --------------------------------------------------------------------------- #
def test_merge_partial_fills_defaults():
    # edge: a partial loaded dict (only one panel) merges onto the defaults so
    # every known panel still has a visibility, and the override wins.
    defaults = L.default_layout()
    loaded = {"visible": {"bugs-panel": True}}
    merged = L.merge_with_defaults(loaded, defaults)
    # the override is applied...
    assert merged["visible"]["bugs-panel"] is True
    # ...and the defaults are preserved for everything else.
    assert merged["visible"]["gates-panel"] is True


def test_merge_extra_unknown_keys_are_dropped():
    # edge: extra/unknown keys in the loaded dict are dropped (only the known
    # schema survives), so a forward/garbage file can't inject junk state.
    defaults = L.default_layout()
    loaded = {"visible": {"gates-panel": False, "ghost-panel": True},
              "evil": {"x": 1}, "windows": {"diff-panel": {"x": 1, "y": 2,
                                                           "w": 3, "h": 4}}}
    merged = L.merge_with_defaults(loaded, defaults)
    assert "evil" not in merged
    # an unknown panel id is not carried into the visible set.
    assert "ghost-panel" not in merged["visible"]
    # a known panel override survives.
    assert merged["visible"]["gates-panel"] is False
    # a well-formed window geometry survives.
    assert merged["windows"]["diff-panel"] == {"x": 1, "y": 2, "w": 3, "h": 4}


def test_merge_bad_value_types_fall_back_to_default():
    # sad: non-bool visibility / malformed window geometry are ignored, not
    # trusted — the default for that key wins.
    defaults = L.default_layout()
    loaded = {"visible": {"gates-panel": "yes-please", "peers-panel": 1},
              "windows": {"diff-panel": "not-a-dict",
                          "bugs-panel": {"x": "nope"}}}
    merged = L.merge_with_defaults(loaded, defaults)
    # a non-bool visibility falls back to the default (True for gates/peers).
    assert merged["visible"]["gates-panel"] is True
    assert merged["visible"]["peers-panel"] is True
    # a malformed window geometry is dropped entirely.
    assert "diff-panel" not in merged["windows"]
    assert "bugs-panel" not in merged["windows"]


def test_merge_non_dict_loaded_returns_defaults():
    # sad: a non-dict "loaded" (e.g. a list slipped through) -> the defaults.
    defaults = L.default_layout()
    assert L.merge_with_defaults([1, 2], defaults) == defaults
    assert L.merge_with_defaults(None, defaults) == defaults


# --------------------------------------------------------------------------- #
# save — happy / sad / edge                                                    #
# --------------------------------------------------------------------------- #
def test_save_creates_parent_dir(tmp_path):
    # happy: save into a not-yet-existing config dir -> the dir is created.
    p = tmp_path / "sub" / "dir" / "tui-layout.json"
    L.save_layout(p, {"visible": {"gates-panel": True}})
    assert p.exists()
    assert L.load_layout(p)["visible"]["gates-panel"] is True


def test_save_only_persists_known_schema(tmp_path):
    # edge: save sanitizes — junk keys are not written to disk.
    p = tmp_path / "tui-layout.json"
    L.save_layout(p, {"visible": {"gates-panel": True}, "evil": "x"})
    raw = json.loads(p.read_text())
    assert "evil" not in raw


def test_save_fail_soft_on_bad_path(tmp_path):
    # sad: saving to an un-writable path must never raise (a quit must not crash
    # just because the layout couldn't be persisted).
    # a path whose parent is a FILE (not a dir) -> save fails soft.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file")
    L.save_layout(blocker / "tui-layout.json", {"visible": {}})  # must not raise
