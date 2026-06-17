"""Stage-7 fleet: the program DAG + the fail-closed program validator.

A ``Program`` is the fleet's input: a DAG of ``ModeRunSpec``s the client owns
(*what* to run), which the fleet schedules (*when/how*). ``validate_program`` is
the first trust boundary -- it rejects a malformed program WHOLE (never a partial
schedule that runs the valid half and silently drops the rest), reports EVERY
defect (never short-circuits on the first), and is PURE/DETERMINISTIC apart from
one bounded ``is_dir`` stat + one ``git rev-parse --git-dir`` per spec for the
tool-root check.

The spine is UNCHANGED: this module only consumes ``op_config.ALLOWED_MODES`` and
the Stage-5 ``worktree.workspace_names`` namer (the branch is a PURE function of
``run_id``, which is what lets the collision check prove non-collision by name).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from peers.spine.op_config import ALLOWED_MODES, OpConfig
from peers.spine.worktree import workspace_names

# What artifact each producer MODE can emit (the cross-tool dependency contract).
# The ONLY artifact a CROSS-TOOL propagation can transfer is a git-sha branch
# (`propagate_branch` pins refs/propagated/<from> to the producer's converged
# git-sha tip; a file/finding artifact has no propagatable branch commit ->
# _converged_commit returns None for it).
#   develop             -> a git-sha branch (an implemented, committed fix)
#   research            -> a file report (a committed report file; NOT a branch)
#   find-bugs:reproduce -> a finding (a reproduced, witnessed, CONVERGED-able defect)
#   bring-up            -> a git-sha branch (an attested fix landed branch-PR; like develop)
# Non-git-sha producers (research=file, find-bugs:reproduce=finding) are NOT
# cross-tool propagatable -- _converged_commit returns None for them -- so a
# cross-tool dep on one is rejected (the case the hunt label used to illustrate).
MODE_ARTIFACTS = {
    "develop": "git-sha",
    "research": "file",
    "find-bugs:reproduce": "finding",
    "bring-up": "git-sha",
}
# The only artifact kind a cross-tool dependency can actually consume.
PROPAGATABLE_ARTIFACT = "git-sha"


def artifact_of(mode: str) -> str | None:
    return MODE_ARTIFACTS.get(mode)


@dataclass
class ModeRunSpec:
    """One node in the fleet DAG: a single mode-run on a single tool.

    The client owns ``tool``/``mode``/``op_config``/``run_id``/``depends_on``;
    ``requires_artifact`` is an OPTIONAL extra assertion a client may make about a
    cross-tool dep -- ``None`` does NOT exempt the cross-tool artifact check (that
    check is DERIVED from the tool identity, see ``validate_program`` check 6).
    """

    tool: Path
    mode: str
    op_config: OpConfig
    run_id: str
    depends_on: list[str] = field(default_factory=list)
    affinity: str | None = None
    writable: bool = True
    requires_artifact: str | None = None

    @property
    def branch(self) -> str | None:
        """The Stage-5 branch name this run would own (``peers/run/<run_id>``),
        or ``None`` for a read-only run (which owns no writable HEAD). The ``/``
        base_root is irrelevant -- ``workspace_names`` is pure and only the branch
        component is read. Raises ``ValueError`` (via the namer) on an unsafe
        ``run_id``; ``validate_program`` reports that defect before relying on it.
        """
        if not self.writable:
            return None
        return workspace_names(Path("/"), self.run_id)[1]


@dataclass
class Program:
    runs: list[ModeRunSpec] = field(default_factory=list)


def _is_git_repo(path: Path) -> bool:
    """Fail-closed: a non-repo / unresolvable path returns False."""
    try:
        return subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--git-dir"],
            capture_output=True, text=True, timeout=120, check=False,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def validate_program(program: Program) -> tuple[bool, list[str]]:
    """Validate ``program`` WHOLE; return ``(ok, errors)`` with ALL defects.

    Never short-circuits -- a program with a cycle AND a missing root AND a
    duplicate id reports every one. A malformed program NEVER yields a partial
    schedule: the caller gets ``(False, [...])`` and runs nothing.
    """
    errors: list[str] = []
    specs = list(program.runs)

    # 0. mode is a KNOWN label AND consistent with op_config.mode. ModeRunSpec.mode
    #    is public fleet input and the cross-tool producer check (6) reads
    #    producer.mode via the artifact map, so an unknown label or one that
    #    disagrees with the op_config the spine already validated must be rejected
    #    fail-closed -- never silently scheduled. Defense in depth: check the
    #    label against ALLOWED_MODES AND that spec.mode == op_config.mode (an
    #    OpConfig built directly, bypassing from_dict's mode allow-list, is still
    #    caught by the first arm).
    for spec in specs:
        if spec.mode not in ALLOWED_MODES:
            errors.append(
                f"unknown mode {spec.mode!r} (in {spec.run_id}); "
                f"allowed: {sorted(ALLOWED_MODES)}"
            )
        # getattr (not attribute access) so a malformed spec with op_config=None
        # fails CLOSED (an error) rather than raising AttributeError out of the
        # validator -- the F1 boundary must never crash on hostile public input.
        oc_mode = getattr(spec.op_config, "mode", None)
        if oc_mode != spec.mode:
            errors.append(
                f"mode {spec.mode!r} disagrees with op_config.mode "
                f"{oc_mode!r} (in {spec.run_id})"
            )

    # 1. run-ids: non-empty + namer-safe + unique. First spec wins the id->spec map.
    id_map: dict[str, ModeRunSpec] = {}
    for spec in specs:
        rid = spec.run_id
        if not rid:
            errors.append(f"run_id is empty or None: {rid!r}")
            continue
        try:
            workspace_names(Path("/"), rid)
        except ValueError:
            errors.append(f"run_id is not a safe single component: {rid!r}")
            continue
        if rid in id_map:
            errors.append(f"duplicate run_id: {rid}")
            continue
        id_map[rid] = spec

    known_ids = set(id_map)

    # 2. depends_on resolvable: every dep id must be a known run-id.
    for spec in specs:
        if not spec.run_id:
            continue
        for dep in spec.depends_on:
            if dep not in known_ids:
                errors.append(f"unknown depends_on: {dep} (in {spec.run_id})")

    # 3. acyclic: iterative Kahn over the dep graph (edges among known nodes only;
    #    a self-dep a->a is a 1-node cycle that can never reach in-degree 0).
    indegree = {rid: 0 for rid in known_ids}
    adj: dict[str, list[str]] = {rid: [] for rid in known_ids}
    for rid, spec in id_map.items():
        for dep in spec.depends_on:
            if dep in known_ids:
                adj[dep].append(rid)
                indegree[rid] += 1
    queue = [rid for rid in known_ids if indegree[rid] == 0]
    drained = 0
    while queue:
        node = queue.pop()
        drained += 1
        for nxt in adj[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if drained != len(known_ids):
        remaining = [rid for rid in known_ids if indegree[rid] > 0]
        errors.append(f"cycle among: {sorted(remaining)}")

    # 4. tool roots exist AND are git repos (is_dir follows symlinks + accepts ANY
    #    dir; every downstream git op assumes a real repo -- fail closed here).
    for spec in specs:
        tool = Path(spec.tool)
        if not tool.is_dir():
            errors.append(f"missing tool root: {spec.tool} (in {spec.run_id})")
        elif not _is_git_repo(tool.resolve()):
            errors.append(f"tool root is not a git repo: {spec.tool} (in {spec.run_id})")

    # 5. same-branch collision (writable runs): group by (resolved tool, branch).
    #    Resolving the tool key folds path-spelling/symlink variants of one physical
    #    repo together. Read-only runs own no writable HEAD => exempt.
    groups: dict[tuple[Path, str], list[str]] = {}
    for spec in specs:
        if not spec.writable or not spec.run_id:
            continue
        try:
            branch = workspace_names(Path("/"), spec.run_id)[1]
        except ValueError:
            continue  # already flagged in check 1
        key = (Path(spec.tool).resolve(), branch)
        groups.setdefault(key, []).append(spec.run_id)
    for (tool, branch), ids in groups.items():
        if len(ids) >= 2:
            errors.append(f"two writable runs on same branch {branch} on {tool}: {ids}")

    # 6. producer-can-emit (cross-tool artifact -- DERIVED from tool identity, NOT
    #    opt-in). A cross-tool edge REQUIRES the propagatable git-sha branch artifact;
    #    an intra-tool edge sequences on CONVERGED state and is exempt.
    for spec in specs:
        if not spec.run_id:
            continue
        for dep in spec.depends_on:
            producer = id_map.get(dep)
            if producer is None:
                continue  # unknown dep already flagged in check 2
            cross_tool = Path(spec.tool).resolve() != Path(producer.tool).resolve()
            if not cross_tool:
                continue
            emitted = artifact_of(producer.mode)
            if emitted != PROPAGATABLE_ARTIFACT or (
                spec.requires_artifact is not None and spec.requires_artifact != emitted
            ):
                errors.append(
                    f"dep {dep} cannot emit {PROPAGATABLE_ARTIFACT} for {spec.run_id} "
                    f"(mode {producer.mode} emits {emitted})"
                )

    return (not errors, errors)
