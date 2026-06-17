"""Generic, config-driven readers for the ``differential`` bring-up oracle.

The differential oracle compares the tool's OWN verdict against the corpus
ground-truth via two injected readers. These make both CLI-constructible from a
manifest (so ``differential`` is no longer library-API-only):

- :func:`make_corpus_expected_reader` — the ground-truth verdict from a corpus
  Case field (e.g. ``case.data["expected"]``).
- :func:`make_sqlite_tool_verdict_reader` — the tool's verdict from a sqlite DB it
  writes (e.g. a ``findings.sqlite3``), resolved per-case relative to the
  run's working dir, fail-closed (a missing/corrupt DB raises -> tool-bug; a
  missing row is ``None`` -> tool-verdict-unreadable -> tool-bug; an absent
  ground-truth field is ``None`` -> corpus-error).
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from .models import Case

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _ident(value, name: str) -> str:
    """A safe SQL identifier (table/column) — operator config is interpolated into
    the query, so reject anything that is not a bare identifier (fail-closed)."""
    if not isinstance(value, str) or not _IDENT.match(value):
        raise ValueError(
            f"differential oracle {name!r} must be a bare SQL identifier, got {value!r}")
    return value


def make_corpus_expected_reader(config: dict):
    """Read the ground-truth verdict from a Case field (default ``expected``).
    Returns the value or ``None`` (a deliberate absent ground-truth)."""
    field = config.get("expected_field", "expected")
    if not isinstance(field, str) or not field:
        raise ValueError("differential oracle expected_field must be a non-empty string")

    def expected_verdict(case: Case):
        return case.data.get(field)

    return expected_verdict


def make_sqlite_tool_verdict_reader(config: dict, *, root: Path):
    """Read the tool's verdict for a case from a sqlite DB it wrote.

    Config: ``db`` (path; default ``findings.sqlite3``; resolved per-case relative
    to the run's ``work`` dir, then ``root``, unless absolute), ``table``
    (default ``findings``), ``id_column`` (default ``case_id``), ``verdict_column``
    (default ``status``). Opens read-only: a missing/corrupt DB RAISES (-> the
    oracle routes tool-bug); a missing row returns ``None``."""
    db = config.get("db", "findings.sqlite3")
    if not isinstance(db, str) or not db:
        raise ValueError("differential oracle db must be a non-empty path string")
    table = _ident(config.get("table", "findings"), "table")
    id_col = _ident(config.get("id_column", "case_id"), "id_column")
    verdict_col = _ident(config.get("verdict_column", "status"), "verdict_column")
    root = Path(root)

    def _resolve(work) -> Path:
        p = Path(db)
        if p.is_absolute():
            return p
        if work is not None and (Path(work) / p).exists():
            return Path(work) / p
        return root / p

    def tool_verdict(case: Case, observation, work):
        path = _resolve(work)
        # read-only URI: a missing file raises rather than creating an empty DB.
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            cur = conn.execute(
                f"SELECT {verdict_col} FROM {table} WHERE {id_col} = ?", (case.id,))
            row = cur.fetchone()
        finally:
            conn.close()
        return row[0] if row is not None else None

    return tool_verdict
