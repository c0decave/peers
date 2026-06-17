"""RunLedger — an append-only, hash-chained witness log for a ModeRun.

The ledger is the substrate every later spine task writes to. Each row is a
``LedgerEntry`` carrying the §2.3 schema plus an ``independence`` flag; the
rows form a backwards-walkable hash chain (``prev`` links to the previous
row's ``entry_sha``). ``verify()`` recomputes every digest and checks every
link, so any tamper — including a flipped ``independence`` — is detectable.

Two load-bearing invariants (tighten-only; never weaken):

1. **Authorship is substrate-only.** The public :meth:`RunLedger.append`
   *rejects* any non-None caller-supplied ``author`` with ``ValueError``.
   The only path that sets an author is :meth:`RunLedger.append_attested`
   (Task 2), which derives it from the substrate attestation note. An agent
   cannot write its own author.
2. **Append-only.** Writes go through
   :func:`peers.safe_io.append_text_in_dir_no_symlink` — a no-follow APPEND.
   We never whole-file write-then-rename (that would truncate prior rows).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from peers import safe_io

#: Fields, in order, that feed ``entry_sha``. ``v`` (schema version) and the
#: digest itself are intentionally excluded. ``independence`` IS included so a
#: flipped flag is tamper-evident (load-bearing invariant).
_HASHED_FIELDS = (
    "prev",
    "event",
    "mode_run",
    "author",
    "subject",
    "status",
    "witness",
    "independence",
)


@dataclass
class LedgerEntry:
    """One row of the run ledger (the §2.3 schema + ``independence``).

    ``event`` and ``status`` are always required; every other field carries a
    default so rows are built keyword-only (no positional coupling).
    """

    event: str
    status: str
    v: int = 1
    prev: str | None = None
    mode_run: str | None = None
    author: str | None = None
    subject: str | None = None
    witness: dict | None = None
    independence: bool = False
    entry_sha: str = ""


def _canonical_json(obj: object) -> str:
    """Deterministic JSON: sorted keys, no insignificant whitespace.

    Used ONLY to compute ``entry_sha`` — so a witness dict written with any
    key order hashes identically. (The on-disk line uses default,
    human-readable separators; see :meth:`RunLedger._serialize`.)
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _compute_entry_sha(payload: dict) -> str:
    """Hash the ordered tuple of hashed fields drawn from ``payload``."""
    values = tuple(payload.get(name) for name in _HASHED_FIELDS)
    return hashlib.sha256(_canonical_json(values).encode("utf-8")).hexdigest()


def _field_type(value: object) -> str:
    return type(value).__name__


def _required_str(d: dict, key: str) -> str:
    if key not in d:
        raise ValueError(f"ledger row missing required key {key!r}")
    value = d[key]
    if not isinstance(value, str):
        raise ValueError(
            f"ledger row field {key!r} must be str, got {_field_type(value)}"
        )
    return value


def _optional_str(d: dict, key: str) -> str | None:
    value = d.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(
            f"ledger row field {key!r} must be str or null, "
            f"got {_field_type(value)}"
        )
    return value


def _entry_from_raw(d: dict) -> LedgerEntry:
    """Validate and materialise one raw JSON object as a ledger row.

    ``run.jsonl`` is agent-writable. A forged row with a valid hash but a wrong
    ``event``/``status``/``subject`` type is still corrupt input, because
    downstream consumers branch on the first two and assume the third is a
    hashable ``str | None``.
    """
    return LedgerEntry(
        event=_required_str(d, "event"),
        status=_required_str(d, "status"),
        v=d.get("v", 1),
        prev=d.get("prev"),
        mode_run=d.get("mode_run"),
        author=d.get("author"),
        subject=_optional_str(d, "subject"),
        witness=d.get("witness"),
        independence=bool(d.get("independence", False)),
        entry_sha=d.get("entry_sha", ""),
    )


class RunLedger:
    """A hash-chained, append-only JSONL ledger at ``path``."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    # ---- reading -------------------------------------------------------
    def _read_raw(self) -> list[dict]:
        """Parse every line into a dict. Strict: a corrupt line raises."""
        if not self.path.exists():
            return []
        rows: list[dict] = []
        text = self.path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)  # JSONDecodeError (a ValueError) propagates
            # a valid-JSON NON-object row (42, "x", [..], null, true)
            # parses fine but is not a ledger entry. Surface it as the same
            # catchable corruption class as a missing-key row so
            # read()/verify() fail closed instead of crashing downstream on a
            # TypeError/AttributeError (`run.jsonl` is agent-writable).
            if not isinstance(obj, dict):
                raise ValueError(
                    f"ledger row is not a JSON object: {type(obj).__name__}"
                )
            rows.append(obj)
        return rows

    def read(self) -> list[LedgerEntry]:
        """Return all rows as :class:`LedgerEntry`. A corrupt line raises.

        A row missing the required ``event``/``status`` keys, or carrying the
        wrong JSON type for the dataclass schema, is corruption too
        (``.peers/run.jsonl`` is agent-writable). It is surfaced as a
        ``ValueError`` — the same catchable class as the ``JSONDecodeError``
        ``_read_raw`` propagates — rather than an uncatchable ``KeyError`` or
        downstream ``TypeError`` that would escape callers' fail-closed
        ``except (ValueError, OSError)`` handlers (BUG-720/727).
        """
        return [_entry_from_raw(d) for d in self._read_raw()]

    def verify(self) -> bool:
        """Recompute every digest and check every chain link. Fail-closed:
        a corrupt/unparseable ledger, a digest that does not re-derive, or a
        broken ``prev`` link all return ``False``."""
        try:
            rows = self._read_raw()
        except (ValueError, OSError):
            return False
        prev_sha: str | None = None
        for d in rows:
            try:
                _entry_from_raw(d)
            except ValueError:
                return False
            stored = d.get("entry_sha", "")
            if _compute_entry_sha(d) != stored:
                return False           # a field was tampered
            if d.get("prev") != prev_sha:
                return False           # chain link broken
            prev_sha = stored
        return True

    # ---- writing -------------------------------------------------------
    def _last_entry_sha(self) -> str | None:
        """The ``entry_sha`` of the last COMPLETE (JSON-parseable) line. A torn
        trailing line — a partial write left by a crash mid-append — is skipped
        so the append path can still extend the chain (fail-closed termination
        in :func:`peers.spine.mode_run.drive`). ``read()`` and ``verify()`` stay
        STRICT: they still surface the corruption."""
        if not self.path.exists():
            return None
        for line in reversed(self.path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue            # torn / partial line -> skip, keep walking back
            if not isinstance(d, dict):
                continue            # BUG-722: non-object row carries no entry_sha;
                                    # skip it like a torn line and keep walking back
            try:
                entry = _entry_from_raw(d)
            except ValueError:
                continue            # malformed object row -> not a complete ledger row
            if entry.entry_sha:
                return entry.entry_sha
        return None

    @staticmethod
    def _serialize(payload: dict) -> str:
        """Human-readable on-disk line (default separators, NOT canonical).

        The tamper tests rely on ``"independence": true`` appearing verbatim,
        so we keep default ``json.dumps`` spacing here. Canonicalisation is
        only for the digest.
        """
        return json.dumps(payload, ensure_ascii=False)

    def _trailing_newline_missing(self) -> bool:
        """True iff the ledger file exists, is non-empty, and does NOT end with a
        newline — i.e. its last line is a torn/interrupted write. Used so the
        next append starts on its own line instead of concatenating onto the
        partial one (fail-closed durability; read()/verify() still reject the
        torn line)."""
        try:
            with open(self.path, "rb") as fh:
                fh.seek(-1, 2)              # last byte; raises on an empty file
                return fh.read(1) != b"\n"
        except (OSError, ValueError):
            return False                    # missing / empty -> nothing to heal

    def _build_and_append(self, payload: dict) -> LedgerEntry:
        """Compute the digest for ``payload``, append one JSONL line, return
        the entry. ``payload`` must already carry every field EXCEPT
        ``entry_sha`` (filled here) and ``prev`` (filled here)."""
        payload["prev"] = self._last_entry_sha()
        payload["entry_sha"] = _compute_entry_sha(payload)
        line = self._serialize(payload) + "\n"
        if self._trailing_newline_missing():
            line = "\n" + line              # never merge onto a torn final line
        safe_io.append_text_in_dir_no_symlink(
            self.path.parent, self.path.name, line,
        )
        return LedgerEntry(**payload)

    def append(
        self,
        *,
        event: str,
        status: str,
        subject: str | None = None,
        witness: dict | None = None,
        author: str | None = None,
        mode_run: str | None = None,
        independence: bool = False,
    ) -> LedgerEntry:
        """Append one entry. **Rejects** a non-None ``author``: authorship is
        substrate-only (see :meth:`append_attested`)."""
        if author is not None:
            raise ValueError(
                "author is substrate-only; use append_attested(repo, sha, ...) "
                "to set an attested author. A caller may not supply one.",
            )
        payload = {
            "v": 1, "event": event, "mode_run": mode_run, "author": None,
            "subject": subject, "status": status, "witness": witness,
            "independence": independence,
        }
        return self._build_and_append(payload)

    def append_attested(
        self,
        repo: Path | str,
        sha: str,
        *,
        event: str,
        status: str,
        subject: str | None = None,
        witness: dict | None = None,
        mode_run: str | None = None,
        independence: bool = False,
    ) -> LedgerEntry:
        """Append one entry whose ``author`` is the **substrate-attested** peer
        of commit ``sha`` in ``repo`` (STEP-2).

        This is the ONLY sanctioned author path. The author is derived from
        :func:`peers.spine.authorship.resolve_author` (the ``peers-attest``
        note), NEVER from caller/agent content — so it deliberately *bypasses*
        the public-``append`` caller-author guard. An unattested ``sha``
        resolves to ``author=None`` (the entry is still written and chained).
        All other fields forward unchanged.
        """
        # Imported lazily to avoid an import cycle (authorship imports nothing
        # from this module, but keeping it lazy mirrors the plan's "thin seam").
        from peers.spine.authorship import resolve_author

        author = resolve_author(repo, sha)
        # Record the ATTESTING commit on an independence row so the
        # ``authorship-attested`` gate can RE-DERIVE the author from the substrate
        # note (``resolve_author(repo, attest_sha)``) instead of trusting the
        # agent-writable row (full-depth-analysis §1). The witness is the only
        # free-form carrier; a non-dict/absent witness is normalized to a dict so
        # the attesting sha is always present on an independence row.
        wit = witness
        if independence:
            wit = dict(witness) if isinstance(witness, dict) else {}
            wit["attest_sha"] = sha
        payload = {
            "v": 1, "event": event, "mode_run": mode_run, "author": author,
            "subject": subject, "status": status, "witness": wit,
            "independence": independence,
        }
        return self._build_and_append(payload)
        # NOTE: there is intentionally NO caller-author write path on this class.
        # An earlier `append_authored_for_test` helper was removed (self-hosting
        # review): even labelled test-only, an unguarded forge-author primitive on
        # the production RunLedger let a forged `author` green every gate. Tests
        # that need an authored row use `append_attested` against a real
        # `refs/notes/peers-attest` fixture (see tests/unit/test_spine_gates.py).
