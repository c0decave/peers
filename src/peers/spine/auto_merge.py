"""Stage 6 (§6.3) — the auto-merge EXECUTION primitive.

Deciding ``auto-merge`` (:func:`peers.spine.landing.build_landing_contract`) is
necessary but NOT sufficient: the branch may have advanced between the decision
and the merge (TOCTOU). :func:`land` is the separate primitive that re-verifies
EVERYTHING at merge time and only then performs the merge:

1. **the decision** — reuse ``build_landing_contract`` (the S2 conjunction; never
   re-implemented). Not ``auto-merge`` ⇒ ``not-auto-merge``, no merge.
2. **the SOURCE is the attested CONVERGED commit** — ``propagate._converged_commit``
   (the sha bound to the ledger), NEVER the live ``rev-parse <branch>`` tip a
   producer may have advanced past convergence to un-attested code (the Stage-5
   REVIEW-A lesson). The converged commit MUST itself be substrate-attested
   (``resolve_author is not None``) BEFORE any merge — else ``unattested-converged``,
   so no ``independence=True``/``author=None`` row ever poisons the append-only
   authorship-attested gate (REVIEW-B).
3. **a FRESH RECHECK** over a FRESHLY re-read ledger — re-pin ``converged``
   (refuse ``converged-moved``), re-``evaluate_spine_gates``, and run the injected
   ``recheck`` on a fresh detached worktree leased AT the converged commit (the
   converged TREE, not the live tip).
4. **re-detect self-hosting** on the converged diff vs the RUN's RECORDED base
   (``run.base_sha`` — never ``merge-base(target_ref, …)``, which an attacker-
   influenced ``target_ref`` could shift past the spine touch; a missing base is
   the NAMED ``undeterminable-base``, not silently folded into self-hosting).
5. **the merge** — an ATOMIC compare-and-swap fast-forward of the canonical
   ``refs/heads/<name>`` (checkout-free; the Stage-5 "never mutate a branch a
   worktree holds" lesson). A non-ff / CAS-race / tag-shadowed branch ⇒
   ``merge-conflict``, the target is NEVER touched.
6. **an attested ``landed`` row** — author = the producer's attested peer (NEVER
   re-attested); ``independence = author is not None`` (never the literal ``True``
   — the second REVIEW-B layer).

Every error path is fail-closed ``LandingResult(landed=False, ...)`` (S5).
"""
from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from peers.spine.authorship import resolve_author
from peers.spine.gates import all_pass, evaluate_spine_gates, resolves_to_commit
from peers.spine.landing import build_landing_contract
from peers.spine.propagate import _converged_commit
from peers.spine.self_hosting import is_self_hosting

__all__ = ["LandingResult", "land", "tighten_only_advisory"]


@dataclass
class LandingResult:
    """The outcome of an :func:`land` attempt (scalar defaults only — NO mutable
    default). ``landed`` is the only required field; ``merged_sha``/``target_ref``
    are set on success, ``reason`` names the fail-closed cause otherwise."""

    landed: bool
    merged_sha: str | None = None
    target_ref: str | None = None
    reason: str = ""


def _git(repo, *args) -> subprocess.CompletedProcess:
    """The single fail-closed git seam (same shape as ``propagate._git``). ALL of
    ``land()``'s git calls route through here so the CAS-race test can monkeypatch
    ONE point and observe the moment just before the compare-and-swap write."""
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=120, check=False)


def _changed_paths(repo, base, commit) -> list[str] | None:
    """The changed paths of ``base..commit`` (``--no-renames -z``), or ``None`` on
    any git failure (an undeterminable diff ⇒ the caller's ``is_self_hosting``
    fails safe to self-hosting). ``--no-renames`` surfaces a rename-out-of-
    governance as a delete of the OLD path (B1); ``-z`` emits raw UTF-8 with NO
    C-quoting and splits on NUL (never ``splitlines()``, never honouring
    ``core.quotePath`` — B2). ``base`` is the RUN's recorded fork point."""
    r = _git(repo, "diff", "--name-only", "--no-renames", "-z", f"{base}..{commit}")
    if r.returncode != 0:
        return None
    return [p for p in r.stdout.split("\x00") if p]


def _canonical_branch_ref(repo, target_ref) -> str | None:
    """Normalise ``target_ref`` to ONE canonical ``refs/heads/<name>`` for BOTH
    the read and the write (B6). Reject anything that is not an EXISTING local
    branch, fail-closed: a tag, a remote-tracking ref, ``HEAD``, an option-like
    token, an already-``refs/heads/``-prefixed double, or a name with no such
    branch. The branch must resolve via ``rev-parse`` of the FULL ref (a bare name
    resolves through tag/remote precedence; the full ref does not)."""
    # Reject already-qualified non-branch / double-prefixed / empty / HEAD up front.
    if (not isinstance(target_ref, str) or not target_ref
            or target_ref == "HEAD" or target_ref.startswith("-")
            or target_ref.startswith("refs/heads/refs/")
            or target_ref.startswith("refs/remotes/")
            or target_ref.startswith("refs/tags/")):
        return None
    name = target_ref[len("refs/heads/"):] if target_ref.startswith("refs/heads/") else target_ref
    if "/" in name and name.split("/")[0] in ("refs",):   # any other refs/* form
        return None
    full = f"refs/heads/{name}"
    # the branch must EXIST as a local branch (rev-parse the FULL ref, not the bare
    # name -- a bare name resolves through tag/remote precedence; the full ref does
    # not). rc=0 + a 40-hex sha => a real local branch.
    r = _git(repo, "rev-parse", "--verify", "--quiet", full)
    if r.returncode != 0 or len(r.stdout.strip()) != 40:
        return None
    return full


def land(run, provider, *, target_ref: str, recheck: Callable[[Path, str], bool],
         repo) -> LandingResult:
    """Execute (or fail-closed refuse) an ``auto-merge`` landing for ``run``.

    ``recheck(worktree_path, converged_sha) -> bool`` is the injected test re-run
    (run on a FRESH detached worktree leased AT the converged commit via
    ``provider``). ``provider`` is the Stage-5 ``WorktreeProvider`` port; ``repo``
    is the shared repo whose ``refs/heads/<target_ref>`` is the merge target.
    Returns a :class:`LandingResult`; every error path is fail-closed (S5)."""
    try:
        rows = run.ledger.read()
        # 1. the DECISION: build the contract from the live ledger (S2 reused, not
        #    re-implemented). self_hosting=False HERE only asks "does the run
        #    otherwise qualify?"; the AUTHORITATIVE self-hosting re-detection is
        #    step 4 below on the real diff (S4).
        lc = build_landing_contract(
            rows, repo=repo, mode_run=run.mode_run, branch=run.branch or "",
            head_sha=_converged_commit(rows),
            landing_mode=run.op_config.landing, self_hosting=False)
        if lc.landing_mode != "auto-merge":
            return LandingResult(landed=False, reason="not-auto-merge")
        if run.branch is None:
            return LandingResult(landed=False, reason="no-isolated-branch")
        # 1b. B6: normalise the target to a single canonical refs/heads/<name>;
        #     reject a tag / remote / double-prefixed / non-existent / HEAD target.
        branch_ref = _canonical_branch_ref(repo, target_ref)
        if branch_ref is None:
            return LandingResult(landed=False, reason="bad-target-ref")
        # 2. S3: the merge SOURCE is the attested CONVERGED commit, never the live tip.
        converged = _converged_commit(rows)
        if converged is None or not resolves_to_commit(Path(repo), converged):
            return LandingResult(landed=False, reason="no-converged-commit")
        # 2b. B5 / REVIEW-B (defense in depth, BEFORE any merge): the converged
        #     commit MUST itself be substrate-attested. _converged_commit returns
        #     the witness sha, NOT the attested-author commit -- a forged witness
        #     could point at a real-but-UNATTESTED commit. Refuse here so no
        #     independence=True / author=None row is ever written (append-only).
        author = resolve_author(Path(repo), converged)
        if author is None:
            return LandingResult(landed=False, reason="unattested-converged")
        # 3. S3: FRESH RECHECK. RE-READ the ledger immediately before the merge (a
        #    genuinely fresh read -- a concurrent poisoning/advancing append
        #    between the decision and here is now OBSERVED, and `converged` is
        #    RE-PINNED from it) and re-confirm the converged commit + attestation.
        rows2 = run.ledger.read()
        converged2 = _converged_commit(rows2)
        if converged2 is None or converged2 != converged:
            return LandingResult(landed=False, reason="converged-moved")
        if resolve_author(Path(repo), converged2) is None:
            return LandingResult(landed=False, reason="unattested-converged")
        # re-evaluate the spine gates on the FRESH rows AND run the injected test
        # re-run on a FRESH detached checkout of the CONVERGED commit (the converged
        # TREE), leased via the injected provider -- NOT the live branch tip the
        # producer may have advanced past convergence.
        gates = evaluate_spine_gates(rows2, mode_run=run.mode_run, repo=repo,
                                     head=run.branch or "HEAD")  # HONEST-01 anchor
        if not all_pass(gates):
            return LandingResult(landed=False, reason="recheck-failed")
        with provider.lease(repo, f"recheck-{run.mode_run}", base=converged) as rws:
            if not recheck(rws.worktree_path, converged):
                return LandingResult(landed=False, reason="recheck-failed")
        # 4. S4: RE-DETECT self-hosting on the converged diff vs the RUN's RECORDED
        #    base (defense in depth). NEVER merge-base(target_ref, converged): an
        #    attacker-influenced target_ref could shift the base past the spine-
        #    touching commit and shrink the detection window. The run base is the
        #    lease-time fork point; a missing base is a NAMED fail-closed reason.
        base = getattr(run, "base_sha", None)
        if not base or not resolves_to_commit(Path(repo), base):
            return LandingResult(landed=False, reason="undeterminable-base")
        changed = _changed_paths(repo, base, converged)
        hosting, _reason = is_self_hosting(repo, changed_paths=changed, target_repo=repo)
        if hosting:
            return LandingResult(landed=False, reason="self-hosting")
        # 5. the merge: an ATOMIC compare-and-swap fast-forward of
        #    refs/heads/<branch> (B4) -- checkout-free. Resolve the BRANCH sha (not
        #    the bare name -> no tag/remote precedence, B6), ancestry-check it,
        #    then 4-arg update-ref CAS so a target that raced forward between the
        #    capture and the write is REFUSED, never clobbered.
        old = _git(repo, "rev-parse", "--verify", "--quiet", branch_ref)
        if old.returncode != 0 or len(old.stdout.strip()) != 40:
            return LandingResult(landed=False, reason="merge-conflict")
        old_sha = old.stdout.strip()
        anc = _git(repo, "merge-base", "--is-ancestor", old_sha, converged)
        if anc.returncode != 0:                       # branch is NOT an ancestor -> non-ff
            return LandingResult(landed=False, reason="merge-conflict")
        upd = _git(repo, "update-ref", branch_ref, converged, old_sha)   # 4-arg CAS
        if upd.returncode != 0:                       # the branch raced -> refused, untouched
            return LandingResult(landed=False, reason="merge-conflict")
        # 6. record the ATTESTED landed row (author = the producer's attested peer
        #    of `converged` -- NEVER re-attested; the merger does not re-author the
        #    work). independence = (author is not None) -- a SECOND REVIEW-B layer:
        #    never the literal True, so dropping the 2b guard still cannot write an
        #    unattested independence row.
        run.ledger.append_attested(
            repo, converged, event="landed", subject=branch_ref, status="ok",
            witness={"kind": "git-sha", "uri": converged, "sha256": converged,
                     "target_ref": branch_ref, "from_branch": run.branch},
            independence=author is not None, mode_run=run.mode_run)
        return LandingResult(landed=True, merged_sha=converged, target_ref=branch_ref)
    except Exception as e:            # S5: ANY error on the merge path -> no merge.
        return LandingResult(landed=False, reason=f"error:{type(e).__name__}")


# A gate-registration key line in gates.py: the ``"<name>":`` keys of the
# evaluate_spine_gates return dict (e.g. ``"witness-ledgered": True,``).
_GATE_KEY_RE = re.compile(r'^\s*"([A-Za-z0-9_-]+)":', re.MULTILINE)


def _gate_keys_at(repo, rev) -> set[str] | None:
    """The set of gate-registration keys in ``gates.py`` at ``rev`` (the
    ``"<name>":`` keys of the ``evaluate_spine_gates`` return dict), or ``None`` on
    any git/parse error (an undeterminable side ⇒ the caller fails safe)."""
    r = _git(repo, "show", f"{rev}:src/peers/spine/gates.py")
    if r.returncode != 0:
        return None
    return set(_GATE_KEY_RE.findall(r.stdout))


def tighten_only_advisory(repo, *, base, head) -> dict:
    """Flag a gate-WEAKENING (a gate registration present at ``base`` but absent at
    ``head``) in a self-hosting run's diff — recorded for the §6.3 human-review
    seam. Adding a gate is STRENGTHENING (``weakens=False``). Fail-safe: any
    undeterminable diff ⇒ ``weakens=True`` (the reviewer is told it could not be
    cleared, never a silent pass). Advisory ONLY — self-hosting is branch-pr
    regardless; this does not block. Returns
    ``{"weakens": bool, "removed_gates": list[str], "reason": str}``."""
    try:
        before = _gate_keys_at(repo, base)
        after = _gate_keys_at(repo, head)
        if before is None or after is None:
            return {"weakens": True, "removed_gates": [], "reason": "undeterminable"}
        removed = sorted(before - after)
        return {"weakens": bool(removed), "removed_gates": removed,
                "reason": "gate-removed" if removed else ""}
    except Exception:
        return {"weakens": True, "removed_gates": [], "reason": "undeterminable"}
