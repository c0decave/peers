"""Wiring tests for the Wave-2 live-tee enable path.

Proves the OFF-by-default flag plumbs end-to-end:
  config `observability.tee_stream` / env `PEERS_TEE_STREAM`
    -> OrchestratorDriver.tee_stream
    -> tick_loop passes tee_dir/tee_tag to health_guard.invoke()
    -> `.peers/log/peers/tick-<N>-<peer>.stream.jsonl` is written.

Categories: happy (enabled → file written), default-off (no flag → no file),
sad/edge (env flag parsing: truthy/falsey/garbage).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from peers._driver_orchestrator_impl import (
    OrchestratorDriver,
    _tee_stream_env_enabled,
)
from peers.peer_spec import PeerSpec

ROOT = Path(__file__).parent.parent.parent


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "widget.py").write_text("x\n")
    _git(path, "add", "widget.py")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _stdout_peer_fixture(tmp_path: Path) -> Path:
    """A peer that prints a recognisable line to stdout, then commits a
    proper handoff so the tick is well-formed."""
    p = tmp_path / "stdout_peer.py"
    p.write_text(
        "import os, subprocess, sys, uuid\n"
        "sys.stdin.read()\n"
        "print('STREAM-MARKER live tee line', flush=True)\n"
        "peer = os.environ.get('PEERS_PEER_NAME', 'claude')\n"
        "subprocess.run(['git','config','user.email','f@p'], check=True)\n"
        "subprocess.run(['git','config','user.name',peer], check=True)\n"
        "open('widget.py','a').write('m-'+uuid.uuid4().hex[:8]+'\\n')\n"
        "subprocess.run(['git','add','widget.py'], check=True)\n"
        "body=('turn\\n\\n## Self-Review\\nRe-read diff; fine.\\n\\n'\n"
        "      'Self-Review: pass\\nPeer-Status: handoff\\nPeer: '+peer+'\\n')\n"
        "subprocess.run(['git','commit','-q','-m',body], check=True)\n"
        "sys.exit(0)\n"
    )
    return p


def _driver(repo: Path, peer_fixture: Path, *, tee_stream: bool):
    argv = (sys.executable, str(peer_fixture))
    return OrchestratorDriver(
        repo=repo, peer_dir=repo / ".peers",
        goals=[],
        peer_specs=[
            PeerSpec(name="claude", tool="claude", argv=argv,
                     prompt_mode="stdin"),
            PeerSpec(name="codex", tool="codex", argv=argv,
                     prompt_mode="stdin"),
        ],
        idle_timeout_s=15, absolute_max_runtime_s=30,
        tee_stream=tee_stream,
    )


def test_tee_stream_enabled_writes_stream_file(tmp_path: Path, monkeypatch):
    """happy: tee_stream=True → tick-00001-claude.stream.jsonl appears with
    the peer's live stdout."""
    monkeypatch.delenv("PEERS_TEE_STREAM", raising=False)
    repo = _make_repo(tmp_path / "r")
    peer = _stdout_peer_fixture(tmp_path)
    drv = _driver(repo, peer, tee_stream=True)
    drv.run(max_ticks=1)

    peers_log = repo / ".peers" / "log" / "peers"
    streams = sorted(peers_log.glob("*.stream.jsonl"))
    assert streams, f"no .stream.jsonl written; dir={list(peers_log.glob('*'))}"
    body = "\n".join(p.read_text() for p in streams)
    assert "STREAM-MARKER live tee line" in body
    # Tag matches the tick-<NNNNN>-<peer> naming.
    assert any(s.name.startswith("tick-00001-") for s in streams)


def test_tee_stream_default_off_writes_no_stream_file(tmp_path: Path,
                                                      monkeypatch):
    """default-off: no flag → no .stream.jsonl (byte-identical launch)."""
    monkeypatch.delenv("PEERS_TEE_STREAM", raising=False)
    repo = _make_repo(tmp_path / "r")
    peer = _stdout_peer_fixture(tmp_path)
    drv = _driver(repo, peer, tee_stream=False)
    assert drv.tee_stream is False
    drv.run(max_ticks=1)

    peers_log = repo / ".peers" / "log" / "peers"
    assert list(peers_log.glob("*.stream.jsonl")) == []


def test_env_flag_enables_when_constructor_false(tmp_path: Path, monkeypatch):
    """happy: PEERS_TEE_STREAM=1 turns the tee on even with tee_stream=False
    passed to the constructor (env wins)."""
    monkeypatch.setenv("PEERS_TEE_STREAM", "1")
    repo = _make_repo(tmp_path / "r")
    peer = _stdout_peer_fixture(tmp_path)
    drv = _driver(repo, peer, tee_stream=False)
    assert drv.tee_stream is True


@pytest.mark.parametrize("val,expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True),
    ("on", True), (" On ", True),
    ("0", False), ("false", False), ("", False), ("nope", False),
    ("2", False),
])
def test_env_flag_parsing(monkeypatch, val, expected):
    """sad/edge: only explicit truthy tokens enable; everything else is OFF."""
    monkeypatch.setenv("PEERS_TEE_STREAM", val)
    assert _tee_stream_env_enabled() is expected


def test_env_flag_unset_is_off(monkeypatch):
    """default-off: unset env → OFF."""
    monkeypatch.delenv("PEERS_TEE_STREAM", raising=False)
    assert _tee_stream_env_enabled() is False
