"""Test weasel-scan opt-in soft gate (Task 8.4).

Scans PLAN.md + DELIVERY.md for forbidden hedging phrases ("should
work", "appears to", etc.). Always exits 0 -- the gate is registered
``type: soft`` in goals.yaml, so the GoalEngine ignores the exit code.
"""
from __future__ import annotations

from peers.templates.modes.implement.checks import weasel_scan


def test_no_files_clean(tmp_path, capsys):
    """Neither PLAN.md nor DELIVERY.md present -- nothing to scan."""
    rc = weasel_scan.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out.lower()


def test_no_weasel_phrases_clean(tmp_path, capsys):
    """Files present with clean prose -- exit 0, clean."""
    (tmp_path / "PLAN.md").write_text(
        "# F\n## Meta\nsurfaces: [cli]\nacceptance: pytest\n\n## Steps\n"
        "- [x] [STEP-1] added auth (abc1234)\n"
    )
    (tmp_path / "DELIVERY.md").write_text(
        "## [STEP-1] auth\n- **Commit:** abc1234\n- **Tests:** test_auth.py\n"
        "- **Justification:** verified by acceptance.sh exit 0.\n"
    )
    rc = weasel_scan.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out.lower()


def test_weasel_phrase_in_delivery_warns(tmp_path, capsys):
    """`should work` in DELIVERY.md -- soft warn (still exit 0)."""
    (tmp_path / "DELIVERY.md").write_text(
        "## [STEP-1] auth\n- **Justification:** it should work in production.\n"
    )
    rc = weasel_scan.main(str(tmp_path))
    assert rc == 0  # soft
    out = capsys.readouterr().out
    assert "should work" in out.lower()
    assert "warn" in out.lower()


def test_weasel_phrase_in_plan_warns(tmp_path, capsys):
    """`I think` in PLAN.md -- soft warn."""
    (tmp_path / "PLAN.md").write_text(
        "# F\n## Meta\nsurfaces: [cli]\nacceptance: pytest\n\n## Steps\n"
        "- [x] [STEP-1] I think this works (abc1234)\n"
    )
    rc = weasel_scan.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "i think" in out.lower()


def test_exit_zero_regardless_of_env_var(tmp_path, monkeypatch, capsys):
    """The legacy WEASEL_SCAN_STRICT env var no longer has any effect:
    soft gates always exit 0, since the GoalEngine routes them through
    per-peer JSON review and ignores the exit code entirely.
    """
    monkeypatch.setenv("WEASEL_SCAN_STRICT", "1")
    (tmp_path / "DELIVERY.md").write_text(
        "## [STEP-1] auth\n- **Justification:** probably fine, seems to work.\n"
    )
    rc = weasel_scan.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "warn" in out.lower()
    assert "probably" in out.lower()
