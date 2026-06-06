import peers._driver_orchestrator_impl as impl


def test_run_codemap_step_writes_files(tmp_path):
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "mod.py").write_text(
        "def pub(a):\n    return a\n", encoding="utf-8")
    pd = tmp_path / ".peers"
    pd.mkdir()

    class _Stub:
        repo = tmp_path
        peer_dir = pd
    impl.OrchestratorDriver._run_codemap_step(_Stub())
    assert (pd / "CODEMAP.yaml").is_file()
    assert (pd / "codemap.md").is_file()


def test_codemap_enabled_defaults_true():
    import inspect
    sig = inspect.signature(impl.OrchestratorDriver.__init__)
    assert sig.parameters["codemap_enabled"].default is True


# Orchestrator-integration tests — _run_codemap_step is invoked from run()
# before the tick loop when codemap_enabled=True. Mirrors the recon
# integration tests (test_recon.py::test_orchestrator_calls_recon_when_enabled
# and ::test_orchestrator_skips_recon_when_disabled), swapping recon->codemap.

def test_orchestrator_calls_codemap_when_enabled(tmp_path, monkeypatch):
    """OrchestratorDriver with codemap_enabled=True (the default) calls
    run_codemap at run-start. Verify by patching the facade alias
    peers.driver_orchestrator._run_codemap and confirming invocation."""
    import subprocess
    from pathlib import Path
    from peers.driver_orchestrator import OrchestratorDriver
    from peers.peer_spec import PeerSpec

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"],
                   cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"],
                   cwd=repo, check=True)
    (repo / "x").write_text("x")
    subprocess.run(["git", "add", "x"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"],
                   cwd=repo, check=True)
    peer_dir = repo / ".peers"
    peer_dir.mkdir(mode=0o700)

    calls: list[tuple[Path, Path]] = []

    def fake_run_codemap(repo_arg, peer_dir_arg, force=False):
        calls.append((Path(repo_arg), Path(peer_dir_arg)))
        return "codemap: stub"

    monkeypatch.setattr(
        "peers.driver_orchestrator._run_codemap", fake_run_codemap,
    )

    drv = OrchestratorDriver(
        repo=repo, peer_dir=peer_dir,
        goals=[], peer_specs=[
            PeerSpec(name=n, tool=n if n in ("claude", "codex") else "claude",
                     argv=("true",), prompt_mode="stdin")
            for n in ("claude", "codex")
        ],
        codemap_enabled=True,
    )
    drv.run(max_ticks=0)

    assert len(calls) == 1
    assert calls[0][0].resolve() == repo.resolve()
    assert calls[0][1].resolve() == peer_dir.resolve()


def test_orchestrator_skips_codemap_when_disabled(tmp_path, monkeypatch):
    import subprocess
    from peers.driver_orchestrator import OrchestratorDriver
    from peers.peer_spec import PeerSpec

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"],
                   cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"],
                   cwd=repo, check=True)
    (repo / "x").write_text("x")
    subprocess.run(["git", "add", "x"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"],
                   cwd=repo, check=True)
    peer_dir = repo / ".peers"
    peer_dir.mkdir(mode=0o700)

    calls: list = []

    def fake_run_codemap(*a, **kw):
        calls.append(a)
        return "codemap: stub"

    monkeypatch.setattr(
        "peers.driver_orchestrator._run_codemap", fake_run_codemap,
    )

    drv = OrchestratorDriver(
        repo=repo, peer_dir=peer_dir,
        goals=[], peer_specs=[
            PeerSpec(name=n, tool=n if n in ("claude", "codex") else "claude",
                     argv=("true",), prompt_mode="stdin")
            for n in ("claude", "codex")
        ],
        codemap_enabled=False,
    )
    drv.run(max_ticks=0)

    assert calls == []


# document-mode seed gating: _run_document_seed_step is invoked from run()
# only when the sole active mode is `document` (detected from modes-applied.txt).

def _doc_init(tmp_path, mode_line):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "x").write_text("x")
    subprocess.run(["git", "add", "x"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    peer_dir = repo / ".peers"
    peer_dir.mkdir(mode=0o700)
    if mode_line:
        (peer_dir / "modes-applied.txt").write_text(mode_line)
    return repo, peer_dir


def _doc_peers():
    from peers.peer_spec import PeerSpec
    return [PeerSpec(name=n, tool=n if n in ("claude", "codex") else "claude",
                     argv=("true",), prompt_mode="stdin")
            for n in ("claude", "codex")]


def test_orchestrator_seeds_codemap_for_document_mode(tmp_path, monkeypatch):
    from peers.driver_orchestrator import OrchestratorDriver

    repo, peer_dir = _doc_init(
        tmp_path, "2026-06-01T00:00:00+00:00  document  v1  sha256=x\n")
    seeds: list = []
    monkeypatch.setattr("peers.driver_orchestrator._seed_repo_codemap",
                        lambda r: (seeds.append(r), "document-seed: stub")[1])
    monkeypatch.setattr("peers.driver_orchestrator._run_codemap",
                        lambda *a, **k: "codemap: stub")
    drv = OrchestratorDriver(repo=repo, peer_dir=peer_dir, goals=[],
                             peer_specs=_doc_peers())
    assert drv.mode_name == "document"
    drv.run(max_ticks=0)
    assert len(seeds) == 1


def test_orchestrator_does_not_seed_for_non_document_mode(tmp_path, monkeypatch):
    from peers.driver_orchestrator import OrchestratorDriver

    repo, peer_dir = _doc_init(
        tmp_path, "2026-06-01T00:00:00+00:00  audit  v1  sha256=x\n")
    seeds: list = []
    monkeypatch.setattr("peers.driver_orchestrator._seed_repo_codemap",
                        lambda r: (seeds.append(r), "stub")[1])
    monkeypatch.setattr("peers.driver_orchestrator._run_codemap",
                        lambda *a, **k: "codemap: stub")
    drv = OrchestratorDriver(repo=repo, peer_dir=peer_dir, goals=[],
                             peer_specs=_doc_peers())
    drv.run(max_ticks=0)
    assert seeds == []


def test_orchestrator_seeds_architecture_for_document_mode(tmp_path, monkeypatch):
    """document mode also seeds ARCHITECTURE.md (alongside CODEMAP.yaml) so the
    architecture-grounded gate starts red and drives the human-docs build."""
    from peers.driver_orchestrator import OrchestratorDriver

    repo, peer_dir = _doc_init(
        tmp_path, "2026-06-01T00:00:00+00:00  document  v1  sha256=x\n")
    arch_seeds: list = []
    monkeypatch.setattr("peers.driver_orchestrator._seed_repo_codemap",
                        lambda r: "document-seed: stub")
    monkeypatch.setattr("peers.driver_orchestrator._seed_repo_architecture",
                        lambda r: (arch_seeds.append(r), "arch: stub")[1],
                        raising=False)
    monkeypatch.setattr("peers.driver_orchestrator._run_codemap",
                        lambda *a, **k: "codemap: stub")
    drv = OrchestratorDriver(repo=repo, peer_dir=peer_dir, goals=[],
                             peer_specs=_doc_peers())
    assert drv.mode_name == "document"
    drv.run(max_ticks=0)
    assert len(arch_seeds) == 1
