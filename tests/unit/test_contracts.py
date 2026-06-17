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


def test_write_frozen_contracts_handles_empty_plan_md_edge(tmp_path):
    # edge: an empty PLAN.md body is a legitimate boundary — the SHA is
    # of empty bytes, the file is still 0444, and verify_contracts must
    # treat it as clean.
    plan_dir = _plan_dir(tmp_path)
    write_frozen_contracts(
        plan_dir, acceptance="true", e2e=None, plan_md_content="",
    )
    plan_orig = plan_dir / "PLAN.original.md"
    assert plan_orig.read_bytes() == b""
    assert (plan_orig.stat().st_mode & 0o777) == 0o444
    verify_contracts(plan_dir)


def test_write_frozen_contracts_handles_unicode_plan_content_edge(tmp_path):
    # edge: a PLAN.md with unicode characters must round-trip through the
    # frozen layout and through verify_contracts unchanged (utf-8 encode
    # vs. read bytes must agree on the sha256).
    plan_dir = _plan_dir(tmp_path)
    body = "# Feature\n\n## Meta\nacceptance: pytest\n\n— Übersicht 🚀\n"
    write_frozen_contracts(
        plan_dir, acceptance="true", e2e=None, plan_md_content=body,
    )
    verify_contracts(plan_dir)
    plan_orig = plan_dir / "PLAN.original.md"
    assert plan_orig.read_text(encoding="utf-8") == body


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


def test_deep_malformed_contracts_sha_raises_mismatch_BUG_516(
    tmp_path: Path,
) -> None:
    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir()
    write_frozen_contracts(
        plan_dir,
        "pytest",
        None,
        "# F\n## Meta\nsurfaces: [cli]\nacceptance: pytest\n## Steps\n- [ ] [STEP-1] x\n",
    )
    (plan_dir / "contracts.sha").write_text("[" * 10000)

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
    # write_frozen_contracts seeds the log with an init entry
    # using "genesis" as the previous chain value; the first amend then
    # chains from that init entry's prefix, not from "genesis".
    amend_acceptance(plan_dir, "pytest -q", reason="quiet")
    lines = (plan_dir / "contracts.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2, lines
    init_prefix, _, init_rest = lines[0].partition(" ")
    expected_init = hashlib.sha256(
        ("genesis" + init_rest + "\n").encode("utf-8"),
    ).hexdigest()[:16]
    assert init_prefix == expected_init
    amend_prefix, _, amend_rest = lines[1].partition(" ")
    expected_amend = hashlib.sha256(
        (init_prefix + amend_rest + "\n").encode("utf-8"),
    ).hexdigest()[:16]
    assert amend_prefix == expected_amend


def test_amend_hashchain_continues(tmp_path):
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir)
    amend_acceptance(plan_dir, "pytest -q", reason="quiet")
    amend_acceptance(plan_dir, "pytest -v", reason="verbose")
    lines = (plan_dir / "contracts.log").read_text(encoding="utf-8").splitlines()
    # init + two amends.
    assert len(lines) == 3, lines
    prev_prefix = lines[1].split(" ", 1)[0]
    third_prefix, _, third_rest = lines[2].partition(" ")
    expected = hashlib.sha256(
        (prev_prefix + third_rest + "\n").encode("utf-8"),
    ).hexdigest()[:16]
    assert third_prefix == expected


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


# --- BUG-157: contract writes must refuse symlinked leaves ---------------

def test_amend_acceptance_refuses_symlinked_acceptance_sh(tmp_path):
    """BUG-157: if a peer pre-replaces acceptance.sh with a symlink to an
    outside same-user writable file, amend_acceptance must refuse to write
    through the symlink. The victim's content must be unchanged."""
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir)

    acc_path = plan_dir / "contracts" / "acceptance.sh"
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched\n", encoding="utf-8")

    # Replace acceptance.sh with a symlink to the victim.
    _chmod_writable(acc_path)
    acc_path.unlink()
    acc_path.symlink_to(victim)

    with pytest.raises(OSError, match="symlink"):
        amend_acceptance(plan_dir, "pytest -x", reason="symlink-attack")

    # Victim must not have been overwritten by the amend payload.
    assert victim.read_text() == "untouched\n"


def test_write_frozen_contracts_refuses_symlinked_plan_original(tmp_path):
    """BUG-157 happy-path: write_frozen_contracts must also refuse to
    follow a pre-planted symlink on its initial write."""
    plan_dir = _plan_dir(tmp_path)
    victim = tmp_path / "victim.md"
    victim.write_text("KEEP ME\n", encoding="utf-8")
    (plan_dir / "PLAN.original.md").symlink_to(victim)

    with pytest.raises(OSError, match="symlink"):
        write_frozen_contracts(
            plan_dir,
            acceptance="pytest", e2e=None,
            plan_md_content="# new plan\n",
        )
    assert victim.read_text() == "KEEP ME\n"


def test_amend_acceptance_refuses_chmod_through_symlink(tmp_path):
    """BUG-157 edge: even before the write, the pre-chmod step must not
    follow a symlink — the hardened impl rejects with OSError on lstat
    before touching the victim's mode."""
    import os as _os
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir)

    acc_path = plan_dir / "contracts" / "acceptance.sh"
    victim = tmp_path / "victim.txt"
    victim.write_text("x\n", encoding="utf-8")
    _os.chmod(victim, 0o600)

    _chmod_writable(acc_path)
    acc_path.unlink()
    acc_path.symlink_to(victim)

    original_mode = _os.stat(victim).st_mode & 0o777

    with pytest.raises(OSError, match="symlink"):
        amend_acceptance(plan_dir, "pytest -x", reason="symlink-chmod-attack")

    assert _os.stat(victim).st_mode & 0o777 == original_mode


# --- BUG-164: contracts.sha / contracts.log / pin writes harden -------------

def test_amend_refuses_symlinked_contracts_sha_BUG_188(tmp_path):
    """BUG-188: _load_pins must reject a symlinked contracts.sha BEFORE
    amend_acceptance mutates acceptance.sh. Previously read_text followed
    the link, _load_pins succeeded, _write_read_only rewrote acceptance.sh,
    and only then _save_pins (via write_text_no_symlink) raised OSError —
    leaving the frozen layout half-mutated: contracts.sha unchanged but
    pointing at the OLD sha, while acceptance.sh has the NEW body.
    Recovery required manual rollback. The fix rejects up front via
    lstat in _load_pins, propagating an OSError so the operator sees the
    specific safe-IO refusal.
    """
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir)
    acc_path = plan_dir / "contracts" / "acceptance.sh"
    original_body = acc_path.read_bytes()

    # contracts.sha is a symlink to a valid JSON file. The link target's
    # body would have made _load_pins succeed before the BUG-188 fix.
    sha_path = plan_dir / "contracts.sha"
    backup = tmp_path / "backup-sha.json"
    backup.write_text(sha_path.read_text(), encoding="utf-8")
    _chmod_writable(sha_path)
    sha_path.unlink()
    sha_path.symlink_to(backup)

    with pytest.raises(OSError, match="symlink"):
        amend_acceptance(plan_dir, "pytest -x", reason="symlinked-sha")

    assert acc_path.read_bytes() == original_body, (
        "BUG-188: amend_acceptance must not rewrite acceptance.sh "
        "when contracts.sha is a symlink — partial mutation wedges "
        "the frozen layout"
    )


def test_amend_refuses_hardlinked_contracts_sha_BUG_188(tmp_path):
    """BUG-188 edge: contracts.sha as a hardlink to another file gets
    the same fail-fast guard so amend_acceptance does not mutate the
    pin and leave the frozen layout drifting from the log state."""
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir)
    acc_path = plan_dir / "contracts" / "acceptance.sh"
    original_body = acc_path.read_bytes()

    sha_path = plan_dir / "contracts.sha"
    twin = tmp_path / "twin-sha.json"
    twin.write_text(sha_path.read_text(), encoding="utf-8")
    _chmod_writable(sha_path)
    sha_path.unlink()
    import os as _os
    _os.link(twin, sha_path)

    with pytest.raises(OSError, match="hard-linked"):
        amend_acceptance(plan_dir, "pytest -x", reason="hardlinked-sha")
    assert acc_path.read_bytes() == original_body
    # Twin must not have been written through.
    assert twin.read_text() == sha_path.read_text()


def test_amend_refuses_hardlinked_acceptance_BUG_164(tmp_path):
    """BUG-164: _write_read_only opens with O_TRUNC before checking
    st_nlink, so a hardlinked contract leaf gets truncated even
    though we then bail. The fix must check nlink BEFORE any
    destructive open."""
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir)
    acc_path = plan_dir / "contracts" / "acceptance.sh"
    victim = tmp_path / "victim_hardlinked.txt"
    victim.write_text("PROTECT ME\n", encoding="utf-8")

    _chmod_writable(acc_path)
    acc_path.unlink()
    import os as _os
    _os.link(victim, acc_path)
    assert acc_path.stat().st_nlink == 2

    with pytest.raises(OSError):
        amend_acceptance(plan_dir, "pytest -x", reason="hardlink-attack")
    # Victim must not have been clobbered to empty / contents preserved.
    assert victim.read_text() == "PROTECT ME\n"


def test_amend_refuses_symlinked_contracts_sha_BUG_164(tmp_path):
    """BUG-164: contracts.sha is written via Path.write_text() which
    follows symlinks. A pre-planted symlink must be refused."""
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir)
    sha_path = plan_dir / "contracts.sha"
    victim = tmp_path / "victim_sha.json"
    victim.write_text("{}\n", encoding="utf-8")

    sha_path.unlink()
    sha_path.symlink_to(victim)
    with pytest.raises(OSError):
        amend_acceptance(plan_dir, "pytest -x", reason="sha-symlink-attack")
    assert victim.read_text() == "{}\n"


def test_amend_refuses_symlinked_contracts_log_BUG_164(tmp_path):
    """BUG-164: contracts.log read+append goes through Path.open
    which follows symlinks. A symlink there can redirect or poison
    the audit chain."""
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir)
    log_path = plan_dir / "contracts.log"
    victim = tmp_path / "victim_log.txt"
    victim.write_text("attacker prev\n", encoding="utf-8")

    # write_frozen_contracts now seeds contracts.log; remove
    # the seed entry so the attack scenario (pre-planted symlink) holds.
    log_path.unlink()
    log_path.symlink_to(victim)
    with pytest.raises(OSError):
        amend_acceptance(plan_dir, "pytest -x", reason="log-symlink-attack")
    # Victim untouched.
    assert victim.read_text() == "attacker prev\n"


# --- BUG-178: contracts.sha must be chain-bound to the audit log ------------

def test_verify_contracts_detects_silent_pin_rewrite_BUG_178(tmp_path):
    """BUG-178: a peer with write access to .peers/ can rewrite
    acceptance.sh and update contracts.sha to match in one shot.
    Without binding the pins to the hash-chained audit log,
    verify_contracts() treats this as clean — no amend was ever recorded.

    After the fix, write_frozen_contracts() seeds an initial chain entry
    that pins the genesis state and verify_contracts() cross-checks the
    current pins against the latest chain entry. A silent rewrite that
    does not extend the chain must now be rejected.
    """
    import json
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir)
    # The fix requires contracts.log to be seeded at init.
    log_path = plan_dir / "contracts.log"
    assert log_path.is_file(), (
        "BUG-178 prerequisite: write_frozen_contracts must seed contracts.log "
        "with an init entry recording the genesis pin state"
    )
    # Attacker rewrites acceptance.sh and updates contracts.sha to match.
    acc_path = plan_dir / "contracts" / "acceptance.sh"
    _chmod_writable(acc_path)
    new_body = "#!/bin/sh\nset -e\necho FAKE PASS\n"
    acc_path.write_text(new_body, encoding="utf-8")

    sha_path = plan_dir / "contracts.sha"
    _chmod_writable(sha_path)
    pins = json.loads(sha_path.read_text(encoding="utf-8"))
    pins["acceptance.sh"] = hashlib.sha256(
        new_body.encode("utf-8"),
    ).hexdigest()
    sha_path.write_text(
        json.dumps(pins, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # Silent pin-rewrite (no amend log entry) must be caught.
    with pytest.raises(ContractsMismatch):
        verify_contracts(plan_dir)


def test_verify_contracts_detects_missing_contracts_log_BUG_178(tmp_path):
    """BUG-178: after the fix the audit log is part of the root of trust.
    Deleting contracts.log removes the chain anchor and must fail
    verification (so an attacker can't bypass chain-binding by simply
    removing the log)."""
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir)
    log_path = plan_dir / "contracts.log"
    # On the buggy code the init entry is never written, so the unlink
    # itself fails — the test still fails (TDD expectation).
    log_path.unlink()
    with pytest.raises(ContractsMismatch):
        verify_contracts(plan_dir)


def test_verify_contracts_detects_chain_break_BUG_178(tmp_path):
    """BUG-178: a tampered chain prefix on the latest log entry must be
    rejected. This is the post-fix counterpart to silent-pin-rewrite:
    even if the attacker also touches the log, a broken chain reveals
    the tamper."""
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir)
    log_path = plan_dir / "contracts.log"
    if not log_path.is_file():
        pytest.fail(
            "BUG-178: write_frozen_contracts did not seed contracts.log "
            "with the genesis chain entry",
        )
    _chmod_writable(log_path)
    lines = log_path.read_text(encoding="utf-8").splitlines()
    # Corrupt the chain prefix of the first (only) entry.
    prefix, _, rest = lines[0].partition(" ")
    lines[0] = ("0" * len(prefix)) + " " + rest
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ContractsMismatch):
        verify_contracts(plan_dir)


def test_amend_acceptance_records_pin_state_in_chain_BUG_178(tmp_path):
    """BUG-178: each amend entry must carry the post-amend pin-state so
    verify_contracts can confirm the chain encodes the current pins."""
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir)
    amend_acceptance(plan_dir, "pytest -q", reason="quiet")
    # Verify is clean immediately after a legitimate amend.
    verify_contracts(plan_dir)
    # And after a second amend.
    amend_acceptance(plan_dir, "pytest -v", reason="verbose")
    verify_contracts(plan_dir)


def test_verify_contracts_refuses_symlinked_pinned_file_BUG_211(tmp_path):
    """BUG-211 (sad): verify_contracts must refuse a pinned file that has
    been swapped for a symlink, even if the target's sha256 matches the
    pin. A silent follow defeats the BUG-178 chain by reopening the
    'silent bypass' the chain was supposed to close (TOCTOU between
    verify and the acceptance run completes the attack once the leaf
    has been redirected).
    """
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir)

    acc = plan_dir / "contracts" / "acceptance.sh"
    # Stash the original frozen content elsewhere, then redirect the
    # contracts/ leaf to it via a symlink. Hashes still match the pin
    # because the data is bit-identical, but the leaf is now follow-able
    # — and an attacker can flip the target between verify and execute.
    relocated = tmp_path / "stash" / "acceptance.sh"
    relocated.parent.mkdir(parents=True)
    relocated.write_bytes(acc.read_bytes())
    _chmod_writable(acc)
    acc.unlink()
    acc.symlink_to(relocated)

    with pytest.raises((ContractsMismatch, OSError)):
        verify_contracts(plan_dir)


def test_verify_contracts_refuses_symlinked_plan_original_BUG_211(tmp_path):
    """BUG-211 (sad, edge): also enforced on PLAN.original.md, which
    lives at plan_dir top-level (different code path than the contracts/
    subdir leaves but the same vulnerability class).
    """
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir)

    plan_orig = plan_dir / "PLAN.original.md"
    relocated = tmp_path / "stash_plan" / "PLAN.original.md"
    relocated.parent.mkdir(parents=True)
    relocated.write_bytes(plan_orig.read_bytes())
    _chmod_writable(plan_orig)
    plan_orig.unlink()
    plan_orig.symlink_to(relocated)

    with pytest.raises((ContractsMismatch, OSError)):
        verify_contracts(plan_dir)


def test_verify_contracts_happy_after_BUG_211_fix(tmp_path):
    """Happy regression-defence: legitimate regular-file pins continue
    to verify cleanly after the no-symlink hardening. Prevents the fix
    from over-reaching onto valid frozen layouts."""
    plan_dir = _plan_dir(tmp_path)
    _make(plan_dir, e2e="playwright test e2e/")
    verify_contracts(plan_dir)
