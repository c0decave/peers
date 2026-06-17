"""Read-only, fail-soft readers for the TUI. All reads go through peers.safe_io.

Every reader degrades safely: a missing, corrupt, oversized, or symlinked source
yields a safe default (``{}`` / ``[]`` / a zeroed view) and never raises.

``safe_io.read_text_no_symlink(path, max_bytes=...)`` *truncates* at ``max_bytes``
rather than raising. For ``read_state`` the oversize->{} invariant is ENFORCED:
it reads ``max_bytes + 1`` and a result longer than ``max_bytes`` is rejected as
oversized *before* parsing, so a valid-JSON prefix that fits within the cap can
never be parsed out of an oversized file. (For ``tick_entries`` truncation is
correct and intended: the torn final line is simply skipped.)

Two kinds of readers live here:

* *composables* operate on an already-parsed ``state: dict`` and never touch the
  filesystem: ``gate_views``, ``peer_views``, ``budget_view``,
  ``convergence_view``, ``current_peer``.
* *file readers* take a ``path``/``repo`` and read themselves (each fail-soft):
  ``read_state``, ``tick_entries``, ``commit_review_view``, ``bug_views``,
  ``fleet_entries``, ``run_snapshot``, ``plan_progress``, ``commit_diff``,
  ``log_lines``, ``peer_tool``, ``escalation_state``, ``spine_runs``,
  ``autonomy_ledger_view``.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import stat
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from peers import safe_io

from peers_ctl.tui.snapshots import (
    AutonomyLedgerView,
    BudgetView,
    BugView,
    CommitReviewRow,
    ConvergenceView,
    FleetEntry,
    GateSnapshotRow,
    GateView,
    LogRow,
    PeerView,
    PlanStep,
    RunSnapshot,
    SpineRunEntry,
    TickEntry,
)

_STATE_MAX = 5 * 1024 * 1024
_RUNS_MAX = 16 * 1024 * 1024
_BUGS_MAX = 8 * 1024 * 1024
_PROJECTS_MAX = 4 * 1024 * 1024
_MODES_MAX = 256 * 1024
_SPINE_RUN_MAX = 1 * 1024 * 1024
_PLAN_MAX = 1 * 1024 * 1024
# Caps the diff window output in CHARACTERS (len(str)), not bytes — the git
# stdout is decoded to text before this truncation applies.
_DIFF_MAX_CHARS = 512 * 1024
_STOP_REASON_MAX = 64 * 1024
_GIT_DIFF_TIMEOUT_S = 15.0
_CONFIG_MAX = 1 * 1024 * 1024
#: how many bytes of a HALTED.md we surface in the escalation banner excerpt.
_HALTED_EXCERPT_MAX = 4096

#: which CLI tool backs a peer when its config can't be read. claude is the
#: safe default: it is the genuinely-live peer (its session jsonl streams in
#: real time), and the Live panel decoder treats an unknown line as raw anyway.
_DEFAULT_PEER_TOOL = "claude"
#: tool values we trust from config (mirrors peers.peer_spec.KNOWN_TOOLS). An
#: unrecognized value is NOT trusted — we fall back to the default rather than
#: feed an unknown schema to the stream decoder.
_KNOWN_PEER_TOOLS = ("claude", "codex", "opencode")

#: A PLAN.md checklist line: ``- [ ]`` / ``- [x]`` / ``- [X]`` at line-start
#: (leading whitespace allowed). Captures the done-marker and the trailing text.
#: An inline ``- [x]`` mid-prose does NOT match (mirrors stuck_progress's rule).
_PLAN_STEP_RE = re.compile(r"(?m)^[ \t]*-[ \t]*\[([ xX])\][ \t]*(.*)$")
#: Smaller per-project cap for the fleet rollup reads. Each project's
#: ``.peers/state.json`` is normally KB-sized; with many projects the default
#: 5 MiB cap (``_STATE_MAX``) makes the aggregate worst case large, so the fleet
#: rollup reads each state.json under a tighter 1 MiB budget. Behavior is
#: otherwise identical (oversize -> {} -> safe per-entry fallbacks).
_FLEET_STATE_MAX = 1 * 1024 * 1024
_GIT_LOG_TIMEOUT_S = 30.0

# Severities that block convergence (mirrors peers.bug_hunt.BLOCKING_SEVERITIES).
_BLOCKING_SEVERITIES = frozenset({"crit", "high", "med"})


def read_state(path: Path, *, max_bytes: int = _STATE_MAX) -> dict:
    """Return parsed state.json, or {} on any problem (missing/corrupt/oversized/symlink).

    The oversize->{} invariant is ENFORCED, not best-effort: ``safe_io`` truncates
    at the cap, so we read ``max_bytes + 1`` and reject any result longer than
    ``max_bytes`` *before* parsing. This stops a valid-JSON prefix that fits the
    cap (followed by huge trailing bytes) from being parsed out of an oversized
    file. With multibyte content the ``max_bytes + 1`` truncation may cut a
    multibyte character so the (character-length) check doesn't fire, but the
    truncated bytes then fail JSON parsing — which is what enforces the cap.
    """
    try:
        text = safe_io.read_text_no_symlink(path, max_bytes=max_bytes + 1)
    except (OSError, ValueError):
        return {}
    if len(text) > max_bytes:
        return {}  # oversized -> refuse before parsing
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def gate_views(state: dict, *, soft_needed: dict[str, int] | None = None) -> list[GateView]:
    """Build hard + soft gate views from a run state dict.

    Hard gates live in ``state["goals_status"][gid] = {state,diagnostic,duration_ms}``;
    a cached hard PASS has ``duration_ms == 0`` -> ``cached=True``. Soft gates live in
    ``state["soft_status"][gid] = {consensus_count, ...}``; ``state["stuck_counter"]``
    is ``{gid:int}`` holding ONLY currently-stuck gates (missing gid -> 0). The soft
    need-count comes from ``soft_needed`` (default 2).

    NOTE on ``cached``: a hard PASS with ``duration_ms == 0`` is inferred to be a
    cache hit, following the substrate's 0ms cache-hit convention. A genuine
    sub-millisecond PASS is indistinguishable from a cached one under this rule.
    """
    if not isinstance(state, dict):
        return []
    soft_needed = soft_needed or {}
    out: list[GateView] = []
    stuck = state.get("stuck_counter", {})
    if not isinstance(stuck, dict):
        stuck = {}
    for gid, g in (state.get("goals_status") or {}).items():
        if not isinstance(g, dict):
            continue
        dur = _coerce_int(g.get("duration_ms", 0))
        out.append(GateView(
            id=gid, kind="hard", state=str(g.get("state", "unknown")),
            stuck=_coerce_int(stuck.get(gid, 0)), duration_ms=dur,
            diagnostic=str(g.get("diagnostic", "")),
            cached=(g.get("state") == "pass" and dur == 0), consensus=None,
        ))
    for gid, s in (state.get("soft_status") or {}).items():
        if not isinstance(s, dict):
            continue
        count = _coerce_int(s.get("consensus_count", 0))
        need = _coerce_int(soft_needed.get(gid, 2), 2)
        out.append(GateView(
            id=gid, kind="soft",
            state="reached" if count >= need else "pending",
            stuck=0, duration_ms=0, diagnostic="",
            cached=False, consensus=(count, need),
        ))
    return out


def convergence_view(state: dict) -> ConvergenceView:
    """Build the convergence view.

    ``consecutive_clean_ticks`` is present generally (default 0). The phase fields
    ``convergence_phase`` / ``phase_b_extra_ticks`` exist ONLY in implement-mode and
    are absent otherwise -> read with ``.get()`` so absent surfaces as ``None``.

    A non-dict ``state`` degrades to the zero/None default (never raises).
    """
    if not isinstance(state, dict):
        state = {}
    return ConvergenceView(
        consecutive_clean_ticks=_coerce_int(
            state.get("consecutive_clean_ticks", 0)
        ),
        convergence_phase=_opt_str(state.get("convergence_phase")),  # None outside implement-mode
        phase_b_extra_ticks=_coerce_opt_int(state.get("phase_b_extra_ticks")),
    )


def _coerce_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def peer_views(state: dict) -> list[PeerView]:
    """Build per-peer views from ``state["peers"]``.

    FOUR states (``healthy|degraded|halted|unavailable``); an unknown state string
    is passed through for display rather than dropped. ``recent_runs`` entries are
    ``bool | float`` (0.5 = productive-no-handoff) and are NOT coerced to bool.
    ``consecutive_fails`` is a float. ``last_run`` is SPARSE -> kept as-is for
    ``.get()`` access. Missing/garbage peer maps degrade to ``[]``.
    """
    peers = state.get("peers") if isinstance(state, dict) else None
    if not isinstance(peers, dict):
        return []
    out: list[PeerView] = []
    for name, t in peers.items():
        if not isinstance(t, dict):
            continue
        runs = t.get("recent_runs")
        last_run = t.get("last_run")
        out.append(PeerView(
            name=str(name),
            state=str(t.get("state", "unavailable")),
            consecutive_fails=_coerce_float(t.get("consecutive_fails", 0.0)),
            recent_runs=list(runs) if isinstance(runs, list) else [],
            last_run=last_run if isinstance(last_run, dict) else {},
        ))
    return out


def current_peer(state: dict) -> str | None:
    """Return ``peer_order[turn_index]`` or None on missing/out-of-bounds/garbage."""
    if not isinstance(state, dict):
        return None
    order = state.get("peer_order")
    idx = state.get("turn_index")
    # Guard: peer_order must be a list; turn_index a real int (bool is an int
    # subclass, so reject it explicitly) within bounds.
    if not isinstance(order, list) or not isinstance(idx, int) or isinstance(idx, bool):
        return None
    if 0 <= idx < len(order):
        peer = order[idx]
        return str(peer) if peer is not None else None
    return None


def _coerce_int(value: object, default: int = 0) -> int:
    try:
        return int(cast(Any, value))
    except (TypeError, ValueError, OverflowError):
        return default


def _coerce_opt_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(cast(Any, value))
    except (TypeError, ValueError, OverflowError):
        return None


def _coerce_opt_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _opt_str(value: object) -> str | None:
    """Coerce a ``str | None`` field: keep None, stringify anything else.

    A malformed line (e.g. ``{"peer": 99}``) must not leak a non-str onto a
    ``str | None`` field.
    """
    return None if value is None else str(value)


def budget_view(state: dict) -> BudgetView:
    """Build the budget view from ``state["budget"]``.

    Caps (``max_*``) are optional -> None when absent. ``max_usd_mode`` is a string
    knob (not an operator cap) paired with ``max_usd_mode_reason``.
    ``wasted_runtime_per_tick`` is a capped(20) list of ``{iteration,peer,duration_s}``;
    absent or non-list -> ``[]``. A missing budget block degrades to zeros/None/[].
    """
    b = state.get("budget") if isinstance(state, dict) else None
    if not isinstance(b, dict):
        b = {}
    wasted = b.get("wasted_runtime_per_tick")
    return BudgetView(
        spent_runtime_s=_coerce_int(b.get("spent_runtime_s", 0)),
        max_runtime_s=_coerce_opt_int(b.get("max_runtime_s")),
        spent_tokens=_coerce_int(b.get("spent_tokens", 0)),
        max_tokens=_coerce_opt_int(b.get("max_tokens")),
        spent_usd=_coerce_float(b.get("spent_usd", 0.0)),
        max_usd=_coerce_opt_float(b.get("max_usd")),
        max_usd_mode=_opt_str(b.get("max_usd_mode")),
        max_usd_mode_reason=_opt_str(b.get("max_usd_mode_reason")),
        consecutive_failures=_coerce_int(b.get("consecutive_failures", 0)),
        wasted_runtime=list(wasted) if isinstance(wasted, list) else [],
    )


def _tick_from_entry(entry: dict) -> TickEntry:
    """Map one decoded runs.jsonl object into a TickEntry.

    The synthetic ``{"event":"exit",...}`` line carries no
    ``iteration``/``peer``/``classification`` and is flagged via ``is_exit``.
    """
    if entry.get("event") == "exit":
        return TickEntry(
            iteration=None, peer=None, classification=None, success=None,
            tokens=0, usd=0.0, head_before=None, head_after=None, warnings=[],
            ts=str(entry.get("ts", "")), is_exit=True,
            exit_reason=_opt_str(entry.get("reason")),
        )
    warnings = entry.get("warnings_emitted")
    return TickEntry(
        iteration=_coerce_opt_int(entry.get("iteration")),
        peer=_opt_str(entry.get("peer")),
        classification=_opt_str(entry.get("classification")),
        success=entry.get("success") if isinstance(entry.get("success"), bool) else None,
        tokens=_coerce_int(entry.get("tokens_this_tick", 0)),
        usd=_coerce_float(entry.get("usd_this_tick", 0.0)),
        head_before=_opt_str(entry.get("head_before")),
        head_after=_opt_str(entry.get("head_after")),
        warnings=list(warnings) if isinstance(warnings, list) else [],
        ts=str(entry.get("ts", "")),
    )


def tick_entries(path: Path, *, max_bytes: int = _RUNS_MAX) -> list[TickEntry]:
    """Parse runs.jsonl into TickEntry rows, fail-soft.

    ONE JSON object per line. Blank lines and any line that does not decode as a
    JSON object are skipped (this covers a torn/incomplete FINAL line). The
    synthetic ``{"event":"exit",...}`` line is surfaced with ``is_exit=True``.
    Missing/oversized/symlinked file -> ``[]``.
    """
    try:
        text = safe_io.read_text_no_symlink(path, max_bytes=max_bytes)
    except (OSError, ValueError):
        return []
    out: list[TickEntry] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue  # torn tail / malformed line -> skip
        if not isinstance(entry, dict):
            continue
        out.append(_tick_from_entry(entry))
    return out


def _count_gates(gates: dict) -> tuple[int, int]:
    """Count (green, total) over a per-tick gates snapshot.

    Hard gates are green when ``state == "pass"``. Soft gates carry an
    ``"<count>/<needed>"`` string and are green when ``count >= needed``. A
    malformed soft string counts toward the total but not green. The
    ``"_truncated"`` marker (if present) is NOT a gate and is ignored."""
    green = 0
    total = 0
    hard = gates.get("hard")
    if isinstance(hard, dict):
        for st in hard.values():
            total += 1
            if st == "pass":
                green += 1
    soft = gates.get("soft")
    if isinstance(soft, dict):
        for cons in soft.values():
            total += 1
            if isinstance(cons, str) and "/" in cons:
                left, _, right = cons.partition("/")
                try:
                    if int(left) >= int(right):
                        green += 1
                except (TypeError, ValueError):
                    pass
    return green, total


def _parse_ts(ts: object) -> datetime | None:
    """Best-effort ISO-8601 parse; ``None`` on anything unparseable."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def gate_history(path: Path, *, max_bytes: int = _RUNS_MAX) -> list[GateSnapshotRow]:
    """Reconstruct the per-tick gate-stand history from ``runs.jsonl``, fail-soft.

    Only lines that CARRY a per-tick ``gates`` map (Wave-2 §5.2) become rows;
    pre-Wave-2 lines without it and the synthetic ``{"event":"exit",...}`` line
    are skipped. ``gap_s`` on each row is the seconds since the PREVIOUS row's
    ts (the tick duration/gap), ``None`` for the first row or an unparseable ts.

    Mirrors ``tick_entries``' robustness: blank/torn/non-object lines and any
    line whose ``gates`` value is not a dict are skipped; a missing / oversized
    / symlinked file yields ``[]``. Never raises."""
    try:
        text = safe_io.read_text_no_symlink(path, max_bytes=max_bytes)
    except (OSError, ValueError):
        return []
    out: list[GateSnapshotRow] = []
    prev_dt: datetime | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue  # torn tail / malformed line -> skip
        if not isinstance(entry, dict):
            continue
        if entry.get("event") == "exit":
            continue  # synthetic exit line carries no real gate stand
        gates = entry.get("gates")
        if not isinstance(gates, dict):
            continue  # pre-Wave-2 line (no snapshot) or garbage -> skip
        ts = str(entry.get("ts", ""))
        cur_dt = _parse_ts(ts)
        gap_s: float | None = None
        if cur_dt is not None and prev_dt is not None:
            try:
                gap_s = (cur_dt - prev_dt).total_seconds()
            except (TypeError, ValueError):
                gap_s = None
        if cur_dt is not None:
            prev_dt = cur_dt
        green, total = _count_gates(gates)
        out.append(GateSnapshotRow(
            iteration=_coerce_opt_int(entry.get("iteration")),
            ts=ts, gates=gates, green=green, total=total, gap_s=gap_s,
        ))
    return out


def gate_snapshot_views(row: GateSnapshotRow) -> list[GateView]:
    """Map one ``GateSnapshotRow`` into ``GateView`` rows so the Gates panel can
    render a PAST tick with the SAME coloring/formatting as the live view.

    Pure + total: a non-dict ``gates`` or a malformed soft ``"n/m"`` string is
    rendered defensively (pending, no consensus) and never raises. Historical
    snapshots carry no ``stuck``/``duration``/``diagnostic``/``cached`` data
    (those are live-only), so those fields are zeroed."""
    gates = getattr(row, "gates", None)
    if not isinstance(gates, dict):
        return []
    out: list[GateView] = []
    hard = gates.get("hard")
    if isinstance(hard, dict):
        for gid, st in hard.items():
            st_s = str(st) if st in ("pass", "fail") else "unknown"
            out.append(GateView(
                id=str(gid), kind="hard", state=st_s, stuck=0, duration_ms=0,
                diagnostic="", cached=False, consensus=None,
            ))
    soft = gates.get("soft")
    if isinstance(soft, dict):
        for gid, cons in soft.items():
            count = needed = None
            if isinstance(cons, str) and "/" in cons:
                left, _, right = cons.partition("/")
                try:
                    count, needed = int(left), int(right)
                except (TypeError, ValueError):
                    count = needed = None
            reached = (
                count is not None and needed is not None and count >= needed
            )
            out.append(GateView(
                id=str(gid), kind="soft",
                state="reached" if reached else "pending",
                stuck=0, duration_ms=0, diagnostic="", cached=False,
                consensus=((count, needed)
                           if count is not None and needed is not None
                           else None),
            ))
    return out


# Field separator WITHIN a record. ASCII unit-separator (0x1f) cannot appear in
# a sha or in a substrate-written note. It CAN, in principle, appear in an
# attacker-controlled commit subject/body, so the record fields are ordered
# [sha, note, subject, body] (note before the free-text subject) and the body
# re-absorbs any leftover separators. With ``git log -z`` each whole record is
# NUL-terminated, so we split the stream on NUL into clean records, then each
# record on the unit-separator.
_FIELD_SEP = "\x1f"

#: Full notes ref the substrate writes attribution to (peers.attest.NOTES_REF).
#: We inline this note via ``--notes=<ref>`` + ``%N`` so the attestation read is
#: folded into the SAME bounded ``git log`` call (no per-commit ``git notes
#: show`` fork). See ``commit_review_view`` for the equivalence rationale.
_ATTEST_NOTES_REF = "refs/notes/peers-attest"


def commit_review_view(repo: Path, *, limit: int = 20) -> list[CommitReviewRow]:
    """Read the last ``limit`` commits with parsed trailers + an attestation badge.

    Runs a SINGLE bounded ``git -C <repo> log -z -n <limit>
    --notes=refs/notes/peers-attest --format=%H%x1f%N%x1f%s%x1f%B`` (NUL-
    terminated records, unit-separated fields), parses trailers with
    ``peers.comm_layer.parse_trailers``, and reads the substrate attestation
    INLINE from the ``%N`` notes field.

    SECURITY (field-order matters): the commit SUBJECT ``%s`` is attacker-
    controlled and CAN contain the unit-separator ``\\x1f``, which would shift the
    split fields. The substrate-written note ``%N`` is therefore placed BEFORE the
    free-text subject so an embedded separator in the subject can only bleed into
    the trailing BODY (joined back together), never into the NOTE slot. This stops
    a crafted subject like ``"innocent\\x1fclaude"`` from forging an
    ``attested_peer`` / ``attest_match`` with no real ``peers-attest`` note. (The
    note content is the bare peer name written by the substrate and contains no
    separator.)

    Why inline (not ``peers.attest.attested_peer`` per commit): the substrate
    writes attribution as ``git notes --ref=peers-attest add -f -m <peer>``, so
    the note content is exactly the peer name, and ``attested_peer`` returns just
    ``git notes show``'s stdout stripped (no extra validation). Therefore
    ``%N.strip()`` is EQUIVALENT to ``attested_peer(repo, sha)``. Folding it into
    the existing ``git log`` makes the WHOLE attestation read bounded by the one
    30s timeout and removes the N+1 unbounded ``git notes show`` forks that could
    hang on a slow/locked repo. Fail-soft is preserved: a repo with NO notes ref
    just yields empty ``%N`` (``attested_peer=None``); the ``--notes`` flag is
    inert when the ref is absent.

    ``attest_match`` is True ONLY when an attestation EXISTS and equals the
    ``Peer:`` trailer. Absence of an attestation -> False (NOT a forgery alarm);
    a present attestation that disagrees with the trailer -> False (the forgery
    signal). Any git error / non-repo dir -> ``[]`` (fail-soft, never raises).
    """
    # Lazy import: keep module import-time light and Textual-free, and avoid
    # paying for the comm_layer import unless this reader is used.
    from peers.comm_layer import parse_trailers

    try:
        n = int(limit)
    except (TypeError, ValueError):
        return []
    if n <= 0:
        return []
    # Field order: sha, NOTE (substrate-written, separator-free), then the
    # attacker-controlled subject + body. Putting %N before %s means a \x1f
    # smuggled into the subject can only spill into the body, never the note.
    fmt = f"%H{_FIELD_SEP}%N{_FIELD_SEP}%s{_FIELD_SEP}%B"
    try:
        cp = subprocess.run(
            ["git", "-C", str(repo), "log", "-z", "-n", str(n),
             f"--notes={_ATTEST_NOTES_REF}", f"--format={fmt}"],
            capture_output=True, text=True, check=False,
            timeout=_GIT_LOG_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if cp.returncode != 0:
        return []
    rows: list[CommitReviewRow] = []
    # ``-z`` NUL-terminates each record; the stream ends with a trailing NUL, so
    # the final split element is an empty string -> skipped by the empty-sha guard.
    for record in cp.stdout.split("\x00"):
        if not record:
            continue
        fields = record.split(_FIELD_SEP)
        if len(fields) < 4:
            continue  # malformed / truncated record -> skip
        sha = fields[0].strip()
        note = fields[1]      # substrate-written; separator-free (before subject)
        subject = fields[2]   # attacker-controlled free text
        body = _FIELD_SEP.join(fields[3:])  # body absorbs any sep in subject/body
        if not sha:
            continue
        try:
            trailers = parse_trailers(body)
        except Exception:
            trailers = {}
        if not isinstance(trailers, dict):
            trailers = {}
        trailer_peer = trailers.get("Peer")
        # Inline attestation: %N strip is equivalent to attested_peer(repo, sha)
        # (see docstring). Empty note -> None (absence, not a forgery alarm).
        attested = note.strip() or None
        attest_match = attested is not None and attested == trailer_peer
        rows.append(CommitReviewRow(
            sha=sha,
            subject=subject.strip(),
            trailers=trailers,
            trailer_peer=trailer_peer,
            attested_peer=attested,
            attest_match=attest_match,
        ))
    return rows


def _bug_from_entry(entry: dict) -> BugView:
    """Map one decoded bugs.jsonl object into a BugView (sparse-tolerant)."""
    return BugView(
        id=str(entry.get("id", "")),
        severity=str(entry.get("severity", "")).lower(),
        title=str(entry.get("title", "")),
        status=str(entry.get("status", "")).lower(),
        filed_tick=_coerce_opt_int(entry.get("filed_tick")),
        resolved_tick=_coerce_opt_int(entry.get("resolved_tick")),
        author=_opt_str(entry.get("author")),
    )


def bug_views(bugs_jsonl_path: Path, *, max_bytes: int = _BUGS_MAX) -> list[BugView]:
    """Parse ``.peers/bugs.jsonl`` into BugView rows, fail-soft.

    ONE JSON object per line. Blank lines, undecodable lines (incl. a torn final
    line), and decoded values that are not JSON objects are all skipped. A
    missing/oversized/symlinked file -> ``[]`` (never raises).
    """
    try:
        text = safe_io.read_text_no_symlink(bugs_jsonl_path, max_bytes=max_bytes)
    except (OSError, ValueError):
        return []
    out: list[BugView] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue
        out.append(_bug_from_entry(entry))
    return out


def blocking_open(bugs: list[BugView]) -> int:
    """Count OPEN bugs at a blocking severity (crit/high/med).

    A non-list input degrades to 0 (never raises).
    """
    if not isinstance(bugs, list):
        return 0
    return sum(
        1 for b in bugs
        if isinstance(b, BugView)
        and b.status == "open"
        and b.severity in _BLOCKING_SEVERITIES
    )


def _default_config_dir() -> Path:
    """The config dir peers_ctl uses (XDG_CONFIG_HOME/peers-ctl else ~/.config/peers-ctl)."""
    # Defer to store.default_config_dir for one source of truth; fall back to the
    # same rule inline if the import is somehow unavailable (fail-soft).
    try:
        from peers_ctl.store import default_config_dir
        return default_config_dir()
    except Exception:
        import os
        base = os.environ.get("XDG_CONFIG_HOME")
        if base:
            return Path(base) / "peers-ctl"
        return Path.home() / ".config" / "peers-ctl"


def _project_alert(peer_dir: Path, state: dict) -> bool:
    """alert = .peers/HALTED.md exists OR there are pending warnings in state."""
    try:
        if (peer_dir / "HALTED.md").exists():
            return True
    except OSError:
        pass
    warnings = state.get("warnings") if isinstance(state, dict) else None
    return isinstance(warnings, list) and len(warnings) > 0


def fleet_entries(*, config_dir: Path | None = None) -> list[FleetEntry]:
    """Read the project registry (NO reconcile) into FleetEntry rows.

    CRITICAL: this never calls ``peers-ctl list``/``status`` or ``reconcile()``
    (those shell ``podman ps``). It reads ``<config_dir>/projects.yaml`` directly
    via ``safe_io`` + ``yaml.safe_load`` — a pure, side-effect-free read (it does
    NOT seed/create the registry the way ``store.Store`` would).

    ``state``/``pid`` come straight from the registry record. ``iteration`` and the
    gate tally are read from each project's ``<path>/.peers/state.json`` directly
    (via :func:`read_state` + :func:`gate_views`). ``alert`` is True when
    ``<path>/.peers/HALTED.md`` exists or the state carries pending warnings.

    Fail-soft: a missing/corrupt/oversized registry, or any per-project error,
    degrades to ``[]`` / safe per-entry fallbacks (never raises).
    """
    import yaml

    cfg = Path(config_dir) if config_dir is not None else _default_config_dir()
    projects_path = cfg / "projects.yaml"
    try:
        text = safe_io.read_text_no_symlink(projects_path, max_bytes=_PROJECTS_MAX)
    except (OSError, ValueError):
        return []
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    if not isinstance(raw, dict):
        return []
    projects = raw.get("projects")
    if not isinstance(projects, list):
        return []

    out: list[FleetEntry] = []
    for rec in projects:
        if not isinstance(rec, dict):
            continue
        name = rec.get("name")
        path = rec.get("path")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(path, str) or not path:
            continue
        state_str = str(rec.get("state", "unknown")) if rec.get("state") else "unknown"
        pid = _coerce_opt_int(rec.get("pid"))
        # Read the per-project state.json directly (no reconcile). A missing /
        # vanished path yields {} -> safe fallbacks.
        peer_dir = Path(path) / ".peers"
        # Tighter cap for the fleet rollup: these state.json files are normally
        # KB-sized, so cap aggregate worst case across many projects.
        st = read_state(peer_dir / "state.json", max_bytes=_FLEET_STATE_MAX)
        iteration = _coerce_opt_int(st.get("iteration")) if st else None
        gates = gate_views(st) if st else []
        if gates:
            gates_total: int | None = len(gates)
            gates_green: int | None = sum(
                1 for g in gates
                if (g.kind == "hard" and g.state == "pass")
                or (g.kind == "soft" and g.state == "reached")
            )
        else:
            gates_total = None
            gates_green = None
        alert = _project_alert(peer_dir, st)
        out.append(FleetEntry(
            name=name,
            path=path,
            state=state_str,
            pid=pid,
            iteration=iteration,
            gates_green=gates_green,
            gates_total=gates_total,
            alert=alert,
        ))
    return out


def _detect_mode(peer_dir: Path, state: dict) -> str | None:
    """Active mode from ``.peers/modes-applied.txt`` (2nd token per line) else
    from ``state["mode"]``. Returns None when neither is available.

    The trail lines are ``<timestamp> <mode-name> <version> <sha256=...>``;
    multiple modes are joined with ``+`` (e.g. ``audit+thorough``).
    """
    try:
        text = safe_io.read_text_no_symlink(peer_dir / "modes-applied.txt",
                                            max_bytes=_MODES_MAX)
    except (OSError, ValueError):
        text = ""
    names: list[str] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            names.append(parts[1])
    if names:
        return "+".join(names)
    mode = state.get("mode") if isinstance(state, dict) else None
    return _opt_str(mode)


def run_snapshot(project_path: Path, name: str) -> RunSnapshot:
    """Compose the per-run view over ``<project_path>/.peers/state.json``.

    Reads state via :func:`read_state` and composes the Unit-B readers
    (:func:`gate_views`, :func:`peer_views`, :func:`budget_view`,
    :func:`convergence_view`, :func:`current_peer`). ``mode`` comes from
    ``.peers/modes-applied.txt`` if present, else ``state["mode"]``. A missing
    state.json -> a RunSnapshot with ``state_present=False`` and safe defaults
    (never raises).
    """
    peer_dir = Path(project_path) / ".peers"
    state = read_state(peer_dir / "state.json")
    if not state:
        return RunSnapshot(
            name=str(name), state_present=False, iteration=0, mode=None,
            phase=None, current_peer=None,
        )
    warnings_raw = state.get("warnings")
    return RunSnapshot(
        name=str(name),
        state_present=True,
        iteration=_coerce_int(state.get("iteration", 0)),
        mode=_detect_mode(peer_dir, state),
        phase=_opt_str(state.get("phase")),
        current_peer=current_peer(state),
        gates=gate_views(state),
        peers=peer_views(state),
        budget=budget_view(state),
        convergence=convergence_view(state),
        warnings=list(warnings_raw) if isinstance(warnings_raw, list) else [],
    )


def plan_progress(
    project_path: Path, *, max_bytes: int = _PLAN_MAX,
) -> tuple[int, int, list[PlanStep]]:
    """Count PLAN checklist progress for the Tasks/Steps window. Fail-soft.

    Reads ``<project_path>/.peers/PLAN.original.md`` (the frozen,
    operator-supplied copy) in preference to ``<project_path>/.peers/PLAN.md``
    (peer-editable during checkoff), mirroring ``skeptic_engine``'s precedence.
    Counts line-start ``- [x]`` / ``- [X]`` (done) vs ``- [ ]`` (open) checklist
    items; an inline ``- [x]`` mid-prose does NOT count.

    Returns ``(done, total, steps)`` where ``steps`` are the parsed
    :class:`PlanStep` rows in file order. The checkoff author is not part of the
    canonical line format (``- [x] [STEP-N] text (sha)``) so ``PlanStep.author``
    is left ``None`` here — the trustworthy author is the substrate attestation
    in the Konsens window, never a forgeable inline annotation.

    A missing/corrupt/oversized/symlinked PLAN (neither file readable) ->
    ``(0, 0, [])`` (never raises).
    """
    peer_dir = Path(project_path) / ".peers"
    text = ""
    for name in ("PLAN.original.md", "PLAN.md"):
        try:
            text = safe_io.read_text_no_symlink(peer_dir / name, max_bytes=max_bytes + 1)
        except (OSError, ValueError):
            text = ""
            continue
        if len(text) > max_bytes:
            # oversized -> refuse this file; try the next candidate.
            text = ""
            continue
        if text:
            break
    if not text:
        return (0, 0, [])
    steps: list[PlanStep] = []
    done = 0
    for marker, body in _PLAN_STEP_RE.findall(text):
        is_done = marker in ("x", "X")
        if is_done:
            done += 1
        steps.append(PlanStep(done=is_done, text=body.strip(), author=None))
    return (done, len(steps), steps)


#: A safe git revision for the diff window: a hex object name (4-64 hex chars)
#: or the literal ``HEAD``. The ``sha`` arrives from agent-writable ``.peers/``
#: data (e.g. a ``runs.jsonl`` ``head_after``) with no upstream validation, so a
#: hostile value like ``--output=/tmp/PWNED`` would make ``git show --output=...``
#: WRITE A FILE. We reject anything that is not a plain hex sha or ``HEAD`` BEFORE
#: shelling out (defense layer 1; layer 2 is ``--end-of-options`` below).
_SHA_RE = re.compile(r"[0-9a-fA-F]{4,64}")


def commit_diff(
    repo: Path, sha: str | None, *, max_bytes: int = _DIFF_MAX_CHARS,
) -> str:
    """Return ``git show --stat --patch <sha>`` for the Diff window. Fail-soft.

    Runs a SINGLE bounded ``git -C <repo> show --stat --patch --no-color
    --end-of-options <sha> --`` (list-arg argv, no shell, a 15s timeout) and
    returns its stdout truncated to ``max_bytes`` characters.

    SECURITY (defense in depth): the ``sha`` is UNTRUSTED — it can originate from
    agent-writable ``.peers/`` data (e.g. a ``runs.jsonl`` ``head_after``) that is
    read with no hex validation. A trailing ``--`` does NOT stop git from parsing
    options that come BEFORE the sha, so a hostile sha like ``--output=/tmp/PWNED``
    would make ``git show --output=...`` WRITE AN ARBITRARY FILE. Two independent
    layers prevent option injection:

    1. ``--end-of-options`` is inserted immediately before the sha so git treats
       everything after it as a non-option (revision/pathspec), never a flag.
    2. The sha is validated against ``_SHA_RE`` (hex 4-64) or the literal
       ``HEAD`` BEFORE shelling out; anything else returns ``""`` and never runs
       git at all.

    A non-repo dir, a bad/unknown/empty/option-like sha, a git error, or a
    timeout all degrade to ``""`` (never raises). An oversized diff is truncated
    to the cap.
    """
    if not sha or not isinstance(sha, str):
        return ""
    s = sha.strip()
    if not s:
        return ""
    # Defense layer 2 (validate before shelling): only a plain hex object name or
    # the literal HEAD is allowed. Reject option-like / pathspec-like shas here so
    # git is never invoked with attacker-controlled flags.
    if s != "HEAD" and not _SHA_RE.fullmatch(s):
        return ""
    try:
        cp = subprocess.run(
            # Defense layer 1: --end-of-options pins everything after it as a
            # non-option, so even a future bypass of the validation above cannot
            # smuggle a git flag through the sha.
            ["git", "-C", str(repo), "show", "--stat", "--patch",
             "--no-color", "--end-of-options", s, "--"],
            capture_output=True, text=True, check=False,
            timeout=_GIT_DIFF_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if cp.returncode != 0:
        return ""
    out = cp.stdout or ""
    if len(out) > max_bytes:
        out = out[:max_bytes]
    return out


def _stop_reason_row(peer_dir: Path) -> LogRow | None:
    """Read ``.peers/last-stop-reason.txt`` (format ``<reason> <iso_ts>\\n``)
    into a single ``kind="stop"`` LogRow, or None when absent/unreadable."""
    try:
        text = safe_io.read_text_no_symlink(
            peer_dir / "last-stop-reason.txt", max_bytes=_STOP_REASON_MAX)
    except (OSError, ValueError):
        return None
    text = text.strip()
    if not text:
        return None
    # Format is "<reason> <iso_utc_timestamp>"; the timestamp is the last token.
    parts = text.split()
    ts = parts[-1] if len(parts) >= 2 else ""
    return LogRow(kind="stop", text=text, ts=ts, iteration=None)


def log_lines(project_path: Path, *, limit: int = 200) -> list[LogRow]:
    """Merge recent run-log events for the Log window. Fail-soft.

    Sources (each read independently, each degrading to nothing on error):

    * ``state['warnings_history']`` — a bounded list of ``{ts, iter, w}`` rows
      the substrate appends per tick. Surfaced as ``kind="warning"`` rows.
    * ``.peers/last-stop-reason.txt`` — the clean-exit sentinel
      (``<reason> <iso_ts>``). Surfaced as a single ``kind="stop"`` row.

    The warnings are capped to the most recent ``limit`` (newest survive), then
    the stop row (if any) is appended. A missing/corrupt state.json yields no
    warning rows but the stop sentinel is still read. Returns ``[]`` when nothing
    is available (never raises). The controller log file is intentionally NOT
    tailed here — it is not trivially locatable in Wave 1.
    """
    peer_dir = Path(project_path) / ".peers"
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = 200
    if n < 0:
        n = 0
    rows: list[LogRow] = []
    state = read_state(peer_dir / "state.json")
    hist = state.get("warnings_history") if isinstance(state, dict) else None
    if isinstance(hist, list):
        # keep only the most recent `n` warnings (the list is append-ordered).
        recent = hist[-n:] if n else []
        for h in recent:
            if not isinstance(h, dict):
                continue
            rows.append(LogRow(
                kind="warning",
                text=str(h.get("w", "")),
                ts=str(h.get("ts", "")),
                iteration=_coerce_opt_int(h.get("iter")),
            ))
    stop = _stop_reason_row(peer_dir)
    if stop is not None:
        rows.append(stop)
    return rows


def peer_tool(
    project_path: Path, peer_name: str | None, *, max_bytes: int = _CONFIG_MAX,
) -> str:
    """Return the CLI tool (``claude`` | ``codex`` | ``opencode``) backing a peer.

    Reads ``<project_path>/.peers/config.yaml`` and resolves the peer's ``tool``
    from the new ``peers:`` list shape OR the legacy ``tools:`` map shape (in the
    legacy shape the map key IS both the peer name and its tool). The Live-Stream
    window uses this to choose its source: a ``claude`` peer streams genuinely
    live via its session jsonl (``peers-ctl peek``); ``codex``/``opencode`` tail
    the per-tick stdout log.

    Fail-soft + total: a missing/corrupt/oversized/symlinked config, an unknown
    peer name, or a ``tool`` value outside the known set all fall back to
    ``claude`` (the genuinely-live default) — this NEVER raises. claude is the
    safe default because its decoder fails soft to raw on any unexpected line."""
    if not peer_name:
        return _DEFAULT_PEER_TOOL
    peer_dir = Path(project_path) / ".peers"
    try:
        text = safe_io.read_text_no_symlink(
            peer_dir / "config.yaml", max_bytes=max_bytes + 1,
        )
    except (OSError, ValueError):
        return _DEFAULT_PEER_TOOL
    if not text or len(text) > max_bytes:
        return _DEFAULT_PEER_TOOL
    import yaml
    try:
        cfg = yaml.safe_load(text)
    except yaml.YAMLError:
        return _DEFAULT_PEER_TOOL
    if not isinstance(cfg, dict):
        return _DEFAULT_PEER_TOOL
    tool: str | None = None
    # new shape: `peers:` is an ordered list of {name, tool, ...}.
    peers = cfg.get("peers")
    if isinstance(peers, list):
        for entry in peers:
            if isinstance(entry, dict) and entry.get("name") == peer_name:
                t = entry.get("tool")
                if isinstance(t, str):
                    tool = t
                break
    # legacy shape: `tools:` is a {name: {...}} map; the key is name == tool.
    if tool is None:
        tools = cfg.get("tools")
        if isinstance(tools, dict) and peer_name in tools:
            tool = peer_name
    if tool in _KNOWN_PEER_TOOLS:
        return tool
    return _DEFAULT_PEER_TOOL


def _open_child_dir_no_follow(parent_fd: int, name: str, display: Path) -> int:
    if name in ("", ".", "..") or Path(name).name != name:
        raise ValueError(f"directory component must be plain: {name!r}")
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        st = os.fstat(fd)
        lst = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(lst.st_mode):
            raise OSError(f"refusing symlinked directory: {display}")
        if not stat.S_ISDIR(st.st_mode):
            raise OSError(f"refusing non-directory: {display}")
        if (st.st_dev, st.st_ino) != (lst.st_dev, lst.st_ino):
            raise OSError(f"refusing swapped directory: {display}")
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_nested_dir_no_follow(root: Path, rel_parts: tuple[str, ...]) -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    fds: list[int] = []
    try:
        root_fd = os.open(str(root), flags)
        fds.append(root_fd)
        root_st = os.fstat(root_fd)
        root_lst = root.lstat()
        if stat.S_ISLNK(root_lst.st_mode):
            raise OSError(f"refusing symlinked root: {root}")
        if not stat.S_ISDIR(root_st.st_mode):
            raise OSError(f"refusing non-directory root: {root}")
        if (root_st.st_dev, root_st.st_ino) != (
            root_lst.st_dev, root_lst.st_ino
        ):
            raise OSError(f"refusing swapped root: {root}")
        parent_fd = root_fd
        display = root
        for name in rel_parts:
            display = display / name
            child_fd = _open_child_dir_no_follow(parent_fd, name, display)
            fds.append(child_fd)
            parent_fd = child_fd
        return os.dup(parent_fd)
    finally:
        for fd in reversed(fds):
            try:
                os.close(fd)
            except OSError:
                pass


def _newest_regular_entry_under_root(
    project_path: Path, rel_parts: tuple[str, ...], pattern: str,
) -> Path | None:
    try:
        dir_fd = _open_nested_dir_no_follow(project_path, rel_parts)
    except (OSError, ValueError):
        return None
    newest: tuple[float, Path] | None = None
    try:
        try:
            names = os.listdir(dir_fd)
        except OSError:
            return None
        base = Path(project_path).joinpath(*rel_parts)
        for name in names:
            if not fnmatch.fnmatchcase(name, pattern):
                continue
            try:
                st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            path = base / name
            if newest is None or st.st_mtime > newest[0]:
                newest = (st.st_mtime, path)
        return newest[1] if newest is not None else None
    finally:
        os.close(dir_fd)


def newest_tee_stream(project_path: Path, peer: str) -> Path | None:
    """Newest ``.peers/log/peers/tick-*-<peer>.stream.jsonl`` for a peer, or None.

    Wave-2 unified live tee (substrate §5.1): when the run is launched with the
    live tee enabled (``observability.tee_stream`` / ``PEERS_TEE_STREAM``), each
    peer's live stdout is mirrored here AS IT HAPPENS. The Live-Stream window
    tails this uniformly for ALL peers — so codex/opencode become as
    live-watchable as claude. Returns the newest (in-flight tick) file, or None
    when teeing is off / nothing written yet. Fail-soft: a missing dir / OSError
    -> None (caller falls back to the legacy claude-peek / tick-log source)."""
    return _newest_regular_entry_under_root(
        Path(project_path),
        (".peers", "log", "peers"),
        f"tick-*-{peer}.stream.jsonl",
    )


def newest_tick_log(project_path: Path, peer: str) -> Path | None:
    """Newest ``.peers/log/peers/tick-*-<peer>.stdout.log`` for a peer, or None.

    The codex/opencode Live fallback tails this completed per-tick stdout log
    (tick-level) when the unified live tee (:func:`newest_tee_stream`) is off.
    Fail-soft: a missing dir / OSError -> None."""
    return _newest_regular_entry_under_root(
        Path(project_path),
        (".peers", "log", "peers"),
        f"tick-*-{peer}.stdout.log",
    )


def live_stream_kind(project_path: Path, peer: str, tool: str) -> str:
    """Decide which Live-Stream source to use for ``peer`` (pure, testable).

    Precedence (Wave-2 §5.1 read side):
      1. ``"tee"``  — a ``tick-*-<peer>.stream.jsonl`` exists → tail it
         (genuinely live for ALL peers, decoded by ``tool``).
      2. ``"peek"`` — no tee + ``claude`` peer → legacy ``peers-ctl peek``.
      3. ``"ticklog"`` — no tee + codex/opencode + a completed stdout log.
      4. ``"none"`` — nothing to follow yet.

    Separated from the Textual app so the selection logic is covered in default
    CI (the app module imports Textual)."""
    if newest_tee_stream(project_path, peer) is not None:
        return "tee"
    if tool == "claude":
        return "peek"
    if newest_tick_log(project_path, peer) is not None:
        return "ticklog"
    return "none"


def escalation_state(project_path: Path | str) -> dict:
    """Read the escalation markers a run leaves when it hands control back.

    The agentic-os / loop substrate writes ``<project>/.peers/HALTED.md`` when a
    run halts and asks the operator to intervene, and ``CONCERNS.md`` when the
    peers surface concerns short of a halt. This is the *inverse* of autonomy —
    the Eskalations-Banner lights up RED when either is present.

    Returns ``{halted: bool, concerns: bool, halted_excerpt: str}``. Fail-soft:
    a bad/inaccessible path -> the quiet default ``{False, False, ""}`` (never
    raises). PRESENCE is reported honestly from a plain ``exists()`` probe; the
    EXCERPT is read via ``safe_io`` (no-symlink, size-capped) so a symlinked or
    oversized marker still reports presence but degrades the excerpt to ``""``
    (defense in depth — we never follow a symlinked marker to read its bytes).
    """
    quiet = {"halted": False, "concerns": False, "halted_excerpt": ""}
    try:
        peer_dir = Path(project_path) / ".peers"
        halted_path = peer_dir / "HALTED.md"
        concerns_path = peer_dir / "CONCERNS.md"
        halted = halted_path.exists()
        concerns = concerns_path.exists()
    except OSError:
        return quiet
    excerpt = ""
    if halted:
        try:
            excerpt = safe_io.read_text_no_symlink(
                halted_path, max_bytes=_HALTED_EXCERPT_MAX,
            )
        except (OSError, ValueError):
            # symlinked / unreadable marker -> presence stays honest, excerpt "".
            excerpt = ""
    return {"halted": bool(halted), "concerns": bool(concerns),
            "halted_excerpt": excerpt}


def spine_runs(repo: Path) -> list[SpineRunEntry]:
    """Enumerate the Wave-2 spine run registry at ``<repo>/.peers/spine-runs/*.json``.

    HONEST EMPTY-STATE: in Wave 1 this directory does not exist yet -> ``[]``
    (the autonomy windows render an empty-state, not a fabricated run). When
    present, each JSON file decodes to one :class:`SpineRunEntry`; a malformed /
    oversized / non-object file is skipped (never raises).
    """
    d = Path(repo) / ".peers" / "spine-runs"
    try:
        if not d.is_dir():
            return []
        files = sorted(d.glob("*.json"))
    except OSError:
        return []
    out: list[SpineRunEntry] = []
    for f in files:
        try:
            text = safe_io.read_text_no_symlink(f, max_bytes=_SPINE_RUN_MAX)
        except (OSError, ValueError):
            continue
        try:
            rec = json.loads(text)
        except (ValueError, TypeError):
            continue
        if not isinstance(rec, dict):
            continue
        out.append(SpineRunEntry(
            mode_run=_opt_str(rec.get("mode_run")),
            worktree_path=_opt_str(rec.get("worktree_path")),
            branch=_opt_str(rec.get("branch")),
            ledger_path=_opt_str(rec.get("ledger_path")),
            pid=_coerce_opt_int(rec.get("pid")),
            started_at=_opt_str(rec.get("started_at")),
        ))
    return out


def _summarize_ledger_rows(rows: list) -> list[dict]:
    """Summarize ledger rows for display (event/status/author/independence/subject).

    NOTE: ``independence`` is surfaced here for AUDIT visibility only — it is the
    STORED flag and is NEVER used to decide convergence or gate state (those are
    re-derived in :func:`autonomy_ledger_view`).
    """
    out: list[dict] = []
    for r in rows:
        out.append({
            "event": getattr(r, "event", None),
            "status": getattr(r, "status", None),
            "author": getattr(r, "author", None),
            "independence": bool(getattr(r, "independence", False)),
            "subject": getattr(r, "subject", None),
        })
    return out


def autonomy_ledger_view(
    run_jsonl_path: Path,
    *,
    mode_run: str | None = None,
    dry_n: int = 3,
    repo: Path | str | None = None,
) -> AutonomyLedgerView:
    """Re-derive the autonomy/spine ledger view from ``run_jsonl_path``.

    If the ledger exists, read it via :class:`peers.spine.ledger.RunLedger`, run
    ``verify()`` for the integrity badge, and RE-DERIVE everything else:
    gate state via :func:`peers.spine.gates.evaluate_spine_gates`, convergence
    via :func:`peers.spine.propagate.is_converged`, and the dry streak via
    :func:`peers.spine.stop_on_dry.dry_streak`. The stored ``independence`` flag
    is NEVER trusted for any decision — only re-derived predicates are.

    Honesty scope (do not over-read the guarantee): a derived ``converged`` is
    only as trustworthy as that re-derivation — gates re-evaluated and the hash
    chain verified — which in turn ASSUMES substrate-attested authorship. ``.peers``
    is agent-writable, so a hand-written ``run.jsonl`` with a recomputed-valid hash
    chain and an arbitrary ``author`` would pass ``verify()`` and the
    ``authorship-attested`` gate (which only checks the author is not None). That
    is a substrate limit, not something this reader introduces or closes; it simply
    does not trust the stored flag on top of it.

    A missing/unreadable ledger -> an empty view (``verified=None``, ``gates={}``,
    ``converged=False``, ``dry_streak=0``, ``events=[]``). Any error degrades to
    the empty view (never raises).

    NOTE: ``dry_n`` affects the displayed stop-on-dry gate only; the ``converged``
    verdict uses the substrate's default dry threshold (``is_converged`` re-derives
    convergence internally and takes no ``dry_n``). We do NOT re-derive convergence
    here — that would risk the honesty seam.
    """
    from peers.spine.gates import evaluate_spine_gates
    from peers.spine.ledger import RunLedger
    from peers.spine.propagate import is_converged
    from peers.spine.stop_on_dry import dry_streak as _dry_streak

    empty = AutonomyLedgerView(
        verified=None, gates={}, converged=False, dry_streak=0, events=[],
    )
    path = Path(run_jsonl_path)
    try:
        if not path.exists():
            return empty
    except OSError:
        return empty

    led = RunLedger(path)
    # verify() is internally fail-closed (returns False on a corrupt ledger).
    try:
        verified = led.verify()
    except Exception:
        verified = False
    # read() is STRICT (raises on a corrupt line); degrade to the empty view but
    # keep the verify() badge so a tampered ledger still surfaces verified=False.
    try:
        rows = led.read()
    except Exception:
        return AutonomyLedgerView(
            verified=verified, gates={}, converged=False, dry_streak=0, events=[],
        )
    try:
        gates = evaluate_spine_gates(rows, mode_run=mode_run, dry_n=dry_n, repo=repo)
    except Exception:
        gates = {}
    try:
        converged = is_converged(rows, mode_run=mode_run, repo=repo)
    except Exception:
        converged = False
    try:
        streak = _dry_streak(rows)
    except Exception:
        streak = 0
    return AutonomyLedgerView(
        verified=verified,
        gates=gates,
        converged=converged,
        dry_streak=streak,
        events=_summarize_ledger_rows(rows),
    )
