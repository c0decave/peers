"""STEP-3 — the research source cache (§5.3).

An append-only, no-follow JSONL store of every fetched/read :class:`Source` a
sweep gathered. It is the backing store the ``fetched-source`` / ``file``
witnesses re-derive from in later steps: a witness is trustworthy only because
its ``content_hash`` keys back to a recorded source here, never because a model
asserted it.

Two load-bearing properties:

1. **Append-only, no-follow.** Writes go through
   :func:`peers.safe_io.append_text_in_dir_no_symlink` (a no-follow APPEND),
   mirroring the run ledger — a swapped/symlinked parent or leaf is refused and
   prior rows are never truncated.
2. **Content-addressed lookup.** :meth:`by_content_hash` returns the recorded
   source for a hash, so a witness's claimed ``content_hash`` can be checked
   against what was actually fetched. The store keeps every ``add`` (a failed
   fetch is recorded with its ``access_failure``, never silently dropped); on a
   duplicate hash the FIRST recorded source wins (a deterministic read).
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from peers import safe_io
from peers.research.ports import Source


class SourceCache:
    """A content-addressed, append-only JSONL cache of fetched/read sources."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def add(self, source: Source) -> None:
        """Append one :class:`Source` as a JSON line (no-follow append)."""
        line = json.dumps(asdict(source), ensure_ascii=False) + "\n"
        safe_io.append_text_in_dir_no_symlink(
            self.path.parent, self.path.name, line)

    def by_content_hash(self, content_hash: str) -> Source | None:
        """Return the FIRST recorded :class:`Source` whose ``content_hash``
        matches, or ``None`` if absent / the cache file does not exist.

        Reads via the no-follow read primitive. A corrupt/torn trailing line is
        skipped (fail-soft on read) so a crash mid-append can't blind the whole
        lookup — but a matching, well-formed earlier row is still found.
        """
        if not self.path.exists():
            return None
        raw = safe_io.read_bytes_no_symlink(self.path).decode("utf-8", "ignore")
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue  # torn / partial line -> skip, keep scanning
            if d.get("content_hash") == content_hash:
                return Source(
                    url=d["url"],
                    resolved_origin=d["resolved_origin"],
                    content_hash=d["content_hash"],
                    retrieval_time=d["retrieval_time"],
                    access_failure=d.get("access_failure"),
                )
        return None
