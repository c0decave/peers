import pytest
from pathlib import Path
from peers.spine.worktree import (RunWorkspace, PropagationResult,
                                  WorktreeProvider, workspace_names)


def test_runworkspace_and_result_dataclasses():
    ws = RunWorkspace(worktree_path=Path("/w"), branch="peers/run/r1",
                      base_sha="a" * 40, mode_run="r1")
    assert ws.branch == "peers/run/r1" and ws.mode_run == "r1"
    r = PropagationResult(ok=True, witness={"kind": "git-sha"}, artifact="peers/run/r1")
    assert r.ok is True and r.reason == ""


def test_provider_protocol_is_runtime_checkable():
    from contextlib import contextmanager

    class _P:
        @contextmanager
        def lease(self, repo, mode_run, *, base=None):
            yield None
        def propagate(self, from_ws, to_ws, artifact):
            return PropagationResult(ok=True)

    class _NotAProvider:            # missing propagate()
        @contextmanager
        def lease(self, repo, mode_run, *, base=None):
            yield None

    assert isinstance(_P(), WorktreeProvider)
    assert not isinstance(_NotAProvider(), WorktreeProvider)


def test_namer_is_pure_and_deterministic(tmp_path):
    wt, branch = workspace_names(tmp_path, "r1")
    assert branch == "peers/run/r1"
    # the worktree LEAF is the run-local mkdtemp base + mode_run (OUTSIDE the
    # repo); the namer is pure in (base_root, mode_run) -> two calls with the
    # SAME base_root agree, and the path basename is exactly mode_run.
    assert wt.name == "r1"
    assert workspace_names(tmp_path, "r1") == (wt, branch)     # pure given a fixed base_root


@pytest.mark.parametrize("bad", ["a/b", "..", "", ".", "x/../y", "a\\b", "\\x"])
def test_namer_rejects_unsafe_mode_run(tmp_path, bad):
    with pytest.raises(ValueError):
        workspace_names(tmp_path, bad)
