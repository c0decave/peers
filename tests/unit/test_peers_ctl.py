"""peers-ctl multi-project controller.

Coverage:
- Store: add / remove / list / get / log_path_for, registry persistence
  under XDG_CONFIG_HOME-style override.
- Runner: start with stub `peers` binary, stop sends SIGTERM, crashed
  processes are reconciled to `crashed`.
- CLI dispatch: each subcommand wired correctly.
"""
from __future__ import annotations

import os
import signal
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from peers_ctl.cli import (
    cmd_add, cmd_list, cmd_remove, cmd_start, cmd_status, cmd_stop,
    cmd_logs, cmd_report,
)
from peers_ctl.runner import start_project
from peers_ctl.store import (
    Project, Store, is_pid_alive, prune_logs, reconcile,
)


# --- Store --------------------------------------------------------------

def test_store_add_and_list(tmp_path: Path):
    s = Store(tmp_path / "ctl")
    s.add(Project(name="snake", path=str(tmp_path / "snake")))
    projects = s.list_projects()
    assert [p.name for p in projects] == ["snake"]
    # newly-added projects start in "fresh" state — they
    # have never been started, so "stopped" was misleading.
    assert projects[0].state == "fresh"
    assert s.log_path_for("snake").is_file()
    assert s.log_path_for("snake").read_text() == ""


def test_store_rejects_duplicate(tmp_path: Path):
    s = Store(tmp_path / "ctl")
    s.add(Project(name="x", path=str(tmp_path / "a")))
    with pytest.raises(ValueError):
        s.add(Project(name="x", path=str(tmp_path / "b")))


def test_store_rejects_path_traversal_project_name(tmp_path: Path):
    s = Store(tmp_path / "ctl")
    with pytest.raises(ValueError, match="invalid project name"):
        s.add(Project(name="../../escape", path=str(tmp_path / "p")))


def test_store_remove_running_project_rejected(tmp_path: Path):
    s = Store(tmp_path / "ctl")
    s.add(Project(name="x", path=str(tmp_path / "a")))
    s.update("x", state="running", pid=999999)
    with pytest.raises(ValueError, match="still running"):
        s.remove("x")


def test_store_persistence(tmp_path: Path):
    cfg = tmp_path / "ctl"
    s = Store(cfg)
    s.add(Project(name="x", path=str(tmp_path / "p")))
    s2 = Store(cfg)
    assert [p.name for p in s2.list_projects()] == ["x"]


def test_store_refuses_symlinked_registry(tmp_path: Path):
    cfg = tmp_path / "ctl"
    cfg.mkdir()
    bait = tmp_path / "projects.yaml"
    bait.write_text("projects: []\n")
    (cfg / "projects.yaml").symlink_to(bait)
    s = Store(cfg)

    with pytest.raises(RuntimeError, match="unreadable or unsafe"):
        s.list_projects()


def test_store_refuses_symlinked_logs_dir_on_init(tmp_path: Path):
    cfg = tmp_path / "ctl"
    cfg.mkdir()
    outside = tmp_path / "outside-logs"
    outside.mkdir()
    (cfg / "logs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="symlinked logs dir"):
        Store(cfg)


def test_safe_log_path_refuses_late_logs_dir_symlink(tmp_path: Path):
    cfg = tmp_path / "ctl"
    s = Store(cfg)
    shutil.rmtree(cfg / "logs")
    outside = tmp_path / "outside-logs"
    outside.mkdir()
    (cfg / "logs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlinked logs dir"):
        s.safe_log_path_for(Project(name="x", path=str(tmp_path / "p")))


def test_log_path_for_uses_config_dir(tmp_path: Path):
    s = Store(tmp_path / "ctl")
    assert s.log_path_for("snake") == tmp_path / "ctl" / "logs" / "snake.log"


def test_log_path_for_rejects_unsafe_name(tmp_path: Path):
    s = Store(tmp_path / "ctl")
    with pytest.raises(ValueError, match="invalid project name"):
        s.log_path_for("../snake")


def test_store_add_refuses_symlinked_default_log_leaf(tmp_path: Path):
    cfg = tmp_path / "ctl"
    s = Store(cfg)
    bait = tmp_path / "bait.log"
    bait.write_text("keep me")
    s.log_path_for("snake").symlink_to(bait)

    with pytest.raises(ValueError, match="symlinked log_path"):
        s.add(Project(name="snake", path=str(tmp_path / "snake")))

    assert bait.read_text() == "keep me"
    assert s.list_projects() == []


def test_corrupt_registry_raises(tmp_path: Path):
    cfg = tmp_path / "ctl"
    cfg.mkdir()
    (cfg / "projects.yaml").write_text("not valid: :: yaml :\n: : :")
    with pytest.raises(RuntimeError, match="corrupt"):
        Store(cfg).list_projects()


def test_oversized_registry_raises(tmp_path: Path):
    from peers_ctl.store import _PROJECTS_REGISTRY_MAX_BYTES

    cfg = tmp_path / "ctl"
    s = Store(cfg)
    s.path.write_bytes(b"x" * (_PROJECTS_REGISTRY_MAX_BYTES + 1))

    with pytest.raises(RuntimeError, match="registry too large"):
        s.list_projects()


def test_registry_at_size_cap_loads(tmp_path: Path):
    from peers_ctl.store import _PROJECTS_REGISTRY_MAX_BYTES

    cfg = tmp_path / "ctl"
    s = Store(cfg)
    base = b"projects: []\n"
    filler = b"#" + b"x" * (_PROJECTS_REGISTRY_MAX_BYTES - len(base) - 1)
    s.path.write_bytes(base + filler)

    assert s.list_projects() == []


# --- Reconcile ----------------------------------------------------------

def test_reconcile_marks_dead_pid_as_crashed(tmp_path: Path):
    s = Store(tmp_path / "ctl")
    s.add(Project(name="x", path=str(tmp_path / "p")))
    # Use a PID that's guaranteed not to exist.
    s.update("x", state="running", pid=0x7fffffff)
    reconcile(s)
    p = s.get("x")
    assert p.state == "crashed"
    assert p.pid is None


def test_reconcile_clean_self_termination_marks_stopped(tmp_path: Path):
    """step 2a: when the inner orchestrator self-terminates
    cleanly (convergence, max_ticks, budget exhausted) it writes a
    sentinel `.peers/last-stop-reason.txt`. Reconcile reads this and
    distinguishes clean stop from real crash. Observed in v6/v7: both
    runs `Stopped: complete` but peers-ctl listed them as `crashed`."""
    s = Store(tmp_path / "ctl")
    target = tmp_path / "p"
    (target / ".peers").mkdir(parents=True)
    (target / ".peers" / "last-stop-reason.txt").write_text(
        "complete 2026-05-25T17:00:00+00:00\n",
    )
    s.add(Project(name="x", path=str(target)))
    s.update("x", state="running", pid=0x7fffffff)
    reconcile(s)
    p = s.get("x")
    assert p.state == "stopped", (
        f"clean self-termination should mark stopped, got {p.state}"
    )
    assert p.pid is None


def test_reconcile_max_ticks_termination_marks_stopped(tmp_path: Path):
    s = Store(tmp_path / "ctl")
    target = tmp_path / "p"
    (target / ".peers").mkdir(parents=True)
    (target / ".peers" / "last-stop-reason.txt").write_text(
        "max_ticks 2026-05-25T17:00:00+00:00\n",
    )
    s.add(Project(name="x", path=str(target)))
    s.update("x", state="running", pid=0x7fffffff)
    reconcile(s)
    p = s.get("x")
    assert p.state == "stopped"


def test_reconcile_budget_exhausted_marks_stopped(tmp_path: Path):
    s = Store(tmp_path / "ctl")
    target = tmp_path / "p"
    (target / ".peers").mkdir(parents=True)
    (target / ".peers" / "last-stop-reason.txt").write_text(
        "budget:max_runtime 2026-05-25T17:00:00+00:00\n",
    )
    s.add(Project(name="x", path=str(target)))
    s.update("x", state="running", pid=0x7fffffff)
    reconcile(s)
    p = s.get("x")
    assert p.state == "stopped"


def test_reconcile_peer_unavailable_marks_crashed(tmp_path: Path):
    """A halt-class exit (peer-unavailable, goal-mutation) is operator
    action required — keep as crashed so the operator notices."""
    s = Store(tmp_path / "ctl")
    target = tmp_path / "p"
    (target / ".peers").mkdir(parents=True)
    (target / ".peers" / "last-stop-reason.txt").write_text(
        "peer-unavailable:codex 2026-05-25T17:00:00+00:00\n",
    )
    s.add(Project(name="x", path=str(target)))
    s.update("x", state="running", pid=0x7fffffff)
    reconcile(s)
    p = s.get("x")
    assert p.state == "crashed"


def test_reconcile_no_sentinel_marks_crashed(tmp_path: Path):
    """When the process dies hard without writing a sentinel (segfault,
    SIGKILL, OOM), reconcile still falls back to crashed. Preserves the
    pre-Phase-V behavior for actual crashes."""
    s = Store(tmp_path / "ctl")
    target = tmp_path / "p"
    (target / ".peers").mkdir(parents=True)
    s.add(Project(name="x", path=str(target)))
    s.update("x", state="running", pid=0x7fffffff)
    reconcile(s)
    p = s.get("x")
    assert p.state == "crashed"


def test_reconcile_unrecognized_sentinel_reason_marks_crashed(
    tmp_path: Path,
) -> None:
    """An unknown reason in the sentinel file is treated as crash —
    fail-CLOSED rather than silently passing as stopped."""
    s = Store(tmp_path / "ctl")
    target = tmp_path / "p"
    (target / ".peers").mkdir(parents=True)
    (target / ".peers" / "last-stop-reason.txt").write_text(
        "wat-is-das 2026-05-25T17:00:00+00:00\n",
    )
    s.add(Project(name="x", path=str(target)))
    s.update("x", state="running", pid=0x7fffffff)
    reconcile(s)
    p = s.get("x")
    assert p.state == "crashed"


# --- Reconcile (container mode, probe-failure / symmetric) -----

def _make_container_project(
    store: Store, name: str, target: Path, *, state: str,
    container_name: str | None = None,
) -> Project:
    """Helper: create a project record marked as container mode."""
    cname = container_name or f"peers-ctl_{name}"
    target.mkdir(parents=True, exist_ok=True)
    (target / ".peers").mkdir(exist_ok=True)
    store.add(Project(
        name=name, path=str(target),
        notes=f"container=1 container_name={cname} container_id=deadbeef",
    ))
    store.update(name, state=state, pid=None)
    return store.get(name)


def _patch_podman_probe(monkeypatch, *, behavior):
    """Replace subprocess.run inside store.reconcile() probe.

    `behavior` is a callable receiving the argv list and returning either:
    - subprocess.CompletedProcess (normal return)
    - raises TimeoutExpired / FileNotFoundError (failure modes)
    """
    real_run = subprocess.run

    def fake_run(argv, *args, **kwargs):
        if isinstance(argv, list) and len(argv) >= 2 and argv[1] == "ps":
            return behavior(argv)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_reconcile_probe_timeout_does_not_false_positive_crash(
    tmp_path: Path, monkeypatch,
) -> None:
    """A transient podman-ps timeout must NOT falsely mark a running
    container as crashed. Observed in v8: container ran continuously
    for 2h, but a single failed probe flipped state to crashed and it
    was never recovered (see in HANDOFF)."""
    s = Store(tmp_path / "ctl")
    _make_container_project(
        s, "v8like", tmp_path / "v8", state="running",
    )

    def boom(argv):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=10)

    _patch_podman_probe(monkeypatch, behavior=boom)
    reconcile(s)
    p = s.get("v8like")
    assert p.state == "unknown", (
        f"transient probe failure should yield 'unknown', not "
        f"{p.state!r}"
    )


def test_reconcile_probe_filenotfound_does_not_false_positive_crash(
    tmp_path: Path, monkeypatch,
) -> None:
    """If podman binary briefly isn't on PATH, that's not evidence
    of container death."""
    s = Store(tmp_path / "ctl")
    _make_container_project(
        s, "p", tmp_path / "p", state="running",
    )

    def missing(argv):
        raise FileNotFoundError(2, "no such file", argv[0])

    _patch_podman_probe(monkeypatch, behavior=missing)
    reconcile(s)
    p = s.get("p")
    assert p.state == "unknown"


def test_reconcile_recovers_crashed_when_container_alive(
    tmp_path: Path, monkeypatch,
) -> None:
    """If a project is marked crashed but the container is actually
    running, reconcile must flip it back to running. Without this,
    a single false-positive crash (Patch A scenario) sticks forever."""
    s = Store(tmp_path / "ctl")
    _make_container_project(
        s, "v8like", tmp_path / "v8", state="crashed",
        container_name="peers-ctl_v8like",
    )

    def alive(argv):
        return subprocess.CompletedProcess(
            argv, 0, stdout="peers-ctl_v8like\n", stderr="",
        )

    _patch_podman_probe(monkeypatch, behavior=alive)
    reconcile(s)
    p = s.get("v8like")
    assert p.state == "running"


def test_reconcile_recovers_unknown_when_container_alive(
    tmp_path: Path, monkeypatch,
) -> None:
    """unknown is the natural state after a transient probe failure;
    next reconcile with a successful probe must resolve it."""
    s = Store(tmp_path / "ctl")
    _make_container_project(
        s, "p", tmp_path / "p", state="unknown",
        container_name="peers-ctl_p",
    )

    def alive(argv):
        return subprocess.CompletedProcess(
            argv, 0, stdout="peers-ctl_p\n", stderr="",
        )

    _patch_podman_probe(monkeypatch, behavior=alive)
    reconcile(s)
    p = s.get("p")
    assert p.state == "running"


def test_reconcile_stopped_terminal_state_not_rewritten(
    tmp_path: Path, monkeypatch,
) -> None:
    """REGRESSION GUARD: A project already in terminal state
    `stopped` with a dead container must NOT be flipped to `crashed`
    just because the sentinel file is gone, and its `last_stopped_at`
    must NOT be bumped to 'now'. Terminal states are sticky."""
    s = Store(tmp_path / "ctl")
    target = tmp_path / "v3"
    _make_container_project(
        s, "v3like", target, state="stopped",
        container_name="peers-ctl_v3like",
    )
    # Pretend it was stopped two days ago and the sentinel is long gone
    original_stop = "2026-05-24T17:41:59.825499+00:00"
    s.update("v3like", last_stopped_at=original_stop)

    def gone(argv):
        return subprocess.CompletedProcess(
            argv, 0, stdout="", stderr="",
        )

    _patch_podman_probe(monkeypatch, behavior=gone)
    reconcile(s)
    p = s.get("v3like")
    assert p.state == "stopped", (
        f"stopped + dead container must stay stopped, got {p.state!r}"
    )
    assert p.last_stopped_at == original_stop, (
        "reconcile must not bump last_stopped_at on a terminal state"
    )


def test_reconcile_crashed_terminal_state_not_rewritten(
    tmp_path: Path, monkeypatch,
) -> None:
    """Counterpart of the stopped-sticky test: an old `crashed`
    project's `last_stopped_at` must not be churned by every dashboard
    refresh. Terminal == sticky for both crashed and stopped."""
    s = Store(tmp_path / "ctl")
    _make_container_project(
        s, "v2like", tmp_path / "v2", state="crashed",
        container_name="peers-ctl_v2like",
    )
    original_stop = "2026-05-23T20:50:42.296287+00:00"
    s.update("v2like", last_stopped_at=original_stop)

    def gone(argv):
        return subprocess.CompletedProcess(
            argv, 0, stdout="", stderr="",
        )

    _patch_podman_probe(monkeypatch, behavior=gone)
    reconcile(s)
    p = s.get("v2like")
    assert p.state == "crashed"
    assert p.last_stopped_at == original_stop


def test_reconcile_unknown_with_dead_container_marks_crashed(
    tmp_path: Path, monkeypatch,
) -> None:
    """If state is unknown and the container is now confirmed dead
    without a clean-stop sentinel, mark crashed (standard semantics)."""
    s = Store(tmp_path / "ctl")
    _make_container_project(
        s, "p", tmp_path / "p", state="unknown",
        container_name="peers-ctl_p",
    )

    def empty(argv):
        return subprocess.CompletedProcess(
            argv, 0, stdout="", stderr="",
        )

    _patch_podman_probe(monkeypatch, behavior=empty)
    reconcile(s)
    p = s.get("p")
    assert p.state == "crashed"


def test_reconcile_does_not_touch_fresh_projects(
    tmp_path: Path, monkeypatch,
) -> None:
    """A 'fresh' project (registered, never started) must stay fresh
    regardless of probe outcome — no PID/container to probe yet."""
    s = Store(tmp_path / "ctl")
    s.add(Project(name="brand-new", path=str(tmp_path / "bn")))
    # default state is fresh
    assert s.get("brand-new").state == "fresh"

    def boom(argv):  # would normally false-positive crash
        raise subprocess.TimeoutExpired(cmd=argv, timeout=10)

    _patch_podman_probe(monkeypatch, behavior=boom)
    reconcile(s)
    assert s.get("brand-new").state == "fresh"


def test_unknown_is_valid_project_state(tmp_path: Path) -> None:
    """Persisting/loading a project with state='unknown' must round-trip
    cleanly; the registry should not reject or silently drop it."""
    s = Store(tmp_path / "ctl")
    s.add(Project(name="p", path=str(tmp_path / "p")))
    s.update("p", state="unknown")
    # Force reload from disk
    s2 = Store(tmp_path / "ctl")
    p = s2.get("p")
    assert p is not None and p.state == "unknown"


def test_is_pid_alive_self():
    assert is_pid_alive(os.getpid())


def test_is_pid_alive_zero_false():
    assert not is_pid_alive(0)
    assert not is_pid_alive(None)


def test_is_pid_alive_non_int_false():
    assert not is_pid_alive("123")  # type: ignore[arg-type]


# --- Runner -------------------------------------------------------------

def _peers_stub(tmp_path: Path, sleep_s: float = 5.0) -> Path:
    """Create a fake `peers` shim that sleeps for sleep_s then exits 0."""
    stub = tmp_path / "peers_stub.sh"
    stub.write_text(f"#!/bin/sh\nsleep {sleep_s}\n")
    stub.chmod(0o755)
    return stub


def _stub_target(tmp_path: Path) -> Path:
    """A directory that LOOKS like a peers target (.peers/config.yaml
    present), enough to pass the runner's pre-flight check."""
    target = tmp_path / "target"
    (target / ".peers").mkdir(parents=True)
    (target / ".peers" / "config.yaml").write_text("driver: orchestrator\n")
    return target


def test_start_and_stop_project(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "ctl"
    s = Store(cfg)
    target = _stub_target(tmp_path)
    stub = _peers_stub(tmp_path, sleep_s=30)
    monkeypatch.setenv("PEERS_CTL_PEERS_BIN", str(stub))
    # Re-import runner so it picks up the env var.
    import importlib
    import peers_ctl.runner as runner_mod
    importlib.reload(runner_mod)

    proj = Project(name="snake", path=str(target))
    s.add(proj)
    pid = runner_mod.start_project(s, s.get("snake"))
    assert pid > 0
    assert is_pid_alive(pid)

    p_after = s.get("snake")
    assert p_after.state == "running"
    assert p_after.pid == pid

    runner_mod.stop_project(s, s.get("snake"), grace_s=2)
    # After stop, the project's state must be `stopped`.
    p_after = s.get("snake")
    assert p_after.state == "stopped"
    assert p_after.pid is None
    # The runner reaps zombies, so within a brief moment the PID
    # must no longer be alive from our POV.
    for _ in range(10):
        if not is_pid_alive(pid):
            break
        time.sleep(0.1)
    assert not is_pid_alive(pid), \
        f"pid {pid} still alive after stop_project + reap"


def test_stop_project_returns_promptly_when_child_dies_immediately(
    tmp_path: Path, monkeypatch,
):
    """BUG-134: stop_project must not waste the full grace_s polling a
    zombie. The peers stub here exits within ~5ms of SIGTERM (sh+sleep
    under killpg), so a 2s grace budget must NOT translate into 2s of
    wall-clock — we expect ≤1s, leaving the first 1s as the fix's
    correct-behavior window and the next 1s as the bug's waste window.
    """
    cfg = tmp_path / "ctl"
    s = Store(cfg)
    target = _stub_target(tmp_path)
    stub = _peers_stub(tmp_path, sleep_s=30)
    monkeypatch.setenv("PEERS_CTL_PEERS_BIN", str(stub))
    import importlib
    import peers_ctl.runner as runner_mod
    importlib.reload(runner_mod)

    proj = Project(name="snake", path=str(target))
    s.add(proj)
    pid = runner_mod.start_project(s, s.get("snake"))
    assert pid > 0
    assert is_pid_alive(pid)

    t0 = time.monotonic()
    runner_mod.stop_project(s, s.get("snake"), grace_s=2.0)
    elapsed = time.monotonic() - t0
    # Without the zombie-reap fix, the loop polls is_pid_alive every
    # 200 ms for the full 2 s grace because kill(0) keeps reporting
    # the unreaped child as alive. With the fix, waitpid(WNOHANG)
    # promptly reaps the dead sh inside the poll loop and we exit
    # within one polling interval.
    assert elapsed < 1.0, (
        f"stop_project took {elapsed:.3f}s — zombie child wrongly "
        "kept the grace loop alive past the early-exit window."
    )

    p_after = s.get("snake")
    assert p_after.state == "stopped"
    assert p_after.pid is None


def test_stop_project_escalates_to_kill_process_group_after_leader_exits(
    tmp_path: Path, monkeypatch,
):
    """BUG-135: a fast-exiting leader must not let TERM-ignoring group
    members survive after stop_project marks the project stopped."""
    cfg = tmp_path / "ctl"
    s = Store(cfg)
    target = _stub_target(tmp_path)
    pidfile = tmp_path / "grandchild.pid"
    child_code = (
        "import os, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    stub = tmp_path / "peers_spawn_ignorer.py"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import signal, subprocess, sys, time\n"
        f"child_code = {child_code!r}\n"
        "subprocess.Popen([sys.executable, '-c', child_code])\n"
        "def term(_sig, _frame):\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, term)\n"
        "while True:\n"
        "    time.sleep(1)\n"
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PEERS_CTL_PEERS_BIN", str(stub))

    import importlib
    import peers_ctl.runner as runner_mod
    importlib.reload(runner_mod)

    s.add(Project(name="snake", path=str(target)))
    pid = runner_mod.start_project(s, s.get("snake"))
    assert pid > 0
    for _ in range(50):
        if pidfile.exists():
            break
        time.sleep(0.1)
    assert pidfile.exists(), "grandchild did not publish its pid"
    child_pid = int(pidfile.read_text())

    try:
        assert is_pid_alive(child_pid)
        runner_mod.stop_project(s, s.get("snake"), grace_s=0.3)
        for _ in range(40):
            if not is_pid_alive(child_pid):
                break
            time.sleep(0.05)
        assert not is_pid_alive(child_pid), (
            "TERM-ignoring process-group child survived stop_project"
        )
    finally:
        if is_pid_alive(child_pid):
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_process_group_helpers_reject_nonpositive_pgid():
    """BUG-136: pgid<=0 must not reach killpg/proc-scan paths.

    `os.killpg(0, 0)` targets the CALLER's pgrp, which would let a
    leaked 0 escalate to SIGKILL on peers-ctl itself. And /proc lists
    kernel threads with pgid==0, so the scan path would falsely report
    liveness for a pgid==0 caller. Both helpers must short-circuit
    before issuing any syscall.
    """
    import peers_ctl.runner as runner_mod

    # Sad path: explicit zero is the dangerous value.
    assert runner_mod._process_group_alive(0) is False
    assert runner_mod._process_group_has_live_members(0) is False
    # Edge: negative pgid (defensive — should never occur).
    assert runner_mod._process_group_alive(-1) is False
    assert runner_mod._process_group_has_live_members(-1) is False
    # Edge: None continues to be rejected.
    assert runner_mod._process_group_alive(None) is False
    assert runner_mod._process_group_has_live_members(None) is False
    # Edge: bool is an int subclass but not a real pgid.
    assert runner_mod._process_group_alive(True) is False
    assert runner_mod._process_group_has_live_members(True) is False
    # Happy path: caller's own pgid is positive and reports alive.
    own_pgid = os.getpgid(0)
    assert own_pgid > 0
    assert runner_mod._process_group_alive(own_pgid) is True
    assert runner_mod._process_group_has_live_members(own_pgid) is True


def test_signal_uses_positive_process_group(monkeypatch):
    import peers_ctl.runner as runner_mod

    calls: list[tuple[str, int, int]] = []

    def fake_killpg(pgid: int, sig: int) -> None:
        calls.append(("killpg", pgid, sig))

    def fake_kill(pid: int, sig: int) -> None:
        calls.append(("kill", pid, sig))

    monkeypatch.setattr(runner_mod.os, "killpg", fake_killpg)
    monkeypatch.setattr(runner_mod.os, "kill", fake_kill)

    runner_mod._signal(999999, 12345, signal.SIGTERM)

    assert calls == [("killpg", 12345, signal.SIGTERM)]


def test_signal_rejects_nonpositive_pgid_and_falls_back_to_pid(monkeypatch):
    import peers_ctl.runner as runner_mod

    calls: list[tuple[str, int, int]] = []

    def fake_killpg(pgid: int, sig: int) -> None:
        calls.append(("killpg", pgid, sig))

    def fake_kill(pid: int, sig: int) -> None:
        calls.append(("kill", pid, sig))

    monkeypatch.setattr(runner_mod.os, "killpg", fake_killpg)
    monkeypatch.setattr(runner_mod.os, "kill", fake_kill)

    runner_mod._signal(999990, 0, signal.SIGTERM)
    runner_mod._signal(999991, -1, signal.SIGKILL)
    runner_mod._signal(999992, None, signal.SIGTERM)
    runner_mod._signal(999993, True, signal.SIGKILL)

    assert calls == [
        ("kill", 999990, signal.SIGTERM),
        ("kill", 999991, signal.SIGKILL),
        ("kill", 999992, signal.SIGTERM),
        ("kill", 999993, signal.SIGKILL),
    ]


def test_alive_via_pgid_or_pid_catches_leader_outside_captured_group(monkeypatch):
    """Review W2: if the target re-setpgid's out of the captured pgid, the
    /proc group scan finds no members for the stale pgid → it must NOT be
    declared dead while the leader PID is still alive (else SIGKILL
    escalation is skipped on a live process — a regression vs the old
    is_pid_alive path). _alive_via_pgid_or_pid ORs in is_pid_alive(pid)."""
    import peers_ctl.runner as runner_mod

    monkeypatch.setattr(
        runner_mod, "_process_group_has_live_members", lambda p: False
    )
    monkeypatch.setattr(runner_mod, "is_pid_alive", lambda p: True)

    assert runner_mod._alive_via_pgid_or_pid(4242, 4242) is True


def test_alive_via_pgid_or_pid_uses_valid_pgid_discriminator(monkeypatch):
    """BUG-138: stop_project's liveness dispatch must check
    `_valid_pgid` so a leaked invalid pgid (0, negative, bool) falls
    back to `is_pid_alive(pid)` instead of trusting a False from
    `_process_group_has_live_members` (which short-circuits for any
    invalid pgid per BUG-136). Without this, an invalid pgid would
    prematurely conclude the process is dead and skip SIGKILL
    escalation while the actual PID may still be alive.
    """
    import peers_ctl.runner as runner_mod

    pid = os.getpid()
    own_pgid = os.getpgid(0)
    assert own_pgid > 0

    # Happy: valid positive pgid → process-group probe is used.
    pg_calls: list[int | None] = []
    is_alive_calls: list[int] = []
    original_pg = runner_mod._process_group_has_live_members
    original_alive = runner_mod.is_pid_alive

    def fake_pg(p):
        pg_calls.append(p)
        return original_pg(p)

    def fake_is_alive(p):
        is_alive_calls.append(p)
        return original_alive(p)

    monkeypatch.setattr(runner_mod, "_process_group_has_live_members", fake_pg)
    monkeypatch.setattr(runner_mod, "is_pid_alive", fake_is_alive)

    assert runner_mod._alive_via_pgid_or_pid(pid, own_pgid) is True
    assert pg_calls == [own_pgid]
    assert is_alive_calls == []

    # Edge: None pgid → falls back to is_pid_alive(pid).
    pg_calls.clear()
    is_alive_calls.clear()
    assert runner_mod._alive_via_pgid_or_pid(pid, None) is True
    assert pg_calls == []
    assert is_alive_calls == [pid]

    # Sad: invalid pgid (0, -1, bool True) must fall back to
    # is_pid_alive(pid) — NOT call _process_group_has_live_members
    # (which would silently return False and falsely report dead).
    for bad_pgid in (0, -1, True):
        pg_calls.clear()
        is_alive_calls.clear()
        assert runner_mod._alive_via_pgid_or_pid(pid, bad_pgid) is True, (
            f"dispatch with pgid={bad_pgid!r} did not fall back to "
            "is_pid_alive — the bug allows premature dead detection."
        )
        assert pg_calls == [], (
            f"dispatch with pgid={bad_pgid!r} consulted "
            f"_process_group_has_live_members ({pg_calls!r}); "
            "BUG-138 dispatch asymmetry not fixed."
        )
        assert is_alive_calls == [pid]

    # Sad: clearly-dead PID + invalid pgid → still returns False
    # (the fallback is honest, not optimistic).
    dead_pid = 2**31 - 1
    pg_calls.clear()
    is_alive_calls.clear()
    assert runner_mod._alive_via_pgid_or_pid(dead_pid, 0) is False
    assert pg_calls == []
    assert is_alive_calls == [dead_pid]


def test_start_rejects_already_running(tmp_path: Path):
    s = Store(tmp_path / "ctl")
    target = _stub_target(tmp_path)
    proj = Project(name="x", path=str(target), state="running",
                   pid=os.getpid())  # self is "alive"
    s.add(proj)
    with pytest.raises(ValueError, match="already running"):
        start_project(s, s.get("x"))


def test_start_rejects_missing_path(tmp_path: Path):
    s = Store(tmp_path / "ctl")
    proj = Project(name="x", path=str(tmp_path / "nowhere"))
    s.add(proj)
    with pytest.raises(ValueError, match="path"):
        start_project(s, s.get("x"))


def test_start_rejects_uninitialised_target(tmp_path: Path):
    s = Store(tmp_path / "ctl")
    bare = tmp_path / "bare"
    bare.mkdir()
    proj = Project(name="x", path=str(bare))
    s.add(proj)
    with pytest.raises(ValueError, match="config.yaml"):
        start_project(s, s.get("x"))


def test_start_passes_max_usd_to_peers_run(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "ctl"
    s = Store(cfg)
    target = _stub_target(tmp_path)
    argv_log = tmp_path / "argv.txt"
    stub = tmp_path / "peers_args.sh"
    stub.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {argv_log}\n"
        "sleep 30\n"
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PEERS_CTL_PEERS_BIN", str(stub))

    import importlib
    import peers_ctl.runner as runner_mod
    importlib.reload(runner_mod)

    s.add(Project(name="snake", path=str(target)))
    pid = runner_mod.start_project(
        s, s.get("snake"), max_ticks=2, max_usd=1.25,
    )
    try:
        for _ in range(30):
            if argv_log.exists():
                break
            time.sleep(0.1)
        args = argv_log.read_text().splitlines()
        assert args == [
            "-C", str(target), "run",
            "--max-ticks", "2",
            "--max-usd", "1.25",
        ]
    finally:
        runner_mod.stop_project(s, s.get("snake"), grace_s=1)
    assert not is_pid_alive(pid)


def test_stop_missing_starttime_allows_current_child_fallback(tmp_path: Path):
    cfg = tmp_path / "ctl"
    s = Store(cfg)
    target = _stub_target(tmp_path)
    s.add(Project(name="snake", path=str(target)))
    proc = subprocess.Popen(
        ["sh", "-c", "sleep 30"],
        cwd=target,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    try:
        s.update(
            "snake",
            state="running",
            pid=proc.pid,
            notes="max_ticks=None max_usd=None starttime=MISSING container=0",
        )

        import peers_ctl.runner as runner_mod

        runner_mod.stop_project(s, s.get("snake"), grace_s=1)

        p_after = s.get("snake")
        assert p_after.state == "stopped"
        assert p_after.pid is None
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=1)


def test_stop_missing_starttime_reaps_exited_child(tmp_path: Path):
    cfg = tmp_path / "ctl"
    s = Store(cfg)
    target = _stub_target(tmp_path)
    s.add(Project(name="snake", path=str(target)))
    proc = subprocess.Popen(
        ["sh", "-c", "exit 0"],
        cwd=target,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    time.sleep(0.1)
    s.update(
        "snake",
        state="running",
        pid=proc.pid,
        notes="max_ticks=None max_usd=None starttime=MISSING container=0",
    )

    import peers_ctl.runner as runner_mod

    runner_mod.stop_project(s, s.get("snake"), grace_s=0)

    p_after = s.get("snake")
    assert p_after.state == "stopped"
    assert p_after.pid is None


def test_stop_missing_starttime_non_child_refuses_without_clearing_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    cfg = tmp_path / "ctl"
    s = Store(cfg)
    target = _stub_target(tmp_path)
    s.add(Project(name="snake", path=str(target)))
    pid = os.getpid()
    s.update(
        "snake",
        state="running",
        pid=pid,
        notes="max_ticks=None max_usd=None starttime=MISSING container=0",
    )

    import peers_ctl.runner as runner_mod

    monkeypatch.setattr(runner_mod, "_is_current_child", lambda _pid: False)

    with pytest.raises(RuntimeError, match="starttime was unavailable"):
        runner_mod.stop_project(s, s.get("snake"), grace_s=0)

    p_after = s.get("snake")
    assert p_after.state == "running"
    assert p_after.pid == pid


# --- Budget overrides (--max-runtime / --reset-budget / pre-flight) ---

def test_parse_duration_accepts_unit_suffixes() -> None:
    """Operator-friendly DURATION format: bare integer = seconds,
    suffixes s/m/h/d/w expand. Whitespace tolerated."""
    from peers_ctl.runner import _parse_duration
    assert _parse_duration("300") == 300
    assert _parse_duration("300s") == 300
    assert _parse_duration("90m") == 5400
    assert _parse_duration("6h") == 21600
    assert _parse_duration("2d") == 172800
    assert _parse_duration("1w") == 604800
    assert _parse_duration("  6h  ") == 21600


def test_parse_duration_rejects_invalid() -> None:
    """Strict parser — bad input should raise ValueError, not silently
    misinterpret. Empty, non-positive, garbage tokens, mixed units."""
    from peers_ctl.runner import _parse_duration
    for bad in ("", "0", "0h", "-1h", "6 hours", "6hh", "h6", "6.5h",
                "abc", "6h2m"):
        with pytest.raises(ValueError):
            _parse_duration(bad)


def _stub_target_with_state(tmp_path: Path, *,
                             spent_runtime_s: int = 0,
                             max_runtime_s: int = 21600,
                             spent_iterations: int = 0,
                             spent_tokens: int = 0,
                             spent_usd: float = 0.0,
                             consecutive_failures: int = 0) -> Path:
    """Like _stub_target but also writes a .peers/state.json so the
    budget-related code paths have something to inspect / mutate."""
    target = _stub_target(tmp_path)
    import json
    state = {
        "iteration": spent_iterations,
        "peer_order": ["claude", "codex"],
        "turn_index": 0,
        "peers": {"claude": {}, "codex": {}},
        "budget": {
            "spent_runtime_s": spent_runtime_s,
            "max_runtime_s": max_runtime_s,
            "spent_iterations": spent_iterations,
            "spent_tokens": spent_tokens,
            "spent_usd": spent_usd,
            "consecutive_failures": consecutive_failures,
            "max_consecutive_failures": 5,
            "max_tokens": None,
            "max_usd": None,
            "wasted_runtime_s": 0,
        },
    }
    (target / ".peers" / "state.json").write_text(json.dumps(state))
    return target


def test_start_aborts_when_budget_exhausted(tmp_path: Path) -> None:
    """UX foot-gun fix (OnionBird incident 2026-05-26): when
    spent_runtime_s >= max_runtime_s the loop would silently exit
    after 0 ticks with `budget:max_runtime`. start_project now
    refuses with an actionable error message, pointing at
    --max-runtime / --reset-budget / --force."""
    s = Store(tmp_path / "ctl")
    target = _stub_target_with_state(
        tmp_path, spent_runtime_s=23045, max_runtime_s=21600,
    )
    proj = Project(name="x", path=str(target))
    s.add(proj)
    with pytest.raises(ValueError, match="budget already exhausted"):
        start_project(s, s.get("x"))


def test_start_force_overrides_exhausted_budget(
    tmp_path: Path, monkeypatch,
) -> None:
    """`--force` is the explicit escape hatch — operator accepts that
    the loop will exit immediately and just wants to record the
    sentinel."""
    cfg = tmp_path / "ctl"
    s = Store(cfg)
    target = _stub_target_with_state(
        tmp_path, spent_runtime_s=23045, max_runtime_s=21600,
    )
    stub = _peers_stub(tmp_path, sleep_s=0.5)
    monkeypatch.setenv("PEERS_CTL_PEERS_BIN", str(stub))
    import importlib
    import peers_ctl.runner as runner_mod
    importlib.reload(runner_mod)
    proj = Project(name="x", path=str(target))
    s.add(proj)
    pid = runner_mod.start_project(s, s.get("x"), force=True)
    assert pid > 0
    # Cleanup
    runner_mod.stop_project(s, s.get("x"), grace_s=2)


def test_start_max_runtime_writes_state_before_launch(
    tmp_path: Path, monkeypatch,
) -> None:
    """`--max-runtime 12h` overrides budget.max_runtime_s in state.json
    *before* the peers process starts, so the inner loop reads the
    new cap on its first tick."""
    cfg = tmp_path / "ctl"
    s = Store(cfg)
    target = _stub_target_with_state(
        tmp_path, spent_runtime_s=0, max_runtime_s=21600,
    )
    stub = _peers_stub(tmp_path, sleep_s=0.5)
    monkeypatch.setenv("PEERS_CTL_PEERS_BIN", str(stub))
    import importlib
    import peers_ctl.runner as runner_mod
    importlib.reload(runner_mod)
    proj = Project(name="x", path=str(target))
    s.add(proj)
    runner_mod.start_project(s, s.get("x"), max_runtime_s=43200)
    import json
    state = json.loads((target / ".peers" / "state.json").read_text())
    assert state["budget"]["max_runtime_s"] == 43200, (
        f"expected 43200, got {state['budget']['max_runtime_s']}"
    )
    runner_mod.stop_project(s, s.get("x"), grace_s=2)


def test_start_reset_budget_zeroes_spent_counters(
    tmp_path: Path, monkeypatch,
) -> None:
    """`--reset-budget` zeroes spent_runtime_s, spent_iterations,
    spent_tokens, spent_usd, wasted_runtime_s, and
    consecutive_failures — semantically a 'fresh session' on top of
    the existing state. max_runtime_s itself is NOT zeroed."""
    cfg = tmp_path / "ctl"
    s = Store(cfg)
    target = _stub_target_with_state(
        tmp_path,
        spent_runtime_s=23045, max_runtime_s=21600,
        spent_iterations=33, spent_tokens=1_500_000,
        spent_usd=42.0, consecutive_failures=3,
    )
    stub = _peers_stub(tmp_path, sleep_s=0.5)
    monkeypatch.setenv("PEERS_CTL_PEERS_BIN", str(stub))
    import importlib
    import peers_ctl.runner as runner_mod
    importlib.reload(runner_mod)
    proj = Project(name="x", path=str(target))
    s.add(proj)
    runner_mod.start_project(s, s.get("x"), reset_budget=True)
    import json
    state = json.loads((target / ".peers" / "state.json").read_text())
    b = state["budget"]
    assert b["spent_runtime_s"] == 0
    assert b["spent_iterations"] == 0
    assert b["spent_tokens"] == 0
    assert b["spent_usd"] == 0.0
    assert b["consecutive_failures"] == 0
    assert b["wasted_runtime_s"] == 0
    # max_runtime_s must be PRESERVED
    assert b["max_runtime_s"] == 21600
    runner_mod.stop_project(s, s.get("x"), grace_s=2)


def test_reset_budget_preserves_state_iteration(
    tmp_path: Path, monkeypatch,
) -> None:
    """`--reset-budget` resets BUDGET counters but NOT `state.iteration`.
    `state.iteration` is the resume-pointer ('next tick is N+1'),
    visible in the operator's tick log; `budget.spent_iterations`
    is a separate cap counter. Decoupling them means an operator
    can give a project a fresh budget without losing the
    tick-number continuity in their logs — the OnionBird case
    (2026-05-26): `--max-runtime` bump alone resumed at tick 27
    cleanly; `--reset-budget` would keep that 'tick 27' visible
    even though the budget counters restart at 0."""
    cfg = tmp_path / "ctl"
    s = Store(cfg)
    target = _stub_target_with_state(
        tmp_path, spent_runtime_s=23045, max_runtime_s=21600,
        spent_iterations=26,
    )
    # Manually set state.iteration to 26 (separate from spent_iterations
    # in semantics, same value in practice — driver_orchestrator.py
    # increments both per tick).
    import json
    state_path = target / ".peers" / "state.json"
    st = json.loads(state_path.read_text())
    st["iteration"] = 26
    state_path.write_text(json.dumps(st))
    stub = _peers_stub(tmp_path, sleep_s=0.5)
    monkeypatch.setenv("PEERS_CTL_PEERS_BIN", str(stub))
    import importlib
    import peers_ctl.runner as runner_mod
    importlib.reload(runner_mod)
    proj = Project(name="x", path=str(target))
    s.add(proj)
    runner_mod.start_project(s, s.get("x"), reset_budget=True)
    after = json.loads(state_path.read_text())
    assert after["iteration"] == 26, (
        f"state.iteration must be preserved across --reset-budget; "
        f"got {after['iteration']}"
    )
    assert after["budget"]["spent_iterations"] == 0
    assert after["budget"]["spent_runtime_s"] == 0
    runner_mod.stop_project(s, s.get("x"), grace_s=2)


def test_start_max_runtime_and_reset_budget_combine(
    tmp_path: Path, monkeypatch,
) -> None:
    """Both flags compose: bump the ceiling AND clean the spent
    counters."""
    cfg = tmp_path / "ctl"
    s = Store(cfg)
    target = _stub_target_with_state(
        tmp_path, spent_runtime_s=23045, max_runtime_s=21600,
        spent_iterations=33,
    )
    stub = _peers_stub(tmp_path, sleep_s=0.5)
    monkeypatch.setenv("PEERS_CTL_PEERS_BIN", str(stub))
    import importlib
    import peers_ctl.runner as runner_mod
    importlib.reload(runner_mod)
    proj = Project(name="x", path=str(target))
    s.add(proj)
    runner_mod.start_project(
        s, s.get("x"),
        max_runtime_s=86400, reset_budget=True,
    )
    import json
    state = json.loads((target / ".peers" / "state.json").read_text())
    b = state["budget"]
    assert b["spent_runtime_s"] == 0
    assert b["spent_iterations"] == 0
    assert b["max_runtime_s"] == 86400
    runner_mod.stop_project(s, s.get("x"), grace_s=2)


def test_apply_budget_overrides_writes_sidecar_without_state(
    tmp_path: Path,
) -> None:
    """First-start fix: on a freshly-init'd project state.json does not
    exist yet, so writing the cap only to state.json silently dropped
    `--max-runtime`. The operator override must persist to the sidecar
    (.peers/budget-overrides.json) regardless, so the orchestrator can
    re-apply it after the config overlay on the first tick."""
    import json
    import peers_ctl.runner as runner_mod
    from peers.budget_accountant import OPERATOR_BUDGET_OVERRIDE_FILE

    target = _stub_target(tmp_path)  # has .peers/config.yaml, NO state.json
    assert not (target / ".peers" / "state.json").exists()
    proj = Project(name="x", path=str(target))

    runner_mod._apply_budget_overrides(proj, 43200, False)

    sidecar = target / ".peers" / OPERATOR_BUDGET_OVERRIDE_FILE
    assert sidecar.exists(), "operator override must persist to the sidecar"
    assert json.loads(sidecar.read_text())["max_runtime_s"] == 43200


def test_apply_budget_overrides_reset_clears_sidecar(tmp_path: Path) -> None:
    """`--reset-budget` returns a project to its config.yaml defaults, so
    it must also clear any persisted operator cap override."""
    import json
    import peers_ctl.runner as runner_mod
    from peers.budget_accountant import OPERATOR_BUDGET_OVERRIDE_FILE

    target = _stub_target(tmp_path)
    sidecar = target / ".peers" / OPERATOR_BUDGET_OVERRIDE_FILE
    sidecar.write_text(json.dumps({"max_runtime_s": 43200}))
    proj = Project(name="x", path=str(target))

    runner_mod._apply_budget_overrides(proj, None, True)

    assert not sidecar.exists(), "reset must clear the operator override"


def test_start_rejects_non_positive_max_usd(tmp_path: Path):
    s = Store(tmp_path / "ctl")
    target = _stub_target(tmp_path)
    s.add(Project(name="snake", path=str(target)))
    with pytest.raises(ValueError, match="max_usd"):
        start_project(s, s.get("snake"), max_usd=0)


def test_start_rejects_non_positive_max_ticks(tmp_path: Path):
    s = Store(tmp_path / "ctl")
    target = _stub_target(tmp_path)
    s.add(Project(name="snake", path=str(target)))
    with pytest.raises(ValueError, match="max_ticks"):
        start_project(s, s.get("snake"), max_ticks=0)


def test_start_cleans_up_host_process_when_registry_update_fails(
    tmp_path: Path, monkeypatch
):
    cfg = tmp_path / "ctl"
    s = Store(cfg)
    target = _stub_target(tmp_path)
    stub = _peers_stub(tmp_path, sleep_s=30)
    monkeypatch.setenv("PEERS_CTL_PEERS_BIN", str(stub))

    import importlib
    import peers_ctl.runner as runner_mod
    importlib.reload(runner_mod)

    s.add(Project(name="snake", path=str(target)))
    cleaned: list[int] = []

    def fail_update(*_args, **_kwargs):
        raise RuntimeError("registry write failed")

    def record_cleanup(proc):
        cleaned.append(proc.pid)
        proc.kill()
        proc.wait(timeout=1)

    monkeypatch.setattr(s, "update", fail_update)
    monkeypatch.setattr(runner_mod, "_terminate_spawned_process", record_cleanup)

    with pytest.raises(RuntimeError, match="registry write failed"):
        runner_mod.start_project(s, s.get("snake"))

    assert cleaned


def test_start_refuses_symlinked_log_path(tmp_path: Path):
    s = Store(tmp_path / "ctl")
    target = _stub_target(tmp_path)
    bait = tmp_path / "bait.log"
    bait.write_text("keep me")
    link = tmp_path / "project.log"
    link.symlink_to(bait)
    with pytest.raises(ValueError, match="symlink"):
        s.add(Project(name="snake", path=str(target), log_path=str(link)))

    assert bait.read_text() == "keep me"


def test_start_refuses_late_logs_parent_symlink(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "ctl"
    s = Store(cfg)
    target = _stub_target(tmp_path)
    stub = _peers_stub(tmp_path, sleep_s=30)
    monkeypatch.setenv("PEERS_CTL_PEERS_BIN", str(stub))

    import importlib
    import peers_ctl.runner as runner_mod
    importlib.reload(runner_mod)

    s.add(Project(name="snake", path=str(target)))
    project = s.get("snake")
    assert project is not None
    real_safe_log_path_for = s.safe_log_path_for
    outside = tmp_path / "outside-logs"
    outside.mkdir()

    def swap_logs_dir_after_validation(p: Project) -> Path:
        log = real_safe_log_path_for(p)
        shutil.rmtree(cfg / "logs")
        (cfg / "logs").symlink_to(outside, target_is_directory=True)
        return log

    monkeypatch.setattr(s, "safe_log_path_for", swap_logs_dir_after_validation)

    with pytest.raises(OSError):
        runner_mod.start_project(s, project)

    assert not (outside / "snake.log").exists()


# --- prune_logs ---------------------------------------------------------

def test_prune_skips_running_projects(tmp_path: Path):
    s = Store(tmp_path / "ctl")
    s.add(Project(name="x", path=str(tmp_path / "p"),
                  state="running", pid=os.getpid()))
    log = s.log_path_for("x")
    log.write_text("hello")
    # Backdate.
    os.utime(log, (0, 0))
    assert prune_logs(s, older_than_days=1) == 0
    assert log.exists()


def test_prune_deletes_old_log_for_stopped(tmp_path: Path):
    s = Store(tmp_path / "ctl")
    s.add(Project(name="x", path=str(tmp_path / "p")))
    log = s.log_path_for("x")
    log.write_text("hello")
    os.utime(log, (0, 0))
    s.update("x", log_path=str(log))
    assert prune_logs(s, older_than_days=1) == 1
    assert not log.exists()


def test_prune_skips_registry_log_path_outside_logs(tmp_path: Path, caplog):
    cfg = tmp_path / "ctl"
    s = Store(cfg)
    victim = tmp_path / "victim.log"
    victim.write_text("do not delete")
    os.utime(victim, (0, 0))
    s.path.write_text(
        "projects:\n"
        "  - name: x\n"
        f"    path: {tmp_path / 'p'}\n"
        "    state: stopped\n"
        f"    log_path: {victim}\n"
    )

    with caplog.at_level("WARNING", logger="peers_ctl.store"):
        assert prune_logs(s, older_than_days=1) == 0

    assert victim.exists()
    assert "unsafe log_path" in caplog.text


def test_store_logs_skipped_malformed_registry_entry(tmp_path: Path, caplog):
    cfg = tmp_path / "ctl"
    s = Store(cfg)
    s.path.write_text("projects:\n  - not-a-project\n")

    with caplog.at_level("WARNING", logger="peers_ctl.store"):
        assert s.list_projects() == []

    assert "skipping malformed project registry entry" in caplog.text


def test_store_mutation_refuses_symlinked_tmp_file(tmp_path: Path):
    cfg = tmp_path / "ctl"
    s = Store(cfg)
    bait = tmp_path / "bait.yaml"
    bait.write_text("keep me")
    s.path.with_suffix(s.path.suffix + ".tmp").symlink_to(bait)

    with pytest.raises(OSError):
        s.add(Project(name="x", path=str(tmp_path / "p")))

    assert bait.read_text() == "keep me"


def test_store_mutation_refuses_symlinked_lock_file(tmp_path: Path):
    cfg = tmp_path / "ctl"
    s = Store(cfg)
    bait = tmp_path / "bait.lock"
    bait.write_text("keep me")
    (cfg / ".lock").symlink_to(bait)

    with pytest.raises(OSError):
        s.add(Project(name="x", path=str(tmp_path / "p")))

    assert bait.read_text() == "keep me"


def test_prune_logs_warns_on_unlink_failure(
    tmp_path: Path, monkeypatch, caplog
):
    s = Store(tmp_path / "ctl")
    s.add(Project(name="x", path=str(tmp_path / "p")))
    log = s.log_path_for("x")
    log.write_text("hello")
    os.utime(log, (0, 0))
    s.update("x", log_path=str(log))

    real_unlink = Path.unlink

    def fail_unlink(self):
        if self == log:
            raise OSError("permission denied")
        return real_unlink(self)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with caplog.at_level("WARNING", logger="peers_ctl.store"):
        assert prune_logs(s, older_than_days=1) == 0

    assert "could not prune log" in caplog.text


# --- CLI ---------------------------------------------------------------

def test_cmd_add_and_remove_round_trip(tmp_path: Path, capsys):
    cfg = tmp_path / "ctl"
    target = _stub_target(tmp_path)
    rc = cmd_add(name="snake", path=target, config_dir=cfg)
    assert rc == 0
    assert Store(cfg).get("snake") is not None
    rc = cmd_remove("snake", config_dir=cfg)
    assert rc == 0
    assert Store(cfg).get("snake") is None


def test_cmd_add_uses_basename_when_no_name(tmp_path: Path):
    cfg = tmp_path / "ctl"
    target = _stub_target(tmp_path)
    rc = cmd_add(name=None, path=target, config_dir=cfg)
    assert rc == 0
    assert Store(cfg).get("target") is not None


def test_cmd_add_rejects_invalid_name(tmp_path: Path, capsys):
    cfg = tmp_path / "ctl"
    target = _stub_target(tmp_path)
    rc = cmd_add(name="../bad", path=target, config_dir=cfg)
    assert rc == 2
    assert "invalid project name" in capsys.readouterr().err


def test_cmd_list_empty(tmp_path: Path, capsys):
    cfg = tmp_path / "ctl"
    rc = cmd_list(cfg)
    out = capsys.readouterr().out
    assert rc == 0
    assert "no projects" in out


def test_cmd_remove_missing(tmp_path: Path, capsys):
    cfg = tmp_path / "ctl"
    rc = cmd_remove("ghost", config_dir=cfg)
    assert rc == 1
    err = capsys.readouterr().err
    assert "no such project" in err


def test_cmd_status_unknown_project(tmp_path: Path, capsys):
    cfg = tmp_path / "ctl"
    rc = cmd_status("ghost", cfg)
    assert rc == 1


def test_cmd_logs_empty_after_add(tmp_path: Path, capsys):
    cfg = tmp_path / "ctl"
    target = _stub_target(tmp_path)
    cmd_add("snake", target, cfg)
    rc = cmd_logs("snake", lines=5, config_dir=cfg)
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""


def test_cmd_logs_refuses_symlinked_log_path(tmp_path: Path, capsys):
    cfg = tmp_path / "ctl"
    s = Store(cfg)
    target = _stub_target(tmp_path)
    bait = tmp_path / "secret.log"
    bait.write_text("do not print\n")
    link = tmp_path / "snake.log"
    link.symlink_to(bait)
    s.path.write_text(
        "projects:\n"
        "  - name: snake\n"
        f"    path: {target}\n"
        "    state: stopped\n"
        f"    log_path: {link}\n"
    )

    rc = cmd_logs("snake", lines=5, config_dir=cfg)
    captured = capsys.readouterr()

    assert rc == 1
    assert "log not yet written" in captured.err
    assert "do not print" not in captured.out


def test_cmd_report_writes_controller_markdown(tmp_path: Path, capsys):
    cfg = tmp_path / "ctl"
    target = _stub_target(tmp_path)
    (target / "README.md").write_text("# Snake\n")
    log_dir = target / ".peers" / "log"
    log_dir.mkdir()
    (log_dir / "runs.jsonl").write_text(
        '{"ts":"2026-05-22T10:00:00Z","iter":1}\n'
        '{"ts":"2026-05-22T10:01:00Z","event":"exit"}\n'
    )
    s = Store(cfg)
    s.add(Project(name="snake", path=str(target)))

    rc = cmd_report(config_dir=cfg)
    captured = capsys.readouterr()

    assert rc == 0
    assert f"wrote {cfg / 'REPORT.md'}" in captured.out
    report = (cfg / "REPORT.md").read_text()
    assert report.startswith("# peers-ctl report\n")
    # Newly-added project: state is "fresh" before any start.
    assert "| snake | fresh | 1 |" in report
    assert "present:" in report
    assert str(s.log_path_for("snake")) in report


def test_cmd_report_can_scope_to_one_project(tmp_path: Path, capsys):
    cfg = tmp_path / "ctl"
    s = Store(cfg)
    first = _stub_target(tmp_path)
    second = tmp_path / "other"
    (second / ".peers" / "log").mkdir(parents=True)
    (second / ".peers" / "config.yaml").write_text("driver: orchestrator\n")
    (second / ".peers" / "log" / "runs.jsonl").write_text("")
    s.add(Project(name="snake", path=str(first)))
    s.add(Project(name="other", path=str(second)))

    rc = cmd_report("other", config_dir=cfg)

    assert rc == 0
    capsys.readouterr()
    report = (cfg / "REPORT-other.md").read_text()
    assert "| other | fresh |" in report
    assert "| snake |" not in report


def test_cmd_report_rejects_unknown_project(tmp_path: Path, capsys):
    cfg = tmp_path / "ctl"
    Store(cfg)

    rc = cmd_report("ghost", config_dir=cfg)

    assert rc == 1
    assert "no such project" in capsys.readouterr().err


def test_cmd_report_refuses_symlinked_report_file(tmp_path: Path, capsys):
    cfg = tmp_path / "ctl"
    target = _stub_target(tmp_path)
    Store(cfg).add(Project(name="snake", path=str(target)))
    bait = tmp_path / "bait.md"
    bait.write_text("keep me\n")
    (cfg / "REPORT.md").symlink_to(bait)

    rc = cmd_report(config_dir=cfg)

    assert rc == 1
    assert "cannot write report safely" in capsys.readouterr().err
    assert bait.read_text() == "keep me\n"


def test_cmd_start_uses_runner(tmp_path: Path, monkeypatch, capsys):
    cfg = tmp_path / "ctl"
    target = _stub_target(tmp_path)
    cmd_add("snake", target, cfg)

    invocations: list[dict] = []

    def fake_start(store, project, max_ticks=None, max_usd=None,
                   max_runtime_s=None, reset_budget=False, force=False,
                   extra_args=(), container=False):
        invocations.append({
            "name": project.name, "max_ticks": max_ticks,
            "max_usd": max_usd, "container": container,
        })
        store.update(project.name, state="running", pid=12345)
        return 12345

    import peers_ctl.cli as cli_mod
    monkeypatch.setattr(cli_mod, "start_project", fake_start)
    rc = cmd_start("snake", max_ticks=3, max_usd=0.5, config_dir=cfg)
    assert rc == 0
    assert invocations == [
        {"name": "snake", "max_ticks": 3, "max_usd": 0.5,
         "container": False},
    ]


def test_cmd_start_reports_runner_runtime_error(
    tmp_path: Path, monkeypatch, capsys
):
    cfg = tmp_path / "ctl"
    target = _stub_target(tmp_path)
    cmd_add("snake", target, cfg)

    def fake_start(*args, **kwargs):
        raise RuntimeError("podman run failed")

    import peers_ctl.cli as cli_mod
    monkeypatch.setattr(cli_mod, "start_project", fake_start)

    rc = cmd_start("snake", config_dir=cfg)

    assert rc == 1
    assert "podman run failed" in capsys.readouterr().err


def test_cmd_stop_uses_runner(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "ctl"
    target = _stub_target(tmp_path)
    cmd_add("snake", target, cfg)
    Store(cfg).update("snake", state="running", pid=999999)

    invocations: list[str] = []

    def fake_stop(store, project, grace_s=10.0):
        invocations.append(project.name)
        store.update(project.name, state="stopped", pid=None)
        return 0

    import peers_ctl.cli as cli_mod
    monkeypatch.setattr(cli_mod, "stop_project", fake_stop)
    rc = cmd_stop("snake", config_dir=cfg)
    assert rc == 0
    assert invocations == ["snake"]


def test_cmd_stop_reports_runner_error(tmp_path: Path, monkeypatch, capsys):
    cfg = tmp_path / "ctl"
    target = _stub_target(tmp_path)
    cmd_add("snake", target, cfg)
    Store(cfg).update("snake", state="running", pid=999999)

    def fake_stop(*_args, **_kwargs):
        raise RuntimeError("stop failed")

    import peers_ctl.cli as cli_mod
    monkeypatch.setattr(cli_mod, "stop_project", fake_stop)

    rc = cmd_stop("snake", config_dir=cfg)

    assert rc == 1
    assert "stop failed" in capsys.readouterr().err


def test_default_config_dir_uses_xdg(monkeypatch, tmp_path: Path):
    from peers_ctl.store import default_config_dir
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert default_config_dir() == tmp_path / "peers-ctl"


def test_entrypoint_module_imports():
    import peers_ctl.cli as m
    assert hasattr(m, "main")
