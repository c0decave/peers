from __future__ import annotations

from datetime import datetime, timezone
import gzip
import json
import os
import stat
import sys
from typing import Any

from peers.driver_helpers import _format_tick_status
from peers.health_guard import RunResult
from peers.safe_io import (
    _ensure_private_dir,
    _open_private_nested_dir_fd_no_symlink,
    _write_text_in_private_nested_dir_no_symlink,
    append_text_in_dir_no_symlink,
)


class DriverObservabilityMixin:
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
        append_text_in_dir_no_symlink(
            log_dir, "runs.jsonl", json.dumps(entry) + "\n"
        )
