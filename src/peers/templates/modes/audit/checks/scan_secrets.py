#!/usr/bin/env python3
"""Small stdlib secret-pattern gate for audit scaffolds."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from peers.safe_io import read_bytes_no_symlink


PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key id"),
    # BUG-130 (+review I1): match any `BEGIN [<words> ]PRIVATE KEY[ BLOCK]`
    # banner — plain PKCS#8 (`BEGIN PRIVATE KEY`, default `openssl genpkey`),
    # ENCRYPTED PKCS#8, RSA/EC/DSA/OPENSSH, and PGP key blocks — not just the
    # four prefixed variants the original pattern covered.
    (re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
     "private key"),
    (re.compile(r"(?i)password\s*[:=]\s*['\"][^'\"]{6,}"), "password"),
    (re.compile(r"(?i)api[_-]?key\s*[:=]\s*['\"][a-z0-9_\-]{16,}"), "API key"),
    (re.compile(r"ghp_[A-Za-z0-9]{30,}"), "GitHub PAT"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "OpenAI-style secret"),
]
SKIP = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv", ".peers"}
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_ALLOWLIST_BYTES = 64 * 1024

# per-repo file listing documented false positives. fa15e9f added
# the file but the scanner never read it. Format per non-empty/non-comment
# line: ``path:line:label`` where ``path`` is relative to the scan root,
# ``line`` is the 1-based line number, and ``label`` is the exact pattern
# label the scanner emits (e.g. ``private key``, ``API key``). Lines that
# start with ``#`` and blank lines are ignored.
ALLOWLIST_FILENAME = ".secrets-allowlist"


def tracked_files(root: str) -> list[Path]:
    run = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=root, capture_output=True, text=True, check=False,
    )
    if run.returncode == 0:
        return [Path(root) / path for path in run.stdout.splitlines() if path]
    return [path for path in Path(root).rglob("*") if path.is_file()]


def load_allowlist(root: str) -> set[tuple[str, int, str]]:
    """Return the set of ``(rel_path, line, label)`` triples exempted via
    ``.secrets-allowlist``. Missing file → empty set (no exemptions).
    Unsafe, oversized, and malformed entries are skipped: an unparseable
    line just fails to exempt anything, it cannot widen exposure.
    BUG-143, BUG-149."""
    allow: set[tuple[str, int, str]] = set()
    try:
        raw_bytes = read_bytes_no_symlink(
            Path(root) / ALLOWLIST_FILENAME,
            max_bytes=MAX_ALLOWLIST_BYTES + 1,
        )
    except OSError:
        return allow
    if len(raw_bytes) > MAX_ALLOWLIST_BYTES:
        return allow
    raw = raw_bytes.decode("utf-8", errors="replace")
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split(":", 2)
        if len(parts) != 3:
            continue
        rel_path, lineno_str, label = (p.strip() for p in parts)
        try:
            lineno = int(lineno_str)
        except ValueError:
            continue
        allow.add((rel_path, lineno, label))
    return allow


def main(root: str = ".") -> int:
    findings: list[str] = []
    files = tracked_files(root)
    allowlist = load_allowlist(root)
    root_path = Path(root)
    for file in files:
        if any(part in SKIP for part in file.parts):
            continue
        if file.is_symlink():
            findings.append(f"{file}: symlinked file not scanned")
            continue
        try:
            raw = read_bytes_no_symlink(file, max_bytes=MAX_FILE_BYTES + 1)
        except (OSError, IsADirectoryError):
            continue
        if len(raw) > MAX_FILE_BYTES:
            findings.append(f"{file}: file too large to scan (>{MAX_FILE_BYTES} bytes)")
            continue
        text = raw.decode("utf-8", errors="ignore")
        try:
            rel = file.relative_to(root_path).as_posix()
        except ValueError:
            rel = file.as_posix()
        for rx, label in PATTERNS:
            for match in rx.finditer(text):
                line = text[:match.start()].count("\n") + 1
                if (rel, line, label) in allowlist:
                    continue
                findings.append(f"{file}:{line}: {label}")
    if findings:
        print("secrets FAIL:\n  " + "\n  ".join(findings))
        return 1
    print(f"secrets: clean ({len(files)} files scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
