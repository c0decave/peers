"""Domain-neutral corpus :class:`Case` for the bring-up mode.

A corpus-intake adapter yields a stream of these; the loop drives one per sweep.
Adapter-specific payload lives inside ``data``; ``expected_ref`` points at the
ground-truth the differential oracle checks against (e.g. an exploit-corpus
enrichment row). The top-level shape is fixed and parsed fail-closed.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from collections.abc import Iterable

_CASE_KEYS = frozenset({"id", "data", "expected_ref", "source"})


@dataclass(frozen=True)
class Case:
    """One normalised corpus case the loop drives the tool-under-test against."""

    id: str
    data: dict = field(default_factory=dict)
    expected_ref: str | None = None
    source: str = ""


def normalize_case(raw: dict) -> Case:
    """Validate + normalise a raw case mapping into a :class:`Case`, fail-closed.

    Rejects a non-mapping, an unknown top-level key, a missing/empty/non-str
    ``id``, or a non-mapping ``data`` — so a malformed corpus entry can never
    silently become a half-formed task.
    """
    if not isinstance(raw, dict):
        raise ValueError("case must be a mapping")
    unknown = set(raw) - _CASE_KEYS
    if unknown:
        raise ValueError(f"unknown case key(s): {sorted(unknown)}")
    cid = raw.get("id")
    if not isinstance(cid, str) or not cid:
        raise ValueError("case requires a non-empty 'id'")
    data = raw.get("data", {})
    if not isinstance(data, dict):
        raise ValueError("case data must be a mapping")
    expected_ref = raw.get("expected_ref")
    if expected_ref is not None and not isinstance(expected_ref, str):
        raise ValueError("case expected_ref must be a string or None")
    source = raw.get("source", "")
    if not isinstance(source, str):
        raise ValueError("case source must be a string")
    return Case(id=cid, data=dict(data), expected_ref=expected_ref, source=source)


def require_unique_case_ids(cases: Iterable[Case]) -> None:
    """Raise ``ValueError`` naming the duplicate case-ids, if any.

    Duplicate ids would mask coverage (two cases collapse to one in any
    id-keyed map), so every entry seam — the loop and the one-pass
    sweep-and-report — fails CLOSED on them rather than silently dropping a
    case. Shared so both seams enforce the identical rule.
    """
    ids = [c.id for c in cases]
    dups = sorted(cid for cid, n in Counter(ids).items() if n > 1)
    if dups:
        raise ValueError(f"duplicate case id(s): {dups} — would mask coverage")
