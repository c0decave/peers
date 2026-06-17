#!/usr/bin/env python3
"""Snapshot and compare public Python API symbols."""
from __future__ import annotations

import ast
import os
import stat
import subprocess
import sys
from pathlib import Path

from peers.safe_io import (
    atomic_write_text_in_dir_no_symlink,
    read_bytes_no_symlink,
    read_text_no_symlink,
)


BASELINE = Path(".peers/api-baseline.txt")
MAX_SOURCE_FILE_BYTES = 8 * 1024 * 1024


def _refuse_if_linked(path: Path) -> str | None:
    """BUG-174: refuse a symlinked or hard-linked baseline leaf.

    BUG-177: also walk the ancestor chain and refuse a symlinked PARENT
    component (e.g. a pre-planted ``.peers/`` directory symlink) — a leaf-
    only lstat resolves through it to a regular file and is fooled.
    """
    for _parent in path.parents:
        if _parent == _parent.parent:  # stop at '.' (cwd) / '/' (fs root)
            break
        try:
            _pst = os.lstat(_parent)
        except FileNotFoundError:
            continue
        except OSError as e:
            return f"cannot stat {_parent}: {e}"
        if stat.S_ISLNK(_pst.st_mode):
            return f"refusing symlinked parent: {_parent}"
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        return f"cannot stat {path}: {e}"
    if stat.S_ISLNK(st.st_mode):
        return f"refusing symlinked leaf: {path}"
    if stat.S_ISREG(st.st_mode) and st.st_nlink != 1:
        return f"refusing hard-linked leaf: {path}"
    return None


def _parse_source(path: Path) -> ast.Module | None:
    try:
        raw = read_bytes_no_symlink(
            path, max_bytes=MAX_SOURCE_FILE_BYTES + 1,
        )
    except OSError:
        return None
    if len(raw) > MAX_SOURCE_FILE_BYTES:
        return None
    try:
        return ast.parse(raw.decode("utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return None


def public_symbols(srcdir: str = "src") -> list[str]:
    src_root = Path(srcdir)
    out: list[str] = []
    for file in sorted(src_root.rglob("*.py")):
        if file.name.startswith("_"):
            continue
        tree = _parse_source(file)
        if tree is None:
            continue
        try:
            rel = file.with_suffix("").relative_to(src_root)
        except ValueError:
            continue
        mod = ".".join(rel.parts)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not node.name.startswith("_"):
                out.append(f"{mod}.{node.name}")
    return out


def main(repo: str = ".") -> int:
    if "--dump" in sys.argv:
        for symbol in public_symbols():
            print(symbol)
        return 0
    baseline = Path(repo) / BASELINE
    if "--snapshot" in sys.argv:
        # replace the operator-paste workflow ("run --dump and pipe
        # output into the baseline file") with a first-class writer. Uses
        # the same lstat-pre-check + atomic-no-follow write the no_regression
        # gate uses, so a pre-planted symlink at the baseline path is
        # surfaced rather than silently replaced.
        baseline.parent.mkdir(parents=True, exist_ok=True)
        err = _refuse_if_linked(baseline)
        if err:
            print(f"api_stable FAIL: {err}")
            return 1
        symbols = sorted(set(public_symbols(str(Path(repo) / "src"))))
        try:
            atomic_write_text_in_dir_no_symlink(
                baseline, "\n".join(symbols) + ("\n" if symbols else ""),
            )
        except OSError as e:
            print(f"api_stable: refusing snapshot of {baseline}: {e}")
            return 1
        print(f"api_stable: snapshot saved to {baseline} ({len(symbols)} symbols)")
        return 0
    err = _refuse_if_linked(baseline)
    if err:
        print(f"api_stable FAIL: {err}")
        return 1
    try:
        baseline_text = read_text_no_symlink(baseline)
    except FileNotFoundError:
        print(f"api_stable: missing {baseline}; run with --snapshot first")
        return 1
    except OSError as e:
        print(f"api_stable: refusing to read {baseline}: {e}")
        return 1
    declared = set(baseline_text.splitlines())
    actual = set(public_symbols(str(Path(repo) / "src")))
    changed = (actual - declared) | (declared - actual)
    log = subprocess.run(
        ["git", "-C", repo, "log", "peers-baseline..HEAD", "--format=%B"],
        capture_output=True, text=True, check=False,
    ).stdout
    allowed = {
        line.split(":", 1)[1].strip().split(":")[0].strip()
        for line in log.splitlines()
        if line.startswith("Breaking-API:")
    }
    unannounced = changed - allowed
    if unannounced:
        print("api_stable FAIL: unannounced API changes:")
        for symbol in sorted(unannounced):
            print(f"  {symbol}")
        return 1
    print("api_stable: clean")
    return 0


if __name__ == "__main__":
    # Path arg (if present) is the first positional that is not a flag.
    repo_arg = "."
    for a in sys.argv[1:]:
        if not a.startswith("--"):
            repo_arg = a
            break
    sys.exit(main(repo_arg))
