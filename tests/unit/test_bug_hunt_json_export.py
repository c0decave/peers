import json
import subprocess
from pathlib import Path

from peers.bug_hunt import summary_json


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True)


def test_summary_json_shape(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("x\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / "bug.txt").write_text("x\n")
    _git(repo, "add", "bug.txt")
    _git(repo, "commit", "-q", "-m", """BUG-001: overflow

## Bug-Report
{"id":"BUG-001","severity":"high","title":"overflow","cwe":"CWE-190","file":"src/a.c","function":"parse"}

Bug-Report: BUG-001
Peer: claude
""")

    parsed = json.loads(summary_json(repo))

    assert parsed["total"] == 1
    assert parsed["by_severity"]["high"] == 1
    assert parsed["by_cwe"]["CWE-190"] == 1
    assert parsed["reports"][0]["file"] == "src/a.c"
