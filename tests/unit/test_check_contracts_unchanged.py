"""Test contracts-unchanged check (Task 2.7)."""
from __future__ import annotations
from pathlib import Path

from peers.templates.modes.implement.checks import contracts_unchanged
from peers_ctl.contracts import write_frozen_contracts


def _make_setup(tmp_path: Path) -> Path:
    """Create minimal valid frozen-contracts project layout.

    Uses write_frozen_contracts() so the layout (including the BUG-178
    audit-log seed entry) stays in sync with the writer code path.
    """
    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir(parents=True)
    write_frozen_contracts(
        plan_dir,
        acceptance="exit 0",
        e2e=None,
        plan_md_content=(
            "# F\n## Meta\nsurfaces: [cli]\nacceptance: x\n"
            "## Steps\n- [ ] [STEP-1] x\n"
        ),
    )
    return tmp_path


def test_clean_contracts_pass(tmp_path, capsys):
    _make_setup(tmp_path)
    rc = contracts_unchanged.main(str(tmp_path))
    assert rc == 0
    assert "clean" in capsys.readouterr().out


def test_tampered_acceptance_fails(tmp_path, capsys):
    _make_setup(tmp_path)
    acc = tmp_path / ".peers" / "contracts" / "acceptance.sh"
    acc.chmod(0o644)
    acc.write_text("evil\n")
    rc = contracts_unchanged.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "tampered" in out


def test_missing_contracts_sha_fails(tmp_path, capsys):
    _make_setup(tmp_path)
    (tmp_path / ".peers" / "contracts.sha").unlink()
    rc = contracts_unchanged.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "missing" in out.lower() or "contracts.sha" in out


def test_missing_acceptance_fails(tmp_path, capsys):
    _make_setup(tmp_path)
    (tmp_path / ".peers" / "contracts" / "acceptance.sh").chmod(0o644)
    (tmp_path / ".peers" / "contracts" / "acceptance.sh").unlink()
    rc = contracts_unchanged.main(str(tmp_path))
    assert rc == 1


def test_malformed_contracts_sha_fails(tmp_path, capsys):
    _make_setup(tmp_path)
    (tmp_path / ".peers" / "contracts.sha").write_text("{ not json")
    rc = contracts_unchanged.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "malformed" in out.lower()
