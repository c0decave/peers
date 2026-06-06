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


def tracked_files(root: str) -> list[Path]:
    run = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=root, capture_output=True, text=True, check=False,
    )
    if run.returncode == 0:
        return [Path(root) / path for path in run.stdout.splitlines() if path]
    return [path for path in Path(root).rglob("*") if path.is_file()]


def main(root: str = ".") -> int:
    findings: list[str] = []
    files = tracked_files(root)
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
        for rx, label in PATTERNS:
            for match in rx.finditer(text):
                line = text[:match.start()].count("\n") + 1
                findings.append(f"{file}:{line}: {label}")
    if findings:
        print("secrets FAIL:\n  " + "\n  ".join(findings))
        return 1
    print(f"secrets: clean ({len(files)} files scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
