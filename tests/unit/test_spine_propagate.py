import subprocess
from tests.unit._isolation_helpers import (_git, _attested_repo, _commit_on_branch,
                                          _run, _sha256_file)

from peers.spine.worktree import GitWorktreeProvider
from peers.spine.propagate import is_converged, propagate_branch
from peers.spine.gates import resolves_to_commit
from peers.spine.ledger import RunLedger


def _converged_producer_ledger(repo, ledger_path, mode_run, branch_tip):
    """Build a CONVERGED producer ledger: run-start + an attested+git-sha-witnessed
    confirmed-work over a REAL attested branch-tip commit + a stop row."""
    from peers.spine.op_config import OpConfig, load_op_config
    led = RunLedger(ledger_path)
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run=mode_run)
    led.append_attested(repo, branch_tip, event="confirmed-work", subject="F1",
                        status="pass",
                        witness={"kind": "git-sha", "uri": branch_tip, "sha256": branch_tip},
                        independence=True, mode_run=mode_run)
    led.append(event="stop", status="complete", mode_run=mode_run)
    return led


def test_is_converged_true_on_attested_witnessed_ledger(tmp_path):
    _attested_repo(tmp_path)
    tip = _commit_on_branch(tmp_path, "peers/run/p1", "fix.py", "fix", peer="claude")
    led = _converged_producer_ledger(tmp_path, tmp_path / "p.jsonl", "p1", tip)
    assert is_converged(led.read(), mode_run="p1", repo=tmp_path,
                        head="peers/run/p1") is True   # HONEST-01: anchor on the run branch


def test_is_converged_false_when_no_confirmed_work(tmp_path):
    _attested_repo(tmp_path)
    led = RunLedger(tmp_path / "p.jsonl")
    from peers.spine.op_config import OpConfig, load_op_config
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="p1")
    led.append(event="dry-round", status="dry", mode_run="p1")
    led.append(event="stop", status="dry", mode_run="p1")
    assert is_converged(led.read(), mode_run="p1", repo=tmp_path) is False


def test_propagate_refused_when_producer_not_converged(tmp_path):
    _attested_repo(tmp_path)
    _commit_on_branch(tmp_path, "peers/run/p1", "fix.py", "fix")
    # an UN-converged producer ledger (only a dry-round, no confirmed-work)
    led = RunLedger(tmp_path / "p.jsonl")
    from peers.spine.op_config import OpConfig, load_op_config
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="p1")
    led.append(event="dry-round", status="dry", mode_run="p1")
    producer = _run(tmp_path, mode_run="p1", branch="peers/run/p1")
    producer._ledger = led                       # bind the prebuilt ledger
    prov = GitWorktreeProvider()
    with prov.lease(tmp_path, "c1") as consumer_ws:
        before = consumer_ws.worktree_path / "fix.py"
        res = propagate_branch(producer, consumer_ws, repo=tmp_path)
        assert res.ok is False and res.reason == "not-converged"
        assert not before.exists()               # NOTHING written into the consumer


def _consumer_ref(consumer_ws, from_run):
    r = subprocess.run(["git", "-C", str(consumer_ws.worktree_path), "rev-parse",
                        "--verify", "--quiet", f"refs/propagated/{from_run}"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def test_propagate_moves_attested_branch_and_consumer_re_derives(tmp_path):
    _attested_repo(tmp_path)
    tip = _commit_on_branch(tmp_path, "peers/run/p1", "fix.py", "fix", peer="claude")
    led = _converged_producer_ledger(tmp_path, tmp_path / "p.jsonl", "p1", tip)
    producer = _run(tmp_path, mode_run="p1", branch="peers/run/p1")
    producer._ledger = led
    prov = GitWorktreeProvider()
    with prov.lease(tmp_path, "c1") as consumer_ws:
        # discriminating NEGATIVE: the consumer-owned ref does NOT exist yet
        assert _consumer_ref(consumer_ws, "p1") is None
        res = propagate_branch(producer, consumer_ws, repo=tmp_path)
        assert res.ok is True
        # the edge witness RECORDS which converged tip was transferred (git-sha)
        assert res.witness["kind"] == "git-sha" and res.witness["sha256"] == tip
        # discriminating POSITIVE: the consumer-owned ref now pins exactly the tip
        # (resolves_to_commit alone is non-discriminating -- shared ODB resolves any
        # committed tip; the refs/propagated/<from_run> ref is set ONLY by propagate)
        assert _consumer_ref(consumer_ws, "p1") == tip
        assert resolves_to_commit(consumer_ws.worktree_path, tip) is True
        # the propagation edge is recorded on the CONSUMER ledger, attested+witnessed
        rows = consumer_ws_ledger(consumer_ws).read()
        prop = [r for r in rows if r.event == "propagation"]
        assert prop and prop[-1].author == "claude" and prop[-1].independence is True
        assert prop[-1].witness["from_run"] == "p1" and prop[-1].witness["to_run"] == "c1"


def test_propagate_ok_while_producer_holds_its_branch_checked_out(tmp_path):
    # THE regression test for the branch -f collision: the producer run is LIVE --
    # peers/run/p1 is checked out in its leased worktree -- exactly the concurrent
    # case Stage 5 exists for. `git branch -f peers/run/p1` would fail rc=128 here;
    # the consumer-owned refs/propagated/p1 update-ref succeeds.
    _attested_repo(tmp_path)
    prov = GitWorktreeProvider()
    with prov.lease(tmp_path, "p1") as producer_ws:
        # the producer commits its converged work ON its checked-out branch
        (producer_ws.worktree_path / "fix.py").write_text("fix")
        _git(producer_ws.worktree_path, "add", "fix.py")
        _git(producer_ws.worktree_path, "commit", "-q", "-m", "fix")
        tip = _git(producer_ws.worktree_path, "rev-parse", "HEAD").strip()
        from peers import attest
        base = _git(producer_ws.worktree_path, "rev-parse", "HEAD~1").strip()
        attest.attest_commits(producer_ws.worktree_path, "claude", base, tip)
        producer = _run(tmp_path, mode_run="p1", branch=producer_ws.branch)
        producer._ledger = _converged_producer_ledger(
            tmp_path, tmp_path / "p.jsonl", "p1", tip)
        with prov.lease(tmp_path, "c1") as consumer_ws:
            res = propagate_branch(producer, consumer_ws, repo=tmp_path)
            assert res.ok is True and res.reason == ""     # NOT move-failed
            assert _consumer_ref(consumer_ws, "p1") == tip


def consumer_ws_ledger(ws):
    return RunLedger(ws.worktree_path / ".peers" / "run.jsonl")


def test_propagate_ships_attested_converged_commit_not_live_tip(tmp_path):
    # REVIEW-A: a producer converges over attested commit X, then its branch
    # advances to an UN-attested commit Y (a live producer committing past
    # convergence -- exactly the concurrent case Stage 5 exists for). Propagation
    # must ship the CONVERGED artifact X, NEVER the live, un-attested tip Y.
    _attested_repo(tmp_path)
    x = _commit_on_branch(tmp_path, "peers/run/p1", "fix.py", "fix", peer="claude")
    led = _converged_producer_ledger(tmp_path, tmp_path / "p.jsonl", "p1", x)
    # advance peers/run/p1 to an UN-attested Y (no attest_commits)
    _git(tmp_path, "checkout", "-q", "peers/run/p1")
    (tmp_path / "more.py").write_text("more")
    _git(tmp_path, "add", "more.py")
    _git(tmp_path, "commit", "-q", "-m", "past-convergence")
    y = _git(tmp_path, "rev-parse", "HEAD").strip()
    _git(tmp_path, "checkout", "-q", "-")
    assert x != y
    producer = _run(tmp_path, mode_run="p1", branch="peers/run/p1")
    producer._ledger = led
    prov = GitWorktreeProvider()
    with prov.lease(tmp_path, "c1") as consumer_ws:
        res = propagate_branch(producer, consumer_ws, repo=tmp_path)
        assert res.ok is True
        assert res.witness["sha256"] == x            # the CONVERGED commit, not y
        assert res.witness["sha256"] != y
        assert _consumer_ref(consumer_ws, "p1") == x  # the pin is the converged tip
        # the consumer ledger is NOT poisoned -- the propagation row carries the
        # producer's attested author (non-None)
        rows = consumer_ws_ledger(consumer_ws).read()
        prop = [r for r in rows if r.event == "propagation"][-1]
        assert prop.author == "claude" and prop.independence is True


def test_propagate_refuses_unattested_converged_sha_no_consumer_poison(tmp_path):
    # REVIEW-B: a producer ledger whose confirmed-work row is attested via a REAL
    # commit X, but whose witness sha is FORGED to point at an un-attested commit U.
    # is_converged passes (the witness re-derives -- U resolves -- and the row's
    # author is non-None from X), so propagation must NOT trust the witness sha
    # blindly: it must fail-closed because the SHIPPED commit U is not attested,
    # else it would write an independence=True/author=None row that PERMANENTLY
    # poisons the consumer's authorship-attested gate (append-only).
    _attested_repo(tmp_path)
    x = _commit_on_branch(tmp_path, "peers/run/p1", "fix.py", "fix", peer="claude")
    _git(tmp_path, "checkout", "-q", "peers/run/p1")
    (tmp_path / "u.py").write_text("u")
    _git(tmp_path, "add", "u.py")
    _git(tmp_path, "commit", "-q", "-m", "u")
    u = _git(tmp_path, "rev-parse", "HEAD").strip()
    _git(tmp_path, "checkout", "-q", "-")
    from peers.spine.op_config import OpConfig, load_op_config
    led = RunLedger(tmp_path / "p.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="p1")
    led.append_attested(tmp_path, x, event="confirmed-work", subject="F1", status="pass",
                        witness={"kind": "git-sha", "uri": u, "sha256": u},  # FORGED to U
                        independence=True, mode_run="p1")
    led.append(event="stop", status="complete", mode_run="p1")
    producer = _run(tmp_path, mode_run="p1", branch="peers/run/p1")
    producer._ledger = led
    prov = GitWorktreeProvider()
    with prov.lease(tmp_path, "c1") as consumer_ws:
        res = propagate_branch(producer, consumer_ws, repo=tmp_path)
        assert res.ok is False and res.reason == "unattested-tip"
        rows = consumer_ws_ledger(consumer_ws).read()
        assert not [r for r in rows if r.event == "propagation"]   # nothing written
        from peers.spine.gates import _gate_authorship_attested
        assert _gate_authorship_attested(rows, tmp_path) is True    # gate not poisoned


def test_propagate_no_artifact_when_no_git_sha_confirmed_work(tmp_path):
    # REVIEW-E: a CONVERGED producer whose confirmed-work is FILE-witnessed only
    # (research-style: no git-sha branch commit to propagate) -> no-artifact,
    # nothing written. Pins the fail-closed "no propagatable branch commit" path.
    _attested_repo(tmp_path)
    x = _commit_on_branch(tmp_path, "peers/run/p1", "fix.py", "fix", peer="claude")
    report = tmp_path / "report.md"
    report.write_text("# report")
    fsha = _sha256_file(report)
    from peers.spine.op_config import OpConfig, load_op_config
    led = RunLedger(tmp_path / "p.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "research"}), led, mode_run="p1")
    led.append_attested(tmp_path, x, event="confirmed-work", subject="F1", status="pass",
                        witness={"kind": "file", "uri": str(report), "sha256": fsha},
                        independence=True, mode_run="p1")
    led.append(event="stop", status="complete", mode_run="p1")
    producer = _run(tmp_path, mode_run="p1", branch="peers/run/p1")
    producer._ledger = led
    prov = GitWorktreeProvider()
    with prov.lease(tmp_path, "c1") as consumer_ws:
        res = propagate_branch(producer, consumer_ws, repo=tmp_path)
        assert res.ok is False and res.reason == "no-artifact"
        assert _consumer_ref(consumer_ws, "p1") is None


def test_propagate_move_failed_when_producer_branch_absent(tmp_path):
    # A git-sha producer whose claimed branch ref is ABSENT is refused fail-closed,
    # nothing pinned. HONEST-01 (strict anchor): the convergence re-check anchors
    # attest-reachability on the producer's branch; an absent branch (whose tip
    # cannot contain the attested commit) is caught as `not-converged` BEFORE the
    # branch move is attempted — strictly more fail-closed, still pins nothing.
    _attested_repo(tmp_path)
    x = _commit_on_branch(tmp_path, "peers/run/p1", "fix.py", "fix", peer="claude")
    led = _converged_producer_ledger(tmp_path, tmp_path / "p.jsonl", "p1", x)
    # the ledger is converged over X, but point the run at a NON-EXISTENT branch
    producer = _run(tmp_path, mode_run="p1", branch="peers/run/ghost")
    producer._ledger = led
    prov = GitWorktreeProvider()
    with prov.lease(tmp_path, "c1") as consumer_ws:
        res = propagate_branch(producer, consumer_ws, repo=tmp_path)
        assert res.ok is False and res.reason == "not-converged"
        assert _consumer_ref(consumer_ws, "p1") is None


def test_propagate_does_not_re_attest(tmp_path):
    # the consumer must NOT re-author the producer's commit: the attested peer of
    # the moved tip stays the PRODUCER's peer, never the consumer.
    _attested_repo(tmp_path)
    tip = _commit_on_branch(tmp_path, "peers/run/p1", "fix.py", "fix", peer="claude")
    led = _converged_producer_ledger(tmp_path, tmp_path / "p.jsonl", "p1", tip)
    producer = _run(tmp_path, mode_run="p1", branch="peers/run/p1")
    producer._ledger = led
    prov = GitWorktreeProvider()
    with prov.lease(tmp_path, "c1") as consumer_ws:
        propagate_branch(producer, consumer_ws, repo=tmp_path)
        from peers.spine.authorship import resolve_author
        assert resolve_author(consumer_ws.worktree_path, tip) == "claude"   # unchanged
