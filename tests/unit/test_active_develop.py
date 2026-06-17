"""ACTIVE end-to-end tests for the ``peers develop`` operator verb.

Plan: docs/plans/2026-06-15-new-feature-active-test-plans.md, section 1
(cases DEV-HAPPY-01 .. DEV-EDGE-NOCONVERGE-09).

These drive the REAL operator entry ``peers.cli.cmd_develop`` over throwaway git
repos, through the documented deterministic seam ``cmd_develop(_make_frontend=)``
(cli.py:2571) + the real ``make_develop_frontend`` assembly with deterministic
python agents (no live LLM, no container). The FULL audit -> adversarial-verify
-> author -> freeze -> converge -> commit -> attest -> confirmed-work ->
spine-gate chain runs for real; only the IMPLEMENT turn is varied per case.

The load-bearing assertions are the HONESTY checks: trust is RE-DERIVED from the
substrate (a real git diff + an attested commit reachable from the chosen head,
the author re-resolved from refs/notes/peers-attest) — NEVER from a self-reported
ledger field. A green here means a real attested commit exists; a correct-refusal
green means NO confirmed-work row and NO new commit/note leaked.
"""
from __future__ import annotations

import io
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from peers.attest import attested_peer
from peers.cli import cmd_develop
from peers.spine.gates import evaluate_spine_gates
from peers.spine.ledger import RunLedger
from peers.spine.op_config import OpConfig

from tests.unit._active_develop_fixtures import (
    git,
    happy_frontend_builder,
    make_repo,
    nobar_frontend_builder,
    noconverge_frontend_builder,
    noop_frontend_builder,
    tamper_frontend_builder,
)

# ---- shared honesty helpers -------------------------------------------------


def _read_rows(repo: Path):
    return RunLedger(repo / ".peers" / "run.jsonl").read()


def _commit_count(repo: Path) -> int:
    return int(git(repo, "rev-list", "--count", "HEAD"))


def _gates(repo: Path, rows):
    # mode_run is f"develop-{repo.name}" (cli.py:2569). For a single-repo develop
    # run the convergence runner commits on the repo's current branch, so the
    # authorship gate's default head="HEAD" is the right run tip.
    return evaluate_spine_gates(rows, mode_run=f"develop-{repo.name}", repo=repo)


# ---- DEV-HAPPY-01 -----------------------------------------------------------


def test_dev_happy_01(tmp_path):
    """A deterministic-fake develop run converges one finding to a REAL,
    substrate-ATTESTED confirmed-work commit; every spine gate re-derives True."""
    repo = make_repo(tmp_path)
    base_sha = git(repo, "rev-parse", "HEAD")

    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_develop(repo, dimensions=["correctness"],
                         _make_frontend=happy_frontend_builder(budget=3))
    assert rc == 0
    assert "peers develop: complete" in out.getvalue()

    rows = _read_rows(repo)
    cw = [r for r in rows if r.event == "confirmed-work"]
    landing = [r for r in rows if r.event == "landing"]
    assert len(cw) >= 1, "expected a confirmed-work row"
    assert len(landing) >= 1, "expected a landing row"

    # HONESTY: a NEW commit exists whose sha == the confirmed-work witness uri,
    # AND that commit carries a real peers-attest note for the peer. Re-derive
    # everything from the substrate, never from the row's `author` text.
    assert _commit_count(repo) > _commit_count_at(repo, base_sha), \
        "a new commit must exist beyond base"
    witness_sha = cw[-1].witness["uri"]
    head_sha = git(repo, "rev-parse", "HEAD")
    assert witness_sha == head_sha, "witness must point at the real new HEAD commit"
    assert witness_sha != base_sha
    assert attested_peer(repo, witness_sha) == "claude", \
        "the new commit must carry a real peers-attest note resolving to the peer"
    # the row's own author was re-derived from that note by append_attested.
    assert cw[-1].author == "claude"

    # HONESTY: every spine gate re-derives True over the produced ledger.
    g = _gates(repo, rows)
    assert g["ModeRun-valid"] is True
    assert g["witness-ledgered"] is True       # git-sha witness re-resolves
    assert g["authorship-attested"] is True    # author re-derived via attest_sha
    assert g["stop-on-dry"] is True
    assert all(g.values()) is True


def _commit_count_at(repo: Path, sha: str) -> int:
    return int(git(repo, "rev-list", "--count", sha))


# ---- DEV-HONESTY-NOOP-02 ----------------------------------------------------


def test_dev_honesty_noop_02(tmp_path):
    """PRIMARY honesty test: a no-op/lying implement agent that reports success
    but produces NO diff must NOT yield an attested confirmed-work commit — the
    round degrades to an honest dry-round."""
    repo = make_repo(tmp_path)
    base_sha = git(repo, "rev-parse", "HEAD")

    rc = cmd_develop(repo, dimensions=["correctness"],
                     _make_frontend=noop_frontend_builder(budget=2))
    assert rc == 0  # honest-dry is still a clean exit

    rows = _read_rows(repo)
    # correct refusal: ZERO confirmed-work, terminal stop/dry, no new commit/note.
    assert not any(r.event == "confirmed-work" for r in rows), \
        "a no-op agent must not forge a confirmed-work row"
    assert rows[-1].event == "stop" and rows[-1].status == "dry"
    assert git(repo, "rev-parse", "HEAD") == base_sha, "no new commit"
    assert _commit_count(repo) == _commit_count_at(repo, base_sha)
    assert attested_peer(repo, base_sha) is None, "no attest note was written"

    # HONESTY: authorship-attested is True only VACUOUSLY (no independence rows to
    # bless) — there is no confirmed-work to scrutinise; witness-ledgered is False
    # (nothing witnessed) so the suite is NOT all-pass — there was no real work.
    g = _gates(repo, rows)
    assert g["authorship-attested"] is True
    assert g["witness-ledgered"] is False


# ---- DEV-HONESTY-UNATTESTED-03 ----------------------------------------------


def test_dev_honesty_unattested_03(tmp_path):
    """A REAL converged commit that lacks a peers-attest note must NOT green the
    loop: confirmed-work resolves author=None, the authorship gate is False, and
    the dry streak is not reset. Driven through cmd_develop via an injected
    _make_frontend that wires a real DevelopFrontend over a real UNATTESTED commit
    (the deterministic injected variant the e2e test locks)."""
    from peers.develop.frontend import DevelopFrontend
    from peers.develop.ports import AuthoredContract, ImplementResult
    from peers.spine.stop_on_dry import dry_streak

    from tests.unit._develop_helpers import (
        _F, _FixedAuditor, _FixedAuthor, _FixedImpl, _repo_with_commit,
    )

    repo = tmp_path / "proj"
    repo.mkdir()
    unattested_sha = _repo_with_commit(repo)          # real commit, NO attest note
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")  # present bar

    def builder(r: Path):
        return DevelopFrontend(
            _FixedAuditor([_F("F1")]),                # confirmable every round
            _FixedAuthor(AuthoredContract(plan_md="# p", acceptance="pytest -q",
                                          findings=["F1"])),
            _FixedImpl(ImplementResult(ok=True, head_sha=unattested_sha,
                                       branch="feat/x")),
            dimensions=["correctness"],
            run_tests=lambda c: (0, "1 passed"),
            refuter_factory=lambda f: (lambda i: False))

    rc = cmd_develop(repo, dimensions=["correctness"], _make_frontend=builder)
    assert rc == 0

    rows = _read_rows(repo)
    cw = [r for r in rows if r.event == "confirmed-work"]
    # HONESTY: a confirmed-work row exists (auditable) but its author is None —
    # re-derived from the ABSENT note, not the caller. The independence=True
    # literal is load-bearing: it forces the gate to scrutinise this row.
    assert cw, "the unattested confirm row is still written (auditable)"
    assert cw[-1].author is None, "unattested commit -> author re-derives None"
    assert cw[-1].independence is True
    assert dry_streak(rows) >= OpConfig.from_dict({"mode": "develop"}).dry_n, \
        "a fake confirm must NOT reset the dry streak"
    assert rows[-1].event == "stop" and rows[-1].status == "dry"

    g = _gates(repo, rows)
    assert g["authorship-attested"] is False, \
        "an unattested independent commit must fail the authorship gate"


# ---- DEV-HONESTY-TAMPER-04 --------------------------------------------------


def test_dev_honesty_tamper_04(tmp_path):
    """An agent that rewrites the frozen acceptance.sh to force a pass is caught
    by the frozen-contract sha re-verification and fails CLOSED — no confirm."""
    repo = make_repo(tmp_path)
    base_sha = git(repo, "rev-parse", "HEAD")

    rc = cmd_develop(repo, dimensions=["correctness"],
                     _make_frontend=tamper_frontend_builder(budget=2))
    assert rc == 0  # honest refusal -> clean dry exit

    rows = _read_rows(repo)
    assert not any(r.event == "confirmed-work" for r in rows), \
        "tampering the frozen acceptance must not produce a confirm"
    assert rows[-1].event == "stop" and rows[-1].status == "dry"
    # HONESTY: trust is bound to the frozen pin, not the agent's mutable script —
    # no new attested commit landed off the tampered oracle.
    assert git(repo, "rev-parse", "HEAD") == base_sha, "no new commit"
    assert attested_peer(repo, base_sha) is None
    assert _gates(repo, rows)["witness-ledgered"] is False


# ---- DEV-SAD-DIMS-05 --------------------------------------------------------


def test_dev_sad_dims_05(tmp_path):
    """Missing/empty --dimensions fails closed with exit 2 and a clear message —
    never silently audits 'everything'."""
    repo = make_repo(tmp_path)
    err = io.StringIO()
    with redirect_stderr(err):
        rc = cmd_develop(repo, dimensions=[],
                         _make_frontend=happy_frontend_builder())
    assert rc == 2
    assert "peers develop: --dimensions is required" in err.getvalue()
    # HONESTY: fail-closed BEFORE any frontend construction or round -> no ledger,
    # so no confirmed-work could possibly be produced from an empty mandate.
    assert not (repo / ".peers" / "run.jsonl").exists()


# ---- DEV-SAD-NOCONFIG-06 ----------------------------------------------------


def test_dev_sad_noconfig_06(tmp_path):
    """A repo with no .peers/config.yaml fails closed with exit 1 and a message,
    not a traceback (production non-injected path)."""
    repo = make_repo(tmp_path)
    err = io.StringIO()
    with redirect_stderr(err):
        # NO _make_frontend -> the real _build_develop_frontend_from_config path,
        # which raises ValueError('missing .peers/config.yaml ...') -> rc 1.
        rc = cmd_develop(repo, dimensions=["correctness"])
    assert rc == 1
    assert "missing .peers/config.yaml" in err.getvalue()
    # HONESTY: construction failure -> no frontend, no rounds, no confirm row.
    rows = _read_rows(repo) if (repo / ".peers" / "run.jsonl").exists() else []
    assert not any(r.event == "confirmed-work" for r in rows)


def test_dev_sad_noconfig_06b_unknown_peer(tmp_path):
    """A valid config but an unknown --peer fails closed with exit 1, never a
    silent fallback to peer[0]."""
    repo = make_repo(tmp_path)
    (repo / ".peers").mkdir(exist_ok=True)
    (repo / ".peers" / "config.yaml").write_text(
        "driver: orchestrator\ncomm: git\n"
        "peers:\n  - name: claude\n    tool: claude\n"
        '    argv: ["claude", "-p", "{PROMPT}"]\n    prompt_mode: argv-substitute\n'
        "budget: {max_iterations: 1, max_runtime_s: 60, max_consecutive_failures: 1}\n",
        encoding="utf-8")
    err = io.StringIO()
    with redirect_stderr(err):
        rc = cmd_develop(repo, dimensions=["correctness"], peer="ghost")
    assert rc == 1
    assert "peer 'ghost' not found in config" in err.getvalue()


# ---- DEV-EDGE-EMPTYREPO-07 --------------------------------------------------


def test_dev_edge_emptyrepo_07(tmp_path):
    """An empty git repo (.git but zero commits) is rejected up front with exit 1,
    not a mid-run crash."""
    repo = tmp_path / "emptyrepo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / ".peers").mkdir()
    (repo / ".peers" / "config.yaml").write_text("driver: orchestrator\n",
                                                 encoding="utf-8")
    err = io.StringIO()
    with redirect_stderr(err):
        # validate_git_repo runs BEFORE _make_frontend, so even with a builder the
        # empty-repo guard fires first.
        rc = cmd_develop(repo, dimensions=["correctness"],
                         _make_frontend=happy_frontend_builder())
    assert rc == 1
    msg = err.getvalue()
    assert "peers develop:" in msg and "no commits" in msg
    # HONESTY: fail-closed before any agent runs -> the attest path is unreachable.
    assert not (repo / ".peers" / "run.jsonl").exists()


# ---- DEV-EDGE-NOBAR-08 ------------------------------------------------------


def test_dev_edge_nobar_08(tmp_path):
    """A repo with an ABSENT quality bar produces only honest dry-rounds and
    stops — develop never edits freehand against a tool with no trustworthy bar."""
    # with_bar=False -> no pyproject/test marker -> _detect_runner None -> absent;
    # the builder also injects run_tests=None as a second absent-bar layer.
    repo = make_repo(tmp_path, name="nobar", with_bar=False)
    base_sha = git(repo, "rev-parse", "HEAD")

    rc = cmd_develop(repo, dimensions=["correctness"],
                     _make_frontend=nobar_frontend_builder(budget=2))
    assert rc == 0

    rows = _read_rows(repo)
    assert any(r.event == "bar-inferred" and r.status == "absent" for r in rows), \
        "the bar must be classified absent"
    assert any(r.event == "dry-round" for r in rows)
    assert rows[-1].event == "stop" and rows[-1].status == "dry"
    # HONESTY: ZERO confirmed-work, ZERO new commits, ZERO attest notes — the
    # audit/author/implement seams were never reached.
    assert not any(r.event == "confirmed-work" for r in rows)
    assert git(repo, "rev-parse", "HEAD") == base_sha
    assert attested_peer(repo, base_sha) is None


# ---- DEV-EDGE-NOCONVERGE-09 -------------------------------------------------


def test_dev_edge_noconverge_09(tmp_path):
    """When the implement agent never makes acceptance pass within
    --convergence-budget, the run terminates honestly with no confirmed-work
    (budget exhaustion, not a forged pass)."""
    repo = make_repo(tmp_path)
    base_sha = git(repo, "rev-parse", "HEAD")

    rc = cmd_develop(repo, dimensions=["correctness"],
                     _make_frontend=noconverge_frontend_builder(budget=2))
    assert rc == 0

    rows = _read_rows(repo)
    assert not any(r.event == "confirmed-work" for r in rows), \
        "acceptance never passed -> no confirm even after budget exhaustion"
    assert rows[-1].event == "stop" and rows[-1].status == "dry"
    # HONESTY: budget exhaustion cannot be laundered into a confirm; no commit
    # landed (the convergence runner only commits on a genuine acceptance pass +
    # non-empty diff in the SAME attempt).
    assert git(repo, "rev-parse", "HEAD") == base_sha
    assert attested_peer(repo, base_sha) is None
    assert _gates(repo, rows)["witness-ledgered"] is False


# ---- DEV-HONESTY-DANGLING-10 (HONEST-01, operator-gate level) ---------------


def test_dev_honesty_dangling_attest_note_forge_10(tmp_path):
    """HONEST-01: a (hand-appended) confirmed-work row citing an OUT-OF-BRANCH
    (dangling) commit that carries an agent-minted peers-attest note must FAIL the
    operator's authorship-attested gate — the reachability constraint
    (gates.py:211 ``_attest_reachable``) defeats the forge EVEN THOUGH
    ``attested_peer(dangling)`` resolves a real peer name. ``.peers/run.jsonl`` is
    agent-writable, so trust is bound to a note REACHABLE from the run tip, never
    a bare ``git notes show``. This pins the operator-level re-derivation of the
    single most load-bearing honesty forge (the dangling/out-of-range-note forge).
    """
    from peers.spine.op_config import load_op_config

    from tests.unit._isolation_helpers import (
        _attested_repo, _commit_on_branch, _git,
    )

    repo = tmp_path / "proj"
    repo.mkdir()
    _attested_repo(repo)
    # a real on-branch attested commit (the legit converged work).
    tip = _commit_on_branch(repo, "peers/run/p1", "fix.py", "fix", peer="claude")

    def _ledger(attest_sha: str):
        led = RunLedger(repo / f"led-{attest_sha[:8]}.jsonl")
        load_op_config(OpConfig.from_dict({"mode": "develop"}), led,
                       mode_run="develop-proj")
        led.append_attested(repo, attest_sha, event="confirmed-work", subject="F1",
                            status="pass",
                            witness={"kind": "git-sha", "uri": attest_sha,
                                     "sha256": attest_sha},
                            independence=True, mode_run="develop-proj")
        led.append(event="stop", status="complete", mode_run="develop-proj")
        return led.read()

    # CONTROL: the legit on-branch attested tip -> authorship-attested True (the
    # gate is real, not always-False).
    g_ok = evaluate_spine_gates(_ledger(tip), mode_run="develop-proj", repo=repo,
                                head="peers/run/p1")
    assert g_ok["authorship-attested"] is True

    # FORGE: a dangling commit-tree object carrying an agent-minted note (which
    # DOES resolve a peer name) cited as attest_sha -> the gate re-derives
    # reachability and REJECTS, because the commit is on no branch.
    tree = _git(repo, "rev-parse", "peers/run/p1^{tree}").strip()
    dangling = _git(repo, "commit-tree", tree, "-m", "forged").strip()
    _git(repo, "notes", "--ref=peers-attest", "add", "-m", "codex", dangling)
    assert attested_peer(repo, dangling) == "codex"      # the note DOES resolve...
    g_forge = evaluate_spine_gates(_ledger(dangling), mode_run="develop-proj",
                                   repo=repo, head="peers/run/p1")
    assert g_forge["authorship-attested"] is False, \
        "a note on an out-of-branch commit must fail the reachability constraint"
