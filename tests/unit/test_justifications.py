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
