#!/usr/bin/env python3
"""Opt-in soft gate: heuristic match between PLAN.md step text and commit diff.

Schicht-6 opt-in gate for implement-mode (Task 8.4). Closes a narrow
gap left by ``plan-step-traceable``: that gate proves a checked step's
``(SHA)`` annotation references a real commit whose changed files
intersect the declared ``touches:`` list. But it does not look at the
commit *message* or check whether the step's prose actually describes
what the commit did.

This gate does a soft heuristic comparison: for each ``[x] [STEP-N]``
entry with a trailing ``(SHA)``, it pulls ``git log -1 --format=%s``
(commit subject) and ``git show --name-only`` (changed files), then
checks whether the *meaningful nouns* from the step text appear in
either. A miss is not a failure (PLAN.md text and commit messages
intentionally differ in tone); it is a finding for the reviewer to
glance at.

Heuristic
---------
* Extract content words from step text (lowercase, length >= 4, not
  in a small stopword list).
* Token-match against commit subject (lowercase) + changed file paths
  (lowercase, basename stem only).
* If zero content words overlap, file a finding.

Opt-in mechanism
----------------
This gate is "soft-opt-in": it runs unconditionally but only when a
git repo is present and PLAN.md exists. When no checked-and-SHA-stamped
steps are present, exits 0 with ``clean``.

Soft semantics
--------------
Always exits 0. Findings are advisory; the reviewer reads them and
either accepts the divergence (terminology differs, fine) or asks the
implementer to amend the step text or the commit.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


_STEP_RE = re.compile(
    r"^\s*-\s*\[[xX]\]\s*\[(?P<id>STEP-\d+)\]\s*(?P<text>.+?)\s*$"
)
_SHA_RE = re.compile(r"\(([0-9a-f]{7,40})\)\s*$")

_STOPWORDS: frozenset[str] = frozenset(
    {
        "with",
        "from",
        "that",
        "this",
        "into",
        "when",
        "step",
        "than",
        "have",
        "been",
        "will",
        "make",
        "made",
        "also",
        "some",
        "more",
        "less",
        "very",
        "many",
        "much",
        "such",
        "then",
        "there",
        "their",
        "what",
        "which",
        "where",
        "while",
        "would",
        "could",
        "should",
        "shall",
        "about",
        "after",
        "before",
        "again",
        "still",
        "those",
        "these",
        "your",
        "they",
        "them",
        "just",
        "like",
        "only",
        "even",
        "tests",
        "test",
        "code",
        "module",
        "file",
        "files",
    }
)

# Word: 4+ letter run of alphanumerics / underscore.
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")


def _content_words(text: str) -> set[str]:
    return {
        w.lower()
        for w in _WORD_RE.findall(text)
        if w.lower() not in _STOPWORDS
    }


def _git_subject(project_root: Path, sha: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(project_root), "log", "-1", "--format=%s", sha],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _git_changed_files(project_root: Path, sha: str) -> list[str]:
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(project_root),
            "show",
            "--name-only",
            "--format=",
            sha,
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _haystack_words(subject: str, files: list[str]) -> set[str]:
    haystack: list[str] = [subject]
    for f in files:
        # Strip extension; keep stem + path components.
        haystack.append(f.replace("/", " ").replace("_", " ").replace(".", " "))
    return _content_words(" ".join(haystack))


def _parse_steps(plan_path: Path) -> list[tuple[str, str, str]]:
    """Return [(step_id, step_text_no_sha, sha)] for each [x] step with SHA."""
    out: list[tuple[str, str, str]] = []
    if not plan_path.is_file():
        return out
    try:
        text = plan_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        m = _STEP_RE.match(line)
        if not m:
            continue
        full_text = m.group("text").strip()
        m_sha = _SHA_RE.search(full_text)
        if not m_sha:
            continue
        sha = m_sha.group(1)
        cleaned = full_text[: m_sha.start()].rstrip()
        out.append((m.group("id"), cleaned, sha))
    return out


def main(project_dir: str = ".") -> int:
    """Soft scan: heuristic content-word match between step text and commit."""
    project_root = Path(project_dir).resolve()
    plan_path = project_root / "PLAN.md"

    steps = _parse_steps(plan_path)
    if not steps:
        print(
            "diff-vs-claim: clean (no checked-and-SHA-stamped steps "
            "to compare)"
        )
        return 0

    findings: list[str] = []
    for step_id, step_text, sha in steps:
        step_words = _content_words(step_text)
        if not step_words:
            # No content words to compare against; skip silently.
            continue
        subject = _git_subject(project_root, sha)
        files = _git_changed_files(project_root, sha)
        if not subject and not files:
            findings.append(
                f"{step_id} ({sha}): commit not found in git history "
                "-- plan-step-traceable should also catch this"
            )
            continue
        haystack = _haystack_words(subject, files)
        overlap = step_words & haystack
        if not overlap:
            findings.append(
                f"{step_id} ({sha}): no content-word overlap between "
                f"step text {step_text!r} and commit subject "
                f"{subject!r} / changed files {files!r}"
            )

    if findings:
        print(
            f"diff-vs-claim WARN: {len(findings)} step(s) where claim "
            "and commit diverge:"
        )
        for f in findings:
            print(f"  {f}")
        print(
            "  hint: either amend the commit message / step text to "
            "share vocabulary, or accept that terminology differs "
            "intentionally"
        )
        return 0  # soft

    print(
        f"diff-vs-claim: clean ({len(steps)} checked step(s), all "
        "with commit-text overlap)"
    )
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
