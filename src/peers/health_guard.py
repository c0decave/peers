"""Supervises a single CLI invocation.

Health model:
- Productive output keeps the run alive: every byte from stdout/stderr
  resets the idle deadline.
- Idle-timeout (default 15 min): kill only when there has been NO
  output for that long.
- Absolute-max-runtime (default 2 h): paranoid ceiling.
- Error patterns: per-stream regex; first match classifies as
  `api-error` and kills the child immediately.
- Per-stream search uses a rolling cursor so cost is O(new bytes), not
  O(total bytes squared).
- Per-stream buffer has a soft cap with head/tail truncation to avoid
  RAM exhaustion on noisy children.
"""
from __future__ import annotations

import os
import re
import select
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from peers.structured_halt import (
    classify_structured_halt,
    classify_structured_transient,
)


_BUF_SOFT_CAP_BYTES = 2 * 1024 * 1024  # 2 MiB per stream (default)
_BUF_KEEP_HEAD_LINES = 100
_BUF_KEEP_TAIL_LINES = 200
_PATTERN_SEARCH_TIMEOUT_S = 0.25
_READ_CHUNK_BYTES = 8192
# How long the reader's `select` blocks before re-checking its stop flag.
# Short = quick shutdown when the substrate signals; long = less wakeup
# overhead per stream. 0.25s is well below the operative idle_timeout
# but big enough that pure-CPU cost on a quiet stream is negligible.
_STOP_POLL_INTERVAL_S = 0.25

# follow-up (2026-05-24): in-loop orphan-zombie sweep cadence in
# the PID-1 container case. At ~10/s peak peer-helper exit rate, a 2s
# interval bounds peak zombie count to ~20 — well under the 8192 pids
# cgroup limit. The sweep itself is cheap (one os.listdir + stat read
# per /proc/[pid]) and only fires when peers is PID 1.
_ZOMBIE_SWEEP_INTERVAL_S = 2.0
# Bug A revisit: 60s was too tight — claude can legitimately think
# for 60-120s between tool calls (long extended-thinking, tool result
# processing). With Bug D now fixed (stream-json is the runtime default
# again), normal liveness comes from stdout. The jsonl-fallback is the
# emergency parachute for the rare case where stream-json fails but
# jsonl still writes. 300s (5min) gives meaningful buffer without
# letting a genuinely-stuck peer drag on forever.
_CLAUDE_JSONL_LIVENESS_WINDOW_S = 300


def claude_session_jsonl_path(cwd: str | Path) -> Path | None:
    """Return claude's session-jsonl directory for an absolute cwd.

    Claude stores per-session jsonl files below
    ``~/.claude/projects/<encoded-cwd>/`` where slashes in the absolute
    working directory are replaced by ``-``. The returned path is the
    directory; callers can inspect the newest ``*.jsonl`` file inside it.
    """
    home = os.environ.get("HOME")
    if not home:
        return None
    cwd_s = str(cwd)
    if not cwd_s.startswith("/"):
        return None
    encoded = "-" + cwd_s.lstrip("/").replace("/", "-")
    return Path(home) / ".claude" / "projects" / encoded


def jsonl_mtime_within(jsonl_dir: Path, within_seconds: int) -> bool:
    """True iff any session jsonl in ``jsonl_dir`` was touched recently."""
    if within_seconds <= 0 or not jsonl_dir.is_dir():
        return False
    threshold = time.time() - within_seconds
    try:
        candidates = list(jsonl_dir.glob("*.jsonl"))
    except OSError:
        return False
    for path in candidates:
        try:
            if path.stat().st_mtime >= threshold:
                return True
        except OSError:
            continue
    return False


def _utf8_size(text: str) -> int:
    return len(text.encode("utf-8", errors="replace"))


def _lines_utf8_size(lines: list[str]) -> int:
    return sum(_utf8_size(line) for line in lines)


def _take_utf8_prefix(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    return text.encode("utf-8", errors="replace")[:limit].decode(
        "utf-8", errors="ignore"
    )


def _take_utf8_suffix(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    return text.encode("utf-8", errors="replace")[-limit:].decode(
        "utf-8", errors="ignore"
    )


class _PatternSearchTimeout(Exception):
    def __init__(self, pattern: str) -> None:
        super().__init__(pattern)
        self.pattern = pattern


class _AlarmTimeout(Exception):
    def __init__(self) -> None:
        super().__init__("pattern search timed out")


def _search_pattern(pat: re.Pattern, text: str) -> re.Match | None:
    if not hasattr(signal, "setitimer"):
        return pat.search(text)

    def _raise_timeout(_signum, _frame):
        raise _AlarmTimeout()

    previous_handler = None
    handler_captured = False
    previous_timer = None
    try:
        previous_handler = signal.getsignal(signal.SIGALRM)
        handler_captured = True
        signal.signal(signal.SIGALRM, _raise_timeout)
        previous_timer = signal.setitimer(
            signal.ITIMER_REAL, _PATTERN_SEARCH_TIMEOUT_S
        )
    except (ValueError, AttributeError):
        if handler_captured:
            try:
                signal.signal(signal.SIGALRM, previous_handler)
            except (ValueError, AttributeError):
                pass
        return pat.search(text)
    try:
        return pat.search(text)
    except _AlarmTimeout as e:
        raise _PatternSearchTimeout(pat.pattern) from e
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        if handler_captured:
            signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer is not None and previous_timer[0] > 0:
            signal.setitimer(
                signal.ITIMER_REAL, previous_timer[0], previous_timer[1]
            )


# Halt-class matches stop the WHOLE run and demand operator action, so a
# false positive is expensive (it wedges the loop — see the v15 internal testing,
# 2026-06-04). A peer routinely echoes repo/tool content that contains
# error-shaped text: `git log --oneline` subjects, reproduce-test source,
# bug-report JSON, diffs. The halt patterns already drop QUOTED echoes via
# their `[^"]` prefix, but unquoted echoes (a commit subject, a markdown
# bullet) slip through.
#
# The discriminator is INTENTIONALLY ANCHORED to the LINE START (all `.match`,
# no `.search`): an echoed line carries its tell as a *prefix* — a git
# short-hash, a diff/markdown marker, a leading file:line citation, a
# `- [peer] …` review bullet, a JSON field key. A genuine CLI error line
# starts with the error itself (`ERROR: …`, a timestamp, a level), so it is
# NOT classified as echo even when it mentions a file:line or a BUG-NNN
# mid-message (review finding C2). Erring toward NOT-echo keeps real
# auth/quota halts firing — the safe direction, since codex has no structured
# backstop. (Residual: a real error beginning with a bare 7-40 hex token +
# space is rare enough to accept as the git-log shape.)
_ECHO_LINE_PREFIX = re.compile(
    r"""^(?:
        [0-9a-f]{7,40}\                       # git short-hash + space (git log --oneline)
      | (?:diff\ |index\ |@@\ |\+\+\+\ |---\ ) # unified-diff structural lines
      | \+                                     # unified-diff added line (a real error never starts with +)
      | \s*[#>]\                               # markdown heading / blockquote
      | \s*\d+[.)]\                            # markdown numbered list item
      | \s*[-*]\ +\[[^\]]+\]                   # markdown bullet + [tag] (peers review/handoff echo)
      | \s*"[^"]+"\s*:                          # JSON field key (bug-report body)
      | \s*[\w./-]+\.(?:py|md|ya?ml|txt|rs|js|ts|sh|toml|json|c|cc|cpp|h):\d+  # LEADING file:line citation
    )""",
    re.VERBOSE,
)


def _is_echoed_repo_content(line: str) -> bool:
    """True iff `line` *starts with* a shape only echoed repo/tool content
    has (git-log hash, diff/markdown marker, leading file:line citation,
    `- [peer]` review bullet, JSON field). Anchored at the line start so a
    genuine CLI error line — which leads with the error, not a citation — is
    never classified as echo (review finding C2)."""
    return bool(_ECHO_LINE_PREFIX.match(line))


def _matched_line(text: str, match: "re.Match") -> str:
    """The full line of `text` containing `match` (newline-delimited)."""
    start = text.rfind("\n", 0, match.start()) + 1
    end = text.find("\n", match.end())
    return text[start:] if end == -1 else text[start:end]


def _first_actionable_match(
    text: str,
    patterns: "Sequence[re.Pattern]",
    halt_pattern_strs: "frozenset[str] | set[str]",
) -> "re.Match | None":
    """Return the first match that should ACT (classify api-error / halt).
    A halt-pattern match whose line is echoed repo/tool content is skipped
    and scanning continues — mirroring the `[^"]` prefix that already drops
    quoted file dumps. Non-halt (error_patterns) matches always act.
    Preserves the SIGALRM-guarded search and the halt-before-error priority
    of `patterns` (halt patterns come first in the combined list)."""
    for pat in patterns:
        is_halt = pat.pattern in halt_pattern_strs
        offset = 0
        while offset < len(text):
            m = _search_pattern(pat, text[offset:])
            if m is None:
                break
            if is_halt and _is_echoed_repo_content(
                _matched_line(text[offset:], m)
            ):
                # Skip the ENTIRE echoed line, not just this match. Advancing
                # only past `m.end()` would re-anchor `(?m)^` mid-line, and the
                # post-offset remainder loses the echo prefix — so a second
                # halt-keyword hit on the same echoed line would forge a halt
                # (review finding C1). Resume at the next line start; `offset`
                # therefore always sits at a line boundary, keeping `^` and
                # `_matched_line` line-aligned.
                nl = text.find("\n", offset + m.end())
                if nl == -1:
                    break
                offset = nl + 1
                continue
            return m
    return None


@dataclass
class RunResult:
    classification: str
    # success | soft-fail | process-fail | idle-timeout | absolute-timeout | api-error
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    output_digest: str = ""
    # When classification == "api-error", this names the regex pattern
    # (the literal string from config.health.error_patterns) that hit,
    # plus a short context snippet of the matched text. Empty otherwise.
    # Surfaced into runs.jsonl so operators can diagnose api-error ticks
    # without grepping the raw container log.
    matched_error_pattern: str = ""
    matched_error_snippet: str = ""
    # BUG-007 defense-in-depth (audit-log layer): when an api-error or
    # process-fail came from pattern matching, record which scan path
    # caught it: "in-loop" (poll-loop scan_new), "post-join" (rescan
    # after reader.join — closes the race fixed by BUG-007), or ""
    # (no pattern match). A non-trivial post-join frequency is the
    # operational signal that scan_buf sizing or the race window
    # needs investigation. Empty for non-pattern classifications.
    matched_error_source: str = ""
    # True iff either stream hit the soft cap and got head/tail
    # truncated. Surfaces "the peer produced a wall of output we
    # couldn't keep" — useful in post-mortems even if the truncation
    # was harmless.
    truncated: bool = False
    # (post-2026-05-24): set True when the matched pattern
    # came from `health.halt_patterns` instead of `health.error_patterns`.
    # The orchestrator translates this into an exit_event
    # `peer-unavailable:<peer>` and halts the whole run instead of
    # silently degrading the peer. Intended for OAuth-expired and
    # quota-exhausted shapes where retrying is useless and the
    # operator must intervene.
    halt_required: bool = False
    # True when stdout/stderr were quiet past idle_timeout_s, but the
    # claude session jsonl was still being updated. This is the
    # print-mode false-idle escape hatch: liveness is stdout OR jsonl.
    jsonl_liveness_fallback_used: bool = False
    jsonl_liveness_fallbacks: int = 0


class _StreamCollector:
    """Reads a pipe in a background thread.

    - Appends every line to `self.buf`.
    - Updates `shared['last_output_t']` on every byte.
    - Exposes a scan cursor for incremental pattern search.
    - Caps buf size with head/tail truncation.

    All mutation is serialised via `self.lock`.
    """

    def __init__(self, name: str, stream, shared: dict,
                 shared_lock: threading.Lock,
                 buf_cap_bytes: int = _BUF_SOFT_CAP_BYTES,
                 scan_enabled: bool = True) -> None:
        self.name = name
        self.stream = stream
        self.shared = shared
        self.shared_lock = shared_lock
        self.lock = threading.Lock()
        self.buf: list[str] = []
        self._scan_buf: list[str] = []
        self._size = 0  # bytes
        self._scan_size = 0
        self._scan_cursor = 0
        self._truncated = False
        self._cap_bytes = buf_cap_bytes
        self._scan_enabled = scan_enabled
        self._scan_cap_bytes = max(buf_cap_bytes, _READ_CHUNK_BYTES)
        # a grandchild process that
        # inherits the pipe's write-end can keep `os.read` blocked
        # forever even after the parent peer is reaped (e.g. a node
        # subprocess of `claude` that called setsid() escapes our
        # killpg). A blocking reader thread is daemon=True so it can't
        # block exit, but it counts against per-process NPROC and
        # leaks across ticks until the substrate hits "can't start new
        # thread". `_stop` lets `invoke()` signal "you may exit even
        # if no EOF arrived"; the reader checks it via `select` between
        # reads so the cost is one fd-poll per `_STOP_POLL_INTERVAL_S`.
        self._stop = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name=f"hg-reader-{name}", daemon=True,
        )

    def _run(self) -> None:
        import codecs

        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        fd = self.stream.fileno()
        while not self._stop.is_set():
            try:
                readable, _, _ = select.select(
                    [fd], [], [], _STOP_POLL_INTERVAL_S
                )
            except (OSError, ValueError):
                # fd closed under us (substrate cleanup) → exit cleanly.
                break
            if not readable:
                continue
            try:
                chunk = os.read(fd, _READ_CHUNK_BYTES)
            except OSError:
                break
            if not chunk:
                break
            text = decoder.decode(chunk)
            if text:
                self._append_chunk(text, len(chunk))
            with self.shared_lock:
                self.shared["last_output_t"] = time.monotonic()
        tail = decoder.decode(b"", final=True)
        if tail:
            self._append_chunk(tail, _utf8_size(tail))
        try:
            self.stream.close()
        except Exception:
            pass

    def request_stop(self) -> None:
        """Signal the reader thread to exit at the next stop-poll.

        Use after `_terminate_and_reap` (or natural exit) so the reader
        doesn't outlive `invoke()` when a grandchild kept the pipe
        write-end open. Cooperates with `select(timeout=_STOP_POLL_…)`
        in `_run`, so exit happens within ~_STOP_POLL_INTERVAL_S of
        this call."""
        self._stop.set()

    def _append_chunk(self, text: str, byte_size: int) -> None:
        with self.lock:
            self.buf.append(text)
            self._size += byte_size
            if self._scan_enabled:
                self._scan_buf.append(text)
                self._scan_size += byte_size
                if self._scan_size > self._scan_cap_bytes:
                    scan_text = "".join(self._scan_buf)
                    scan_tail = _take_utf8_suffix(
                        scan_text, self._scan_cap_bytes
                    )
                    self._scan_buf = [scan_tail]
                    self._scan_size = _utf8_size(scan_tail)
            if self._size > self._cap_bytes:
                self._truncate_locked()

    def _truncate_locked(self) -> None:
        # the early-return below used to
        # be `if len(self.buf) <= 301: return`, which let a small
        # number of HUGE lines bypass truncation entirely (one 100 MiB
        # line → buf has 1 element → return → RAM keeps growing).
        # Two-tier strategy:
        #
        #   1. If lines are plentiful (> 300), do the existing
        #      head-N-lines + tail-N-lines + marker truncation.
        #   2. If lines are few but size still over cap, byte-truncate
        #      the tail of the largest line(s) so total size returns
        #      under cap.
        #
        # Either path sets `_truncated=True` so the RunResult contract
        # ("we capped your output") holds.
        if self._size <= self._cap_bytes:
            return
        if (len(self.buf)
                > _BUF_KEEP_HEAD_LINES + _BUF_KEEP_TAIL_LINES + 1):
            head = self.buf[:_BUF_KEEP_HEAD_LINES]
            tail = self.buf[-_BUF_KEEP_TAIL_LINES:]
            kept = _lines_utf8_size(head + tail)
            marker = (
                f"\n... <{max(0, self._size - kept)} bytes truncated> ...\n"
            )
            candidate = head + [marker] + tail
            candidate_size = _lines_utf8_size(candidate)
            if candidate_size <= self._cap_bytes:
                self.buf = candidate
                self._size = candidate_size
                self._truncated = True
                self._scan_cursor = max(0, len(self.buf) - len(tail))
                return
        # Few-but-huge case, or many small lines with a configured cap
        # smaller than the head/tail line policy: keep a byte-bounded
        # head/tail snapshot plus a marker.
        full = "".join(self.buf)
        full_size = _utf8_size(full)
        if full_size <= self._cap_bytes:
            self._size = full_size
            return
        marker_reserve = 256 if self._cap_bytes > 512 else 128
        payload_cap = max(0, self._cap_bytes - marker_reserve)
        if self._cap_bytes > 1024 * 1024:
            payload_cap = min(
                payload_cap,
                max(0, (self._cap_bytes // 2) - marker_reserve),
            )
        head_cap = min(64 * 1024, payload_cap // 4)
        tail_cap = max(0, payload_cap - head_cap)
        head_blob = full[:head_cap]
        tail_blob = full[-tail_cap:] if tail_cap else ""
        head_blob = _take_utf8_prefix(full, head_cap)
        tail_blob = _take_utf8_suffix(full, tail_cap)
        omitted = max(
            0,
            full_size - _utf8_size(head_blob) - _utf8_size(tail_blob),
        )
        marker = (
            f"\n... <{omitted} bytes truncated "
            f"({len(self.buf)} line(s) total)> ...\n"
        )
        self.buf = [head_blob, marker, tail_blob]
        self._size = _lines_utf8_size(self.buf)
        self._truncated = True
        self._scan_cursor = max(0, len(self.buf) - 1)

    def start(self) -> None:
        self.thread.start()

    def join(self, timeout: float | None = None) -> None:
        self.thread.join(timeout=timeout)

    def text(self) -> str:
        with self.lock:
            return "".join(self.buf)

    def scan_new(
        self,
        patterns: Sequence[re.Pattern],
        halt_pattern_strs: "frozenset[str] | set[str]" = frozenset(),
    ) -> re.Match | None:
        """Search the unscanned portion of buf for the first ACTIONABLE
        pattern match. Advances cursor whether or not a match is found.
        A halt-pattern match on echoed repo/tool content is skipped (see
        `_first_actionable_match`)."""
        with self.lock:
            new_text = "".join(self._scan_buf)
            self._scan_buf = []
            self._scan_size = 0
            self._scan_cursor = len(self.buf)
        return _first_actionable_match(new_text, patterns, halt_pattern_strs)


class HealthGuard:
    def __init__(self, cwd: Path) -> None:
        self.cwd = Path(cwd)

    @staticmethod
    def _reap_orphans_if_pid1() -> int:
        """when peers runs as the
        container's PID 1 (default ENTRYPOINT in peers:dev), orphaned
        grandchildren of peer subprocesses — typically claude/codex's
        node helpers that called setsid() and outlived their parent —
        are re-parented to us. Python's default SIGCHLD is ignored, so
        these orphans become zombies that count against the container's
        pids cgroup (podman default 2048). After ~3-10 invocations the
        cgroup fills, and the NEXT pthread_create raises
        `RuntimeError: can't start new thread` — even though RLIMIT_NPROC
        is unlimited.

        Called at the END of invoke(), AFTER subprocess.Popen.wait() has
        reaped our direct child via _terminate_and_reap. So any
        remaining reapable PIDs are guaranteed orphans, not racing with
        subprocess's own bookkeeping. Returns the count for diagnostics.

        No-op outside container (os.getpid() != 1) — host init/systemd
        does this for us there."""
        if os.getpid() != 1:
            return 0
        reaped = 0
        while True:
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return reaped
            if pid == 0:
                return reaped
            reaped += 1

    @staticmethod
    def _sweep_zombies_via_proc(skip_pid: int,
                                proc_root: str = "/proc") -> int:
        """follow-up (2026-05-24): drains zombie direct-children
        WITHOUT racing the subprocess module's tracked PID. Used inside
        the invoke() poll loop where `_reap_orphans_if_pid1` cannot
        safely run — waitpid(-1) there would snipe the tracked peer's
        pid, causing Popen.wait() to synthesize a fake returncode=0 and
        mask real failures.

        Algorithm:
          1. Walk /proc/[pid]/stat for each numeric entry.
          2. Keep only Z-state, ppid==our_pid, pid != skip_pid.
          3. waitpid(pid, WNOHANG) each one — no race because the call
             targets a specific pid, not -1.

        No-op outside container (os.getpid() != 1)."""
        our_pid = os.getpid()
        if our_pid != 1:
            return 0
        reaped = 0
        try:
            entries = os.listdir(proc_root)
        except OSError:
            return 0
        for entry in entries:
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == skip_pid:
                continue
            try:
                with open(f"{proc_root}/{entry}/stat",
                          encoding="utf-8", errors="replace") as f:
                    data = f.read()
            except OSError:
                continue
            # /proc/[pid]/stat: "pid (comm) state ppid ..." — comm may
            # contain spaces/parens, so rfind ')' to anchor.
            rparen = data.rfind(")")
            if rparen < 0:
                continue
            rest = data[rparen + 2:].split(maxsplit=2)
            if len(rest) < 2:
                continue
            state = rest[0]
            try:
                ppid = int(rest[1])
            except ValueError:
                continue
            if state != "Z" or ppid != our_pid:
                continue
            try:
                os.waitpid(pid, os.WNOHANG)
                reaped += 1
            except (ChildProcessError, OSError):
                pass
        return reaped

    @staticmethod
    def _terminate_and_reap(proc: subprocess.Popen) -> None:
        # follow-up: with `start_new_session=True` the peer is
        # its own session leader (pgid == proc.pid), so claude/codex
        # node grandchildren that survive their parent are now in our
        # peer's pgroup, NOT the substrate's. Signal the whole group
        # to keep them from leaking; fall back to single-PID on
        # failure (ProcessLookupError = already gone, PermissionError
        # = should never happen for our own children but be safe).
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                proc.terminate()
            except ProcessLookupError:
                return
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            proc.wait()

    def invoke(
        self,
        argv: Sequence[str],
        prompt: str,
        idle_timeout_s: int = 15 * 60,
        absolute_max_runtime_s: int = 2 * 3600,
        prompt_mode: str = "stdin",
        error_patterns: Sequence[str] | None = None,
        halt_patterns: Sequence[str] | None = None,
        poll_interval_s: float = 0.25,
        buf_cap_bytes: int = _BUF_SOFT_CAP_BYTES,
        extra_env: dict[str, str] | None = None,
        tool: str | None = None,
    ) -> RunResult:
        if prompt_mode == "argv-substitute":
            effective_argv = [a.replace("{PROMPT}", prompt) for a in argv]
            send_stdin: str | None = None
            stdin_pipe = subprocess.DEVNULL
        elif prompt_mode == "stdin":
            effective_argv = list(argv)
            send_stdin = prompt
            stdin_pipe = subprocess.PIPE
        else:
            raise ValueError(f"unknown prompt_mode: {prompt_mode}")

        compiled_patterns = [re.compile(p) for p in (error_patterns or [])]
        # halt patterns are scanned BEFORE error_patterns so a
        # match against an AUTH/QUOTA shape wins over a transient one
        # (e.g., a stderr line that fits both gets the halt
        # classification, which is the safer side).
        compiled_halt_patterns = [
            re.compile(p) for p in (halt_patterns or [])
        ]
        # Combined list passed to the stream scanner. Halt patterns
        # come first so .scan_new (which returns on the first match)
        # prefers them when both classes could match the same line.
        combined_patterns = compiled_halt_patterns + compiled_patterns
        halt_pattern_strs = {p.pattern for p in compiled_halt_patterns}
        child_env = None
        if extra_env:
            child_env = {**os.environ, **extra_env}

        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(
                effective_argv,
                cwd=self.cwd,
                stdin=stdin_pipe,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                # isolate the peer into its own session so any
                # group-targeted signals from inside the peer's subtree
                # (e.g. `kill 0`, Node's broadcast-on-shutdown) cannot
                # reach the substrate. In container mode the substrate
                # is PID 1 and would otherwise share pgid=session=1
                # with the peer, so a stray pkill from claude-code's
                # node subprocess SIGTERMs PID 1.
                start_new_session=True,
                env=child_env,
            )
        except (FileNotFoundError, PermissionError, OSError) as e:
            return RunResult(
                classification="process-fail",
                exit_code=127 if isinstance(e, FileNotFoundError) else 126,
                stdout="",
                stderr=str(e),
                duration_ms=int((time.monotonic() - t0) * 1000),
            )

        # Default holders for the api-error surfacing fields; populated
        # below if a configured error_patterns regex matches.
        matched_pattern_re: str = ""
        matched_snippet: str = ""
        matched_source: str = ""
        config_error: str = ""

        # Start readers BEFORE writing stdin to avoid pipe-buffer deadlock.
        shared = {"last_output_t": time.monotonic()}
        shared_lock = threading.Lock()
        stdout_col = _StreamCollector(
            "out", proc.stdout, shared, shared_lock,
            buf_cap_bytes=buf_cap_bytes,
            scan_enabled=bool(combined_patterns),
        )
        stderr_col = _StreamCollector(
            "err", proc.stderr, shared, shared_lock,
            buf_cap_bytes=buf_cap_bytes,
            scan_enabled=bool(combined_patterns),
        )
        stdout_col.start()
        stderr_col.start()

        stdin_thread: threading.Thread | None = None
        if stdin_pipe is subprocess.PIPE and proc.stdin is not None:
            def _write_stdin() -> None:
                try:
                    if send_stdin is not None:
                        proc.stdin.write(
                            send_stdin.encode("utf-8", errors="replace")
                        )
                except BrokenPipeError:
                    pass
                except OSError:
                    pass
                try:
                    proc.stdin.close()
                except (BrokenPipeError, OSError, ValueError):
                    pass

            stdin_thread = threading.Thread(
                target=_write_stdin, name="hg-stdin", daemon=True,
            )
            stdin_thread.start()

        classification: str | None = None
        jsonl_liveness_fallbacks = 0
        # halt_required is sticky once set (a halt-class match
        # cannot be downgraded by a later transient match). The orchestrator
        # reads this off RunResult and translates into a peer-unavailable
        # exit_event.
        halt_required = False
        # follow-up (2026-05-24): inside the poll loop, drain
        # orphan zombies via /proc every _ZOMBIE_SWEEP_INTERVAL_S.
        # Without this, peer-spawned helpers (claude/codex node procs
        # that call setsid then exit, leaving grandchildren that
        # eventually exit themselves) accumulate as zombies against
        # the pids cgroup for the FULL duration of the peer's run
        # (minutes per tick). The end-of-invoke `_reap_orphans_if_pid1`
        # only catches them after we're already done — far too late
        # when claude bursts hundreds of helpers per tick.
        last_zombie_sweep = time.monotonic()
        try:
            while True:
                rc = proc.poll()

                if combined_patterns:
                    # Search each stream independently and incrementally,
                    # so a pattern hit anywhere in either stream fires.
                    # halt_patterns are at the front of `combined_patterns`
                    # so a line matching both classes binds to halt.
                    try:
                        match = stdout_col.scan_new(
                            combined_patterns, halt_pattern_strs
                        )
                        if match is None:
                            match = stderr_col.scan_new(
                                combined_patterns, halt_pattern_strs
                            )
                    except _PatternSearchTimeout as e:
                        classification = "process-fail"
                        matched_pattern_re = e.pattern
                        matched_snippet = (
                            "health.error_patterns regex timed out after "
                            f"{_PATTERN_SEARCH_TIMEOUT_S:.2f}s"
                        )
                        matched_source = "in-loop"
                        config_error = (
                            "healthguard: error pattern timed out; "
                            f"pattern={e.pattern!r}. Tighten or remove "
                            "this regex."
                        )
                        self._terminate_and_reap(proc)
                        rc = proc.returncode
                        break
                    if match is not None:
                        classification = "api-error"
                        matched_pattern_re = match.re.pattern
                        matched_snippet = match.group(0)[:200]
                        matched_source = "in-loop"
                        if matched_pattern_re in halt_pattern_strs:
                            halt_required = True
                        self._terminate_and_reap(proc)
                        rc = proc.returncode
                        break

                if rc is not None:
                    break

                with shared_lock:
                    last_output = shared["last_output_t"]
                now = time.monotonic()

                if (now - last_output) > idle_timeout_s:
                    jsonl_dir = claude_session_jsonl_path(self.cwd)
                    if jsonl_dir is not None and jsonl_mtime_within(
                        jsonl_dir,
                        within_seconds=_CLAUDE_JSONL_LIVENESS_WINDOW_S,
                    ):
                        jsonl_liveness_fallbacks += 1
                        with shared_lock:
                            shared["last_output_t"] = now
                        continue
                    classification = "idle-timeout"
                    self._terminate_and_reap(proc)
                    rc = proc.returncode
                    break

                if (now - t0) > absolute_max_runtime_s:
                    classification = "absolute-timeout"
                    self._terminate_and_reap(proc)
                    rc = proc.returncode
                    break

                if (now - last_zombie_sweep) >= _ZOMBIE_SWEEP_INTERVAL_S:
                    self._sweep_zombies_via_proc(skip_pid=proc.pid)
                    last_zombie_sweep = now

                time.sleep(poll_interval_s)
        except KeyboardInterrupt:
            self._terminate_and_reap(proc)
            raise

        # F1: reader threads need enough time to drain a noisy child
        # that just printed many MB to stderr before exiting. The
        # previous 2 s cap could truncate that drain mid-buffer. 30 s
        # is generous; with stdin EOF and child exit we expect EOF on
        # both pipes well before that. The buffer's per-stream cap
        # (2 MiB head/tail truncation) bounds memory.
        #
        # if a grandchild kept the
        # pipe write-end open, `os.read` would block past the 30s join
        # timeout and the daemon thread would leak (eventually NPROC).
        # request_stop() lets the reader exit at the next select-poll
        # (~_STOP_POLL_INTERVAL_S) even when EOF never arrives. We give
        # readers ~2s of grace to consume any pending bytes from a fast
        # exit, then signal stop, then join.
        stop_signal_delay_s = min(2.0, 30 / 15)
        stdout_col.thread.join(timeout=stop_signal_delay_s)
        stderr_col.thread.join(timeout=stop_signal_delay_s)
        stdout_col.request_stop()
        stderr_col.request_stop()
        stdout_col.join(timeout=30)
        stderr_col.join(timeout=30)
        # F3: also join the stdin writer so its FD doesn't leak past
        # invoke().
        if stdin_thread is not None:
            stdin_thread.join(timeout=5)

        # sweep orphan grandchildren now that our direct child is
        # reaped. No-op outside container. See _reap_orphans_if_pid1 for
        # the full rationale (pids-cgroup exhaustion via zombies).
        self._reap_orphans_if_pid1()

        stdout = stdout_col.text()
        stderr = stderr_col.text()

        # BUG-007 (2026-05-24): the in-loop scan reads incremental
        # `scan_buf` slices. If the child writes the pattern to a
        # stream and exits before the reader thread has appended the
        # bytes to scan_buf (race window: bytes still in the kernel
        # pipe), every `scan_new()` returns None and the loop breaks
        # on `rc is not None`. The reader-join below then drains the
        # pipe, so the bytes land in `buf` / `text()` — but
        # `classification` is still None and would fall through to
        # "success" for rc==0. Result: api-error patterns like
        # "Rate limit exceeded" silently misclassify as success and
        # the orchestrator retries instead of backing off. Final
        # rescan against the post-drain text closes the race; mirrors
        # the in-loop timeout handling so a bad regex can't bypass
        # this path either.
        if classification is None and combined_patterns:
            for col_text in (stdout, stderr):
                # halt_patterns are at the front of `combined_patterns`
                # — same priority order as the in-loop scan. Echoed-content
                # halt matches are skipped (mirrors the in-loop scan_new).
                try:
                    hit = _first_actionable_match(
                        col_text, combined_patterns, halt_pattern_strs
                    )
                except _PatternSearchTimeout as e:
                    classification = "process-fail"
                    matched_pattern_re = e.pattern
                    matched_snippet = (
                        "health.error_patterns regex timed out after "
                        f"{_PATTERN_SEARCH_TIMEOUT_S:.2f}s"
                    )
                    matched_source = "post-join"
                    config_error = (
                        "healthguard: error pattern timed out; "
                        f"pattern={e.pattern!r}. Tighten or remove "
                        "this regex."
                    )
                    break
                if hit is not None:
                    classification = "api-error"
                    matched_pattern_re = hit.re.pattern
                    matched_snippet = hit.group(0)[:200]
                    matched_source = "post-join"
                    if hit.re.pattern in halt_pattern_strs:
                        halt_required = True
                    break

        # Option C (v15 internal testing follow-up): structured halt classification.
        # When the peer tool exposes a structured status channel (claude's
        # stream-json result envelope), an unrecoverable auth/quota/usage-limit
        # error reported THERE halts the run — a signal a peer echoing repo
        # content cannot forge (echoes are text events, not the result
        # envelope). Additive: it only asserts a halt the regex path missed,
        # never downgrades an existing one.
        if tool and not halt_required:
            structured = classify_structured_halt(tool, stdout, stderr, rc)
            if structured is not None:
                matched_pattern_re, matched_snippet = structured
                matched_source = "structured"
                classification = "api-error"
                halt_required = True

        # Transient server rate-limit (429/5xx/overloaded) on the structured
        # channel: NOT a halt, NOT a hard process-fail. Classify `rate-limited`
        # so the loop backs off and retries the SAME peer without degrading it
        # (v17 internal testing operator finding: a transient 429 misclassified as
        # process-fail degraded the peer, then the turn manager benched it for
        # the rest of the run). Only fires when nothing else already classified
        # the run, so it never downgrades a real failure or a halt. (I5: this
        # runs AFTER the regex error_patterns scan; a broad un-anchored operator
        # error_pattern matching the JSON envelope line would classify
        # `api-error` first and pre-empt this. The default patterns are
        # ^...ERROR/FATAL-anchored and do not match an envelope line.)
        if tool and not halt_required and classification is None:
            transient = classify_structured_transient(tool, stdout, stderr, rc)
            if transient is not None:
                matched_pattern_re, matched_snippet = transient
                matched_source = "structured-transient"
                classification = "rate-limited"

        if config_error:
            stderr = (stderr + "\n" + config_error + "\n").lstrip()
        dur = int((time.monotonic() - t0) * 1000)

        if classification is None:
            classification = "success" if rc == 0 else "process-fail"

        return RunResult(
            classification=classification,
            exit_code=rc,
            stdout=stdout,
            stderr=stderr,
            duration_ms=dur,
            truncated=stdout_col._truncated or stderr_col._truncated,
            matched_error_pattern=matched_pattern_re,
            matched_error_snippet=matched_snippet,
            matched_error_source=matched_source,
            halt_required=halt_required,
            jsonl_liveness_fallback_used=jsonl_liveness_fallbacks > 0,
            jsonl_liveness_fallbacks=jsonl_liveness_fallbacks,
        )
