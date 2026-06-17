"""STEP-8 — the fail-closed spine gate evaluator.

The gates consume **ledger entries**, not free text, and every one is a pure,
default-deny predicate: a missing precondition evaluates to ``False``, never
``True``. Four gates make up the Stage-0 spine suite:

- **ModeRun-valid** — row 0 is a ``run-start`` carrying an ``op-config`` witness
  and a ``mode_run`` (matching the one under evaluation).
- **witness-ledgered** — the §2.2 self-greening closure. There must be at least
  one ``confirmed-work`` row, and EVERY such row's witness digest must
  **re-derive** from an out-of-band artifact: a ``file`` witness is re-hashed
  from disk; a ``git-sha`` witness must resolve (via ``git rev-parse``) to a
  commit whose full SHA **equals the claimed digest** (``repo`` required) — so a
  fabricated digest pointed at a real commit, or a symbolic ref that does not
  resolve to exactly the claim, is rejected. A fabricated ``sha256`` (incl. the
  ``"x"`` sentinel), a missing file, a content mismatch, an unresolvable ref, or
  an unknown witness kind → rejected.
- **authorship-attested** — every entry that claims ``independence=True`` carries
  a non-None ``author``. NOTE: this gate only checks author *presence*; the
  substrate guarantee that a non-None author is *real* is upheld UPSTREAM by
  Invariant 1 — ``append()`` rejects caller-supplied authors and
  ``append_attested`` (substrate note) is the only path that sets one — so the
  gate is sound for any ledger written through the sanctioned API. It does not,
  on its own, re-derive authorship from the attestation note; a Stage-4 follow-up
  would add an attesting commit-SHA to independence rows and verify via
  ``attest.attested_peer`` (so a hand-edited JSONL with a recomputed digest is
  also caught). A forged independence claim with no author is rejected here.
- **stop-on-dry** — the run is in a consistent terminal state: if the dry streak
  reached ``dry_n`` the ledger must contain a ``stop`` row (the run did not
  ignore its own stop condition).

``all_pass(res)`` is ``all(res.values())``.
"""
from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Sequence
from pathlib import Path

from peers.safe_io import read_bytes_no_symlink
from peers.spine.ledger import LedgerEntry
from peers.spine.stop_on_dry import dry_streak

#: A sha256 we treat as obviously-bogus regardless of artifact (the contract's
#: fabricated-witness sentinel).
_BOGUS_SHA = "x"

#: Upper bound on a file-kind witness artifact re-derived by the gate. The
#: ``uri`` is agent-controlled (run.jsonl is agent-writable), so a multi-GB
#: file must not OOM the gate evaluator. 64 MiB is far above any real
#: confirmed-work artifact (reports are KB-MB); an oversized read truncates
#: and the sha will not match, so the witness fails closed.
_WITNESS_MAX_BYTES = 64 * 1024 * 1024


def _git_resolve_commit(repo: Path, rev: str) -> str | None:
    """Resolve ``rev`` to a full 40-hex commit SHA in ``repo``, or ``None``.
    Fail-closed: any subprocess/OS error, a non-commit, or an unresolvable ref
    → ``None``."""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
             f"{rev}^{{commit}}"],
            capture_output=True, text=True,
        )
    except (OSError, ValueError):
        return None
    out = r.stdout.strip().lower()
    if r.returncode != 0 or len(out) != 40:
        return None
    return out


def _attest_reachable(repo: Path, sha: str, head: str) -> bool:
    """The attesting commit must be reachable from the run's tip ``head`` — the
    SUBSTRATE-controlled ref the caller passes (the run's branch, or its pinned
    ``refs/peers/fleet/<id>`` post-teardown). ``attest_commits`` only writes
    ``peers-attest`` notes on commits in the observed ``(since, head]`` range
    (ancestors of the run's head), so an attest_sha NOT reachable from ``head`` is
    a forge (the HONEST-01 dangling/out-of-range-note variant).

    STRICT-ONLY by design — there is NO "reachable from any ref" backstop: every
    ref namespace (refs/heads/*, refs/tags/*, refs/peers/*) is agent-writable from
    the run worktree, so an agent could ``git update-ref`` a side ref at a forged
    commit and pass an any-ref check (adversarial-review finding). The substrate
    chooses ``head``; the agent cannot influence which ref the gate anchors on."""
    from peers.attest import is_ancestor

    return is_ancestor(repo, sha, head)


def resolves_to_commit(repo: Path, sha: object) -> bool:
    """Public, shared commit-resolver. True iff ``sha`` is a string that resolves
    to a commit whose FULL 40-hex id EQUALS ``sha`` in ``repo`` — exactly the
    condition the ``witness-ledgered`` gate re-derives for a ``git-sha`` witness.
    Fail-closed on any error / non-commit / abbreviated / symbolic ref.

    This is the SOLE resolver both research (``peers.research.frontend``) and
    (later) develop import — no copy-paste fork. develop's module-private
    ``_resolves_to_commit`` SHOULD migrate to this — flag-at-review.
    """
    if not isinstance(sha, str) or not sha:
        return False
    resolved = _git_resolve_commit(repo, sha)
    return resolved is not None and resolved == sha.lower()


def _witness_re_derives(witness: object, repo: Path | None) -> bool:
    """Re-derive a witness digest from its out-of-band artifact. Fail-closed for
    anything we cannot positively re-derive."""
    if not isinstance(witness, dict):
        return False
    kind = witness.get("kind")
    claimed = witness.get("sha256")
    if not isinstance(claimed, str) or not claimed or claimed == _BOGUS_SHA:
        return False

    if kind == "file":
        uri = witness.get("uri")
        if not isinstance(uri, str) or not uri:
            return False
        try:
            # Use the SAME hardened reader the producer uses (baseline.py:
            # read_bytes_no_symlink): `uri` comes from the agent-writable
            # run.jsonl, so fail closed on a symlinked / non-regular /
            # hard-linked / oversized file rather than follow it.
            data = read_bytes_no_symlink(Path(uri), max_bytes=_WITNESS_MAX_BYTES)
        except OSError:
            return False                    # missing / unreadable / unsafe file
        return hashlib.sha256(data).hexdigest() == claimed

    if kind == "git-sha":
        if repo is None:
            return False                    # cannot resolve a commit -> reject
        # Resolve the reference and require it to BE the claimed commit identity.
        # Merely existing is not enough (the old hole): a fabricated digest
        # pointed at a real commit, or a symbolic ref (HEAD/branch) that does not
        # resolve to exactly `claimed`, must fail.
        ref = witness.get("uri")
        if not isinstance(ref, str) or not ref:
            ref = claimed
        resolved = _git_resolve_commit(repo, ref)
        if resolved is None:
            return False
        return resolved == claimed.lower()

    return False                            # unknown witness kind -> reject


def _gate_mode_run_valid(rows: Sequence[LedgerEntry], mode_run: str | None) -> bool:
    if not rows:
        return False
    first = rows[0]
    if first.event != "run-start":
        return False
    wit = first.witness
    if not isinstance(wit, dict) or wit.get("kind") != "op-config":
        return False
    if first.mode_run is None:
        return False
    if mode_run is not None and first.mode_run != mode_run:
        return False
    return True


def _gate_witness_ledgered(rows: Sequence[LedgerEntry], repo: Path | None) -> bool:
    confirmed = [r for r in rows if r.event == "confirmed-work"]
    if not confirmed:
        return False                        # nothing witnessed -> no self-green
    return all(_witness_re_derives(r.witness, repo) for r in confirmed)


def _gate_authorship_attested(
    rows: Sequence[LedgerEntry], repo: Path | None, *, head: str = "HEAD",
) -> bool:
    # Every independence row's author is RE-DERIVED from the substrate
    # ``refs/notes/peers-attest`` note, NOT trusted off the row. ``.peers/run.jsonl``
    # is agent-writable and the hash-chain is unkeyed (no secret), so an agent can
    # hand-append a ``confirmed-work`` row with ``author="claude"`` + a recomputed
    # chain that ``verify()`` still accepts (full-depth-analysis §1). The binding
    # the row claims is therefore re-checked here: ``append_attested`` stamps the
    # attesting commit (``witness['attest_sha']``) and we require
    # ``resolve_author(repo, attest_sha) == author`` (non-None). A forged row (no
    # ``attest_sha``, an unattested commit, or a decoy commit attested to a
    # different peer) fails closed. ``repo`` is required to re-derive — its absence
    # on an independence row is itself fail-closed.
    #
    # HONEST-01 (HIGH): ``attested_peer`` is a bare ``git notes show`` with NO
    # reachability constraint, so a peer could ``commit-tree`` a dangling object,
    # ``git notes --ref=peers-attest add`` a forged peer name on it, and cite it as
    # ``attest_sha`` — the FU-2 dangling/out-of-range-note forge (fixed for the
    # checkoff/review gates, NOT this one; ``reachable_attested_peer`` had 0
    # callers). The attesting commit MUST be reachable from the run's tip
    # (``head``): ``attest_commits`` only writes notes on commits in the observed
    # ``(since, head]`` range (ancestors of the run's head), so a note on an
    # out-of-branch commit is a forge. ``head`` defaults to ``HEAD`` (fail-closed
    # for a branch-isolated run whose caller did not pass the branch); production
    # callers pass ``run.branch`` / the run's pinned ref.
    from peers.spine.authorship import resolve_author

    for r in rows:
        if not r.independence:
            continue
        if r.author is None or repo is None:
            return False
        wit = r.witness if isinstance(r.witness, dict) else {}
        attest_sha = wit.get("attest_sha")
        if not isinstance(attest_sha, str) or not attest_sha:
            return False                    # no verifiable attesting commit
        if not _attest_reachable(repo, attest_sha, head):
            return False                    # dangling / out-of-range-note forge
        if resolve_author(repo, attest_sha) != r.author:
            return False                    # unattested / forged / decoy-peer author
    return True


def _gate_stop_on_dry(rows: Sequence[LedgerEntry], dry_n: int) -> bool:
    try:
        streak = dry_streak(rows)
    except Exception:
        return False
    if streak >= dry_n:
        return any(r.event == "stop" for r in rows)
    return True


def evaluate_spine_gates(
    rows: Sequence[LedgerEntry],
    *,
    mode_run: str | None = None,
    dry_n: int = 3,
    repo: Path | str | None = None,
    head: str = "HEAD",
) -> dict[str, bool]:
    """Evaluate the Stage-0 spine gate suite over ``rows``. Returns a
    ``{gate_name: bool}`` dict; every gate is default-deny. ``head`` is the run's
    tip (branch/pinned ref) the authorship gate anchors reachability on (HONEST-01)."""
    repo_path = Path(repo) if repo is not None else None
    return {
        "ModeRun-valid": _gate_mode_run_valid(rows, mode_run),
        "witness-ledgered": _gate_witness_ledgered(rows, repo_path),
        "authorship-attested": _gate_authorship_attested(rows, repo_path, head=head),
        "stop-on-dry": _gate_stop_on_dry(rows, dry_n),
    }


def all_pass(res: dict[str, bool]) -> bool:
    """True iff every gate passed (and at least one gate was evaluated)."""
    return bool(res) and all(res.values())
