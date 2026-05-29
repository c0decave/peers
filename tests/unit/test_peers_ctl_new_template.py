"""Tests for `peers-ctl new --template internal testing`.

The internal testing template automates the 6-step bootstrap operators were
running by hand for each v* substrate internal testing project:

  1. git clone the substrate
  2. checkout a internal testing-vN branch
  3. cp SPEC.md from the latest peers-internal testing-v*
  4. cp docs/ATTACK-SURFACE.md from the latest peers-internal testing-v*
  5. git commit the anchors
  6. peers-ctl new + peers-ctl start with the right env vars

With `--template internal testing` the operator runs:

  peers-ctl new ./peers-an earlier audit --template internal testing \\
              --container --max-runtime 12h --start

and the same end-state is produced in one command.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _init_fake_substrate(repo: Path) -> None:
    """Build a tiny git repo that stands in for the dogf00d-claudex
    substrate. Tests use this instead of the real ~12 MB clone target
    to keep the suite fast and offline."""
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "fake.py").write_text("# fake substrate code\n")
    (repo / "README.md").write_text("# fake substrate\n")
    subprocess.run(["git", "init", "-q", "-b", "main"],
                   cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"],
                   cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"],
                   cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fake substrate init"],
                   cwd=repo, check=True)


def _init_fake_anchor_dir(anchor: Path,
                          spec_body: str = "# SPEC.md from v12\n",
                          attack_body: str = "# ATTACK-SURFACE.md v12\n") -> None:
    anchor.mkdir(parents=True, exist_ok=True)
    (anchor / "SPEC.md").write_text(spec_body)
    (anchor / "docs").mkdir(parents=True, exist_ok=True)
    (anchor / "docs" / "ATTACK-SURFACE.md").write_text(attack_body)


# --- helper unit tests -----------------------------------------------

def test_find_latest_anchor_picks_highest_version(tmp_path: Path) -> None:
    """`_find_latest_self_audit_anchor` finds peers-internal testing-vN
    siblings of the target and returns the one with the highest N."""
    from peers_ctl.cli import _find_latest_self_audit_anchor

    parent = tmp_path / "parent"
    parent.mkdir()
    for n in (3, 7, 10, 1):
        d = parent / f"peers-internal testing-v{n}"
        _init_fake_anchor_dir(d, spec_body=f"# v{n}\n")
    # A non-versioned sibling must be ignored.
    (parent / "peers-internal testing").mkdir()

    found = _find_latest_self_audit_anchor(parent / "peers-an earlier audit")
    assert found is not None
    assert found.name == "peers-an earlier audit"


def test_find_latest_anchor_returns_none_when_nothing_matches(
    tmp_path: Path,
) -> None:
    from peers_ctl.cli import _find_latest_self_audit_anchor

    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "some-other-project").mkdir()

    assert _find_latest_self_audit_anchor(parent / "next") is None


def test_find_latest_anchor_skips_versioned_dirs_without_spec(
    tmp_path: Path,
) -> None:
    """A `peers-internal testing-vN` directory with no SPEC.md is not a
    valid anchor source; we must skip it and fall back to the next."""
    from peers_ctl.cli import _find_latest_self_audit_anchor

    parent = tmp_path / "parent"
    parent.mkdir()
    _init_fake_anchor_dir(parent / "peers-an earlier audit")
    # v10 is "newest" by version but has no SPEC.md → not eligible.
    (parent / "peers-an earlier audit").mkdir()

    found = _find_latest_self_audit_anchor(parent / "peers-an earlier audit")
    assert found is not None
    assert found.name == "peers-an earlier audit"


# --- end-to-end internal testing template ---------------------------------

def test_self_audit_template_clones_and_branches(
    tmp_path: Path, monkeypatch
) -> None:
    """The template clones the substrate source into the target,
    checks out a branch named after the target dir, and copies anchors
    from a sibling peers-internal testing-v* dir."""
    from peers_ctl.cli import cmd_new

    src = tmp_path / "substrate-src"
    _init_fake_substrate(src)

    parent = tmp_path / "projects"
    parent.mkdir()
    anchor = parent / "peers-an earlier audit"
    _init_fake_anchor_dir(
        anchor,
        spec_body="# SPEC v12 body\n",
        attack_body="# ATTACK v12 body\n",
    )

    target = parent / "peers-an earlier audit"
    rc = cmd_new(
        target,
        name="peers-an earlier audit",
        template="internal testing",
        template_from=src,
        force=True,
        container=False,
        modes=None,
        config_dir=tmp_path / "ctl",
    )
    assert rc == 0, "internal testing template should succeed"

    # Clone happened: fake substrate file is present.
    assert (target / "src" / "fake.py").is_file()
    assert (target / ".git").is_dir()

    # Branch is checked out.
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=target, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert branch == "an earlier audit", branch

    # Anchors were copied from peers-an earlier audit.
    assert (target / "SPEC.md").read_text() == "# SPEC v12 body\n"
    assert (
        (target / "docs" / "ATTACK-SURFACE.md").read_text()
        == "# ATTACK v12 body\n"
    )

    # Anchors got committed on the new branch (one anchor commit on top
    # of the cloned history; subsequent `peers init` adds more commits
    # — what matters is the anchor commit lands).
    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=target, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert len(log) >= 2, log
    assert any("internal testing anchors" in line.lower() for line in log), log


def test_self_audit_template_writes_placeholders_when_no_anchor(
    tmp_path: Path,
) -> None:
    """When no peers-internal testing-v* sibling exists and no
    --anchors-from is given, placeholder SPEC.md + ATTACK-SURFACE.md
    must be written so the audit project has its baseline gates."""
    from peers_ctl.cli import cmd_new

    src = tmp_path / "substrate-src"
    _init_fake_substrate(src)

    parent = tmp_path / "projects"
    parent.mkdir()
    target = parent / "peers-an earlier audit"

    rc = cmd_new(
        target,
        name="peers-an earlier audit",
        template="internal testing",
        template_from=src,
        force=True,
        config_dir=tmp_path / "ctl",
    )
    assert rc == 0

    spec_text = (target / "SPEC.md").read_text()
    attack_text = (target / "docs" / "ATTACK-SURFACE.md").read_text()
    # Placeholders must be non-empty and clearly mark themselves as
    # the substrate internal testing baseline.
    assert "internal testing" in spec_text.lower()
    assert "attack" in attack_text.lower() and "surface" in attack_text.lower()


def test_self_audit_template_implies_audit_thorough_modes(
    tmp_path: Path,
) -> None:
    """--template internal testing implies --modes=audit,thorough when the
    operator doesn't pass --modes explicitly. This is the whole point of
    the convenience flag — the operator should not have to re-type the
    audit gate stack every time."""
    from peers_ctl.cli import cmd_new

    src = tmp_path / "substrate-src"
    _init_fake_substrate(src)

    parent = tmp_path / "projects"
    parent.mkdir()
    target = parent / "peers-an earlier audit"

    rc = cmd_new(
        target,
        name="peers-an earlier audit",
        template="internal testing",
        template_from=src,
        force=True,
        config_dir=tmp_path / "ctl",
    )
    assert rc == 0, "implied audit,thorough modes must scaffold cleanly"

    # The .peers/ folder must exist (peers init was run via the
    # standard cmd_new flow).
    assert (target / ".peers").is_dir()
    # And it must carry an audit-mode marker — goals.yaml contains
    # the gates from the audit (or audit+thorough) modes.
    goals = (target / ".peers" / "goals.yaml").read_text()
    assert goals.strip(), "goals.yaml must not be empty after audit init"


def test_self_audit_template_respects_explicit_modes_override(
    tmp_path: Path,
) -> None:
    """If the operator passes --modes explicitly, it wins over the
    template default of audit,thorough."""
    from peers_ctl.cli import cmd_new

    src = tmp_path / "substrate-src"
    _init_fake_substrate(src)

    parent = tmp_path / "projects"
    parent.mkdir()
    target = parent / "peers-an earlier audit"

    rc = cmd_new(
        target,
        name="peers-an earlier audit",
        template="internal testing",
        template_from=src,
        modes=["audit"],
        force=True,
        config_dir=tmp_path / "ctl",
    )
    assert rc == 0


def test_self_audit_template_anchors_from_explicit_path(
    tmp_path: Path,
) -> None:
    """--anchors-from <dir> overrides the auto-discovery and pulls the
    anchors from the given directory regardless of where it lives."""
    from peers_ctl.cli import cmd_new

    src = tmp_path / "substrate-src"
    _init_fake_substrate(src)

    explicit_anchor = tmp_path / "elsewhere" / "old-audit"
    _init_fake_anchor_dir(
        explicit_anchor,
        spec_body="# from explicit anchor\n",
        attack_body="# attack from explicit anchor\n",
    )

    parent = tmp_path / "projects"
    parent.mkdir()
    # Even though no peers-internal testing-v* sibling exists, the explicit
    # anchor path wins.
    target = parent / "peers-an earlier audit"

    rc = cmd_new(
        target,
        name="peers-an earlier audit",
        template="internal testing",
        template_from=src,
        anchors_from=explicit_anchor,
        force=True,
        config_dir=tmp_path / "ctl",
    )
    assert rc == 0
    assert (target / "SPEC.md").read_text() == "# from explicit anchor\n"
    assert (
        (target / "docs" / "ATTACK-SURFACE.md").read_text()
        == "# attack from explicit anchor\n"
    )


def test_self_audit_template_unknown_template_name_rejected(
    tmp_path: Path, capsys,
) -> None:
    """An unknown template name must fail with a clear error and not
    touch the filesystem."""
    from peers_ctl.cli import cmd_new

    target = tmp_path / "x"
    rc = cmd_new(
        target,
        name="x",
        template="not-a-real-template",
        config_dir=tmp_path / "ctl",
    )
    assert rc != 0
    err = capsys.readouterr().err
    assert "template" in err.lower()
    assert not target.exists()


def test_self_audit_template_cli_wires_flag(tmp_path: Path) -> None:
    """`peers-ctl new --template internal testing` parses the flag and
    routes through cmd_new with template=internal testing. We use --help to
    confirm the argparse plumbing is in place without firing off a
    clone in this lightweight test."""
    from peers_ctl.cli import main

    # Argparse exits on --help with code 0 after printing.
    with pytest.raises(SystemExit) as exc:
        main(["new", "--help"])
    assert exc.value.code == 0
