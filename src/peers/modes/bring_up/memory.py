"""Phase-5 — calibrated cross-run memory.

Two trust tiers that NEVER mix:
 - FACTS (trusted): a case passed a REAL oracle sweep, pinned to the tool
   commit-sha at which peers observed it (peers' own observation — never an
   agent-written claim). Used to resume (``known_green``). The honesty floor lives
   here: green is never inherited across a code change unless the operator opts
   into ``trust-cached``.
 - HINTS (untrusted): free-form advisory strings (prior root-causes, working
   flags). Capped by ``hint_budget`` and **never** gate-bearing — a hint can never
   make a case ``known_green``.

The readers (``known_green`` to skip a re-sweep, ``hints`` to seed the Fixer) are
consumed once warm-start/full-resume is wired into the frontend; the writers run
today. Within a single run, ``on-change`` invalidates every fact after the first
landed fix (the tool-sha moves), so a warm-start skip can only ever apply to a
prior run's still-fresh fact — never masking an intra-run regression.

Backed by an append-style JSONL file (rewritten atomically, no-symlink). A torn
trailing line is tolerated on load (fail-soft).
"""
from __future__ import annotations

import json
from pathlib import Path

from peers import safe_io

_MAX_BYTES = 16 * 1024 * 1024


class BringUpMemory:
    def __init__(self, path: Path | None = None, *, mode: str = "hints-only",
                 reverify: str = "on-change", hint_budget: int = 3) -> None:
        self._path = Path(path) if path is not None else None
        self._mode = mode
        self._reverify = reverify
        self._hint_budget = hint_budget
        self._entries: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if not (self._path and self._path.is_file() and not self._path.is_symlink()):
            return []
        out: list[dict] = []
        for line in safe_io.read_text_no_symlink(
                self._path, max_bytes=_MAX_BYTES).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue  # tolerate a torn trailing line — fail-soft
            if isinstance(obj, dict):
                out.append(obj)
        return out

    def _append(self, entry: dict) -> None:
        self._entries.append(entry)
        if self._path is not None:
            text = "".join(
                json.dumps(e, sort_keys=True) + "\n" for e in self._entries)
            safe_io.atomic_write_text_in_dir_no_symlink(self._path, text)

    # ---- writers ----
    def record_fact(self, case_id: str, tool_sha: str) -> None:
        """Trusted tier: a case attested-green at ``tool_sha``."""
        self._append({"kind": "fact", "case_id": case_id, "tool_sha": tool_sha})

    def record_hint(self, case_id: str, tool_sha: str, text: str) -> None:
        """Untrusted tier: an advisory hint for a future run."""
        self._append({"kind": "hint", "case_id": case_id,
                      "tool_sha": tool_sha, "text": text})

    # ---- readers ----
    def _latest_fact(self, case_id: str) -> dict | None:
        found = None
        for e in self._entries:
            if e.get("kind") == "fact" and e.get("case_id") == case_id:
                found = e
        return found

    def known_green(self, case_id: str, *, current_sha: str) -> bool:
        """Whether ``case_id`` may be treated as green WITHOUT re-sweeping it.

        Only FACTS count; hints never do. Honours the staleness floor:
        ``on-change`` trusts a fact only while the tool-sha is unchanged.
        """
        if self._mode in ("off", "hints-only"):
            return False
        if self._reverify == "always":
            return False
        fact = self._latest_fact(case_id)
        if fact is None:
            return False
        if self._reverify == "trust-cached":
            return True
        # on-change: trust only while nothing changed.
        return fact.get("tool_sha") == current_sha

    def hints(self, case_id: str) -> list[str]:
        """Up to ``hint_budget`` most-recent advisory hints (anti-anchoring cap)."""
        if self._mode == "off":
            return []
        texts = [e["text"] for e in self._entries
                 if e.get("kind") == "hint" and e.get("case_id") == case_id
                 and isinstance(e.get("text"), str)]
        if self._hint_budget < 0:
            return texts
        return texts[-self._hint_budget:] if self._hint_budget else []
