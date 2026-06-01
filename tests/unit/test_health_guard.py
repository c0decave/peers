from pathlib import Path
import threading

import pytest

from peers.health_guard import HealthGuard

FIX = Path(__file__).parent.parent / "fixtures"


def test_search_pattern_restores_sigalrm_handler_and_timer(monkeypatch):
    """Happy path: SIGALRM guard returns a match and restores globals."""
    import re
    import peers.health_guard as hg_mod

    class FakeSignal:
        SIGALRM = "SIGALRM"
        ITIMER_REAL = "ITIMER_REAL"

        def __init__(self) -> None:
            self.handler = "previous-handler"
            self.calls: list[tuple] = []

        def getsignal(self, signum):
            self.calls.append(("getsignal", signum))
            return self.handler

        def signal(self, signum, handler):
            self.calls.append(("signal", signum, handler))
            self.handler = handler

        def setitimer(self, which, seconds, interval=0):
            self.calls.append(("setitimer", which, seconds, interval))
            if seconds == hg_mod._PATTERN_SEARCH_TIMEOUT_S:
                return (7.0, 0.5)
            return (0.0, 0.0)

    fake = FakeSignal()
    monkeypatch.setattr(hg_mod, "signal", fake)

    match = hg_mod._search_pattern(re.compile("needle"), "hay needle stack")

    assert match is not None
    assert match.group(0) == "needle"
    assert fake.handler == "previous-handler"
    assert ("setitimer", "ITIMER_REAL", 0, 0) in fake.calls
    assert ("setitimer", "ITIMER_REAL", 7.0, 0.5) in fake.calls


def test_search_pattern_without_setitimer_falls_back_to_regex(monkeypatch):
    """Edge path: platforms without setitimer still perform the search."""
    import re
    import peers.health_guard as hg_mod

    class FakeSignalNoTimer:
        pass

    monkeypatch.setattr(hg_mod, "signal", FakeSignalNoTimer())

    match = hg_mod._search_pattern(re.compile("needle"), "needle")

    assert match is not None
    assert match.group(0) == "needle"


def test_search_pattern_rolls_back_handler_when_timer_setup_fails(monkeypatch):
    """Sad path for BUG-101: failed timer setup must not leak SIGALRM."""
    import re
    import peers.health_guard as hg_mod

    class FakeSignal:
        SIGALRM = "SIGALRM"
        ITIMER_REAL = "ITIMER_REAL"

        def __init__(self) -> None:
            self.handler = "previous-handler"
            self.signal_calls: list[object] = []

        def getsignal(self, signum):
            assert signum == self.SIGALRM
            return self.handler

        def signal(self, signum, handler):
            assert signum == self.SIGALRM
            self.signal_calls.append(handler)
            self.handler = handler

        def setitimer(self, which, seconds, interval=0):
            assert which == self.ITIMER_REAL
            raise ValueError("timer setup unavailable")

    fake = FakeSignal()
    monkeypatch.setattr(hg_mod, "signal", fake)

    match = hg_mod._search_pattern(re.compile("needle"), "needle")

    assert match is not None
    assert match.group(0) == "needle"
    assert fake.handler == "previous-handler"
    assert fake.signal_calls[-1] == "previous-handler"


def test_reap_orphans_if_pid1_is_noop_outside_container(tmp_path: Path):
    """the orphan-reaper must be a
    no-op when peers is NOT PID 1 (e.g. running on the host under
    systemd/init). Otherwise it would race with subprocess.Popen.wait()
    and the host's process tree."""
    import os
    # We're not PID 1 inside pytest. The function should bail without
    # calling waitpid at all (which would either reap pytest-runner
    # children or fail with ECHILD).
    assert os.getpid() != 1
    assert HealthGuard._reap_orphans_if_pid1() == 0


def test_reap_orphans_if_pid1_reaps_when_simulated_pid1(monkeypatch):
    """when simulated as PID 1, the reaper sweeps zombies available
    via waitpid(-1, WNOHANG) and returns the count. Mock os.getpid +
    os.waitpid so the test stays hermetic — no real process tree
    perturbation."""
    import os
    monkeypatch.setattr(os, "getpid", lambda: 1)
    yields = iter([(101, 0), (102, 0), (103, 0), (0, 0)])
    monkeypatch.setattr(os, "waitpid", lambda *_args, **_kw: next(yields))
    assert HealthGuard._reap_orphans_if_pid1() == 3


def test_reap_orphans_if_pid1_returns_zero_on_no_children(monkeypatch):
    """when waitpid raises ChildProcessError (no reapable children
    at all), the reaper must return 0 cleanly, not propagate."""
    import os
    monkeypatch.setattr(os, "getpid", lambda: 1)
    def _no_children(*a, **kw):
        raise ChildProcessError("no children")
    monkeypatch.setattr(os, "waitpid", _no_children)
    assert HealthGuard._reap_orphans_if_pid1() == 0


def test_invoke_merges_extra_env(tmp_path: Path):
    import sys as _sys

    script = tmp_path / "print_env.py"
    script.write_text(
        "import os\n"
        "print(os.environ.get('PEERS_TEST_EXTRA_ENV', ''))\n"
    )

    result = HealthGuard(tmp_path).invoke(
        [_sys.executable, str(script)],
        prompt="",
        extra_env={"PEERS_TEST_EXTRA_ENV": "from-extra"},
    )

    assert result.classification == "success"
    assert result.stdout.strip() == "from-extra"


def test_sweep_zombies_via_proc_skips_tracked_pid(monkeypatch, tmp_path):
    """follow-up (BUG-zombie-leak, 2026-05-24): the existing
    `_reap_orphans_if_pid1` uses waitpid(-1) which races with
    subprocess.Popen.wait() — calling it inside invoke()'s poll loop
    could snipe the tracked peer pid, leaving Popen.wait to fall through
    to a synthetic returncode=0 (masking real exits/failures).

    `_sweep_zombies_via_proc(skip_pid)` walks /proc, picks only
    direct-children (ppid==os.getpid()) in Z-state, EXCEPT skip_pid,
    and waitpid's each specifically. No race with the tracked PID.

    Hermetic test: fake /proc layout with three zombies (one is
    skip_pid) + one running process. Only the two non-skip zombies
    must be reaped."""
    import os
    fake_proc = tmp_path / "proc"
    fake_proc.mkdir()
    our_pid = 1
    # Layout:
    #   PID 100: zombie, ppid=1, our direct child → must be reaped
    #   PID 200: zombie, ppid=1, our direct child but skip_pid=200 → MUST NOT
    #   PID 300: zombie, ppid=1, our direct child → must be reaped
    #   PID 400: running, ppid=1, our direct child → not touched
    #   PID 500: zombie, ppid=999, not our child → not touched
    def write_stat(pid: int, comm: str, state: str, ppid: int) -> None:
        d = fake_proc / str(pid)
        d.mkdir()
        # /proc/[pid]/stat format: pid (comm) state ppid ...
        (d / "stat").write_text(f"{pid} ({comm}) {state} {ppid} 0 0\n")
    write_stat(100, "git", "Z", 1)
    write_stat(200, "claude", "Z", 1)
    write_stat(300, "git", "Z", 1)
    write_stat(400, "node", "S", 1)
    write_stat(500, "git", "Z", 999)
    (fake_proc / "self").mkdir()    # non-numeric, must be ignored

    monkeypatch.setattr(os, "getpid", lambda: our_pid)
    reaped_pids: list[int] = []
    def fake_waitpid(pid, flags):
        reaped_pids.append(pid)
        return (pid, 0)
    monkeypatch.setattr(os, "waitpid", fake_waitpid)

    n = HealthGuard._sweep_zombies_via_proc(skip_pid=200, proc_root=str(fake_proc))
    assert n == 2
    assert sorted(reaped_pids) == [100, 300]


def test_sweep_zombies_via_proc_is_noop_outside_container(tmp_path, monkeypatch):
    """Sweep must be a no-op when not PID 1 — same safety rationale as
    `_reap_orphans_if_pid1`."""
    import os
    assert os.getpid() != 1
    assert HealthGuard._sweep_zombies_via_proc(skip_pid=0) == 0


# ===== (post-2026-05-24): halt_patterns =====
#
# Some failure classes (AUTH expired, QUOTA exhausted) need OPERATOR
# action — silent degradation wastes budget. invoke() accepts a
# separate `halt_patterns` list; on match, the resulting RunResult
# carries `halt_required=True` so the orchestrator can stop the whole
# run instead of just degrading the peer.


def test_phase2_halt_pattern_match_sets_halt_required(tmp_path):
    """happy: an AUTH-shape line in stderr that matches a
    halt_pattern flips RunResult.halt_required True. classification
    stays api-error so legacy code paths keep working."""
    hg = HealthGuard(cwd=tmp_path)
    auth_pat = (
        r"(?im)^[^\"]*?\b(ERROR|FATAL)\b[^\"]*?"
        r"\bauthentication[ _-]?(failed|error)\b"
    )
    # Fake CLI: emit a real-shape OAuth error to stderr, then exit 1.
    script = tmp_path / "fake_auth_fail.sh"
    script.write_text(
        "#!/bin/sh\n"
        "echo '2026-05-25T00:00:00Z ERROR auth: authentication failed' 1>&2\n"
        "sleep 0.3\n"
        "exit 1\n"
    )
    script.chmod(0o755)
    r = hg.invoke(
        [str(script)], prompt="ignored",
        idle_timeout_s=10, absolute_max_runtime_s=10,
        halt_patterns=[auth_pat],
    )
    assert r.classification == "api-error"
    assert r.halt_required is True
    assert "authentication failed" in r.matched_error_snippet


def test_phase2_error_pattern_does_NOT_set_halt_required(tmp_path):
    """edge: matching the regular (non-halt) error_patterns
    keeps the existing degrade-after-3 behavior. halt_required stays
    False."""
    hg = HealthGuard(cwd=tmp_path)
    rate_pat = (
        r"(?im)^[^\"]*?\b(ERROR|FATAL)\b[^\"]*?\brate.?limit\b"
    )
    script = tmp_path / "fake_rate_limit.sh"
    script.write_text(
        "#!/bin/sh\n"
        "echo '2026-05-25T00:00:00Z ERROR client: rate limit exceeded' 1>&2\n"
        "sleep 0.3\n"
        "exit 1\n"
    )
    script.chmod(0o755)
    r = hg.invoke(
        [str(script)], prompt="ignored",
        idle_timeout_s=10, absolute_max_runtime_s=10,
        error_patterns=[rate_pat],
    )
    assert r.classification == "api-error"
    assert r.halt_required is False


def test_phase2_halt_patterns_skip_file_dump(tmp_path):
    """sad: a peer dumping the v4-regression line (file
    content containing the auth-fail fixture in quotes) must NOT
    trigger halt_required because the tightened pattern uses
    [^\"]*?. Real OAuth errors trigger; file content does not."""
    hg = HealthGuard(cwd=tmp_path)
    auth_pat = (
        r"(?im)^[^\"]*?\b(ERROR|FATAL)\b[^\"]*?"
        r"\bauthentication[ _-]?(failed|error)\b"
    )
    script = tmp_path / "fake_grep_dump.sh"
    script.write_text(
        "#!/bin/sh\n"
        "echo 'tests/unit/test_x.py:544:        \"2026-05-21T... ERROR "
        "auth: authentication failed for OAuth token\",' 1>&2\n"
        "sleep 0.3\n"
        "exit 0\n"
    )
    script.chmod(0o755)
    r = hg.invoke(
        [str(script)], prompt="ignored",
        idle_timeout_s=10, absolute_max_runtime_s=10,
        halt_patterns=[auth_pat],
    )
    assert r.classification == "success"
    assert r.halt_required is False


def test_phase2_no_halt_patterns_keeps_legacy_behavior(tmp_path):
    """legacy/default: invoke() called WITHOUT halt_patterns
    behaves exactly as before. halt_required is always False."""
    hg = HealthGuard(cwd=tmp_path)
    script = tmp_path / "fake_ok.sh"
    script.write_text(
        "#!/bin/sh\necho hi\nexit 0\n"
    )
    script.chmod(0o755)
    r = hg.invoke(
        [str(script)], prompt="ignored",
        idle_timeout_s=10, absolute_max_runtime_s=10,
    )
    assert r.classification == "success"
    assert r.halt_required is False


def test_invoke_returns_promptly_when_grandchild_holds_pipe(tmp_path: Path):
    """regression: a peer that
    spawns a grandchild which inherits stdio AND leaves the process
    group (setsid) used to make the substrate's reader threads block
    in os.read() until the join timeout (30s) fired and the daemon
    threads leaked, eventually triggering 'can't start new thread'
    after enough ticks.

    The fixture grandchild sleeps 5s with the parent's stdout pipe
    open after the parent has exited cleanly with returncode 0. With
    the request_stop mechanism, invoke() must return well before the
    grandchild's 5s sleep elapses (parent exit + ~2s grace + signal
    + ~0.25s select-poll = ~3s total)."""
    import time
    hg = HealthGuard(cwd=tmp_path)
    threads_before = threading.active_count()
    t0 = time.monotonic()
    r = hg.invoke([str(FIX / "fake_cli_grandchild_holds_pipe.sh")],
                  prompt="ignored",
                  idle_timeout_s=30, absolute_max_runtime_s=15)
    dt = time.monotonic() - t0

    # Parent exited 0 with output → success classification.
    assert r.classification == "success", (r.classification, r.stdout, r.stderr)
    assert r.exit_code == 0
    assert "hello from parent" in r.stdout

    # Without request_stop this would be ~5s (grandchild sleep) or 30s
    # (join timeout). With it, ~3s upper bound.
    assert dt < 4.0, (
        f"invoke took {dt:.2f}s — reader threads likely blocked on "
        "grandchild-held pipe; request_stop / select-poll isn't "
        "shutting them down."
    )

    # Reader threads must have actually exited, not just been
    # abandoned. Give them a brief moment after invoke returns.
    time.sleep(0.5)
    leaked = [
        t for t in threading.enumerate()
        if t.name.startswith("hg-reader-")
    ]
    assert not leaked, (
        f"reader threads leaked past invoke(): {[t.name for t in leaked]}"
    )
    # Sanity: total thread count returned close to pre-invoke level
    # (some pytest/system threads may have shifted; ±2 slack).
    threads_after = threading.active_count()
    assert threads_after <= threads_before + 2, (
        f"thread count grew from {threads_before} to {threads_after}"
    )


def test_ok_run_classified_success(tmp_path: Path):
    hg = HealthGuard(cwd=tmp_path)
    r = hg.invoke([str(FIX / "fake_cli_ok.sh")], prompt="ignored",
                  idle_timeout_s=30, absolute_max_runtime_s=5)
    assert r.classification == "success"
    assert r.exit_code == 0


def test_nonzero_exit_classified_process_fail(tmp_path: Path):
    hg = HealthGuard(cwd=tmp_path)
    r = hg.invoke([str(FIX / "fake_cli_fail.sh")], prompt="ignored",
                  idle_timeout_s=30, absolute_max_runtime_s=5)
    assert r.classification == "process-fail"
    assert r.exit_code == 1


def test_hang_classified_absolute_timeout(tmp_path: Path):
    """A child that sleeps silently long enough hits the absolute cap
    first (no output → also no idle reset, but absolute is shorter here)."""
    hg = HealthGuard(cwd=tmp_path)
    r = hg.invoke([str(FIX / "fake_cli_hang.sh")], prompt="ignored",
                  idle_timeout_s=30, absolute_max_runtime_s=2)
    # idle_timeout=30 > absolute=2, so absolute fires first.
    assert r.classification == "absolute-timeout"


def test_missing_executable_classified_process_fail(tmp_path: Path):
    hg = HealthGuard(cwd=tmp_path)
    r = hg.invoke(["/no/such/binary"], prompt="x",
                  idle_timeout_s=30, absolute_max_runtime_s=1)
    assert r.classification == "process-fail"


def test_non_executable_peer_binary_is_process_fail(tmp_path: Path):
    script = tmp_path / "not-executable.sh"
    script.write_text("#!/bin/sh\necho nope\n")
    script.chmod(0o644)
    hg = HealthGuard(cwd=tmp_path)

    r = hg.invoke([str(script)], prompt="x",
                  idle_timeout_s=30, absolute_max_runtime_s=1)

    assert r.classification == "process-fail"
    assert r.exit_code == 126


def test_tolerates_non_utf8_output(tmp_path: Path):
    """A tool emitting mojibake on stderr must not crash HealthGuard."""
    hg = HealthGuard(cwd=tmp_path)
    r = hg.invoke([str(FIX / "fake_cli_mojibake.sh")], prompt="x",
                  idle_timeout_s=30, absolute_max_runtime_s=5)
    assert r.classification == "success"
    assert "ok stdout" in r.stdout
    # stderr is allowed to contain the U+FFFD replacement or similar
    assert "broken bytes" in r.stderr


def test_argv_substitute_replaces_placeholder(tmp_path: Path):
    """In argv-substitute mode, the literal {PROMPT} in argv is replaced
    with the prompt string at invocation time. No stdin is written."""
    hg = HealthGuard(cwd=tmp_path)
    script = tmp_path / "echo.sh"
    script.write_text('#!/bin/sh\necho "got: $1"\nexit 0\n')
    script.chmod(0o755)
    r = hg.invoke([str(script), "{PROMPT}"], prompt="hello world",
                  prompt_mode="argv-substitute",
                  idle_timeout_s=10, absolute_max_runtime_s=5)
    assert r.classification == "success"
    assert "got: hello world" in r.stdout


def test_argv_substitute_without_placeholder_is_safe(tmp_path: Path):
    """argv with no {PROMPT} placeholder is a no-op for the prompt."""
    hg = HealthGuard(cwd=tmp_path)
    r = hg.invoke([str(FIX / "fake_cli_ok.sh")], prompt="hello",
                  prompt_mode="argv-substitute",
                  idle_timeout_s=10, absolute_max_runtime_s=5)
    assert r.classification == "success"


def test_slow_but_productive_run_is_not_killed_by_short_idle_timeout(
        tmp_path: Path):
    """A tool that takes 6s total but emits every 1s must NOT be killed
    by a 2s idle-timeout — it's productive."""
    hg = HealthGuard(cwd=tmp_path)
    r = hg.invoke(
        [str(FIX / "fake_cli_slow_with_output.sh")], prompt="",
        idle_timeout_s=2, absolute_max_runtime_s=30,
    )
    assert r.classification == "success", r.stderr
    assert "DONE" in r.stdout
    assert "progress 6" in r.stdout


def test_no_newline_progress_resets_idle_timeout(tmp_path: Path):
    script = tmp_path / "dots.py"
    script.write_text(
        "import sys, time\n"
        "for _ in range(5):\n"
        "    sys.stdout.write('.')\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.2)\n"
    )
    hg = HealthGuard(cwd=tmp_path)
    import sys as _sys

    r = hg.invoke([_sys.executable, str(script)], prompt="",
                  idle_timeout_s=0.4, absolute_max_runtime_s=5)

    assert r.classification == "success"
    assert r.stdout == "....."


def test_silent_child_killed_by_idle_timeout(tmp_path: Path):
    """If the tool goes silent past idle_timeout, kill + classify as
    idle-timeout."""
    hg = HealthGuard(cwd=tmp_path)
    r = hg.invoke(
        [str(FIX / "fake_cli_silent_then_done.sh")], prompt="",
        idle_timeout_s=1, absolute_max_runtime_s=30,
    )
    assert r.classification == "idle-timeout"


def test_absolute_max_runtime_still_caps(tmp_path: Path):
    """Even a constantly-talking child gets killed at the absolute cap."""
    chatty = tmp_path / "chatty.sh"
    chatty.write_text(
        "#!/bin/sh\nwhile true; do echo tick; sleep 0.2; done\n"
    )
    chatty.chmod(0o755)
    hg = HealthGuard(cwd=tmp_path)
    r = hg.invoke([str(chatty)], prompt="",
                  idle_timeout_s=10, absolute_max_runtime_s=2)
    assert r.classification == "absolute-timeout"


def test_error_pattern_in_output_classifies_as_api_error(tmp_path: Path):
    """A configured error pattern in stdout/stderr classifies as api-error
    and kills the child early."""
    hg = HealthGuard(cwd=tmp_path)
    r = hg.invoke(
        [str(FIX / "fake_cli_api_error.sh")], prompt="",
        idle_timeout_s=10, absolute_max_runtime_s=30,
        error_patterns=[r"Rate limit exceeded"],
    )
    assert r.classification == "api-error"
    assert "Rate limit" in r.stderr
    # which pattern fired must be surfaced for runs.jsonl.
    assert r.matched_error_pattern == r"Rate limit exceeded"
    assert "Rate limit exceeded" in r.matched_error_snippet


def test_error_pattern_matches_unterminated_line_before_idle(tmp_path: Path):
    script = tmp_path / "api_error_no_newline.py"
    script.write_text(
        "import sys, time\n"
        "sys.stderr.write('client failed: API error 503')\n"
        "sys.stderr.flush()\n"
        "time.sleep(5)\n"
    )
    hg = HealthGuard(cwd=tmp_path)
    import sys as _sys

    r = hg.invoke(
        [_sys.executable, str(script)], prompt="",
        idle_timeout_s=1, absolute_max_runtime_s=10,
        error_patterns=[r"API error 5[0-9][0-9]"],
    )

    assert r.classification == "api-error"
    assert r.matched_error_pattern == r"API error 5[0-9][0-9]"
    assert "API error 503" in r.matched_error_snippet


def test_no_error_pattern_match_leaves_pattern_empty(tmp_path: Path):
    """When the child exits cleanly, no api-error fields are populated."""
    hg = HealthGuard(cwd=tmp_path)
    r = hg.invoke(
        ["true"], prompt="",
        idle_timeout_s=10, absolute_max_runtime_s=30,
        error_patterns=[r"Rate limit exceeded"],
    )
    assert r.classification == "success"
    assert r.matched_error_pattern == ""
    assert r.matched_error_snippet == ""


def test_error_pattern_timeout_fails_fast(tmp_path: Path, monkeypatch):
    import peers.health_guard as hg_mod

    def slow_search(pat, _text):
        raise hg_mod._PatternSearchTimeout(pat.pattern)

    monkeypatch.setattr(hg_mod, "_search_pattern", slow_search)
    hg = HealthGuard(cwd=tmp_path)

    r = hg.invoke(
        ["sh", "-c", "echo hello; sleep 30"], prompt="",
        idle_timeout_s=10, absolute_max_runtime_s=30,
        error_patterns=[r"(a+)+$"],
    )

    assert r.classification == "process-fail"
    assert r.matched_error_pattern == r"(a+)+$"
    assert "timed out" in r.matched_error_snippet
    assert "error pattern timed out" in r.stderr


def test_stdin_mode_default_still_writes_stdin(tmp_path: Path):
    """Default mode (stdin) keeps existing test-fake behaviour."""
    hg = HealthGuard(cwd=tmp_path)
    script = tmp_path / "cat.sh"
    script.write_text('#!/bin/sh\ncat\nexit 0\n')
    script.chmod(0o755)
    r = hg.invoke([str(script)], prompt="hi from stdin",
                  prompt_mode="stdin",
                  idle_timeout_s=10, absolute_max_runtime_s=5)
    assert r.classification == "success"
    assert "hi from stdin" in r.stdout


def test_noisy_child_output_is_capped(tmp_path: Path):
    """H4: a child producing massive output should not exhaust RAM —
    the collector keeps a head/tail snapshot plus a truncation marker."""
    flood = tmp_path / "flood.py"
    flood.write_text(
        "import sys\n"
        "for i in range(60_000):\n"
        "    sys.stdout.write('X'*200 + chr(10))\n"
    )
    import sys as _sys
    hg = HealthGuard(cwd=tmp_path)
    r = hg.invoke([_sys.executable, str(flood)], prompt="",
                  prompt_mode="argv-substitute",
                  idle_timeout_s=10, absolute_max_runtime_s=30)
    assert r.classification == "success"
    # Output should be capped well below the actual ~12 MB
    assert len(r.stdout) < 4 * 1024 * 1024, len(r.stdout)
    assert "truncated" in r.stdout


def test_small_buffer_cap_truncates_few_large_lines(tmp_path: Path):
    script = tmp_path / "one_big_line.py"
    script.write_text("import sys\nsys.stdout.write('x' * 10000)\n")
    hg = HealthGuard(cwd=tmp_path)
    import sys as _sys

    r = hg.invoke([_sys.executable, str(script)], prompt="",
                  prompt_mode="argv-substitute",
                  idle_timeout_s=5, absolute_max_runtime_s=10,
                  buf_cap_bytes=1024)

    assert r.truncated is True
    assert "truncated" in r.stdout
    assert len(r.stdout.encode("utf-8", errors="replace")) <= 1024


def test_buffer_cap_counts_utf8_bytes(tmp_path: Path):
    script = tmp_path / "unicode_line.py"
    script.write_text("import sys\nsys.stdout.write('🙂' * 1000)\n")
    hg = HealthGuard(cwd=tmp_path)
    import sys as _sys

    r = hg.invoke([_sys.executable, str(script)], prompt="",
                  prompt_mode="argv-substitute",
                  idle_timeout_s=5, absolute_max_runtime_s=10,
                  buf_cap_bytes=2048)

    assert r.truncated is True
    assert len(r.stdout.encode("utf-8", errors="replace")) <= 2048


def test_scan_buffer_disabled_without_error_patterns_is_bounded():
    from peers.health_guard import _StreamCollector

    sc = _StreamCollector.__new__(_StreamCollector)
    sc.lock = threading.Lock()
    sc.buf = []
    sc._scan_buf = []
    sc._size = 0
    sc._scan_size = 0
    sc._scan_cursor = 0
    sc._truncated = False
    sc._cap_bytes = 1024
    sc._scan_enabled = False
    sc._scan_cap_bytes = 1024

    for _ in range(20):
        sc._append_chunk("x" * 4096, 4096)

    assert sc._scan_buf == []
    assert sc._scan_size == 0
    assert sc._size <= 1024


def test_scan_buffer_with_error_patterns_is_capped():
    from peers.health_guard import _StreamCollector

    sc = _StreamCollector.__new__(_StreamCollector)
    sc.lock = threading.Lock()
    sc.buf = []
    sc._scan_buf = []
    sc._size = 0
    sc._scan_size = 0
    sc._scan_cursor = 0
    sc._truncated = False
    sc._cap_bytes = 1024
    sc._scan_enabled = True
    sc._scan_cap_bytes = 1024

    for _ in range(20):
        sc._append_chunk("x" * 4096, 4096)

    assert sc._scan_size <= 1024
    assert len("".join(sc._scan_buf).encode()) <= 1024


def test_error_pattern_after_huge_prefix_still_matches(tmp_path: Path):
    script = tmp_path / "late_api_error.py"
    script.write_text(
        "import sys, time\n"
        "sys.stderr.write('x' * 50000)\n"
        "sys.stderr.write(' API error 502')\n"
        "sys.stderr.flush()\n"
        "time.sleep(5)\n"
    )
    hg = HealthGuard(cwd=tmp_path)
    import sys as _sys

    r = hg.invoke(
        [_sys.executable, str(script)], prompt="",
        idle_timeout_s=1, absolute_max_runtime_s=10,
        error_patterns=[r"API error 5[0-9][0-9]"],
        buf_cap_bytes=1024,
    )

    assert r.classification == "api-error"
    assert r.matched_error_pattern == r"API error 5[0-9][0-9]"
    assert "API error 502" in r.matched_error_snippet


def test_large_prompt_does_not_deadlock(tmp_path: Path):
    """C1: child writes >64 KiB to stdout BEFORE consuming stdin, plus
    we send a >64 KiB prompt. Without starting readers first, both
    sides block on full pipes."""
    script = tmp_path / "noisy.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.write('x' * 200_000)\n"
        "sys.stdout.flush()\n"
        "sys.stdin.read()\n"
        "print('done')\n"
    )
    hg = HealthGuard(cwd=tmp_path)
    import sys as _sys
    r = hg.invoke([_sys.executable, str(script)], prompt="y" * 200_000,
                  idle_timeout_s=5, absolute_max_runtime_s=15)
    assert r.classification == "success", f"deadlock: {r.classification}"
    assert "done" in r.stdout


def test_error_pattern_seen_only_after_reader_drain_classifies_as_api_error(
    tmp_path: Path, monkeypatch,
):
    """BUG-007: a child that writes the error pattern to stderr and
    exits before the reader thread has appended the bytes to the
    incremental scan-buffer must still classify as `api-error`.

    The poll loop's `scan_new()` can return `None` on every iteration
    when there is a race between child exit and reader drain (the
    bytes are still in the kernel pipe), and the loop will break on
    `rc is not None` before the pattern ever surfaces. After joining
    the reader threads, the pattern *is* in `stdout_col.text()` /
    `stderr_col.text()` — `invoke()` must rescan there or the api-error
    silently classifies as `success` and the orchestrator retries
    instead of backing off.

    We deterministically simulate the race by monkeypatching
    `_StreamCollector.scan_new` to always return `None`. The reader
    thread still populates `buf` so `text()` works; only the
    incremental scan is blinded. Pre-fix this test classifies as
    `success`; post-fix it classifies as `api-error`."""
    import peers.health_guard as hg_mod

    def blind_scan(_self, _patterns):
        return None

    monkeypatch.setattr(hg_mod._StreamCollector, "scan_new", blind_scan)

    hg = HealthGuard(cwd=tmp_path)
    r = hg.invoke(
        [str(FIX / "fake_cli_api_error.sh")], prompt="",
        idle_timeout_s=10, absolute_max_runtime_s=30,
        error_patterns=[r"Rate limit exceeded"],
    )
    assert r.classification == "api-error", (
        r.classification, r.stdout, r.stderr,
    )
    assert "Rate limit" in r.stderr
    assert r.matched_error_pattern == r"Rate limit exceeded"
    assert "Rate limit exceeded" in r.matched_error_snippet


def test_in_loop_match_records_matched_error_source_in_loop(tmp_path: Path):
    """BUG-007 audit-log layer: when the in-loop scan catches the
    pattern (the common case, no race), matched_error_source must be
    'in-loop' so operators can distinguish it from the post-join
    rescan path."""
    hg = HealthGuard(cwd=tmp_path)
    r = hg.invoke(
        [str(FIX / "fake_cli_api_error.sh")], prompt="",
        idle_timeout_s=10, absolute_max_runtime_s=30,
        error_patterns=[r"Rate limit exceeded"],
    )
    assert r.classification == "api-error"
    assert r.matched_error_source == "in-loop"


def test_post_join_rescan_records_matched_error_source_post_join(
    tmp_path: Path, monkeypatch,
):
    """BUG-007 audit-log layer: when the race blinds the in-loop scan
    and only the post-join rescan catches the pattern,
    matched_error_source must be 'post-join'. A non-trivial
    post-join frequency in runs.jsonl is the operational signal
    that scan_buf sizing or the race window needs investigation."""
    import peers.health_guard as hg_mod

    def blind_scan(_self, _patterns):
        return None

    monkeypatch.setattr(hg_mod._StreamCollector, "scan_new", blind_scan)

    hg = HealthGuard(cwd=tmp_path)
    r = hg.invoke(
        [str(FIX / "fake_cli_api_error.sh")], prompt="",
        idle_timeout_s=10, absolute_max_runtime_s=30,
        error_patterns=[r"Rate limit exceeded"],
    )
    assert r.classification == "api-error"
    assert r.matched_error_source == "post-join"


def test_no_pattern_match_leaves_matched_error_source_empty(tmp_path: Path):
    """BUG-007 audit-log layer: when no pattern matched (clean
    success or non-pattern process-fail), matched_error_source stays
    empty so the runs.jsonl entry doesn't pollute with a misleading
    'in-loop' / 'post-join' for cases that never matched anything."""
    hg = HealthGuard(cwd=tmp_path)
    r = hg.invoke(
        [str(FIX / "fake_cli_api_error.sh")], prompt="",
        idle_timeout_s=10, absolute_max_runtime_s=30,
    )
    assert r.classification == "success"
    assert r.matched_error_source == ""


def test_post_join_rescan_skipped_when_no_patterns(tmp_path: Path):
    """Sad-path companion to BUG-007: when no error_patterns are
    configured, the post-join rescan must NOT run (no patterns to
    test) and the classification must remain `success` for a clean
    rc==0 child. Guards against the rescan accidentally inventing an
    api-error classification when there's nothing to scan for."""
    hg = HealthGuard(cwd=tmp_path)
    r = hg.invoke(
        [str(FIX / "fake_cli_api_error.sh")], prompt="",
        idle_timeout_s=10, absolute_max_runtime_s=30,
    )
    assert r.classification == "success"
    assert r.matched_error_pattern == ""
    assert r.matched_error_snippet == ""


def test_post_join_rescan_honors_pattern_timeout(
    tmp_path: Path, monkeypatch,
):
    """Edge case for BUG-007 fix: if the post-join rescan's regex
    times out (catastrophic backtracking), the result must surface as
    `process-fail` with the timeout diagnostic, NOT silently fall
    back to `success`. Mirrors the in-loop timeout handling so a bad
    regex can't bypass either scan path."""
    import peers.health_guard as hg_mod

    def blind_scan(_self, _patterns):
        return None

    monkeypatch.setattr(hg_mod._StreamCollector, "scan_new", blind_scan)

    real_search = hg_mod._search_pattern
    call_count = {"n": 0}

    def search_with_late_timeout(pat, text):
        call_count["n"] += 1
        # First call comes from the post-join rescan; trigger the
        # timeout there to exercise the new path.
        raise hg_mod._PatternSearchTimeout(pat.pattern)

    monkeypatch.setattr(hg_mod, "_search_pattern", search_with_late_timeout)

    hg = HealthGuard(cwd=tmp_path)
    r = hg.invoke(
        [str(FIX / "fake_cli_api_error.sh")], prompt="",
        idle_timeout_s=10, absolute_max_runtime_s=30,
        error_patterns=[r"Rate limit exceeded"],
    )
    assert r.classification == "process-fail"
    assert r.matched_error_pattern == r"Rate limit exceeded"
    assert "timed out" in r.matched_error_snippet
    assert "error pattern timed out" in r.stderr
    # Reference real_search so the import isn't unused if we
    # restructure later — kept here as a sanity reference.
    assert real_search is not None


def test_keyboard_interrupt_terminates_child(tmp_path: Path, monkeypatch):
    """SIGINT during the poll loop must kill the subprocess and re-raise."""
    import peers.health_guard as hg_mod
    hg = HealthGuard(cwd=tmp_path)

    real_sleep = hg_mod.time.sleep
    call_count = {"n": 0}

    def fake_sleep(s):
        # First poll-loop sleep: deliver SIGINT.
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise KeyboardInterrupt()
        real_sleep(s)

    monkeypatch.setattr(hg_mod.time, "sleep", fake_sleep)

    with pytest.raises(KeyboardInterrupt):
        hg.invoke([str(FIX / "fake_cli_hang.sh")], prompt="x",
                  idle_timeout_s=30, absolute_max_runtime_s=30)
