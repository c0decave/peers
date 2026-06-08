"""Regression test for /tmp accumulation across pytest sessions.

`peers init` (src/peers/cli.py) intentionally hardens the scaffolded
`.peers/checks/` directory by chmod'ing it (and the .py files inside)
to 0o555 — read+exec, no write. Rationale lives in cli.py: stops a
peer from `rm`ing or rewriting a gate script mid-run.

Side effect for tests: integration tests invoke `peers init` against a
tmp_path. The resulting `<tmp>/.peers/checks/` is read-only. pytest's
own cross-session cleanup uses `rm_rf` which can `chmod` and retry on
PermissionError for files, but does NOT chmod parent dirs back to
writable before unlinking children. After ~3-4 sessions the read-only
trees pile up in `/tmp/pytest-of-USER/` and the 512MB tmpfs fills,
breaking `no-prior-regression` because pytest cannot write JUnit XML.

We don't want to weaken the production chmod (BUG-258 family — the
read-only dir is the actual defense). Instead conftest.py installs a
`pytest_sessionfinish` hook that walks the session basetemp and
restores write permission on every directory so pytest's NEXT-session
cleanup of old numbered dirs succeeds.

Tests below cover all three classes:

  happy : a sub-tree with mixed read-only and writable dirs is fully
          restored to writable after the hook runs.
  edge  : missing basetemp / empty basetemp / symlink in basetemp do
          not raise.
  sad   : a permission-denied chmod (parent is read-only and we lack
          ownership) is swallowed — the hook must never fail the
          pytest session, only best-effort.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest


# Import the hook from the suite's own conftest. We do this lazily
# inside each test because conftest.py is loaded by pytest itself and
# not normally importable by name.
def _load_hook():
    import importlib.util

    conftest_path = Path(__file__).resolve().parent.parent / "conftest.py"
    spec = importlib.util.spec_from_file_location(
        "_peers_conftest_for_test", conftest_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._restore_basetemp_writable


def _make_tree(root: Path) -> None:
    """Build: root/a/ (0o555 dir) containing a 0o444 file and a 0o555 subdir."""
    a = root / "a"
    a.mkdir()
    (a / "f.py").write_text("x")
    sub = a / "sub"
    sub.mkdir()
    (sub / "g.py").write_text("y")
    # Chmod from leaves inward, mirroring how cli.py hardens.
    (sub / "g.py").chmod(0o444)
    sub.chmod(0o555)
    (a / "f.py").chmod(0o444)
    a.chmod(0o555)


def test_happy_restore_writable_on_chmod_555_tree(tmp_path):
    _make_tree(tmp_path)
    hook = _load_hook()
    hook(tmp_path)

    for d in [tmp_path / "a", tmp_path / "a" / "sub"]:
        mode = stat.S_IMODE(d.stat().st_mode)
        assert mode & stat.S_IWUSR, f"{d} should be u+w (got {oct(mode)})"


def test_edge_missing_basetemp_is_noop(tmp_path):
    hook = _load_hook()
    missing = tmp_path / "does-not-exist"
    hook(missing)


def test_edge_empty_basetemp_is_noop(tmp_path):
    hook = _load_hook()
    hook(tmp_path)
    assert tmp_path.is_dir()


def test_edge_symlink_does_not_follow(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "marker").write_text("untouched")
    os.chmod(outside / "marker", 0o400)
    os.chmod(outside, 0o500)

    base = tmp_path / "base"
    base.mkdir()
    os.symlink(outside, base / "linked")

    hook = _load_hook()
    hook(base)

    mode = stat.S_IMODE(outside.stat().st_mode)
    assert mode == 0o500, (
        f"hook must not chmod through symlinks (outside dir mode={oct(mode)})"
    )
    os.chmod(outside, 0o700)


def test_sad_unreadable_dir_does_not_raise(tmp_path, monkeypatch):
    a = tmp_path / "a"
    a.mkdir()
    (a / "x").write_text("z")

    def boom(path, mode):
        raise PermissionError(f"refused: {path}")

    monkeypatch.setattr(os, "chmod", boom)

    hook = _load_hook()
    hook(tmp_path)


def test_sad_basetemp_is_file_does_not_raise(tmp_path):
    f = tmp_path / "not-a-dir"
    f.write_text("hi")
    hook = _load_hook()
    hook(f)
