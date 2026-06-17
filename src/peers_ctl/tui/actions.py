"""Action layer: build exact ``python -m peers_ctl <verb>`` argv and shell the
real verbs.

No write logic is reimplemented here. Every mutation runs through the real
peers-ctl verb, which keeps its hash-chain / contract / symlink defenses
authoritative. Builders return list-arg argv only; ``run_verb`` invokes
subprocess WITHOUT ``shell=True`` and never string-interpolates a command.

Positional / flag names are pinned to ``peers_ctl.cli.build_parser``:
  - ``start`` / ``stop``  -> positional ``name``
  - ``resume`` / ``amend`` -> positional ``project_name``
  - ``ack-block``          -> positionals ``project_name`` then ``step_id``
  - ``new``                -> positional ``path``; ``--modes`` is a single
    comma-joined CSV value (NOT repeated flags)
"""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass

_BASE = [sys.executable, "-m", "peers_ctl"]

#: Conservative project-name charset. A project name is a short slug in
#: practice; anything outside ``[A-Za-z0-9._-]`` (and any leading ``-``, which
#: would look like a flag) is refused rather than shelled. projects.yaml is
#: host-written (not agent-writable), so this is belt-and-suspenders — it mirrors
#: the ``--end-of-options`` / ``_SHA_RE`` discipline used in ``commit_diff`` so a
#: flag-like name can never sneak into an argv built here.
_PROJECT_NAME_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]*\Z")


def _valid_project_name(name: object) -> bool:
    """True iff ``name`` is a safe project name: a non-empty string that does NOT
    start with ``-`` and contains only ``[A-Za-z0-9._-]``."""
    return isinstance(name, str) and bool(_PROJECT_NAME_RE.fullmatch(name))


def _base(config_dir: str | None) -> list[str]:
    """Base argv: ``python -m peers_ctl`` with an optional ``--config-dir`` that
    must precede the verb (it is a top-level parser option)."""
    argv = list(_BASE)
    if config_dir:
        argv += ["--config-dir", config_dir]
    return argv


# --------------------------------------------------------------------------- #
# Task 14: argv builders                                                       #
# --------------------------------------------------------------------------- #
def build_start_argv(
    project_name: str,
    *,
    max_ticks: int | None = None,
    max_usd: float | None = None,
    max_runtime: str | None = None,
    reset_budget: bool = False,
    container: bool = False,
    checkpoint: bool = False,
    config_dir: str | None = None,
) -> list[str]:
    """`start <name>` (+ optional budget / container / checkpoint flags).

    cli.py: positional is ``name``; flags ``--max-ticks --max-usd
    --max-runtime --reset-budget --container --checkpoint`` all exist.
    """
    argv = _base(config_dir) + ["start", project_name]
    if max_ticks is not None:
        argv += ["--max-ticks", str(max_ticks)]
    if max_usd is not None:
        argv += ["--max-usd", str(max_usd)]
    if max_runtime is not None:
        argv += ["--max-runtime", str(max_runtime)]
    if reset_budget:
        argv += ["--reset-budget"]
    if container:
        argv += ["--container"]
    if checkpoint:
        argv += ["--checkpoint"]
    return argv


def build_stop_argv(
    project_name: str,
    *,
    grace_s: float | None = None,
    config_dir: str | None = None,
) -> list[str]:
    """`stop <name>` (+ optional ``--grace-s``). cli.py positional is ``name``."""
    argv = _base(config_dir) + ["stop", project_name]
    if grace_s is not None:
        argv += ["--grace-s", str(grace_s)]
    return argv


def build_resume_argv(
    project_name: str,
    *,
    config_dir: str | None = None,
) -> list[str]:
    """`resume <project_name>`. cli.py positional is ``project_name``."""
    return _base(config_dir) + ["resume", project_name]


def build_peek_argv(
    name: str,
    *,
    session: str | None = None,
    config_dir: str | None = None,
) -> list[str] | None:
    """`peek <name>` (+ optional ``--session``) for the Live-Stream follow.

    cli.py: ``peek`` takes a positional ``name`` and an optional ``--session``.
    Returns the list-arg argv, or **None** when ``name`` fails validation
    (empty / leading ``-`` / any char outside ``[A-Za-z0-9._-]``) — the caller
    refuses to shell a flag-like name rather than risk argv injection.
    """
    if not _valid_project_name(name):
        return None
    argv = _base(config_dir) + ["peek", name]
    if session:
        argv += ["--session", str(session)]
    return argv


def build_ack_block_argv(
    *,
    project_name: str,
    step_id: str,
    reason: str,
    config_dir: str | None = None,
) -> list[str]:
    """`ack-block <project_name> <step_id> --reason <reason>`.

    cli.py: ack-block takes TWO positionals, ``project_name`` THEN ``step_id``,
    plus a required ``--reason``. ``project_name`` is therefore required here
    (it is a positional, not a registry/config-dir selection).
    """
    return _base(config_dir) + [
        "ack-block", project_name, step_id, "--reason", reason,
    ]


def build_amend_argv(
    *,
    project_name: str,
    acceptance: str,
    reason: str,
    config_dir: str | None = None,
) -> list[str]:
    """`amend <project_name> --acceptance <cmd> --reason <text>`.

    cli.py positional is ``project_name``; ``--acceptance`` + ``--reason`` are
    both required.
    """
    return _base(config_dir) + [
        "amend", project_name, "--acceptance", acceptance, "--reason", reason,
    ]


def build_new_argv(
    *,
    path: str,
    modes: list[str] | None = None,
    driver: str | None = None,
    container: bool = False,
    lang: str | None = None,
    plan: str | None = None,
    template: str | None = None,
    peer_model: str | None = None,
    peer_reasoning: str | None = None,
    peer_provider: str | None = None,
    config_dir: str | None = None,
) -> list[str]:
    """`new <path>` (+ optional scaffold flags).

    cli.py: positional is ``path``; ``--modes`` takes ONE comma-joined CSV
    value; ``--driver/--lang/--plan/--template/--peer-model/--peer-reasoning/
    --peer-provider`` are valued flags; ``--container`` is a bare flag.

    Note: the ``--peer-model/--peer-reasoning/--peer-provider`` CLI flags are
    ``action="append"`` (support multiple per-peer values), but this builder
    emits a single directive each in Wave 1a; if Plan 1b's wizard needs
    per-peer targeting, widen these params to ``list[str] | None``.
    """
    argv = _base(config_dir) + ["new", path]
    if modes:
        argv += ["--modes", ",".join(modes)]
    if driver:
        argv += ["--driver", driver]
    if container:
        argv += ["--container"]
    if lang:
        argv += ["--lang", lang]
    if plan:
        argv += ["--plan", plan]
    if template:
        argv += ["--template", template]
    if peer_model:
        argv += ["--peer-model", peer_model]
    if peer_reasoning:
        argv += ["--peer-reasoning", peer_reasoning]
    if peer_provider:
        argv += ["--peer-provider", peer_provider]
    return argv


# --------------------------------------------------------------------------- #
# Task 15: run_verb (no shell)                                                 #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class VerbResult:
    """Fail-soft result of a single ``peers-ctl`` verb invocation."""

    rc: int
    stdout: str
    stderr: str
    timed_out: bool = False


def _as_text(value: bytes | str | None) -> str:
    """Coerce a captured stream (str under text=True, but typed bytes|str|None on
    ``TimeoutExpired``) to a definite str — empty for None, utf-8 for bytes."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def run_verb(
    argv: list[str],
    *,
    cwd: str | None = None,
    timeout: float = 120.0,
) -> VerbResult:
    """Run a peers-ctl verb. list-arg argv only; never ``shell=True``.

    Captures rc / stdout / stderr. Fail-soft and total: this never raises for a
    spawn or timeout failure — it always returns a structured ``VerbResult``. On
    ``TimeoutExpired`` it returns ``rc=124, timed_out=True`` (so the UI can
    surface a hung verb without raising); on a spawn failure (bad ``argv[0]`` —
    missing interpreter/binary, or any ``OSError``) it returns ``rc=127``
    (the conventional "command not found" code), ``timed_out=False``.

    ``cwd`` plumbing (design §6.1): the launch wizard's ``new --plan`` runs an
    up-to-60s acceptance preflight in the target directory; the UI (Plan 1b)
    must call ``run_verb(build_new_argv(...), cwd=<target dir>)`` off the event
    loop with a progress indicator. The cwd plumbing lives here; the threading
    is Plan 1b. For the ``new --plan`` path the wizard should pass an explicit
    ``timeout`` of at least ~90s (cover the up-to-60s preflight plus scaffold
    overhead) rather than relying on the default.
    """
    try:
        cp = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        return VerbResult(
            rc=124,
            stdout=_as_text(e.stdout),
            stderr=_as_text(e.stderr),
            timed_out=True,
        )
    except (FileNotFoundError, OSError) as e:
        return VerbResult(rc=127, stdout="", stderr=str(e), timed_out=False)
    return VerbResult(rc=cp.returncode, stdout=cp.stdout, stderr=cp.stderr)


# --------------------------------------------------------------------------- #
# Task 16: doctor_preflight                                                    #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DoctorResult:
    """Structured result of ``peers-ctl doctor`` for gating host/container +
    start buttons. ``ok = (rc == 0)``; ``lines`` keep stdout+stderr for display.
    """

    ok: bool
    lines: list[str]
    rc: int


def doctor_preflight(config_dir: str | None = None) -> DoctorResult:
    """Run ``peers-ctl doctor`` and return a small structured result.

    The wizard uses ``ok`` to gate host-vs-container + start buttons. Lines are
    the combined stdout/stderr (display only). Fail-soft: a timeout surfaces as
    ``ok=False`` via the non-zero rc.
    """
    argv = _base(config_dir) + ["doctor"]
    res = run_verb(argv)
    combined = "\n".join(p for p in (res.stdout, res.stderr) if p)
    lines = combined.splitlines() if combined else []
    return DoctorResult(ok=(res.rc == 0), lines=lines, rc=res.rc)


# --------------------------------------------------------------------------- #
# Unit H: stream_verb — a long-lived, killable streaming subprocess           #
# --------------------------------------------------------------------------- #
#: how long ``close()`` waits for a graceful SIGTERM before SIGKILL-ing.
_STREAM_KILL_GRACE_S = 1.5


class StreamHandle:
    """A long-lived subprocess whose stdout is read incrementally + killable.

    Used by the Live-Stream window to follow ``peers-ctl peek <name>`` (claude)
    or a tailed per-tick stdout log (codex/opencode). Distinct from
    :func:`run_verb` (one-shot, captures everything): this one **streams** and is
    explicitly **killable** — the panel ``close()``s it when the window closes or
    the active run switches, which terminates the WHOLE process group so a
    followed ``tail``/``peek`` (and any child it spawned) cannot leak.

    Fail-soft: a spawn error (bad ``argv[0]`` / any ``OSError``) yields a handle
    that produces no lines and reports ``error``; it never raises. A background
    reader thread pumps stdout into a queue so reads are non-blocking and a
    line that never arrives can't wedge the UI thread.

    The process is launched in its own process group (``start_new_session=True``)
    so ``close()`` can signal the whole group, and ``stderr`` is merged into
    ``stdout`` so a spawned tool's error text is visible in the stream.
    """

    def __init__(self, argv: list[str], *, cwd: str | None = None) -> None:
        self._argv = list(argv)
        self._q: "queue.Queue[str | None]" = queue.Queue()
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._closed = False
        self._eof = False
        self.error: str | None = None
        try:
            # start_new_session=True -> the child leads a new process group, so
            # close() can kill the whole group (the followed tail + any child).
            self._proc = subprocess.Popen(  # noqa: S603 (list-arg, no shell)
                self._argv,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,  # line-buffered
                start_new_session=True,
            )
        except (FileNotFoundError, OSError) as e:
            self.error = str(e)
            self._eof = True
            self._q.put(None)  # immediate EOF sentinel
            return
        self._reader = threading.Thread(
            target=self._pump, name="stream_verb_reader", daemon=True,
        )
        self._reader.start()

    # -- background reader --------------------------------------------------- #
    def _pump(self) -> None:
        """Read stdout line-by-line into the queue until EOF; then a sentinel."""
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            for line in proc.stdout:
                self._q.put(line)
        except (OSError, ValueError):
            # stream closed under us (e.g. close() killed the proc): treat as EOF.
            pass
        finally:
            self._q.put(None)  # EOF sentinel

    # -- properties ---------------------------------------------------------- #
    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    def is_running(self) -> bool:
        """True iff the subprocess was spawned and has not yet exited."""
        return self._proc is not None and self._proc.poll() is None

    # -- incremental reads --------------------------------------------------- #
    def read_line(self, *, timeout: float | None = 0.0) -> str | None:
        """Return the next available stdout line, or ``None`` if none is ready.

        Non-blocking by default (``timeout=0.0``); pass a small ``timeout`` to
        wait briefly. Returns ``None`` both when no line is ready AND at EOF —
        check :meth:`is_running` / :attr:`error` to disambiguate. A line keeps
        its trailing newline (the caller strips/decodes)."""
        if self._eof and self._q.empty():
            return None
        try:
            item = self._q.get(timeout=timeout) if timeout else self._q.get_nowait()
        except queue.Empty:
            return None
        if item is None:
            self._eof = True
            return None
        return item

    def iter_lines(self):
        """Yield stdout lines until the process closes its stdout (EOF).

        Blocking — for tests / a worker thread, NOT the UI event loop. A
        never-spawned (failed) handle yields nothing immediately. Once EOF has
        been observed (by this method OR by ``read_line`` consuming the
        sentinel), this drains any already-queued lines and stops — it never
        blocks waiting for a sentinel that a prior ``read_line`` already took."""
        while True:
            if self._eof:
                # EOF already seen: drain what's buffered, then stop (don't
                # block on a sentinel a prior read_line may have consumed).
                try:
                    while True:
                        item = self._q.get_nowait()
                        if item is not None:
                            yield item
                except queue.Empty:
                    return
            item = self._q.get()
            if item is None:
                self._eof = True
                return
            yield item

    # -- teardown ------------------------------------------------------------ #
    def close(self) -> None:
        """Terminate the process group (graceful SIGTERM then SIGKILL). Idempotent.

        Safe on a never-spawned handle and on an already-exited process. Kills the
        whole session/group so a followed ``tail``/``peek`` child can't leak."""
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            self._signal_group(proc, signal.SIGTERM)
            try:
                proc.wait(timeout=_STREAM_KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                self._signal_group(proc, signal.SIGKILL)
                try:
                    proc.wait(timeout=_STREAM_KILL_GRACE_S)
                except subprocess.TimeoutExpired:
                    pass
        # close the pipe so the pump thread unblocks and exits.
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except OSError:
            pass
        if self._reader is not None:
            self._reader.join(timeout=_STREAM_KILL_GRACE_S)

    @staticmethod
    def _signal_group(proc: subprocess.Popen, sig: int) -> None:
        """Signal the child's whole process group, falling back to the child."""
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            # group gone / no perm: best-effort signal the child directly.
            try:
                proc.send_signal(sig)
            except (ProcessLookupError, OSError):
                pass

    # context-manager sugar so callers can `with stream_verb(...) as h:`.
    def __enter__(self) -> "StreamHandle":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def stream_verb(argv: list[str], *, cwd: str | None = None) -> StreamHandle:
    """Launch a long-lived streaming subprocess (list-arg, NO shell).

    Returns a :class:`StreamHandle` that yields stdout lines incrementally and
    kills the whole process group on :meth:`StreamHandle.close`. Fail-soft: a
    spawn error surfaces on ``handle.error`` and the handle simply yields nothing
    (it never raises). Use this for streaming ``peers-ctl peek``/``tail`` and for
    tailing a per-tick stdout log."""
    return StreamHandle(argv, cwd=cwd)


# --------------------------------------------------------------------------- #
# Unit H: Live-Stream line decoding                                           #
# --------------------------------------------------------------------------- #
#: One decoded stream row: ``(kind, text)`` where ``kind`` drives the panel
#: color. ``text`` = normal, ``tool`` = cyan, ``result``/``error`` = red,
#: ``raw`` = muted.
DecodedRow = tuple[str, str]

#: codex ``exec --json`` / opencode ``--format json`` event ``type`` values we
#: classify as errors (red). Anything else JSON falls through to a result/text
#: summary; a non-JSON line is rendered raw (muted).
_ERROR_EVENT_TYPES = {"error", "turn.failed", "stream_error", "fatal"}


def _truncate(text: str, limit: int = 160) -> str:
    text = str(text).replace("\n", " ").replace("\r", " ")
    return text if len(text) <= limit else text[: max(0, limit - 3)] + "..."


def _decode_claude_line(line: str) -> list[DecodedRow]:
    """Decode one claude session-jsonl line via ``peers.peek.decode_event``.

    Reuses the canonical decoder so the Live view matches ``peers-ctl peek``.
    The decoder yields strings prefixed ``TEXT:`` / ``TOOL:`` / ``RES:`` which we
    map to ``text`` / ``tool`` / ``result`` kinds. A non-JSON / non-dict line
    fails soft to a single ``raw`` row."""
    from peers.peek import decode_event

    try:
        ev = json.loads(line)
    except (ValueError, TypeError):
        return [("raw", line.strip())]
    if not isinstance(ev, dict):
        return [("raw", line.strip())]
    out: list[DecodedRow] = []
    for summary in decode_event(ev):
        # decode_event emits e.g. "10:00:00 assistant TEXT: ..." — classify on
        # the marker token it inserts (TEXT/TOOL/RES).
        if " TOOL:" in summary:
            out.append(("tool", summary))
        elif " RES:" in summary:
            # a tool_result row (red regardless: result/err share the alert
            # color per the design tokens — the err= flag is in the text).
            out.append(("result", summary))
        else:
            out.append(("text", summary))
    return out


def _decode_json_event_line(line: str) -> list[DecodedRow]:
    """Decode one codex/opencode JSON event line into a colored summary.

    codex ``exec --json`` and opencode ``--format json`` both emit one JSON
    object per line with a ``type`` discriminator. We surface a compact summary
    and classify error events red. A non-JSON line -> a single ``raw`` row."""
    try:
        ev = json.loads(line)
    except (ValueError, TypeError):
        return [("raw", line.strip())]
    if not isinstance(ev, dict):
        return [("raw", line.strip())]
    etype = str(ev.get("type", "") or "")
    # pick the most useful free-text field the event carries.
    detail = ""
    for key in ("message", "text", "content", "delta", "reason"):
        val = ev.get(key)
        if isinstance(val, str) and val:
            detail = val
            break
    summary = f"{etype} {detail}".strip() if etype else (detail or line.strip())
    if etype in _ERROR_EVENT_TYPES:
        return [("error", _truncate(summary))]
    if etype.startswith("turn.") or etype in ("result", "completed"):
        return [("result", _truncate(summary))]
    return [("text", _truncate(summary))]


def decode_stream_line(line: str, *, tool: str) -> list[DecodedRow]:
    """Decode one raw peer-stream line into colored ``(kind, text)`` rows.

    ``tool`` selects the decoder: ``claude`` reuses ``peers.peek.decode_event``
    (genuinely live via the session jsonl); ``codex``/``opencode`` parse the JSON
    event line. ``kind`` ∈ {text, tool, result, error, raw} drives the panel
    color (text=normal, tool=cyan, result/error=red, raw=muted).

    Fail-soft + total: a blank line yields no rows; any parse failure or an
    unknown tool yields a single ``raw`` row (never raises).

    Every ``raw`` row is bounded to the same length the JSON/claude summaries
    use (:func:`_truncate`): ``.peers`` logs are agent-writable, so a malformed
    / huge line must never reach the Live panel as one unbounded Label."""
    if line is None:
        return []
    stripped = line.strip()
    if not stripped:
        return []
    if tool == "claude":
        rows = _decode_claude_line(stripped)
    elif tool in ("codex", "opencode"):
        rows = _decode_json_event_line(stripped)
    else:
        # unknown tool: never guess a schema — render raw.
        rows = [("raw", stripped)]
    # Bound every raw fall-through row centrally (the other kinds already
    # truncate at construction). Idempotent for already-short rows.
    return [
        (kind, _truncate(text)) if kind == "raw" else (kind, text)
        for kind, text in rows
    ]
