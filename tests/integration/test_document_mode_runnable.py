"""End-to-end (no live LLM): `--modes=document` scaffolds a runnable project,
the substrate seed produces a drift-clean CODEMAP, and the gates correctly
distinguish an undocumented map (summaries-complete RED) from a fully-summarized
one (GREEN) while the structural gates stay green throughout. This is the
deterministic proof that document mode is end-to-end runnable.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

import peers
from peers.cli import cmd_init, cmd_run_check
from peers.codemap_gen import REPO_CODEMAP_FILE, seed_repo_codemap

# `cmd_run_check` invokes each check as a subprocess with cwd=<target repo>.
# Pin PYTHONPATH to the ABSOLUTE src holding the `peers` we're testing so the
# subprocess imports the same code as this process (a relative `PYTHONPATH=src`
# would otherwise resolve against the target repo's cwd, or fall back to an
# installed `peers` lacking new symbols — a worktree-only artifact).
_SRC = str(Path(peers.__file__).resolve().parents[1])


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "src" / "pkg" / "mod.py").write_text(
        "def pub(a, b):\n    return a\n\n\n"
        "class C:\n    def m(self, x):\n        return x\n",
        encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    return repo


def test_document_mode_scaffolds_and_gates_drive_the_build(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", _SRC)
    repo = _repo(tmp_path)

    # 1. scaffold a document-mode project through the real init path
    assert cmd_init(repo, force=False, modes=["document"]) == 0
    assert (repo / ".peers" / "goals.yaml").is_file()
    assert "document" in (repo / ".peers" / "modes-applied.txt").read_text()
    gids = {g["id"] for g in
            yaml.safe_load((repo / ".peers" / "goals.yaml").read_text())["goals"]}
    assert {"codemap-grounded", "codemap-summaries-complete",
            "summaries-cross-review"} <= gids

    # 2. seed the structural CODEMAP (what the orchestrator does at run start)
    seed_repo_codemap(repo)
    assert (repo / REPO_CODEMAP_FILE).is_file()

    # 3. structural gates GREEN on the seed; summaries-complete RED (build target)
    assert cmd_run_check(repo, "grounded") == 0
    assert cmd_run_check(repo, "signature_match") == 0
    assert cmd_run_check(repo, "complete") == 0
    assert cmd_run_check(repo, "summaries_complete") == 1  # nothing documented yet

    # 4. peers add summaries → summaries-complete flips GREEN; structure untouched
    target = repo / REPO_CODEMAP_FILE
    cm = yaml.safe_load(target.read_text())
    for e in cm["entries"]:
        e["summary"] = f"Documents {e['id']} — verified against the source."
    target.write_text(yaml.safe_dump(cm, sort_keys=False), encoding="utf-8")
    assert cmd_run_check(repo, "summaries_complete") == 0
    assert cmd_run_check(repo, "grounded") == 0
    assert cmd_run_check(repo, "signature_match") == 0
    assert cmd_run_check(repo, "complete") == 0

    # 5. Phase 2: agents-in-sync is RED until AGENTS.md is generated, then GREEN.
    assert cmd_run_check(repo, "agents_in_sync") == 1  # AGENTS.md not generated yet
    from peers.cli import cmd_agents_doc
    assert cmd_agents_doc(repo) == 0                    # deterministic render
    assert (repo / "AGENTS.md").is_file()
    assert cmd_run_check(repo, "agents_in_sync") == 0   # now in sync
    # a hand-edit to AGENTS.md re-breaks the gate (drift caught)
    (repo / "AGENTS.md").write_text(
        (repo / "AGENTS.md").read_text() + "\nhand edit\n", encoding="utf-8")
    assert cmd_run_check(repo, "agents_in_sync") == 1
