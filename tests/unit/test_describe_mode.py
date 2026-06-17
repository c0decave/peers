"""Tests for the `describe` mode checks.

describe is the iterative-doc-writing mode (peers write SPEC.md,
ARCHITECTURE.md, DESIGN.md until they converge). Three hard checks:
- description_files_present: 3 files exist + ≥500 bytes each
- description_sections_present: required ## sections + ≥50 bytes body
- description_converged: last N commits to docs are non-substantive
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from peers.templates.modes.describe.checks import (
    description_converged,
    description_files_present,
    description_sections_present,
)


# ---------------- description_files_present ----------------

def _make_doc(repo: Path, name: str, content: str) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(content)


def _full_docs(repo: Path) -> None:
    _make_doc(
        repo, "SPEC.md",
        "# Spec\n\n" + ("Lorem ipsum dolor sit amet. " * 30),
    )
    _make_doc(
        repo, "ARCHITECTURE.md",
        "# Arch\n\n" + ("Lorem ipsum dolor sit amet. " * 30),
    )
    _make_doc(
        repo, "DESIGN.md",
        "# Design\n\n" + ("Lorem ipsum dolor sit amet. " * 30),
    )


def test_files_present_clean_passes(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    _full_docs(repo)
    assert description_files_present.main(str(repo)) == 0
    assert "clean" in capsys.readouterr().out


def test_files_present_missing_spec_fails(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    _full_docs(repo)
    (repo / "SPEC.md").unlink()
    assert description_files_present.main(str(repo)) == 1
    assert "SPEC.md: missing" in capsys.readouterr().out


def test_files_present_short_file_fails(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    _full_docs(repo)
    (repo / "DESIGN.md").write_text("# tiny\n")
    rc = description_files_present.main(str(repo))
    out = capsys.readouterr().out
    assert rc == 1
    assert "DESIGN.md" in out
    assert "too short" in out


def test_files_present_symlink_refused(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    _full_docs(repo)
    target = tmp_path / "elsewhere.md"
    target.write_text("X" * 1000)
    (repo / "SPEC.md").unlink()
    (repo / "SPEC.md").symlink_to(target)
    rc = description_files_present.main(str(repo))
    assert rc == 1
    assert "symlink" in capsys.readouterr().out


# ---------------- description_sections_present ----------------

def _full_sections(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    pad = "Concrete content describing the section in detail. " * 3
    (repo / "SPEC.md").write_text(
        "# Spec\n\n"
        f"## Threat Model\n\n{pad}\n\n"
        f"## Invariants\n\n{pad}\n\n"
        f"## API\n\n{pad}\n"
    )
    (repo / "ARCHITECTURE.md").write_text(
        "# Arch\n\n"
        f"## Components\n\n{pad}\n\n"
        f"## Data Flow\n\n{pad}\n"
    )
    (repo / "DESIGN.md").write_text(
        "# Design\n\n"
        f"## Decisions\n\n{pad}\n\n"
        f"## Tradeoffs\n\n{pad}\n"
    )


def test_sections_present_clean_passes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _full_sections(repo)
    assert description_sections_present.main(str(repo)) == 0


def test_sections_present_missing_threat_model_fails(
    tmp_path: Path, capsys,
) -> None:
    repo = tmp_path / "repo"
    _full_sections(repo)
    text = (repo / "SPEC.md").read_text()
    (repo / "SPEC.md").write_text(text.replace("## Threat Model", "## TM"))
    rc = description_sections_present.main(str(repo))
    assert rc == 1
    assert "## Threat Model" in capsys.readouterr().out


def test_sections_present_empty_section_fails(
    tmp_path: Path, capsys,
) -> None:
    repo = tmp_path / "repo"
    _full_sections(repo)
    (repo / "DESIGN.md").write_text(
        "## Decisions\n\n## Tradeoffs\n\n"  # both empty
    )
    rc = description_sections_present.main(str(repo))
    out = capsys.readouterr().out
    assert rc == 1
    assert "Decisions" in out and "too short" in out


def test_sections_present_missing_arch_components_fails(
    tmp_path: Path, capsys,
) -> None:
    repo = tmp_path / "repo"
    _full_sections(repo)
    (repo / "ARCHITECTURE.md").write_text(
        "# Arch\n\n## Data Flow\n\nContent.\n"
    )
    rc = description_sections_present.main(str(repo))
    out = capsys.readouterr().out
    assert rc == 1
    assert "ARCHITECTURE.md" in out
    assert "Components" in out


# ---------------- description_converged ----------------

def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"],
                   cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"],
                   cwd=repo, check=True)
    (repo / "INIT").write_text("init\n")
    subprocess.run(["git", "add", "INIT"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"],
                   cwd=repo, check=True)


def _commit_doc_change(repo: Path, name: str, content: str, msg: str) -> str:
    (repo / name).write_text(content)
    subprocess.run(["git", "add", name], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=repo, check=True)
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    return out


def test_converged_no_commits_fails(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    rc = description_converged.main(str(repo))
    assert rc == 1
    assert "no commits" in capsys.readouterr().out


def test_converged_fewer_than_n_commits_fails(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_doc_change(repo, "SPEC.md", "# Spec\n\n## TM\n\nfoo\n", "draft")
    # Default N=2, but only 1 commit so far
    rc = description_converged.main(str(repo))
    assert rc == 1


def test_converged_two_small_commits_passes(
    tmp_path: Path, capsys,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = "# Spec\n\n## TM\n\n" + ("x " * 200) + "\n"
    _commit_doc_change(repo, "SPEC.md", base, "initial draft")
    # small fix 1
    _commit_doc_change(
        repo, "SPEC.md", base + "small fix\n", "fix typo",
    )
    # small fix 2
    _commit_doc_change(
        repo, "SPEC.md", base + "small fix\nanother\n", "tighten phrasing",
    )
    rc = description_converged.main(str(repo))
    out = capsys.readouterr().out
    assert rc == 0
    assert "clean" in out


def test_converged_substantive_recent_commit_fails(
    tmp_path: Path, capsys,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = "# Spec\n\n## TM\n\nfoo\n"
    _commit_doc_change(repo, "SPEC.md", base, "draft")
    # small fix
    _commit_doc_change(
        repo, "SPEC.md", base + "fix\n", "minor",
    )
    # MASSIVE addition (>100 lines)
    big = base + ("\nline " + "x " * 5 + "\n") * 200
    _commit_doc_change(repo, "SPEC.md", big, "massive expansion")
    rc = description_converged.main(str(repo))
    out = capsys.readouterr().out
    assert rc == 1
    assert "substantive" in out
    assert "added" in out


def test_converged_new_section_is_substantive(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = "# Spec\n\n## TM\n\nfoo\n"
    _commit_doc_change(repo, "SPEC.md", base, "draft")
    # tweak
    _commit_doc_change(repo, "SPEC.md", base + "tweak\n", "tweak")
    # NEW section: ## API added (not in parent) — substantive
    _commit_doc_change(
        repo, "SPEC.md",
        base + "tweak\n## API\n\nendpoint list\n",
        "add API section",
    )
    rc = description_converged.main(str(repo))
    assert rc == 1


def test_converged_high_deletion_ratio_is_substantive(
    tmp_path: Path, capsys,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    big = "# Spec\n\n## TM\n\n" + ("line content\n" * 200)
    _commit_doc_change(repo, "SPEC.md", big, "initial big draft")
    _commit_doc_change(repo, "SPEC.md", big + "fix\n", "tweak")
    # delete most content
    _commit_doc_change(
        repo, "SPEC.md", "# Spec\n\n## TM\n\nshort\n", "big delete",
    )
    rc = description_converged.main(str(repo))
    out = capsys.readouterr().out
    assert rc == 1
    assert "deletion ratio" in out


def test_converged_respects_custom_n(tmp_path: Path) -> None:
    """`.peers/config.yaml` goals.describe_convergence_n=3 needs 3 clean."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    peer_dir = repo / ".peers"
    peer_dir.mkdir()
    (peer_dir / "config.yaml").write_text(
        "goals:\n  describe_convergence_n: 3\n",
    )
    # initial big commit (substantive — creates the doc with section)
    base = "# Spec\n\n## TM\n\n" + ("x " * 200) + "\n"
    _commit_doc_change(repo, "SPEC.md", base, "initial draft")
    # Now 3 small follow-up commits — none substantive
    _commit_doc_change(repo, "SPEC.md", base + "f1\n", "fix 1")
    _commit_doc_change(repo, "SPEC.md", base + "f1\nf2\n", "fix 2")
    # After only 2 small follow-ups: N=3 includes the initial substantive →
    # still fails.
    assert description_converged.main(str(repo)) == 1
    _commit_doc_change(repo, "SPEC.md", base + "f1\nf2\nf3\n", "fix 3")
    # Now last 3 commits are all small follow-ups → pass
    assert description_converged.main(str(repo)) == 0


def test_read_convergence_n_fails_closed_when_yaml_missing_and_config_exists(
    tmp_path: Path, monkeypatch,
) -> None:
    # FU-1 defense-in-depth: PyYAML is a hard dependency, but if it is ever
    # unavailable (ImportError -> module-level yaml=None) we cannot parse a
    # config that may set a STRICTER describe_convergence_n. Silently falling
    # back to DEFAULT_N would weaken a configured HARD gate, so when a
    # config.yaml is present we fail CLOSED rather than guess.
    monkeypatch.setattr(description_converged, "yaml", None)
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir()
    (peer_dir / "config.yaml").write_text(
        "goals:\n  describe_convergence_n: 5\n",
    )
    with pytest.raises(RuntimeError):
        description_converged._read_convergence_n(tmp_path)


def test_read_convergence_n_yaml_missing_no_config_uses_default(
    tmp_path: Path, monkeypatch,
) -> None:
    # edge: with PyYAML unavailable AND no config.yaml there is nothing
    # configured to violate, so the gate keeps running with DEFAULT_N rather
    # than failing closed spuriously (don't over-correct the hardening).
    monkeypatch.setattr(description_converged, "yaml", None)
    assert (
        description_converged._read_convergence_n(tmp_path)
        == description_converged.DEFAULT_N
    )


def test_converged_refuses_symlinked_config_leaf(
    tmp_path: Path, capsys,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    peer_dir = repo / ".peers"
    peer_dir.mkdir()
    outside = tmp_path / "outside-config.yaml"
    outside.write_text("goals:\n  describe_convergence_n: 1\n")
    try:
        (peer_dir / "config.yaml").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable for this platform: {exc}")

    assert description_converged.main(str(repo)) == 1
    out = capsys.readouterr().out
    assert "description_converged FAIL" in out
    assert "config.yaml unreadable" in out


def test_converged_refuses_symlinked_peers_ancestor(
    tmp_path: Path, capsys,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    outside_peers = tmp_path / "outside-peers"
    outside_peers.mkdir()
    (outside_peers / "config.yaml").write_text(
        "goals:\n  describe_convergence_n: 1\n",
    )
    try:
        (repo / ".peers").symlink_to(outside_peers, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable for this platform: {exc}")

    assert description_converged.main(str(repo)) == 1
    out = capsys.readouterr().out
    assert "description_converged FAIL" in out
    assert "config.yaml unreadable" in out


# ---------------- mode discoverability ----------------

def test_describe_mode_discoverable_via_peers_modes() -> None:
    """Mode discovery scans the templates/modes/ dir; the describe dir
    should be picked up so `--modes=describe` works."""
    from peers.modes import discover

    modes = discover()
    assert "describe" in modes
    m = modes["describe"]
    assert m.version >= 1
    assert m.description  # non-empty


def test_describe_goals_yaml_is_valid_yaml() -> None:
    """Catch yaml syntax mistakes in the goals.yaml early."""
    import yaml as _yaml
    from peers.modes import _builtin_modes_dir  # type: ignore[attr-defined]

    goals_path = _builtin_modes_dir() / "describe" / "goals.yaml"
    text = goals_path.read_text()
    parsed = _yaml.safe_load(text)
    assert isinstance(parsed, dict)
    assert "goals" in parsed
    ids = {g["id"] for g in parsed["goals"] if isinstance(g, dict)}
    assert "description-files-present" in ids
    assert "description-sections-present" in ids
    assert "description-converged" in ids
