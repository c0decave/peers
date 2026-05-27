"""Test contracts-unchanged check (Task 2.7)."""
from __future__ import annotations
import json
from hashlib import sha256
from pathlib import Path

from peers.templates.modes.implement.checks import contracts_unchanged


def _make_setup(tmp_path: Path) -> Path:
    """Create minimal valid frozen-contracts project layout."""
    plan_dir = tmp_path / ".peers"
    contracts = plan_dir / "contracts"
    contracts.mkdir(parents=True)
    acc = contracts / "acceptance.sh"
    acc.write_text("#!/bin/sh\nexit 0\n")
    acc.chmod(0o444)
    plan_orig = plan_dir / "PLAN.original.md"
    plan_orig.write_text("# F\n## Meta\nsurfaces: [cli]\nacceptance: x\n## Steps\n- [ ] [STEP-1] x\n")
    plan_orig.chmod(0o444)
    sha_map = {
        "acceptance.sh": sha256(acc.read_bytes()).hexdigest(),
        "PLAN.original.md": sha256(plan_orig.read_bytes()).hexdigest(),
    }
    (plan_dir / "contracts.sha").write_text(json.dumps(sha_map, indent=2))
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
