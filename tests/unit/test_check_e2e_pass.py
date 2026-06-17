"""Test e2e-pass check (Task 2.3)."""
from __future__ import annotations
import json
from hashlib import sha256
from pathlib import Path

from peers.templates.modes.implement.checks import e2e_pass
from peers_ctl.contracts import _append_chain_entry, _INIT_EVENT, _pin_state_hash


def _make_e2e_setup(tmp_path: Path, e2e_body: str) -> Path:
    """Create project with .peers/contracts/{acceptance,e2e}.sh + contracts.sha."""
    plan_dir = tmp_path / ".peers"
    contracts = plan_dir / "contracts"
    contracts.mkdir(parents=True)
    acc = contracts / "acceptance.sh"
    acc.write_text("#!/bin/sh\nexit 0\n")
    acc.chmod(0o444)
    e2e = contracts / "e2e.sh"
    e2e.write_text(e2e_body)
    e2e.chmod(0o444)
    plan_orig = plan_dir / "PLAN.original.md"
    plan_orig.write_text("# F\n## Meta\nsurfaces: [web]\nacceptance: x\ne2e: y\n## Steps\n- [ ] [STEP-1] x\n")
    plan_orig.chmod(0o444)
    sha_map = {
        "acceptance.sh": sha256(acc.read_bytes()).hexdigest(),
        "e2e.sh": sha256(e2e.read_bytes()).hexdigest(),
        "PLAN.original.md": sha256(plan_orig.read_bytes()).hexdigest(),
    }
    (plan_dir / "contracts.sha").write_text(json.dumps(sha_map, indent=2))
    # chain-bind initial pin state in contracts.log.
    _append_chain_entry(plan_dir, _INIT_EVENT, "", _pin_state_hash(sha_map))
    return tmp_path


def _replace_contracts_sha_with_symlink(tmp_path: Path) -> None:
    sha_path = tmp_path / ".peers" / "contracts.sha"
    backup = tmp_path / "contracts-sha-backup.json"
    backup.write_text(sha_path.read_text(encoding="utf-8"), encoding="utf-8")
    sha_path.unlink()
    sha_path.symlink_to(backup)


def _make_non_e2e_setup(tmp_path: Path) -> Path:
    """Project with NO e2e.sh — should result in skip."""
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
    _append_chain_entry(plan_dir, _INIT_EVENT, "", _pin_state_hash(sha_map))
    return tmp_path


def test_e2e_pass_when_exit_zero(tmp_path, capsys):
    _make_e2e_setup(tmp_path, "#!/bin/sh\nexit 0\n")
    rc = e2e_pass.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out


def test_e2e_skip_when_no_e2e_sh(tmp_path, capsys):
    _make_non_e2e_setup(tmp_path)
    rc = e2e_pass.main(str(tmp_path))
    assert rc == 0  # skip is success
    out = capsys.readouterr().out
    assert "skipped" in out.lower()
    assert "non-UI" in out or "non-ui" in out.lower()


def test_e2e_fail_when_exit_nonzero(tmp_path, capsys):
    _make_e2e_setup(tmp_path, "#!/bin/sh\necho 'playwright: 3 tests failed'\nexit 1\n")
    rc = e2e_pass.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "exit 1" in out
    assert "playwright" in out


def test_e2e_tampered_detected(tmp_path, capsys):
    _make_e2e_setup(tmp_path, "#!/bin/sh\nexit 0\n")
    # tamper with e2e.sh
    e2e = tmp_path / ".peers" / "contracts" / "e2e.sh"
    e2e.chmod(0o644)
    e2e.write_text("#!/bin/sh\necho cheating\nexit 0\n")
    rc = e2e_pass.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "tampered" in out


def test_e2e_timeout(tmp_path, capsys):
    _make_e2e_setup(tmp_path, "#!/bin/sh\nsleep 30\n")
    rc = e2e_pass.main(str(tmp_path), timeout=1)
    assert rc == 1
    out = capsys.readouterr().out
    assert "timed out" in out


def test_e2e_unsafe_contracts_sha_reports_fail_without_traceback_BUG_521(
    tmp_path, capsys,
):
    _make_e2e_setup(tmp_path, "#!/bin/sh\nexit 0\n")
    _replace_contracts_sha_with_symlink(tmp_path)

    rc = e2e_pass.main(str(tmp_path))

    assert rc == 1
    out = capsys.readouterr().out
    assert "e2e-pass FAIL" in out
    assert "contracts.sha" in out
