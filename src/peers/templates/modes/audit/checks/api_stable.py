#!/usr/bin/env python3
"""Snapshot and compare public Python API symbols."""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path


BASELINE = Path(".peers/api-baseline.txt")


def public_symbols(srcdir: str = "src") -> list[str]:
    out: list[str] = []
    for file in sorted(Path(srcdir).rglob("*.py")):
        if file.name.startswith("_"):
            continue
        try:
            tree = ast.parse(file.read_text(errors="ignore"))
        except SyntaxError:
            continue
        mod = ".".join(file.with_suffix("").parts[1:])
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
    if not baseline.exists():
        print(f"api_stable: missing {baseline}; run with --dump first")
        return 1
    declared = set(baseline.read_text().splitlines())
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
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "--dump" else "."))
