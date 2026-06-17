"""FU-1: the substrate refuses to converge on a forged baseline.

The no-prior-regression / no-skipped-tests HARD gates trust an agent-writable
baseline file (.peers/passing-baseline.txt, .peers/skip-baseline.txt). A peer
that forges the baseline mid-run defeats the gate (P0 review, 2026-06-12).

The defence is substrate-side and cannot be reached by the agent: the
orchestrator captures the run-start baseline digest in process memory
(self._baseline_digests) BEFORE any peer touches code, and the convergence
decision (_all_green_including_soft) re-derives the live digest and refuses to
declare convergence when it diverged. The agent runs as a separate, short-lived
subprocess and cannot rewrite the orchestrator's in-memory anchor, so — unlike
the on-disk baseline — the anchor is not forgeable.
"""
from __future__ import annotations

import copy
import subprocess
from pathlib import Path

from peers.driver_orchestrator import OrchestratorDriver
from peers.peer_spec import PeerSpec
from peers.regression_baseline import snapshot_baseline_digests
from peers.state_store import DEFAULT_STATE


def _specs(*names: str) -> list[PeerSpec]:
    return [
        PeerSpec(name=n, tool=n if n in ("claude", "codex") else "claude",
                 argv=("true",), prompt_mode="stdin")
        for n in names
    ]


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"],
                   check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"],
                   check=True)
    (path / "x").write_text("x")
    subprocess.run(["git", "-C", str(path), "add", "x"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"],
                   check=True)
    return path


def _driver_with_baseline(tmp_path: Path) -> tuple[OrchestratorDriver, Path]:
    repo = _init_repo(tmp_path / "r")
    peer_dir = repo / ".peers"
    peer_dir.mkdir()
    (peer_dir / "passing-baseline.txt").write_text(
        "tests.test_calc::test_addition\ntests.test_calc::test_subtraction\n"
    )
    drv = OrchestratorDriver(
        repo=repo, peer_dir=peer_dir, goals=[], peer_specs=_specs("claude", "codex"),
    )
    # Mirror run-start: orchestrator captures the digest in process memory.
    drv._baseline_digests = snapshot_baseline_digests(peer_dir)
    return drv, peer_dir


def test_convergence_allowed_when_baseline_intact(tmp_path: Path) -> None:
    # happy/control: no hard gates, baseline untouched → convergence proceeds.
    drv, _peer_dir = _driver_with_baseline(tmp_path)
    state = copy.deepcopy(DEFAULT_STATE)
    assert drv._all_green_including_soft(state) is True


def test_convergence_blocked_when_passing_baseline_forged(
    tmp_path: Path,
) -> None:
    # sad/security: the PoC — a peer forges the passing baseline to hide a
    # regression. Even with all hard gates "green", the substrate refuses to
    # converge because the live baseline diverged from the run-start anchor.
    drv, peer_dir = _driver_with_baseline(tmp_path)
    (peer_dir / "passing-baseline.txt").write_text(
        "tests.test_calc::test_addition\n"  # dropped the now-red test
    )
    state = copy.deepcopy(DEFAULT_STATE)
    assert drv._all_green_including_soft(state) is False


def test_convergence_unaffected_when_no_baseline_anchor(tmp_path: Path) -> None:
    # edge: a run with no baseline gate configured (empty anchor) is never
    # blocked by this guard — existing non-baseline modes are unchanged.
    repo = _init_repo(tmp_path / "r")
    peer_dir = repo / ".peers"
    peer_dir.mkdir()
    drv = OrchestratorDriver(
        repo=repo, peer_dir=peer_dir, goals=[], peer_specs=_specs("claude", "codex"),
    )
    assert drv._baseline_digests == {}
    state = copy.deepcopy(DEFAULT_STATE)
    assert drv._all_green_including_soft(state) is True
