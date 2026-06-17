"""Test no-shortcut-markers check (Task 5.1)."""
from __future__ import annotations

import subprocess
from pathlib import Path

from peers.templates.modes.implement.checks import no_shortcut_markers


def _setup(tmp_path: Path, src_files: dict[str, str]) -> Path:
    """Create src/ with given files, return project_dir."""
    src = tmp_path / "src"
    src.mkdir()
    for name, body in src_files.items():
        f = src / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    return tmp_path


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout


def _git_setup(tmp_path: Path, src_files: dict[str, str]) -> Path:
    """_setup + git init + initial commit (so FU-2 review commits resolve)."""
    proj = _setup(tmp_path, src_files)
    _git(proj, "init", "-q")
    _git(proj, "config", "commit.gpgsign", "false")
    _git(proj, "config", "user.email", "t@t")
    _git(proj, "config", "user.name", "t")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-q", "-m", "init")
    return proj


def _attested_review(repo: Path, artifact: str, peer: str) -> str:
    """The OTHER peer signs off via a substrate-attested peers-review commit."""
    _git(repo, "commit", "-q", "--allow-empty",
         "-m", f"peers-review: {artifact}\n\nLGTM")
    sha = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "notes", "--ref=peers-attest", "add", "-f", "-m", peer, sha)
    return sha


def test_clean_src_passes(tmp_path, capsys):
    _setup(tmp_path, {"a.py": "def f():\n    return 1\n"})
    rc = no_shortcut_markers.main(str(tmp_path))
    assert rc == 0


def test_todo_marker_fails(tmp_path, capsys):
    _setup(tmp_path, {"a.py": "def f():\n    # TODO: fix this\n    return 1\n"})
    rc = no_shortcut_markers.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "TODO" in out
    assert "a.py" in out


def test_fixme_marker_fails(tmp_path, capsys):
    _setup(tmp_path, {"a.py": "def f():\n    return 1  # FIXME later\n"})
    rc = no_shortcut_markers.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "FIXME" in out


def test_xxx_hack_placeholder_stub_fail(tmp_path, capsys):
    _setup(tmp_path, {
        "a.py": "# XXX: temp\n",
        "b.py": "# HACK around bug\n",
        "c.py": "# PLACEHOLDER for X\n",
        "d.py": "x = 'STUB'\n",
    })
    rc = no_shortcut_markers.main(str(tmp_path))
    assert rc == 1


def test_not_implemented_in_concrete_class_fails(tmp_path, capsys):
    _setup(tmp_path, {"a.py": """class Foo:
    def bar(self):
        raise NotImplementedError("subclass me")
"""})
    rc = no_shortcut_markers.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "NotImplementedError" in out


def test_not_implemented_in_abstract_class_allowed(tmp_path, capsys):
    _setup(tmp_path, {"a.py": """from abc import ABC, abstractmethod
class Foo(ABC):
    @abstractmethod
    def bar(self):
        raise NotImplementedError
"""})
    rc = no_shortcut_markers.main(str(tmp_path))
    assert rc == 0


def test_not_implemented_in_protocol_allowed(tmp_path, capsys):
    _setup(tmp_path, {"a.py": """from typing import Protocol
class Foo(Protocol):
    def bar(self):
        raise NotImplementedError
"""})
    rc = no_shortcut_markers.main(str(tmp_path))
    assert rc == 0


def test_justified_marker_passes_with_review_commit(tmp_path, capsys):
    # FU-2: the JUSTIFIED line is forgiven once an independent peer (codex)
    # signs off on src/a.py via a substrate-attested peers-review commit.
    proj = _git_setup(
        tmp_path,
        {"a.py": "def f():\n    pass  # TODO  # JUSTIFIED: waits on issue 42\n"},
    )
    _attested_review(proj, "src/a.py", "codex")
    rc = no_shortcut_markers.main(str(proj))
    assert rc == 0


def test_justified_marker_without_signoff_fails(tmp_path, capsys):
    _setup(tmp_path, {"a.py": "def f():\n    pass  # TODO  # JUSTIFIED: handwave\n"})
    # No justifications.log written
    rc = no_shortcut_markers.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "JUSTIFIED" in out or "unsigned" in out.lower() or "TODO" in out


def test_skips_tests_directory(tmp_path, capsys):
    """tests/ paths are not scanned -- only src/ matters for this gate."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def x(): pass\n")  # clean
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("# TODO: write tests\n")  # has TODO but in tests/
    rc = no_shortcut_markers.main(str(tmp_path))
    assert rc == 0  # tests/ TODO is fine


def test_skips_peer_template_check_implementations(tmp_path, capsys):
    """The peers repo's own policy templates name the markers they reject."""
    path = (
        tmp_path / "src" / "peers" / "templates" / "modes"
        / "implement" / "checks" / "no_shortcut_markers.py"
    )
    path.parent.mkdir(parents=True)
    path.write_text('MARKERS = ("TODO", "FIXME", "XXX", "HACK")\n')

    rc = no_shortcut_markers.main(str(tmp_path))

    assert rc == 0


def test_forged_log_entry_does_not_bless_FU_2(tmp_path, capsys):
    """FU-2 (supersedes BUG-173): a justifications.log entry no longer grants
    the escape at all — the forgeable, agent-authored log is not the mechanism.
    Only a substrate-attested peers-review commit forgives a JUSTIFIED marker,
    so a hand-written (even chain-valid-looking) entry waives nothing.
    """
    proj = _git_setup(
        tmp_path,
        {"a.py": "def f():\n    pass  # TODO  # JUSTIFIED: forged\n"},
    )
    plan_dir = proj / ".peers"
    plan_dir.mkdir()
    (plan_dir / "justifications.log").write_text(
        "0000000000000000 src/a.py:2 attacker forged reason\n",
    )
    rc = no_shortcut_markers.main(str(proj))
    assert rc == 1
    assert "src/a.py" in capsys.readouterr().out


def test_multi_author_laundering_does_not_bless_FU_2(tmp_path, capsys):
    # CRITICAL (adversarial review): A (claude) authors+justifies the marker on
    # line 2; B (codex) makes a trivial edit to ANOTHER line (becoming the
    # 'last editor'); A self-reviews. Excluding only the last editor (codex)
    # would let A self-bless — the guard must exclude the MARKER LINE's author.
    proj = _git_setup(
        tmp_path,
        {"a.py": "def f():\n    pass  # TODO  # JUSTIFIED: deferred\n"},
    )
    init = _git(proj, "rev-parse", "HEAD").strip()
    _git(proj, "notes", "--ref=peers-attest", "add", "-f", "-m", "claude", init)
    # codex appends a blank line (line 3) — does NOT touch the marker on line 2
    (proj / "src" / "a.py").write_text(
        "def f():\n    pass  # TODO  # JUSTIFIED: deferred\n\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-q", "-m", "codex trivial")
    edit = _git(proj, "rev-parse", "HEAD").strip()
    _git(proj, "notes", "--ref=peers-attest", "add", "-f", "-m", "codex", edit)
    _attested_review(proj, "src/a.py", "claude")  # A (marker author) self-reviews
    rc = no_shortcut_markers.main(str(proj))
    assert rc == 1
    assert "src/a.py" in capsys.readouterr().out


def test_coeditor_who_did_not_author_marker_can_review_FU_2(tmp_path, capsys):
    # complement (not over-strict): codex edited a NON-marker line, so codex is
    # NOT the marker's author and CAN review claude's justified marker.
    proj = _git_setup(
        tmp_path,
        {"a.py": "def f():\n    pass  # TODO  # JUSTIFIED: deferred\n"},
    )
    init = _git(proj, "rev-parse", "HEAD").strip()
    _git(proj, "notes", "--ref=peers-attest", "add", "-f", "-m", "claude", init)
    (proj / "src" / "a.py").write_text(
        "def f():\n    pass  # TODO  # JUSTIFIED: deferred\n\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-q", "-m", "codex trivial")
    edit = _git(proj, "rev-parse", "HEAD").strip()
    _git(proj, "notes", "--ref=peers-attest", "add", "-f", "-m", "codex", edit)
    _attested_review(proj, "src/a.py", "codex")  # codex (not marker author) reviews
    rc = no_shortcut_markers.main(str(proj))
    assert rc == 0


def test_self_review_does_not_bless_FU_2(tmp_path, capsys):
    """FU-2 sad: the file's own author cannot self-bless its shortcut — a
    peers-review commit attested to the author (codex) is excluded."""
    proj = _git_setup(
        tmp_path,
        {"a.py": "def f():\n    pass  # TODO  # JUSTIFIED: self\n"},
    )
    # attest the initial commit (the author of src/a.py) to codex
    init = _git(proj, "rev-parse", "HEAD").strip()
    _git(proj, "notes", "--ref=peers-attest", "add", "-f", "-m", "codex", init)
    _attested_review(proj, "src/a.py", "codex")  # codex reviewing its own file
    rc = no_shortcut_markers.main(str(proj))
    assert rc == 1
    assert "src/a.py" in capsys.readouterr().out
