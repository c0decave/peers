"""HONEST-01 (HIGH): the spine convergence authorship gate must reject an attest
note minted on an OUT-OF-BRANCH (dangling) commit.

`_gate_authorship_attested` re-derived the independence author via plain
`attested_peer` (a bare `git notes show`) with NO reachability constraint, so a
peer could `commit-tree` a dangling object, `git notes --ref=peers-attest add`
a forged peer name on it, cite it as `attest_sha`, and pass convergence — the
exact FU-2 dangling/out-of-range-note forge, fixed for the checkoff/review gates
but NOT this one (`reachable_attested_peer` existed with 0 callers). The fix
anchors reachability on the run's branch/tip. happy / sad / edge.
"""
from __future__ import annotations

from peers.spine.ledger import RunLedger
from peers.spine.op_config import OpConfig, load_op_config
from peers.spine.propagate import is_converged
from tests.unit._isolation_helpers import _attested_repo, _commit_on_branch, _git


def _ledger_over(repo, ledger_path, mode_run, attest_sha):
    led = RunLedger(ledger_path)
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run=mode_run)
    led.append_attested(repo, attest_sha, event="confirmed-work", subject="F1",
                        status="pass",
                        witness={"kind": "git-sha", "uri": attest_sha,
                                 "sha256": attest_sha},
                        independence=True, mode_run=mode_run)
    led.append(event="stop", status="complete", mode_run=mode_run)
    return led


def _dangling_with_forged_note(repo, like_tip, peer):
    """A commit-tree object reachable from NO branch, carrying an agent-minted
    peers-attest note (the forge)."""
    tree = _git(repo, "rev-parse", f"{like_tip}^{{tree}}").strip()
    dangling = _git(repo, "commit-tree", tree, "-m", "forged").strip()
    _git(repo, "notes", "--ref=peers-attest", "add", "-m", peer, dangling)
    return dangling


# ---- sad: the forge MUST be rejected --------------------------------------
def test_is_converged_rejects_out_of_branch_attest_note_forge(tmp_path):
    _attested_repo(tmp_path)
    tip = _commit_on_branch(tmp_path, "peers/run/p1", "fix.py", "fix", peer="claude")
    dangling = _dangling_with_forged_note(tmp_path, tip, "codex")
    led = _ledger_over(tmp_path, tmp_path / "p.jsonl", "p1", dangling)
    # the forged note resolves an author, but the commit is on no branch:
    assert is_converged(led.read(), mode_run="p1", repo=tmp_path,
                        head="peers/run/p1") is False


# ---- happy: a legit on-branch attested commit still converges --------------
def test_is_converged_true_for_on_branch_attested_commit(tmp_path):
    _attested_repo(tmp_path)
    tip = _commit_on_branch(tmp_path, "peers/run/p1", "fix.py", "fix", peer="claude")
    led = _ledger_over(tmp_path, tmp_path / "p.jsonl", "p1", tip)
    assert is_converged(led.read(), mode_run="p1", repo=tmp_path,
                        head="peers/run/p1") is True


# ---- edge: the dangling forge is rejected even with the DEFAULT anchor --------
def test_is_converged_rejects_dangling_forge_even_with_default_head(tmp_path):
    _attested_repo(tmp_path)
    tip = _commit_on_branch(tmp_path, "peers/run/p1", "fix.py", "fix", peer="claude")
    dangling = _dangling_with_forged_note(tmp_path, tip, "codex")
    led = _ledger_over(tmp_path, tmp_path / "p.jsonl", "p1", dangling)
    # no head passed -> default HEAD; the any-ref backstop still rejects the
    # dangling (unreferenced) commit regardless of the anchor.
    assert is_converged(led.read(), mode_run="p1", repo=tmp_path) is False


# ---- edge: default anchor (HEAD) is fail-closed for an off-HEAD branch commit ---
def test_is_converged_default_head_is_fail_closed_for_branch_commit(tmp_path):
    _attested_repo(tmp_path)
    tip = _commit_on_branch(tmp_path, "peers/run/p1", "fix.py", "fix", peer="claude")
    led = _ledger_over(tmp_path, tmp_path / "p.jsonl", "p1", tip)
    # STRICT-ONLY: the commit is on peers/run/p1, not HEAD; with no head passed the
    # anchor defaults to HEAD and the gate fails CLOSED. The caller MUST pass the
    # run's branch/pinned ref (there is no any-ref backstop — it was agent-forgeable).
    assert is_converged(led.read(), mode_run="p1", repo=tmp_path) is False
