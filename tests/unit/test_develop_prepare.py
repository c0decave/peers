"""STEP-2 — ``DevelopFrontend.prepare`` infers the quality bar.

``prepare`` delegates to :func:`peers.spine.direction.infer_bar` (passing the
REQUIRED ``ledger=`` kwarg so the ``bar-inferred`` row is recorded) and records
whether the bar is trustworthy. An **absent** bar blocks ALL work: ``run()``
then emits only a dry round (an honest stop — never silent freehand work over a
tool with no trustworthy baseline).
"""
from __future__ import annotations

from pathlib import Path

from peers.develop.frontend import DevelopFrontend
from peers.spine.mode_run import ModeRun
from peers.spine.op_config import OpConfig

from tests.unit._develop_helpers import _NullAuditor, _NullAuthor, _NullImpl


def _fe(**kw) -> DevelopFrontend:
    base = dict(auditor=_NullAuditor(), author=_NullAuthor(),
                implementer=_NullImpl(), dimensions=["correctness"],
                run_tests=lambda cmd: (0, "1 passed"))
    base.update(kw)
    return DevelopFrontend(**base)


def _modrun(tmp_path) -> ModeRun:
    return ModeRun(tool=tmp_path,
                   op_config=OpConfig.from_dict({"mode": "develop"}),
                   ledger_path=tmp_path / "run.jsonl", mode_run="r1")


# ---- happy ---------------------------------------------------------------
def test_prepare_records_present_bar(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    run = _modrun(tmp_path)
    fe = _fe()
    fe.prepare(run)
    assert fe.bar.kind == "present"
    assert fe._blocked is False
    rows = run.ledger.read()
    bar_rows = [r for r in rows if r.event == "bar-inferred"]
    assert len(bar_rows) == 1                      # exactly one row (ledger= wired)
    assert bar_rows[0].status == "pass"
    assert bar_rows[0].witness["bar"] == "present"


# ---- CB-4: the bar runner must target run.tool at call time --------------
def test_prepare_bar_runner_binds_to_run_tool_at_call_time(tmp_path):
    # CB-4 (fleet-only): in a fleet run the leased worktree (run.tool) != the repo
    # bound at construction. When a run_tests_factory is injected (the production
    # default), prepare() must build the bar runner against run.tool at call time
    # -- never a repo frozen at construction. The construction-bound run_tests
    # (which here would FAIL the bar) must NOT be the one used.
    seen: list[Path] = []

    def factory(repo):
        seen.append(Path(repo))
        return lambda cmd: (0, "1 passed")

    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    run = _modrun(tmp_path)
    fe = _fe(run_tests=lambda cmd: (1, "WRONG: construction-bound runner used"),
             run_tests_factory=factory)
    fe.prepare(run)
    assert seen == [tmp_path]                 # factory was bound to run.tool
    assert fe.bar.kind == "present"           # used the factory runner, not the wrong one


# ---- sad: no trustworthy bar blocks work --------------------------------
def test_prepare_absent_bar_blocks_work(tmp_path):
    # no runner marker at all -> infer_bar detects no runner -> absent.
    run = _modrun(tmp_path)
    fe = _fe(run_tests=lambda cmd: None)
    fe.prepare(run)
    assert fe.bar.kind == "absent" and fe._blocked is True


def test_prepare_runner_present_but_unrunnable_baseline_is_absent(tmp_path):
    # sad: a runner IS detected (pyproject) but the injected baseline cannot
    # run (returns None) -> classify() fail-closes to absent -> blocked.
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    run = _modrun(tmp_path)
    fe = _fe(run_tests=lambda cmd: None)
    fe.prepare(run)
    assert fe.bar.kind == "absent" and fe._blocked is True
    assert [r.event for r in run.ledger.read() if r.event == "bar-inferred"]


# ---- edge: a red baseline is "weak", which Stage 1 does NOT block --------
def test_prepare_weak_bar_is_recorded_and_does_not_block(tmp_path):
    # A non-zero baseline classifies "weak" (red/flaky). Per the Stage-1
    # contract (direction.py: weak/absent is the caller's signal; only the
    # detector ships in Stage 0/1), only an *absent* bar blocks. This pins
    # that weak does NOT block -- see CONCERNS.md (weak-bar-does-not-block).
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    run = _modrun(tmp_path)
    fe = _fe(run_tests=lambda cmd: (1, "1 failed"))
    fe.prepare(run)
    assert fe.bar.kind == "weak" and fe._blocked is False
    assert [r.status for r in run.ledger.read()
            if r.event == "bar-inferred"] == ["weak"]


# ---- a blocked run does nothing but emit one dry round -------------------
def test_blocked_run_emits_only_a_dry_round(tmp_path):
    run = _modrun(tmp_path)
    fe = _fe(run_tests=lambda cmd: None)        # absent -> blocked
    fe.prepare(run)
    fe.run(run)
    rows = run.ledger.read()
    assert rows[-1].event == "dry-round" and rows[-1].status == "dry"
    # blocked -> no auditing/gate/confirmed-work happened.
    assert not any(r.event in ("gate", "confirmed-work", "landing") for r in rows)
