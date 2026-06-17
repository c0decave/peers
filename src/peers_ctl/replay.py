"""Item 12: peers-ctl replay — offline tick history review.

Reviewer-convenience: walk a project's `.peers/log/runs.jsonl` and
print per-tick detail so the operator can re-trace what the loop did
without re-running it. NO LLM calls, NO container starts, NO git
mutations. The only git invocation is an OPTIONAL read-only
`git -C <project> diff <head_before>..<head_after>` per tick when the
caller passes ``show_diffs=True``.

Public entry points:
    - ``replay_project(name, options) -> int`` — main entrypoint, returns
      a process-exit code (0 ok / 1 user-visible error / 2 input error).
    - ``register_subparser(sub)`` — attach the ``replay`` subparser to
      an ``argparse._SubParsersAction``. Caller is expected to dispatch
      ``args.cmd == "replay"`` to :func:`cmd_replay`.
    - ``cmd_replay(name, *, show_prompts, show_diffs, from_tick, to_tick,
      config_dir)`` — CLI-side adapter that builds :class:`ReplayOptions`
      from kwargs and prints to ``sys.stdout``.

Wiring note for cli.py integrator:
    Three other subagents are concurrently modifying cli.py, so this
    module deliberately avoids editing it. After their work lands,
    add to ``peers_ctl/cli.py``:

        from peers_ctl.replay import cmd_replay, register_subparser
        ...
        register_subparser(sub)
        ...
        if args.cmd == "replay":
            return cmd_replay(
                args.name,
                show_prompts=args.show_prompts,
                show_diffs=args.show_diffs,
                from_tick=args.from_tick,
                to_tick=args.to_tick,
                config_dir=cd,
            )

    Once that's in place, ``peers-ctl replay <name>`` works on the CLI.
    Until then, the module is reachable via ``replay_project`` from
    Python.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Iterable

from peers.safe_io import read_text_no_symlink, read_text_under_root_no_follow
from peers_ctl.store import Store, validate_project_name

# cap prompt-file reads so a runaway file (or a hostile prompt
# log) cannot wedge the operator's terminal. 4 MiB matches the substrate's
# prompt-budget envelope with plenty of headroom for trailing tool output.
_MAX_PROMPT_BYTES = 4 * 1024 * 1024

# cap the project-controlled runs.jsonl read so a malicious
# project cannot make replay disclose a same-user file via a planted
# symlink or consume unbounded memory via a giant log. 32 MB matches the
# compare command and is well above realistic tick volumes.
_MAX_RUNS_BYTES = 32 * 1024 * 1024

# head_before / head_after come from project-controlled
# runs.jsonl and were previously handed to `git diff <a>..<b>` verbatim.
# Reject anything that isn't a 4..64-char hex object id so option-like
# values like "--upload-pack=..." or ref strings like "refs/heads/main"
# never reach git's argv. SHA-256 git uses 64 hex chars; SHA-1 uses 40
# and abbreviated forms are at least 4. We deliberately do NOT resolve
# the ref — the goal is to keep replay read-only and side-effect free.
_HEX_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")
_RUNS_REL = (".peers", "log", "runs.jsonl")


def _root_and_rel_for_tail(
    path: Path, tail: tuple[str, ...],
) -> tuple[Path, tuple[str, ...]] | None:
    parts = Path(path).parts
    if len(parts) <= len(tail):
        return None
    if tuple(parts[-len(tail):]) != tail:
        return None
    root_parts = parts[:-len(tail)]
    if not root_parts:
        return None
    return Path(*root_parts), tail


def _open_dir_under_root_no_follow(root: Path, rel_parts: tuple[str, ...]) -> int:
    """Open ``root/rel_parts`` as a directory, refusing symlink components."""
    if not rel_parts:
        raise ValueError("rel_parts must include at least one directory")
    for name in rel_parts:
        if name in ("", ".", "..") or Path(name).name != name:
            raise ValueError(f"rel_parts must be plain components: {name!r}")
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    root_fd = os.open(str(root), flags)
    fds_to_close: list[int] = [root_fd]
    try:
        root_lst = root.lstat()
        root_st = os.fstat(root_fd)
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
            child_lst = os.stat(
                name, dir_fd=parent_fd, follow_symlinks=False,
            )
            if stat.S_ISLNK(child_lst.st_mode):
                raise OSError(f"refusing symlinked dir: {display}")
            if not stat.S_ISDIR(child_lst.st_mode):
                raise OSError(f"refusing non-directory: {display}")
            child_fd = os.open(name, flags, dir_fd=parent_fd)
            fds_to_close.append(child_fd)
            child_st = os.fstat(child_fd)
            if (child_st.st_dev, child_st.st_ino) != (
                child_lst.st_dev, child_lst.st_ino
            ):
                raise OSError(f"refusing swapped dir: {display}")
            parent_fd = child_fd
        return os.dup(parent_fd)
    finally:
        for fd in reversed(fds_to_close):
            try:
                os.close(fd)
            except OSError:
                pass


# --- options dataclass --------------------------------------------------

@dataclass
class ReplayOptions:
    """Knobs for :func:`replay_project`.

    Attributes:
        show_prompts: If True, look for ``.peers/log/prompts/iter-N/``
            and print whatever prompt files live there. Best-effort: a
            missing directory yields a short diagnostic, not an error.
        show_diffs: If True, run ``git -C <project> diff
            <head_before>..<head_after>`` per tick when both SHAs are
            present AND differ. Read-only; the diff is appended to the
            tick block.
        from_tick: Lower bound (inclusive) on the iteration number.
            None means "no lower bound".
        to_tick: Upper bound (inclusive) on the iteration number. None
            means "no upper bound".
        out: Output stream (typically ``sys.stdout``). Injected so tests
            can capture without monkeypatching stdout.
        config_dir: Optional alternate ``peers-ctl`` config dir; flows
            through to :class:`peers_ctl.store.Store` for project
            registry lookups.
    """
    show_prompts: bool = False
    show_diffs: bool = False
    from_tick: int | None = None
    to_tick: int | None = None
    out: IO[str] = field(default_factory=lambda: sys.stdout)
    config_dir: Path | None = None


# --- internals ----------------------------------------------------------

_BLOCK_SEPARATOR = "---"


def _resolve_project_dir(name: str, config_dir: Path | None) -> Path | None:
    """Look up a project's on-disk path.

    Tries the controller registry first (so projects added via
    ``peers-ctl add /some/path`` resolve to their explicit path), then
    falls back to ``$PEERS_PROJECTS_ROOT/<name>`` for bare-name
    projects scaffolded via ``peers-ctl new``.

    Returns None when neither lookup yields an existing directory —
    the caller is expected to emit a "no such project" diagnostic.
    """
    # Import lazily so the test environment can monkeypatch
    # `PEERS_PROJECTS_ROOT` after this module is imported.
    from peers_ctl.cli import projects_root

    try:
        store = Store(config_dir)
        project = store.get(name)
    except (OSError, ValueError):
        project = None
    if project is not None:
        p = Path(project.path)
        if p.is_dir():
            return p
    candidate = projects_root() / name
    if candidate.is_dir():
        return candidate
    return None


def _read_runs(path: Path) -> Iterable[dict]:
    """Yield JSON objects from a runs.jsonl file. Bad lines skipped.

    BUG-191/518: route project-shaped paths through a root-walking
    no-follow read with a byte cap so neither the leaf nor `.peers/log`
    ancestors can redirect replay outside the project or exhaust memory.
    """
    try:
        root_rel = _root_and_rel_for_tail(Path(path), _RUNS_REL)
        if root_rel is not None:
            root, rel_parts = root_rel
            # protect the `.peers` and `log` ancestors too.
            raw = read_text_under_root_no_follow(
                root, rel_parts, max_bytes=_MAX_RUNS_BYTES,
            )
        else:
            raw = read_text_no_symlink(path, max_bytes=_MAX_RUNS_BYTES)
    except (OSError, ValueError):
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            continue
        if isinstance(entry, dict):
            yield entry


def _runs_history_error(path: Path) -> str | None:
    """Return a user-facing reason when ``path`` is absent or unsafe.

    This mirrors ``_read_runs`` but reads zero bytes: replay_project needs to
    fail closed before rendering a successful-looking empty replay.
    """
    try:
        root_rel = _root_and_rel_for_tail(Path(path), _RUNS_REL)
        if root_rel is not None:
            root, rel_parts = root_rel
            read_text_under_root_no_follow(root, rel_parts, max_bytes=0)
        else:
            read_text_no_symlink(path, max_bytes=0)
    except FileNotFoundError:
        return f"missing {path}"
    except (OSError, ValueError) as e:
        return f"unsafe tick history at {path}: {e}"
    return None


def _format_duration(ms: object) -> str:
    """Render a duration in ms as a human-readable string.

    Keeps the raw ms value so existing tooling that greps for it still
    works, but appends a parenthetical seconds rounding for readability.
    """
    if isinstance(ms, bool):
        # bool is a subclass of int; rendering True as "1ms" surprises
        # readers more than rejecting it.
        return "-"
    if isinstance(ms, (int, float)) and ms >= 0:
        # int(inf) raises OverflowError; runs.jsonl produced with
        # allow_nan=True can contain Infinity literals.
        if isinstance(ms, float) and not math.isfinite(ms):
            return "-"
        if ms >= 1000:
            return f"{int(ms)}ms ({ms / 1000:.2f}s)"
        return f"{int(ms)}ms"
    return "-"


def _format_usd(v: object) -> str:
    """Format a USD float with 4 decimals; '-' for missing."""
    if isinstance(v, (int, float)):
        return f"${float(v):.4f}"
    return "-"


def _format_tokens(v: object) -> str:
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v))
    return "-"


def _format_sha_pair(before: object, after: object) -> str:
    """Render the head transition.

    Same-SHA pairs use ``aaa -> (unchanged)`` so the operator
    immediately spots no-handoff ticks. Missing values render as '-'.
    """
    b = str(before) if before else "-"
    a = str(after) if after else "-"
    if b != "-" and a != "-" and b == a:
        return f"{b} -> (unchanged)"
    return f"{b} -> {a}"


def _render_tick(entry: dict, out: IO[str]) -> None:
    """Print a single tick block. Caller is responsible for separators."""
    iteration = entry.get("iteration")
    peer = entry.get("peer", "?")
    classification = entry.get("classification", "?")
    duration = _format_duration(entry.get("duration_ms"))
    head_before = entry.get("head_before")
    head_after = entry.get("head_after")
    head_pair = _format_sha_pair(head_before, head_after)
    success = entry.get("success")
    soft_fail = entry.get("soft_fail_reason")
    tokens = _format_tokens(entry.get("tokens_this_tick"))
    usd = _format_usd(entry.get("usd_this_tick"))
    ts = entry.get("ts", "")
    peer_state_after = entry.get("peer_state_after", "")

    out.write(f"iteration: {iteration}\n")
    out.write(f"  peer: {peer}\n")
    out.write(f"  classification: {classification}\n")
    out.write(f"  duration: {duration}\n")
    out.write(f"  head: {head_pair}\n")
    out.write(f"  success: {success}\n")
    if soft_fail:
        out.write(f"  soft_fail_reason: {soft_fail}\n")
    out.write(f"  tokens: {tokens}\n")
    out.write(f"  usd: {usd}\n")
    if peer_state_after:
        out.write(f"  peer_state_after: {peer_state_after}\n")
    if ts:
        out.write(f"  ts: {ts}\n")


def _is_safe_hex_sha(value: object) -> bool:
    """BUG-179: only accept project-controlled head SHAs that look like
    a real git object id. We refuse on type/length/charset before any
    git invocation so an attacker-controlled runs.jsonl cannot steer
    the read-only replay path into option parsing or ref hunting.
    """
    return isinstance(value, str) and bool(_HEX_SHA_RE.match(value))


def _render_diff(proj_dir: Path, entry: dict, out: IO[str]) -> None:
    """Append a ``git diff`` for the tick's head transition.

    Skips ticks whose head_before == head_after (no-op handoff) and
    ticks with either SHA missing or not a valid hex object id. Failures are surfaced as a single notice line rather
    than raising — the goal is reviewer convenience, not
    bisect-quality correctness.
    """
    head_before = entry.get("head_before")
    head_after = entry.get("head_after")
    if not head_before or not head_after:
        out.write("  diff: (skipped — head_before/head_after missing)\n")
        return
    if head_before == head_after:
        out.write("  diff: (no change — heads identical)\n")
        return
    if not _is_safe_hex_sha(head_before) or not _is_safe_hex_sha(head_after):
        out.write(
            "  diff: (skipped — invalid head SHA in runs.jsonl; "
            "expected 4..64 hex chars)\n"
        )
        return
    # insert `--` so any future path arg cannot be parsed as
    # an option; revisions are already validated as hex above.
    argv = ["git", "-C", str(proj_dir), "diff",
            f"{head_before}..{head_after}", "--"]
    try:
        # 60s timeout: a single tick's diff should be near-instant
        # against the repo's pack; anything slower means the SHAs are
        # missing and git is hunting refs.
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        out.write(f"  diff: (git diff failed: {e})\n")
        return
    if result.returncode != 0:
        stderr_tail = (result.stderr or "").strip().splitlines()[-1:] or [""]
        out.write(
            f"  diff: (git diff exit={result.returncode}: {stderr_tail[0]})\n"
        )
        return
    out.write("  diff:\n")
    diff_text = result.stdout or "(empty diff)"
    for line in diff_text.splitlines():
        out.write(f"    {line}\n")


def _render_prompts(proj_dir: Path, iteration: object,
                    out: IO[str]) -> None:
    """Best-effort: look for ``.peers/log/prompts/iter-N/`` and print
    any text files inside. When the directory or files are absent,
    leave a short diagnostic.

    Format chosen so an operator can ``grep -A`` for prompt boundaries
    without parsing the structure.

    BUG-176: refuse symlinks. ``Path.is_dir()``, ``Path.iterdir()``,
    ``Path.is_file()`` and ``Path.read_text()`` all follow symlinks, so
    a peer-controlled link inside the prompts directory (or the directory
    itself) would leak any same-user-readable host file to the operator's
    replay output. We pre-check with ``os.lstat`` and skip with a
    diagnostic instead.
    """
    if not isinstance(iteration, int):
        out.write("  prompt: (skipped — iteration not an int)\n")
        return
    rel_dir = (".peers", "log", "prompts", f"iter-{iteration}")
    prompts_dir = proj_dir.joinpath(*rel_dir)
    try:
        dir_fd = _open_dir_under_root_no_follow(proj_dir, rel_dir)
    except FileNotFoundError:
        out.write(f"  prompt: (no prompt directory at {prompts_dir})\n")
        return
    except OSError as e:
        out.write(f"  prompt: (cannot list {prompts_dir}: {e})\n")
        return
    try:
        try:
            names = sorted(os.listdir(dir_fd))
        except OSError as e:
            out.write(f"  prompt: (cannot list {prompts_dir}: {e})\n")
            return
        files: list[str] = []
        for name in names:
            if Path(name).name != name:
                continue
            child = prompts_dir / name
            try:
                lst = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(lst.st_mode):
                out.write(
                    f"  prompt: (refusing symlinked prompt file: {child})\n"
                )
                continue
            if stat.S_ISREG(lst.st_mode):
                files.append(name)
        if not files:
            out.write(f"  prompt: (empty prompt directory at {prompts_dir})\n")
            return
        for name in files:
            out.write(f"  prompt ({name}):\n")
            try:
                text = read_text_under_root_no_follow(
                    proj_dir,
                    (*rel_dir, name),
                    max_bytes=_MAX_PROMPT_BYTES,
                )
            except (OSError, ValueError) as e:
                out.write(f"    (cannot read: {e})\n")
                continue
            for line in text.splitlines():
                out.write(f"    {line}\n")
    finally:
        os.close(dir_fd)


def _filter_by_range(entry: dict, lo: int | None,
                     hi: int | None) -> bool:
    """Return True iff the entry is a real tick within [lo, hi]."""
    iteration = entry.get("iteration")
    if not isinstance(iteration, int):
        return False
    if lo is not None and iteration < lo:
        return False
    if hi is not None and iteration > hi:
        return False
    return True


# --- public API ---------------------------------------------------------

def replay_project(name: str, options: ReplayOptions) -> int:
    """Walk a project's runs.jsonl and print per-tick detail.

    Returns:
        0 — at least one tick was rendered (or runs.jsonl was empty
            after range filtering but the file existed and the project
            resolved).
        1 — project does not exist OR runs.jsonl is missing.
        2 — invalid project name (path-traversal etc.).

    Output goes to ``options.out``. Errors are also written there so a
    test can assert the diagnostic text without splitting stdout/stderr.
    The caller (cli.py adapter) is expected to wire ``options.out =
    sys.stdout`` for the operator-facing path.
    """
    try:
        validate_project_name(name)
    except ValueError as e:
        options.out.write(f"peers-ctl replay: {e}\n")
        return 2

    proj_dir = _resolve_project_dir(name, options.config_dir)
    if proj_dir is None:
        options.out.write(f"peers-ctl replay: no such project: {name}\n")
        return 1

    runs_path = proj_dir / ".peers" / "log" / "runs.jsonl"
    history_error = _runs_history_error(runs_path)
    if history_error is not None:
        options.out.write(
            f"peers-ctl replay: no tick history "
            f"({history_error})\n"
        )
        return 1

    options.out.write(f"# peers-ctl replay: {name} ({proj_dir})\n")
    if options.from_tick is not None or options.to_tick is not None:
        options.out.write(
            f"# range: from_tick={options.from_tick} "
            f"to_tick={options.to_tick}\n"
        )
    options.out.write(_BLOCK_SEPARATOR + "\n")

    rendered_ticks = 0
    exit_event: dict | None = None
    for entry in _read_runs(runs_path):
        if entry.get("event") == "exit":
            # Stash for the trailing summary; runs.jsonl only ever
            # contains one of these per run, but stash the LAST one
            # we see to be safe (it's the most recent).
            exit_event = entry
            continue
        if not _filter_by_range(entry, options.from_tick, options.to_tick):
            continue
        _render_tick(entry, options.out)
        if options.show_diffs:
            _render_diff(proj_dir, entry, options.out)
        if options.show_prompts:
            _render_prompts(proj_dir, entry.get("iteration"), options.out)
        options.out.write(_BLOCK_SEPARATOR + "\n")
        rendered_ticks += 1

    if exit_event is not None:
        reason = exit_event.get("reason", "?")
        ticks = exit_event.get("ticks_in_run", "?")
        ts = exit_event.get("ts", "")
        options.out.write(
            f"# exit: reason={reason} ticks_in_run={ticks} ts={ts}\n"
        )

    if rendered_ticks == 0:
        options.out.write(
            "# (no ticks in the selected range)\n"
        )
    return 0


# --- CLI adapter --------------------------------------------------------

def cmd_replay(name: str, *, show_prompts: bool = False,
               show_diffs: bool = False,
               from_tick: int | None = None,
               to_tick: int | None = None,
               config_dir: Path | None = None) -> int:
    """Thin adapter for cli.py dispatch. Writes to ``sys.stdout``."""
    opts = ReplayOptions(
        show_prompts=show_prompts,
        show_diffs=show_diffs,
        from_tick=from_tick,
        to_tick=to_tick,
        out=sys.stdout,
        config_dir=config_dir,
    )
    return replay_project(name, opts)


def register_subparser(sub: "argparse._SubParsersAction") -> None:
    """Attach the ``replay`` subparser. cli.py should call this from
    inside ``main()`` after the ``sub`` object is created. See the
    module docstring for the dispatch snippet."""
    p = sub.add_parser(
        "replay",
        help=("walk a project's tick history offline — no LLM, no "
              "containers, no git mutations"),
        description=(
            "Re-trace a project's runs.jsonl without re-running the "
            "loop. Reviewer convenience for post-mortems and audits."
        ),
    )
    p.add_argument("name",
                   help="project name (bare name under "
                        "$PEERS_PROJECTS_ROOT or registered via "
                        "`peers-ctl add`)")
    p.add_argument("--show-prompts", action="store_true",
                   help="dump prompt files from "
                        ".peers/log/prompts/iter-N/ when present")
    p.add_argument("--show-diffs", action="store_true",
                   help="run `git diff <head_before>..<head_after>` "
                        "for each tick (read-only)")
    p.add_argument("--from-tick", type=int, default=None,
                   metavar="N",
                   help="lower-bound iteration (inclusive)")
    p.add_argument("--to-tick", type=int, default=None,
                   metavar="M",
                   help="upper-bound iteration (inclusive)")
