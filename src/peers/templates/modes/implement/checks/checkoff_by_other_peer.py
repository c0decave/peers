#!/usr/bin/env python3
"""Exit 1 if any PLAN.md [x] checkoff was authored by the same peer that
last modified one of the step's ``touches:`` files.

Belt-and-suspenders backup for the Task 3.1 pre-commit hook
(``pre-commit-reviewer-checkoff``). The hook prevents *new* violating
commits at the time they are made, but it cannot catch violations that
landed in PRE-EXISTING commits — e.g. when the hook was not installed
when an old checkoff happened, or when commits were imported from
elsewhere. This gate scans the actual git log post-hoc, so a clean
``checkoff-by-other-peer`` run is positive evidence that every closed
step in PLAN.md was reviewed by the other peer.

Algorithm
---------
For every step in state ``done`` (``- [x]``) that declares
``touches: …``:

1. Locate the commit that toggled the step from ``[ ]`` to ``[x]``
   by walking ``git log --reverse --format=%H -- PLAN.md`` and
   inspecting each commit's diff on PLAN.md. The first commit whose
   diff contains both ``-- [ ] [STEP-N]`` (removed) and
   ``+- [x] [STEP-N]`` (added) is the checkoff commit.
2. Capture its author email (``%ae``).
3. For each file in ``touches:``, run
   ``git log -1 --format=%ae <CHECKOFF_SHA>~1 -- <file>`` to get the
   author of the most recent commit modifying that file BEFORE the
   checkoff. (Using ``~1`` excludes the checkoff commit itself, which
   might also touch the file in pathological cases.)
4. If the two emails match, record a violation.

Pass (exit 0) when no violations remain.
Fail (exit 1) with a per-violation diagnostic when any same-author
checkoff is detected.

Skip-friendly: steps without ``touches:`` cannot be enforced (we have
no implementation file to compare against), so they are not flagged
as failures here — Task 3.1's hook already warns on such cases at
commit time, and the operator can fall back on the
``plan-step-traceable`` gate for ground-truth coverage.

Missing PLAN.md or schema-invalid PLAN.md is a hard failure: the
substrate cannot evaluate the gate without a parseable plan.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from peers.attest import (
    attested_authors_of_file,
    attested_peer,
    find_review_commit,
)
from peers_ctl.plan_parser import PlanValidationError, parse_plan


# Match a PLAN.md step line on either side of a unified diff:
#   "+- [x] [STEP-3] add auth"     -> ("+", "x", "STEP-3")
#   "- - [ ] [STEP-3] add auth"    -> ("-", " ", "STEP-3")
# We accept optional surrounding whitespace after the sign because git
# diff sometimes emits "- - [ ]" with a space between the sign and the
# bullet (e.g. via ``--no-color``).
_DIFF_STEP_RE = re.compile(
    r"^(?P<sign>[+-])\s*-\s*\[(?P<mark>[ xX])\]\s*\[(?P<id>STEP-\d+)\]"
)


def _plan_commits(project_root: Path) -> list[str]:
    """Return SHAs of every commit touching PLAN.md, oldest-first."""
    proc = subprocess.run(
        ["git", "-C", str(project_root), "log", "--reverse",
         "--format=%H", "--", "PLAN.md"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _commit_plan_diff(project_root: Path, sha: str) -> str:
    """Return the PLAN.md unified diff introduced by commit ``sha``.

    For the root commit (no parent) we use ``git show`` which emits the
    full file as additions; for normal commits ``git show`` produces a
    diff against the first parent. Either way the +/- prefix on step
    lines is what we need.
    """
    proc = subprocess.run(
        ["git", "-C", str(project_root), "show", "--format=", sha, "--", "PLAN.md"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout


def _find_checkoff_commit(project_root: Path, step_id: str) -> str | None:
    """Return the SHA of the commit that toggled ``step_id`` from
    ``[ ]`` to ``[x]`` in PLAN.md, or ``None`` if no such transition
    exists in history.

    The transition is identified by a single commit's diff containing
    both an ``+- [x] [STEP-N]`` line AND an ``-- [ ] [STEP-N]`` line
    (i.e. the same step removed unchecked and re-added checked). BUG-007:
    return the LATEST such transition (not the first), so a peer can
    correct a wrong-author checkoff by unchecking and re-attesting — the
    gate then evaluates the CURRENT checkoff identity, not a stale one
    that would latch the step into a permanent failure.
    """
    latest: str | None = None
    for sha in _plan_commits(project_root):
        diff = _commit_plan_diff(project_root, sha)
        removed_open = False
        added_done = False
        for line in diff.splitlines():
            m = _DIFF_STEP_RE.match(line)
            if not m or m.group("id") != step_id:
                continue
            sign = m.group("sign")
            mark = m.group("mark").lower()
            if sign == "-" and mark == " ":
                removed_open = True
            elif sign == "+" and mark == "x":
                added_done = True
        if removed_open and added_done:
            latest = sha
    return latest


# Each peer's commits carry a `Peer: <name>` trailer in the message body,
# written by the orchestrator. This is the authoritative peer attribution:
# the git *author* is often a single shared identity (the container's git
# user), under which an email comparison cannot tell the two peers apart and
# the gate would silently pass. We key on the trailer and fall back to the
# author email only when no trailer is present (legacy / hand-made commits).
_PEER_TRAILER_RE = re.compile(r"^Peer:[ \t]*(\S+)[ \t]*$")
_TRAILER_LINE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*:[ \t]*.*$")


def _peer_identity_from_trailers(body: str) -> str:
    """Return the final trailer-block ``Peer:`` identity, if present.

    BUG-140: the prior implementation used ``re.search`` over the full body
    and returned the FIRST ``Peer:`` line, letting an implementer spoof
    attribution by smuggling a fake ``Peer: <other>`` line into the prose
    above a real bottom ``Peer: <self>`` trailer. Mirrors the BUG-139 fix in
    ``pre-commit-reviewer-checkoff`` so both layers agree on identity.
    """
    lines = [line.rstrip("\r") for line in body.rstrip().splitlines()]
    for line in reversed(lines):
        if line.strip() == "":
            break
        m = _PEER_TRAILER_RE.match(line)
        if m:
            return f"peer:{m.group(1).lower()}"
        if not _TRAILER_LINE_RE.match(line):
            break
    return ""


CommitIdentity = tuple[str, str]


def _commit_identity(project_root: Path, sha: str) -> CommitIdentity:
    """Return ``(peer_identity, email_identity)`` for ``sha``.

    Identity resolution priority:

    1. The substrate-written ``peers-attest`` git note — agent-unforgeable,
       derived from the tick HEAD-delta. This OVERRIDES the commit's ``Peer:``
       trailer, so a peer cannot reattribute its own implementation by stamping
       the other peer's name on its commit.
    2. The ``Peer:`` trailer — for legacy / not-yet-attested commits (e.g.
       history imported before this fix, or made outside the loop).
    3. The author email — last-resort fallback so mixed peer/email histories
       can fail closed when they share one git author identity.
    """
    note = attested_peer(project_root, sha)
    if note:
        peer_id = f"peer:{note.lower()}"
    else:
        peer_id = ""
        proc = subprocess.run(
            ["git", "-C", str(project_root), "log", "-1", "--format=%B", sha],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            peer_id = _peer_identity_from_trailers(proc.stdout)
    proc = subprocess.run(
        ["git", "-C", str(project_root), "log", "-1", "--format=%ae", sha],
        capture_output=True, text=True,
    )
    ae = proc.stdout.strip() if proc.returncode == 0 else ""
    email_id = f"email:{ae}" if ae else ""
    return (peer_id, email_id)


def _identity_known(identity: CommitIdentity) -> bool:
    return bool(identity[0] or identity[1])


def _identity_matches(impl: CommitIdentity, checkoff: CommitIdentity) -> bool:
    """True when the two commit identities prove the same principal.

    If both commits carry peer trailers, compare only peer identities; this
    preserves the shared-git-author case where claude and codex commit under
    one email. If either side lacks a peer trailer, fall back to matching
    author email so malformed/legacy trailer histories with the same shared
    email do not pass as "reviewed by another peer" without evidence.
    """
    impl_peer, impl_email = impl
    checkoff_peer, checkoff_email = checkoff
    if impl_peer and checkoff_peer:
        return impl_peer == checkoff_peer
    return bool(impl_email and checkoff_email and impl_email == checkoff_email)


def _identity_label(identity: CommitIdentity) -> str:
    peer_id, email_id = identity
    if peer_id and email_id:
        return f"{peer_id}/{email_id}"
    return peer_id or email_id or "<unknown>"


def _last_impl_identity(
    project_root: Path, checkoff_sha: str, path: str
) -> CommitIdentity:
    """Peer identity of the most recent commit modifying ``path`` strictly
    before ``checkoff_sha``.

    Returns an empty identity if no such commit exists (file was never touched
    before the checkoff — a different failure that ``plan-step-traceable``
    catches).
    """
    proc = subprocess.run(
        ["git", "-C", str(project_root), "log", "-1", "--format=%H",
         f"{checkoff_sha}~1", "--", path],
        capture_output=True, text=True,
    )
    impl_sha = proc.stdout.strip() if proc.returncode == 0 else ""
    if not impl_sha:
        return ("", "")
    return _commit_identity(project_root, impl_sha)


def _has_independent_review(
    project_root: Path, path: str, impl_id: CommitIdentity
) -> bool:
    """True when an independent reviewer signed off on ``path`` via a
    substrate-attested ``peers-review: <path>`` commit — i.e. the two-key rule
    is satisfied for a co-implemented file the checkoff peer also authored.

    Only honored when the implementer has a peer identity (the normal
    attested/``Peer:``-trailer case). For legacy email-only commits we cannot
    attribute a peer-name implementer to exclude, so no escape applies (the
    step must then be checked off by a genuinely different peer).

    FU-2 (P0 review 2026-06-12): the previous escape consulted the
    justifications.log reviewer field, which is agent-authored free text bound
    to no unforgeable identity — a peer could forge an entry naming the other
    peer (a self-bless). The escape is now bound to the unforgeable
    ``refs/notes/peers-attest`` note via :func:`peers.attest.find_review_commit`,
    which searches REACHABLE history for a ``peers-review: <path>`` commit the
    substrate attributed to a peer OTHER than the implementer. The agent never
    chooses the sha (mirroring ``_find_checkoff_commit``), so the dangling-note,
    no-backing, and cite-an-unrelated-commit forges are all closed.
    """
    impl_peer = impl_id[0]
    if not impl_peer:
        return False
    # Exclude EVERY attested author of the file, not just the implementer — a
    # peer could otherwise author the file, have a co-peer make a trivial edit
    # (becoming the "last editor"), then self-review (the original author would
    # no longer be excluded). The checkoff gate is file-level (touches:), so we
    # exclude the whole-file author set, plus the resolved implementer
    # (impl_peer is "peer:<name>"; find_review_commit compares against the raw
    # peers-attest note value).
    exclude = attested_authors_of_file(project_root, path)
    exclude.add(impl_peer.split(":", 1)[1] if ":" in impl_peer else impl_peer)
    return find_review_commit(project_root, path, exclude_peer=exclude) is not None


def main(project_dir: str = ".") -> int:
    """Verify all step checkoffs were done by a different peer than the
    implementer of the step's ``touches:`` files.

    Belt-and-suspenders with the pre-commit hook from Task 3.1.

    Skip-friendly: steps without ``touches:`` declared cannot be
    enforced and are not flagged as failures (defer to operator
    awareness via the hook's stderr warning).
    """
    project_root = Path(project_dir).resolve()
    plan_path = project_root / "PLAN.md"
    if not plan_path.is_file():
        print("checkoff-by-other-peer FAIL: PLAN.md not found")
        return 1
    try:
        plan = parse_plan(plan_path)
    except PlanValidationError as e:
        print(f"checkoff-by-other-peer FAIL: PLAN.md invalid: {e}")
        return 1

    checked_with_touches = [
        s for s in plan.steps if s.state == "done" and s.touches
    ]
    if not checked_with_touches:
        print("checkoff-by-other-peer: clean (no enforceable checkoffs)")
        return 0

    violations: list[str] = []
    for step in checked_with_touches:
        checkoff_sha = _find_checkoff_commit(project_root, step.id)
        if not checkoff_sha:
            # No [ ]->[x] transition found in history. Could be a
            # step that was born [x] (rare) or whose toggle predates
            # PLAN.md being tracked. We cannot enforce here; defer.
            continue
        checkoff_id = _commit_identity(project_root, checkoff_sha)
        if not _identity_known(checkoff_id):
            continue
        for tf in step.touches:
            impl_id = _last_impl_identity(project_root, checkoff_sha, tf)
            if not _identity_known(impl_id):
                # File was new in checkoff commit, or untracked before:
                # plan-step-traceable handles that class of failure.
                continue
            if not _identity_matches(impl_id, checkoff_id):
                # Checked off by a DIFFERENT peer than the file's author =
                # independently reviewed by the checkoff itself. OK.
                continue
            # the same peer implemented AND checked off this file (the
            # norm for co-implemented steps). Valid only if the OTHER peer made
            # an independent, substrate-attested `peers-review: <file>` commit
            # (FU-2) — otherwise it is a self-bless.
            if _has_independent_review(project_root, tf, impl_id):
                continue
            violations.append(
                f"  {step.id}: {tf} implemented and checked off by the same "
                f"peer ({_identity_label(impl_id)}) with no independent "
                f"review — the OTHER peer must sign off with a "
                f"`peers-review: {tf}` commit"
            )

    if violations:
        print(
            f"checkoff-by-other-peer FAIL: "
            f"{len(violations)} same-author checkoff violation(s):"
        )
        for v in violations:
            print(v)
        print(
            "  hint: implement-mode requires the OTHER peer to mark steps "
            "[x] after review"
        )
        return 1

    print(
        f"checkoff-by-other-peer: clean "
        f"({len(checked_with_touches)} checkoff(s) verified, all by other peer)"
    )
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
