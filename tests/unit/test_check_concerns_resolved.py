"""Test concerns-resolved check (Task 6.3)."""
from __future__ import annotations
from pathlib import Path

from peers.templates.modes.implement.checks import concerns_resolved


def _write_concerns(tmp_path: Path, content: str):
    (tmp_path / "CONCERNS.md").write_text(content)


def test_empty_concerns_fails(tmp_path, capsys):
    """Empty (or absent) CONCERNS.md is a pessimism-quota failure: the
    gate's purpose is the convergence anchor, and zero filed concerns
    across a multi-tick run is overwhelmingly rubber-stamping."""
    rc = concerns_resolved.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "pessimism" in out.lower() or "empty" in out.lower() or "concerns" in out.lower()


def test_all_addressed_passes(tmp_path, capsys):
    _write_concerns(tmp_path, """# Concerns

## Concern 1 — token refresh race
- raised-tick: 3
- raised-peer: codex
- description: race between refresh and logout
- status: addressed (commit: abc1234)

## Concern 2 — log rotation
- raised-tick: 5
- raised-peer: claude
- description: logs grow unbounded
- status: addressed (commit: def5678)
""")
    rc = concerns_resolved.main(str(tmp_path))
    assert rc == 0


def test_one_open_concern_fails(tmp_path, capsys):
    _write_concerns(tmp_path, """# Concerns

## Concern 1 — token refresh
- raised-tick: 3
- raised-peer: codex
- description: race issue
- status: addressed (commit: abc1234)

## Concern 2 — log rotation
- raised-tick: 5
- raised-peer: claude
- description: unbounded
- status: open
""")
    rc = concerns_resolved.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "Concern 2" in out or "log rotation" in out or "open" in out.lower()


def test_user_ack_passes(tmp_path, capsys):
    _write_concerns(tmp_path, """# Concerns

## Concern 1 — known limitation
- raised-tick: 4
- raised-peer: claude
- description: cache won't update on stale reads
- status: [USER-ACK] (reason: out of scope for this feature)
""")
    rc = concerns_resolved.main(str(tmp_path))
    assert rc == 0


def test_invalid_status_fails(tmp_path, capsys):
    _write_concerns(tmp_path, """# Concerns

## Concern 1 — foo
- raised-tick: 3
- raised-peer: codex
- description: x
- status: ???
""")
    rc = concerns_resolved.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "Concern 1" in out or "invalid" in out.lower()


def test_concern_without_status_fails(tmp_path, capsys):
    _write_concerns(tmp_path, """# Concerns

## Concern 1 — foo
- description: x
""")
    rc = concerns_resolved.main(str(tmp_path))
    assert rc == 1
