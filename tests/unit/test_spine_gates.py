"""STEP-8 — the fail-closed gate evaluator.

`evaluate_spine_gates(rows, *, mode_run, dry_n, repo)` returns a dict of pure,
default-deny predicates. The load-bearing one is **witness-ledgered**: it does
not merely check a witness dict is present — it RE-DERIVES every witness digest
from an out-of-band artifact (re-hash the file at `uri`; resolve the git commit),
so a fabricated `sha256` is rejected. This is the §2.2 self-greening closure.

Covers happy (a well-formed ledger → all gates pass), edge (git-sha witness
re-derivation; authorship vacuous when nothing claims independence; stop-on-dry
consistency), sad (missing/forged witness; unauthored independence; malformed
ModeRun; a constant-False stub would fail the green path).
"""
import hashlib
import subprocess

import pytest

from peers.spine.gates import all_pass, evaluate_spine_gates
from peers.spine.ledger import RunLedger
from peers.spine.op_config import OpConfig, load_op_config


def _file_witness(tmp_path, content="ok"):
    p = tmp_path / "evidence.txt"
    p.write_text(content)
    return {"kind": "file", "uri": str(p),
            "sha256": hashlib.sha256(content.encode()).hexdigest()}


def _git(p, *a):
    return subprocess.run(["git", "-C", str(p), *a], capture_output=True,
                          text=True, check=True).stdout


def _repo_with_commit(p):
    _git(p, "init", "-q")
    _git(p, "config", "user.email", "t@t")
    _git(p, "config", "user.name", "t")
    _git(p, "config", "commit.gpgsign", "false")
    (p / "a.py").write_text("a")
    _git(p, "add", "a.py")
    _git(p, "commit", "-q", "-m", "a")
    return _git(p, "rev-parse", "HEAD").strip()


def _attested_repo(p, peer="claude"):
    """init repo, two commits, attest the HEAD-delta to ``peer`` via the real
    ``refs/notes/peers-attest`` note; return the HEAD sha. This is the SANCTIONED
    way to build an authored ledger row in tests (no forge primitive)."""
    from peers import attest
    _git(p, "init", "-q")
    _git(p, "config", "user.email", "t@t")
    _git(p, "config", "user.name", "t")
    _git(p, "config", "commit.gpgsign", "false")
    (p / "a.py").write_text("a")
    _git(p, "add", "a.py")
    _git(p, "commit", "-q", "-m", "a")
    base = _git(p, "rev-parse", "HEAD").strip()
    (p / "b.py").write_text("b")
    _git(p, "add", "b.py")
    _git(p, "commit", "-q", "-m", "b")
    sha = _git(p, "rev-parse", "HEAD").strip()
    attest.attest_commits(p, peer, base, sha)
    return sha


# ----------------------------------------------------------------- happy path
def test_well_formed_ledger_all_gates_pass(tmp_path):
    sha = _attested_repo(tmp_path, "claude")
    led = RunLedger(tmp_path / "run.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="r1")
    led.append(event="bar-inferred", status="pass")
    led.append_attested(tmp_path, sha, event="confirmed-work", subject="u1",
                        status="pass", witness=_file_witness(tmp_path),
                        independence=True)
    res = evaluate_spine_gates(led.read(), mode_run="r1", repo=tmp_path)
    assert res["witness-ledgered"] is True
    assert res["authorship-attested"] is True
    assert res["ModeRun-valid"] is True
    assert res["stop-on-dry"] is True
    assert all_pass(res) is True


# ----------------------------------------------------------------- authorship
def test_authorship_gate_fails_on_unauthored_independence(tmp_path):
    led = RunLedger(tmp_path / "run.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="r1")
    led.append(event="confirmed-work", subject="u1", status="pass",
               witness=_file_witness(tmp_path), author=None, independence=True)
    res = evaluate_spine_gates(led.read(), mode_run="r1")
    assert res["authorship-attested"] is False


def test_authorship_vacuous_when_nothing_claims_independence(tmp_path):
    # edge: no independence claims -> nothing to forge -> gate True. No author
    # needed (independence=False), so a plain append suffices.
    led = RunLedger(tmp_path / "run.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="r1")
    led.append(event="confirmed-work", subject="u1", status="pass",
               witness=_file_witness(tmp_path), independence=False)
    assert evaluate_spine_gates(led.read(), mode_run="r1")["authorship-attested"] is True


# -- authorship is RE-DERIVED from the substrate note, not the agent-writable row
# (the §1 full-depth-analysis BLOCKER: .peers/run.jsonl is agent-writable; an agent
#  can hand-append a confirmed-work row with author="claude" + independence=True and
#  recompute the unkeyed sha256 chain, so verify() passes. The gate must re-derive
#  authorship from refs/notes/peers-attest, NOT trust r.author.)
from peers.spine.ledger import LedgerEntry  # noqa: E402


def test_authorship_gate_rejects_forged_git_sha_independence_row(tmp_path):
    # sad/security: a REAL but UN-attested commit + a hand-set author on an
    # independence git-sha row. witness-ledgered passes (the commit resolves), but
    # authorship must FAIL because resolve_author(repo, commit) is None.
    c = _repo_with_commit(tmp_path)          # a real commit with NO peers-attest note
    forged = LedgerEntry(event="confirmed-work", status="pass", author="claude",
                         independence=True,
                         witness={"kind": "git-sha", "uri": c, "sha256": c})
    res = evaluate_spine_gates([forged], mode_run="r1", repo=tmp_path)
    assert res["authorship-attested"] is False


def test_authorship_gate_rejects_file_witness_independence_without_attest_sha(tmp_path):
    # sad/security: a file-witness independence row with a hand-set author and NO
    # attesting-commit reference (attest_sha) cannot be re-derived -> fail-closed.
    forged = LedgerEntry(event="confirmed-work", status="pass", author="claude",
                         independence=True, witness=_file_witness(tmp_path))
    res = evaluate_spine_gates([forged], mode_run="r1", repo=tmp_path)
    assert res["authorship-attested"] is False


def test_authorship_gate_rejects_attest_sha_pointing_at_a_different_peer(tmp_path):
    # sad/security: the row claims author="claude" but its attest_sha resolves to
    # a DIFFERENT attested peer -> mismatch -> fail-closed (no decoy-commit forge).
    sha = _attested_repo(tmp_path, "codex")  # attested to codex, not claude
    forged = LedgerEntry(
        event="confirmed-work", status="pass", author="claude", independence=True,
        witness={"kind": "file", "uri": str(tmp_path / "a.py"),
                 "sha256": hashlib.sha256(b"a").hexdigest(), "attest_sha": sha})
    res = evaluate_spine_gates([forged], mode_run="r1", repo=tmp_path)
    assert res["authorship-attested"] is False


def test_authorship_gate_fails_closed_without_repo_on_independence(tmp_path):
    # edge: an independence row cannot be authorship-verified with no repo to
    # re-derive against -> fail-closed (no silent pass).
    forged = LedgerEntry(event="confirmed-work", status="pass", author="claude",
                         independence=True, witness=_file_witness(tmp_path))
    assert evaluate_spine_gates([forged], mode_run="r1")["authorship-attested"] is False


# ----------------------------------------------------------------- witness
def test_witness_gate_fails_without_witness(tmp_path):
    led = RunLedger(tmp_path / "run.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="r1")
    led.append(event="confirmed-work", subject="u1", status="pass")   # no witness
    assert evaluate_spine_gates(led.read(), mode_run="r1")["witness-ledgered"] is False


def test_witness_gate_fails_on_fabricated_sha(tmp_path):
    led = RunLedger(tmp_path / "run.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="r1")
    led.append(event="confirmed-work", subject="u1", status="pass",
               witness={"kind": "file", "uri": str(tmp_path / "nope.txt"),
                        "sha256": "x"})  # bogus
    assert evaluate_spine_gates(led.read(), mode_run="r1")["witness-ledgered"] is False


def test_witness_gate_fails_on_file_content_mismatch(tmp_path):
    # sad: the file exists but its real hash != the claimed sha256.
    led = RunLedger(tmp_path / "run.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="r1")
    wit = _file_witness(tmp_path, content="ok")
    (tmp_path / "evidence.txt").write_text("TAMPERED")   # change after the fact
    led.append(event="confirmed-work", subject="u1", status="pass", witness=wit)
    assert evaluate_spine_gates(led.read(), mode_run="r1")["witness-ledgered"] is False


def test_witness_gate_requires_at_least_one_confirmed_unit(tmp_path):
    # deliberate fail-closed tightening: a run that confirmed nothing has not
    # witnessed anything -> the witness gate must NOT vacuously green.
    led = RunLedger(tmp_path / "run.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="r1")
    led.append(event="bar-inferred", status="pass")
    assert evaluate_spine_gates(led.read(), mode_run="r1")["witness-ledgered"] is False


def test_witness_git_sha_re_derives(tmp_path):
    sha = _attested_repo(tmp_path, "claude")
    led = RunLedger(tmp_path / "run.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="r1")
    led.append_attested(
        tmp_path, sha, event="confirmed-work", subject="u1", status="pass",
        witness={"kind": "git-sha", "uri": sha, "sha256": sha}, independence=True)
    res = evaluate_spine_gates(led.read(), mode_run="r1", repo=tmp_path)
    assert res["witness-ledgered"] is True and all_pass(res) is True


def test_witness_git_sha_fabricated_digest_on_real_commit_fails(tmp_path):
    # BLOCKER regression: a real, existing commit at `uri` does NOT excuse a
    # fabricated `sha256`. The gate must re-derive: resolved(uri) must EQUAL the
    # claimed digest, not merely exist.
    sha = _repo_with_commit(tmp_path)
    led = RunLedger(tmp_path / "run.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="r1")
    led.append(event="confirmed-work", subject="u1", status="pass",
               witness={"kind": "git-sha", "uri": sha, "sha256": "deadbeef" * 5})
    assert evaluate_spine_gates(led.read(), mode_run="r1",
                                repo=tmp_path)["witness-ledgered"] is False


def test_witness_git_sha_symbolic_ref_with_lie_fails(tmp_path):
    # a symbolic ref (HEAD) that resolves to a real commit must still match the
    # claimed digest exactly.
    _repo_with_commit(tmp_path)
    led = RunLedger(tmp_path / "run.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="r1")
    led.append(event="confirmed-work", subject="u1", status="pass",
               witness={"kind": "git-sha", "uri": "HEAD", "sha256": "a" * 40})
    assert evaluate_spine_gates(led.read(), mode_run="r1",
                                repo=tmp_path)["witness-ledgered"] is False


def test_witness_git_sha_without_repo_fails_closed(tmp_path):
    sha = _repo_with_commit(tmp_path)
    led = RunLedger(tmp_path / "run.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="r1")
    led.append(event="confirmed-work", subject="u1", status="pass",
               witness={"kind": "git-sha", "uri": sha, "sha256": sha})
    # repo not provided -> the commit cannot be resolved -> rejected.
    assert evaluate_spine_gates(led.read(), mode_run="r1")["witness-ledgered"] is False


def test_witness_git_sha_unknown_commit_fails(tmp_path):
    _repo_with_commit(tmp_path)
    led = RunLedger(tmp_path / "run.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="r1")
    led.append(event="confirmed-work", subject="u1", status="pass",
               witness={"kind": "git-sha", "uri": "0" * 40, "sha256": "0" * 40})
    assert evaluate_spine_gates(led.read(), mode_run="r1",
                                repo=tmp_path)["witness-ledgered"] is False


def test_witness_unknown_kind_fails(tmp_path):
    led = RunLedger(tmp_path / "run.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="r1")
    led.append(event="confirmed-work", subject="u1", status="pass",
               witness={"kind": "vibe", "uri": "trust me", "sha256": "deadbeef"})
    assert evaluate_spine_gates(led.read(), mode_run="r1")["witness-ledgered"] is False


# ----------------------------------------------------------------- ModeRun-valid
def test_mode_run_valid_fails_on_empty_ledger():
    assert evaluate_spine_gates([], mode_run="r1")["ModeRun-valid"] is False


def test_mode_run_valid_fails_when_first_row_not_run_start(tmp_path):
    led = RunLedger(tmp_path / "run.jsonl")
    led.append(event="dry-round", status="dry")   # first row is not run-start
    assert evaluate_spine_gates(led.read(), mode_run="r1")["ModeRun-valid"] is False


def test_mode_run_valid_fails_without_op_config_witness(tmp_path):
    led = RunLedger(tmp_path / "run.jsonl")
    led.append(event="run-start", status="ok", mode_run="r1")   # no op-config witness
    assert evaluate_spine_gates(led.read(), mode_run="r1")["ModeRun-valid"] is False


def test_mode_run_valid_fails_on_mode_run_mismatch(tmp_path):
    led = RunLedger(tmp_path / "run.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="r1")
    assert evaluate_spine_gates(led.read(), mode_run="OTHER")["ModeRun-valid"] is False


# ----------------------------------------------------------------- stop-on-dry
def test_stop_on_dry_gate_fails_when_overdry_without_stop(tmp_path):
    # sad: the streak reached the threshold but the run never recorded a stop.
    led = RunLedger(tmp_path / "run.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="r1")
    for _ in range(3):
        led.append(event="dry-round", status="dry")
    res = evaluate_spine_gates(led.read(), mode_run="r1", dry_n=3)
    assert res["stop-on-dry"] is False


def test_stop_on_dry_gate_passes_when_overdry_with_stop(tmp_path):
    led = RunLedger(tmp_path / "run.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="r1")
    for _ in range(3):
        led.append(event="dry-round", status="dry")
    led.append(event="stop", status="dry")
    res = evaluate_spine_gates(led.read(), mode_run="r1", dry_n=3)
    assert res["stop-on-dry"] is True


# --------------------------------------------------------- constant-false stub
def test_constant_false_stub_would_fail_green_path(tmp_path):
    # A `return {g: False for g in GATES}` implementation cannot satisfy the
    # green-path test above; this asserts the green path is genuinely all-True
    # (so a stub regression is caught).
    sha = _attested_repo(tmp_path, "claude")
    led = RunLedger(tmp_path / "run.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="r1")
    led.append_attested(tmp_path, sha, event="confirmed-work", subject="u1",
                        status="pass", witness=_file_witness(tmp_path),
                        independence=True)
    res = evaluate_spine_gates(led.read(), mode_run="r1", repo=tmp_path)
    assert any(v for v in res.values())          # not a constant-False dict
    assert all_pass(res) is True


def test_public_append_rejects_caller_author(tmp_path):
    # the ONLY author path is append_attested; the public append must reject a
    # caller-supplied author.
    led = RunLedger(tmp_path / "run.jsonl")
    with pytest.raises(ValueError):
        led.append(event="confirmed-work", status="pass", author="claude")


def test_no_forge_author_primitive_on_ledger():
    # self-hosting hardening: the unguarded test-only forge helper was removed,
    # so there is no in-process way to write a caller-chosen author.
    assert not hasattr(RunLedger, "append_authored_for_test")
