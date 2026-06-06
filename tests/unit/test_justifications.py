"""Test justifications log (Task 5.2)."""
from __future__ import annotations

import pytest

from peers_ctl.justifications import (
    JustificationError,
    append_justification,
    is_justified,
    verify_log_chain,
)


def test_initially_no_justifications(tmp_path):
    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir()
    signed, signer = is_justified(plan_dir, "src/a.py", 42)
    assert signed is False
    assert signer is None


def test_append_then_query(tmp_path):
    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir()
    append_justification(
        plan_dir, "src/a.py", 42, "needs upstream fix", "codex@p.local",
    )
    signed, signer = is_justified(plan_dir, "src/a.py", 42)
    assert signed is True
    assert signer == "codex@p.local"


def test_unrelated_line_not_justified(tmp_path):
    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir()
    append_justification(plan_dir, "src/a.py", 42, "x", "codex@p.local")
    signed, _ = is_justified(plan_dir, "src/a.py", 43)
    assert signed is False
    signed, _ = is_justified(plan_dir, "src/b.py", 42)
    assert signed is False


def test_hashchain_genesis(tmp_path):
    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir()
    append_justification(plan_dir, "src/a.py", 1, "x", "codex")
    log = (plan_dir / "justifications.log").read_text()
    line = log.strip().splitlines()[0]
    prefix = line.split(" ", 1)[0]
    assert len(prefix) == 16
    int(prefix, 16)


def test_hashchain_continues(tmp_path):
    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir()
    append_justification(plan_dir, "src/a.py", 1, "first", "codex")
    append_justification(plan_dir, "src/b.py", 2, "second", "claude")
    log = (plan_dir / "justifications.log").read_text()
    lines = log.strip().splitlines()
    assert len(lines) == 2
    prefixes = [ln.split(" ", 1)[0] for ln in lines]
    assert prefixes[0] != prefixes[1]


def test_verify_log_chain_clean(tmp_path):
    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir()
    append_justification(plan_dir, "src/a.py", 1, "x", "codex")
    append_justification(plan_dir, "src/b.py", 2, "y", "claude")
    verify_log_chain(plan_dir)  # no exception


def test_verify_log_chain_tampered(tmp_path):
    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir()
    append_justification(plan_dir, "src/a.py", 1, "x", "codex")
    append_justification(plan_dir, "src/b.py", 2, "y", "claude")
    # Tamper: edit the middle line
    log_path = plan_dir / "justifications.log"
    lines = log_path.read_text().splitlines()
    lines[0] = lines[0].replace("src/a.py", "src/EVIL.py")
    log_path.write_text("\n".join(lines) + "\n")
    with pytest.raises(JustificationError):
        verify_log_chain(plan_dir)


def test_verify_log_chain_missing_file_ok(tmp_path):
    """Missing log file is fine -- just means no justifications yet."""
    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir()
    verify_log_chain(plan_dir)  # no exception


def test_append_justification_rejects_oversized_negative_line_edge(tmp_path):
    # edge: the line_number=0 boundary and negative values both reject
    # at append-time. line_number=1 is the smallest accepted value;
    # confirm both halves of the boundary so the contract is pinned.
    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir()
    with pytest.raises(JustificationError, match="positive int"):
        append_justification(plan_dir, "src/x.py", 0, "shortcut", "rev")
    with pytest.raises(JustificationError, match="positive int"):
        append_justification(plan_dir, "src/x.py", -5, "shortcut", "rev")
    # Smallest accepted boundary IS line 1.
    append_justification(plan_dir, "src/x.py", 1, "shortcut", "rev")
    signed, _ = is_justified(plan_dir, "src/x.py", 1)
    assert signed is True


def test_is_justified_skips_malformed_lines_in_otherwise_valid_log_edge(tmp_path):
    # edge: a malformed-but-non-empty line in the middle of the log
    # must NOT abort the query — `is_justified` is a best-effort lookup
    # and the gate caller verifies the chain separately.
    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir()
    append_justification(plan_dir, "src/a.py", 7, "ok", "rev")
    log_path = plan_dir / "justifications.log"
    # Inject one obviously-malformed line (missing reviewer/reason).
    log_path.write_text(log_path.read_text() + "deadbeefdeadbeef bogus\n")
    signed, signer = is_justified(plan_dir, "src/a.py", 7)
    assert signed is True
    assert signer == "rev"


def test_is_justified_refuses_symlinked_log_BUG_197(tmp_path):
    """BUG-197: `is_justified` must not read attacker-controlled content
    via a symlinked ``.peers/justifications.log``.

    A same-UID project peer can replace the log file with a symlink to
    any same-user readable file (e.g. a benign-looking forged entry
    they wrote into a world-readable tempfile) and currently
    ``is_justified`` would read external content as gate truth.

    Fail-closed behavior is acceptable as either ``(False, None)`` or
    a :class:`JustificationError` — both refuse the forged sign-off.
    """
    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir()
    # External attacker-controlled "log" that asserts src/a.py:42 is
    # blessed by codex. If the gate follows the symlink, it accepts
    # this forged claim.
    forged = tmp_path / "forged.log"
    forged.write_text(
        "deadbeefdeadbeef src/a.py:42 codex@evil.test fake-signoff\n",
    )
    (plan_dir / "justifications.log").symlink_to(forged)

    try:
        signed, signer = is_justified(plan_dir, "src/a.py", 42)
    except (JustificationError, OSError):
        return  # raising is the strictest fail-closed behavior
    assert signed is False, (
        f"followed symlink and accepted forged sign-off from {signer}"
    )


def test_verify_log_chain_refuses_symlinked_log_BUG_197(tmp_path):
    """BUG-197: chain verification must not be tricked by reading a
    symlinked log. Otherwise the chain could appear "clean" because
    the attacker authored a self-consistent external file."""
    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir()
    forged = tmp_path / "forged.log"
    forged.write_text("")  # empty = trivially "valid" by current impl
    (plan_dir / "justifications.log").symlink_to(forged)

    with pytest.raises(JustificationError):
        verify_log_chain(plan_dir)


def test_append_justification_refuses_symlinked_log_BUG_197(tmp_path):
    """BUG-197: append must not follow a symlinked log and write into
    a same-user writable file outside the plan_dir."""
    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir()
    victim = tmp_path / "victim.log"
    victim.write_text("untouched\n")
    (plan_dir / "justifications.log").symlink_to(victim)

    with pytest.raises((JustificationError, OSError)):
        append_justification(plan_dir, "src/a.py", 1, "x", "rev")
    assert victim.read_text() == "untouched\n", (
        "append followed symlink and appended to external victim"
    )


def test_justifications_happy_after_BUG_197_fix(tmp_path):
    """Sanity: with no symlink trickery, append → is_justified →
    verify_log_chain still round-trips post-fix. Guards against an
    over-eager hardening from breaking the no-symlink happy path."""
    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir()
    append_justification(plan_dir, "src/x.py", 9, "fix later", "rev")
    signed, signer = is_justified(plan_dir, "src/x.py", 9)
    assert signed is True
    assert signer == "rev"
    verify_log_chain(plan_dir)
