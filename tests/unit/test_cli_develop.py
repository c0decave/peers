"""The `peers develop` operator verb — the headline reachability win (DEV-01 /
SPEC-02): develop was library/test-only with zero production constructors. This
gives an operator a single-repo entry that drives the real DevelopFrontend.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from peers.cli import _build_develop_frontend_from_config, build_parser, cmd_develop
from peers.develop.adapters import LLMAuditor
from peers.develop.assembly import make_develop_frontend
from peers.develop.frontend import DevelopFrontend

_VALID_CONFIG = """\
driver: orchestrator
comm: git
peers:
  - name: claude
    tool: claude
    argv: ["claude", "-p", "{PROMPT}"]
    prompt_mode: argv-substitute
budget: {max_iterations: 1, max_runtime_s: 60, max_consecutive_failures: 1}
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "seed").write_text("x", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


# --- happy path ---------------------------------------------------------------
def test_happy_develop_drives_real_frontend_and_writes_ledger(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    def fake_make(r: Path):
        # real DevelopFrontend, deterministic empty audit -> clean dry termination
        return make_develop_frontend(
            r, run_agent=lambda p: "[]", impl_run_agent=lambda p, w: "",
            dimensions=["correctness"], run_tests=lambda c: (0, "1 passed"))

    rc = cmd_develop(repo, dimensions=["correctness"], _make_frontend=fake_make)
    assert rc == 0
    assert (repo / ".peers" / "run.jsonl").exists()


# --- sad path -----------------------------------------------------------------
def test_sad_missing_repo_fails_closed(tmp_path: Path) -> None:
    rc = cmd_develop(tmp_path / "does-not-exist", dimensions=["correctness"])
    assert rc == 1


def test_sad_no_dimensions_fails_closed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    rc = cmd_develop(repo, dimensions=[])
    assert rc == 2


def test_sad_frontend_construction_error_fails_closed(tmp_path: Path) -> None:
    # TQ-04: a frontend that cannot be built (e.g. missing config) returns 1, not
    # a traceback.
    repo = _repo(tmp_path)

    def boom(_r):
        raise ValueError("no peers configured")

    rc = cmd_develop(repo, dimensions=["correctness"], _make_frontend=boom)
    assert rc == 1


def test_sad_missing_config_default_path_fails_closed(tmp_path: Path) -> None:
    # the real (non-injected) path: a repo with no .peers/config.yaml -> rc 1.
    repo = _repo(tmp_path)
    rc = cmd_develop(repo, dimensions=["correctness"])
    assert rc == 1


# --- live config-wiring path (test gap closed) --------------------------------
def test_live_config_path_builds_real_wired_frontend(tmp_path: Path) -> None:
    # The production (non-injected) build: a valid .peers/config.yaml is loaded,
    # the first peer spec selected, run_agent wired via agent_runner_from_spec, and
    # a real DevelopFrontend assembled -- WITHOUT invoking a live model. Exercises
    # the full _build_develop_frontend_from_config path the missing-config sad test
    # never reaches (closes the LOW live-config test gap), and confirms the CB-4
    # factory is wired on the production path.
    repo = _repo(tmp_path)
    (repo / ".peers").mkdir()
    (repo / ".peers" / "config.yaml").write_text(_VALID_CONFIG, encoding="utf-8")

    fe = _build_develop_frontend_from_config(
        repo, dimensions=["correctness"], peer=None, budget=5)

    assert isinstance(fe, DevelopFrontend)
    assert isinstance(fe.auditor, LLMAuditor)        # real adapter, not a stub
    assert fe.dimensions == ["correctness"]
    assert fe.run_tests_factory is not None           # CB-4 production wiring


def test_live_config_path_unknown_peer_fails_closed(tmp_path: Path) -> None:
    # sad: a --peer that is not in the config raises ValueError (cmd_develop maps it
    # to rc 1), never silently falls back to peer[0].
    import pytest
    repo = _repo(tmp_path)
    (repo / ".peers").mkdir()
    (repo / ".peers" / "config.yaml").write_text(_VALID_CONFIG, encoding="utf-8")
    with pytest.raises(ValueError):
        _build_develop_frontend_from_config(
            repo, dimensions=["correctness"], peer="ghost", budget=5)


# --- edge ---------------------------------------------------------------------
def test_edge_develop_subcommand_is_registered() -> None:
    parser = build_parser()
    ns = parser.parse_args(["develop", "/some/repo", "--dimensions", "correctness,security"])
    assert ns.cmd == "develop"
    assert ns.repo == "/some/repo"
    assert ns.dimensions == "correctness,security"
