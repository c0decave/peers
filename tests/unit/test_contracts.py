"""Unit tests for peers_ctl.contracts (frozen contracts for implement-mode).

Covers Task 1.2: SHA-pinned acceptance.sh / e2e.sh / PLAN.original.md
under .peers/ with hash-chained audit log for legitimate amendments.
"""
from __future__ import annotations

import hashlib
import re
import stat
from pathlib import Path

import pytest

from peers_ctl.contracts import (
    ContractsMismatch,
    amend_acceptance,
    verify_contracts,
    write_frozen_contracts,
)


_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+\-]\d{4}$")


def _plan_dir(tmp_path: Path) -> Path:
    p = tmp_path / ".peers"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _make(plan_dir: Path, *, e2e: str | None = None) -> None:
    write_frozen_contracts(
        plan_dir,
        acceptance="pytest tests/acceptance/",
        e2e=e2e,
        plan_md_content="# Feature\n\n## Meta\nacceptance: pytest\n",
    )


def _chmod_writable(path: Path) -> None:
    """Restore owner write so tests can tamper / clean up."""
    path.chmod(0o644)


def test_write_then_verify_clean(tmp_path):
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir, e2e="playwright test e2e/")
    # Should not raise
    verify_contracts(plan_dir)


def test_write_without_e2e(tmp_path):
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir, e2e=None)
    assert (plan_dir / "contracts" / "acceptance.sh").is_file()
    assert not (plan_dir / "contracts" / "e2e.sh").exists()
    # Empty string should also be treated as no e2e
    other = tmp_path / "other" / ".peers"
    other.mkdir(parents=True)
    write_frozen_contracts(
        other, acceptance="pytest", e2e="", plan_md_content="# x\n",
    )
    assert not (other / "contracts" / "e2e.sh").exists()


def test_write_with_e2e(tmp_path):
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir, e2e="playwright test e2e/")
    e2e_path = plan_dir / "contracts" / "e2e.sh"
    assert e2e_path.is_file()
    txt = e2e_path.read_text(encoding="utf-8")
    assert txt == "#!/bin/sh\nset -e\nplaywright test e2e/\n"


def test_files_are_read_only_mode_0444(tmp_path):
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir, e2e="playwright test e2e/")
    for rel in (
        Path("contracts/acceptance.sh"),
        Path("contracts/e2e.sh"),
        Path("PLAN.original.md"),
    ):
        p = plan_dir / rel
        mode = oct(p.stat().st_mode & 0o777)
        assert mode == "0o444", f"{rel} mode={mode}"


def test_modification_detected_on_acceptance(tmp_path):
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir)
    acc = plan_dir / "contracts" / "acceptance.sh"
    _chmod_writable(acc)
    acc.write_text("#!/bin/sh\nset -e\nrm -rf /\n", encoding="utf-8")
    with pytest.raises(ContractsMismatch) as ei:
        verify_contracts(plan_dir)
    assert "tampered" in str(ei.value)
    assert "acceptance.sh" in str(ei.value)


def test_modification_detected_on_plan_original(tmp_path):
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir)
    plan = plan_dir / "PLAN.original.md"
    _chmod_writable(plan)
    plan.write_text("# Hacked\n", encoding="utf-8")
    with pytest.raises(ContractsMismatch) as ei:
        verify_contracts(plan_dir)
    assert "PLAN.original.md" in str(ei.value)


def test_missing_file_detected(tmp_path):
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir)
    acc = plan_dir / "contracts" / "acceptance.sh"
    _chmod_writable(acc)
    acc.unlink()
    with pytest.raises(ContractsMismatch) as ei:
        verify_contracts(plan_dir)
    assert "missing" in str(ei.value)
    assert "acceptance.sh" in str(ei.value)


def test_missing_contracts_sha_raises(tmp_path):
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir)
    (plan_dir / "contracts.sha").unlink()
    with pytest.raises(ContractsMismatch) as ei:
        verify_contracts(plan_dir)
    assert "contracts.sha missing" in str(ei.value)


def test_malformed_contracts_sha_raises_mismatch(tmp_path: Path) -> None:
    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir()
    write_frozen_contracts(
        plan_dir,
        "pytest",
        None,
        "# F\n## Meta\nsurfaces: [cli]\nacceptance: pytest\n## Steps\n- [ ] [STEP-1] x\n",
    )
    # Tamper: truncate contracts.sha to invalid JSON
    (plan_dir / "contracts.sha").write_text("{not valid json")
    with pytest.raises(ContractsMismatch, match="contracts.sha malformed"):
        verify_contracts(plan_dir)


def test_wrong_shape_contracts_sha_raises_mismatch(tmp_path: Path) -> None:
    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir()
    write_frozen_contracts(
        plan_dir,
        "pytest",
        None,
        "# F\n## Meta\nsurfaces: [cli]\nacceptance: pytest\n## Steps\n- [ ] [STEP-1] x\n",
    )
    # Tamper: rewrite as JSON list (wrong type)
    (plan_dir / "contracts.sha").write_text('["not", "a", "dict"]')
    with pytest.raises(ContractsMismatch, match="wrong shape"):
        verify_contracts(plan_dir)


def test_unknown_key_in_contracts_sha_raises_mismatch(tmp_path: Path) -> None:
    import json
    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir()
    write_frozen_contracts(
        plan_dir,
        "pytest",
        None,
        "# F\n## Meta\nsurfaces: [cli]\nacceptance: pytest\n## Steps\n- [ ] [STEP-1] x\n",
    )
    # Tamper: add an unknown key
    pins = json.loads((plan_dir / "contracts.sha").read_text())
    pins["weird_key"] = "deadbeef" * 8
    (plan_dir / "contracts.sha").write_text(json.dumps(pins))
    with pytest.raises(ContractsMismatch, match="unknown file"):
        verify_contracts(plan_dir)


def test_amend_re_pins_new_sha(tmp_path):
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir)
    amend_acceptance(
        plan_dir, "pytest tests/acceptance/ -x", reason="path typo",
    )
    # Verify still clean post-amend
    verify_contracts(plan_dir)
    # New file contains the new command
    acc = (plan_dir / "contracts" / "acceptance.sh").read_text(encoding="utf-8")
    assert acc == "#!/bin/sh\nset -e\npytest tests/acceptance/ -x\n"


def test_amend_logs_to_contracts_log(tmp_path):
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir)
    amend_acceptance(
        plan_dir, "pytest tests/integration/", reason="wrong path",
    )
    log = (plan_dir / "contracts.log").read_text(encoding="utf-8")
    assert "pytest tests/integration/" in log
    assert "reason: wrong path" in log
    assert "amend acceptance:" in log
    # ISO timestamp present
    parts = log.strip().split()
    # Format: <chain16> <iso> amend acceptance: <cmd> | reason: <reason>
    assert len(parts[0]) == 16
    assert _ISO_RE.match(parts[1]), f"bad iso ts: {parts[1]!r}"


def test_amend_hashchain_genesis(tmp_path):
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir)
    amend_acceptance(plan_dir, "pytest -q", reason="quiet")
    line = (plan_dir / "contracts.log").read_text(encoding="utf-8").rstrip("\n")
    chain_prefix, _, rest = line.partition(" ")
    # rest is: "<iso> amend acceptance: <cmd> | reason: <reason>"
    entry_text = rest + "\n"
    expected = hashlib.sha256(("genesis" + entry_text).encode("utf-8")).hexdigest()[:16]
    assert chain_prefix == expected


def test_amend_hashchain_continues(tmp_path):
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir)
    amend_acceptance(plan_dir, "pytest -q", reason="quiet")
    amend_acceptance(plan_dir, "pytest -v", reason="verbose")
    lines = (plan_dir / "contracts.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    prev_prefix = lines[0].split(" ", 1)[0]
    second_prefix, _, second_rest = lines[1].partition(" ")
    expected = hashlib.sha256(
        (prev_prefix + second_rest + "\n").encode("utf-8"),
    ).hexdigest()[:16]
    assert second_prefix == expected


def test_amend_preserves_read_only(tmp_path):
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir)
    amend_acceptance(plan_dir, "pytest -x", reason="fail-fast")
    acc = plan_dir / "contracts" / "acceptance.sh"
    mode = oct(acc.stat().st_mode & 0o777)
    assert mode == "0o444"
    # Also sanity-check that we can't write to it without chmod
    assert not (acc.stat().st_mode & stat.S_IWUSR)


def test_amend_does_not_touch_plan_original(tmp_path):
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir)
    import json
    sha_before = json.loads((plan_dir / "contracts.sha").read_text(encoding="utf-8"))
    plan_sha_before = sha_before["PLAN.original.md"]
    amend_acceptance(plan_dir, "pytest -x", reason="fail-fast")
    sha_after = json.loads((plan_dir / "contracts.sha").read_text(encoding="utf-8"))
    assert sha_after["PLAN.original.md"] == plan_sha_before
    # And the file content + mode are still pristine
    assert verify_contracts(plan_dir) is None


def test_amend_acceptance_atomic_on_corrupt_sha_BUG_201(tmp_path: Path):
    """BUG-201 reproducer: amend_acceptance must NOT wedge the contracts
    layout when contracts.sha is unreadable. Previously the function rewrote
    acceptance.sh BEFORE calling _load_pins(), so a corrupt contracts.sha
    raised ContractsMismatch with acceptance.sh already replaced and 0444;
    the pin still referenced the OLD sha so every subsequent
    verify_contracts() raised 'frozen file tampered: acceptance.sh' until a
    human manually rolled the script back. Expected behavior: on any failure
    to load pins, leave acceptance.sh untouched so the operator can recover
    by restoring contracts.sha alone.
    """
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir)
    acc_path = plan_dir / "contracts" / "acceptance.sh"
    original_body = acc_path.read_bytes()

    # Corrupt contracts.sha so _load_pins() raises ContractsMismatch.
    _chmod_writable(plan_dir / "contracts.sha")
    (plan_dir / "contracts.sha").write_text(
        "{not valid json", encoding="utf-8",
    )

    with pytest.raises(ContractsMismatch):
        amend_acceptance(plan_dir, "pytest -x", reason="fail-fast")

    # Acceptance must be untouched so an operator who restores contracts.sha
    # from backup recovers without a manual rollback of acceptance.sh.
    assert acc_path.read_bytes() == original_body, (
        "BUG-201: amend_acceptance overwrote acceptance.sh before "
        "_load_pins() detected the corrupt contracts.sha, wedging the "
        "contract layout permanently"
    )
