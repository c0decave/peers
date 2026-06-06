"""End-to-end (no live LLM): `--modes=document` scaffolds a project, the
substrate seeds ARCHITECTURE.md as a narrative outline, and the
`architecture-grounded` gate correctly distinguishes the unfilled seed (RED:
placeholder + every subsystem uncovered) from a covered, anchor-resolving
narrative (GREEN) — through the real `run-check` path. The deterministic proof
that the human-docs gate is runnable.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import peers
from peers.cli import cmd_init, cmd_run_check
from peers.codemap import ARCHITECTURE_FILE
from peers.codemap_gen import seed_repo_architecture, seed_repo_codemap

# `cmd_run_check` invokes each check as a subprocess with cwd=<target repo>; pin
# PYTHONPATH to the ABSOLUTE src holding the `peers` under test (see the note in
# test_document_mode_runnable.py — a relative `src` resolves against the target).
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


def test_architecture_gate_red_on_seed_then_green_when_covered(
        tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", _SRC)
    repo = _repo(tmp_path)

    # 1. scaffold a document-mode project + seed CODEMAP and ARCHITECTURE.md
    assert cmd_init(repo, force=False, modes=["document"]) == 0
    seed_repo_codemap(repo)
    status = seed_repo_architecture(repo)
    assert (repo / ARCHITECTURE_FILE).is_file() and "wrote" in status

    # 2. the fresh seed is RED (placeholder present + the `mod` subsystem
    #    uncovered) through the real run-check resolution path
    assert cmd_run_check(repo, "architecture_grounded") == 1

    # 3. peers write a covered, anchor-resolving narrative → GREEN
    (repo / ARCHITECTURE_FILE).write_text(
        "# Architecture\n\n"
        "The module `[[pkg.mod]]` exposes `[[pkg.mod.pub]]` and the class "
        "`[[pkg.mod.C]]`, whose `[[pkg.mod.C.m]]` carries the per-item work.\n",
        encoding="utf-8")
    assert cmd_run_check(repo, "architecture_grounded") == 0

    # 4. a dangling anchor (a renamed/removed symbol) re-breaks the gate
    (repo / ARCHITECTURE_FILE).write_text(
        "# Architecture\n\nsee `[[pkg.mod.gone]]` and `[[pkg.mod.pub]]`\n",
        encoding="utf-8")
    assert cmd_run_check(repo, "architecture_grounded") == 1
