"""Man-page-style detailed help for peers / peers-ctl.

Q4: each subcommand ships with a Markdown help-page in two languages
(English + German). Operators reach them via `--help-man` plus an
optional `--de`/`--en` selector. The default language follows the
system `LANG` env var (anything starting with `de` → German;
otherwise English).

Rendering is intentionally raw — we stream the Markdown to stdout
and let operators pipe it through `mdcat` / `glow` / `less` as they
prefer. Keeps the substrate dependency-free.

The templates live under `src/peers/templates/help/{en,de}/*.md` and
are addressed by a small lookup function so a missing file falls
back to a stable "no help-man available" message instead of crashing.
"""
from __future__ import annotations

import importlib.resources
import os
import sys
from pathlib import Path

from peers.safe_io import read_text_no_symlink


_HELP_MAN_MAX_BYTES = 256 * 1024  # plenty for ~150-line man-pages.


def help_man_dir() -> Path:
    """Locate the bundled `templates/help/` directory.

    Wrapped so tests can monkeypatch a fresh path for the
    fall-back-on-missing case without juggling import paths.
    """
    return Path(str(importlib.resources.files("peers").joinpath(
        "templates", "help",
    )))


def pick_lang(args) -> str:
    """Resolve the language for a `--help-man` invocation.

    Explicit `--en` / `--de` flags win; otherwise we honour
    `$LANG` (de_* → German, anything else → English).
    """
    if getattr(args, "en", False):
        return "en"
    if getattr(args, "de", False):
        return "de"
    sys_lang = (os.environ.get("LANG") or "").lower()
    return "de" if sys_lang.startswith("de") else "en"


def lookup_help_man_path(prefix: str, cmd: str | None,
                         subcmd: str | None, lang: str) -> Path:
    """Map a (prefix, cmd, subcmd, lang) tuple to a templates path.

    `prefix` is `peers` or `peers-ctl`. `cmd is None` → the
    bilingual overview page. A `subcmd` (e.g. `tmux up`) is
    folded into the filename with a dash.

    The returned path is NOT guaranteed to exist — the caller's
    fall-back is to print "no help-man available — try --help".
    """
    base = help_man_dir() / lang
    if cmd is None:
        if prefix == "peers-ctl":
            return base / "peers-ctl-overview.md"
        return base / "overview.md"
    name = f"{prefix}-{cmd}"
    if subcmd:
        name = f"{name}-{subcmd}"
    return base / f"{name}.md"


def print_help_man(prefix: str, cmd: str | None, subcmd: str | None,
                   lang: str) -> int:
    """Locate, read, and stream a help-man page to stdout.

    Returns the exit code (0 on a clean print, 0 on fall-back —
    the user asked for help; not finding it is an information
    failure, not a hard error).
    """
    path = lookup_help_man_path(prefix, cmd, subcmd, lang)
    if not path.is_file():
        what = prefix if cmd is None else f"{prefix} {cmd}"
        if subcmd:
            what = f"{what} {subcmd}"
        print(
            f"no help-man page available for `{what}` (lang={lang}) "
            f"— try `{prefix} --help` for the short summary.",
            file=sys.stderr,
        )
        return 0
    try:
        text = read_text_no_symlink(path, max_bytes=_HELP_MAN_MAX_BYTES)
    except OSError as e:
        print(f"cannot read help-man page {path}: {e}", file=sys.stderr)
        return 1
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def attach_help_man_flags(parser) -> None:
    """Bolt `--help-man` + `--de`/`--en` onto an argparse parser.

    Used on the top-level parser AND each subparser so the flags
    work uniformly (`peers --help-man` and `peers init --help-man`
    both succeed). `--de` and `--en` go through a mutually-exclusive
    group so passing both fails fast with a clear error.
    """
    parser.add_argument(
        "--help-man", dest="help_man", action="store_true",
        help="print a detailed man-page-style help document for "
             "this command and exit. Combine with --de/--en to "
             "force a language (default: $LANG → de/en).",
    )
    lang_group = parser.add_mutually_exclusive_group()
    lang_group.add_argument(
        "--de", dest="de", action="store_true",
        help="force German output for --help-man (default if "
             "LANG starts with de_*).",
    )
    lang_group.add_argument(
        "--en", dest="en", action="store_true",
        help="force English output for --help-man (default "
             "otherwise).",
    )
