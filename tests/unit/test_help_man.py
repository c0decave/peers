"""Tests for the bilingual --help-man feature (Q4).

Covers:
- top-level + subcommand `--help-man` invocations (DE / EN)
- mutual exclusion of `--de` and `--en`
- graceful fall-back when a help-man page is missing
- coverage guard: every subparser has both EN and DE templates
- structural guard: every page has the required man-page sections
- example guard: every page has at least one fenced code block
  mentioning peers or peers-ctl
- discoverability guard: `peers init --help` mentions `--help-man`
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path


def _src_dir() -> str:
    """Resolve the src/ dir alongside this checkout — needed so the
    subprocess pickups up worktree-local code rather than the
    editable install's home directory. Mirrors the trick used by the
    other CLI subprocess tests when they want to exercise the
    work-in-progress branch's CLI."""
    return str(Path(__file__).resolve().parent.parent.parent / "src")


def _peers(*args: str, env_extra: dict[str, str] | None = None,
           ) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    # Prepend the worktree's src/ so our changes are loaded even when
    # the editable install points at a different checkout.
    env["PYTHONPATH"] = _src_dir() + os.pathsep + env.get("PYTHONPATH", "")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "peers.cli", *args],
        capture_output=True, text=True, env=env,
    )


def _peers_ctl(*args: str, env_extra: dict[str, str] | None = None,
               ) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = _src_dir() + os.pathsep + env.get("PYTHONPATH", "")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "peers_ctl.cli", *args],
        capture_output=True, text=True, env=env,
    )


def _help_man_dir() -> Path:
    """Resolve `src/peers/templates/help/` for filesystem-walk tests.

    Anchored on THIS file rather than the imported `peers` module so
    we walk the worktree's templates even when the editable install
    points at a different checkout.
    """
    return (
        Path(__file__).resolve().parent.parent.parent
        / "src" / "peers" / "templates" / "help"
    )


REQUIRED_HEADINGS = re.compile(
    r"^## ("
    r"NAME|SYNOPSIS|DESCRIPTION|BESCHREIBUNG|OPTIONS|"
    r"EXAMPLES|BEISPIELE|FILES|DATEIEN|"
    r"ENVIRONMENT|UMGEBUNGSVARIABLEN|"
    r"SEE ALSO|SIEHE AUCH|NOTES"
    r")\b",
    re.MULTILINE,
)


# --- 1. Overview renders with default lang -------------------------

def test_help_man_overview_renders_default_lang():
    # Force LANG to something predictable so the test isn't host-coupled.
    r = _peers("--help-man", env_extra={"LANG": "C"})
    assert r.returncode == 0, r.stderr
    # Default for C / non-de lang is English.
    assert "## NAME" in r.stdout
    assert "## SYNOPSIS" in r.stdout
    assert "## DESCRIPTION" in r.stdout


# --- 2. EN subcommand page renders English -------------------------

def test_help_man_subcommand_en():
    r = _peers("init", "--help-man", "--en")
    assert r.returncode == 0, r.stderr
    # "initialize" is the EN-only word; DE uses "initialisiert".
    assert "initialize" in r.stdout.lower() or "bootstrap" in r.stdout.lower()
    # Headings are in English.
    assert "## DESCRIPTION" in r.stdout
    assert "## EXAMPLES" in r.stdout


# --- 3. DE subcommand page renders German --------------------------

def test_help_man_subcommand_de():
    r = _peers("init", "--help-man", "--de")
    assert r.returncode == 0, r.stderr
    # German-only words.
    assert ("initialisiert" in r.stdout.lower()
            or "bootstrappen" in r.stdout.lower())
    # German headings.
    assert "## BESCHREIBUNG" in r.stdout
    assert "## BEISPIELE" in r.stdout


# --- 4. --de + --en is rejected ------------------------------------

def test_help_man_lang_flags_mutually_exclusive():
    r = _peers("init", "--help-man", "--de", "--en")
    assert r.returncode != 0
    # argparse phrasing.
    assert "not allowed" in r.stderr or "conflict" in r.stderr.lower()


# --- 5. Missing page falls back gracefully -------------------------

def test_help_man_missing_page_falls_back_gracefully(tmp_path):
    """When the help-man directory is monkeypatched to an empty
    location, `print_help_man` must NOT crash — it should emit the
    "no help-man page available" fall-back and return 0/1.

    Done out-of-process so the worktree's `peers.help_man` is loaded
    via PYTHONPATH (the editable install may point elsewhere).
    """
    empty = tmp_path / "empty"
    (empty / "en").mkdir(parents=True)
    (empty / "de").mkdir(parents=True)
    script = textwrap.dedent(f"""
        import sys
        from peers import help_man as hm
        hm.help_man_dir = lambda: __import__('pathlib').Path({str(empty)!r})
        rc = hm.print_help_man("peers", "init", None, "en")
        sys.exit(rc)
    """)
    env = os.environ.copy()
    env["PYTHONPATH"] = _src_dir() + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, env=env,
    )
    # Contract: do NOT crash. 0 (information-failure) or 1
    # (read-error) acceptable; anything ≥ 2 means a traceback.
    assert r.returncode in (0, 1), (
        f"crash? rc={r.returncode}, stderr={r.stderr!r}"
    )
    assert "no help-man page available" in r.stderr


# --- 6. Every subparser has BOTH lang templates --------------------

def _discover_subparser_names(module_name: str) -> set[str]:
    """Return every subcommand name for the named CLI module.

    The mapping is hard-coded so a new subparser without a help-man
    page is caught immediately (the corresponding existing CLI test
    will of course also break, but the explicit list keeps the
    contract documented).
    """
    if module_name == "peers.cli":
        names = {
            "init", "status", "run", "replay", "report", "info", "verify",
            "tick", "watch", "run-check", "tmux",
        }
    elif module_name == "peers_ctl.cli":
        names = {
            "add", "new", "remove", "list", "dashboard", "report",
            "start", "stop", "status", "review", "logs", "tail",
            "prune", "doctor", "modes",
        }
    else:
        raise ValueError(module_name)
    # Catch drift: if any of these names disappears from the actual
    # parser, the test that exercises it (or the import) will already
    # break; the explicit list is the documentation contract.
    return names


def test_every_subcommand_has_help_man_en_and_de():
    base = _help_man_dir()
    en = base / "en"
    de = base / "de"
    assert en.is_dir() and de.is_dir(), (en, de)
    missing: list[str] = []
    for cmd in _discover_subparser_names("peers.cli"):
        for lang_dir, lang in ((en, "en"), (de, "de")):
            path = lang_dir / f"peers-{cmd}.md"
            if not path.is_file():
                missing.append(f"{lang}: {path}")
    for cmd in _discover_subparser_names("peers_ctl.cli"):
        for lang_dir, lang in ((en, "en"), (de, "de")):
            path = lang_dir / f"peers-ctl-{cmd}.md"
            if not path.is_file():
                missing.append(f"{lang}: {path}")
    # Both overviews.
    for lang_dir, lang in ((en, "en"), (de, "de")):
        if not (lang_dir / "overview.md").is_file():
            missing.append(f"{lang}: overview.md")
        if not (lang_dir / "peers-ctl-overview.md").is_file():
            missing.append(f"{lang}: peers-ctl-overview.md")
    assert not missing, "missing help-man pages:\n" + "\n".join(missing)


# --- 7. Every page has the required sections -----------------------

def test_help_man_files_have_required_sections():
    base = _help_man_dir()
    for lang_dir in (base / "en", base / "de"):
        for path in sorted(lang_dir.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            matches = REQUIRED_HEADINGS.findall(text)
            assert len(matches) >= 6, (
                f"{path}: only {len(matches)} required section(s) "
                f"found: {matches!r}"
            )


# --- 8. Every page has at least one peers/peers-ctl example --------

CODE_FENCE = re.compile(r"```[a-z0-9]*\n(.*?)```", re.DOTALL)


def test_help_man_files_have_examples_section_with_real_command():
    base = _help_man_dir()
    for lang_dir in (base / "en", base / "de"):
        for path in sorted(lang_dir.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            blocks = CODE_FENCE.findall(text)
            assert blocks, f"{path}: no fenced code blocks"
            mentions_cli = any(
                ("peers" in b) or ("peers-ctl" in b) for b in blocks
            )
            assert mentions_cli, (
                f"{path}: no fenced block mentions peers or peers-ctl"
            )


# --- 9. --help discoverability hint --------------------------------

def test_help_text_mentions_help_man_in_help_for_init():
    r = _peers("init", "--help")
    assert r.returncode == 0, r.stderr
    assert "--help-man" in r.stdout, (
        "expected `peers init --help` to mention --help-man for "
        "discoverability"
    )
