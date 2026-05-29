"""Frozen contracts for implement-mode (.peers/contracts/).

Task 1.2 of the implement-mode plan. When ``peers-ctl new --modes=implement
--plan PLAN.md`` initialises a project we freeze the acceptance command,
optional e2e command and a snapshot of PLAN.md so peers running in
containers can read them but cannot tamper:

    plan_dir/                  # the .peers/ dir of a project
    |-- PLAN.original.md       # mode 0444
    |-- contracts.sha          # {filename: hex_sha256} (mode 0644)
    |-- contracts.log          # hash-chained audit log (mode 0644, append-only)
    `-- contracts/
        |-- acceptance.sh      # mode 0444
        `-- e2e.sh             # mode 0444 (only if e2e provided)

Public API:

* :func:`write_frozen_contracts` -- create the layout above on first init.
* :func:`verify_contracts` -- every-tick integrity check; raises
  :class:`ContractsMismatch` on missing/tampered/missing-pin file.
* :func:`amend_acceptance` -- user-facing legitimate-change escape; re-pins
  the SHA, preserves 0444 mode, appends a hash-chained audit entry.

The implementation is intentionally narrow: ordinary :mod:`pathlib` I/O,
no ``O_NOFOLLOW`` gymnastics. Contracts live next to state but their
threat model is different -- a malicious peer tampering with the file is
detected by ``verify_contracts``; same-user races are not in scope here.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

__all__ = [
    "ContractsMismatch",
    "amend_acceptance",
    "verify_contracts",
    "write_frozen_contracts",
]


_READ_ONLY_MODE = 0o444
_LOG_FILENAME = "contracts.log"
_SHA_FILENAME = "contracts.sha"
_PLAN_ORIGINAL = "PLAN.original.md"
_ACCEPTANCE = "acceptance.sh"
_E2E = "e2e.sh"
_HASH_CHAIN_SEED = "genesis"
_HASH_CHAIN_PREFIX_LEN = 16
_VALID_KEYS = frozenset({_ACCEPTANCE, _E2E, _PLAN_ORIGINAL})


class ContractsMismatch(RuntimeError):
    """Raised when frozen contracts have been tampered with or removed."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or "frozen contracts mismatch")


def _script_body(command: str) -> str:
    return f"#!/bin/sh\nset -e\n{command}\n"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _resolve_contract_path(plan_dir: Path, fname: str) -> Path:
    """Map a logical contract filename to its on-disk path.

    ``acceptance.sh`` / ``e2e.sh`` live under ``plan_dir/contracts/``;
    ``PLAN.original.md`` lives at ``plan_dir`` top-level.
    """
    if fname in (_ACCEPTANCE, _E2E):
        return plan_dir / "contracts" / fname
    if fname == _PLAN_ORIGINAL:
        return plan_dir / _PLAN_ORIGINAL
    raise ValueError(f"unknown contract filename: {fname!r}")


def _write_read_only(path: Path, content: str) -> None:
    """(Re-)write ``path`` and chmod 0444. Idempotent on existing files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # If the file already exists and is 0444, we need write perms first.
    if path.exists():
        path.chmod(0o644)
    path.write_text(content, encoding="utf-8")
    path.chmod(_READ_ONLY_MODE)


def _load_pins(plan_dir: Path) -> dict[str, str]:
    sha_path = plan_dir / _SHA_FILENAME
    if not sha_path.is_file():
        raise ContractsMismatch("contracts.sha missing")
    try:
        data = json.loads(sha_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ContractsMismatch(f"contracts.sha malformed: {e.msg}") from None
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in data.items()
    ):
        raise ContractsMismatch("contracts.sha has wrong shape")
    unknown = set(data.keys()) - _VALID_KEYS
    if unknown:
        raise ContractsMismatch(
            f"contracts.sha references unknown file(s): {sorted(unknown)}",
        )
    return data


def _save_pins(plan_dir: Path, pins: dict[str, str]) -> None:
    sha_path = plan_dir / _SHA_FILENAME
    sha_path.write_text(
        json.dumps(pins, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_frozen_contracts(
    plan_dir: Path,
    acceptance: str,
    e2e: str | None,
    plan_md_content: str,
) -> None:
    """Create the frozen-contract layout at first project init.

    Writes ``acceptance.sh``, optionally ``e2e.sh``, snapshots ``PLAN.md`` as
    ``PLAN.original.md``, and writes ``contracts.sha`` mapping each filename
    to its sha256. All three files are chmod'd to 0444.
    """
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "contracts").mkdir(parents=True, exist_ok=True)

    pins: dict[str, str] = {}

    acc_body = _script_body(acceptance)
    _write_read_only(_resolve_contract_path(plan_dir, _ACCEPTANCE), acc_body)
    pins[_ACCEPTANCE] = _sha256_hex(acc_body.encode("utf-8"))

    if e2e:
        e2e_body = _script_body(e2e)
        _write_read_only(_resolve_contract_path(plan_dir, _E2E), e2e_body)
        pins[_E2E] = _sha256_hex(e2e_body.encode("utf-8"))

    _write_read_only(
        _resolve_contract_path(plan_dir, _PLAN_ORIGINAL), plan_md_content,
    )
    pins[_PLAN_ORIGINAL] = _sha256_hex(plan_md_content.encode("utf-8"))

    _save_pins(plan_dir, pins)


def verify_contracts(plan_dir: Path) -> None:
    """Verify all frozen contracts match their pinned SHAs.

    Raises :class:`ContractsMismatch` if ``contracts.sha`` is missing, any
    pinned file is missing, or any pinned file's sha256 has changed.
    """
    pins = _load_pins(plan_dir)
    for fname, expected_sha in pins.items():
        path = _resolve_contract_path(plan_dir, fname)
        if not path.is_file():
            raise ContractsMismatch(f"frozen file missing: {fname}")
        actual = _sha256_hex(path.read_bytes())
        if actual != expected_sha:
            raise ContractsMismatch(f"frozen file tampered: {fname}")


def _previous_chain_value(log_path: Path) -> str:
    """Return the previous chain prefix, or the genesis seed for line one."""
    if not log_path.is_file():
        return _HASH_CHAIN_SEED
    last_prefix: str | None = None
    with log_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            prefix, _, _ = line.partition(" ")
            last_prefix = prefix
    return last_prefix if last_prefix is not None else _HASH_CHAIN_SEED


def amend_acceptance(plan_dir: Path, new_command: str, reason: str) -> None:
    """Replace the acceptance command + append a hash-chained audit entry.

    Re-writes ``acceptance.sh`` with ``new_command``, updates
    ``contracts.sha`` (other entries untouched), re-enforces mode 0444 on
    the new file, and appends a tamper-evident line to ``contracts.log``::

        <chain16> <iso8601> amend acceptance: <cmd> | reason: <reason>

    ``<chain16>`` is the first 16 hex chars of
    ``sha256(previous_chain_value + entry_text)`` where ``entry_text`` is
    everything from the iso timestamp onward (including the trailing
    newline). The first entry uses the literal seed ``"genesis"``.
    """
    # Load + validate the pin file BEFORE mutating any frozen file so a
    # corrupt contracts.sha cannot wedge the layout.
    pins = _load_pins(plan_dir)

    # 1. Re-pin acceptance.sh (preserve 0444 afterward).
    acc_body = _script_body(new_command)
    _write_read_only(_resolve_contract_path(plan_dir, _ACCEPTANCE), acc_body)

    pins[_ACCEPTANCE] = _sha256_hex(acc_body.encode("utf-8"))
    _save_pins(plan_dir, pins)

    # 2. Append hash-chained audit entry.
    log_path = plan_dir / _LOG_FILENAME
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    entry_text = (
        f"{timestamp} amend acceptance: {new_command} | reason: {reason}\n"
    )
    prev = _previous_chain_value(log_path)
    chain_prefix = _sha256_hex(
        (prev + entry_text).encode("utf-8"),
    )[:_HASH_CHAIN_PREFIX_LEN]
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"{chain_prefix} {entry_text}")
