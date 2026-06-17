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
import os
import stat
import time
from pathlib import Path

from peers.safe_io import (
    append_text_no_symlink,
    open_text_read_no_symlink,
    read_bytes_no_symlink,
    read_text_no_symlink,
    write_text_no_symlink,
)

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
# chain-bind the pin file. Every chain entry encodes the
# post-amend pin state; verify_contracts re-derives that state from the
# log so a silent rewrite of contracts.sha + acceptance.sh (without a
# matching log entry) is rejected. The state tag is the prefix below.
_STATE_SEP = " | state: "
_INIT_EVENT = "init"
_AMEND_EVENT = "amend acceptance"


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
    """(Re-)write ``path`` and chmod 0444. Idempotent on existing files.

    BUG-157: the legacy implementation used Path.exists / Path.chmod /
    Path.write_text, all of which follow symlinks. A malicious workspace
    could replace acceptance.sh or PLAN.original.md with a symlink before
    `peers-ctl amend` so the operator's chmod+overwrite would land on the
    symlink target. The hardened version uses lstat to detect a symlinked
    leaf (raise OSError, never follow) and opens the file with
    O_NOFOLLOW for both the pre-chmod and the actual rewrite.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lst = os.lstat(path)
    except FileNotFoundError:
        lst = None
    if lst is not None:
        if stat.S_ISLNK(lst.st_mode):
            raise OSError(
                f"refusing to write contract through symlink: {path}"
            )
        if not stat.S_ISREG(lst.st_mode):
            raise OSError(
                f"refusing to write contract over non-regular file: {path}"
            )
        # Pre-existing 0444 file needs writability first. Use chmod()
        # with follow_symlinks=False on platforms that support it; the
        # symlink check above already excluded the symlink case so this
        # path always operates on a regular file.
        try:
            os.chmod(path, 0o644, follow_symlinks=False)
        except (NotImplementedError, OSError):
            os.chmod(path, 0o644)
    # O_TRUNC at open would clobber a hardlinked victim before
    # the nlink check below could reject it. Open without truncation,
    # verify regular + nlink == 1, then truncate.
    flags = os.O_WRONLY | os.O_CREAT
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    fd = os.open(str(path), flags, 0o644)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(
                f"refusing to write contract over non-regular file: {path}"
            )
        if st.st_nlink != 1:
            raise OSError(
                f"refusing to write contract over hard-linked file: {path}"
            )
        os.ftruncate(fd, 0)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as fh:
            fd = -1
            fh.write(content)
    finally:
        if fd >= 0:
            os.close(fd)
    try:
        os.chmod(path, _READ_ONLY_MODE, follow_symlinks=False)
    except (NotImplementedError, OSError):
        os.chmod(path, _READ_ONLY_MODE)


def _load_pins(plan_dir: Path) -> dict[str, str]:
    sha_path = plan_dir / _SHA_FILENAME
    # Path.read_text follows symlinks and hardlinks. If
    # contracts.sha is a symlink to a (valid) JSON file, the old code
    # let _load_pins succeed, then amend_acceptance rewrote
    # acceptance.sh, and only afterwards _save_pins (via
    # write_text_no_symlink) refused the symlink — leaving the frozen
    # contract partially mutated. Reject any non-regular pin file UP
    # FRONT so a corrupt link/file gates the rewrite before mutation.
    # Missing file → ContractsMismatch (caller-recoverable). Non-regular
    # / symlinked / hardlinked → propagate the raw OSError so the
    # operator sees the specific safe-IO refusal.
    try:
        lst = os.lstat(sha_path)
    except FileNotFoundError:
        raise ContractsMismatch("contracts.sha missing") from None
    if not stat.S_ISREG(lst.st_mode):
        # symlink, dir, fifo, etc.
        if stat.S_ISLNK(lst.st_mode):
            raise OSError(
                f"refusing to read contracts.sha through symlink: {sha_path}"
            )
        raise OSError(
            f"refusing to read non-regular contracts.sha: {sha_path}"
        )
    if lst.st_nlink != 1:
        raise OSError(
            f"refusing to read hard-linked contracts.sha: {sha_path}"
        )
    raw = read_text_no_symlink(sha_path)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ContractsMismatch(f"contracts.sha malformed: {e.msg}") from None
    except RecursionError:
        raise ContractsMismatch(
            "contracts.sha malformed: nesting too deep",
        ) from None
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
    # write_text() follows symlinks and ignores hardlinks;
    # write_text_no_symlink refuses both before any truncation.
    write_text_no_symlink(
        sha_path,
        json.dumps(pins, indent=2, sort_keys=True) + "\n",
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
    # seed the audit log with a genesis chain entry pinning the
    # initial pin-state, so verify_contracts can detect a later silent
    # rewrite of contracts.sha + acceptance.sh.
    _append_chain_entry(
        plan_dir, _INIT_EVENT, "", _pin_state_hash(pins),
    )


def verify_contracts(plan_dir: Path) -> None:
    """Verify all frozen contracts match their pinned SHAs.

    Raises :class:`ContractsMismatch` if ``contracts.sha`` is missing, any
    pinned file is missing, any pinned file's sha256 has changed, or the
    hash-chained audit log (``contracts.log``) does not record the current
    pin state.

    BUG-178: contracts.sha is workspace-writable and a same-user peer
    can rewrite acceptance.sh together with the pin. The audit log
    walks every recorded amendment and binds the latest chain entry to
    the post-amend pin state, so a silent pin rewrite that does not also
    extend the chain is detected here. A wholesale rewrite of the log
    (including the chain) still requires the attacker to leave an audit
    record — they cannot silently bypass the gate.
    """
    pins = _load_pins(plan_dir)
    for fname, expected_sha in pins.items():
        path = _resolve_contract_path(plan_dir, fname)
        # read_bytes_no_symlink lstats the leaf, refuses
        # symlinks / hardlinks (st_nlink != 1) / non-regular files, and
        # opens with O_NOFOLLOW. Path.is_file() and Path.read_bytes()
        # both follow symlinks, so a same-UID adversary could swap the
        # leaf for a symlink targeting a hash-matching file and silently
        # pass the gate. The hash-chain audit (next step) doesn't catch
        # this — the chain encodes the pin map, not the leaf inode.
        try:
            data = read_bytes_no_symlink(path)
        except FileNotFoundError as e:
            raise ContractsMismatch(f"frozen file missing: {fname}") from e
        except OSError as e:
            raise ContractsMismatch(
                f"frozen file unreadable / non-regular: {fname}: {e}"
            ) from e
        actual = _sha256_hex(data)
        if actual != expected_sha:
            raise ContractsMismatch(f"frozen file tampered: {fname}")
    log_path = plan_dir / _LOG_FILENAME
    _, logged_state = _walk_chain(log_path)
    if logged_state != _pin_state_hash(pins):
        raise ContractsMismatch(
            "contracts.sha does not match the latest audit-log state",
        )


def _previous_chain_value(log_path: Path) -> str:
    """Return the previous chain prefix, or the genesis seed for line one."""
    # refuse symlinks at the log path; Path.open("r")
    # followed them and let an attacker plant the chain prev.
    if log_path.is_symlink():
        raise OSError(
            f"refusing to read contract audit log through symlink: {log_path}"
        )
    if not log_path.is_file():
        return _HASH_CHAIN_SEED
    last_prefix: str | None = None
    with open_text_read_no_symlink(log_path) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            prefix, _, _ = line.partition(" ")
            last_prefix = prefix
    return last_prefix if last_prefix is not None else _HASH_CHAIN_SEED


def _pin_state_hash(pins: dict[str, str]) -> str:
    """Hash the pin dict canonically. Used to bind contracts.sha to
    each chain entry."""
    canonical = json.dumps(pins, sort_keys=True, separators=(",", ":"))
    return _sha256_hex(canonical.encode("utf-8"))


def _append_chain_entry(
    plan_dir: Path, event: str, payload: str, state_hash: str,
) -> None:
    """Append a chain-bound entry to ``contracts.log``.

    Format::

        <chain16> <iso> <event>[: <payload>] | state: <state-hash>

    ``event`` is :data:`_INIT_EVENT` or :data:`_AMEND_EVENT`.
    ``payload`` is the human-readable suffix (e.g. command + reason) or
    the empty string for ``init``. ``state_hash`` is the post-event
    :func:`_pin_state_hash` of contracts.sha.
    """
    log_path = plan_dir / _LOG_FILENAME
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    body = f"{event}: {payload}" if payload else event
    entry_text = f"{timestamp} {body}{_STATE_SEP}{state_hash}\n"
    prev = _previous_chain_value(log_path)
    chain_prefix = _sha256_hex(
        (prev + entry_text).encode("utf-8"),
    )[:_HASH_CHAIN_PREFIX_LEN]
    append_text_no_symlink(log_path, f"{chain_prefix} {entry_text}")


def _parse_chain_entry(line: str) -> tuple[str, str, str] | None:
    """Return (chain_prefix, body_without_prefix, state_hash) or None.

    None on malformed lines (no state suffix, missing prefix, etc.).
    """
    line = line.rstrip("\n")
    if not line:
        return None
    prefix, _, rest = line.partition(" ")
    if len(prefix) != _HASH_CHAIN_PREFIX_LEN or not rest:
        return None
    body, sep, state_hash = rest.rpartition(_STATE_SEP)
    if not sep or not body or not state_hash:
        return None
    return prefix, body, state_hash


def _walk_chain(log_path: Path) -> tuple[str, str]:
    """Validate the hash-chain and return (final_chain_prefix, state_hash).

    Raises :class:`ContractsMismatch` when the log is missing, empty,
    malformed, or its chain is broken.
    """
    if log_path.is_symlink():
        raise OSError(
            f"refusing to read contract audit log through symlink: {log_path}"
        )
    if not log_path.is_file():
        raise ContractsMismatch("contracts.log missing")
    prev = _HASH_CHAIN_SEED
    last_state: str | None = None
    last_prefix: str | None = None
    saw_entry = False
    with open_text_read_no_symlink(log_path) as f:
        for raw in f:
            entry = _parse_chain_entry(raw)
            if entry is None:
                if raw.strip() == "":
                    continue
                raise ContractsMismatch("contracts.log malformed entry")
            prefix, body, state_hash = entry
            # The entry_text used for the chain hash is the line minus the
            # leading "<prefix> ", plus its terminating newline.
            entry_text = (
                f"{body}{_STATE_SEP}{state_hash}\n"
            )
            expected_prefix = _sha256_hex(
                (prev + entry_text).encode("utf-8"),
            )[:_HASH_CHAIN_PREFIX_LEN]
            if expected_prefix != prefix:
                raise ContractsMismatch(
                    "contracts.log chain broken at entry "
                    f"{prefix} (expected {expected_prefix})"
                )
            prev = prefix
            last_state = state_hash
            last_prefix = prefix
            saw_entry = True
    if not saw_entry or last_state is None or last_prefix is None:
        raise ContractsMismatch("contracts.log has no entries")
    return last_prefix, last_state


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

    # 2. Append hash-chained audit entry binding the new pin state
    #. _append_chain_entry uses append_text_no_symlink which
    # refuses symlinks/hardlinks.
    payload = f"{new_command} | reason: {reason}"
    _append_chain_entry(
        plan_dir, _AMEND_EVENT, payload, _pin_state_hash(pins),
    )
