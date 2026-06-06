"""BUG-142: the orchestrator attributes a tick's commits by HEAD-delta.

After a tick, every commit the peer produced in ``(head_before, head_after]``
must carry a ``peers-attest`` note naming the peer that actually ran — derived
from the tick boundary the orchestrator observed, NOT from the (forgeable)
``Peer:`` trailer.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from peers.attest import attested_peer
from peers.driver_orchestrator import OrchestratorDriver
from peers.peer_spec import PeerSpec


def _specs(*names: str) -> list[PeerSpec]:
    return [PeerSpec(name=n, tool=n, argv=("true",), prompt_mode="stdin")
            for n in names]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "dash@localhost.local")
    _git(path, "config", "user.name", "dash")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "README").write_text("x\n")
    _git(path, "add", "README")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _driver(repo: Path) -> OrchestratorDriver:
    return OrchestratorDriver(
        repo=repo, peer_dir=repo / ".peers", goals=[],
        peer_specs=_specs("claude", "codex"),
    )


def test_attest_tick_commits_attributes_full_head_delta(tmp_path: Path):
    repo = _init_repo(tmp_path / "r")
    drv = _driver(repo)
    head_before = drv.comm.head_sha()

    # The peer produces two commits this tick; the first forges `Peer: codex`.
    (repo / "a.py").write_text("a")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "a\n\nPeer: codex")
    (repo / "b.py").write_text("b")
    _git(repo, "add", "b.py")
    _git(repo, "commit", "-q", "-m", "b")
    head_after = drv.comm.head_sha()

    drv._attest_tick_commits("claude", head_before, head_after)

    shas = _git(repo, "rev-list", "--reverse",
                f"{head_before}..HEAD").split()
    # Both commits — including the forged-trailer one — are attributed to the
    # peer that actually ran the tick.
    assert [attested_peer(repo, s) for s in shas] == ["claude", "claude"]
    # The boundary commit is not (re)attributed.
    assert attested_peer(repo, head_before) is None


def test_attest_tick_commits_empty_delta_is_noop(tmp_path: Path):
    repo = _init_repo(tmp_path / "r")
    drv = _driver(repo)
    head = drv.comm.head_sha()
    # No new commits this tick (e.g. a dry-run reset left HEAD unchanged).
    drv._attest_tick_commits("claude", head, head)
    assert attested_peer(repo, head) is None
