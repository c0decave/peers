"""BUG-168: document-mode gates must use no-follow bounded I/O.

CODEMAP.yaml, ARCHITECTURE.md, and AGENTS.md are read by hard gates.
The previous code used `Path.is_file()` (follows symlinks) and
`Path.read_text()` / `Path.read_bytes()` (no size cap), so a repo
could plant a symlink at any of these names to an external file the
peers process happens to be allowed to read, or to an oversized blob
that exhausts memory.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from peers.codemap import (
    CodeMap,
    CodeMapError,
    check_architecture,
    parse_codemap,
)
from peers.codemap_gen import check_agents_sync


@pytest.fixture
def empty_codemap() -> CodeMap:
    return CodeMap(entries=())


def test_parse_codemap_refuses_symlinked_yaml_BUG_168(tmp_path: Path) -> None:
    """A symlink at CODEMAP.yaml must be refused, not silently followed."""
    real = tmp_path / "elsewhere.yaml"
    real.write_text("entries: []\n")
    link = tmp_path / "CODEMAP.yaml"
    link.symlink_to(real)
    with pytest.raises(CodeMapError):
        parse_codemap(link)


def test_check_architecture_refuses_symlinked_md_BUG_168(
    tmp_path: Path, empty_codemap: CodeMap,
) -> None:
    """A symlinked ARCHITECTURE.md should produce a violation rather
    than read the symlink target."""
    proj = tmp_path / "proj"
    proj.mkdir()
    elsewhere = tmp_path / "evil.md"
    elsewhere.write_text(
        "external file content [[peers.cli]]\n"
    )
    link = proj / "ARCHITECTURE.md"
    link.symlink_to(elsewhere)
    violations = check_architecture(proj, empty_codemap)
    assert violations
    joined = " ".join(violations).lower()
    assert "symlink" in joined or "refus" in joined or "unreadable" in joined


def test_check_agents_sync_refuses_symlinked_md_BUG_168(
    tmp_path: Path, empty_codemap: CodeMap,
) -> None:
    """A symlinked AGENTS.md should produce a violation rather than
    read the symlink target."""
    proj = tmp_path / "proj"
    proj.mkdir()
    elsewhere = tmp_path / "evil.md"
    elsewhere.write_text("external content\n")
    link = proj / "AGENTS.md"
    link.symlink_to(elsewhere)
    violations = check_agents_sync(proj, empty_codemap)
    assert violations
    joined = " ".join(violations).lower()
    assert "symlink" in joined or "refus" in joined or "unreadable" in joined


def test_parse_codemap_rejects_oversize_yaml_BUG_168(tmp_path: Path) -> None:
    """A multi-hundred-MB CODEMAP must not be slurped into memory.
    The cap is an order-of-magnitude protection, not a hard contract;
    we just need an error rather than unbounded read_text."""
    p = tmp_path / "CODEMAP.yaml"
    # 12 MiB > default 8 MiB cap
    p.write_text("# pad\n" + ("x" * (12 * 1024 * 1024)) + "\nentries: []\n")
    with pytest.raises(CodeMapError):
        parse_codemap(p)
