"""TDD for the full-depth-analysis MINOR/MEDIUM fixes (#9-#16)."""
from __future__ import annotations

from pathlib import Path

import pytest


# --- #16: recon tree renders exactly `depth` levels (was depth+1) ----------
def test_recon_tree_renders_exactly_depth_levels(tmp_path: Path):
    from peers.recon import _tree
    deep = tmp_path / "aaa" / "bbb" / "ccc"
    deep.mkdir(parents=True)
    (deep / "f.txt").write_text("x")
    lines = list(_tree(tmp_path, depth=2))
    joined = "\n".join(lines)
    assert "bbb" in joined          # 2nd level rendered
    assert "ccc" not in joined      # 3rd level NOT rendered (depth=2)


# --- #14: a non-string mode-goal id must not raise a raw TypeError ----------
def test_merge_skips_non_string_goal_id(tmp_path: Path):
    from peers.modes import Mode, merge
    md = tmp_path / "m"
    (md).mkdir()
    (md / "mode.yaml").write_text("name: m\n")
    (md / "goals.yaml").write_text(
        "goals:\n  - id: [a, b]\n    type: hard\n    cmd: 'true'\n"
        "  - id: real\n    type: hard\n    cmd: 'true'\n")
    mode = Mode(name="m", version=1, path=md)
    doc, _checks = merge([mode])          # must NOT raise TypeError
    ids = {g.get("id") for g in doc.get("goals", []) if isinstance(g, dict)}
    assert "real" in ids                  # the good goal survives; the bad one skipped


# --- #12: retry_on_fail validated + capped ---------------------------------
def _goals_yaml(tmp_path: Path, retry) -> Path:
    p = tmp_path / "goals.yaml"
    p.write_text(f"goals:\n  - id: g\n    type: hard\n    cmd: 'true'\n"
                 f"    pass_when: 'exit_code == 0'\n    retry_on_fail: {retry}\n")
    return p


def test_retry_on_fail_caps_and_rejects(tmp_path: Path):
    from peers.goals import load_goals
    g = load_goals(_goals_yaml(tmp_path, 100000))[0]
    assert g.retry_on_fail == 5           # capped, not unbounded
    with pytest.raises(ValueError, match="retry_on_fail"):
        load_goals(_goals_yaml(tmp_path, 2.7))   # float rejected with a goal-id error


# --- #9: _default_snapshot returns reuse only when a baseline is pinned ------
def test_default_snapshot_keys_on_pinned_baseline(tmp_path: Path):
    from peers.spine.baseline import _default_snapshot
    peers = tmp_path / ".peers"
    peers.mkdir()
    assert _default_snapshot(tmp_path) is None        # nothing pinned
    (peers / "passing-baseline.txt").write_text("")   # empty -> still nothing
    assert _default_snapshot(tmp_path) is None
    (peers / "passing-baseline.txt").write_text("tests/unit/test_x::test_y\n")
    assert _default_snapshot(tmp_path) is not None     # a green baseline IS pinned


# --- #13: _safe_head_sha degrades to None instead of crashing the tick ------
def test_safe_head_sha_returns_none_on_git_error():
    import subprocess
    from peers.tick_loop import _safe_head_sha

    class _Boom:
        def head_sha(self):
            raise subprocess.CalledProcessError(128, ["git", "rev-parse", "HEAD"])

    class _Ok:
        def head_sha(self):
            return "abc"

    assert _safe_head_sha(_Boom()) is None
    assert _safe_head_sha(_Ok()) == "abc"


# --- #11: the main-thread SIGALRM ReDoS bound still works (the off-main path
#         is knowingly unbounded — a watchdog can't bound a GIL-holding regex)
def test_safe_regex_main_thread_bound_still_works():
    from peers.goals import _safe_regex_search
    with pytest.raises(ValueError, match="timed out"):
        _safe_regex_search(r"(a+)+$", "a" * 40 + "X")


# --- #15: check_complete flags an unparseable source file (fail-closed) ------
def test_check_complete_flags_unparseable_source(tmp_path: Path):
    from peers.codemap import CodeMap, check_complete
    src = tmp_path / "src" / "pkg"
    src.mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("")
    (src / "broken.py").write_text("def oops(:\n")   # SyntaxError
    msgs = check_complete(tmp_path, CodeMap(entries=[]))
    assert any("broken.py" in m and "unparseable" in m for m in msgs)
