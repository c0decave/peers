"""Wave-1a: argv builders + no-shell verb runner + doctor preflight.

All builders return list-arg argv (never a shell string); run_verb shells the
real peers-ctl verb without shell=True. Positional/flag names are asserted
against the live cli.py build_parser contract:
  - start/stop  -> positional `name`
  - resume/amend -> positional `project_name`
  - ack-block    -> positionals `project_name` then `step_id`
  - new          -> positional `path`; --modes is a comma-joined CSV
"""

from __future__ import annotations

import sys

from peers_ctl.tui import actions as A


# --------------------------------------------------------------------------- #
# Task 14: argv builders                                                       #
# --------------------------------------------------------------------------- #
def test_build_start_argv_happy():
    argv = A.build_start_argv("proj", max_ticks=10, max_runtime="4h", container=False)
    assert argv[:3] == [sys.executable, "-m", "peers_ctl"]
    # start's positional is `name`, placed right after the verb.
    assert argv[argv.index("start") + 1] == "proj"
    assert "--max-ticks" in argv and "10" in argv
    assert "--max-runtime" in argv and "4h" in argv
    assert "--container" not in argv  # host


def test_build_start_argv_sad_minimal():
    # No optional args -> just base + verb + positional, nothing else.
    argv = A.build_start_argv("proj")
    assert argv == [sys.executable, "-m", "peers_ctl", "start", "proj"]


def test_build_start_argv_edge_all_flags_and_config_dir():
    argv = A.build_start_argv(
        "proj",
        max_ticks=5,
        max_usd=2.5,
        max_runtime="6h",
        reset_budget=True,
        container=True,
        checkpoint=True,
        config_dir="/cfg",
    )
    # --config-dir must precede the verb.
    assert argv[3:5] == ["--config-dir", "/cfg"]
    assert argv[argv.index("start") + 1] == "proj"
    assert "--max-ticks" in argv and "5" in argv
    assert "--max-usd" in argv and "2.5" in argv
    assert "--max-runtime" in argv and "6h" in argv
    assert "--reset-budget" in argv
    assert "--container" in argv
    assert "--checkpoint" in argv


def test_build_stop_argv_happy():
    argv = A.build_stop_argv("proj", grace_s=10.0)
    assert argv[argv.index("stop") + 1] == "proj"
    assert "--grace-s" in argv and "10.0" in argv


def test_build_stop_argv_sad_no_grace():
    argv = A.build_stop_argv("proj")
    assert argv == [sys.executable, "-m", "peers_ctl", "stop", "proj"]


def test_build_resume_argv_positional_is_project_name():
    argv = A.build_resume_argv("proj")
    assert argv == [sys.executable, "-m", "peers_ctl", "resume", "proj"]
    assert argv[argv.index("resume") + 1] == "proj"


def test_build_resume_argv_with_config_dir():
    argv = A.build_resume_argv("proj", config_dir="/cfg")
    assert argv == [
        sys.executable, "-m", "peers_ctl", "--config-dir", "/cfg", "resume", "proj",
    ]


def test_build_peek_argv_happy():
    argv = A.build_peek_argv("proj")
    assert argv == [sys.executable, "-m", "peers_ctl", "peek", "proj"]


def test_build_peek_argv_with_config_dir_and_session():
    argv = A.build_peek_argv("proj", session="sess1", config_dir="/cfg")
    assert argv == [
        sys.executable, "-m", "peers_ctl", "--config-dir", "/cfg",
        "peek", "proj", "--session", "sess1",
    ]
    # the name lands as a positional, never as a flag.
    assert argv[argv.index("peek") + 1] == "proj"


def test_build_peek_argv_allows_dotted_dashed_slug():
    # edge: a conservative slug with dots/underscores/internal dashes is fine.
    argv = A.build_peek_argv("my_proj-2.0")
    assert argv == [sys.executable, "-m", "peers_ctl", "peek", "my_proj-2.0"]


def test_build_peek_argv_refuses_flag_like_name():
    # sad path: a leading-dash name (looks like a flag) is refused -> None,
    # never shelled.
    assert A.build_peek_argv("--foo") is None
    assert A.build_peek_argv("-x") is None


def test_build_peek_argv_refuses_empty_or_illegal_chars():
    # sad/edge: empty, whitespace, and out-of-charset names are all refused.
    assert A.build_peek_argv("") is None
    assert A.build_peek_argv("a b") is None
    assert A.build_peek_argv("a/b") is None
    assert A.build_peek_argv("a;rm -rf") is None
    assert A.build_peek_argv("a$(whoami)") is None


def test_build_ack_block_uses_both_positionals():
    # ack-block's positionals are project_name THEN step_id (verified in cli.py).
    argv = A.build_ack_block_argv(
        project_name="proj", step_id="STEP-3", reason="external dep missing"
    )
    i = argv.index("ack-block")
    assert argv[i + 1] == "proj"     # project_name first
    assert argv[i + 2] == "STEP-3"   # step_id second
    assert "--reason" in argv and "external dep missing" in argv


def test_build_ack_block_sad_with_config_dir():
    argv = A.build_ack_block_argv(
        project_name="proj", step_id="STEP-1", reason="r", config_dir="/cfg"
    )
    assert argv[3:5] == ["--config-dir", "/cfg"]
    i = argv.index("ack-block")
    assert argv[i + 1] == "proj" and argv[i + 2] == "STEP-1"


def test_build_amend_argv_happy():
    argv = A.build_amend_argv(project_name="proj", acceptance="pytest -q", reason="re-pin")
    assert argv[argv.index("amend") + 1] == "proj"  # positional is project_name
    assert "--acceptance" in argv and "pytest -q" in argv
    assert "--reason" in argv and "re-pin" in argv


def test_build_new_argv_minimal():
    argv = A.build_new_argv(path="/tmp/proj")
    assert argv == [sys.executable, "-m", "peers_ctl", "new", "/tmp/proj"]


def test_build_new_argv_modes_csv_join_and_flags():
    argv = A.build_new_argv(
        path="/tmp/proj",
        modes=["audit", "security"],
        driver="orchestrator",
        lang="python",
        plan="/tmp/PLAN.md",
        template="internal testing",
        peer_model="opus",
        peer_reasoning="high",
        peer_provider="anthropic",
        config_dir="/cfg",
    )
    assert argv[3:5] == ["--config-dir", "/cfg"]
    assert argv[argv.index("new") + 1] == "/tmp/proj"
    # --modes takes a single comma-joined CSV value, not repeated flags.
    assert "--modes" in argv
    assert argv[argv.index("--modes") + 1] == "audit,security"
    assert argv[argv.index("--driver") + 1] == "orchestrator"
    assert argv[argv.index("--lang") + 1] == "python"
    assert argv[argv.index("--plan") + 1] == "/tmp/PLAN.md"
    assert argv[argv.index("--template") + 1] == "internal testing"
    assert argv[argv.index("--peer-model") + 1] == "opus"
    assert argv[argv.index("--peer-reasoning") + 1] == "high"
    assert argv[argv.index("--peer-provider") + 1] == "anthropic"


def test_build_new_argv_edge_host_vs_container():
    host = A.build_new_argv(path="/tmp/p", container=False)
    cont = A.build_new_argv(path="/tmp/p", container=True)
    assert "--container" not in host
    assert "--container" in cont


def test_no_shell_string_anywhere():
    # Builders must return list-arg argv, never a single shell string.
    for argv in (A.build_stop_argv("p"), A.build_resume_argv("p")):
        assert isinstance(argv, list) and all(isinstance(x, str) for x in argv)


# --------------------------------------------------------------------------- #
# Task 15: run_verb (no shell)                                                 #
# --------------------------------------------------------------------------- #
def test_run_verb_help_ok():
    res = A.run_verb([sys.executable, "-m", "peers_ctl", "--help"], timeout=30)
    assert res.rc == 0
    assert "usage" in (res.stdout + res.stderr).lower()
    assert res.timed_out is False


def test_run_verb_bad_verb_nonzero():
    res = A.run_verb(
        [sys.executable, "-m", "peers_ctl", "definitely-not-a-verb"], timeout=30
    )
    assert res.rc != 0
    assert res.timed_out is False


def test_run_verb_timeout_edge():
    # A tiny timeout against a sleeping command -> timed_out True, rc 124.
    res = A.run_verb(
        [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2
    )
    assert res.timed_out is True
    assert res.rc == 124


def test_run_verb_missing_binary_returns_127():
    # A bad argv[0] (missing interpreter/binary) must NOT raise; the fail-soft
    # contract returns a structured rc=127 ("command not found") result.
    res = A.run_verb(["/nonexistent/definitely-not-a-binary-xyz"], timeout=5)
    assert res.rc == 127
    assert res.timed_out is False


def test_run_verb_is_frozen_dataclass():
    res = A.run_verb([sys.executable, "-c", "print('hi')"], timeout=30)
    assert res.rc == 0 and res.stdout.strip() == "hi"
    import dataclasses
    assert dataclasses.is_dataclass(res)
    try:
        res.rc = 99  # frozen -> must raise
        raised = False
    except dataclasses.FrozenInstanceError:
        raised = True
    assert raised


# --------------------------------------------------------------------------- #
# Task 16: doctor_preflight                                                    #
# --------------------------------------------------------------------------- #
def test_doctor_preflight_structured_from_canned(monkeypatch):
    canned = A.VerbResult(rc=0, stdout="ok line 1\nok line 2", stderr="", timed_out=False)
    monkeypatch.setattr(A, "run_verb", lambda *a, **k: canned)
    res = A.doctor_preflight()
    assert isinstance(res, A.DoctorResult)
    assert res.ok is True
    assert res.rc == 0
    assert "ok line 1" in res.lines and "ok line 2" in res.lines


def test_doctor_preflight_ok_false_on_nonzero(monkeypatch):
    canned = A.VerbResult(rc=2, stdout="bad", stderr="err line", timed_out=False)
    monkeypatch.setattr(A, "run_verb", lambda *a, **k: canned)
    res = A.doctor_preflight(config_dir="/cfg")
    assert res.ok is False
    assert res.rc == 2
    assert "bad" in res.lines and "err line" in res.lines


def test_doctor_preflight_builds_argv_with_config_dir(monkeypatch):
    captured = {}

    def fake_run_verb(argv, **kwargs):
        captured["argv"] = argv
        return A.VerbResult(rc=0, stdout="", stderr="", timed_out=False)

    monkeypatch.setattr(A, "run_verb", fake_run_verb)
    A.doctor_preflight(config_dir="/cfg")
    argv = captured["argv"]
    assert argv[:3] == [sys.executable, "-m", "peers_ctl"]
    assert argv[3:5] == ["--config-dir", "/cfg"]
    assert argv[-1] == "doctor"


def test_doctor_preflight_real_smoke():
    # End-to-end: real doctor invocation must return a DoctorResult without raising.
    res = A.doctor_preflight()
    assert isinstance(res, A.DoctorResult)
    assert isinstance(res.ok, bool)
    assert isinstance(res.lines, list)
