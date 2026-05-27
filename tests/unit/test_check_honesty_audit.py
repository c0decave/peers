"""Test honesty-audit check (Task 6.4)."""
from __future__ import annotations
from pathlib import Path

from peers.templates.modes.implement.checks import honesty_audit


def _write_audit(tmp_path: Path, content: str):
    (tmp_path / "HONESTY_AUDIT.md").write_text(content)


_COMPLETE_AUDIT = """# Honesty Audit

## claude

### Weakest part
Token refresh path has no exponential backoff on auth-server failures
during peak load conditions.

### Likely uncaught bug
Sessions don't get invalidated on password change because we use
in-memory cache without an external invalidation signal.

### Skipped or shortcut
No mutation testing was run since it was opt-in and we deferred to
the next iteration.

## codex

### Weakest part
Middleware regex for header parsing is brittle on multi-line headers
and might mishandle continuation-style RFC 822 folds.

### Likely uncaught bug
Race between session expiry and refresh can produce a 401 response
even when the user has fresh credentials.

### Skipped or shortcut
Coverage for the rate-limit-exceeded path is missing — added a TODO
but justified via PLAN.md.
"""


def test_complete_audit_passes(tmp_path, capsys):
    _write_audit(tmp_path, _COMPLETE_AUDIT)
    rc = honesty_audit.main(str(tmp_path))
    assert rc == 0


def test_missing_audit_file_fails(tmp_path, capsys):
    rc = honesty_audit.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "HONESTY_AUDIT.md" in out


def test_only_one_peer_section_fails(tmp_path, capsys):
    _write_audit(tmp_path, """# Honesty Audit

## claude

### Weakest part
something substantive here that is more than five words long obviously

### Likely uncaught bug
likely uncaught race condition under heavy load conditions

### Skipped or shortcut
mutation testing was deferred for later iteration
""")
    rc = honesty_audit.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "peer" in out.lower() or "section" in out.lower() or "claude" in out or "codex" in out


def test_missing_subsection_fails(tmp_path, capsys):
    _write_audit(tmp_path, """# Honesty Audit

## claude
### Weakest part
something substantive here that is more than five words long obviously
### Likely uncaught bug
likely uncaught race condition under heavy load conditions

## codex
### Weakest part
some substantive part here easily exceeds the minimum word threshold
### Likely uncaught bug
race condition between two database transactions on insert

### Skipped or shortcut
mutation testing skipped for now
""")  # claude is missing "Skipped or shortcut"
    rc = honesty_audit.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "Skipped" in out or "claude" in out


def test_trivial_content_fails(tmp_path, capsys):
    _write_audit(tmp_path, """# Honesty Audit

## claude
### Weakest part
none
### Likely uncaught bug
none
### Skipped or shortcut
none

## codex
### Weakest part
n/a
### Likely uncaught bug
n/a
### Skipped or shortcut
n/a
""")
    rc = honesty_audit.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "trivial" in out.lower() or "too short" in out.lower() or "words" in out.lower()


def test_extra_peer_sections_allowed(tmp_path, capsys):
    """A third peer (e.g. gemini) shouldn't cause a fail."""
    _write_audit(tmp_path, _COMPLETE_AUDIT + """

## gemini
### Weakest part
the test fixture handling for integration tests is verbose and repetitive

### Likely uncaught bug
edge case where two requests share the same nonce due to clock skew

### Skipped or shortcut
property-based fuzzing of input handlers not included in this iteration
""")
    rc = honesty_audit.main(str(tmp_path))
    assert rc == 0


def test_case_insensitive_subsection_headers(tmp_path, capsys):
    """Accept slight casing variations in section names."""
    _write_audit(tmp_path, """# Honesty Audit

## claude
### Weakest Part
substantive content here easily more than five words obviously yes
### Likely Uncaught Bug
substantive content here easily more than five words obviously yes
### Skipped Or Shortcut
substantive content here easily more than five words obviously yes

## codex
### Weakest Part
substantive content here easily more than five words obviously yes
### Likely Uncaught Bug
substantive content here easily more than five words obviously yes
### Skipped Or Shortcut
substantive content here easily more than five words obviously yes
""")
    rc = honesty_audit.main(str(tmp_path))
    assert rc == 0  # title-case OK


def test_empty_audit_file_fails(tmp_path, capsys):
    _write_audit(tmp_path, "")
    rc = honesty_audit.main(str(tmp_path))
    assert rc == 1
