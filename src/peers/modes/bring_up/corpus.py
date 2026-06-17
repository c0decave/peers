"""Phase-1 — corpus-intake adapters → normalised :class:`Case` streams.

Each adapter turns one external corpus shape into a deterministic list of Cases.
External IO (a ``corpus query`` subprocess, a pytest collection) is injected so
the units stay deterministic; :func:`make_corpus_adapter` wires the real
defaults. File reads go through ``safe_io`` no-symlink primitives — the corpus is
untrusted external data (the design's prompt-injection-hygiene stance).
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from peers import safe_io

from .manifest import Corpus
from .models import Case

_ROW_KEYS = ("rows", "results", "entries")
_QUEUE_KEYS = ("queue", "targets")
_QUERY_FLAGS = {  # select-key -> corpus query flag
    "cve": "--cve", "cwe": "--cwe", "lang": "--lang",
    "max_trust_tier": "--max-trust-tier", "grep": "--grep", "limit": "--limit",
}


@runtime_checkable
class CorpusAdapter(Protocol):
    def cases(self) -> list[Case]:
        """Return the normalised case stream for this corpus selection."""


class ExploitCorpusAdapter:
    """Maps an ``exploit-corpus`` query JSON envelope into Cases."""

    def __init__(self, select: dict, *, query_json: Callable[[dict], str]) -> None:
        self._select = dict(select)
        self._query_json = query_json

    def cases(self) -> list[Case]:
        raw = self._query_json(self._select)
        try:
            doc = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(
                f"exploit-corpus query did not return valid JSON: {exc}") from exc
        rows = None
        if isinstance(doc, list):
            rows = doc
        elif isinstance(doc, dict):
            for k in _ROW_KEYS:
                if isinstance(doc.get(k), list):
                    rows = doc[k]
                    break
        if rows is None:
            raise ValueError("exploit-corpus query returned no rows array")
        out: list[Case] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("exploit-corpus row is not a mapping")
            eid = row.get("entry_id")
            if not isinstance(eid, str) or not eid:
                raise ValueError("exploit-corpus row without an 'entry_id'")
            cves = row.get("cves") or []
            cve = cves[0] if isinstance(cves, list) and cves else None
            expected = f"exploit-corpus:enrichment:{cve}" if cve else None
            out.append(Case(
                id=eid,
                data={"cve": cve, "lang": row.get("lang"), "row": row},
                expected_ref=expected,
                source="exploit-corpus",
            ))
        return out


class QueueFileAdapter:
    """Reads a queue-style ``queue.json`` (a list, or a ``queue``/``targets``
    wrapper) of case-id strings or ``{"cve": ..., ...}`` dicts."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def cases(self) -> list[Case]:
        try:
            text = safe_io.read_text_no_symlink(self._path, max_bytes=8 * 1024 * 1024)
        except OSError as exc:
            raise ValueError(f"queue file not readable: {self._path} ({exc})") from exc
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"queue file is not valid JSON: {exc}") from exc
        items = doc
        if isinstance(doc, dict):
            items = None
            for k in _QUEUE_KEYS:
                if isinstance(doc.get(k), list):
                    items = doc[k]
                    break
        if not isinstance(items, list):
            raise ValueError("queue file must be a list or a {queue|targets: [...]}")
        out: list[Case] = []
        for item in items:
            if isinstance(item, str) and item:
                out.append(Case(id=item, data={"cve": item}, source="queue-file"))
            elif isinstance(item, dict):
                cve = item.get("cve")
                if not isinstance(cve, str) or not cve:
                    raise ValueError("queue entry requires a 'cve'")
                out.append(Case(id=cve, data=dict(item), source="queue-file"))
            else:
                raise ValueError("queue entry must be a CVE string or a mapping")
        return out


class IntakeDirAdapter:
    """Reads a directory of ``*.intake.json`` bundles."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def cases(self) -> list[Case]:
        if not self._path.is_dir() or self._path.is_symlink():
            raise ValueError(f"intake dir not a directory: {self._path}")
        out: list[Case] = []
        for f in sorted(self._path.glob("*.intake.json")):
            try:
                text = safe_io.read_text_no_symlink(f, max_bytes=16 * 1024 * 1024)
                data = json.loads(text)
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"malformed intake bundle {f.name}: {exc}") from exc
            if not isinstance(data, dict):
                raise ValueError(f"malformed intake bundle {f.name}: not a mapping")
            build_id = data.get("target", {}).get("build_id") \
                if isinstance(data.get("target"), dict) else None
            cid = build_id if isinstance(build_id, str) and build_id else f.stem
            out.append(Case(id=cid, data=data, source="intake-dir"))
        return out


class PytestAdapter:
    """One Case per collected pytest node id (the tool's own suite as the corpus)."""

    def __init__(self, *, collect_nodeids: Callable[[], list[str]]) -> None:
        self._collect = collect_nodeids

    def cases(self) -> list[Case]:
        return [
            Case(id=nid, data={"nodeid": nid}, source="pytest")
            for nid in self._collect()
        ]


def _default_corpus_query(root: Path) -> Callable[[dict], str]:
    def run(select: dict) -> str:
        args = ["corpus", "query", "--json"]
        for key, flag in _QUERY_FLAGS.items():
            if key in select and select[key] is not None:
                args += [flag, str(select[key])]
        proc = subprocess.run(args, capture_output=True, text=True,
                              cwd=str(root), timeout=120, check=False)
        if proc.returncode != 0:
            raise ValueError(
                f"corpus query failed (rc={proc.returncode}): {proc.stderr.strip()}")
        return proc.stdout
    return run


def _default_pytest_collect(root: Path) -> Callable[[], list[str]]:
    def collect() -> list[str]:
        proc = subprocess.run(
            ["python3", "-m", "pytest", "--collect-only", "-q"],
            capture_output=True, text=True, cwd=str(root), timeout=300, check=False)
        ids: list[str] = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if "::" in line and not line.startswith(("=", "_", "platform", "rootdir")):
                ids.append(line)
        return ids
    return collect


def make_corpus_adapter(
    corpus: Corpus,
    *,
    root: Path,
    query_json: Callable[[dict], str] | None = None,
    collect_nodeids: Callable[[], list[str]] | None = None,
) -> CorpusAdapter:
    """Build the adapter named by ``corpus.adapter``, wiring real defaults."""
    root = Path(root)
    if corpus.adapter == "exploit-corpus":
        return ExploitCorpusAdapter(
            corpus.select, query_json=query_json or _default_corpus_query(root))
    if corpus.adapter == "queue-file":
        rel = corpus.select.get("path")
        if not isinstance(rel, str) or not rel:
            raise ValueError("queue-file corpus requires select.path")
        return QueueFileAdapter(root / rel)
    if corpus.adapter == "intake-dir":
        rel = corpus.select.get("path")
        if not isinstance(rel, str) or not rel:
            raise ValueError("intake-dir corpus requires select.path")
        return IntakeDirAdapter(root / rel)
    if corpus.adapter == "pytest":
        return PytestAdapter(
            collect_nodeids=collect_nodeids or _default_pytest_collect(root))
    raise ValueError(f"unknown corpus adapter: {corpus.adapter}")  # pragma: no cover
