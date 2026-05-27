"""Install reviewer-only-checkoff hook into a project's .git/hooks/.

Usage:
    python -m peers.templates.modes.implement.hooks.install <project_root>
    # or
    python src/peers/templates/modes/implement/hooks/install.py <project_root>

If a pre-commit hook already exists, it is backed up to pre-commit.bak.
"""
from __future__ import annotations

import shutil
import stat
import sys
from pathlib import Path

HOOK_NAME = "pre-commit-reviewer-checkoff"


def install(project_root: Path) -> Path:
    """Install the hook. Returns destination path."""
    src = Path(__file__).parent / HOOK_NAME
    if not src.is_file():
        raise FileNotFoundError(f"hook source missing: {src}")
    hooks_dir = project_root / ".git" / "hooks"
    if not hooks_dir.is_dir():
        raise FileNotFoundError(
            f"not a git repository (no .git/hooks): {project_root}"
        )
    dst = hooks_dir / "pre-commit"
    if dst.exists():
        backup = dst.with_suffix(".bak")
        shutil.copy(dst, backup)
    shutil.copy(src, dst)
    dst.chmod(
        dst.stat().st_mode
        | stat.S_IXUSR
        | stat.S_IXGRP
        | stat.S_IXOTH
    )
    print(f"installed {dst}")
    return dst


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    install(target)
