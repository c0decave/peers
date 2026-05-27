import subprocess
import sys
from pathlib import Path

SCRIPT = (Path(__file__).parent.parent.parent
          / "src" / "peers" / "templates" / "modes" / "audit" / "checks"
          / "verify_self_review.py")


def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, check=True,
                       capture_output=True, text=True)
    return r.stdout


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "README").write_text("x\n")
    _git(path, "add", "README")
    _git(path, "commit", "-q", "-m", "init")


def _run_check(cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=cwd, capture_output=True, text=True,
    )


def test_passes_when_handoff_has_self_review(tmp_path: Path):
    repo = tmp_path / "r"
    _init_repo(repo)
    (repo / "a").write_text("x")
    _git(repo, "add", "a")
    body = (
        "Add a\n\n"
        "## Self-Review\n"
        "Checked: nothing dramatic.\n\n"
        "Self-Review: pass\n"
        "Peer-Status: handoff\n"
        "Peer: claude\n"
    )
    _git(repo, "commit", "-q", "-m", body)
    r = _run_check(repo)
    assert r.returncode == 0, r.stderr


def test_duplicate_self_review_trailer_last_value_wins(tmp_path: Path):
    repo = tmp_path / "r"
    _init_repo(repo)
    (repo / "a").write_text("x")
    _git(repo, "add", "a")
    body = (
        "Add a\n\n"
        "## Self-Review\n"
        "Earlier draft failed, final review passed.\n\n"
        "Self-Review: fail\n"
        "Self-Review: pass\n"
        "Peer-Status: handoff\n"
        "Peer: claude\n"
    )
    _git(repo, "commit", "-q", "-m", body)
    r = _run_check(repo)
    assert r.returncode == 0, r.stderr


def test_fails_when_handoff_missing_trailer(tmp_path: Path):
    repo = tmp_path / "r"
    _init_repo(repo)
    (repo / "a").write_text("x")
    _git(repo, "add", "a")
    body = (
        "Add a\n\n"
        "## Self-Review\nChecked.\n\n"
        "Peer-Status: handoff\n"
        "Peer: claude\n"
    )
    _git(repo, "commit", "-q", "-m", body)
    r = _run_check(repo)
    assert r.returncode == 1
    assert "Self-Review" in r.stderr


def test_fails_when_handoff_missing_body_section(tmp_path: Path):
    repo = tmp_path / "r"
    _init_repo(repo)
    (repo / "a").write_text("x")
    _git(repo, "add", "a")
    body = (
        "Add a\n\nbody only\n\n"
        "Self-Review: pass\nPeer-Status: handoff\nPeer: claude\n"
    )
    _git(repo, "commit", "-q", "-m", body)
    r = _run_check(repo)
    assert r.returncode == 1


def test_fails_when_no_handoff_commit_at_all(tmp_path: Path):
    repo = tmp_path / "r"
    _init_repo(repo)
    r = _run_check(repo)
    assert r.returncode == 1
    assert "no handoff" in r.stderr.lower()
