"""Tier-2: composite liveness signals. The socket signal distinguishes a peer
waiting on a slow API (ESTABLISHED conn with bytes queued = alive) from a hung
one (no active transfer), and degrades safely to None when /proc is absent.
"""
from __future__ import annotations

from pathlib import Path

from peers.liveness import proc_state_alive, socket_active


def _write_tcp(proc: Path, pid: int, *rows: str, fname: str = "net/tcp") -> None:
    d = proc / str(pid) / "net"
    d.mkdir(parents=True, exist_ok=True)
    header = ("  sl  local_address rem_address   st tx_queue:rx_queue "
              "tr tm->when retrnsmt   uid\n")
    (proc / str(pid) / fname).write_text(header + "".join(r + "\n" for r in rows))


def test_socket_active_true_when_established_with_queue(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    # st=01 (ESTABLISHED), tx_queue=0000000A (>0) -> data in flight -> alive
    _write_tcp(proc, 123,
               "   0: 0100007F:1F90 0100007F:C9B2 01 0000000A:00000000 ...")
    assert socket_active(123, proc_root=proc) is True


def test_socket_active_false_when_established_idle(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    # ESTABLISHED but both queues empty -> idle keepalive, not active
    _write_tcp(proc, 123,
               "   0: 0100007F:1F90 0100007F:C9B2 01 00000000:00000000 ...")
    assert socket_active(123, proc_root=proc) is False


def test_socket_active_false_when_only_non_established(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    # st=06 (TIME_WAIT) with a queue -> not ESTABLISHED -> not active
    _write_tcp(proc, 123,
               "   0: 0100007F:1F90 0100007F:C9B2 06 0000000A:00000000 ...")
    assert socket_active(123, proc_root=proc) is False


def test_socket_active_edge_ignores_malformed_queue_rows(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    _write_tcp(
        proc,
        123,
        "   0: too-short",
        "   1: 0100007F:1F90 0100007F:C9B2 01 nothex:00000000 ...",
        "   2: 0100007F:1F90 0100007F:C9B2 01 0000000A ...",
    )
    assert socket_active(123, proc_root=proc) is False


def test_socket_active_none_when_proc_unavailable(tmp_path: Path) -> None:
    assert socket_active(999, proc_root=tmp_path / "nonexistent") is None


# --- proc_state_alive: the 4th hang-kill signal (process-state via procfs) ---
# Distinguishes "blocked awaiting a response / running" (ALIVE) from "stuck in a
# non-I/O wait like futex" (not alive). Empirically grounded: a peer blocked in
# recvfrom (syscall 45) or running (stat state R) is demonstrably live; one
# whose whole tree sits in futex_do_wait (202) is a deadlock candidate.

def _write_thread(proc: Path, pid: int, tid: int, syscall: str, state: str,
                  children: str = "") -> None:
    task = proc / str(pid) / "task" / str(tid)
    task.mkdir(parents=True, exist_ok=True)
    (task / "stat").write_text(f"{tid} (peer) {state} 1 1 0 0 -1 0\n")
    (task / "syscall").write_text(syscall + "\n")
    (task / "children").write_text(children)


def test_proc_state_alive_true_when_blocked_in_recvfrom(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    # syscall 45 = recvfrom -> awaiting network data -> ALIVE
    _write_thread(proc, 100, 100, "45 0x7f 0x400 0x0", "S")
    assert proc_state_alive(100, proc_root=proc) is True


def test_proc_state_alive_true_when_running(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    # state R (on CPU); syscall reports "running"
    _write_thread(proc, 100, 100, "running", "R")
    assert proc_state_alive(100, proc_root=proc) is True


def test_proc_state_alive_true_when_in_epoll_wait(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    # syscall 232 = epoll_wait -> event-loop I/O wait (node) -> keep-alive
    _write_thread(proc, 100, 100, "232 0x3 0x0", "S")
    assert proc_state_alive(100, proc_root=proc) is True


def test_proc_state_alive_false_when_whole_tree_in_futex(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    # syscall 202 = futex (lock wait) -> not I/O, not running -> deadlock cand.
    _write_thread(proc, 100, 100, "202 0x55 0x80", "S", children="101")
    _write_thread(proc, 100, 101, "202 0x55 0x80", "S")
    assert proc_state_alive(100, proc_root=proc) is False


def test_proc_state_alive_sad_false_for_corrupt_syscall(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    _write_thread(proc, 100, 100, "not-a-syscall", "S")
    assert proc_state_alive(100, proc_root=proc) is False


def test_proc_state_alive_true_via_child_thread(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    # parent idle in futex, but a CHILD process is blocked in recv -> tree alive
    _write_thread(proc, 100, 100, "202 0x55 0x80", "S", children="200")
    _write_thread(proc, 200, 200, "45 0x7f 0x400 0x0", "S")
    assert proc_state_alive(100, proc_root=proc) is True


def test_proc_state_alive_none_when_proc_absent(tmp_path: Path) -> None:
    assert proc_state_alive(999, proc_root=tmp_path / "nope") is None
