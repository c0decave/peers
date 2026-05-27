"""Test diff-vs-claim opt-in soft gate (Task 8.4).

Heuristic content-word overlap between PLAN.md `[x]` step text and the
referenced commit's subject + changed files. Always exits 0; findings
are advisory.
"""
from __future__ import annotations

import subprocess

from peers.templates.modes.implement.checks import diff_vs_claim


def _git_init(repo: str) -> None:
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(
        ["git", "-C", repo, "config", "user.email", "t@t"], check=True
    )
    subprocess.run(
        ["git", "-C", repo, "config", "user.name", "t"], check=True
    )
    subprocess.run(
        ["git", "-C", repo, "config", "commit.gpgsign", "false"],
        check=True,
    )


def _git_commit(repo: str, fname: str, body: str, msg: str) -> str:
    from pathlib import Path as P

    P(repo, fname).parent.mkdir(parents=True, exist_ok=True)
    P(repo, fname).write_text(body)
    subprocess.run(["git", "-C", repo, "add", fname], check=True)
    subprocess.run(
        ["git", "-C", repo, "commit", "-q", "-m", msg], check=True
    )
    proc = subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()[:12]


def test_no_steps_clean(tmp_path, capsys):
    """No PLAN.md / no checked SHA-stamped steps -- clean."""
    rc = diff_vs_claim.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out.lower()


def test_no_checked_steps_clean(tmp_path, capsys):
    """PLAN.md present but only [ ] steps -- clean."""
    (tmp_path / "PLAN.md").write_text(
        "# F\n## Meta\nsurfaces: [cli]\nacceptance: pytest\n\n## Steps\n"
        "- [ ] [STEP-1] do thing\n"
    )
    rc = diff_vs_claim.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out.lower()


def test_matching_content_words_clean(tmp_path, capsys):
    """Step text shares content words with commit subject -- clean."""
    repo = str(tmp_path)
    _git_init(repo)
    sha = _git_commit(
        repo,
        "src/authentication.py",
        "def login(): ...\n",
        "add authentication module with login function",
    )
    (tmp_path / "PLAN.md").write_text(
        "# F\n## Meta\nsurfaces: [cli]\nacceptance: pytest\n\n## Steps\n"
        f"- [x] [STEP-1] add authentication module ({sha})\n"
    )
    rc = diff_vs_claim.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out.lower()


def test_divergent_content_words_warn(tmp_path, capsys):
    """Step text has zero overlap with commit -- soft warn (still exit 0)."""
    repo = str(tmp_path)
    _git_init(repo)
    sha = _git_commit(
        repo,
        "src/parser.py",
        "def parse(): ...\n",
        "rename frobnicate to wibble",
    )
    (tmp_path / "PLAN.md").write_text(
        "# F\n## Meta\nsurfaces: [cli]\nacceptance: pytest\n\n## Steps\n"
        f"- [x] [STEP-1] implement banking integration ({sha})\n"
    )
    rc = diff_vs_claim.main(str(tmp_path))
    assert rc == 0  # soft
    out = capsys.readouterr().out
    assert "warn" in out.lower()
    assert "STEP-1" in out
