from __future__ import annotations

from datetime import datetime, timezone
import gzip
import json
import os
import stat
import sys
from typing import Any

from peers.driver_helpers import _format_tick_status
from peers.driver_host import _DriverHost
from peers.health_guard import RunResult
from peers.safe_io import (
    _ensure_private_dir,
    _open_private_nested_dir_fd_no_symlink,
    _write_text_in_private_nested_dir_no_symlink,
    append_text_in_dir_no_symlink,
)


# Wave-2 §5.2: cap the per-tick gates snapshot so a project with an absurd
# number of gates can never bloat a single runs.jsonl line. Counts hard + soft
# entries combined; beyond the cap the snapshot is truncated (deterministic by
# sorted gate-id) and flagged with a "_truncated" marker.
_GATES_SNAPSHOT_MAX_ENTRIES = 200

# Defensive: any single gate-id longer than this is skipped (a gate-id is a
# short slug in practice; an oversized one signals a corrupt/hostile source).
_GATES_SNAPSHOT_MAX_ID_LEN = 200


def _build_gates_snapshot(
    goals_status: Any,
    soft_status: Any,
    soft_needed: dict[str, int],
) -> dict[str, Any] | None:
    """Build a compact, bounded ``{"hard": {...}, "soft": {...}}`` snapshot of
    this tick's gate stand from the IN-MEMORY results the driver already
    computed — never recompute, never shell.

    Shape (compact by design):
      ``hard``: ``{gate_id: "pass"|"fail"|"unknown"}`` from ``goals_status[gid].state``.
      ``soft``: ``{gate_id: "<count>/<needed>"}`` consensus from ``soft_status``.

    Fail-closed contract: this is pure + total. It NEVER raises — a garbage
    source (non-dict, non-dict entry, oversized id) is simply skipped. Returns
    ``None`` when there is nothing trustworthy to record (so the caller omits
    the field entirely rather than writing an empty map).

    The result is bounded to :data:`_GATES_SNAPSHOT_MAX_ENTRIES` total entries
    (hard preferred, then soft; both iterated in sorted gate-id order for
    determinism). A truncated snapshot carries ``"_truncated": True``.
    """
    snap: dict[str, Any] = {}
    remaining = _GATES_SNAPSHOT_MAX_ENTRIES
    truncated = False

    if isinstance(goals_status, dict):
        hard: dict[str, str] = {}
        for gid in sorted(goals_status.keys(), key=str):
            if remaining <= 0:
                truncated = True
                break
            info = goals_status.get(gid)
            if not isinstance(info, dict):
                continue
            gid_s = str(gid)
            if not gid_s or len(gid_s) > _GATES_SNAPSHOT_MAX_ID_LEN:
                continue
            st = info.get("state")
            st_s = str(st) if st in ("pass", "fail") else "unknown"
            hard[gid_s] = st_s
            remaining -= 1
        if hard:
            snap["hard"] = hard

    if isinstance(soft_status, dict):
        soft: dict[str, str] = {}
        for gid in sorted(soft_status.keys(), key=str):
            if remaining <= 0:
                truncated = True
                break
            entry = soft_status.get(gid)
            if not isinstance(entry, dict):
                continue
            gid_s = str(gid)
            if not gid_s or len(gid_s) > _GATES_SNAPSHOT_MAX_ID_LEN:
                continue
            try:
                count = int(entry.get("consensus_count", 0) or 0)
            except (TypeError, ValueError):
                count = 0
            needed = soft_needed.get(gid_s, 2)
            try:
                needed = int(needed)
            except (TypeError, ValueError):
                needed = 2
            # Defense in depth: a corrupt in-memory consensus_count (or a
            # hostile soft_needed) could be an enormous int whose decimal
            # rendering would blow past the per-line size bound. Clamp the
            # magnitude to [0, 10**9] AFTER coercion, before building the
            # "n/m" string — the numerator/denominator never exceed 10 digits.
            count = max(0, min(count, 10**9))
            needed = max(0, min(needed, 10**9))
            soft[gid_s] = f"{count}/{needed}"
            remaining -= 1
        if soft:
            snap["soft"] = soft

    if not snap:
        return None
    if truncated:
        snap["_truncated"] = True
    return snap


def _first_error_line(stderr: str, *, limit: int = 200) -> str:
    """First non-empty stderr line, stripped + truncated, for inline tick-end
    diagnostics on a failure classification. Empty string if stderr is blank."""
    for raw in stderr.splitlines():
        line = raw.strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""


class DriverObservabilityMixin(_DriverHost):
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
        # Surface the agent's own error inline on a failure classification, so an
        # otherwise-opaque "process-fail head=no-new-commit dur=0s" names its cause
        # (e.g. claude refusing --dangerously-skip-permissions as root) instead of
        # hiding it in a per-tick .stderr.log the operator must go digging for.
        reason = ""
        if not success and run.classification != "success" and run.stderr:
            snippet = _first_error_line(run.stderr)
            if snippet:
                reason = f" -- {snippet}"
        print(
            f"peers: tick {state['iteration']} {status_str} "
            f"head={head_short} dur={tick_dt}s{reason}",
            file=sys.stderr, flush=True,
        )
        if self.verbose:
            self._echo_peer_output(state["iteration"], peer, run)

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

    #: the tee-stream file suffixes the Wave-2 live tee writes. These end in
    #: ``.jsonl`` (NOT ``.log``), so they must be rotated as their OWN group with
    #: the same threshold/cap as the ``.log`` group — otherwise a long run with
    #: the tee enabled grows the directory without bound.
    _PEER_TEE_STREAM_SUFFIXES = (".stream.jsonl", ".stream.err.jsonl")

    def _maybe_rotate_peer_logs(self) -> None:
        """Gzip the oldest file in each rotation group once that group grows
        past the threshold. Two independent groups share the SAME
        :data:`_PEER_LOG_ROTATE_THRESHOLD`:

          * the per-tick ``tick-*.log`` stdout/stderr logs, and
          * the Wave-2 live-tee ``tick-*.stream.jsonl`` /
            ``tick-*.stream.err.jsonl`` streams (these end in ``.jsonl``, NOT
            ``.log``, so without their own group they would never rotate).

        Defensive (best-effort): any error is silently swallowed — this is a
        disk-budget safeguard, not a correctness feature. The gzipped output
        ends in ``.gz`` and so is never re-counted by either group's predicate.
        """
        dir_fd = -1
        try:
            dir_fd = _open_private_nested_dir_fd_no_symlink(
                self.peer_dir, ("log", "peers"),
            )
            names = os.listdir(dir_fd)
            log_group = sorted(
                name for name in names
                if name.startswith("tick-") and name.endswith(".log")
            )
            tee_group = sorted(
                name for name in names
                if name.startswith("tick-")
                and name.endswith(self._PEER_TEE_STREAM_SUFFIXES)
            )
            self._rotate_oldest_in_group(dir_fd, log_group)
            self._rotate_oldest_in_group(dir_fd, tee_group)
        except Exception:
            pass
        finally:
            if dir_fd >= 0:
                os.close(dir_fd)

    def _rotate_oldest_in_group(self, dir_fd: int, group: list[str]) -> None:
        """Gzip + unlink the oldest file in ``group`` iff the group is over the
        threshold. ``dir_fd`` is an already-opened (no-symlink) directory fd;
        ``group`` is the sorted list of candidate names. Fail-soft: any error is
        swallowed (best-effort disk-budget safeguard)."""
        try:
            if len(group) <= self._PEER_LOG_ROTATE_THRESHOLD:
                return
            oldest = group[0]
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
            "jsonl_liveness_fallback_used": (
                run.jsonl_liveness_fallback_used
            ),
            "jsonl_liveness_fallbacks": run.jsonl_liveness_fallbacks,
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
        # Wave-2 §5.2: a compact, bounded per-tick gate snapshot so the gate
        # stand of past ticks is reconstructable for the TUI history scrubber.
        # Sourced from the SAME in-memory results the driver already computed
        # this tick (state["goals_status"] + state["soft_status"]) — never
        # recomputed, never shelled. Fail-CLOSED: any error omits the field and
        # the tick is still logged (observability must never break a run).
        try:
            soft_needed = {
                g.id: g.consensus_needed
                for g in getattr(self, "goals", [])
                if getattr(g, "type", None) == "soft"
            }
            gates_snapshot = _build_gates_snapshot(
                state.get("goals_status"),
                state.get("soft_status"),
                soft_needed,
            )
            if gates_snapshot is not None:
                entry["gates"] = gates_snapshot
        except Exception:
            pass  # fail-closed: omit the field, never break the tick.
        append_text_in_dir_no_symlink(
            log_dir, "runs.jsonl", json.dumps(entry) + "\n"
        )
