#!/usr/bin/env python3
"""Exit 1 if non-trivial Python modules lack happy, edge, and sad tests.

Test files are discovered two ways: by name (``test_<src_stem>*.py``) or, as a
fallback, by content — a test file that imports the source module is treated
as covering it. Each test file is classified into happy/edge/sad buckets by
matching test function names against a vocabulary of common keywords (e.g.
``valid``, ``empty``, ``refuses``) and via an explicit ``# kind: <class>``
marker comment for cases the vocabulary misses. Source files under
``src/<pkg>/templates/`` are skipped because those scripts are copied
verbatim into target projects and exercised through integration tests with
their own naming conventions.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from peers.safe_io import read_bytes_no_symlink


# Vocabulary is kept tight enough to still flag *missing* coverage while
# accepting real test names in this codebase ("refuses_hard_link" reads as
# a sad-path test; "appends_regular_leaf" reads as a happy-path test).
# For tests whose names don't match any keyword, authors can opt into a
# class explicitly with a ``# kind: happy`` (or edge/sad) marker comment.
KIND_RE = {
    "happy": re.compile(
        r"(?i)("
        r"happy|ok|success|nominal|baseline|"
        r"valid|default|minimal|canonical|"
        r"single|passthrough|alias|green|"
        r"writes?|appends?|creates?|reads?|"
        r"accepts?|parses?|evaluates?|supports?|"
        r"contains?|includes?|allows?|rendered|"
        r"round[_-]?trip|"
        r"_pass\b|\bpass(es|ed)?\b"
        r")"
    ),
    "edge": re.compile(
        r"(?i)("
        r"edge|boundary|empty|max|min|long|unicode|"
        r"multiple|duplicate|oversized|symlink|"
        r"concurrent|race|toctou|truncat|bounded|"
        r"regular[_-]leaf|still_(applies|fires|works)"
        r")"
    ),
    "sad": re.compile(
        r"(?i)("
        r"sad|fail|error|invalid|exception|timeout|broken|"
        r"refuses?|rejects?|missing|corrupt|garbage|denied|"
        r"unknown|ignores?|wrong|skip|malformed|"
        r"bad[_-]"
        r")"
    ),
}
KIND_MARKER_RE = re.compile(r"#\s*kind:\s*([a-z, ]+)", re.IGNORECASE)
MAX_TEST_FILE_BYTES = 2 * 1024 * 1024

# Source roots we don't apply the check to. Templates under
# ``src/<pkg>/templates/`` are scripts copied verbatim into other
# projects; they are exercised through integration tests with their
# own naming.
SKIP_PATH_FRAGMENTS = ("/templates/",)


def kinds_in(testfile: Path) -> set[str]:
    try:
        raw = read_bytes_no_symlink(testfile, max_bytes=MAX_TEST_FILE_BYTES + 1)
    except OSError:
        return set()
    if len(raw) > MAX_TEST_FILE_BYTES:
        return set()
    text = raw.decode("utf-8", errors="ignore")
    kinds: set[str] = set()
    for name in re.findall(r"def\s+(test_\w+)", text):
        for kind, rx in KIND_RE.items():
            if rx.search(name):
                kinds.add(kind)
    for marker in KIND_MARKER_RE.findall(text):
        for word in re.split(r"[,\s]+", marker.lower()):
            if word in KIND_RE:
                kinds.add(word)
    return kinds


def _module_path_for(src: Path, srcdir: Path) -> str:
    try:
        rel = src.with_suffix("").relative_to(srcdir)
    except ValueError:
        return ""
    return ".".join(rel.parts)


def _content_references(testfile: Path, module_path: str) -> bool:
    try:
        raw = read_bytes_no_symlink(testfile, max_bytes=MAX_TEST_FILE_BYTES + 1)
    except OSError:
        return False
    if len(raw) > MAX_TEST_FILE_BYTES:
        return False
    text = raw.decode("utf-8", errors="ignore")
    if f"from {module_path}" in text or f"import {module_path}" in text:
        return True
    parent = module_path.rsplit(".", 1)
    if len(parent) == 2:
        pkg, leaf = parent
        if re.search(rf"from\s+{re.escape(pkg)}\s+import\s+[\w,\s()]*\b{re.escape(leaf)}\b", text):
            return True
    return False


def _candidates_for(src: Path, srcdir: Path, testdir: Path) -> list[Path]:
    by_name = list(testdir.rglob(f"test_{src.stem}*.py"))
    if by_name:
        return by_name
    module_path = _module_path_for(src, srcdir)
    if not module_path:
        return []
    found: list[Path] = []
    for tf in testdir.rglob("test_*.py"):
        if _content_references(tf, module_path):
            found.append(tf)
    return found


def _should_skip(src: Path) -> bool:
    posix = src.as_posix()
    return any(frag in posix for frag in SKIP_PATH_FRAGMENTS)


def main(srcdir: str = "src", testdir: str = "tests") -> int:
    src_root = Path(srcdir)
    test_root = Path(testdir)
    missing: list[str] = []
    for src in src_root.rglob("*.py"):
        if src.name.startswith("_") or src.name == "__init__.py":
            continue
        if _should_skip(src):
            continue
        try:
            if sum(1 for _ in src.open(encoding="utf-8", errors="ignore")) < 50:
                continue
        except OSError:
            continue
        candidates = _candidates_for(src, src_root, test_root)
        if not candidates:
            missing.append(f"{src}: no test_{src.stem}* in {testdir}/ and no import-reference found")
            continue
        gap = {"happy", "edge", "sad"} - set().union(
            *(kinds_in(candidate) for candidate in candidates)
        )
        if gap:
            missing.append(f"{src}: missing {sorted(gap)} test class(es)")
    if missing:
        print("coverage_3class FAIL:\n  " + "\n  ".join(missing))
        return 1
    print(f"coverage_3class: clean ({srcdir} / {testdir})")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3] if len(sys.argv) >= 3 else ()))
