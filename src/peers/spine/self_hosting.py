"""STEP-1 (S1): the pure, fail-safe self-hosting detector.

This is the load-bearing trust boundary of the auto-merge seam (§6.3): the one
predicate that decides whether the agent is allowed to auto-merge a change. It
is PURE (no LLM, no network beyond a local `git rev-parse`), DETERMINISTIC, and
**fail-safe** -- every uncertain input resolves to ``True`` (self-hosting =>
branch-pr => human review). A change that touches the spine, the gate
registry/definitions, op_config, authorship/attestation, the gate-runner, or
``.peers`` governance must NEVER be classified trusted: auto-merging the gates
that govern the agent is the catastrophic failure this seam exists to prevent.

Defense in depth -- three independent layers each force ``True`` on their own:

* the **target-identity** layer (the target repo *is* peers, by git common-dir
  or the sentinel marker -- a ``/tmp`` worktree or a copy included),
* the **empty/error** layer (a ``None`` / empty changed-paths is "we could not
  determine what changed", never "a no-op is trusted"),
* the **path-glob governance** layer (any changed path under the enforcement
  surface, with quotePath / symlink / traversal guards).

The whole thing is wrapped in a top-level ``try/except Exception`` so any
unexpected failure (a raising ``git``, a non-iterable ``changed_paths``, a
``tomllib`` parse error) also resolves to ``(True, "detection-error")``.
"""
from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path, PurePosixPath

__all__ = [
    "GOVERNANCE_BASENAMES",
    "GOVERNANCE_DIR_PREFIXES",
    "GOVERNANCE_FILES",
    "GOVERNANCE_GLOBS",
    "GOVERNANCE_SEGMENTS",
    "is_self_hosting",
]


# --------------------------------------------------------------------------
# The governance surface -- a default-DENY model over directory PREFIXES + an
# exact-FILE set + distinctive BASENAMES + path SEGMENTS. NOT a brittle
# ``*``/``**`` glob split: a future plain-``*`` glob would silently under-cover
# a nested subtree, so ``_path_matches`` does a normalized, SEGMENT-aware test
# and every nested path under a governance dir matches by construction.
# --------------------------------------------------------------------------

# Governance DIRECTORY prefixes -- ANY path at or under one is governance.
GOVERNANCE_DIR_PREFIXES = (
    "src/peers/spine/",          # the WHOLE spine (gates, landing, op_config,
                                 #   authorship, ledger, propagate, auto_merge,
                                 #   self_hosting, ...) -- nested subtrees too
    ".peers/",                   # .peers governance (checks, goals.yaml, baselines)
)

# Governance FILE leaves -- a path whose full rel-path is an exact match, OR
# whose basename matches one of these, is governance. For governance we
# over-flag deliberately (the cost of a false positive is a human review).
GOVERNANCE_FILES = (
    "src/peers/attest.py",              # the substrate attestation primitive
    "src/peers/op_config.py",           # the (top-level) intake config
    "src/peers/async_gate_runner.py",   # the gate runner
    "src/peers/anti_cheat_guard.py",    # anti-cheat enforcement
    "src/peers/safe_io.py",             # the fail-closed write primitive (BUG-118/119)
    "src/peers/structured_halt.py",     # the echo-immune halt
    "src/peers/goals.py",               # gate/goal definitions
    "src/peers/goal_engine.py",         # goal engine
    "src/peers/goal_reload.py",         # goal reload
    "src/peers/driver_gate_pipeline.py",  # the gate pipeline
    "src/peers/driver_tick_hooks.py",   # attest tick hooks
    "src/peers/driver_soft_reviews.py",  # soft-review hooks
    "pyproject.toml",                   # entrypoints + pytest/test config
    "setup.cfg",                        # packaging/config
)

# Governance basenames -- ANY path whose basename is one of these is governance
# (gate-input manifests + diff-behaviour config a run could weaponise + the
# bundled check digests; these live under many subtrees: research/checks,
# templates/modes/**/checks, etc.).
GOVERNANCE_BASENAMES = (
    "goals.yaml",        # a gate-input manifest (mode goals)
    "checks.sha256",     # a bundled gate-check digest manifest
    ".gitattributes",    # diff= / textconv / binary attrs (could hide a path)
)

# Governance DIRECTORY SEGMENTS -- ANY path with one of these as a path segment
# is governance (a ``checks/`` body changed to always-pass weakens a gate; it
# appears under src/peers/research/checks, templates/modes/**/checks, .peers/checks).
GOVERNANCE_SEGMENTS = ("checks",)

# The auditable registry the coverage test reads (the human-readable union).
# Segments are rendered ``/<seg>/`` so the registry reads as "any path with a
# ``checks/`` segment"; the live matcher below uses GOVERNANCE_SEGMENTS directly.
GOVERNANCE_GLOBS = (
    GOVERNANCE_DIR_PREFIXES
    + GOVERNANCE_FILES
    + GOVERNANCE_BASENAMES
    + tuple(f"/{seg}/" for seg in GOVERNANCE_SEGMENTS)
)

# A backslash immediately followed by a digit -- the ``core.quotePath`` C-octal
# escape (e.g. ``\303``). A legitimately-tracked source path never carries one.
_OCTAL_ESCAPE_RE = re.compile(r"\\\d")


def _normalize(p: object) -> str | None:
    """Return a clean POSIX relative path, or ``None`` if uncertain.

    The caller fails safe (self-hosting) on ``None``. Rejected as uncertain:
    a non-``str``/empty value; a ``core.quotePath`` C-quoted path (leading
    ``"`` or a backslash-octal marker); any embedded NUL/control byte (a real
    ``-z`` diff NUL-delimits *between* paths, so a NUL *inside* a path is
    malformed); an absolute path; any path with a ``..`` traversal segment.
    """
    if not isinstance(p, str) or not p:
        return None
    # B2 quotePath belt-and-suspenders: a C-quoted path is uncertain.
    if p[0] == '"':
        return None
    if "\\" in p and _OCTAL_ESCAPE_RE.search(p):
        return None
    # Symlink/control fail-safe: any control byte (NUL incl.) => uncertain.
    for ch in p:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            return None
    pp = PurePosixPath(p.replace("\\", "/"))
    if pp.is_absolute():
        return None
    if any(part in ("..", ".") for part in pp.parts):
        return None
    return pp.as_posix()


def _path_matches(rel: str) -> bool:
    """True iff ``rel`` touches the governance surface (default-deny).

    Segment-aware throughout (split on ``/``), so no nested path under a
    governance directory can slip through and no future glob can under-cover.
    The CALLER returns trusted only when NO path matched AND the target is not
    peers.
    """
    # (a) governance directory prefixes -- the dir itself or anything under it.
    for pre in GOVERNANCE_DIR_PREFIXES:
        if rel == pre.rstrip("/") or rel.startswith(pre):
            return True
    name = PurePosixPath(rel).name
    # (b) governance files -- exact rel-path OR basename (deliberate over-flag).
    for f in GOVERNANCE_FILES:
        if rel == f or name == PurePosixPath(f).name:
            return True
    # (c) governance basenames anywhere in the tree.
    if name in GOVERNANCE_BASENAMES:
        return True
    # (d) governance directory segments anywhere in the path.
    return any(seg in GOVERNANCE_SEGMENTS for seg in rel.split("/"))


# --------------------------------------------------------------------------
# Repo-identity layer (B3): identity by REPO IDENTITY, not literal path
# equality, so a /tmp worktree of peers (path != source root, common-dir IS
# peers') or a copy of peers (different common-dir, sentinel present) is caught.
# --------------------------------------------------------------------------

def _common_dir(p: object) -> str | None:
    """Resolve ``<p>``'s absolute git common dir, or ``None`` on any failure.

    A WORKTREE shares the source repo's common dir, so a ``/tmp`` worktree of
    peers resolves to the SOURCE ``.git``. Returns ``None`` (not raise) on a
    non-repo / missing-git / non-zero exit -- the caller treats ``None`` as
    "could not identify".
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(p), "rev-parse",
             "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return out or None


def _peers_common_dir() -> str | None:
    """peers' OWN git common dir.

    This module lives at ``src/peers/spine/self_hosting.py`` so
    ``parents[3]`` is the repo root.
    """
    return _common_dir(Path(__file__).resolve().parents[3])


def _has_peers_sentinel(p: object) -> bool:
    """True iff ``<p>`` looks like a peers checkout by its sentinel marker.

    The marker is ``<p>/src/peers/spine`` present AND ``<p>/pyproject.toml``
    declaring ``name == "peers"`` (``project.name`` / ``tool.poetry.name`` /
    top-level ``name``). Catches a COPY of peers whose common-dir differs (a
    fresh ``git init`` over a peers checkout). Returns ``False`` on any
    read/parse error -- the identity decision then rests on the common-dir layer.
    """
    try:
        root = Path(str(p))
        if not (root / "src" / "peers" / "spine").is_dir():
            return False
        with open(root / "pyproject.toml", "rb") as fh:
            meta = tomllib.load(fh)
        project = meta.get("project")
        if isinstance(project, dict) and project.get("name") == "peers":
            return True
        tool = meta.get("tool")
        if isinstance(tool, dict):
            poetry = tool.get("poetry")
            if isinstance(poetry, dict) and poetry.get("name") == "peers":
                return True
        return meta.get("name") == "peers"
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return False


def _repo_identity(p: object) -> tuple[str | None, bool]:
    """The two identity signals for an arbitrary repo: ``(common_dir, sentinel)``."""
    return (_common_dir(p), _has_peers_sentinel(p))


def _peers_identity() -> tuple[str | None, bool]:
    """The two identity signals for peers itself."""
    return (_peers_common_dir(), True)


def is_self_hosting(
    repo: object,
    *,
    changed_paths: list[str] | None,
    target_repo: object | None = None,
) -> tuple[bool, str]:
    """Decide whether a change is self-hosting (=> branch-pr => human review).

    Returns ``(flag, reason)``. ``flag`` is ``True`` for self-hosting and on
    ANY uncertainty; ``(False, "")`` only on the fully-trusted path (no
    governance touch AND target is not peers). ``reason`` names the cause.

    ``repo`` is currently unused by the path-glob layer but kept in the
    signature for symmetry with the gates / Stage-7 callers (flag-at-review).
    """
    try:
        # Layer 1 (defense in depth): the target IS peers, by REPO IDENTITY
        # (git common-dir OR the sentinel marker), so a /tmp worktree or a copy
        # of peers is flagged even though its path != peers' source root. This
        # is the dogfood isolation path: run.tool is a leased worktree.
        if target_repo is not None:
            tgt_common, tgt_sentinel = _repo_identity(target_repo)
            peers_common, _ = _peers_identity()
            # BUG-601 (defense in depth): if peers' OWN identity is undeterminable
            # (peers_common is None -- git broken/missing at peers' root) the
            # `tgt_common == peers_common` comparison below silently no-ops, so we
            # cannot certify the target is NOT a peers worktree by common-dir. The
            # sentinel marker is the only remaining POSITIVE signal; if it is also
            # absent we fail safe rather than fall through to the path-glob layer
            # (S1: any uncertainty about identity => self-hosting). A present
            # sentinel still resolves to target-is-peers below (positive ID wins).
            if peers_common is None and not tgt_sentinel:
                return (True, "undeterminable-peers-identity")
            if tgt_common is None and not tgt_sentinel:
                return (True, "undeterminable-target")  # cannot identify -> fail safe
            if (tgt_common is not None and peers_common is not None
                    and tgt_common == peers_common):
                return (True, "target-is-peers")        # same repo (worktree included)
            if tgt_sentinel:
                return (True, "target-is-peers")        # a copy of peers (sentinel)
        # Layer 2: an undeterminable / empty diff is uncertain -> fail safe.
        if changed_paths is None:
            return (True, "undeterminable-diff")
        if not changed_paths:
            return (True, "empty-diff")
        # Layer 3: ANY governance-touching path -> self-hosting.
        for raw in changed_paths:
            rel = _normalize(raw)
            if rel is None:
                return (True, f"unnormalizable-path:{raw!r}")
            if _path_matches(rel):
                return (True, f"governance-touch:{rel}")
        return (False, "")            # trusted: no governance touch, target != peers
    except Exception:                 # S1/S5: ANY error in detection -> fail safe
        return (True, "detection-error")
