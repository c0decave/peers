#!/usr/bin/env python3
"""Exit 1 if ``src/`` contains shortcut markers without reviewer sign-off.

Schicht-5 cleanliness gate for implement-mode (Task 5.1). The peers
loop tends to leave behind half-finished placeholders -- ``# TODO`` /
``# FIXME`` / ``raise NotImplementedError`` -- as long as the
acceptance script happens to pass. This gate forbids that vocabulary
in production code (everything under ``src/``) unless every
occurrence carries an explicit, reviewer-signed escape.

Forbidden vocabulary (substring match, case-sensitive)
------------------------------------------------------
``TODO``, ``FIXME``, ``XXX``, ``HACK``, ``PLACEHOLDER``, ``STUB``
inside any line of a ``.py`` file under ``src/``. ``NotImplementedError``
and ``raise NotImplemented`` are handled specially via the AST so
abstract/protocol classes can still raise them legitimately (see
below).

Skip-listed paths
-----------------
* ``tests/`` -- test scaffolding regularly contains FIXMEs about
  upstream bugs, and the substrate's own ``tests-no-unjustified-skip-
  or-fail`` gate covers test-side quality.
* ``.peers/`` -- substrate state, not production code.
* Everything outside ``src/`` -- the gate is scoped to the project's
  production tree.

Escape via reviewer sign-off
----------------------------
Two ingredients are required:

1. The offending line ends with ``# JUSTIFIED: <reason>`` (the comment
   may also contain the original marker, e.g. ``# TODO  # JUSTIFIED:
   waits on issue 42``).
2. An independent ``peers-review: <relpath>`` commit by the OTHER peer
   (FU-2). The reviewer signs off by making a commit whose message carries
   ``peers-review: <relpath>``; the substrate attributes it via the
   unforgeable ``refs/notes/peers-attest`` note, and the gate SEARCHES
   reachable history for it (:func:`peers.attest.find_review_commit`),
   excluding the file's own author so a peer cannot self-bless.

Missing either half is a violation. The annotation alone is rejected
on purpose -- otherwise the implementer could simply tack
``# JUSTIFIED: lazy`` onto every TODO they introduce. This replaces the
previous escape, which trusted an agent-authored (forgeable)
``.peers/justifications.log`` reviewer field; that log is no longer
consulted for this gate.

NotImplementedError AST exception
---------------------------------
Concrete subclasses raising ``NotImplementedError`` are the typical
"I'll get to it later" failure mode this gate exists to catch. But
abstract bases legitimately raise it as a forced override signal.
We use :mod:`ast` to walk class definitions: if a ``raise
NotImplementedError`` (or ``raise NotImplemented``) sits inside a
class whose bases textually include ``ABC`` / ``Protocol`` / anything
matching ``abc.ABC`` / ``typing.Protocol``, the violation is dropped.
The base-class match is textual (we look at the literal source of
each base), so subclassing via aliases is supported as long as the
alias name is one we recognise.

Exit codes
----------
* ``0`` -- ``src/`` is clean, or every match is justified+signed,
  or no ``src/`` directory exists (nothing to scan).
* ``1`` -- at least one violation; stdout lists them as
  ``<relpath>:<lineno>: <marker>: <line snippet>``.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

from peers.attest import (
    attested_authors_of_file,
    attested_line_author_peers,
    find_review_commit,
)


_FORBIDDEN_MARKERS = ("TODO", "FIXME", "XXX", "HACK", "PLACEHOLDER", "STUB")
_NOT_IMPL_MARKERS = ("NotImplementedError", "raise NotImplemented")
_JUSTIFIED_TAG = "# JUSTIFIED:"

# Base-class name patterns that exempt a class from the
# NotImplementedError rule. Match is on the literal AST source of
# each base, so e.g. ``class Foo(ABC):`` and
# ``class Foo(abc.ABC):`` both work.
_ABSTRACT_BASE_PATTERNS = (
    "ABC",
    "abc.ABC",
    "ABCMeta",
    "abc.ABCMeta",
    "Protocol",
    "typing.Protocol",
)


def _is_abstract_class(node: ast.ClassDef) -> bool:
    """True if the class derives from a known abstract/protocol base.

    We unparse each base expression and check whether its textual
    form ends in any of the recognised patterns. ``Protocol`` is
    matched both bare and as ``typing.Protocol``; same for ``ABC``.
    Keyword-only bases (``metaclass=ABCMeta``) are also recognised
    via the ``keywords`` list.
    """
    for base in node.bases:
        text = ast.unparse(base)
        for pat in _ABSTRACT_BASE_PATTERNS:
            if text == pat or text.endswith("." + pat):
                return True
    for kw in node.keywords:
        if kw.arg == "metaclass":
            text = ast.unparse(kw.value)
            for pat in _ABSTRACT_BASE_PATTERNS:
                if text == pat or text.endswith("." + pat):
                    return True
    return False


def _collect_abstract_class_line_ranges(
    tree: ast.Module,
) -> list[tuple[int, int]]:
    """Return ``[(start_line, end_line), ...]`` for every abstract class.

    We use plain line ranges (no nesting awareness) because the
    only thing we need to decide is "is this ``raise
    NotImplementedError`` statement physically inside an abstract
    class body?". For nested classes we err on the side of
    exempting (outer abstract class covers inner concrete one's
    body too) -- this is a known limitation but matches the
    common case where abstract bases contain methods, not classes.
    """
    ranges: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and _is_abstract_class(node):
            start = node.lineno
            end = node.end_lineno or start
            ranges.append((start, end))
    return ranges


def _line_in_any_range(lineno: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= lineno <= end for start, end in ranges)


def _line_independently_reviewed(
    project_root: Path, relpath: str, lineno: int,
) -> bool:
    """True when an independent peer signed off on ``relpath`` via a
    substrate-attested ``peers-review: <relpath>`` commit (FU-2).

    The peer that AUTHORED the justified marker line is excluded so it cannot
    self-bless its own shortcut. We exclude by the MARKER LINE's author
    (``git blame``) rather than the file's last editor — the latter is
    launderable (a co-peer's trivial edit to another line flips the exclusion
    target away from the real author). A co-peer who only edited OTHER lines is
    therefore still a valid reviewer. If the marker line cannot be attributed
    (uncommitted / unattested), we fall back to the whole-file author set
    (stricter, fail-closed).
    """
    exclude = attested_line_author_peers(project_root, relpath, [lineno])
    if not exclude:
        exclude = attested_authors_of_file(project_root, relpath)
    return find_review_commit(
        project_root, relpath, exclude_peer=exclude) is not None


def _scan_python_file(
    path: Path,
    relpath: str,
    project_root: Path,
) -> list[str]:
    """Return a list of violation messages for ``path``.

    Each message is ``<relpath>:<lineno>: <marker>: <snippet>``.
    Annotated + independently-reviewed lines are filtered out; AST-allowed
    NotImplementedError occurrences (abstract / Protocol bases) are
    filtered out.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    abstract_ranges: list[tuple[int, int]] = []
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        # Unparseable file -- still scan textually for forbidden
        # markers but skip the AST-based NotImplementedError exemption.
        tree = None
    if tree is not None:
        abstract_ranges = _collect_abstract_class_line_ranges(tree)

    violations: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        # Find which markers (if any) hit this line. We deliberately
        # detect ALL markers on a line (not just the first) so a
        # single justification covering one marker still flags
        # the other if its rationale doesn't cover it.
        hits: list[str] = []
        for m in _FORBIDDEN_MARKERS:
            if m in line:
                hits.append(m)
        not_impl_hit: str | None = None
        for m in _NOT_IMPL_MARKERS:
            if m in line:
                not_impl_hit = m
                break

        if not hits and not_impl_hit is None:
            continue

        # NotImplementedError inside an abstract / Protocol class is
        # always allowed -- no annotation needed.
        if not_impl_hit and _line_in_any_range(lineno, abstract_ranges):
            not_impl_hit = None
            if not hits:
                continue

        if not hits and not_impl_hit is None:
            continue

        # Annotation + sign-off check: if the line carries the JUSTIFIED tag
        # AND an independent peer signed off on the file via a
        # substrate-attested `peers-review: <relpath>` commit (FU-2), all
        # markers on this line are forgiven.
        if _JUSTIFIED_TAG in line:
            if _line_independently_reviewed(project_root, relpath, lineno):
                continue
            # Annotated but not independently reviewed -- explicit message so
            # the operator knows the next step (the OTHER peer must make a
            # `peers-review: <relpath>` commit).
            snippet = line.strip()[:120]
            violations.append(
                f"{relpath}:{lineno}: JUSTIFIED-but-unreviewed: {snippet}",
            )
            continue

        # Bare match -- report each distinct marker.
        snippet = line.strip()[:120]
        for m in hits:
            violations.append(f"{relpath}:{lineno}: {m}: {snippet}")
        if not_impl_hit is not None:
            violations.append(
                f"{relpath}:{lineno}: NotImplementedError: {snippet}",
            )

    return violations


def _iter_src_files(src_root: Path) -> list[Path]:
    files: list[Path] = []
    for path in src_root.rglob("*.py"):
        if not path.is_file():
            continue
        rel = path.relative_to(src_root).as_posix()
        if rel.startswith("peers/templates/modes/") and "/checks/" in rel:
            # The peers repository vendors this gate and sibling gates as
            # templates. Those policy implementations necessarily spell out
            # the vocabulary they reject, so scanning them would be a
            # self-hit rather than a production shortcut.
            continue
        files.append(path)
    return sorted(files)


def main(project_dir: str = ".") -> int:
    """Forbid shortcuts in ``src/``: TODO / FIXME / XXX / HACK /
    PLACEHOLDER / STUB / NotImplementedError outside Protocol/ABC.

    Escape via ``# JUSTIFIED: <reason>`` annotation on the same line AND an
    independent ``peers-review: <relpath>`` commit by the OTHER peer (FU-2:
    substrate-attested, agent-unforgeable — replaces the forgeable
    justifications.log reviewer field).
    """
    project_root = Path(project_dir).resolve()
    src_root = project_root / "src"

    if not src_root.is_dir():
        print("no-shortcut-markers: clean (no src/ to scan)")
        return 0

    all_violations: list[str] = []
    files = _iter_src_files(src_root)
    for path in files:
        rel = path.relative_to(project_root).as_posix()
        all_violations.extend(_scan_python_file(path, rel, project_root))

    if all_violations:
        print(
            f"no-shortcut-markers FAIL: {len(all_violations)} "
            f"violation(s) in {len(files)} file(s):"
        )
        for v in all_violations:
            print(f"  {v}")
        print(
            "  hint: each violation needs a `# JUSTIFIED: <reason>` "
            "annotation on the same line AND an independent "
            "`peers-review: <relpath>` commit by the OTHER peer"
        )
        return 1

    print(f"no-shortcut-markers: clean ({len(files)} file(s) scanned)")
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
