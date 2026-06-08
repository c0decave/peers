"""Tier-2: composite liveness signals for hang detection.

A peer that emits no stdout for a while may still be alive — waiting on a slow
API response (an ESTABLISHED socket with bytes in flight). This signal lets the
health guard distinguish 'thinking' from 'hung' so it can kill a genuine hang
faster (composite-AND with stdout cadence + session-jsonl mtime) without
false-killing a quiet-but-live peer.

Signals degrade safely: when unavailable they return ``None`` and the composite
ignores them — a signal can only ever PREVENT a kill (keep-alive), never cause
one.
"""
from __future__ import annotations

from pathlib import Path

# /proc/net/tcp connection state for ESTABLISHED (see include/net/tcp_states.h).
_TCP_ESTABLISHED = "01"

# x86-64 syscall numbers that mean "blocked waiting on I/O" (legitimately alive,
# not hung): receive/read family + the poll/epoll/select event-loop waits. A
# peer awaiting a slow API response sits in one of these (recvfrom for a Rust
# client; epoll_wait/ppoll for a node event loop), producing ZERO stdout / jsonl
# / socket-queue activity — the exact blind spot the other three signals share.
# On non-x86-64 arches these numbers differ; the signal then simply degrades
# (a thread won't match, so it does not assert liveness — never causes a kill).
_IO_WAIT_SYSCALLS = frozenset({
    0,    # read
    19,   # readv
    295,  # preadv
    45,   # recvfrom
    47,   # recvmsg
    299,  # recvmmsg
    7,    # poll
    271,  # ppoll
    23,   # select
    270,  # pselect6
    232,  # epoll_wait
    281,  # epoll_pwait
    441,  # epoll_pwait2
})
_MAX_TREE_DEPTH = 6


def _first_int(path: Path) -> int | None:
    try:
        return int(path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _thread_dir_alive(tdir: Path) -> bool:
    """True if the thread at ``tdir`` (a ``…/task/<tid>`` dir) is on-CPU (stat
    state R) or blocked in an I/O-wait syscall (awaiting a response, not hung)."""
    try:
        stat = (tdir / "stat").read_text()
        rparen = stat.rfind(")")
        if rparen != -1 and stat[rparen + 2:rparen + 3] == "R":
            return True
    except OSError:
        pass
    sc = _first_int(tdir / "syscall")
    return sc is not None and sc in _IO_WAIT_SYSCALLS


def _gather_thread_dirs(
    proc_root: Path, pid: int, depth: int = 0
) -> list[Path]:
    """All thread dirs (``…/task/<tid>``) in the process subtree rooted at
    ``pid`` (its threads + child processes, bounded depth)."""
    task_dir = proc_root / str(pid) / "task"
    try:
        tids = [p.name for p in task_dir.iterdir() if p.name.isdigit()]
    except OSError:
        return []
    out = [task_dir / t for t in tids]
    if depth < _MAX_TREE_DEPTH:
        for t in tids:
            try:
                kids = (task_dir / t / "children").read_text().split()
            except OSError:
                kids = []
            for k in kids:
                if k.isdigit():
                    out.extend(_gather_thread_dirs(proc_root, int(k), depth + 1))
    return out


def proc_state_alive(pid: int, proc_root: Path = Path("/proc")) -> bool | None:
    """Tier-2 C3/C5 4th signal. True iff ANY thread in the peer's process tree
    is on-CPU (running) or blocked in an I/O-wait syscall — i.e. the peer is
    working or legitimately awaiting a response, even when stdout, the session
    jsonl, and the socket queues are all silent (a model "thinking" before the
    first streamed token). False when the whole tree sits in non-I/O blocking
    states (e.g. futex) — a genuine-hang / deadlock candidate. None when /proc
    is unavailable for ``pid`` (signal unknown -> caller ignores it).

    Keep-alive only: like the other liveness signals it can PREVENT a kill,
    never cause one (False/None never force a kill on their own)."""
    if not (proc_root / str(pid)).exists():
        return None
    dirs = _gather_thread_dirs(proc_root, pid)
    if not dirs:
        return None
    for tdir in dirs:
        if _thread_dir_alive(tdir):
            return True
    return False


def socket_active(pid: int, proc_root: Path = Path("/proc")) -> bool | None:
    """True iff the process's network namespace has an ESTABLISHED TCP
    connection with bytes queued (data actively in flight) — i.e. the peer is
    transferring with the API right now. False if connections exist but are all
    idle. None if /proc is unavailable (signal unknown -> caller ignores it).

    Why queue-depth, not "any ESTABLISHED": in production a peer connects to the
    API *through* the local egress/auth-proxy chain, so its own ESTABLISHED
    sockets are the persistent keep-alive connections to that local proxy. Those
    stay ESTABLISHED even when no API call is in flight, so "any ESTABLISHED"
    would always be true and never let the composite hang-kill fire. Queued
    bytes on the proxy connection are the signal that a request/response is
    actively moving. The cost is a brief blind spot during a model's
    pre-first-token "thinking" gap (tx=rx=0); the composite hang-kill tolerates
    this because the session-jsonl mtime is the parallel keep-alive and
    absolute_max is the hard backstop.

    Namespace scope: ``/proc/<pid>/net/tcp`` is the pid's whole NETWORK
    NAMESPACE, not its individual sockets. This is correct only when the peer
    runs in its own netns (the containerized production path); a peer sharing
    the host netns would see host-wide connections. Callers that may run a peer
    in the host netns should treat this as advisory (keep-alive only).
    """
    found_any = False
    for fname in ("net/tcp", "net/tcp6"):
        path = proc_root / str(pid) / fname
        try:
            text = path.read_text()
        except (OSError, ValueError):
            continue
        found_any = True
        for line in text.splitlines()[1:]:  # skip the column header
            cols = line.split()
            if len(cols) < 5 or cols[3] != _TCP_ESTABLISHED:
                continue
            tx_rx = cols[4].split(":")
            if len(tx_rx) != 2:
                continue
            try:
                tx = int(tx_rx[0], 16)
                rx = int(tx_rx[1], 16)
            except ValueError:
                continue
            if tx > 0 or rx > 0:
                return True
    return False if found_any else None
