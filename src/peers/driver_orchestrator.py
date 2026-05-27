"""External orchestrator driver: a tight Python while-loop."""
from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from peers.bug_hunt import count_new_blocking_or_flag_bug_reports
from peers.comm_layer import GitCommLayer, HybridCommLayer
from peers.goal_engine import GoalEngine, GoalResult
from peers.goals import Goal, _GOALS_YAML_MAX_BYTES
from peers.health_guard import HealthGuard, RunResult
from peers.peer_spec import PeerSpec
from peers.prompt_builder import build_prompt
from peers.recon import run_recon as _run_recon
from peers.safe_io import (
    _ensure_private_dir,
    _open_private_nested_dir_fd_no_symlink,
    _write_text_in_private_nested_dir_no_symlink,
    append_text_in_dir_no_symlink,
    open_text_no_symlink,
    read_bytes_no_symlink,
    read_text_no_symlink,
    write_text_no_symlink,
)
from peers.state_store import (
    StateStore, current_peer_name,
)
from peers.turn_manager import TurnManager


_AUTO_SKEPTIC_PROMPT_PREFIX = """\
=== POST-CONVERGENCE SKEPTIC RE-AUDIT ===

Der substrate hat soeben convergence-reached gemeldet: N consecutive
clean ticks ohne neue blocking/shallow Bug-Reports. **Das ist verdächtig.**
Dieser tick ist eine KRITISCHE WIEDERHOLUNG der eigenen Konvergenz:

- Hat der vorherige Audit wirklich alle relevanten Failure-Modes geprüft,
  oder hat sich das peer-Paar in seiner eigenen Zusammenfassung beruhigt?
- Welche src-Files wurden seit peers-baseline NIE oder zu oberflächlich
  reviewt? Pick mindestens DREI und audit-e sie gewissenhaft jetzt.
- Welche neuen src-Files entstanden während diesem run (z.B. recon.py, additions)? Wurden DIE selbst auditiert oder nur
  passiv mit-gefixt?
- Welche soft-reviews wurden mit "passed" abgesegnet, hätten aber bei
  ehrlicher Prüfung ein FAIL verdient?
- Welche Bug-Reports wurden weak-fix'd oder shallow-fix'd geschlossen?

Wenn du in dieser Re-Audit-Phase MINDESTENS EINEN echten neuen Bug
findest (crit/high/med, mit Repro), file ihn — der `consecutive_clean_
ticks`-Counter resettet sich automatisch und der Loop läuft weiter.
Wenn nach gewissenhafter Suche WIRKLICH nichts da ist, dokumentiere
konkret welche 5+ Failure-Modes du in diesem Pass explizit ausgeschlossen
hast (pro Modul). Pauschal-"sauber" gilt nicht.

Erst NACH diesem Skeptiker-Tick wird der run als terminal-success
beendet. Ein einziger Tick — also ehrlicher als üblich.

=== END SKEPTIC HEADER ===
"""


_CONFIGURABLE_BUDGET_LIMITS = (
    "max_iterations", "max_runtime_s", "max_consecutive_failures",
    "max_tokens", "max_usd", "max_usd_mode",
)


def _hash_goals_yaml(path: Path) -> str:
    data = read_bytes_no_symlink(path, max_bytes=_GOALS_YAML_MAX_BYTES + 1)
    if len(data) > _GOALS_YAML_MAX_BYTES:
        raise ValueError(
            f"goals.yaml too large (max {_GOALS_YAML_MAX_BYTES} bytes)"
        )
    return hashlib.sha256(data).hexdigest()


# G5: per-tool token output parsers. Each callable returns
# (tokens_used, usd_spent) parsed from a run's stdout+stderr. Keyed by
# `PeerSpec.tool` (not by peer name) so n>2 deployments with multiple
# claudes share the same parser.
def _parse_codex_tokens(text: str) -> tuple[int, float]:
    """Codex CLI emits `tokens used\n<N>` near the end."""
    m = re.search(r"tokens used\s*\n\s*([\d,]+)", text)
    if not m:
        return 0, 0.0
    try:
        return int(m.group(1).replace(",", "")), 0.0
    except ValueError:
        return 0, 0.0


def _parse_claude_tokens(text: str) -> tuple[int, float]:
    """Claude Code (`claude -p`) emits no token summary in its default
    text mode — `max_usd` is effectively off there.

    When invoked with `--output-format json` the entire stdout is a
    single JSON envelope:

        {"type":"result","subtype":"success","is_error":false,
         "result":"<final assistant text>", "total_cost_usd":0.0123,
         "usage":{"input_tokens":N, "cache_creation_input_tokens":N,
                  "cache_read_input_tokens":N, "output_tokens":N, ...},
         ...}

    `--output-format stream-json --verbose` emits one JSON object per
    line; the final `type:"result"` object carries the same usage block.

    Strategy: try to detect a `{...}` envelope containing `"usage"` (or
    a `type:"result"` line in stream-json) and parse it. Fall back to
    the legacy text regex for backwards compatibility with plain
    `claude -p` output (where we just have to live with 0 tokens).
    """
    tokens, usd = _parse_claude_json_envelope(text)
    if tokens or usd:
        return tokens, usd

    # the previous "legacy text" fallback
    # used `(\d[\d,]*)\s+tokens?` (case-insensitive, sum-of-matches),
    # which gleefully added EVERY token-mention in the peer's narrative
    # output ("I'll keep this under 200 tokens" → +200). Same trap for
    # `\$(\d+\.\d+)` and dollar mentions. With max_tokens / max_usd
    # active this falsely tripped BudgetCheck.
    #
    # Replacement: only accept a STRICT summary banner like
    # `tokens used: 1,234` or `total tokens: 1,234`, anchored at
    # start-of-line (most claude wrappers emit these). USD summary
    # must say "Cost:" / "cost:" / "total cost:" before the dollar.
    # When in doubt, return (0, 0) — under-counting is preferable to
    # killing the loop on phantom budget breaches.
    tokens = 0
    usd = 0.0
    # Accept either "N tokens used" or "tokens used: N", both anchored
    # at line start AND end so we don't catch prose like
    # "1,234 tokens used to make the request".
    for pat in (
        r"(?im)^\s*(\d[\d,]*)\s+tokens?\s+used\.?\s*$",
        r"(?im)^\s*(?:total\s+)?tokens?\s+used\s*[:=]\s*(\d[\d,]*)\s*$",
    ):
        m = re.search(pat, text)
        if m:
            try:
                tokens = int(m.group(1).replace(",", ""))
                break
            except ValueError:
                tokens = 0
    for pat in (
        r"(?im)^\s*(?:total\s+)?cost\s*[:=]\s*\$(\d+\.\d+)\s*$",
        r"(?im)^\s*\$(\d+\.\d+)\s+(?:total\s+)?cost\s*$",
    ):
        m = re.search(pat, text)
        if m:
            try:
                usd = float(m.group(1))
                break
            except ValueError:
                usd = 0.0
    return tokens, usd


def _parse_claude_json_envelope(text: str) -> tuple[int, float]:
    """Best-effort scan for a claude `--output-format json` envelope
    (single object) or a `--output-format stream-json` result-line.

    Returns (0, 0.0) when nothing JSON-like is found. Never raises.
    Tolerates stderr noise mixed into stdout: scans non-empty lines and
    the full text body looking for the first decodable envelope that
    carries a `usage` block or a `total_cost_usd` field.
    """
    def _from_obj(obj: object) -> tuple[int, float] | None:
        if not isinstance(obj, dict):
            return None
        # stream-json: only the final `type:"result"` block has usage.
        if "usage" not in obj and "total_cost_usd" not in obj:
            return None
        usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
        token_keys = (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "output_tokens",
        )
        tok = 0
        for k in token_keys:
            v = usage.get(k)
            if isinstance(v, int):
                tok += v
        cost = obj.get("total_cost_usd")
        usd = float(cost) if isinstance(cost, (int, float)) else 0.0
        return tok, usd

    # stream-json: try each line as JSON; collect any result-bearing one.
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] != "{":
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        parsed = _from_obj(obj)
        if parsed is not None:
            return parsed

    # single-envelope json: try to find {...} that contains "usage" or
    # "total_cost_usd". Be permissive: full-text parse first.
    try:
        obj = json.loads(text.strip())
    except json.JSONDecodeError:
        obj = None
    parsed = _from_obj(obj) if obj is not None else None
    if parsed is not None:
        return parsed

    # last resort: greedy outermost {...} substring (handles cases
    # where claude prints a banner line before the JSON body).
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            obj = None
        parsed = _from_obj(obj) if obj is not None else None
        if parsed is not None:
            return parsed
    return 0, 0.0


_TOKEN_PARSERS = {
    "claude": _parse_claude_tokens,
    "codex": _parse_codex_tokens,
}


def _apply_config_budget(state: dict[str, Any],
                         cfg_budget: dict[str, Any],
                         peer_tools: list[str] | None = None) -> None:
    """Overlay user-configured limits from config.yaml onto state.budget,
    preserving the spent_* counters across runs.

    If `max_usd_mode` is omitted or set to "auto", we resolve it now
    against the actual peer tools (OAuth detection) and persist the
    resolved value into state for downstream BudgetCheck calls.
    """
    from peers.billing import resolve_max_usd_mode
    for key in _CONFIGURABLE_BUDGET_LIMITS:
        if key in cfg_budget:
            state["budget"][key] = cfg_budget[key]
    declared = cfg_budget.get("max_usd_mode", "auto")
    mode, reason = resolve_max_usd_mode(declared, peer_tools or [])
    state["budget"]["max_usd_mode"] = mode
    state["budget"]["max_usd_mode_reason"] = reason


class BudgetCheck:
    """Tick-precondition budget gate.

    `max_usd_mode` (state["budget"]["max_usd_mode"]) controls how the
    USD cap is treated:
      - "hard"  → cap exceeded ⇒ exit reason `max_usd` (legacy behavior).
      - "warn"  → cap exceeded ⇒ emit one-time warning, do NOT exit.
                  This is correct for OAuth-billed CLIs where
                  `total_cost_usd` reports the API-equivalent price but
                  the user pays a flat subscription.
      - "off"   → ignore `max_usd` entirely.

    Token / iteration / runtime / consecutive-failure caps are always
    hard-enforced regardless of mode.
    """

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.b = state["budget"]

    def reason(self) -> str | None:
        if self.b["spent_iterations"] >= self.b["max_iterations"]:
            return "max_iterations"
        if self.b["spent_runtime_s"] >= self.b["max_runtime_s"]:
            return "max_runtime"
        if self.b["consecutive_failures"] >= self.b["max_consecutive_failures"]:
            return "max_consecutive_failures"
        mt = self.b.get("max_tokens")
        if mt is not None and self.b.get("spent_tokens", 0) >= mt:
            return "max_tokens"
        mu = self.b.get("max_usd")
        if mu is not None and self.b.get("spent_usd", 0.0) >= mu:
            mode = self.b.get("max_usd_mode", "hard")
            if mode == "hard":
                return "max_usd"
            if mode == "warn":
                # Emit a one-time soft warning so it lands in state.warnings
                # and the operator can spot it, but don't kill the loop.
                warned_for = self.b.get("max_usd_warned_for")
                if warned_for != mu:
                    _warn_once(
                        self.state,
                        f"max_usd:{mu:.2f} reached "
                        f"(spent ${self.b['spent_usd']:.2f}) "
                        f"but mode=warn — loop continues",
                    )
                    self.b["max_usd_warned_for"] = mu
            # mode == "off" → silent
        return None


def _extract_first_json_object(body: str) -> dict | None:
    """Scan `body` for the first balanced `{...}` block and return its
    decoded JSON dict, or None if no parseable object is found.

    Replaces the legacy single-nesting regex
    `\\{[^{}]*(?:\\{[^{}]*\\}[^{}]*)*\\}` which rejected soft-review
    JSON containing nested objects. Tolerates:
    - braces inside string literals (skipped via a tiny string-aware
      state machine);
    - multiple consecutive `{...}` blocks (first parseable wins);
    - prose before/after the block.
    """
    in_string = False
    escape = False
    depth = 0
    start = -1
    for i, ch in enumerate(body):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                snippet = body[start:i + 1]
                try:
                    val = json.loads(snippet)
                except json.JSONDecodeError:
                    start = -1
                    continue
                if isinstance(val, dict):
                    return val
                start = -1
    return None


def _warn_once(state: dict[str, Any], msg: str) -> None:
    """Append `msg` to state.warnings only if the most recent warning
    differs (idempotent across ticks). Bounded growth via L5 clamp
    elsewhere in the substrate."""
    warnings = state.setdefault("warnings", [])
    if warnings and warnings[-1] == msg:
        return
    warnings.append(msg)


# ---------------------------------------------------------------------------
# Task 4.1 — implement-mode Phase 0 state machine.
#
# Phase 0 is a 3-tick prep prelude that runs ONLY in implement-mode,
# before the normal tick loop produces implementation commits:
#
#   tick 0 → "recon"         — peer writes RECON.md
#   tick 1 → "alignment"     — peer writes PLAN.aligned.md
#   tick 2 → "architecture"  — peer writes ARCHITECTURE.intended.md
#   tick 3+→ "implementation"— normal per-step implementation ticks
#
# Every other mode (audit/security/thorough/unknown custom modes) stays
# in "implementation" from tick 0 — strict backward compatibility for
# pre-implement-mode users.
#
# Tasks 4.2 – 4.4 wire real Phase 0 prompts on top of this scaffold; the
# driver currently just records the phase string in state.json and logs
# a tick-start marker so operators can see which phase is active. The
# normal prompt path is unchanged (placeholder behaviour — we do NOT
# yet alter peer prompts based on phase).
_PHASE_0_TICKS = (
    "recon",         # tick 0
    "alignment",     # tick 1
    "architecture",  # tick 2
)
PHASE_IMPLEMENTATION = "implementation"


def _resolve_phase(mode_name: str, tick_number: int) -> str:
    """Return the phase string for the upcoming tick.

    Pure function — depends only on `mode_name` (string match against
    the literal "implement") and `tick_number` (0-indexed count of
    completed ticks; i.e. the index of the tick about to fire).

    Unknown modes (anything other than "implement") always return
    "implementation" — Phase 0 is intentionally exclusive to first-
    party implement-mode to avoid surprising custom-mode authors.
    """
    if mode_name != "implement":
        return PHASE_IMPLEMENTATION
    if 0 <= tick_number < len(_PHASE_0_TICKS):
        return _PHASE_0_TICKS[tick_number]
    return PHASE_IMPLEMENTATION


# Task 6.2 — blind-review tick orchestration.
#
# During implement-mode's "implementation" phase, the implementer and
# reviewer roles alternate on a per-tick basis so each tick has a
# well-defined "blind reader" of the previous tick's code-diff.
#
#   tick 3 → implementer  (writes IMPLEMENTATION_NOTES.md)
#   tick 4 → reviewer     (writes REVIEW_NOTES.md without peeking)
#   tick 5 → implementer
#   tick 6 → reviewer
#   ...
#
# The driver does not change the existing peer rotation (A → B → A →
# B...) — peers are simply told what role they hold for the upcoming
# tick via a prompt overlay (`blind_review_implementer.md` /
# `blind_review_reviewer.md`).
#
# All non-implement modes (audit / security / thorough / custom user
# modes) and all Phase 0 ticks (recon / alignment / architecture)
# return "normal" — they are unaffected by this overlay.
def _resolve_peer_role(mode_name: str, phase: str, tick: int) -> str:
    """Return 'implementer' | 'reviewer' | 'normal' for the upcoming tick.

    For implement-mode in the "implementation" phase (tick 3 onward),
    alternates: even offsets from tick 3 are implementer, odd are
    reviewer. Every other (mode, phase) combination returns "normal"
    — strict backward-compat for non-implement modes and for the
    Phase 0 prelude ticks.
    """
    if mode_name != "implement":
        return "normal"
    if phase != PHASE_IMPLEMENTATION:
        return "normal"
    return "implementer" if (tick - 3) % 2 == 0 else "reviewer"


# Task 6.5 — two-phase convergence for implement-mode.
#
# Phase A: ALL hard gates green for N=phase_a_n consecutive ticks
#          (default 5) → transition to Phase B.
# Phase B: AFTER Phase A clears, additional M=phase_b_n ticks (default 2)
#          where the three skeptic gates (blind-review + honesty-audit +
#          concerns-resolved) also pass → transition to "complete".
#
# Pure function: no I/O, no state mutation. The driver carries the
# tick-by-tick counters in state.json and feeds them in. For non-
# implement modes the function passes `current_phase` through unchanged
# — strict backward-compat (other modes' convergence machinery is
# untouched).
def _resolve_convergence_state(
    mode_name: str,
    current_phase: str,
    consecutive_clean: int,
    phase_a_n: int,
    phase_b_n: int,
    phase_b_extra_ticks: int,
) -> str:
    """Return the next `convergence_phase` value.

    Args:
      mode_name: active mode (only "implement" engages the state machine).
      current_phase: "A" | "B" | "complete" (unknown values are treated
        as "A" — safe default; we never auto-promote a corrupted state).
      consecutive_clean: ticks in a row where ALL hard gates passed.
      phase_a_n: required `consecutive_clean` to advance A → B.
      phase_b_n: required `phase_b_extra_ticks` to advance B → complete.
      phase_b_extra_ticks: ticks since entering Phase B where the three
        skeptic gates (blind-review, honesty-audit, concerns-resolved)
        all passed.
    """
    if mode_name != "implement":
        return current_phase
    if current_phase == "complete":
        return "complete"
    if current_phase == "A":
        if consecutive_clean >= phase_a_n:
            return "B"
        return "A"
    if current_phase == "B":
        if phase_b_extra_ticks >= phase_b_n:
            return "complete"
        return "B"
    return "A"


def _should_checkpoint(
    peer_dir: Path,
    *,
    prev_phase: str | None,
    curr_phase: str,
) -> bool:
    """Return True iff the driver should pause for operator review.

    Task 4.5: when `peers-ctl start <project> --checkpoint` is used,
    the CLI writes a `.peers/checkpoint_requested` marker before
    launching the loop. The driver checks this marker on every tick
    and, when the phase transitions from "architecture" (Phase 0
    tick 2) to "implementation" (the first real impl tick), exits
    cleanly so the operator can review RECON.md + PLAN.aligned.md +
    ARCHITECTURE.intended.md before any code lands.

    Pure-ish helper: only filesystem side-effect is `is_file()` on
    the marker. Phase comparison is exact-string — this is the v1
    contract (architecture → implementation is the sole trigger).
    """
    if prev_phase != "architecture" or curr_phase != PHASE_IMPLEMENTATION:
        return False
    marker = peer_dir / "checkpoint_requested"
    return marker.is_file()


def _load_phase_prompt(mode_name: str, phase: str) -> str | None:
    """Return the text of a Phase 0 prompt template, or None if missing.

    Resolves to `src/peers/templates/modes/<mode_name>/prompts/<phase>.md`
    inside the installed `peers` package — i.e. discovered via the
    package's own __file__, NOT the caller's cwd. The lookup is
    intentionally narrow: any mode/phase combination without a shipped
    template returns None, which signals "no overlay; use the default
    prompt" to the caller.

    Used by the driver to overlay Phase 0 instructions (Tasks 4.2-4.4)
    on the per-tick prompt when implement-mode is active during ticks
    0/1/2 (recon → alignment → architecture).
    """
    # Reject obviously bogus / path-traversing inputs early so the
    # template lookup never escapes the templates/ tree.
    if not mode_name or not phase:
        return None
    if "/" in mode_name or "/" in phase or ".." in mode_name or ".." in phase:
        return None
    pkg_root = Path(__file__).resolve().parent
    prompt_path = (
        pkg_root / "templates" / "modes" / mode_name / "prompts"
        / f"{phase}.md"
    )
    if not prompt_path.is_file():
        return None
    try:
        return prompt_path.read_text(encoding="utf-8")
    except OSError:
        return None


def _detect_mode_name(peer_dir: Path) -> str:
    """Best-effort lookup of the active mode from `.peers/modes-applied.txt`.

    Returns "implement" only when implement-mode is the SOLE active mode
    (v1 of implement-mode is documented as standalone — not composable
    with audit/security/thorough). Returns "" on missing file, parse
    failure, or composed mode-stacks containing implement alongside
    others. The empty string flows through `_resolve_phase` as
    "implementation" — i.e. Phase 0 is silently skipped in those cases.

    Format (one mode per line, written by `peers init --modes`):
        2026-05-26T12:34:56+00:00  implement     v1  sha256=...
    """
    trail = peer_dir / "modes-applied.txt"
    try:
        text = trail.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    names: list[str] = []
    for line in text.splitlines():
        # The mode name is the 2nd whitespace-separated token (timestamp
        # is the 1st). Skip blank / malformed lines.
        parts = line.split()
        if len(parts) >= 2:
            names.append(parts[1])
    if names == ["implement"]:
        return "implement"
    return ""


def _format_tick_status(*, success: bool, classification: str) -> str:
    """Operator-readable tick-end status label.

    Returns one of:
      `handoff`     — peer made a valid handoff commit
                      (`Peer-Status: handoff` + `Self-Review: pass`).
      `no-handoff`  — peer process exited cleanly (rc=0, no error)
                      but did NOT produce a valid handoff commit
                      (no new commit, missing trailers, history
                      rewritten, etc).
      <classification> — peer subprocess errored; classification is
                         `process-fail` / `api-error` /
                         `idle-timeout` / `absolute-timeout`.

    Replaces the older `fail({classification})` formatting (2026-05-26
    operator-UX fix): `fail(success)` was self-contradictory at a
    glance — the run did NOT fail, the peer simply didn't commit.
    """
    if success:
        return "handoff"
    if classification == "success":
        return "no-handoff"
    return classification


class OrchestratorDriver:
    """Wires substrate parts together into a tick loop."""

    def __init__(
        self,
        repo: Path,
        peer_dir: Path,
        goals: list[Goal],
        peer_specs: list[PeerSpec],
        state_store: StateStore | None = None,
        idle_timeout_s: int = 15 * 60,
        absolute_max_runtime_s: int = 2 * 3600,
        cfg_budget: dict[str, Any] | None = None,
        error_patterns: Sequence[str] | None = None,
        halt_patterns: Sequence[str] | None = None,
        dry_run: bool = False,
        comm_variant: str = "git",
        buf_cap_bytes: int = 2 * 1024 * 1024,
        goals_timeout_s: int = 120,
        verbose: bool = False,
        recon_enabled: bool = True,
        auto_skeptic_enabled: bool = True,
    ) -> None:
        if len(peer_specs) < 2:
            raise ValueError(
                f"need at least 2 peers, got {len(peer_specs)}"
            )
        self.repo = Path(repo)
        self.peer_dir = Path(peer_dir)
        self.goals = goals
        self.peer_specs = peer_specs
        self.peer_names = [p.name for p in peer_specs]
        self.peers_by_name: dict[str, PeerSpec] = {
            p.name: p for p in peer_specs
        }
        self.state_store = state_store or StateStore(
            self.peer_dir / "state.json",
            peer_order=self.peer_names,
        )
        self.health = HealthGuard(self.repo)
        self.engine = GoalEngine(goals, cwd=self.repo,
                                 timeout_s=goals_timeout_s)
        if comm_variant == "hybrid":
            self.comm = HybridCommLayer(self.repo, self.peer_dir)
        elif comm_variant == "git":
            self.comm = GitCommLayer(self.repo)
        else:
            raise ValueError(
                f"unknown comm_variant {comm_variant!r}; expected "
                "'git' or 'hybrid'"
            )
        self.comm_variant = comm_variant
        self.idle_timeout_s = idle_timeout_s
        self.absolute_max_runtime_s = absolute_max_runtime_s
        self.error_patterns = list(error_patterns or [])
        # halt_patterns trigger an immediate peer-unavailable
        # exit instead of degraded-on-retry. Intended for AUTH/QUOTA.
        self.halt_patterns = list(halt_patterns or [])
        self.cfg_budget = cfg_budget or {}
        self.dry_run = dry_run
        self.buf_cap_bytes = int(buf_cap_bytes)
        # end-of-tick stdout/stderr echo to substrate stderr.
        self.verbose = bool(verbose)
        # Recon pre-tick hook: substrate-only project digest written to
        # .peers/recon.md before the loop starts. Default on; disable
        # via --without-recon for runs where the digest is hand-prepared
        # or unwanted.
        self.recon_enabled = bool(recon_enabled)
        # when convergence-reached is about to fire, run ONE
        # extra "skeptic re-audit" tick first. If that tick stays clean
        # → really terminal. If it files a new blocking bug → counter
        # resets, loop continues. Default on; disable via
        # --without-post-convergence-skeptic for runs where false-
        # convergence is acceptable.
        self.auto_skeptic_enabled = bool(auto_skeptic_enabled)
        self._head_before_invoke: str | None = None
        self._peer_dir_identity: tuple[int, int] | None = None
        # H1: snapshot the expected goals.yaml hash ONCE at driver
        # init, in memory. The per-tick mutation check compares the
        # live file hash against this snapshot — so a peer that
        # rewrites both goals.yaml AND goals.sha256 in lockstep can
        # no longer fool the lock.
        self._goal_hash_snapshot: str | None = self._read_goal_hash_snapshot()
        # Task 4.1: detect active mode for Phase 0 state machine. Empty
        # string ("") flows through `_resolve_phase` as "implementation"
        # — i.e. backward-compat for every non-implement mode (audit,
        # security, thorough, custom user modes, or runs without a
        # `modes-applied.txt` audit trail at all).
        self.mode_name: str = _detect_mode_name(self.peer_dir)

    def _capture_peer_dir_identity(self) -> tuple[int, int]:
        try:
            st = self.peer_dir.lstat()
        except OSError as e:
            raise RuntimeError(
                f"{self.peer_dir} is unavailable; refusing to operate: {e}"
            ) from e
        if stat.S_ISLNK(st.st_mode):
            raise RuntimeError(
                f"{self.peer_dir} is a symlink "
                f"({os.readlink(self.peer_dir)!r}); refusing to operate. "
                "Remove it manually to continue."
            )
        if not stat.S_ISDIR(st.st_mode):
            raise RuntimeError(
                f"{self.peer_dir} is not a directory; refusing to operate."
            )
        return (st.st_dev, st.st_ino)

    def _verify_peer_dir_identity(self) -> None:
        current = self._capture_peer_dir_identity()
        if self._peer_dir_identity is None:
            self._peer_dir_identity = current
            return
        if current != self._peer_dir_identity:
            raise RuntimeError(
                f"{self.peer_dir} changed while the loop was running; "
                "refusing control-plane IO. Restore the original .peers "
                "directory and restart."
            )

    def _save_state(self, state: dict[str, Any]) -> None:
        self._verify_peer_dir_identity()
        self.state_store.save(state)

    def _write_stop_reason(self, reason: str) -> None:
        """Write `.peers/last-stop-reason.txt` so `peers-ctl reconcile`
        can distinguish a clean self-termination ("stopped") from a
        hard process death ("crashed"). Pre-Phase-V, v6 and v7 both
        ran to convergence-complete but the controller marked them as
        crashed because there was no clean-exit sentinel.

        Best-effort: never let sentinel-write failure abort the exit
        path. Format: `<reason> <iso_utc_timestamp>\\n`.
        """
        try:
            import datetime as _dt
            sentinel = self.peer_dir / "last-stop-reason.txt"
            tmp = sentinel.with_suffix(sentinel.suffix + ".tmp")
            ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
            tmp.write_text(f"{reason} {ts}\n")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, sentinel)
        except Exception as e:
            print(
                f"peers: warning, failed to write stop-reason sentinel: {e!r}",
                file=sys.stderr,
            )

    def _run_recon_step(self) -> None:
        """Substrate pre-tick recon. Runs once at the start of `run()`
        to write `.peers/recon.md` with a static project digest. The
        peers loop reads this file (via prompt-builder, future hookup)
        so tick 1 isn't burned on figure-out-what-is-this work.

        Recon is substrate-only — no LLM call, no token cost, no budget
        deduction. Errors are logged but do not abort the run; recon is
        a nice-to-have, not a hard prerequisite.
        """
        try:
            status = _run_recon(self.repo, self.peer_dir)
            print(f"peers: {status}", file=sys.stderr)
        except Exception as e:
            print(
                f"peers: warning: recon step failed: {e!r}; "
                "continuing without recon.md",
                file=sys.stderr,
            )

    def _verify_no_control_symlinks(self) -> None:
        """L1: refuse to operate on a .peers/ where any of the
        control files (or substrate-written log/report files) are
        symlinks.

        expanded the list. Previously only
        the static config/state files were checked, but the substrate
        ALSO writes to log/runs.jsonl, HALTED.md, REPORT.md, VERIFY.md
        and comms/*. A peer pre-creating those as symlinks could get
        the substrate to write to e.g. ~/.bashrc, ~/.ssh/authorized_keys
        with the substrate's own privileges. log/runs.jsonl is the
        worst — appended to every tick.
        """
        if self.peer_dir.is_symlink():
            raise RuntimeError(
                f"{self.peer_dir} is a symlink "
                f"({os.readlink(self.peer_dir)!r}); refusing to operate. "
                "Remove it manually to continue."
            )
        static_files = (
            "state.json", "goals.yaml", "goals.sha256",
            "state.json.tmp", "state.json.pre-migration",
            "run.lock", "config.yaml",
            # additions — substrate-managed write targets:
            "HALTED.md", "REPORT.md", "VERIFY.md",
            "log/runs.jsonl",
        )
        for rel in static_files:
            p = self.peer_dir / rel
            if p.is_symlink():
                raise RuntimeError(
                    f"{p} is a symlink ({os.readlink(p)!r}); "
                    "refusing to operate. Remove it manually to "
                    "continue."
                )
        static_dirs = ("log", "comms", "hooks", "checks", "queue")
        for rel in static_dirs:
            p = self.peer_dir / rel
            if p.is_symlink():
                raise RuntimeError(
                    f"{p} is a symlink ({os.readlink(p)!r}); "
                    "refusing to operate. Remove it manually to "
                    "continue."
                )
        # Recursively check the comms tree (sender-to-receiver dirs +
        # the archive) since hybrid comm-layer writes into them too.
        comms_root = self.peer_dir / "comms"
        if comms_root.exists():
            for sub in comms_root.rglob("*"):
                if sub.is_symlink():
                    raise RuntimeError(
                        f"{sub} is a symlink ({os.readlink(sub)!r}); "
                        "refusing to operate (hybrid comm files would "
                        "otherwise write through). Remove it manually."
                    )

    def _read_goal_hash_snapshot(self) -> str | None:
        """Read the goals.sha256 snapshot file ONCE at init time, or
        compute it from goals.yaml if the sha256 file is missing.
        Returns the hex digest, or None if no goals.yaml exists."""
        gfile = self.peer_dir / "goals.yaml"
        if not gfile.exists():
            return None
        snap = self.peer_dir / "goals.sha256"
        if snap.exists():
            try:
                return read_text_no_symlink(snap, max_bytes=129).strip().split()[0]
            except (OSError, IndexError):
                pass
        # Fall back to live hash — equivalent to "init now".
        return _hash_goals_yaml(gfile)

    def _sync_peer_order(self, state: dict[str, Any]) -> None:
        """If the loaded state's peer_order differs from the configured
        one (e.g. user reordered or renamed peers in config.yaml), trust
        the config and rebuild missing entries."""
        if state.get("peer_order") != self.peer_names:
            old_order = state.get("peer_order", [])
            state["peer_order"] = list(self.peer_names)
            # Preserve health entries for peers still present; drop entries
            # for removed peers.
            old_peers = state.get("peers", {})
            new_peers: dict[str, Any] = {}
            for n in self.peer_names:
                new_peers[n] = old_peers.get(n) or {
                    "state": "healthy",
                    "consecutive_fails": 0,
                    "recent_fails": 0,
                    "recent_runs": [],
                }
            state["peers"] = new_peers
            # Try to preserve which peer was up next, otherwise reset.
            if 0 <= state.get("turn_index", -1) < len(old_order):
                old_active = old_order[state["turn_index"]]
                if old_active in self.peer_names:
                    state["turn_index"] = self.peer_names.index(old_active)
                else:
                    state["turn_index"] = 0
            else:
                state["turn_index"] = 0

    def run(self, max_ticks: int | None = None) -> dict[str, Any]:
        # File lock: refuse to run if another peers process is already
        # active against the same .peers/ dir. Prevents two peer loops
        # from clobbering state.json or racing for git on the target.
        lock_path = self.peer_dir / "run.lock"
        if self.peer_dir.is_symlink():
            raise RuntimeError(
                f"{self.peer_dir} is a symlink "
                f"({os.readlink(self.peer_dir)!r}); refusing to operate. "
                "Remove it manually to continue."
            )
        _ensure_private_dir(self.peer_dir)
        self._verify_no_control_symlinks()
        # Open without truncating first: contenders must not erase the
        # currently-running PID before they know they own the flock.
        self._peer_dir_identity = self._capture_peer_dir_identity()
        lock_fp = open_text_no_symlink(lock_path, "a")
        try:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_fp.close()
            return {"reason": "lock-held", "state": None}
        lock_fp.seek(0)
        lock_fp.truncate(0)
        lock_fp.write(f"{os.getpid()}\n")
        lock_fp.flush()

        # route SIGTERM through the same KeyboardInterrupt path
        # the rest of the loop already handles. Without this, a
        # `peers-ctl stop` (which sends SIGTERM) would terminate the
        # process immediately, skipping state.save() and leaving the
        # run.lock file behind.
        def _sigterm_handler(signum, frame):
            raise KeyboardInterrupt
        prev_term = signal.signal(signal.SIGTERM, _sigterm_handler)

        try:
            state = self.state_store.load()
            self._sync_peer_order(state)
            _apply_config_budget(
                state, self.cfg_budget,
                peer_tools=[s.tool for s in self.peer_specs],
            )
            tm = TurnManager(state)
            ticks = 0

            if self.recon_enabled:
                self._run_recon_step()

            try:
                result = self._loop(state, tm, max_ticks, ticks)
                # step 2a: write stop-reason sentinel so
                # peers-ctl reconcile distinguishes clean self-termination
                # from a hard crash.
                self._write_stop_reason(result.get("reason", "unknown"))
                return result
            except KeyboardInterrupt:
                try:
                    self._save_state(state)
                except Exception as e:
                    print(f"peers: warning, failed to persist state on "
                          f"interrupt: {e}", file=sys.stderr)
                # Surface the interrupt in runs.jsonl too — useful in
                # post-mortems to distinguish "operator stopped" from
                # "completed cleanly".
                self._append_exit_event(
                    "interrupted", state.get("budget", {}).get(
                        "spent_iterations", 0,
                    ),
                )
                self._write_stop_reason("interrupted")
                raise
            except Exception as e:
                # (CRITICAL): any non-KeyboardInterrupt exception
                # from _loop would previously skip state.save and lose
                # everything since the last successful save. Persist
                # best-effort, then re-raise so the caller sees it.
                try:
                    self._save_state(state)
                except Exception as save_err:
                    print(
                        f"peers: warning, failed to persist state "
                        f"after exception {type(e).__name__}: "
                        f"{save_err}",
                        file=sys.stderr,
                    )
                self._write_stop_reason(f"error:{type(e).__name__}")
                raise
        finally:
            try:
                signal.signal(signal.SIGTERM, prev_term)
            except Exception:
                pass
            try:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
                lock_fp.close()
            except Exception:
                pass

    def _loop(self, state: dict[str, Any], tm: TurnManager,
              max_ticks: int | None, ticks: int) -> dict[str, Any]:
        while True:
            self._verify_peer_dir_identity()
            early_exit, results = self._pre_tick_exit(state, max_ticks, ticks)
            if early_exit is not None:
                return early_exit

            checkpoint_exit = self._maybe_checkpoint_exit(state, ticks)
            if checkpoint_exit is not None:
                return checkpoint_exit
            peer, spec, prompt = self._prepare_tick_prompt(state, tm, results)
            # tick-N is the *upcoming* tick; iteration is incremented
            # later inside _record_tick_accounting.
            upcoming_tick = state["iteration"] + 1
            # persist the prompt before invoking so a post-mortem
            # always has it even if the substrate crashes mid-tick.
            self._write_prompt_log(upcoming_tick, peer, prompt)
            # visible marker so `peers-ctl logs/<name>.log` shows
            # tick boundaries instead of long silences.
            print(
                f"peers: tick {upcoming_tick} peer={peer} starting...",
                file=sys.stderr, flush=True,
            )
            tick_t0 = time.monotonic()
            self._head_before_invoke = self.comm.head_sha()
            run = self.health.invoke(
                spec.argv, prompt=prompt,
                idle_timeout_s=self.idle_timeout_s,
                absolute_max_runtime_s=self.absolute_max_runtime_s,
                prompt_mode=spec.prompt_mode,
                error_patterns=self.error_patterns,
                halt_patterns=self.halt_patterns,
                buf_cap_bytes=self.buf_cap_bytes,
            )
            self._verify_peer_dir_identity()
            tick_dt = int(time.monotonic() - tick_t0)
            halt_exit = self._handle_pattern_match_and_halt(
                state, ticks, upcoming_tick, peer, run,
            )
            if halt_exit is not None:
                return halt_exit
            # full per-tick peer output to disk before _post_run
            # mutates anything (stdout/stderr can be huge; useful even on
            # success for offline review).
            self._write_peer_output_logs(upcoming_tick, peer, run)
            if run.truncated:
                state.setdefault("warnings", []).append(
                    f"healthguard: peer {peer!r}'s output exceeded the "
                    "2 MiB per-stream cap; head/tail kept, middle "
                    "truncated. Consider quieter prompting or a higher "
                    "cap if signal is being lost."
                )
            success = self._post_run(state, peer, run)
            success = self._apply_anti_cheating_outcome(state, peer, success)
            success = self._apply_dry_run_reset(state, success)

            tm.advance(success=success)
            self._record_tick_accounting(state, success, tick_dt)

            tokens_this_tick, usd_this_tick = self._account_tokens_usd(
                state, spec.tool, run,
            )

            self._update_peer_health(state, peer, success)
            state["dirty_worktree"] = self._dirty_worktree(state)
            if success:
                self._detect_tampering(state)
            self._maybe_halt(state)
            # The warnings list was pop'd into this tick's prompt
            # earlier (after build_prompt). New ones added by
            # anti-cheating + _detect_tampering after the peer's run
            # are the ones we want to log.
            new_warnings = list(state.get("warnings", []))
            self._append_warnings_history(state, new_warnings)
            head_after_sha = self.comm.head_sha()
            self._append_run_log(
                state, peer, run, success,
                tokens_this_tick=tokens_this_tick,
                usd_this_tick=usd_this_tick,
                head_before=self._head_before_invoke,
                head_after=head_after_sha,
                warnings_emitted=new_warnings,
            )
            self._save_state(state)
            # + tick-end marker + optional peer-output echo.
            self._emit_tick_end(state, peer, run, success, tick_dt,
                                head_after_sha)
            self._update_convergence_counter(state)
            ticks += 1

    # --- Task 4.1: Phase 0 state machine ----------------------------

    def _record_phase(self, state: dict[str, Any]) -> None:
        """Stamp the upcoming tick's phase into state.json.

        Called at the start of every tick BEFORE the prompt is built or
        the peer runs. state["iteration"] is the 0-indexed count of
        completed ticks → equals the index of the tick about to fire.
        For non-implement modes this always resolves to "implementation"
        (strict backward-compat for audit / security / thorough / custom
        user modes). For implement-mode the first three ticks resolve to
        recon → alignment → architecture (Phase 0 prep prelude).

        The actual Phase 0 prompt overlay is applied later by
        `_prepare_tick_prompt` (Tasks 4.2-4.4 — see `_load_phase_prompt`).
        Here we only persist the phase string and emit a one-line
        operator marker for non-implementation phases so it's obvious
        from the substrate log which prelude tick is firing.
        """
        phase = _resolve_phase(self.mode_name, state["iteration"])
        state["phase"] = phase
        if phase != PHASE_IMPLEMENTATION:
            print(
                f"peers: phase={phase} (tick {state['iteration']}, "
                f"mode={self.mode_name}) — Phase 0 prompt overlay active",
                file=sys.stderr, flush=True,
            )

    def _maybe_checkpoint_exit(
        self, state: dict[str, Any], ticks: int,
    ) -> dict[str, Any] | None:
        """Task 4.5: pause the loop when --checkpoint was requested
        and Phase 0 just completed.

        Captures the previous phase, calls `_record_phase` to advance
        it, then asks `_should_checkpoint` whether the architecture →
        implementation boundary was just crossed AND
        `.peers/checkpoint_requested` is on disk. When yes, drops an
        `.peers/awaiting_user` marker (best-effort), logs an operator
        message, and returns the loop's exit dict with sentinel
        `checkpoint:phase-0-complete`. Returns None on the normal
        (non-checkpoint) path so the caller proceeds with the tick.
        """
        prev_phase = state.get("phase")
        self._record_phase(state)
        curr_phase = state.get("phase", PHASE_IMPLEMENTATION)
        if not _should_checkpoint(
            self.peer_dir,
            prev_phase=prev_phase, curr_phase=curr_phase,
        ):
            return None
        reason = "checkpoint:phase-0-complete"
        try:
            (self.peer_dir / "awaiting_user").write_text(
                f"checkpoint at iter={state.get('iteration', 0)}\n"
                "review RECON.md + PLAN.aligned.md + "
                "ARCHITECTURE.intended.md, then run "
                "`peers-ctl resume <project>` + "
                "`peers-ctl start <project>` to continue.\n",
                encoding="utf-8",
            )
        except OSError as e:
            print(
                f"peers: warning, failed to write awaiting_user marker: {e!r}",
                file=sys.stderr, flush=True,
            )
        print(
            f"peers: {reason} — Phase 0 prep complete "
            f"(iter={state.get('iteration', 0)}). Loop pausing for "
            "operator review. Clear with `peers-ctl resume <project>` "
            "then re-launch with `peers-ctl start <project>`.",
            file=sys.stderr, flush=True,
        )
        return self._exit_with_fresh_results(state, reason, ticks)

    # --- ///per-tick observability helpers ------------

    def _emit_tick_end(
        self, state: dict[str, Any], peer: str, run: RunResult,
        success: bool, tick_dt: int, head_after_sha: str | None,
    ) -> None:
        """end-of-tick marker + optional verbose echo. Runs
        after `_save_state`, so the iteration number printed here
        matches the on-disk state.
        """
        status_str = _format_tick_status(
            success=success, classification=run.classification,
        )
        if head_after_sha and head_after_sha != self._head_before_invoke:
            head_short = head_after_sha[:8]
        else:
            head_short = "no-new-commit"
        print(
            f"peers: tick {state['iteration']} {status_str} "
            f"head={head_short} dur={tick_dt}s",
            file=sys.stderr, flush=True,
        )
        if self.verbose:
            self._echo_peer_output(state["iteration"], peer, run)


    # Bounded-disk safeguard for the log directory: once more than
    # this many tick-*.log files accumulate, the oldest gets gzipped.
    _PEER_LOG_ROTATE_THRESHOLD = 200

    def _write_peer_output_logs(
        self, tick_n: int, peer: str, run: RunResult,
    ) -> None:
        """persist full stdout/stderr for this tick to disk.
        Skips empty streams (no zero-byte files). Never crashes the
        loop on I/O errors — observability must not break the run.
        """
        try:
            self._verify_peer_dir_identity()
            # 5-digit zero-pad supports runs up to 99999 ticks.
            base = f"tick-{tick_n:05d}-{peer}"
            if run.stdout:
                _write_text_in_private_nested_dir_no_symlink(
                    self.peer_dir, ("log", "peers"),
                    f"{base}.stdout.log", run.stdout,
                )
            if run.stderr:
                _write_text_in_private_nested_dir_no_symlink(
                    self.peer_dir, ("log", "peers"),
                    f"{base}.stderr.log", run.stderr,
                )
            self._maybe_rotate_peer_logs()
        except Exception as e:
            print(
                f"peers: note: could not write per-tick peer log: {e}",
                file=sys.stderr,
            )

    def _write_prompt_log(self, tick_n: int, peer: str, prompt: str) -> None:
        """persist the prompt sent to this peer for offline review."""
        if not prompt:
            return
        try:
            self._verify_peer_dir_identity()
            fname = f"tick-{tick_n:05d}-{peer}.txt"
            _write_text_in_private_nested_dir_no_symlink(
                self.peer_dir, ("log", "prompts"), fname, prompt,
            )
        except Exception as e:
            print(
                f"peers: note: could not write per-tick prompt log: {e}",
                file=sys.stderr,
            )

    def _maybe_rotate_peer_logs(self) -> None:
        """Gzip the oldest `.log` file once the directory grows past
        the threshold. Defensive (best-effort): any error is silently
        swallowed — this is a disk-budget safeguard, not a correctness
        feature.
        """
        dir_fd = -1
        try:
            dir_fd = _open_private_nested_dir_fd_no_symlink(
                self.peer_dir, ("log", "peers"),
            )
            logs = sorted(
                name for name in os.listdir(dir_fd)
                if name.startswith("tick-") and name.endswith(".log")
            )
            if len(logs) <= self._PEER_LOG_ROTATE_THRESHOLD:
                return
            oldest = logs[0]
            gz_name = f"{oldest}.gz"
            src_fd = -1
            dst_fd = -1
            try:
                src_fd = os.open(
                    oldest,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=dir_fd,
                )
                st = os.fstat(src_fd)
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                    return
                dst_fd = os.open(
                    gz_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                    dir_fd=dir_fd,
                )
                with os.fdopen(src_fd, "rb") as src:
                    src_fd = -1
                    with os.fdopen(dst_fd, "wb") as raw:
                        dst_fd = -1
                        with gzip.GzipFile(fileobj=raw, mode="wb") as dst:
                            while True:
                                chunk = src.read(64 * 1024)
                                if not chunk:
                                    break
                                dst.write(chunk)
            finally:
                if src_fd >= 0:
                    os.close(src_fd)
                if dst_fd >= 0:
                    os.close(dst_fd)
            os.unlink(oldest, dir_fd=dir_fd)
        except Exception:
            pass
        finally:
            if dir_fd >= 0:
                os.close(dir_fd)

    # echo caps — enough to see the bottom of a peer's reasoning
    # plus any trailing error noise without flooding the terminal.
    _VERBOSE_STDOUT_TAIL_LINES = 50
    _VERBOSE_STDERR_TAIL_LINES = 25

    def _echo_peer_output(
        self, tick_n: int, peer: str, run: RunResult,
    ) -> None:
        """print the last N lines of stdout + last M lines of stderr
        to substrate stderr, each line prefixed for grep-ability.
        """
        stdout_tail = (run.stdout or "").splitlines()[
            -self._VERBOSE_STDOUT_TAIL_LINES:
        ]
        stderr_tail = (run.stderr or "").splitlines()[
            -self._VERBOSE_STDERR_TAIL_LINES:
        ]
        print(f"=== peer={peer} tick={tick_n} stdout ===",
              file=sys.stderr, flush=True)
        for line in stdout_tail:
            print(f"[peer-stdout] {line}", file=sys.stderr)
        print(f"=== peer={peer} tick={tick_n} stderr ===",
              file=sys.stderr, flush=True)
        for line in stderr_tail:
            print(f"[peer-stderr] {line}", file=sys.stderr)
        print(f"=== peer={peer} tick={tick_n} end ===",
              file=sys.stderr, flush=True)

    def _update_convergence_counter(self, state: dict[str, Any]) -> None:
        """thorough-mode convergence counter. Counts ticks that
        landed WITHOUT a new crit/high/med Bug-Report or weak-fix/shallow-
        fix flag-bug since the tick started. Cheap to compute every tick
        (single `git log <range>`); the actual gating is in
        `convergence_reached.py`. Wrapped to never crash the main loop on
        an audit-side glitch.

        Skipped in dry_run because commits were reset above; counting
        commits in an empty range would increment trivially every tick
        and make convergence-reached pass without doing real work.
        """
        if self.dry_run:
            return
        since = self._head_before_invoke
        if since is None:
            return
        try:
            n_blocking = count_new_blocking_or_flag_bug_reports(
                self.repo, since,
            )
        except Exception:
            n_blocking = 0
        if n_blocking == 0:
            state["consecutive_clean_ticks"] = state.get(
                "consecutive_clean_ticks", 0
            ) + 1
        else:
            state["consecutive_clean_ticks"] = 0
        # Re-save after the counter mutation. Cheap.
        self._save_state(state)

    # --- helpers -----------------------------------------------------

    def _exit_with_fresh_results(
        self, state: dict[str, Any], reason: str, ticks: int,
    ) -> dict[str, Any]:
        results = self.engine.evaluate_hard_gates()
        self._record_results(state, results)
        self._save_state(state)
        self._append_exit_event(reason, ticks)
        return {"reason": reason, "state": state}

    def _pre_tick_exit(
        self, state: dict[str, Any], max_ticks: int | None, ticks: int,
    ) -> tuple[dict[str, Any] | None, dict[str, GoalResult]]:
        if max_ticks is not None and ticks >= max_ticks:
            return self._exit_with_fresh_results(state, "max_ticks", ticks), {}
        budget_reason = BudgetCheck(state).reason()
        if budget_reason is not None:
            reason = f"budget:{budget_reason}"
            return self._exit_with_fresh_results(state, reason, ticks), {}
        mutation_reason = self._goal_mutation_reason()
        if mutation_reason is not None:
            reason = f"goal-mutation:{mutation_reason}"
            self._save_state(state)
            self._append_exit_event(reason, ticks)
            return {"reason": reason, "state": state}, {}
        results = self.engine.evaluate_hard_gates()
        self._record_results(state, results)
        # Task 6.5: maintain implement-mode two-phase convergence counters
        # before consulting `_all_green_including_soft`. No-op for other
        # modes (strict backward-compat).
        self._update_two_phase_counters(state, results)
        if self._all_green_including_soft(state):
            # auto-skeptic re-audit before declaring complete.
            # When convergence is fresh (no skeptic run yet for this
            # convergence cycle), set a flag that injects a critical-
            # re-audit prompt into the next tick. If that tick stays
            # clean → really terminal next time around. If it surfaces
            # a new blocking bug → counter resets, normal loop continues.
            current_iter = state.get("iteration", 0)
            last_skeptic_at = state.get("_auto_skeptic_ran_at", -2)
            if (self.auto_skeptic_enabled
                    and current_iter - last_skeptic_at > 1):
                state["_auto_skeptic_prompt_pending"] = True
                self._save_state(state)
                print(
                    f"peers: convergence-reached at iter={current_iter}; "
                    "running auto-skeptic re-audit tick before terminal "
                    "exit (disable with --without-post-convergence-skeptic)",
                    file=sys.stderr, flush=True,
                )
                return None, results
            # Task 6.5: implement-mode requires two-phase convergence
            # before declaring "complete". Non-implement modes fall
            # through to the original exit unchanged.
            if self.mode_name == "implement":
                if state.get("convergence_phase") != "complete":
                    self._save_state(state)
                    return None, results
            self._save_state(state)
            self._append_exit_event("complete", ticks)
            return {"reason": "complete", "state": state}, results
        return None, results

    # Task 6.5: skeptic gates whose continued green is required during
    # Phase B (after Phase A's N consecutive all-hard-gates-green ticks).
    _PHASE_B_SKEPTIC_GATES = (
        "blind-review", "honesty-audit", "concerns-resolved",
    )

    def _update_two_phase_counters(
        self, state: dict[str, Any], results: dict[str, GoalResult],
    ) -> None:
        """Task 6.5: maintain implement-mode convergence_phase machine.

        Pure additive: writes `convergence_phase`, `consecutive_hard_
        green_ticks`, and `phase_b_extra_ticks` to state on implement-
        mode runs only. Other modes are short-circuited so their
        state.json carries no Task 6.5 fields.
        """
        if self.mode_name != "implement":
            return
        all_hard_green = all(
            r.state == "pass" for r in results.values()
        ) if results else False
        if all_hard_green:
            state["consecutive_hard_green_ticks"] = state.get(
                "consecutive_hard_green_ticks", 0,
            ) + 1
        else:
            state["consecutive_hard_green_ticks"] = 0
        current_phase = state.get("convergence_phase", "A")
        # Track Phase B accumulator: only count ticks where the three
        # skeptic gates are all green AND we are already in Phase B.
        if current_phase == "B":
            skeptic_ok = all(
                results.get(gid) is not None
                and results[gid].state == "pass"
                for gid in self._PHASE_B_SKEPTIC_GATES
            )
            if skeptic_ok:
                state["phase_b_extra_ticks"] = state.get(
                    "phase_b_extra_ticks", 0,
                ) + 1
            else:
                state["phase_b_extra_ticks"] = 0
        else:
            state.setdefault("phase_b_extra_ticks", 0)
        next_phase = _resolve_convergence_state(
            self.mode_name,
            current_phase,
            state["consecutive_hard_green_ticks"],
            phase_a_n=5,
            phase_b_n=2,
            phase_b_extra_ticks=state["phase_b_extra_ticks"],
        )
        state["convergence_phase"] = next_phase

    def _account_tokens_usd(
        self, state: dict[str, Any], tool: str, run: Any,
    ) -> tuple[int, float]:
        """G5: parse tokens/$ from this run's output, keyed by the
        PeerSpec's `tool` (two peers both running `claude` share the
        same parser). Returns (tokens_this_tick, usd_this_tick) for
        the run-log."""
        parser = _TOKEN_PARSERS.get(tool)
        if parser is None:
            return 0, 0.0
        tokens, usd = parser(run.stdout + run.stderr)
        state["budget"]["spent_tokens"] = (
            state["budget"].get("spent_tokens", 0) + tokens
        )
        state["budget"]["spent_usd"] = (
            state["budget"].get("spent_usd", 0.0) + usd
        )
        return tokens, usd

    def _handle_pattern_match_and_halt(
        self, state: dict[str, Any], ticks: int, upcoming_tick: int,
        peer: str, run: Any,
    ) -> dict[str, Any] | None:
        """+II (post-2026-05-24): emit one stderr marker per
        api-error tick (so the operator can see WHICH pattern killed
        the peer without grepping runs.jsonl) and, when the matched
        pattern was a HALT class (AUTH/QUOTA), tear the loop down
        with a peer-unavailable exit_event instead of degrading and
        retrying. Returns the loop's exit dict on halt; None
        otherwise."""
        if (run.classification == "api-error"
                and run.matched_error_pattern):
            halt_tag = " HALT-CLASS" if run.halt_required else ""
            print(
                f"peers: tick {upcoming_tick} peer={peer}{halt_tag} "
                f"matched-pattern source={run.matched_error_source or '?'} "
                f"pattern={run.matched_error_pattern[:80]} "
                f"snippet={run.matched_error_snippet[:120]!r}",
                file=sys.stderr, flush=True,
            )
        if not run.halt_required:
            return None
        self._write_peer_output_logs(upcoming_tick, peer, run)
        pinfo = state["peers"].setdefault(peer, {})
        pinfo["state"] = "unavailable"
        pinfo["unavailable_reason"] = (
            f"halt-pattern: {run.matched_error_pattern[:80]}"
        )
        pinfo["unavailable_at_iter"] = state.get("iteration", 0)
        pinfo["unavailable_snippet"] = run.matched_error_snippet[:200]
        print(
            f"peers: HALT — peer={peer} hit halt-class pattern. "
            "Operator action required (re-login, top-up, etc.). "
            f"Pattern: {run.matched_error_pattern[:80]}",
            file=sys.stderr, flush=True,
        )
        reason = f"peer-unavailable:{peer}"
        self._save_state(state)
        self._append_exit_event(reason, ticks)
        return {"reason": reason, "state": state}

    def _prepare_tick_prompt(
        self, state: dict[str, Any], tm: TurnManager,
        results: dict[str, GoalResult],
    ) -> tuple[str, PeerSpec, str]:
        peer = tm.current()
        others = tm.others()
        other_for_prompt = others[0] if len(others) == 1 else ", ".join(others)
        inbox = self._read_inbox(others, state, receiver=peer)
        stuck = any(
            state["stuck_counter"].get(gid, 0) >= 10
            for gid, gr in results.items() if gr.state == "fail"
        )
        warnings = self._pop_prompt_warnings(state)
        prompt = build_prompt(
            peer=peer, other=other_for_prompt,
            goals=self.goals, results=results,
            inbox=inbox, stuck=stuck,
            warnings=warnings,
            soft_reviews_pending=self._soft_reviews_pending(state, peer),
            comm_variant=self.comm_variant,
            all_peer_names=list(self.peer_names),
        )
        # Tasks 4.2-4.4: implement-mode Phase 0 prompt overlay. When
        # the current phase has a shipped template (recon / alignment /
        # architecture), prepend it to the regular prompt. Other phases
        # (and non-implement modes) get None back and skip the overlay.
        phase = state.get("phase", PHASE_IMPLEMENTATION)
        phase_prompt = _load_phase_prompt(self.mode_name, phase)
        if phase_prompt is not None:
            prompt = phase_prompt + "\n\n" + prompt
        # Task 6.2: blind-review tick role overlay. During implement-
        # mode's implementation phase, each tick is either an
        # implementer-tick (writes IMPLEMENTATION_NOTES.md) or a
        # reviewer-tick (writes REVIEW_NOTES.md without peeking) — see
        # `_resolve_peer_role`. The role-specific prompt is loaded via
        # the same `_load_phase_prompt` mechanism by treating
        # `blind_review_<role>` as a pseudo-phase name; the underlying
        # template lookup is path-traversal-safe.
        tick_for_role = state.get("iteration", 0)
        role = _resolve_peer_role(self.mode_name, phase, tick_for_role)
        if role in ("implementer", "reviewer"):
            role_prompt = _load_phase_prompt(
                self.mode_name, f"blind_review_{role}",
            )
            if role_prompt is not None:
                prompt = role_prompt + "\n\n" + prompt
        # post-convergence skeptic re-audit overlay. When
        # _auto_skeptic_prompt_pending is set (by _pre_tick_exit on
        # first detection of convergence), prepend a critical-re-audit
        # header to the regular prompt and record that the skeptic ran
        # at this iteration (= the tick about to fire, iteration+1).
        if state.pop("_auto_skeptic_prompt_pending", False):
            state["_auto_skeptic_ran_at"] = state.get("iteration", 0) + 1
            prompt = _AUTO_SKEPTIC_PROMPT_PREFIX + "\n\n" + prompt
        return peer, self.peers_by_name[peer], prompt

    def _pop_prompt_warnings(self, state: dict[str, Any]) -> list[str]:
        warnings = state.pop("warnings", [])
        if len(warnings) <= 50:
            return warnings
        return (
            warnings[:5]
            + [f"... <{len(warnings) - 55} warnings omitted> ..."]
            + warnings[-50:]
        )

    def _apply_anti_cheating_outcome(
        self, state: dict[str, Any], peer: str, success: bool,
    ) -> bool:
        pinfo = state["peers"][peer]
        if not success:
            if pinfo.get("failed_cheating"):
                pinfo["failed_cheating"] = 0
            return False
        cheating = self._classify_cheating(state)
        if cheating is None:
            if pinfo.get("failed_cheating"):
                pinfo["failed_cheating"] = 0
            return True
        reverted = self._revert_handoff(reason=cheating)
        pinfo["failed_cheating"] = pinfo.get("failed_cheating", 0) + 1
        pinfo["last_run"]["soft_fail_reason"] = (
            f"anti-cheating revert: {cheating}"
            + (" (revert failed)" if not reverted else "")
        )
        state.setdefault("warnings", []).append(
            f"anti-cheating: your previous handoff was reverted because "
            f"{cheating}. Fix the underlying production code instead of "
            f"relaxing tests / gaming metrics."
        )
        return False

    def _apply_dry_run_reset(
        self, state: dict[str, Any], success: bool,
    ) -> bool:
        if not self.dry_run or self._head_before_invoke is None:
            return success
        try:
            subprocess.run(
                ["git", "reset", "--hard", self._head_before_invoke],
                cwd=self.repo, check=True, capture_output=True,
            )
            return success
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode(
                "utf-8", errors="replace"
            )[-400:]
            state.setdefault("warnings", []).append(
                "dry-run reset FAILED — peer's commits remain in the "
                "working tree (dry-run guarantee broken). git reset "
                f"stderr: {stderr!r}"
            )
            return False

    def _record_tick_accounting(
        self, state: dict[str, Any], success: bool, tick_dt: int,
    ) -> None:
        state["iteration"] += 1
        budget = state["budget"]
        budget["spent_iterations"] += 1
        budget["spent_runtime_s"] += tick_dt
        if not success:
            budget["wasted_runtime_s"] = (
                budget.get("wasted_runtime_s", 0) + tick_dt
            )
        budget["consecutive_failures"] = (
            0 if success else budget["consecutive_failures"] + 1
        )

    def _record_results(self, state: dict[str, Any],
                        results: dict[str, GoalResult]) -> None:
        for gid, r in results.items():
            prev = state["goals_status"].get(gid, {}).get("state")
            state["goals_status"][gid] = {
                "state": r.state,
                "diagnostic": r.diagnostic,
                "duration_ms": r.duration_ms,
            }
            if r.state == "fail":
                if prev == "fail":
                    state["stuck_counter"][gid] = \
                        state["stuck_counter"].get(gid, 0) + 1
                else:
                    state["stuck_counter"][gid] = 1
            else:
                state["stuck_counter"].pop(gid, None)

    def _read_inbox(self, others: list[str],
                    state: dict[str, Any],
                    receiver: str | None = None) -> list[str]:
        bookmarks = state.setdefault("last_inbox_sha", {})
        msgs: list[str] = []
        for other in others:
            last_seen = bookmarks.get(other)
            if last_seen is None:
                # First call: seed cursor at current HEAD so we don't replay
                # all history as inbox.
                bookmarks[other] = self.comm.head_sha()
                continue
            try:
                commits = self.comm.new_commits_by(peer=other,
                                                   since=last_seen)
            except subprocess.CalledProcessError as e:
                state.setdefault("warnings", []).append(
                    f"git error reading inbox for {other}: {e}"
                )
                continue
            for c in commits:
                msgs.append(f"[{other}] {c.subject} ({c.sha[:8]})")
            if commits:
                bookmarks[other] = commits[-1].sha

        # in `comm: hybrid` mode the
        # driver previously WROTE-but-never-READ from the file channel.
        # Peers were instructed (via HYBRID_COMM_BLOCK in the prompt)
        # to drop markdown files at .peers/comms/<from>-to-<to>/ but
        # the substrate ingested none of them — the channel was
        # effectively write-only. Fix: when hybrid is active AND we
        # know which peer is about to run (receiver), fetch their inbox
        # files from each other peer, surface them in the prompt's
        # inbox section, and archive them once consumed.
        if isinstance(self.comm, HybridCommLayer) and receiver is not None:
            self._verify_peer_dir_identity()
            for other in others:
                try:
                    paths = self.comm.fetch_new(other, receiver)
                except OSError as e:
                    state.setdefault("warnings", []).append(
                        f"hybrid inbox read error for {other}→{receiver}: {e}"
                    )
                    continue
                for p in paths:
                    try:
                        text = read_text_no_symlink(p, max_bytes=4001)
                    except OSError as e:
                        state.setdefault("warnings", []).append(
                            f"hybrid inbox skipped {p.name}: {e}"
                        )
                        continue
                    # Trim — long bodies bloat the prompt without value.
                    snippet = text[:4000]
                    if len(text) > 4000:
                        snippet += "\n... (truncated)"
                    msgs.append(
                        f"[{other} → file {p.name}]\n{snippet}"
                    )
                    try:
                        self.comm.archive(p)
                    except OSError as e:
                        state.setdefault("warnings", []).append(
                            f"hybrid inbox archive failed for {p.name}: {e}"
                        )
        return msgs

    def _post_run(self, state: dict[str, Any], peer: str,
                  run: RunResult) -> bool:
        info: dict[str, Any] = {
            "classification": run.classification,
            "duration_ms": run.duration_ms,
        }
        state["peers"][peer]["last_run"] = info

        # Dogfood-R2 finding: claude in -p (print) mode is silent
        # while it works. With a too-low idle_timeout_s, the
        # HealthGuard kills it AFTER it has already committed a valid
        # handoff. Treat idle-timeout + valid handoff as partial-
        # success: the peer's contract was met (`## Self-Review` +
        # trailers), only the print-and-exit step got cut off.
        # Other non-success classifications (process-fail, api-error,
        # absolute-timeout) are NOT promoted — those leave incomplete
        # work much more often.
        accept_despite_class = (run.classification == "idle-timeout")
        if run.classification != "success" and not accept_despite_class:
            info["soft_fail_reason"] = f"run classification {run.classification}"
            return False

        # Verify the peer actually produced a handoff commit.
        since = self._head_before_invoke
        try:
            new_commits = self.comm.new_commits_by(peer=peer, since=since)
        except Exception as e:
            info["soft_fail_reason"] = f"cannot read git: {e}"
            return False

        if not new_commits:
            info["soft_fail_reason"] = (
                "no commit by peer this turn"
                + (f" (classification was {run.classification})"
                   if run.classification != "success" else "")
            )
            return False

        # H9: fast-forward only — reject amend / rebase that rewrites
        # the previous head_sha out of history.
        if since is not None and not self._is_ancestor(since, "HEAD"):
            info["soft_fail_reason"] = (
                f"history was rewritten: {since[:8]} is no longer an "
                "ancestor of HEAD (amend / rebase / reset is not allowed)"
            )
            return False

        # Look for Peer-Review-Of commits in this turn (G4 soft reviews).
        # also count per-tick ingest/reject so runs.jsonl shows
        # WHY a soft-consensus might be stuck at 0/2 — operators were
        # previously left grepping for "soft-review ignored" in warnings.
        #
        # Count ingestions directly from the helper's return value.
        # An earlier version differenced summed history-list lengths
        # before/after the loop, which silently under-counted whenever a
        # goal's history was already at its 20-entry cap: each new
        # ingest shifted an old entry out, the delta landed at 0, and
        # `soft_reviews_rejected` was inflated to `soft_seen`.
        soft_seen = 0
        soft_ingested = 0
        for c in new_commits:
            if c.trailers.get("Peer-Review-Of"):
                soft_seen += 1
            if self._record_soft_review_from_commit(
                    state, c, reviewer=peer):
                soft_ingested += 1
        info["soft_reviews_seen"] = soft_seen
        info["soft_reviews_ingested"] = soft_ingested
        info["soft_reviews_rejected"] = soft_seen - soft_ingested

        # ANY commit in this turn carrying the handoff trailers counts as
        # a valid handoff — peer is allowed to add tidy-up commits AFTER
        # the handoff commit. (Stricter "last commit must be handoff" was
        # too brittle; see integration test test_trailing_junk_commit.)
        if any(
            c.trailers.get("Peer-Status") == "handoff"
            and c.trailers.get("Self-Review") == "pass"
            for c in new_commits
        ):
            # If we got here via idle-timeout (the peer was killed
            # AFTER committing), flag it so the operator knows to
            # raise idle_timeout_s — and so the next prompt can
            # mention it.
            if run.classification == "idle-timeout":
                info["partial_handoff_rescued"] = True
                state.setdefault("warnings", []).append(
                    f"healthguard: {peer!r} hit idle_timeout_s after "
                    "committing a valid handoff. Work was kept. "
                    "Consider raising health.idle_timeout_s if this "
                    "happens repeatedly (claude -p is silent while "
                    "working)."
                )
            return True

        info["soft_fail_reason"] = (
            "no commit in this turn carries Peer-Status: handoff + "
            "Self-Review: pass trailers"
        )
        return False

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        r = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=self.repo, capture_output=True,
        )
        return r.returncode == 0

    _TEST_PATH_RE = None  # lazy compile in _detect_tampering

    def _diff_stats_since_invoke(self) -> tuple[int, int] | None:
        """Returns (test_lines_added_or_removed, src_lines_…) for the
        diff between `_head_before_invoke` and HEAD. None if anything
        is amiss."""
        import re as _re
        if OrchestratorDriver._TEST_PATH_RE is None:
            OrchestratorDriver._TEST_PATH_RE = _re.compile(
                r"(^|/)(tests?/|test_[^/]+\.py$|.*_test\.go$|.*\.test\."
                r"[a-zA-Z]+$)"
            )
        since = self._head_before_invoke
        if since is None or since == self.comm.head_sha():
            return None
        try:
            r = subprocess.run(
                ["git", "diff", "--numstat", f"{since}..HEAD"],
                cwd=self.repo, capture_output=True, text=True,
                check=True, encoding="utf-8", errors="replace",
            )
        except subprocess.CalledProcessError:
            return None
        test_lines = 0
        src_lines = 0
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            add, rem, path = parts
            try:
                delta = int(add) + int(rem)
            except ValueError:
                continue
            if OrchestratorDriver._TEST_PATH_RE.search(path):
                test_lines += delta
            else:
                src_lines += delta
        return test_lines, src_lines

    def _classify_cheating(self, state: dict[str, Any]) -> str | None:
        """classify the turn's diff for anti-cheating
        triggers. Returns a human-readable reason if it's clearly
        cheating, else None.

        Currently the only hard-block trigger is "only test files
        changed in this turn". Coverage-sanity / fail→pass flip
        detection is kept as a warning in `_detect_tampering`
        (less specific, more prone to false positives).
        """
        stats = self._diff_stats_since_invoke()
        if stats is None:
            return None
        test_lines, src_lines = stats
        if test_lines > 0 and src_lines == 0:
            return (
                f"the turn modified only test files "
                f"(+{test_lines} test lines, 0 source). Production "
                f"behavior cannot have been the reason a gate flipped."
            )
        return None

    @staticmethod
    def _stderr_text(e: subprocess.CalledProcessError) -> str:
        if e.stderr is None:
            return str(e)
        if isinstance(e.stderr, bytes):
            return e.stderr.decode("utf-8", errors="replace").strip()
        return str(e.stderr).strip()

    def _revert_handoff(self, reason: str) -> bool:
        """Run `git revert --no-commit <since>..HEAD` then commit the
        merged revert. Returns True on success.

        If neither the revert nor the destructive fallback works, the
        cheating commit stays in the tree — we MUST surface this loud
        and clear so the user knows the integrity guarantee broke.
        """
        since = self._head_before_invoke
        if since is None:
            return False
        try:
            subprocess.run(
                ["git", "revert", "--no-commit", f"{since}..HEAD"],
                cwd=self.repo, check=True, capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            # Fall back to a hard reset — destructive, but anti-cheating
            # is more important than preserving the cheating commit.
            print(
                "peers: anti-cheating revert via `git revert` failed "
                f"({self._stderr_text(e)}); falling back to "
                "`git reset --hard`",
                file=sys.stderr,
            )
            try:
                subprocess.run(
                    ["git", "reset", "--hard", since],
                    cwd=self.repo, check=True, capture_output=True,
                )
                return True
            except subprocess.CalledProcessError as e2:
                print(
                    "peers: CRITICAL: anti-cheating fallback reset ALSO "
                    f"failed; cheating commit "
                    f"{self.comm.head_sha()[:12]} is still in the tree. "
                    f"Manual intervention required. "
                    f"({self._stderr_text(e2)})",
                    file=sys.stderr,
                )
                return False
        try:
            subprocess.run(
                ["git",
                 "-c", "user.email=peers-substrate@local",
                 "-c", "user.name=peers-substrate",
                 "commit", "-m",
                 f"Anti-cheating revert: {reason}\n\n"
                 f"Peer: peers-substrate\n"],
                cwd=self.repo, check=True, capture_output=True,
            )
            return True
        except subprocess.CalledProcessError as e:
            print(
                "peers: anti-cheating revert staged but commit failed: "
                f"{self._stderr_text(e)}. Leaving partial revert in "
                "index.",
                file=sys.stderr,
            )
            return False

    def _detect_tampering(self, state: dict[str, Any]) -> None:
        """G8 + G9: best-effort anti-cheating heuristics, run AFTER
        each successful tick. Findings are appended to
        state['warnings'] and surfaced in the next prompt; we do not
        revert the commit automatically (too aggressive)."""
        import re as _re
        if OrchestratorDriver._TEST_PATH_RE is None:
            OrchestratorDriver._TEST_PATH_RE = _re.compile(
                r"(^|/)(tests?/|test_[^/]+\.py$|.*_test\.go$|.*\.test\."
                r"[a-zA-Z]+$)"
            )
        # Diff stats since the last handoff in this turn.
        since = self._head_before_invoke
        if since is None or since == self.comm.head_sha():
            return
        try:
            r = subprocess.run(
                ["git", "diff", "--numstat", f"{since}..HEAD"],
                cwd=self.repo, capture_output=True, text=True,
                check=True, encoding="utf-8", errors="replace",
            )
        except subprocess.CalledProcessError:
            return

        test_lines = 0
        src_lines = 0
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            add, rem, path = parts
            try:
                delta = int(add) + int(rem)
            except ValueError:
                continue
            if OrchestratorDriver._TEST_PATH_RE.search(path):
                test_lines += delta
            else:
                src_lines += delta

        # G8: heuristic — test-only commit that flips a previously
        # failing test-gate to pass. We can't directly correlate gate
        # state per-file, but flag the pattern for the next prompt.
        if test_lines > 0 and src_lines == 0:
            state.setdefault("warnings", []).append(
                f"test-tampering: turn modified only test files "
                f"(+{test_lines} lines, 0 src). Verify the tests still "
                "match the spec rather than being weakened."
            )

        # G9: heuristic — large coverage / pass-rate improvement with
        # tiny diff is suspicious. We track this in state for the next
        # tick to pick up in the prompt; quantitative check is done by
        # comparing the latest two goal evaluations.
        state.setdefault("recent_diff_stats", {})[
            self.comm.head_sha()
        ] = {"test_lines": test_lines, "src_lines": src_lines}

    def _goal_mutation_reason(self) -> str | None:
        """G7 + H1: detects in-flight tampering of goals.yaml. Compares
        the file's CURRENT hash to the IN-MEMORY snapshot captured by
        OrchestratorDriver.__init__, NOT to the on-disk goals.sha256.
        A peer that rewrites both files together can no longer fool
        the check.

        (e) (post-2026-05-24): allow paired feature work. v4
        tick 17 added `Goal.timeout_s` field + applied it in goals.yaml
        as one commit, which the substrate halted as gaming. A peer
        that legitimately extends the substrate must edit BOTH the
        engine code AND goals.yaml together; pure goals.yaml edits
        without an accompanying source change in the SAME HEAD commit
        still halt. `.peers/` files do NOT count as paired — they're
        all control-plane and a peer coordinating an attack against
        the gate would land there too.
        """
        self._verify_peer_dir_identity()
        gfile = self.peer_dir / "goals.yaml"
        if self._goal_hash_snapshot is None:
            return None
        if not gfile.exists():
            return "goals.yaml disappeared since loop start"
        try:
            actual = _hash_goals_yaml(gfile)
        except (OSError, ValueError) as e:
            return f"goals.yaml unreadable or unsafe during mutation check: {e}"
        if self._goal_hash_snapshot == actual:
            return None

        # (e): is the change paired with an out-of-.peers
        # source edit in the most recent commit? If yes, treat as
        # feature work, refresh the snapshot, and let the loop carry
        # on.
        if self._head_commit_pairs_goals_with_source(gfile):
            # Refresh the snapshot so the *next* tick doesn't keep
            # halting on the same delta.
            self._goal_hash_snapshot = actual
            return None

        return (
            f"goals.yaml hash changed since loop start "
            f"(expected {self._goal_hash_snapshot[:8]}, "
            f"got {actual[:8]}). If intentional, the change must land "
            "in a commit that ALSO touches a source file outside "
            "`.peers/` (e.g., src/) — pure control-plane edits still "
            "trip the mutation guard."
        )

    def _head_commit_pairs_goals_with_source(self, gfile: Path) -> bool:
        """(e): returns True iff HEAD's tree contains the
        current goals.yaml content (i.e., the edit is committed, not
        an uncommitted working-tree change) AND HEAD's commit touched
        at least one file that is NOT under `.peers/`.

        Safe-fail: any git error or unexpected output returns False so
        the calling code falls through to the existing halt-and-block
        behavior."""
        try:
            r = subprocess.run(
                ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                capture_output=True, check=True, text=True, timeout=10,
            )
            head_sha = r.stdout.strip()
            if not re.fullmatch(r"[0-9a-f]{7,64}", head_sha):
                return False
            # Confirm working tree's goals.yaml content matches HEAD's
            # blob — guards against an uncommitted mid-loop edit
            # masquerading as the paired commit.
            blob = subprocess.run(
                ["git", "-C", str(self.repo), "show",
                 f"{head_sha}:.peers/goals.yaml"],
                capture_output=True, check=True, timeout=10,
            )
            if hashlib.sha256(blob.stdout).hexdigest() != _hash_goals_yaml(gfile):
                return False
            # What files did HEAD touch?
            files = subprocess.run(
                ["git", "-C", str(self.repo),
                 "diff-tree", "--no-commit-id", "--name-only", "-r",
                 head_sha],
                capture_output=True, check=True, text=True, timeout=10,
            )
            touched = [
                p for p in files.stdout.split("\n") if p
            ]
            # Need: .peers/goals.yaml is in there AND at least one
            # path that is NOT under .peers/.
            has_goals = ".peers/goals.yaml" in touched
            has_source = any(not p.startswith(".peers/") for p in touched)
            return has_goals and has_source
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                FileNotFoundError, OSError, ValueError):
            return False

    def _soft_reviews_pending(self, state: dict[str, Any],
                              current_peer: str) -> list[Goal]:
        """Return soft goals whose consensus isn't yet reached AND the
        current peer is expected to weigh in on this turn.

        Reviewer modes (matches goals.VALID_REVIEWER_MODES):
        - other: any non-active peer reviews on each of its turns.
        - both: all non-active peers must review.
        - alternating: review duty rotates over peer_order independent
          of TurnManager — see _alternating_reviewer for the index.
        - quorum: same scheduling as `other`, but consensus tallying
          uses quorum_num/quorum_den (see _record_soft_review_from_commit).
        """
        order = state["peer_order"]
        out: list[Goal] = []
        soft_status = state.get("soft_status", {})
        for g in self.goals:
            if g.type != "soft":
                continue
            sg = soft_status.get(g.id, {})
            if self._soft_goal_passed(g, sg, n_peers=len(order)):
                continue  # already green
            mode = g.reviewer or "other"
            if mode in ("other", "both", "quorum"):
                # Any non-author peer (i.e. anyone whose turn it currently
                # isn't, but tracking the "author" perspective is the
                # job of the consensus tally; here we just say "current
                # peer is eligible to review while it's their turn").
                out.append(g)
            elif mode == "alternating":
                # The current peer is eligible iff their index matches
                # the rotating reviewer slot.
                idx = self._alternating_reviewer_index(state, g)
                if idx is not None and order[idx] == current_peer:
                    out.append(g)
        return out

    def _alternating_reviewer_index(self, state: dict[str, Any],
                                    g: Goal) -> int | None:
        """For reviewer=alternating: tracks a per-goal rotating cursor
        in state['soft_status'][g.id]['alt_cursor']. Advances after
        each successful review (see _record_soft_review_from_commit).
        """
        sg = state.setdefault("soft_status", {}).setdefault(
            g.id,
            {"consensus_count": 0, "last_pass": None,
             "history": [], "alt_cursor": 0},
        )
        cursor = sg.get("alt_cursor", 0)
        n = len(state["peer_order"])
        if n <= 0:
            return None
        return cursor % n

    def _soft_goal_passed(self, g: Goal, sg: dict[str, Any],
                          n_peers: int) -> bool:
        """Centralizes "is this soft goal considered green" given the
        reviewer mode."""
        mode = g.reviewer or "other"
        if mode == "quorum":
            assert g.quorum_num and g.quorum_den, "quorum without N/M"
            # Last `quorum_den` reviews must contain ≥ quorum_num pass.
            recent = sg.get("history", [])[-g.quorum_den:]
            if len(recent) < g.quorum_den:
                return False
            return sum(1 for r in recent if r.get("pass")) >= g.quorum_num
        if mode == "both":
            # Every peer must have submitted `consensus_needed`
            # consecutive pass:true reviews. With n=2 that literally
            # means "both peers reviewed"; for n>2 it means "all peers
            # reviewed" (the mode's name is preserved but its semantics
            # generalise to n peers).
            per_peer = sg.get("per_peer", {})
            need = g.consensus_needed
            reviewers_needed = max(n_peers, 1)
            sufficient_reviewers = sum(
                1 for v in per_peer.values()
                if v.get("consensus_count", 0) >= need
            )
            return sufficient_reviewers >= reviewers_needed
        # other / alternating: a single rolling counter.
        return sg.get("consensus_count", 0) >= g.consensus_needed

    def _all_green_including_soft(self, state: dict[str, Any]) -> bool:
        """All hard gates pass AND all soft goals have consensus."""
        if not self.engine.all_green():
            return False
        n = len(state["peer_order"])
        for g in self.goals:
            if g.type != "soft":
                continue
            sg = state.get("soft_status", {}).get(g.id, {})
            if not self._soft_goal_passed(g, sg, n_peers=n):
                return False
        return True

    def _record_soft_review_from_commit(self, state: dict[str, Any],
                                        commit, reviewer: str) -> bool:
        """G4: a peer can ship a soft review by committing with body
        containing `## Review` plus a `Peer-Review-Of: <goal_id>`
        trailer. The body must be parseable as JSON (one block).

        Parsing failures used to be silent — the peer would never
        learn why their review didn't count. We now surface each
        failure as a warning that lands in the next prompt.

        Returns True when the review is ingested into
        `soft_status[gid].history`, False when the commit is not a
        review (no trailer), targets an unknown soft goal, or carries
        no parseable JSON. `_post_run` reads this return value to keep
        the runs.jsonl `soft_reviews_ingested` counter accurate even
        when the history list is at its 20-entry cap (where a
        delta-of-lengths reads as 0 after the trim).
        """
        goal_id = commit.trailers.get("Peer-Review-Of")
        if not goal_id:
            return False
        target_goal = next(
            (g for g in self.goals if g.id == goal_id and g.type == "soft"),
            None,
        )
        if target_goal is None:
            state.setdefault("warnings", []).append(
                f"soft-review ignored: commit {commit.sha[:8]} carries "
                f"Peer-Review-Of: {goal_id!r} but no soft goal with "
                "that id exists in goals.yaml."
            )
            return False
        # Extract first JSON object from body.
        #
        # the old regex
        # `\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}` only handled ONE level of
        # brace nesting, so a soft review carrying a structured payload
        # like `{"pass": true, "details": {"by_section": {...}}}` was
        # silently rejected. Use a brace-counter so arbitrary nesting
        # works (same logic as bug_hunt._first_json_block).
        body = commit.body
        payload = _extract_first_json_object(body)
        if payload is None:
            state.setdefault("warnings", []).append(
                f"soft-review ignored: commit {commit.sha[:8]} for "
                f"goal {goal_id!r} has no parseable JSON object in body. "
                "Re-emit as a fresh commit with a single `{...}` block."
            )
            return False
        passed = bool(payload.get("pass"))
        soft = state.setdefault("soft_status", {}).setdefault(
            goal_id,
            {"consensus_count": 0, "last_pass": None,
             "history": [], "alt_cursor": 0, "per_peer": {}},
        )
        mode = target_goal.reviewer or "other"

        # Rolling counter (used ONLY by other/alternating). Bumping it
        # in `both`/`quorum` mode would leak stale "green" state if the
        # user later edits the goal to `reviewer: other`.
        if mode in ("other", "alternating"):
            if passed:
                if soft.get("last_pass") is True:
                    soft["consensus_count"] = (
                        soft.get("consensus_count", 0) + 1
                    )
                else:
                    soft["consensus_count"] = 1
                soft["last_pass"] = True
            else:
                soft["consensus_count"] = 0
                soft["last_pass"] = False

        # Per-peer counter (used by `both`).
        per_peer = soft.setdefault("per_peer", {})
        pp = per_peer.setdefault(reviewer, {"consensus_count": 0,
                                            "last_pass": None})
        if passed:
            if pp.get("last_pass") is True:
                pp["consensus_count"] = pp.get("consensus_count", 0) + 1
            else:
                pp["consensus_count"] = 1
            pp["last_pass"] = True
        else:
            pp["consensus_count"] = 0
            pp["last_pass"] = False

        # Alternating cursor advances on every recorded review (pass
        # or fail), so duty rotates regardless of outcome.
        if mode == "alternating":
            n = len(state.get("peer_order") or [])
            if n > 0:
                soft["alt_cursor"] = (soft.get("alt_cursor", 0) + 1) % n

        soft.setdefault("history", []).append({
            "pass": passed,
            "reviewer": reviewer,
            "sha": commit.sha,
            "notes": payload.get("notes", ""),
        })
        # Keep history bounded.
        soft["history"] = soft["history"][-20:]
        return True

    def _dirty_worktree(self, state: dict[str, Any] | None = None) -> bool:
        try:
            r = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.repo, capture_output=True, text=True,
                check=True, encoding="utf-8", errors="replace",
            )
        except subprocess.CalledProcessError as e:
            # Fail-safe: a probe-failure is treated as DIRTY, not
            # clean. Otherwise a misbehaving git would mask the
            # tampering signal entirely.
            if state is not None:
                state.setdefault("warnings", []).append(
                    f"dirty-worktree probe failed: git status returned "
                    f"{e.returncode}; treating worktree as dirty"
                )
            return True
        return bool(r.stdout.strip())

    def _update_peer_health(self, state: dict[str, Any], peer: str,
                            success: bool) -> None:
        """Track per-peer recent failures (sliding window of 5). A peer
        with ≥ 3 failures out of the last 5 is marked `degraded`; a
        single success returns it to `healthy`. This is asymmetric on
        purpose (slow degrade, fast recovery): the loop has otherwise
        no leverage to retry a peer that just had a streak.
        adds an extra degrade trigger: ≥ 2 consecutive anti-
        cheating reverts also degrade the peer.

        (Audit note: a mixed [F,T,F,T,F] pattern can thrash
        degraded↔healthy across consecutive ticks. We accept the
        theoretical thrash to keep recovery responsive — a stuck-flag
        bias check happens at the loop level via stuck_counter.)"""
        t = state["peers"][peer]
        history = t.setdefault("recent_runs", [])  # list of bool
        history.append(success)
        if len(history) > 5:
            del history[:-5]
        recent_fails = sum(1 for ok in history if not ok)
        t["recent_fails"] = recent_fails
        prev_state = t.get("state")
        if success and prev_state == "degraded":
            t["state"] = "healthy"
            # (post-2026-05-24): clear degraded annotations
            # when peer recovers, so `peers-ctl status` doesn't show
            # stale "degraded since tick N" when state is healthy.
            t.pop("degraded_reason", None)
            t.pop("degraded_at_iter", None)
        elif (not success) and recent_fails >= 3:
            t["state"] = "degraded"
            if prev_state != "degraded":
                self._record_degraded_annotations(
                    state, peer, "recent-fails:{0}/5".format(recent_fails),
                )
        elif t.get("failed_cheating", 0) >= 2:
            t["state"] = "degraded"
            if prev_state != "degraded":
                self._record_degraded_annotations(
                    state, peer, "anti-cheating-reverts:{0}".format(
                        t.get("failed_cheating", 0),
                    ),
                )

    def _record_degraded_annotations(
        self, state: dict[str, Any], peer: str, base_reason: str,
    ) -> None:
        """(post-2026-05-24): when a peer is first marked
        degraded, persist (a) WHY it was marked (`degraded_reason`)
        and (b) WHICH iteration noticed (`degraded_at_iter`). Both
        surface in `peers-ctl status` and runs.jsonl so the operator
        can decide if it's recoverable (transient api-error → maybe
        wait) or terminal (auth-failed → re-login).

        Also emits one stderr line — substrate-level visibility for
        anyone tailing the container log."""
        t = state["peers"][peer]
        last_run = t.get("last_run") or {}
        classification = last_run.get("classification", "")
        matched = last_run.get("matched_error_pattern", "")
        snippet = last_run.get("matched_error_snippet", "")
        reason_bits = [base_reason]
        if classification:
            reason_bits.append(f"last={classification}")
        if matched:
            reason_bits.append(f"pattern={matched[:60]}")
        reason = " | ".join(reason_bits)
        t["degraded_reason"] = reason
        t["degraded_at_iter"] = state.get("iteration", 0)
        print(
            f"peers: peer={peer} marked DEGRADED at iter="
            f"{state.get('iteration', 0)}: {reason}"
            + (f"\n  snippet: {snippet[:200]}" if snippet else ""),
            file=sys.stderr, flush=True,
        )

    def _maybe_halt(self, state: dict[str, Any]) -> None:
        """If ALL peers are degraded, write HALTED.md and mark state."""
        self._verify_peer_dir_identity()
        order = state["peer_order"]
        all_degraded = all(
            state["peers"][p].get("state") in ("degraded", "halted")
            for p in order
        )
        if not all_degraded:
            return
        # All peers degraded → halt-state across the board.
        for p in order:
            state["peers"][p]["state"] = "halted"
        halted_path = self.peer_dir / "HALTED.md"
        if halted_path.exists():
            return
        diag_lines = [
            "# Peers loop halted: all peers degraded",
            "",
            f"Iteration: {state['iteration']}",
            f"Whose turn was next: {current_peer_name(state)}",
            "",
            "## Peer health",
        ]
        for peer in order:
            t = state["peers"][peer]
            diag_lines.append(
                f"- {peer}: state={t.get('state')} "
                f"recent_fails={t.get('recent_fails')} "
                f"last_run={t.get('last_run')}"
            )
        diag_lines += [
            "",
            "## What to do",
            "",
            "- Check `.peers/log/runs.jsonl` for the failure patterns.",
            "- Verify Claude/Codex auth tokens are valid.",
            "- Adjust `health.idle_timeout_s` or `error_patterns` if " +
            "false positives are killing healthy runs.",
            "- Delete this file once resolved; the loop will pick back up.",
        ]
        try:
            write_text_no_symlink(halted_path, "\n".join(diag_lines) + "\n")
        except Exception as e:
            print(
                f"peers: CRITICAL: could not write HALTED.md "
                f"({halted_path}): {e}. Loop is supposed to halt now "
                "but the marker file is missing; check disk space + "
                "permissions.",
                file=sys.stderr,
            )

    def _append_exit_event(self, reason: str, ticks: int) -> None:
        """Logging fix #7: write a synthetic `event: exit` line to
        runs.jsonl when the loop terminates, so a post-mortem can
        distinguish `complete` vs `max_ticks` vs `budget:X` vs
        `goal-mutation:X` without parsing the run()'s return value."""
        try:
            self._verify_peer_dir_identity()
            log_dir = self.peer_dir / "log"
            _ensure_private_dir(log_dir)
            append_text_in_dir_no_symlink(log_dir, "runs.jsonl", json.dumps({
                "event": "exit",
                "reason": reason,
                "ticks_in_run": ticks,
                "ts": datetime.now(timezone.utc).isoformat(),
            }) + "\n")
        except OSError as e:
            # Best-effort — don't let logging break a clean exit.
            print(f"peers: note: could not write exit event: {e}",
                  file=sys.stderr)

    def _append_warnings_history(self, state: dict[str, Any],
                                 warnings: list[str]) -> None:
        """Audit trail: copy each warning into `state['warnings_history']`
        so post-mortem analysis can reconstruct what fired even after
        the live `warnings` queue is consumed by the next prompt."""
        if not warnings:
            return
        hist = state.setdefault("warnings_history", [])
        now = datetime.now(timezone.utc).isoformat()
        for w in warnings:
            hist.append({"ts": now, "iter": state["iteration"], "w": w})
        # Bound to 500 entries; older ones drop off the front.
        if len(hist) > 500:
            del hist[:-500]

    def _append_run_log(self, state: dict[str, Any], peer: str,
                        run: RunResult, success: bool,
                        tokens_this_tick: int = 0,
                        usd_this_tick: float = 0.0,
                        head_before: str | None = None,
                        head_after: str | None = None,
                        warnings_emitted: list[str] | None = None,
                        ) -> None:
        """One JSON line per tick. Rich enough that you can answer
        post-mortem questions from runs.jsonl alone, without diving
        into state.json or .peers/HALTED.md."""
        self._verify_peer_dir_identity()
        log_dir = self.peer_dir / "log"
        _ensure_private_dir(log_dir)
        last = state["peers"][peer].get("last_run", {})
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "iteration": state["iteration"],
            "peer": peer,
            "tool": self.peers_by_name[peer].tool,
            "classification": run.classification,
            "exit_code": run.exit_code,
            "duration_ms": run.duration_ms,
            "success": success,
            "soft_fail_reason": last.get("soft_fail_reason"),
            "tokens_this_tick": tokens_this_tick,
            "usd_this_tick": round(usd_this_tick, 6),
            "spent_tokens_total": state["budget"].get("spent_tokens", 0),
            "spent_usd_total": round(
                state["budget"].get("spent_usd", 0.0), 6
            ),
            "head_before": head_before,
            "head_after": head_after,
            "peer_state_after": state["peers"][peer].get("state"),
            "warnings_emitted": list(warnings_emitted or []),
        }
        # Only attach matched-pattern keys for api-error ticks; keep
        # other ticks' entries lean. Operators reading runs.jsonl can
        # immediately see WHICH error_pattern fired (and a 200-char
        # snippet of the matched text) instead of grepping the
        # container's stdout log.
        if run.classification == "api-error" and run.matched_error_pattern:
            entry["matched_error_pattern"] = run.matched_error_pattern
            entry["matched_error_snippet"] = run.matched_error_snippet
            # BUG-007 defense-in-depth (audit-log layer): record
            # whether the in-loop scan or the post-join rescan caught
            # it. A rising post-join frequency is the operational
            # signal that the in-loop scan_buf is racing reader-drain
            # for some peer-CLI traffic shape.
            if run.matched_error_source:
                entry["matched_error_source"] = run.matched_error_source
        # persist stderr/stdout tails for ANY non-success
        # tick, not only when an error_pattern matched. Lets the
        # operator diagnose codex/claude exit-on-startup failures
        # (e.g. "Not inside a trusted directory", new auth errors)
        # straight from runs.jsonl without re-running the peer.
        # also keep a (shorter) peek on success ticks — operators
        # often want to see what a handoff peer actually said, without
        # opening .peers/log/peers/tick-NNNN-<peer>.stdout.log. Caps:
        # success 200/400 (peek), non-success 400/800 (forensic).
        if run.classification != "success":
            entry["stderr_tail"] = (run.stderr or "")[-800:]
            entry["stdout_tail"] = (run.stdout or "")[-400:]
        else:
            entry["stderr_tail"] = (run.stderr or "")[-400:]
            entry["stdout_tail"] = (run.stdout or "")[-200:]
        # surface per-tick soft-review ingestion so operators
        # can see why consensus_count is stuck at 0/2 (typical cause:
        # peer emitted JSON inside a code-fence or wrote prose between
        # the braces). Set by _post_run.
        for k in (
            "soft_reviews_seen",
            "soft_reviews_ingested",
            "soft_reviews_rejected",
        ):
            if k in last:
                entry[k] = last[k]
        append_text_in_dir_no_symlink(
            log_dir, "runs.jsonl", json.dumps(entry) + "\n"
        )
