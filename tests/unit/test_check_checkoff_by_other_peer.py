"""Test checkoff-by-other-peer check (Task 3.2)."""
from __future__ import annotations
import subprocess
from pathlib import Path

from peers.templates.modes.implement.checks import checkoff_by_other_peer


def _git(tmp_path: Path, *args: str, env=None):
    return subprocess.run(["git", "-C", str(tmp_path), *args],
                          capture_output=True, text=True, check=True, env=env)


def _init_git(tmp_path: Path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "commit.gpgsign", "false")


def _commit_as(tmp_path: Path, email: str, name: str, files: list[str], message: str) -> str:
    _git(tmp_path, "config", "user.email", email)
    _git(tmp_path, "config", "user.name", name)
    if files:
        _git(tmp_path, "add", *files)
    _git(tmp_path, "commit", "-q", "-m", message)
    return _git(tmp_path, "rev-parse", "HEAD").stdout.strip()


def _attest(tmp_path: Path, sha: str, peer: str):
    """Simulate the substrate writing its tick-HEAD-delta attribution as a
    ``refs/notes/peers-attest`` note (value = peer name) on ``sha``."""
    _git(tmp_path, "notes", "--ref=peers-attest", "add", "-f", "-m", peer, sha)


def _write_plan(tmp_path: Path, body: str):
    (tmp_path / "PLAN.md").write_text(f"""# F
## Meta
surfaces: [cli]
acceptance: pytest
## Steps
{body}
""")


def test_no_checked_steps_passes(tmp_path, capsys):
    _init_git(tmp_path)
    _write_plan(tmp_path, "- [ ] [STEP-1] todo\n  - touches: src/todo.py\n")
    _commit_as(tmp_path, "claude@p.local", "claude", ["PLAN.md"], "init")
    rc = checkoff_by_other_peer.main(str(tmp_path))
    assert rc == 0


def test_checkoff_by_different_peer_passes(tmp_path, capsys):
    _init_git(tmp_path)
    # claude implements
    src = tmp_path / "src" / "auth.py"
    src.parent.mkdir(parents=True)
    src.write_text("def auth(): pass")
    _write_plan(tmp_path, "- [ ] [STEP-1] add auth\n  - touches: src/auth.py\n")
    _commit_as(tmp_path, "claude@p.local", "claude", ["src/auth.py", "PLAN.md"], "step-1 impl")

    # codex reviews + checks off
    _write_plan(tmp_path, "- [x] [STEP-1] add auth\n  - touches: src/auth.py\n")
    _commit_as(tmp_path, "codex@p.local", "codex", ["PLAN.md"], "step-1 reviewed")

    rc = checkoff_by_other_peer.main(str(tmp_path))
    assert rc == 0


def test_checkoff_by_same_peer_fails(tmp_path, capsys):
    _init_git(tmp_path)
    src = tmp_path / "src" / "auth.py"
    src.parent.mkdir(parents=True)
    src.write_text("def auth(): pass")
    _write_plan(tmp_path, "- [ ] [STEP-1] add auth\n  - touches: src/auth.py\n")
    _commit_as(tmp_path, "claude@p.local", "claude", ["src/auth.py", "PLAN.md"], "step-1 impl")

    # claude checks off own work
    _write_plan(tmp_path, "- [x] [STEP-1] add auth\n  - touches: src/auth.py\n")
    _commit_as(tmp_path, "claude@p.local", "claude", ["PLAN.md"], "step-1 self-checkoff")

    rc = checkoff_by_other_peer.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "STEP-1" in out
    assert "claude@p.local" in out


# --- shared git identity: peers distinguished by the `Peer:` trailer -------
# In real runs both peers commit under one container git identity (e.g.
# `dash <user@localhost>`); the email comparison cannot tell them apart.
# The orchestrator writes a `Peer: <name>` trailer that the gate must honour.
_SHARED = "dash@localhost.local"


def test_shared_identity_distinct_peer_trailers_passes(tmp_path, capsys):
    _init_git(tmp_path)
    src = tmp_path / "src" / "auth.py"
    src.parent.mkdir(parents=True)
    src.write_text("def auth(): pass")
    _write_plan(tmp_path, "- [ ] [STEP-1] add auth\n  - touches: src/auth.py\n")
    _commit_as(tmp_path, _SHARED, "dash", ["src/auth.py", "PLAN.md"],
               "step-1 impl\n\nPeer: claude")
    _write_plan(tmp_path, "- [x] [STEP-1] add auth\n  - touches: src/auth.py\n")
    _commit_as(tmp_path, _SHARED, "dash", ["PLAN.md"],
               "step-1 reviewed\n\nPeer: codex")
    # Same author email throughout; only the trailer differs -> must PASS.
    rc = checkoff_by_other_peer.main(str(tmp_path))
    assert rc == 0


def test_shared_identity_same_peer_trailer_fails(tmp_path, capsys):
    _init_git(tmp_path)
    src = tmp_path / "src" / "auth.py"
    src.parent.mkdir(parents=True)
    src.write_text("def auth(): pass")
    _write_plan(tmp_path, "- [ ] [STEP-1] add auth\n  - touches: src/auth.py\n")
    _commit_as(tmp_path, _SHARED, "dash", ["src/auth.py", "PLAN.md"],
               "step-1 impl\n\nPeer: claude")
    _write_plan(tmp_path, "- [x] [STEP-1] add auth\n  - touches: src/auth.py\n")
    _commit_as(tmp_path, _SHARED, "dash", ["PLAN.md"],
               "step-1 self-checkoff\n\nPeer: claude")
    # Same peer implements AND checks off, despite it being one git identity.
    rc = checkoff_by_other_peer.main(str(tmp_path))
    assert rc == 1
    assert "peer:claude" in capsys.readouterr().out


def test_checkoff_without_touches_skipped(tmp_path, capsys):
    """A `trivial_step: true` step is exempt from the touches:
    requirement at parse time (Issue I4); the post-hoc gate has no
    files to anchor on either, so it must not flag a same-peer
    checkoff in that case."""
    _init_git(tmp_path)
    _write_plan(tmp_path, "- [ ] [STEP-1] trivial\n  - trivial_step: true\n")
    _commit_as(tmp_path, "claude@p.local", "claude", ["PLAN.md"], "init")
    _write_plan(tmp_path, "- [x] [STEP-1] trivial\n  - trivial_step: true\n")
    _commit_as(tmp_path, "claude@p.local", "claude", ["PLAN.md"], "self-checkoff but no touches")

    rc = checkoff_by_other_peer.main(str(tmp_path))
    assert rc == 0  # passes — can't enforce without touches


def test_multi_step_one_violation_fails(tmp_path, capsys):
    _init_git(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("a")
    (tmp_path / "src" / "b.py").write_text("b")
    _write_plan(tmp_path, """- [ ] [STEP-1] a
  - touches: src/a.py
- [ ] [STEP-2] b
  - touches: src/b.py
""")
    _commit_as(tmp_path, "claude@p.local", "claude", ["src/a.py", "src/b.py", "PLAN.md"], "init")

    # codex checks off step 1 (clean)
    _write_plan(tmp_path, """- [x] [STEP-1] a
  - touches: src/a.py
- [ ] [STEP-2] b
  - touches: src/b.py
""")
    _commit_as(tmp_path, "codex@p.local", "codex", ["PLAN.md"], "checkoff step-1")

    # claude self-checks step 2 (violation)
    _write_plan(tmp_path, """- [x] [STEP-1] a
  - touches: src/a.py
- [x] [STEP-2] b
  - touches: src/b.py
""")
    _commit_as(tmp_path, "claude@p.local", "claude", ["PLAN.md"], "claude self-checkoff step-2")

    rc = checkoff_by_other_peer.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "STEP-2" in out
    assert "STEP-1" not in out  # step-1 was clean


def test_no_plan_md_fails(tmp_path, capsys):
    _init_git(tmp_path)
    rc = checkoff_by_other_peer.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "PLAN.md" in out


def test_shared_identity_body_peer_spoof_does_not_mask_trailer(tmp_path, capsys):
    """BUG-140 regression: implementer cannot bypass the gate by smuggling a
    fake ``Peer: <other>`` line into the implementation commit body above the
    real bottom ``Peer: <self>`` trailer. Only the final trailer block's last
    Peer trailer should be honored — mirroring BUG-139's hook fix."""
    _init_git(tmp_path)
    src = tmp_path / "src" / "auth.py"
    src.parent.mkdir(parents=True)
    src.write_text("def auth(): pass")
    _write_plan(tmp_path, "- [ ] [STEP-1] add auth\n  - touches: src/auth.py\n")
    _commit_as(
        tmp_path,
        _SHARED,
        "dash",
        ["src/auth.py", "PLAN.md"],
        # Early fake `Peer: codex` line in prose; real `Peer: claude` trailer
        # at the bottom. A correct parser must attribute impl to claude.
        "step-1 impl\n\nSee Peer: codex notes below.\nPeer: codex\nrationale.\n\nPeer: claude",
    )
    _write_plan(tmp_path, "- [x] [STEP-1] add auth\n  - touches: src/auth.py\n")
    _commit_as(
        tmp_path,
        _SHARED,
        "dash",
        ["PLAN.md"],
        "step-1 self-checkoff\n\nPeer: claude",
    )
    # claude implemented AND checked off (same trailer); gate must fail.
    rc = checkoff_by_other_peer.main(str(tmp_path))
    assert rc == 1, "BUG-140: body Peer spoof allowed self-checkoff through gate"
    assert "peer:claude" in capsys.readouterr().out


def test_bug142_substrate_note_overrides_forged_impl_trailer(tmp_path, capsys):
    """BUG-142: a peer cannot forge attribution by stamping its OWN impl commit
    with the other peer's name as the only/final ``Peer:`` trailer.

    claude implements src/auth.py but writes ``Peer: codex`` as the sole final
    trailer (attributing the work to codex), then self-checkoffs as
    ``Peer: claude``. The trailer-only gate is fooled (impl=codex != checkoff=
    claude -> PASS). The substrate's tick-HEAD-delta note attributes the impl
    commit to the peer that actually produced it (claude), which the gate must
    honour over the forged trailer -> REJECT.
    """
    _init_git(tmp_path)
    src = tmp_path / "src" / "auth.py"
    src.parent.mkdir(parents=True)
    src.write_text("def auth(): pass")
    _write_plan(tmp_path, "- [ ] [STEP-1] add auth\n  - touches: src/auth.py\n")
    impl_sha = _commit_as(
        tmp_path,
        _SHARED,
        "dash",
        ["src/auth.py", "PLAN.md"],
        # Forged: claude attributes its own impl to codex as the ONLY trailer.
        "step-1 impl\n\nPeer: codex",
    )
    # Substrate observed this commit during claude's tick -> attests claude.
    _attest(tmp_path, impl_sha, "claude")

    _write_plan(tmp_path, "- [x] [STEP-1] add auth\n  - touches: src/auth.py\n")
    checkoff_sha = _commit_as(
        tmp_path,
        _SHARED,
        "dash",
        ["PLAN.md"],
        "step-1 self-checkoff\n\nPeer: claude",
    )
    _attest(tmp_path, checkoff_sha, "claude")

    rc = checkoff_by_other_peer.main(str(tmp_path))
    assert rc == 1, (
        "BUG-142: forged Peer trailer on the impl commit allowed self-checkoff; "
        "the substrate note attributing the impl to claude must override it"
    )
    assert "peer:claude" in capsys.readouterr().out


def test_shared_identity_missing_impl_peer_trailer_fails_closed(tmp_path, capsys):
    """BUG-141 regression: a commit without a final ``Peer:`` trailer should
    not be treated as a distinct principal from a peer-trailered checkoff when
    both commits share the same git author email."""
    _init_git(tmp_path)
    src = tmp_path / "src" / "auth.py"
    src.parent.mkdir(parents=True)
    src.write_text("def auth(): pass")
    _write_plan(tmp_path, "- [ ] [STEP-1] add auth\n  - touches: src/auth.py\n")
    _commit_as(
        tmp_path,
        _SHARED,
        "dash",
        ["src/auth.py", "PLAN.md"],
        "step-1 impl without final peer trailer",
    )
    _write_plan(tmp_path, "- [x] [STEP-1] add auth\n  - touches: src/auth.py\n")
    _commit_as(
        tmp_path,
        _SHARED,
        "dash",
        ["PLAN.md"],
        "step-1 self-checkoff\n\nPeer: claude",
    )

    rc = checkoff_by_other_peer.main(str(tmp_path))
    assert rc == 1, (
        "BUG-141: mixed email fallback vs peer identity allowed "
        "same-author self-checkoff through the post-hoc gate"
    )
    assert "email:dash@localhost.local" in capsys.readouterr().out
