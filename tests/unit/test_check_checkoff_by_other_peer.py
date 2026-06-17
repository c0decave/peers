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


def _review_commit_as(tmp_path: Path, artifact: str, peer: str) -> str:
    """The reviewer signs off by making a substrate-attested
    ``peers-review: <artifact>`` commit (FU-2 independent-review mechanism)."""
    _git(tmp_path, "config", "user.email", _SHARED)
    _git(tmp_path, "config", "user.name", "dash")
    _git(tmp_path, "commit", "-q", "--allow-empty",
         "-m", f"peers-review: {artifact}\n\nLGTM")
    sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    _attest(tmp_path, sha, peer)
    return sha


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


def _co_impl_setup(tmp_path: Path) -> None:
    """STEP-1 touches two files: a.py last-authored by claude, b.py by codex
    (a co-implemented step). codex checks the step off."""
    _init_git(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("a")
    (tmp_path / "src" / "b.py").write_text("b")
    _write_plan(tmp_path,
                "- [ ] [STEP-1] co\n  - touches: src/a.py, src/b.py\n")
    _commit_as(tmp_path, _SHARED, "dash", ["src/a.py", "PLAN.md"],
               "impl a\n\nPeer: claude")
    _commit_as(tmp_path, _SHARED, "dash", ["src/b.py"], "impl b\n\nPeer: codex")
    _write_plan(tmp_path,
                "- [x] [STEP-1] co\n  - touches: src/a.py, src/b.py\n")
    _commit_as(tmp_path, _SHARED, "dash", ["PLAN.md"], "checkoff\n\nPeer: codex")


def test_co_impl_step_without_independent_review_fails(tmp_path, capsys):
    # BUG-009 guard: codex checked off; b.py is codex's own file with no
    # independent reviewer -> self-bless -> still a violation (the rule is
    # preserved per-file, not silently relaxed).
    _co_impl_setup(tmp_path)
    rc = checkoff_by_other_peer.main(str(tmp_path))
    assert rc == 1
    assert "src/b.py" in capsys.readouterr().out


def test_co_impl_step_passes_with_independent_review_commit(tmp_path, capsys):
    # BUG-009 fix (A2) / FU-2: the co-implemented step converges once the OTHER
    # peer signs an independent review of the self-authored file. a.py is
    # reviewed by the checkoff (claude!=codex); b.py (impl+checkoff both codex)
    # is reviewed via claude's substrate-attested `peers-review: src/b.py`
    # commit -> every file independently reviewed -> PASS.
    _co_impl_setup(tmp_path)
    _review_commit_as(tmp_path, "src/b.py", "claude")
    rc = checkoff_by_other_peer.main(str(tmp_path))
    assert rc == 0


def test_co_impl_forged_log_entry_does_not_bless(tmp_path, capsys):
    # FU-2 security regression: the OLD escape trusted a justifications.log
    # reviewer field (agent-authored free text). A forged entry naming the
    # other peer must NO LONGER grant the escape — the log is not the
    # mechanism; only an attested peers-review commit is.
    from peers_ctl.justifications import append_justification
    _co_impl_setup(tmp_path)
    append_justification(tmp_path / ".peers", "src/b.py", 1,
                         "forged self-bless", "claude")
    rc = checkoff_by_other_peer.main(str(tmp_path))
    assert rc == 1
    assert "src/b.py" in capsys.readouterr().out


def test_co_impl_self_review_commit_does_not_bless(tmp_path, capsys):
    # FU-2 sad: codex cannot review its OWN b.py — a peers-review commit
    # attested to the implementer (codex) is excluded from independent review.
    _co_impl_setup(tmp_path)
    _review_commit_as(tmp_path, "src/b.py", "codex")
    rc = checkoff_by_other_peer.main(str(tmp_path))
    assert rc == 1
    assert "src/b.py" in capsys.readouterr().out


def test_co_impl_multi_author_self_review_rejected(tmp_path, capsys):
    # CRITICAL (adversarial review): claude authors src/b.py; codex makes a
    # trivial edit (becoming the last editor) and checks off; claude — the
    # ORIGINAL author — self-reviews. Excluding only the last editor (codex)
    # would let claude self-bless. Excluding ALL attested file authors rejects
    # it.
    _init_git(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "b.py").write_text("b\n")
    _write_plan(tmp_path, "- [ ] [STEP-1] x\n  - touches: src/b.py\n")
    s_impl = _commit_as(tmp_path, _SHARED, "dash", ["src/b.py", "PLAN.md"],
                        "impl b\n\nPeer: claude")
    _attest(tmp_path, s_impl, "claude")
    # codex trivially edits b.py → becomes the last editor before checkoff
    (tmp_path / "src" / "b.py").write_text("b\nedited\n")
    s_edit = _commit_as(tmp_path, _SHARED, "dash", ["src/b.py"],
                        "tweak\n\nPeer: codex")
    _attest(tmp_path, s_edit, "codex")
    _write_plan(tmp_path, "- [x] [STEP-1] x\n  - touches: src/b.py\n")
    s_co = _commit_as(tmp_path, _SHARED, "dash", ["PLAN.md"],
                      "checkoff\n\nPeer: codex")
    _attest(tmp_path, s_co, "codex")
    _review_commit_as(tmp_path, "src/b.py", "claude")  # original author self-reviews
    rc = checkoff_by_other_peer.main(str(tmp_path))
    assert rc == 1
    assert "src/b.py" in capsys.readouterr().out


def test_co_impl_review_commit_for_other_file_does_not_bless(tmp_path, capsys):
    # FU-2 edge: an attested review of a DIFFERENT file does not satisfy the
    # review requirement for src/b.py (artifact binding is exact).
    _co_impl_setup(tmp_path)
    _review_commit_as(tmp_path, "src/a.py", "claude")  # wrong file
    rc = checkoff_by_other_peer.main(str(tmp_path))
    assert rc == 1
    assert "src/b.py" in capsys.readouterr().out


def test_reattest_escapes_latch(tmp_path, capsys):
    # BUG-007 fix: a wrong (self) checkoff can be corrected by re-attestation.
    # claude self-checks-off (FAIL), then the step is unchecked and codex
    # re-checks-off. The gate must honour the LATEST transition (codex) -> PASS.
    _init_git(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("a")
    _write_plan(tmp_path, "- [ ] [STEP-1] x\n  - touches: src/a.py\n")
    _commit_as(tmp_path, _SHARED, "dash", ["src/a.py", "PLAN.md"],
               "impl a\n\nPeer: claude")
    _write_plan(tmp_path, "- [x] [STEP-1] x\n  - touches: src/a.py\n")
    _commit_as(tmp_path, _SHARED, "dash", ["PLAN.md"],
               "self checkoff\n\nPeer: claude")
    # uncheck, then the other peer re-attests the checkoff
    _write_plan(tmp_path, "- [ ] [STEP-1] x\n  - touches: src/a.py\n")
    _commit_as(tmp_path, _SHARED, "dash", ["PLAN.md"], "uncheck\n\nPeer: codex")
    _write_plan(tmp_path, "- [x] [STEP-1] x\n  - touches: src/a.py\n")
    _commit_as(tmp_path, _SHARED, "dash", ["PLAN.md"],
               "re-checkoff\n\nPeer: codex")
    rc = checkoff_by_other_peer.main(str(tmp_path))
    assert rc == 0


def test_signers_for_file_returns_file_level_reviewers(tmp_path):
    from peers_ctl.justifications import append_justification, signers_for_file
    pd = tmp_path / ".peers"
    append_justification(pd, "src/b.py", 1, "r1", "claude")
    append_justification(pd, "src/b.py", 9, "r2", "claude")
    append_justification(pd, "src/c.py", 1, "r3", "codex")
    assert signers_for_file(pd, "src/b.py") == {"claude"}
    assert signers_for_file(pd, "src/c.py") == {"codex"}
    assert signers_for_file(pd, "src/missing.py") == set()


# --- end-to-end via the production CLI path -------------------------------
# Every test above calls the gate's main() in-process. Production invokes it
# through `peers.cli run-check` (cmd_run_check resolves the script and runs it
# as a subprocess with cwd=repo). These scenario tests exercise that real path
# so a regression in resolution / cwd / exit-code forwarding is caught.

def _cli_run_check(tmp_path: Path, name: str):
    import os
    import sys
    src = str(Path(__file__).resolve().parents[2] / "src")
    env = {**os.environ, "PYTHONPATH": src}
    return subprocess.run(
        [sys.executable, "-m", "peers.cli", "run-check", name],
        cwd=str(tmp_path), env=env, capture_output=True, text=True,
    )


def test_cli_run_check_co_impl_self_bless_rejected(tmp_path):
    """Co-implemented step, codex blesses its own file with no independent
    review → the gate fails through the real CLI path (BUG-009 guard)."""
    _co_impl_setup(tmp_path)
    r = _cli_run_check(tmp_path, "checkoff_by_other_peer")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "src/b.py" in (r.stdout + r.stderr)


def test_cli_run_check_co_impl_independent_review_passes(tmp_path):
    """Same co-implemented step converges once the OTHER peer makes a
    substrate-attested `peers-review: src/b.py` commit (BUG-009 fix / FU-2),
    proven through the real CLI path."""
    _co_impl_setup(tmp_path)
    _review_commit_as(tmp_path, "src/b.py", "claude")
    r = _cli_run_check(tmp_path, "checkoff_by_other_peer")
    assert r.returncode == 0, r.stdout + r.stderr


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
