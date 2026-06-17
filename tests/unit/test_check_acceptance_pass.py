"""Test acceptance-pass check (Task 2.2)."""
from __future__ import annotations
import json
from hashlib import sha256
from pathlib import Path

from peers.templates.modes.implement.checks import acceptance_pass
from peers_ctl.contracts import _append_chain_entry, _INIT_EVENT, _pin_state_hash


def _make_acceptance_setup(tmp_path: Path, script_body: str) -> Path:
    """Create minimal valid project: .peers/contracts/acceptance.sh + contracts.sha.

    Builds the layout by hand so each test can supply an arbitrary
    ``script_body`` (the public ``write_frozen_contracts`` wraps the
    user's command in a fixed shebang/header). The BUG-178 audit-log
    seed entry is then written via the internal ``_append_chain_entry``
    helper so ``verify_contracts`` sees a chain-bound pin state.
    """
    plan_dir = tmp_path / ".peers"
    contracts = plan_dir / "contracts"
    contracts.mkdir(parents=True)
    acc = contracts / "acceptance.sh"
    acc.write_text(script_body)
    acc.chmod(0o444)
    # Need PLAN.original.md and contracts.sha for verify_contracts to pass
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


def _replace_contracts_sha_with_symlink(tmp_path: Path) -> None:
    sha_path = tmp_path / ".peers" / "contracts.sha"
    backup = tmp_path / "contracts-sha-backup.json"
    backup.write_text(sha_path.read_text(encoding="utf-8"), encoding="utf-8")
    sha_path.unlink()
    sha_path.symlink_to(backup)


def test_acceptance_pass_exit_zero(tmp_path, capsys):
    _make_acceptance_setup(tmp_path, "#!/bin/sh\nexit 0\n")
    rc = acceptance_pass.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out


def test_acceptance_fail_exit_one(tmp_path, capsys):
    _make_acceptance_setup(tmp_path, "#!/bin/sh\necho 'test failed: xyz'\nexit 1\n")
    rc = acceptance_pass.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "exit 1" in out
    assert "test failed: xyz" in out


def test_acceptance_missing_acceptance_sh(tmp_path, capsys):
    rc = acceptance_pass.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "not found" in out


def test_acceptance_tampered_detected(tmp_path, capsys):
    _make_acceptance_setup(tmp_path, "#!/bin/sh\nexit 0\n")
    # tamper
    acc = tmp_path / ".peers" / "contracts" / "acceptance.sh"
    acc.chmod(0o644)
    acc.write_text("#!/bin/sh\necho cheating\nexit 0\n")
    rc = acceptance_pass.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "tampered" in out


def test_acceptance_timeout(tmp_path, capsys):
    _make_acceptance_setup(tmp_path, "#!/bin/sh\nsleep 30\n")
    rc = acceptance_pass.main(str(tmp_path), timeout=1)
    assert rc == 1
    out = capsys.readouterr().out
    assert "timed out" in out


def test_acceptance_output_truncation(tmp_path, capsys):
    # >20 lines of output: should be truncated marker in stdout
    script = "#!/bin/sh\n" + "\n".join(f"echo line_{i}" for i in range(50)) + "\nexit 1\n"
    _make_acceptance_setup(tmp_path, script)
    rc = acceptance_pass.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "line_49" in out  # last line included
    assert "line_0" not in out  # first lines truncated
    assert "truncated" in out.lower() or "..." in out  # truncation indicator


def test_acceptance_unsafe_contracts_sha_reports_fail_without_traceback_BUG_521(
    tmp_path, capsys,
):
    _make_acceptance_setup(tmp_path, "#!/bin/sh\nexit 0\n")
    _replace_contracts_sha_with_symlink(tmp_path)

    rc = acceptance_pass.main(str(tmp_path))

    assert rc == 1
    out = capsys.readouterr().out
    assert "acceptance-pass FAIL" in out
    assert "contracts.sha" in out
