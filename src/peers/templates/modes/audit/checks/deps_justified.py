#!/usr/bin/env python3
"""Require Dependency-Justification trailers for newly added deps."""
from __future__ import annotations

import subprocess
import sys
import re


DEP_FILES = ["pyproject.toml", "requirements.txt", "package.json", "Cargo.toml", "go.mod"]
_HUNK_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_TOML_SECTION_RE = re.compile(r"^\s*\[+\s*([^\]]+?)\s*\]+\s*$")
_TOML_DEP_SECTIONS = {
    "project.optional-dependencies",
    "build-system",
    "dependency-groups",
}


def _baseline(repo: str) -> str:
    if subprocess.run(
        ["git", "-C", repo, "rev-parse", "--verify", "peers-baseline"],
        capture_output=True, check=False,
    ).returncode == 0:
        return "peers-baseline"
    roots = subprocess.run(
        ["git", "-C", repo, "rev-list", "--max-parents=0", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.splitlines()
    return roots[-1] if roots else "HEAD"


def _added_lines(repo: str) -> list[tuple[str, int, str]]:
    out = subprocess.run(
        ["git", "-C", repo, "diff", f"{_baseline(repo)}..HEAD", "--unified=0", "--", *DEP_FILES],
        capture_output=True, text=True, check=False,
    ).stdout
    added: list[tuple[str, int, str]] = []
    current_file = ""
    new_lineno: int | None = None
    for line in out.splitlines():
        if line.startswith("+++ b/"):
            current_file = line.removeprefix("+++ b/")
            continue
        match = _HUNK_RE.match(line)
        if match:
            new_lineno = int(match.group(1))
            continue
        if new_lineno is None:
            continue
        if line.startswith("\\ No newline at end of file"):
            continue
        if line.startswith("+") and not line.startswith("+++"):
            text = line[1:].strip()
            if text:
                added.append((current_file, new_lineno, text))
            new_lineno += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        else:
            new_lineno += 1
    return added


def _pyproject_section(lines: list[str], lineno: int) -> str:
    for idx in range(lineno - 1, -1, -1):
        match = _TOML_SECTION_RE.match(lines[idx])
        if match:
            return match.group(1).strip()
    return ""


def _pyproject_array_key(lines: list[str], lineno: int, section: str) -> str:
    for idx in range(lineno - 1, -1, -1):
        text = lines[idx].strip()
        if not text or text.startswith("#"):
            continue
        if _TOML_SECTION_RE.match(text):
            return ""
        if "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip().strip("\"'")
        if "[" in value and "]" not in value:
            return key
        if idx == lineno - 1 and section in _TOML_DEP_SECTIONS | {"project"}:
            return key
        return ""
    return ""


def _quoted_specs(line: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"""["']([^"']+)["']""", line)]


def _pyproject_dep_specs(repo: str, added: list[tuple[str, int, str]]) -> list[str]:
    path = f"{repo}/pyproject.toml"
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return []
    specs: list[str] = []
    for _file, lineno, text in added:
        section = _pyproject_section(lines, lineno)
        if section.startswith("tool."):
            continue
        key = _pyproject_array_key(lines, lineno, section)
        if section == "project" and key != "dependencies":
            continue
        if section == "build-system" and key != "requires":
            continue
        if section not in _TOML_DEP_SECTIONS | {"project"}:
            continue
        specs.extend(_quoted_specs(text))
    return specs


def changed_dep_lines(repo: str) -> list[str]:
    added = _added_lines(repo)
    out: list[str] = []
    pyproject_added = [row for row in added if row[0] == "pyproject.toml"]
    out.extend(_pyproject_dep_specs(repo, pyproject_added))
    out.extend(
        text for path, _lineno, text in added
        if path != "pyproject.toml"
    )
    return out


def justified(repo: str) -> set[str]:
    log = subprocess.run(
        ["git", "-C", repo, "log", f"{_baseline(repo)}..HEAD", "--format=%B"],
        capture_output=True, text=True, check=False,
    ).stdout
    out: set[str] = set()
    for line in log.splitlines():
        if line.startswith("Dependency-Justification:"):
            out.add(line.split(":", 1)[1].strip().split()[0].lower().rstrip(","))
    return out


def main(repo: str = ".") -> int:
    allowed = justified(repo)
    missing = []
    for line in changed_dep_lines(repo):
        pkg = line.split()[0].split("=")[0].split(">")[0].split("<")[0].strip("\"',").lower()
        if pkg and not line.startswith("#") and pkg not in allowed:
            missing.append(f"{pkg} (no Dependency-Justification: trailer)")
    if missing:
        print("deps_justified FAIL:\n  " + "\n  ".join(missing))
        return 1
    print("deps_justified: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
