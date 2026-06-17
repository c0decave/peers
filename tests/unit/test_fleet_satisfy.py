# tests/unit/test_fleet_satisfy.py
"""STEP-3 contract tests for F2 dependency satisfaction by RE-VERIFICATION.

Host-only + deterministic: REAL tmp git repos (via _isolation_helpers), a REAL
``propagate_branch`` for the cross-tool path, and substrate-re-derived authorship
via ``refs/notes/peers-attest`` -- NO network, NO LLM, NO containers.

Coverage (happy / edge / sad):
  - happy: intra satisfied when the producer is CONVERGED; cross satisfied when
    distinct peers + CONVERGED + a REAL propagated edge; the adapter binds intra
    from substrate -> True.
  - edge:  intra ignores a (hypothetical) recorded status; cross without the
    propagated ref; a forged independence=True row is ignored; the adapter
    derives kind=cross + the consumer ws from the consumer run.
  - sad:   non-converged producer; same-peer cross-run self-green; a decoy
    other-peer commit cannot redirect the re-derived consumer tip; an unattested
    consumer tip fails closed; the adapter rejects a non-converged producer.

NOTE on the cross-tool fixture: the real ``GitWorktreeProvider.lease`` is a
``@contextmanager`` that YIELDS a ``RunWorkspace`` and tears the worktree down on
exit -- so the lease MUST be held open (``with``) for the whole assertion; a bare
``lease(...).__enter__()`` discards the context-manager object, which CPython then
GCs, closing the generator and removing the worktree before the test runs. The
cross-tool helper is therefore a ``@contextmanager`` and the tests use ``with``.
"""
from contextlib import contextmanager

import pytest

from peers.spine.ledger import RunLedger
from peers.spine.op_config import OpConfig, load_op_config
from peers.spine.propagate import propagate_branch
from peers.spine.worktree import GitWorktreeProvider

from peers.fleet.program import ModeRunSpec
from peers.fleet.satisfy import dep_satisfied, make_dep_check

from tests.unit._isolation_helpers import (
    _attested_repo, _commit_on_branch, _converged_branch_ledger, _git, _run,
)


def _plain_commit_on(wt, filename, content):
    """A commit on the worktree's current branch WITHOUT attestation (no
    ``peers-attest`` note) -> its ``resolve_author`` is ``None``. Used to build an
    UNATTESTED endpoint (a converged-handoff endpoint must be attested)."""
    from pathlib import Path
    (Path(wt) / filename).write_text(content)
    _git(wt, "add", filename)
    _git(wt, "commit", "-q", "-m", f"plain:{filename}")
    return _git(wt, "rev-parse", "HEAD").strip()


def _converged_producer(repo, mode_run, peer="claude"):
    tip = _commit_on_branch(repo, f"peers/run/{mode_run}", "fix.py", "fix", peer=peer)
    run = _run(repo, mode_run=mode_run, tool=repo, branch=f"peers/run/{mode_run}")
    run._ledger = _converged_branch_ledger(repo, repo / f"{mode_run}.jsonl", mode_run, tip)
    return run, tip


def _spec(tool, run_id, depends_on=()):
    return ModeRunSpec(tool=tool, mode="develop",
                       op_config=OpConfig.from_dict({"mode": "develop"}),
                       run_id=run_id, depends_on=list(depends_on))


def _two_attested_repos(tmp_path):
    """Two SEPARATE attested git repos (distinct ODBs + peers-attest notes refs) --
    the producer side (x) and the consumer side (y) of a cross-tool dependency."""
    x = tmp_path / "x"
    x.mkdir()
    _attested_repo(x)
    y = tmp_path / "y"
    y.mkdir()
    _attested_repo(y)
    return x, y


# ----------------------------------------------------------------------------
# intra-tool: re-derive CONVERGED from the PRODUCER's own ledger
# ----------------------------------------------------------------------------
def test_intra_tool_satisfied_only_when_producer_converged(tmp_path):
    _attested_repo(tmp_path)
    prod, tip = _converged_producer(tmp_path, "a")
    cons = _run(tmp_path, mode_run="b", tool=tmp_path, branch="peers/run/b")
    assert dep_satisfied(prod, cons, producer_repo=tmp_path, consumer_repo=tmp_path,
                         kind="intra") is True


def test_intra_tool_unsatisfied_for_non_converged_producer(tmp_path):
    _attested_repo(tmp_path)
    _commit_on_branch(tmp_path, "peers/run/a", "fix.py", "fix")
    prod = _run(tmp_path, mode_run="a", tool=tmp_path, branch="peers/run/a")
    led = RunLedger(tmp_path / "a.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="a")
    led.append(event="dry-round", status="dry", mode_run="a")          # NOT converged
    prod._ledger = led
    cons = _run(tmp_path, mode_run="b", tool=tmp_path, branch="peers/run/b")
    assert dep_satisfied(prod, cons, producer_repo=tmp_path, consumer_repo=tmp_path,
                         kind="intra") is False


def test_intra_tool_does_not_trust_a_recorded_status(tmp_path):
    # the producer ledger is NOT converged; even if a fleet-ledger claims "converged",
    # dep_satisfied re-derives from the producer ledger -> False.
    _attested_repo(tmp_path)
    _commit_on_branch(tmp_path, "peers/run/a", "fix.py", "fix")
    prod = _run(tmp_path, mode_run="a", tool=tmp_path, branch="peers/run/a")
    led = RunLedger(tmp_path / "a.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="a")
    led.append(event="dry-round", status="dry", mode_run="a")
    prod._ledger = led
    cons = _run(tmp_path, mode_run="b", tool=tmp_path, branch="peers/run/b")
    assert dep_satisfied(prod, cons, producer_repo=tmp_path, consumer_repo=tmp_path,
                         kind="intra") is False                        # ledger ignored


# ----------------------------------------------------------------------------
# cross-tool: CONVERGED + a REAL propagated edge + substrate-re-derived
# producer-peer != consumer-peer, over TWO separate repos
# ----------------------------------------------------------------------------
@contextmanager
def _cross_tool_consumer(producer_run, producer_repo, consumer_repo, *, cons_peer):
    """Lease a consumer worktree of consumer_repo, REAL-propagate the producer's
    converged tip into it, and give the consumer its OWN converged ledger over a
    tip attested to `cons_peer` (so dep_satisfied re-derives the consumer tip from
    the consumer ledger -- NOT a caller value). Yields (cons_run, cons_ws) with the
    worktree lease HELD OPEN for the whole `with` block (see the module docstring:
    the lease must not be GC'd mid-test)."""
    prov = GitWorktreeProvider()
    with prov.lease(consumer_repo, "b") as cons_ws:
        res = propagate_branch(producer_run, cons_ws, repo=producer_repo)
        assert res.ok is True, res.reason
        # the consumer's OWN converged work on ITS leased branch tip (cons_ws.branch
        # — the worktree is already checked out on it), attested to cons_peer.
        # HONEST-01: commit on the run's ACTUAL branch (not a mis-named one), so the
        # convergence gate's reachability anchor (= consumer_run.branch) holds.
        from peers import attest
        wt = cons_ws.worktree_path
        (wt / "use.py").write_text("use")
        _git(wt, "add", "use.py")
        _git(wt, "commit", "-q", "-m", "use")
        cons_tip = _git(wt, "rev-parse", "HEAD").strip()
        attest.attest_commits(wt, cons_peer, cons_ws.base_sha, cons_tip)
        cons = _run(consumer_repo, mode_run="b", tool=cons_ws.worktree_path,
                    branch=cons_ws.branch)
        cons._ledger = _converged_branch_ledger(
            cons_ws.worktree_path, cons_ws.worktree_path / "b.jsonl", "b", cons_tip)
        yield cons, cons_ws


def test_cross_tool_two_separate_repos_distinct_peers_satisfied(tmp_path):
    # major F2-2: the producer and consumer live in SEPARATE repos (distinct ODBs +
    # notes refs). producer-peer is resolved in producer_repo, consumer-peer in
    # consumer_repo. distinct peers + converged + propagated -> satisfied.
    x, y = _two_attested_repos(tmp_path)
    prod, tip = _converged_producer(x, "a", peer="claude")
    with _cross_tool_consumer(prod, x, y, cons_peer="codex") as (cons, cons_ws):
        assert dep_satisfied(prod, cons, producer_repo=x, consumer_repo=y,
                             kind="cross", consumer_ws=cons_ws) is True


def test_cross_tool_without_consumer_ws_fails_closed(tmp_path):
    # F2-2: cross-tool re-verification REQUIRES the consumer workspace (to read
    # refs/propagated/*). A converged producer with consumer_ws=None -> fail closed,
    # never satisfied on the producer's convergence alone.
    _attested_repo(tmp_path)
    prod, tip = _converged_producer(tmp_path, "a")
    cons = _run(tmp_path, mode_run="b", tool=tmp_path, branch="peers/run/b")
    assert dep_satisfied(prod, cons, producer_repo=tmp_path, consumer_repo=tmp_path,
                         kind="cross", consumer_ws=None) is False


def test_cross_tool_rejected_without_propagated_ref(tmp_path):
    x, y = _two_attested_repos(tmp_path)
    prod, tip = _converged_producer(x, "a")
    prov = GitWorktreeProvider()
    with prov.lease(y, "b") as cons_ws:
        # NO propagate_branch call -> refs/propagated/a is absent in the consumer
        cons_tip = _commit_on_branch(cons_ws.worktree_path,
                                     cons_ws.branch.split("/")[-1], "use.py", "use",
                                     peer="codex")
        cons = _run(y, mode_run="b", tool=cons_ws.worktree_path, branch=cons_ws.branch)
        cons._ledger = _converged_branch_ledger(cons_ws.worktree_path,
                                                cons_ws.worktree_path / "b.jsonl", "b",
                                                cons_tip)
        assert dep_satisfied(prod, cons, producer_repo=x, consumer_repo=y,
                             kind="cross", consumer_ws=cons_ws) is False  # not propagated


def test_cross_tool_rejected_on_same_peer_self_green(tmp_path):
    # F2 carry-forward (blocker F2-1): producer AND consumer attested to the SAME peer
    # -> a self-greened cross-run handoff. Re-derived from the substrate, NOT the row's
    # independence field, NOT a caller tip -> rejected.
    x, y = _two_attested_repos(tmp_path)
    prod, tip = _converged_producer(x, "a", peer="claude")
    with _cross_tool_consumer(prod, x, y, cons_peer="claude") as (cons, cons_ws):  # SAME peer
        assert dep_satisfied(prod, cons, producer_repo=x, consumer_repo=y,
                             kind="cross", consumer_ws=cons_ws) is False


def test_cross_tool_decoy_consumer_tip_cannot_fake_distinctness(tmp_path):
    # blocker F2-1: a single peer authors BOTH runs (both claude). It plants an old
    # codex-attested decoy commit in the consumer repo, but it CANNOT redirect the
    # check there -- dep_satisfied re-derives the consumer tip from the CONSUMER LEDGER
    # (still claude) -> same peer -> rejected, even though a codex commit exists.
    x, y = _two_attested_repos(tmp_path)
    prod, tip = _converged_producer(x, "a", peer="claude")
    with _cross_tool_consumer(prod, x, y, cons_peer="claude") as (cons, cons_ws):
        # plant a real codex-attested commit elsewhere in y (the would-be decoy)
        _commit_on_branch(cons_ws.worktree_path, "decoy", "decoy.py", "d", peer="codex")
        # the consumer tip is STILL re-derived from the consumer ledger (claude) ->
        # rejected; the decoy is irrelevant because the caller can't supply a tip.
        assert dep_satisfied(prod, cons, producer_repo=x, consumer_repo=y,
                             kind="cross", consumer_ws=cons_ws) is False


def test_unattested_consumer_tip_fails_closed(tmp_path):
    # the consumer's converged tip is an UNATTESTED plain commit (no peers-attest
    # note) -> it cannot be a converged handoff endpoint (its confirmed-work row has
    # author=None, so the consumer ledger is NOT converged) -> rejected (fail closed),
    # even with a real propagated edge present.
    x, y = _two_attested_repos(tmp_path)        # leaseable (has commits)
    prod, tip = _converged_producer(x, "a", peer="claude")
    prov = GitWorktreeProvider()
    with prov.lease(y, "b") as cons_ws:
        res = propagate_branch(prod, cons_ws, repo=x)
        assert res.ok is True, res.reason
        cons_tip = _plain_commit_on(cons_ws.worktree_path, "use.py", "use")  # NOT attested
        cons = _run(y, mode_run="b", tool=cons_ws.worktree_path, branch=cons_ws.branch)
        cons._ledger = _converged_branch_ledger(cons_ws.worktree_path,
                                                cons_ws.worktree_path / "b.jsonl", "b",
                                                cons_tip)
        assert dep_satisfied(prod, cons, producer_repo=x, consumer_repo=y,
                             kind="cross", consumer_ws=cons_ws) is False


def test_forged_independence_row_is_ignored(tmp_path):
    # F2-regression: a SAME-peer attestation is still rejected even though the
    # consumer ledger's propagation row carries independence=True (the field is
    # ignored; the re-derived substrate authorship wins).
    x, y = _two_attested_repos(tmp_path)
    prod, tip = _converged_producer(x, "a", peer="claude")
    with _cross_tool_consumer(prod, x, y, cons_peer="claude") as (cons, cons_ws):  # same peer
        # even if the consumer ledger carries an independence=True propagation row,
        # dep_satisfied re-derives -> same peer -> rejected.
        assert dep_satisfied(prod, cons, producer_repo=x, consumer_repo=y,
                             kind="cross", consumer_ws=cons_ws) is False


# ----------------------------------------------------------------------------
# make_dep_check production adapter: binds kind/repos/consumer-ws
# ENTIRELY from substrate; draws NOTHING from a fleet-ledger row.
# ----------------------------------------------------------------------------
def test_make_dep_check_intra_binds_from_substrate(tmp_path):
    # identical tool roots -> kind="intra"; a CONVERGED producer -> True. The adapter
    # has no fleet-ledger handle: the verdict is the pure per-run re-verification.
    _attested_repo(tmp_path)
    prod, tip = _converged_producer(tmp_path, "a")
    cons = _run(tmp_path, mode_run="b", tool=tmp_path, branch="peers/run/b")
    specs = {"a": _spec(tmp_path, "a"), "b": _spec(tmp_path, "b", depends_on=["a"])}
    runs = {"a": prod, "b": cons}
    repos = {"a": tmp_path, "b": tmp_path}
    dep_check = make_dep_check(specs, runs, repos)
    assert dep_check("a", "b") is True


def test_make_dep_check_rejects_non_converged_producer(tmp_path):
    # the adapter re-derives from the producer's own ledger: a non-converged
    # producer -> False (a fleet-ledger row claiming "converged" cannot reach it).
    _attested_repo(tmp_path)
    _commit_on_branch(tmp_path, "peers/run/a", "fix.py", "fix")
    prod = _run(tmp_path, mode_run="a", tool=tmp_path, branch="peers/run/a")
    led = RunLedger(tmp_path / "a.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="a")
    led.append(event="dry-round", status="dry", mode_run="a")
    prod._ledger = led
    cons = _run(tmp_path, mode_run="b", tool=tmp_path, branch="peers/run/b")
    specs = {"a": _spec(tmp_path, "a"), "b": _spec(tmp_path, "b", depends_on=["a"])}
    runs = {"a": prod, "b": cons}
    repos = {"a": tmp_path, "b": tmp_path}
    dep_check = make_dep_check(specs, runs, repos)
    assert dep_check("a", "b") is False


def test_make_dep_check_cross_derives_ws_and_rejects_unpropagated(tmp_path):
    # distinct tool roots -> kind="cross"; the adapter DERIVES the consumer ws from
    # the consumer run's own branch. With no refs/propagated/<a> the edge is absent
    # -> False, even though the producer is CONVERGED.
    x, y = _two_attested_repos(tmp_path)
    prod, tip = _converged_producer(x, "a", peer="claude")
    cons_tip = _commit_on_branch(y, "peers/run/b", "use.py", "use", peer="codex")
    cons = _run(y, mode_run="b", tool=y, branch="peers/run/b")
    cons._ledger = _converged_branch_ledger(y, y / "b.jsonl", "b", cons_tip)
    specs = {"a": _spec(x, "a"), "b": _spec(y, "b", depends_on=["a"])}
    runs = {"a": prod, "b": cons}
    repos = {"a": x, "b": y}
    dep_check = make_dep_check(specs, runs, repos)
    assert dep_check("a", "b") is False


# ----------------------------------------------------------------------------
# Second block (STEP-3 hardening + the happy/edge/sad classes the canonical
# names above do not exercise by NAME -- mirrors the STEP-4 scheduler
# convention). The canonical 13 classify edge-less (sad-heavy + one happy via
# "self_green"); these add the missing EDGE boundary, a stable-happy probe, and
# the BUG-300 unknown-kind fail-closed guard.
# ----------------------------------------------------------------------------
def test_intra_dep_satisfied_is_stable_across_repeated_reverification_happy(tmp_path):
    # happy: dep_satisfied RE-READS the producer ledger every call (it is a pure
    # re-verification, not a cached verdict) -- a converged intra producer is
    # satisfied on the first AND a repeated call.
    _attested_repo(tmp_path)
    prod, _tip = _converged_producer(tmp_path, "a")
    cons = _run(tmp_path, mode_run="b", tool=tmp_path, branch="peers/run/b")
    first = dep_satisfied(prod, cons, producer_repo=tmp_path, consumer_repo=tmp_path,
                          kind="intra")
    second = dep_satisfied(prod, cons, producer_repo=tmp_path, consumer_repo=tmp_path,
                           kind="intra")
    assert first is True and second is True


def test_intra_dep_over_empty_producer_ledger_is_unsatisfied_edge(tmp_path):
    # edge: the producer's ledger is EMPTY (a fresh ledger with zero rows -- no
    # op_config, no confirmed-work). is_converged over [] is False, so the intra dep
    # is NOT satisfied -- a boundary the dry-round cases above do not cover (they
    # have rows). Fail-closed on the empty-ledger boundary.
    _attested_repo(tmp_path)
    prod = _run(tmp_path, mode_run="a", tool=tmp_path, branch="peers/run/a")
    prod._ledger = RunLedger(tmp_path / "empty_a.jsonl")     # never written -> read() == []
    assert prod._ledger.read() == []                         # precondition: empty
    cons = _run(tmp_path, mode_run="b", tool=tmp_path, branch="peers/run/b")
    assert dep_satisfied(prod, cons, producer_repo=tmp_path, consumer_repo=tmp_path,
                         kind="intra") is False


def test_dep_satisfied_rejects_unknown_kind_with_valueerror(tmp_path):
    # sad (BUG-300, fix_by claude): kind is a 2-valued discriminator ("intra" /
    # "cross"). The production make_dep_check only ever derives those two, but a
    # DIRECT caller typo ("inter", "Cross", "") must NOT silently fall through to
    # the cross-tool branch (and possibly return a satisfied verdict). An
    # unrecognized kind is a caller contract violation -> raise ValueError, never a
    # bool -- the strongest fail-closed posture (a typo can produce NEITHER True nor
    # a quietly-wrong False).
    _attested_repo(tmp_path)
    prod, _tip = _converged_producer(tmp_path, "a")
    cons = _run(tmp_path, mode_run="b", tool=tmp_path, branch="peers/run/b")
    for bad in ("inter", "Cross", "", "INTRA", "x"):
        with pytest.raises(ValueError):
            dep_satisfied(prod, cons, producer_repo=tmp_path, consumer_repo=tmp_path,
                          kind=bad)
