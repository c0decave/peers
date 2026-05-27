#!/usr/bin/env python3
"""Opt-in soft gate: when PLAN.md requests an extra honesty-audit peer,
verify the corresponding HONESTY_AUDIT.md section exists.

Schicht-6 opt-in gate for implement-mode (Task 8.4). The base
``honesty_audit`` gate requires at least two ``## <peer>`` H2 sections
(typically ``## claude`` + ``## codex``). When a project wants to add a
third independent perspective (e.g. ``gemini``), PLAN.md can declare::

    ## Meta
    ...
    honesty_audit_peer: gemini

When that field is set, this gate verifies that ``HONESTY_AUDIT.md``
carries a ``## <peer>`` H2 section matching the declared peer name
(case-insensitive). The substance of that section is still validated
by the base ``honesty_audit`` gate -- this gate only catches the
"declared an extra peer but never wrote their section" failure mode.

Opt-in mechanism
----------------
* PLAN.md meta key ``honesty_audit_peer:`` set to a non-empty string.
* Otherwise the gate exits 0 with ``skipped (opt-in not enabled)``.

A future v2 may shell out to a real gemini CLI to fetch a third
perspective programmatically; for now the gate is structural only.

Soft semantics
--------------
Always exits 0. Findings (missing section, malformed declaration) are
printed to stdout; the reviewer peer reads them and asks the named
extra peer to add their answers.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


_AUDIT_NAME = "HONESTY_AUDIT.md"
_PLAN_NAME = "PLAN.md"

# Match `honesty_audit_peer: <name>` in PLAN.md, case-insensitive at
# line start (the Meta section is line-oriented `key: value`).
_META_KEY_RE = re.compile(
    r"^\s*honesty_audit_peer\s*:\s*(?P<val>.+?)\s*$",
    re.IGNORECASE,
)

# H2 line opening a per-peer block in HONESTY_AUDIT.md.
_PEER_HEADER_RE = re.compile(r"^##\s+(?P<name>\S.*?)\s*$")


def _read_meta_peer(plan_path: Path) -> str | None:
    """Return the declared extra-peer name from PLAN.md, or None."""
    if not plan_path.is_file():
        return None
    try:
        text = plan_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    in_meta = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("##"):
            in_meta = stripped.lower() == "## meta"
            continue
        if not in_meta:
            continue
        m = _META_KEY_RE.match(line)
        if m:
            val = m.group("val").strip()
            # Trim trailing inline comment ("gemini  # extra peer").
            if "#" in val:
                val = val.split("#", 1)[0].strip()
            return val or None
    return None


def _audit_has_peer(audit_path: Path, peer: str) -> bool:
    if not audit_path.is_file():
        return False
    try:
        text = audit_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    target = peer.strip().lower()
    for line in text.splitlines():
        m = _PEER_HEADER_RE.match(line)
        if m and m.group("name").strip().lower() == target:
            return True
    return False


def main(project_dir: str = ".") -> int:
    """Soft scan: when PLAN.md declares an extra peer, verify their section."""
    project_root = Path(project_dir).resolve()
    plan_path = project_root / _PLAN_NAME
    audit_path = project_root / _AUDIT_NAME

    peer = _read_meta_peer(plan_path)
    if not peer:
        print(
            "honesty-audit-peer-gemini: skipped "
            "(opt-in not enabled -- set `honesty_audit_peer: <name>` "
            "in PLAN.md Meta to activate)"
        )
        return 0

    findings: list[str] = []

    if not audit_path.is_file():
        findings.append(
            f"PLAN.md declares `honesty_audit_peer: {peer}` but "
            f"{_AUDIT_NAME} is missing -- the named peer has no section"
        )
    elif not _audit_has_peer(audit_path, peer):
        findings.append(
            f"PLAN.md declares `honesty_audit_peer: {peer}` but no "
            f"`## {peer}` H2 section is present in {_AUDIT_NAME}"
        )

    if findings:
        print(
            f"honesty-audit-peer-gemini WARN: {len(findings)} issue(s):"
        )
        for f in findings:
            print(f"  {f}")
        print(
            f"  hint: add a `## {peer}` H2 section to {_AUDIT_NAME} "
            "with the same three subsections (Weakest part / Likely "
            "uncaught bug / Skipped or shortcut)"
        )
        return 0  # soft

    print(
        f"honesty-audit-peer-gemini: clean (extra peer `{peer}` "
        "section present)"
    )
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
