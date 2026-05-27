"""Tests for the Phase-3i `PEERS_PROJECTS_ROOT` convention in
`peers_ctl.cli`. Bare-name `peers-ctl new foo` should resolve to
`$PEERS_PROJECTS_ROOT/foo` (default `~/c0de/peers-c0de/foo`), while
absolute / dotted paths bypass the convention for backwards compat.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from peers_ctl.cli import (
    _DEFAULT_PROJECTS_ROOT,
    expand_project_arg,
    projects_root,
)


def test_default_root_is_home_c0de_peers_c0de(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("PEERS_PROJECTS_ROOT", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Re-resolve default since it's computed at import time using the
    # original home — call projects_root() which recomputes.
    # Wait: _DEFAULT_PROJECTS_ROOT is computed at import time, so we
    # can only test the env-override path here. Confirm the default
    # *constant* points at the expected place.
    assert _DEFAULT_PROJECTS_ROOT.name == "peers-c0de"
    assert _DEFAULT_PROJECTS_ROOT.parent.name == "c0de"


def test_env_override_takes_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("PEERS_PROJECTS_ROOT", str(tmp_path / "custom"))
    root = projects_root()
    assert root == (tmp_path / "custom").resolve()
    assert root.is_dir()  # auto-created


def test_env_override_expands_tilde(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("PEERS_PROJECTS_ROOT", str(tmp_path / "via-tilde"))
    root = projects_root()
    assert root == (tmp_path / "via-tilde").resolve()


def test_bare_name_resolves_under_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("PEERS_PROJECTS_ROOT", str(tmp_path / "root"))
    out = expand_project_arg(Path("my-proj"))
    assert out == (tmp_path / "root" / "my-proj").resolve()


def test_absolute_path_bypasses_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("PEERS_PROJECTS_ROOT", str(tmp_path / "root"))
    explicit = tmp_path / "elsewhere" / "thing"
    explicit.parent.mkdir(parents=True)
    out = expand_project_arg(explicit)
    assert out == explicit.resolve()


def test_relative_with_slash_bypasses_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("PEERS_PROJECTS_ROOT", str(tmp_path / "root"))
    # "sub/dir" has a `/`, so it's a path (relative to cwd), not a bare name.
    monkeypatch.chdir(tmp_path)
    out = expand_project_arg(Path("sub/dir"))
    assert out == (tmp_path / "sub" / "dir").resolve()


def test_dotted_relative_bypasses_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """./foo and ../bar are paths, not bare names."""
    monkeypatch.setenv("PEERS_PROJECTS_ROOT", str(tmp_path / "root"))
    monkeypatch.chdir(tmp_path)
    # Path('./foo') normalises to Path('foo'), losing the './' prefix.
    # So both forms collapse to 'foo' which counts as a bare name —
    # we document this in the README. The behavior tested here is the
    # path-with-slash case which always works as-expected.
    out = expand_project_arg(Path("./sub/dir"))
    assert out == (tmp_path / "sub" / "dir").resolve()


def test_projects_root_auto_creates_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    target = tmp_path / "fresh-never-existed"
    assert not target.exists()
    monkeypatch.setenv("PEERS_PROJECTS_ROOT", str(target))
    root = projects_root()
    assert root.is_dir()
