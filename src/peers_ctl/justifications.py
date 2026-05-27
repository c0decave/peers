"""Reviewer-signed justifications log for shortcut-marker escapes (Task 5.2).

Companion infrastructure for the ``no-shortcut-markers`` hard gate
(Task 5.1). The gate forbids TODO/FIXME/XXX/HACK/PLACEHOLDER/STUB and
NotImplementedError in concrete classes under ``src/``. Legitimate
escapes carry a ``# JUSTIFIED: <reason>`` annotation on the offending
line *and* a reviewer-signed entry in ``.peers/justifications.log``;
both halves are required, mirroring the
``checkoff-by-other-peer`` two-key principle (annotation in code by
the implementer, sign-off in the log by the reviewer).

Log layout
----------
``<plan_dir>/justifications.log`` is an append-only text file. Each
line is::

    <chain16> <file>:<line> <reviewer> <reason>

with ``<chain16>`` = first 16 hex chars of
``sha256(previous_chain_value + entry_text)`` and ``entry_text`` =
everything after the chain prefix and its single separating space,
including the trailing newline. The first entry uses the literal seed
``"genesis"`` -- identical to the ``contracts.log`` hash-chain in
:mod:`peers_ctl.contracts`. Reason text may contain spaces; the parser
splits the first three whitespace-separated fields (chain, file:line,
reviewer) and treats the remainder as the reason.

The chain makes tampering (editing or reordering a past entry) cheap
to detect via :func:`verify_log_chain`; the substrate runs that
verification before consulting the log, so a peer cannot rewrite a
historic justification to retroactively bless a violation.

Public API
----------
* :func:`append_justification` -- reviewer signs off on a single
  ``file:line`` shortcut; appends a chain-linked entry.
* :func:`is_justified` -- query whether ``file:line`` has any signed
  entry; returns ``(bool, signer_email_or_None)``.
* :func:`verify_log_chain` -- recompute the chain from genesis and
  raise :class:`JustificationError` on the first mismatch.

Same threat model as contracts.py: a malicious peer tampering with
the log is detected at verify-time; same-user races on append are
not in scope (plan_dir is per-project, exclusive to one substrate).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

__all__ = [
    "JustificationError",
    "append_justification",
    "is_justified",
    "verify_log_chain",
]


_LOG_FILENAME = "justifications.log"
_HASH_CHAIN_SEED = "genesis"
_HASH_CHAIN_PREFIX_LEN = 16


class JustificationError(ValueError):
    """Raised when the justifications log is broken or tampered."""


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _chain_prefix(prev: str, entry_text: str) -> str:
    return _sha256_hex((prev + entry_text).encode("utf-8"))[
        :_HASH_CHAIN_PREFIX_LEN
    ]


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


def _parse_entry(line: str) -> tuple[str, str, int, str, str] | None:
    """Parse one log line into ``(chain, file, line_no, reviewer, reason)``.

    Returns ``None`` on shape failures so callers can decide whether
    to skip (queries) or raise (chain verification). Reason is the
    rest of the line after the third whitespace-separated field;
    it may contain spaces.
    """
    parts = line.split(" ", 3)
    if len(parts) < 4:
        return None
    chain, file_line, reviewer, reason = parts
    file_path, sep, line_no_str = file_line.rpartition(":")
    if not sep or not file_path:
        return None
    try:
        line_no = int(line_no_str)
    except ValueError:
        return None
    return chain, file_path, line_no, reviewer, reason


def append_justification(
    plan_dir: Path,
    file_path: str,
    line_number: int,
    reason: str,
    reviewer_peer: str,
) -> None:
    """Append a reviewer-signed justification entry.

    ``file_path`` should be relative to the project root (e.g.
    ``src/foo.py``). ``reviewer_peer`` is a free-form identifier --
    typically the peer name (``codex`` / ``claude``) or the
    reviewer's git author email. ``reason`` is a short free-form
    rationale and may contain spaces.

    The entry is hash-chained: ``sha256(prev_chain + entry_text)[:16]``
    seeded with ``"genesis"`` for the first line. ``entry_text``
    excludes the chain prefix + its separating space (so the chain
    only attests to the entry's own payload, not its own prefix).

    Validation: newline / carriage-return in any field rejects the
    append with :class:`JustificationError` -- those would split a
    single logical entry across multiple lines and silently break
    the chain.
    """
    for label, value in (
        ("file_path", file_path),
        ("reviewer_peer", reviewer_peer),
        ("reason", reason),
    ):
        if "\n" in value or "\r" in value:
            raise JustificationError(
                f"{label} must not contain newline characters",
            )
    if " " in file_path:
        raise JustificationError("file_path must not contain spaces")
    if " " in reviewer_peer:
        raise JustificationError("reviewer_peer must not contain spaces")
    if not isinstance(line_number, int) or line_number < 1:
        raise JustificationError(
            f"line_number must be a positive int, got {line_number!r}",
        )

    plan_dir.mkdir(parents=True, exist_ok=True)
    log_path = plan_dir / _LOG_FILENAME
    entry_text = f"{file_path}:{line_number} {reviewer_peer} {reason}\n"
    prev = _previous_chain_value(log_path)
    chain = _chain_prefix(prev, entry_text)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"{chain} {entry_text}")


def is_justified(
    plan_dir: Path,
    file_path: str,
    line_number: int,
) -> tuple[bool, str | None]:
    """Return ``(signed, signer)`` for the given ``file:line``.

    Returns ``(False, None)`` if no log file exists yet, the log
    has no matching entry, or the matching entry is malformed.
    Returns the *first* matching signer if multiple sign-offs exist
    for the same ``file:line``.

    This is a pure lookup -- it does NOT verify the chain. Callers
    that care about tamper-detection should run
    :func:`verify_log_chain` separately (typically at gate entry
    time). Doing so on every query would be O(n*m) and noisy.
    """
    log_path = plan_dir / _LOG_FILENAME
    if not log_path.is_file():
        return (False, None)
    with log_path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line:
                continue
            parsed = _parse_entry(line)
            if parsed is None:
                continue
            _chain, fpath, lno, reviewer, _reason = parsed
            if fpath == file_path and lno == line_number:
                return (True, reviewer)
    return (False, None)


def verify_log_chain(plan_dir: Path) -> None:
    """Recompute the hash-chain from genesis; raise on first mismatch.

    A missing log file is treated as "no entries yet" and returns
    without error -- the absence-of-log state is well-defined and
    not a tamper signal. Empty lines are skipped (whitespace-only
    lines from manual editing would normally be a smell, but we
    tolerate them to keep the format forgiving).

    Raises :class:`JustificationError` on:

    * a malformed entry (cannot be parsed into chain + payload);
    * a chain prefix that does not equal
      ``sha256(prev_chain + entry_text)[:16]``.

    Identical algorithm to ``contracts.py``: chain seeded with
    ``"genesis"``, each line's prefix attests to ``prev + payload``
    where ``payload`` is the entry text *after* the prefix and its
    single separating space (including the trailing newline).
    """
    log_path = plan_dir / _LOG_FILENAME
    if not log_path.is_file():
        return
    prev = _HASH_CHAIN_SEED
    with log_path.open("r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.rstrip("\n")
            if not line:
                continue
            chain, _, rest = line.partition(" ")
            if not chain or not rest:
                raise JustificationError(
                    f"malformed entry on line {lineno}: {line!r}",
                )
            # Validate chain-prefix is hex of expected length
            if len(chain) != _HASH_CHAIN_PREFIX_LEN:
                raise JustificationError(
                    f"chain prefix on line {lineno} has length "
                    f"{len(chain)}, expected {_HASH_CHAIN_PREFIX_LEN}",
                )
            try:
                int(chain, 16)
            except ValueError as e:
                raise JustificationError(
                    f"chain prefix on line {lineno} is not hex: {chain!r}",
                ) from e
            entry_text = rest + "\n"
            expected = _chain_prefix(prev, entry_text)
            if chain != expected:
                raise JustificationError(
                    f"hash-chain broken on line {lineno}: "
                    f"got {chain}, expected {expected}",
                )
            prev = chain
