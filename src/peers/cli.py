"""peers CLI entrypoint."""
from __future__ import annotations

import argparse
import importlib.resources
import json
import math
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence

import yaml

from peers.driver_orchestrator import OrchestratorDriver
from peers.goals import load_goals
from peers.help_man import (
    attach_help_man_flags,
    pick_lang,
    print_help_man,
)
from peers.peer_spec import is_valid_peer_name, load_peer_specs
from peers.safe_io import (
    open_text_in_dir_no_symlink,
    open_text_read_no_symlink,
    open_text_no_symlink,
    read_bytes_no_symlink,
    read_text_no_symlink,
    write_text_no_symlink,
)


_CONFIG_YAML_MAX_BYTES = 2 * 1024 * 1024
_ERROR_PATTERNS_MAX_COUNT = 50
_ERROR_PATTERN_MAX_BYTES = 1024


def _templates_dir() -> Path:
    return Path(str(importlib.resources.files("peers").joinpath("templates")))


def _hook_install_marker(target: Path) -> str:
    """Stable per-project identifier used to recognise our own hook
    entries on re-install (idempotency) and on uninstall (not yet
    implemented). Format: `peers:<absolute-resolved-target-path>`."""
    return f"peers:{Path(target).resolve()}"


def _install_claude_settings(
    settings_path: Path, hook_command: str, marker: str,
) -> tuple[str, Path | None]:
    """Merge our Stop-hook into ~/.claude/settings.json idempotently.

    Returns (status, backup_path_or_None) where status is one of:
      - "installed"  — new entry written
      - "updated"    — replaced existing peers entry (e.g. command drift)
      - "noop"       — entry already present and identical
      - "skipped"    — settings.json invalid/unsafe → no change

    Strategy:
    - Read existing JSON (or start fresh with `{}`).
    - Ensure `hooks.Stop` is a list. Other entries are preserved.
    - Each Stop entry has shape `{"matcher": ..., "hooks": [...]}`.
      We look for any nested hook with our `marker` in its `command`;
      if found and identical, noop; if found and drifted, update.
    - Otherwise append a fresh `{"matcher": "", "hooks": [{...}]}` block.
    - Atomic write tmp+rename + backup of the prior file.
    """
    import datetime
    import os

    full_command = f"{hook_command} # {marker}"

    if settings_path.is_symlink():
        return "skipped", None
    if settings_path.exists():
        try:
            existing_raw = settings_path.read_text()
            data = json.loads(existing_raw) if existing_raw.strip() else {}
        except (json.JSONDecodeError, OSError):
            return "skipped", None
        if not isinstance(data, dict):
            return "skipped", None
    else:
        existing_raw = ""
        data = {}

    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return "skipped", None
    stop = hooks.setdefault("Stop", [])
    if not isinstance(stop, list):
        return "skipped", None

    # Look for an existing peers-managed entry by scanning for our marker.
    status: str | None = None
    for entry in stop:
        if not isinstance(entry, dict):
            continue
        for inner in entry.get("hooks") or []:
            if not isinstance(inner, dict):
                continue
            cmd = inner.get("command", "")
            if not isinstance(cmd, str) or marker not in cmd:
                continue
            if cmd == full_command:
                status = "noop"
            else:
                inner["command"] = full_command
                status = "updated"
            break
        if status is not None:
            break

    if status is None:
        stop.append({
            "matcher": "",
            "hooks": [{"type": "command", "command": full_command}],
        })
        status = "installed"

    if status == "noop":
        return "noop", None

    # Backup + atomic write.
    backup: Path | None = None
    if existing_raw:
        ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        backup = settings_path.with_suffix(
            settings_path.suffix + f".bak.peers-{ts}"
        )
        write_text_no_symlink(backup, existing_raw)

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings_path.with_suffix(settings_path.suffix + ".tmp.peers")
    write_text_no_symlink(tmp, json.dumps(data, indent=2) + "\n")
    os.replace(tmp, settings_path)
    return status, backup


def _install_codex_config(
    config_path: Path, hook_command: str, marker: str,
) -> tuple[str, Path | None]:
    """Append `[hooks] on_stop = ...` to ~/.codex/config.toml.

    TOML editing without a write-capable parser is brittle, so the
    strategy is conservative:
    - If the file has no `[hooks]` section, append one with our marker.
    - If `[hooks]` exists with our marker line, noop / update.
    - If `[hooks]` exists WITHOUT our marker (user has their own
      `on_stop`), return "skipped" with a note — we will not clobber.

    Returns (status, backup_path_or_None).
    """
    import datetime
    import os

    marker_comment = f"# {marker}"
    toml_command = json.dumps(hook_command)
    new_block = (
        "\n[hooks]\n"
        f"on_stop = {toml_command}  {marker_comment}\n"
    )

    if config_path.is_symlink():
        return "skipped", None
    if not config_path.exists():
        backup = None
        config_path.parent.mkdir(parents=True, exist_ok=True)
        write_text_no_symlink(config_path, new_block.lstrip("\n"))
        return "installed", backup

    try:
        text = config_path.read_text()
    except OSError:
        return "skipped", None

    if marker in text:
        # Idempotent: if the exact line already present, noop.
        expected_line = (
            f"on_stop = {toml_command}  {marker_comment}"
        )
        if expected_line in text:
            return "noop", None
        # Replace our previous line (command drift).
        new_lines = []
        replaced = False
        for line in text.splitlines():
            if marker in line and "on_stop" in line:
                new_lines.append(expected_line)
                replaced = True
            else:
                new_lines.append(line)
        if replaced:
            ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
            backup = config_path.with_suffix(
                config_path.suffix + f".bak.peers-{ts}"
            )
            write_text_no_symlink(backup, text)
            tmp = config_path.with_suffix(config_path.suffix + ".tmp.peers")
            write_text_no_symlink(tmp, "\n".join(new_lines) + "\n")
            os.replace(tmp, config_path)
            return "updated", backup
        return "skipped", None

    if re.search(r"(?m)^\[hooks\]\s*$", text):
        # An existing [hooks] section without our marker — refuse to
        # mix in: user likely has a custom on_stop already.
        return "skipped", None

    ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = config_path.with_suffix(
        config_path.suffix + f".bak.peers-{ts}"
    )
    write_text_no_symlink(backup, text)
    tmp = config_path.with_suffix(config_path.suffix + ".tmp.peers")
    if not text.endswith("\n"):
        text = text + "\n"
    write_text_no_symlink(tmp, text + new_block)
    os.replace(tmp, config_path)
    return "installed", backup


def _maybe_write_hooks(target: Path, peer_dir: Path) -> None:
    """G2: when --driver=hooks is selected, scaffold the
    Stop-hook snippets for claude and codex so the chain can be wired
    without the user editing JSON/TOML manually.

    We DO NOT touch any existing .claude/settings.json or
    .codex/config.toml. Instead we drop ready-to-paste fragments in
    .peers/hooks/ with clear filenames.

    Security: target paths are shlex-quoted before embedding in the
    hook command strings — peers init refuses sensitive top-level
    paths earlier, but a project under e.g. `/tmp/with space/$(...)`
    would otherwise inject shell into the user's settings.json.
    """
    import shlex
    hooks_dir = peer_dir / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    write_text_no_symlink(
        hooks_dir / "README.md",
        "# Hook-driver snippets\n\n"
        "These files are NOT auto-installed — they show what to add\n"
        "to your `~/.claude/settings.json` and `~/.codex/config.toml`\n"
        "to make Claude/Codex Stop-hooks trigger the next peer tick.\n\n"
        "After editing, verify with:\n\n"
        "    peers status\n"
        "    peers tick   # one manual tick, should run without errors\n"
    )
    quoted_target = shlex.quote(str(Path(target).resolve()))
    # NOTE: -C/--target is a *parent* flag in `peers`, so it MUST come
    # before the subcommand. `peers tick --target X` would fail with an
    # "unrecognized arguments" error.
    claude_snippet = {
        "hooks": {
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                f"peers -C {quoted_target} tick "
                                "--after claude"
                            ),
                        }
                    ],
                }
            ]
        }
    }
    write_text_no_symlink(
        hooks_dir / "claude-stop-hook.json",
        json.dumps(claude_snippet, indent=2) + "\n",
    )
    codex_snippet = (
        "# Append to ~/.codex/config.toml\n"
        "[hooks]\n"
        f"on_stop = \"peers -C {quoted_target} tick --after codex\"\n"
    )
    write_text_no_symlink(hooks_dir / "codex-config.toml", codex_snippet)


def _install_host_hooks(target: Path,
                        claude_settings: Path | None = None,
                        codex_config: Path | None = None) -> int:
    """Run after `_maybe_write_hooks` to also patch the user's host
    config files. Returns 0 on success (incl. noop), 1 if neither file
    could be touched safely (caller should warn).

    Allows test injection of paths; defaults to `~/.claude/settings.json`
    and `~/.codex/config.toml`.
    """
    import shlex
    marker = _hook_install_marker(target)
    quoted_target = shlex.quote(str(Path(target).resolve()))
    claude_cmd = f"peers -C {quoted_target} tick --after claude"
    codex_cmd = f"peers -C {quoted_target} tick --after codex"

    claude_settings = (claude_settings if claude_settings is not None
                       else Path.home() / ".claude" / "settings.json")
    codex_config = (codex_config if codex_config is not None
                    else Path.home() / ".codex" / "config.toml")

    ok = 0
    for label, fn, path, cmd in (
        ("claude", _install_claude_settings, claude_settings, claude_cmd),
        ("codex",  _install_codex_config,    codex_config,    codex_cmd),
    ):
        try:
            status, backup = fn(path, cmd, marker)
        except OSError as e:
            print(f"hooks --install: {label}: I/O error: {e}",
                  file=sys.stderr)
            continue
        if status == "skipped":
            print(
                f"hooks --install: {label}: {path} looks pre-configured "
                f"or unsafe — left untouched. Inspect it manually and "
                f"merge the snippet from .peers/hooks/ if needed.",
                file=sys.stderr,
            )
            continue
        ok += 1
        msg = f"hooks --install: {label}: {status} → {path}"
        if backup is not None:
            msg += f" (backup: {backup.name})"
        print(msg)
    return 0 if ok > 0 else 1


def cmd_init(target: Path, force: bool, driver: str = "orchestrator",
             install_hooks: bool = False,
             modes: list[str] | None = None,
             # legacy alias — kept for back-compat:
             audit_templates: bool = False,
             lang: str = "python") -> int:
    # Normalize legacy --audit-templates → --modes=audit with a stderr note.
    if audit_templates and not modes:
        modes = ["audit"]
        print("peers: --audit-templates is a deprecated alias for "
              "`--modes=audit`. Use --modes going forward.",
              file=sys.stderr)
    modes = modes or []
    # Pre-flight: resolve modes BEFORE scaffolding. If resolution fails
    # (unknown mode, cycle, conflict), the user's .peers/ is left
    # untouched — half-written state is worse than no state.
    resolved_modes: list = []
    merged_goals: dict | None = None
    check_files: list = []
    # Normalize lang aliases up-front so the modes layer (which keys
    # off `lang_<lang>/` subdir names) gets the canonical token.
    from peers.modes import normalize_lang
    lang_canonical = normalize_lang(lang)
    if modes:
        from peers.modes import resolve as _modes_resolve, merge as _modes_merge
        try:
            resolved_modes = _modes_resolve(modes)
            merged_goals, check_files = _modes_merge(
                resolved_modes, lang=lang_canonical
            )
        except ValueError as e:
            print(f"peers: {e}", file=sys.stderr)
            return 1
    target = Path(target)
    if not target.is_dir():
        print(f"target is not an existing directory: {target}",
              file=sys.stderr)
        return 2
    resolved = target.resolve()
    sensitive = {Path("/").resolve(), Path.home().resolve()}
    if resolved in sensitive:
        print(f"refusing to init in {resolved} (sensitive path)",
              file=sys.stderr)
        return 2

    gitignore = target / ".gitignore"
    if gitignore.is_symlink():
        try:
            existing_gitignore = gitignore.read_text()
        except OSError as e:
            print(f"cannot read symlinked .gitignore safely: {e}",
                  file=sys.stderr)
            return 2
        has_peers_entry = any(
            line.strip().rstrip("/") == ".peers"
            for line in existing_gitignore.splitlines()
        )
        if not has_peers_entry:
            print(
                f"refusing to modify symlinked .gitignore: {gitignore}. "
                "Add `.peers/` manually or replace the symlink first.",
                file=sys.stderr,
            )
            return 2

    peers = target / ".peers"
    if peers.is_symlink():
        print(
            f"refusing to operate on {peers}: it is a symlink; "
            "remove it manually first",
            file=sys.stderr,
        )
        return 2
    if peers.exists() and not force:
        print(
            f".peers/ already exists in {target} (use --force to overwrite)",
            file=sys.stderr,
        )
        return 2
    if peers.exists() and force:
        if not peers.is_dir():
            print(
                f"refusing to overwrite {peers}: it exists but is not "
                "a directory",
                file=sys.stderr,
            )
            return 2
        shutil.rmtree(peers)
    peers.mkdir(parents=True)
    (peers / "log").mkdir()
    (peers / "checks").mkdir()
    try:
        with open_text_in_dir_no_symlink(peers / "log", "runs.jsonl", "a"):
            pass
    except OSError as e:
        print(f"cannot create run log safely: {e}", file=sys.stderr)
        return 1

    src = _templates_dir()
    shutil.copy(src / "config.yaml", peers / "config.yaml")
    shutil.copy(src / "goals.yaml", peers / "goals.yaml")
    shutil.copy(
        src / "modes" / "audit" / "checks" / "verify_self_review.py",
        peers / "checks" / "verify_self_review.py",
    )
    (peers / "checks" / "verify_self_review.py").chmod(0o755)

    # New canonical path: --modes=a,b,c → install the pre-resolved set.
    # Runs AFTER the default scaffold so we overwrite goals.yaml with
    # the merged-modes content and drop the merged check scripts in.
    # NOTE: resolve()+merge() already ran as a pre-flight near the top
    # of cmd_init so an unknown-mode / cycle / conflict failure leaves
    # the user's tree untouched. Here we just write the results.
    if modes:
        # Overwrite goals.yaml with the merged set.
        goals_text = yaml.safe_dump(
            merged_goals, sort_keys=False, allow_unicode=True
        )
        # When a non-python lang is requested AND the audit mode is in
        # play, rewrite the audit goal command strings to point at the
        # lang-specific check files we're about to copy
        # (e.g. `python3 .peers/checks/coverage_3class.py …` →
        # `node .peers/checks/coverage_3class.js`). The mapping is
        # audit-specific knowledge that lives next to the audit
        # templates, so we delegate to the existing helper.
        if any(m.name == "audit" for m in resolved_modes) \
                and lang_canonical != "python":
            shell_replacements = {
                "python3 .peers/checks/coverage_3class.py src tests":
                    ".peers/checks/coverage_3class.sh",
                "python3 .peers/checks/scan_secrets.py .":
                    ".peers/checks/scan_secrets.sh .",
                "python3 .peers/checks/deps_justified.py .":
                    ".peers/checks/deps_justified.sh .",
                "python3 .peers/checks/api_stable.py .":
                    ".peers/checks/api_stable.sh .",
                "python3 .peers/checks/no_regression.py .":
                    ".peers/checks/no_regression.sh .",
                "python3 .peers/checks/diff_size_per_resolve.py .":
                    ".peers/checks/diff_size_per_resolve.sh .",
            }
            if lang_canonical == "js":
                replacements = dict(shell_replacements)
                replacements[
                    "python3 .peers/checks/coverage_3class.py src tests"
                ] = "node .peers/checks/coverage_3class.js"
            else:
                replacements = shell_replacements
            for old, new in replacements.items():
                goals_text = goals_text.replace(old, new)
        write_text_no_symlink(peers / "goals.yaml", goals_text)
        # Copy check scripts (consistent with the legacy audit-templates
        # path which used the same pattern).
        (peers / "checks").mkdir(exist_ok=True)
        for src_check in check_files:
            dst = peers / "checks" / src_check.name
            dst.write_bytes(src_check.read_bytes())
            dst.chmod(0o755)
        # Audit trail: one line per mode with iso-timestamp, name,
        # version, and sha256 of its mode.yaml.
        import datetime
        import hashlib
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        lines = []
        for m in resolved_modes:
            mode_yaml_sha = hashlib.sha256(
                (m.path / "mode.yaml").read_bytes()
            ).hexdigest()
            lines.append(
                f"{ts}  {m.name:<15}  v{m.version}  sha256={mode_yaml_sha}"
            )
        write_text_no_symlink(
            peers / "modes-applied.txt", "\n".join(lines) + "\n"
        )

    # G10: tag the target's current HEAD so a human can always roll
    # back to "before peers touched this". Surface the absence so the
    # user knows the rollback anchor isn't available.
    try:
        subprocess.run(
            ["git", "tag", "-f", "peers-baseline"],
            cwd=target, check=True, capture_output=True,
        )
    except subprocess.CalledProcessError:
        print(
            "peers: note: peers-baseline tag NOT set "
            "(not a git repo, or no commits yet). "
            "Rollback anchor unavailable; consider `git init` + an "
            "initial commit before `peers run`.",
            file=sys.stderr,
        )

    # G7: snapshot the goals.yaml hash so future ticks can detect that
    # someone (peer or otherwise) mutated the goals mid-run.
    import hashlib
    h = hashlib.sha256((peers / "goals.yaml").read_bytes()).hexdigest()
    write_text_no_symlink(peers / "goals.sha256", h + "\n")

    # Ensure the target's .gitignore excludes the .peers/ control plane —
    # otherwise `git status --porcelain` would always show `.peers/` as
    # untracked, polluting `dirty_worktree` detection in every tick.
    # M1: also commit the change (with Peer: peers-init trailer so the
    # substrate's new_commits_by filter doesn't conflate it with peer
    # work) so the worktree is clean immediately after init.
    needed = ".peers/"
    existing = gitignore.read_text() if gitignore.exists() else ""
    has_entry = any(
        line.strip().rstrip("/") == ".peers"
        for line in existing.splitlines()
    )
    if not has_entry:
        prefix = "" if existing.endswith("\n") or not existing else "\n"
        write_text_no_symlink(gitignore, existing + prefix + needed + "\n")
        # Commit the .gitignore touch (and ONLY that file) so dirty_worktree
        # isn't True on tick 0. Skip silently if not a git repo / no commits
        # yet (subprocess error path).
        try:
            subprocess.run(
                ["git", "add", ".gitignore"],
                cwd=target, check=True, capture_output=True,
            )
            subprocess.run(
                ["git",
                 "-c", "user.email=peers-init@local",
                 "-c", "user.name=peers-init",
                 "commit", "-m",
                 "peers init: add .peers/ to .gitignore\n\n"
                 "Peer: peers-init\n"],
                cwd=target, check=True, capture_output=True,
            )
        except subprocess.CalledProcessError:
            pass

    # G2: drop hook snippets when driver=hooks selected.
    if driver == "hooks":
        _maybe_write_hooks(target, peers)
        # Update default config to declare driver: hooks.
        # the original implementation did
        # `cfg_text.replace("driver: orchestrator", "driver: hooks")`,
        # which silently no-op'd if the template was reformatted (e.g.
        # `driver: "orchestrator"`, line-wrapped, default changed).
        # Use a line-level regex anchored on the key so format drift
        # in unrelated parts of the template can't break the switch.
        cfg_path = peers / "config.yaml"
        cfg_text = cfg_path.read_text()
        new_text, n_subs = re.subn(
            r"(?m)^(\s*driver\s*:\s*)([\"']?)orchestrator\2",
            r"\1\2hooks\2",
            cfg_text,
        )
        if n_subs == 0:
            print(
                "peers init: warning: could not switch driver to "
                "'hooks' in .peers/config.yaml automatically — the "
                "template's `driver: orchestrator` line was not found. "
                "Edit the file manually and set `driver: hooks`.",
                file=sys.stderr,
            )
        else:
            write_text_no_symlink(cfg_path, new_text)

    print(f"Initialized peers control plane in {peers}")
    if driver == "hooks":
        print(
            f"Driver=hooks: hook snippets written to "
            f"{peers / 'hooks'}; install them per the README there."
        )
        if install_hooks:
            rc = _install_host_hooks(target)
            if rc != 0:
                print(
                    "hooks --install: nothing was patched. Edit your "
                    "~/.claude/settings.json and ~/.codex/config.toml "
                    "manually using the snippets in "
                    f"{peers / 'hooks'}.",
                    file=sys.stderr,
                )
                # init succeeded; only the auto-install failed.
    elif install_hooks:
        print(
            "--install only applies with --driver=hooks; ignoring.",
            file=sys.stderr,
        )
    return 0


def cmd_status(target: Path) -> int:
    """L7: route through StateStore.load so v1 state is migrated to
    v2 in-memory before display (consistent shape across all
    invocations)."""
    from peers.state_store import StateStore
    target = Path(target)
    peer_dir = target / ".peers"
    err = _refuse_symlink_control_dir(peer_dir)
    if err is not None:
        print(err, file=sys.stderr)
        return 1
    state_path = peer_dir / "state.json"
    if not state_path.exists():
        print("no state yet — run `peers run` first", file=sys.stderr)
        return 1
    try:
        state = StateStore(state_path).load()
    except (RuntimeError, OSError) as e:
        print(f"state file corrupt: {e}", file=sys.stderr)
        return 1

    lock_path = peer_dir / "run.lock"
    halted_path = peer_dir / "HALTED.md"
    log_path = peer_dir / "log" / "runs.jsonl"

    print(f"Iteration: {state.get('iteration', 0)}")
    # After StateStore.load migration, the schema is always v2.
    try:
        print(f"Whose turn next: "
              f"{state['peer_order'][state['turn_index']]}")
    except (IndexError, TypeError, KeyError):
        print("Whose turn next: <invalid turn_index>")
    if lock_path.exists():
        held = _lock_file_held(lock_path)
        try:
            pid_raw = read_text_no_symlink(lock_path).strip()
        except OSError:
            pid_raw = ""
        # L4: only display PID if it's a valid integer.
        if held is True and pid_raw.isdigit():
            print(f"Lock held: pid {pid_raw} ({lock_path})")
        elif held is False:
            msg = f"Lock file present but not locked — stale ({lock_path})"
            if pid_raw:
                msg += f"; content={pid_raw!r}"
            print(msg)
        elif pid_raw:
            print(f"Lock file present but content is not a PID "
                  f"({pid_raw!r}) — possibly stale ({lock_path})")
        else:
            print(f"Lock file present but empty — possibly stale "
                  f"({lock_path})")
    if halted_path.exists():
        print(f"HALTED — see {halted_path}")
    if state.get("dirty_worktree"):
        print("WARNING: working tree has uncommitted changes "
              "(previous peer didn't commit)")

    b = state.get("budget", {})
    if b:
        pct_i = 100 * b.get("spent_iterations", 0) / max(b.get("max_iterations", 1), 1)
        pct_r = 100 * b.get("spent_runtime_s", 0) / max(b.get("max_runtime_s", 1), 1)
        wasted = b.get("wasted_runtime_s", 0)
        print(
            f"Budget: iterations {b.get('spent_iterations', 0)}/"
            f"{b.get('max_iterations', '?')} ({pct_i:.0f}%), "
            f"runtime {b.get('spent_runtime_s', 0)}s/"
            f"{b.get('max_runtime_s', '?')}s ({pct_r:.0f}%)"
            + (f", wasted {wasted}s" if wasted else "")
        )
        tokens = b.get("spent_tokens", 0)
        usd = b.get("spent_usd", 0.0)
        if tokens or usd:
            print(f"Cost: {tokens} tokens, ${usd:.4f}")

    print("Goals:")
    for gid, info in state.get("goals_status", {}).items():
        diag = f" — {info.get('diagnostic')}" if info.get("diagnostic") else ""
        print(f"  {gid}: {info.get('state')}{diag}")

    # Schema v2 uses "peers"; fall back to legacy "tools".
    peers_map = state.get("peers") or state.get("tools") or {}
    print("Peers:")
    for name, info in peers_map.items():
        last = info.get("last_run", {})
        last_str = (f" (last: {last.get('classification')}, "
                    f"{last.get('duration_ms', 0)} ms)") if last else ""
        rf = info.get("recent_fails", 0)
        print(f"  {name}: {info.get('state')}{last_str}"
              + (f", recent_fails={rf}" if rf else ""))

    warnings = state.get("warnings") or []
    if warnings:
        print("Warnings:")
        for w in warnings[-5:]:
            print(f"  - {w}")

    if log_path.exists():
        try:
            with open_text_read_no_symlink(log_path) as f:
                n = sum(1 for _ in f)
            print(f"Run log: {n} entries ({log_path})")
        except OSError:
            pass

    return 0


def _refuse_symlink_write_target(path: Path) -> str | None:
    if path.is_symlink():
        try:
            target = path.readlink()
        except OSError:
            target = "<unreadable>"
        return (
            f"refusing to write {path}: it is a symlink to {target}. "
            "Remove it manually first."
        )
    return None


def _refuse_symlink_control_dir(peer_dir: Path) -> str | None:
    if peer_dir.is_symlink():
        try:
            target = peer_dir.readlink()
        except OSError:
            target = "<unreadable>"
        return (
            f"refusing to operate on {peer_dir}: it is a symlink to "
            f"{target}. Remove it manually first."
        )
    return None


def _lock_file_held(lock_path: Path) -> bool | None:
    """Best-effort flock probe for status output.

    Existence alone does not mean the lock is held: older peers
    versions left stale files behind, and current versions remove the
    file best-effort only after releasing the flock.
    """
    import fcntl

    try:
        with open_text_no_symlink(lock_path, "a") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            return False
    except OSError:
        return None


def _positive_int_config(value: object, field: str) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return (
            f"`{field}` must be a positive integer, got "
            f"{type(value).__name__} ({value!r})"
        )
    if value <= 0:
        return f"`{field}` must be positive, got {value}"
    return None


def _positive_number_config(value: object, field: str) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return (
            f"`{field}` must be a positive number, got "
            f"{type(value).__name__} ({value!r})"
        )
    if not math.isfinite(float(value)):
        return f"`{field}` must be finite, got {value!r}"
    if value <= 0:
        return f"`{field}` must be positive, got {value}"
    return None


def _load_config_yaml(cfg_path: Path) -> dict:
    """Load `.peers/config.yaml` with the same no-follow + size guard
    used by state/goals/registry files.
    """
    raw = read_bytes_no_symlink(
        cfg_path, max_bytes=_CONFIG_YAML_MAX_BYTES + 1
    )
    if len(raw) > _CONFIG_YAML_MAX_BYTES:
        raise ValueError(
            f"{cfg_path}: config.yaml too large "
            f"(max {_CONFIG_YAML_MAX_BYTES} bytes)"
        )
    try:
        cfg = yaml.safe_load(raw.decode("utf-8", errors="replace"))
    except yaml.YAMLError as e:
        raise ValueError(f"{cfg_path}: invalid YAML: {e}") from e
    if cfg is None:
        cfg = {}
    if not isinstance(cfg, dict):
        raise ValueError(f"{cfg_path}: top-level value must be a mapping")
    return cfg


def _validate_config(cfg: object, cfg_path: Path) -> str | None:
    """Returns an error message string, or None if cfg is valid."""
    if not isinstance(cfg, dict):
        return f"{cfg_path}: top-level value must be a mapping"
    # Defer peer/tools validation to load_peer_specs; surface its
    # ValueError as a config error.
    try:
        specs = load_peer_specs(cfg)
    except ValueError as e:
        return f"{cfg_path}: {e}"
    if len(specs) < 2:
        return f"{cfg_path}: need at least 2 peers, got {len(specs)}"
    comm = cfg.get("comm", "git")
    if comm not in ("git", "hybrid"):
        return f"{cfg_path}: `comm` must be 'git' or 'hybrid', got {comm!r}"
    health = cfg.get("health")
    if not isinstance(health, dict):
        return f"{cfg_path}: `health` must be a mapping"
    if "idle_timeout_s" not in health and "absolute_max_runtime_s" not in health:
        return (
            f"{cfg_path}: `health.idle_timeout_s` and/or "
            "`health.absolute_max_runtime_s` is required"
        )
    for key in ("idle_timeout_s", "absolute_max_runtime_s"):
        if key in health:
            err = _positive_int_config(health[key], f"health.{key}")
            if err is not None:
                return f"{cfg_path}: {err}"
    if "buf_cap_bytes" in health:
        err = _positive_int_config(
            health["buf_cap_bytes"], "health.buf_cap_bytes"
        )
        if err is not None:
            return f"{cfg_path}: {err}"
    # validate `health.error_patterns`
    # regexes at config-load time, not at first HealthGuard.invoke.
    # An unbalanced bracket etc. was previously a re.error traceback
    # on the FIRST peer tick (after $$ already spent), denying the
    # loop forever.
    error_patterns = health.get("error_patterns", [])
    if error_patterns is not None and not isinstance(error_patterns, list):
        return (
            f"{cfg_path}: `health.error_patterns` must be a list, "
            f"got {type(error_patterns).__name__}"
        )
    if len(error_patterns or []) > _ERROR_PATTERNS_MAX_COUNT:
        return (
            f"{cfg_path}: `health.error_patterns` may contain at most "
            f"{_ERROR_PATTERNS_MAX_COUNT} entries"
        )
    for i, pat in enumerate(error_patterns or []):
        if not isinstance(pat, str):
            return (
                f"{cfg_path}: health.error_patterns[{i}] must be a "
                f"string, got {type(pat).__name__}"
            )
        if len(pat.encode("utf-8", errors="replace")) > _ERROR_PATTERN_MAX_BYTES:
            return (
                f"{cfg_path}: health.error_patterns[{i}] is too large "
                f"(max {_ERROR_PATTERN_MAX_BYTES} bytes)"
            )
        try:
            re.compile(pat)
        except re.error as e:
            return (
                f"{cfg_path}: health.error_patterns[{i}]={pat!r} is "
                f"not a valid regex: {e}"
            )
    # (post-2026-05-24): same validation rules apply to
    # `health.halt_patterns` — same shape, same caps, same regex check.
    # Default to empty so legacy configs without the field stay valid.
    halt_patterns = health.get("halt_patterns", [])
    if halt_patterns is not None and not isinstance(halt_patterns, list):
        return (
            f"{cfg_path}: `health.halt_patterns` must be a list, "
            f"got {type(halt_patterns).__name__}"
        )
    if len(halt_patterns or []) > _ERROR_PATTERNS_MAX_COUNT:
        return (
            f"{cfg_path}: `health.halt_patterns` may contain at most "
            f"{_ERROR_PATTERNS_MAX_COUNT} entries"
        )
    for i, pat in enumerate(halt_patterns or []):
        if not isinstance(pat, str):
            return (
                f"{cfg_path}: health.halt_patterns[{i}] must be a "
                f"string, got {type(pat).__name__}"
            )
        if len(pat.encode("utf-8", errors="replace")) > _ERROR_PATTERN_MAX_BYTES:
            return (
                f"{cfg_path}: health.halt_patterns[{i}] is too large "
                f"(max {_ERROR_PATTERN_MAX_BYTES} bytes)"
            )
        try:
            re.compile(pat)
        except re.error as e:
            return (
                f"{cfg_path}: health.halt_patterns[{i}]={pat!r} is "
                f"not a valid regex: {e}"
            )
    budget = cfg.get("budget")
    if budget is not None:
        if not isinstance(budget, dict):
            return f"{cfg_path}: `budget` must be a mapping"
        for key in (
            "max_iterations", "max_runtime_s", "max_consecutive_failures",
            "max_tokens",
        ):
            if key in budget and budget[key] is not None:
                err = _positive_int_config(budget[key], f"budget.{key}")
                if err is not None:
                    return f"{cfg_path}: {err}"
        if "max_usd" in budget and budget["max_usd"] is not None:
            err = _positive_number_config(budget["max_usd"], "budget.max_usd")
            if err is not None:
                return f"{cfg_path}: {err}"
        if "max_usd_mode" in budget and budget["max_usd_mode"] is not None:
            mode = budget["max_usd_mode"]
            if not isinstance(mode, str) or mode.lower() not in (
                "auto", "hard", "warn", "off",
            ):
                return (
                    f"{cfg_path}: `budget.max_usd_mode` must be one of "
                    "'auto', 'hard', 'warn', 'off', got {mode!r}"
                )
    # validate optional goals.timeout_s. Reject bool BEFORE
    # int (bool is a subclass of int → `int(True) == 1` would silently
    # become a 1-second goal timeout) and reject float (`int(60.9) ==
    # 60` silently truncates). Strict isinstance == int.
    goals_cfg = cfg.get("goals")
    if goals_cfg is not None:
        if not isinstance(goals_cfg, dict):
            return f"{cfg_path}: `goals` must be a mapping"
        ts = goals_cfg.get("timeout_s")
        if ts is not None:
            err = _positive_int_config(ts, "goals.timeout_s")
            if err is not None:
                return f"{cfg_path}: {err}"
    return None


def cmd_info(target: Path) -> int:
    """I3: dump the current `.peers/` configuration (driver, peers,
    budget, goals) to stdout. No subprocess invocation; useful for
    sanity-checking a fresh init or comparing across projects."""
    target = Path(target)
    peer_dir = target / ".peers"
    err = _refuse_symlink_control_dir(peer_dir)
    if err is not None:
        print(err, file=sys.stderr)
        return 1
    cfg_path = target / ".peers" / "config.yaml"
    goals_path = target / ".peers" / "goals.yaml"
    if not cfg_path.exists():
        print(f"missing {cfg_path}; run `peers init` first",
              file=sys.stderr)
        return 1
    try:
        cfg = _load_config_yaml(cfg_path)
    except ValueError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"config error: cannot read {cfg_path}: {e}", file=sys.stderr)
        return 1
    err = _validate_config(cfg, cfg_path)
    if err is not None:
        print(f"config error: {err}", file=sys.stderr)
        return 1
    try:
        peer_specs = load_peer_specs(cfg)
    except ValueError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    try:
        goals = load_goals(goals_path)
    except (ValueError, OSError) as e:
        print(f"goals error: {e}", file=sys.stderr)
        return 1
    print(f"target:  {target.resolve()}")
    print(f"driver:  {cfg.get('driver', 'orchestrator')}")
    print(f"comm:    {cfg.get('comm', 'git')}")
    print(f"peers:   {len(peer_specs)}")
    for s in peer_specs:
        print(f"  - {s.name} (tool={s.tool}, prompt_mode={s.prompt_mode})")
    b = cfg.get("budget", {}) or {}
    from peers.billing import resolve_max_usd_mode
    effective_usd_mode, usd_mode_reason = resolve_max_usd_mode(
        b.get("max_usd_mode"),
        [s.tool for s in peer_specs],
    )
    print(
        f"budget:  iterations≤{b.get('max_iterations', '?')}, "
        f"runtime≤{b.get('max_runtime_s', '?')}s"
        + (f", tokens≤{b.get('max_tokens')}"
           if b.get('max_tokens') is not None else "")
        + (f", USD≤${b.get('max_usd')}"
           if b.get('max_usd') is not None else "")
    )
    if b.get('max_usd') is not None:
        print(f"  max_usd_mode={effective_usd_mode} ({usd_mode_reason})")
    h = cfg.get("health", {}) or {}
    print(
        f"health:  idle≤{h.get('idle_timeout_s', '?')}s, "
        f"abs≤{h.get('absolute_max_runtime_s', '?')}s, "
        f"buf_cap={h.get('buf_cap_bytes', 2*1024*1024)} bytes"
    )
    hard = [g for g in goals if g.type == "hard"]
    soft = [g for g in goals if g.type == "soft"]
    print(f"goals:   {len(goals)} ({len(hard)} hard, {len(soft)} soft)")
    for g in hard:
        print(f"  - hard: {g.id}")
    for g in soft:
        print(f"  - soft: {g.id} "
              f"(reviewer={g.reviewer}, consensus_needed={g.consensus_needed}"
              + (f", quorum={g.quorum_num}/{g.quorum_den}"
                 if g.quorum_num else "")
              + ")")
    return 0


def _verify_load_config_and_goals(peer_dir: Path) -> tuple[dict, list, int, list] | int:
    from peers.goals import load_goals

    cfg_path = peer_dir / "config.yaml"
    goals_path = peer_dir / "goals.yaml"
    if not cfg_path.exists():
        print(f"missing {cfg_path}; run `peers init` first", file=sys.stderr)
        return 1
    try:
        cfg = _load_config_yaml(cfg_path)
    except ValueError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"config error: cannot read {cfg_path}: {e}", file=sys.stderr)
        return 1
    try:
        goals = load_goals(goals_path)
    except (ValueError, OSError) as e:
        print(f"goals error: {e}", file=sys.stderr)
        return 1

    verify_cfg_raw = cfg.get("verify")
    if verify_cfg_raw is None:
        verify_cfg: dict = {}
    elif isinstance(verify_cfg_raw, dict):
        verify_cfg = verify_cfg_raw
    else:
        print(
            f"config error: `verify:` must be a mapping, got "
            f"{type(verify_cfg_raw).__name__}",
            file=sys.stderr,
        )
        return 1
    goals_cfg_raw = cfg.get("goals")
    if goals_cfg_raw is None:
        goals_cfg: dict = {}
    elif isinstance(goals_cfg_raw, dict):
        goals_cfg = goals_cfg_raw
    else:
        print(
            f"config error: `goals:` must be a mapping, got "
            f"{type(goals_cfg_raw).__name__}",
            file=sys.stderr,
        )
        return 1
    goals_timeout_raw = goals_cfg.get("timeout_s", 120)
    err = _positive_int_config(goals_timeout_raw, "goals.timeout_s")
    if err is not None:
        print(f"config error: {err}", file=sys.stderr)
        return 1
    default_timeout_raw = verify_cfg.get("timeout_s", goals_timeout_raw)
    err = _positive_int_config(default_timeout_raw, "verify.timeout_s")
    if err is not None:
        print(f"config error: {err}", file=sys.stderr)
        return 1
    extra_commands_raw = verify_cfg.get("commands")
    if extra_commands_raw is None:
        extra_commands: list = []
    elif isinstance(extra_commands_raw, list):
        extra_commands = extra_commands_raw
    else:
        print(
            f"config error: `verify.commands:` must be a list, got "
            f"{type(extra_commands_raw).__name__}",
            file=sys.stderr,
        )
        return 1
    return cfg, goals, int(default_timeout_raw), extra_commands


def _verify_run_extra_commands(
    target: Path, extra_commands: list, default_timeout: int
) -> list[dict]:
    from peers.goal_engine import _run_goal_cmd
    import subprocess

    extra_results: list[dict] = []
    for spec in extra_commands:
        if not isinstance(spec, dict):
            extra_results.append({
                "name": "<invalid>", "state": "fail",
                "diagnostic": f"verify.commands entry must be a mapping, "
                              f"got {type(spec).__name__}",
                "duration_ms": 0,
            })
            continue
        name = str(spec.get("name") or spec.get("cmd") or "<unnamed>")
        cmd = spec.get("cmd")
        if not isinstance(cmd, str) or not cmd.strip():
            extra_results.append({
                "name": name, "state": "fail",
                "diagnostic": "missing or non-string `cmd`", "duration_ms": 0,
            })
            continue
        timeout_raw = spec.get("timeout_s", default_timeout)
        err = _positive_int_config(timeout_raw, f"verify.commands.{name}.timeout_s")
        if err is not None:
            extra_results.append({
                "name": name, "state": "fail",
                "diagnostic": err, "duration_ms": 0,
            })
            continue
        timeout_s = int(timeout_raw)
        t0 = time.monotonic()
        try:
            proc = _run_goal_cmd(cmd, target, timeout_s)
        except subprocess.TimeoutExpired as e:
            dur = int((time.monotonic() - t0) * 1000)
            tail = (e.stderr or "")[-400:] if isinstance(e.stderr, str) else ""
            extra_results.append({
                "name": name, "state": "fail",
                "diagnostic": f"timeout after {timeout_s}s; stderr-tail={tail!r}",
                "duration_ms": dur,
            })
            continue
        dur = int((time.monotonic() - t0) * 1000)
        state = "pass" if proc.returncode == 0 else "fail"
        diagnostic = "" if proc.returncode == 0 else (
            f"exit={proc.returncode}; stderr-tail={(proc.stderr or '')[-400:]!r}"
        )
        extra_results.append({
            "name": name, "state": state,
            "diagnostic": diagnostic, "duration_ms": dur,
        })
    return extra_results


def _verify_render_md(target: Path, hard_results: dict, extra_results: list[dict]
                      ) -> tuple[str, list[str], bool]:
    n_hard_pass = sum(1 for r in hard_results.values() if r.state == "pass")
    n_extra_pass = sum(1 for r in extra_results if r["state"] == "pass")
    n_hard = len(hard_results)
    n_extra = len(extra_results)
    all_green = (n_hard_pass == n_hard) and (n_extra_pass == n_extra)

    out: list[str] = [
        f"# peers verify — {target.name}",
        "",
        f"**Result:** {'PASS' if all_green else 'FAIL'} "
        f"(hard {n_hard_pass}/{n_hard}, verify {n_extra_pass}/{n_extra})",
        "",
    ]
    if hard_results:
        out += ["## Hard goals", "", "| id | state | duration (ms) | diagnostic |",
                "|---|---|---|---|"]
        for gid, result in hard_results.items():
            d = result.diagnostic.replace("|", "\\|").replace("\n", " ")[:200]
            out.append(f"| `{gid}` | {result.state} | {result.duration_ms} | {d} |")
        out.append("")
    if extra_results:
        out += ["## Verify commands", "",
                "| name | state | duration (ms) | diagnostic |",
                "|---|---|---|---|"]
        for result in extra_results:
            d = result["diagnostic"].replace("|", "\\|").replace("\n", " ")[:200]
            out.append(
                f"| `{result['name']}` | {result['state']} | "
                f"{result['duration_ms']} | {d} |"
            )
        out.append("")
    if not hard_results and not extra_results:
        out.append("_No hard goals and no `verify.commands` configured — "
                   "nothing to check._")
    return "\n".join(out) + "\n", out, all_green


def cmd_verify(target: Path, write_md: bool = True) -> int:
    """Run every HARD goal and user-declared `verify.commands`."""
    from peers.goal_engine import GoalEngine

    target = Path(target)
    peer_dir = target / ".peers"
    err = _refuse_symlink_control_dir(peer_dir)
    if err is not None:
        print(err, file=sys.stderr)
        return 1
    loaded = _verify_load_config_and_goals(peer_dir)
    if isinstance(loaded, int):
        return loaded
    _cfg, goals, default_timeout, extra_commands = loaded
    engine = GoalEngine(goals, cwd=target, timeout_s=default_timeout)
    hard_results = engine.evaluate_hard_gates()
    extra_results = _verify_run_extra_commands(target, extra_commands, default_timeout)
    md, out, all_green = _verify_render_md(target, hard_results, extra_results)
    if write_md:
        peer_dir.mkdir(exist_ok=True)
        verify_path = peer_dir / "VERIFY.md"
        err = _refuse_symlink_write_target(verify_path)
        if err is not None:
            print(err, file=sys.stderr)
            return 1
        try:
            write_text_no_symlink(verify_path, md)
        except OSError as e:
            print(f"cannot write {verify_path}: {e}", file=sys.stderr)
            return 1
    for line in out:
        print(line)
    return 0 if all_green else 1


def _report_load_state(pd: Path) -> dict | int:
    state_path = pd / "state.json"
    if not state_path.exists():
        print(f"no state at {state_path}; nothing to report", file=sys.stderr)
        return 1
    from peers.state_store import StateStore
    try:
        state = StateStore(state_path).load()
    except (RuntimeError, OSError) as e:
        print(f"state file corrupt: {e}", file=sys.stderr)
        return 1
    if not isinstance(state, dict):
        print(
            f"state file corrupt: {state_path}: top-level value is not "
            f"an object ({type(state).__name__})",
            file=sys.stderr,
        )
        return 1
    return state


def _report_load_log_entries(log_path: Path) -> list[dict] | int:
    log_entries: list[dict] = []
    skipped_log_lines = 0
    if not log_path.exists():
        return log_entries
    try:
        log_fp = open_text_read_no_symlink(log_path)
    except OSError as e:
        print(f"cannot read run log {log_path}: {e}", file=sys.stderr)
        return 1
    with log_fp:
        for line_no, line in enumerate(log_fp, start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                skipped_log_lines += 1
                continue
            if not isinstance(entry, dict):
                skipped_log_lines += 1
                print(
                    f"peers report: warning: skipped non-object JSONL "
                    f"entry at {log_path}:{line_no}",
                    file=sys.stderr,
                )
                continue
            log_entries.append(entry)
    if skipped_log_lines:
        print(
            f"peers report: warning: skipped {skipped_log_lines} malformed "
            f"runs.jsonl line(s)",
            file=sys.stderr,
        )
    return log_entries


def _report_render_tick_history(out: list[str], log_entries: list[dict]) -> None:
    tick_entries = [e for e in log_entries if e.get("event") != "exit"]
    if not tick_entries:
        return
    out += ["", f"## Tick history ({len(tick_entries)} entries)", "",
            "| iter | peer | tool | success | cls | dur(ms) | tokens | usd |",
            "|---|---|---|---|---|---|---|---|"]
    for entry in tick_entries[-50:]:
        try:
            usd_this_tick = float(entry.get("usd_this_tick", 0))
        except (TypeError, ValueError):
            usd_this_tick = 0.0
        out.append(
            f"| {entry.get('iteration')} | {entry.get('peer')} | "
            f"{entry.get('tool')} | {entry.get('success')} | "
            f"{entry.get('classification')} | {entry.get('duration_ms')} | "
            f"{entry.get('tokens_this_tick', 0)} | ${usd_this_tick:.4f} |"
        )


def _report_render_exit_events(out: list[str], log_entries: list[dict]) -> None:
    exit_entries = [e for e in log_entries if e.get("event") == "exit"]
    if not exit_entries:
        return
    out += ["", "### Run termination events", ""]
    for entry in exit_entries:
        out.append(
            f"- {entry.get('ts', '?')} — reason: **{entry.get('reason')}** "
            f"(ticks in run: {entry.get('ticks_in_run', '?')})"
        )


def _report_render(target: Path, pd: Path, state: dict,
                   log_entries: list[dict]) -> list[str]:
    out: list[str] = []
    out.append(f"# peers report — {target.name}")
    out.append("")
    out.append(f"- iterations: {state.get('iteration', 0)}")
    order = state.get("peer_order", [])
    idx = state.get("turn_index", 0)
    if order and 0 <= idx < len(order):
        out.append(f"- next-up peer: `{order[idx]}`")
    out.append(f"- peer_order: {order}")
    if (pd / "HALTED.md").exists():
        out.append("- **HALTED** — see `.peers/HALTED.md`")

    out += ["", "## Goals", "", "| id | state | diagnostic |", "|---|---|---|"]
    for gid, info in (state.get("goals_status") or {}).items():
        diagnostic = (info.get("diagnostic") or "").replace("|", "\\|")[:80]
        out.append(f"| `{gid}` | {info.get('state')} | {diagnostic} |")
    soft = state.get("soft_status", {}) or {}
    if soft:
        out += ["", "### Soft-review consensus", ""]
        for gid, sg in soft.items():
            out.append(
                f"- `{gid}`: consensus_count={sg.get('consensus_count', 0)}, "
                f"last_pass={sg.get('last_pass')}"
            )

    b = state.get("budget", {}) or {}
    if b:
        out += ["", "## Budget", ""]
        out.append(f"- iterations: {b.get('spent_iterations', 0)} / "
                   f"{b.get('max_iterations', '?')}")
        out.append(f"- runtime_s: {b.get('spent_runtime_s', 0)} / "
                   f"{b.get('max_runtime_s', '?')}"
                   + (f" (wasted {b.get('wasted_runtime_s', 0)}s)"
                      if b.get('wasted_runtime_s') else ""))
        tokens = b.get("spent_tokens", 0)
        usd = b.get("spent_usd", 0.0)
        if tokens or usd:
            out.append(f"- tokens: {tokens}")
            out.append(f"- USD: ${usd:.4f}")

    out += ["", "## Peers", "",
            "| name | state | consecutive_fails | recent_fails | failed_cheating |",
            "|---|---|---|---|---|"]
    for name, info in (state.get("peers") or {}).items():
        out.append(
            f"| `{name}` | {info.get('state')} | "
            f"{info.get('consecutive_fails', 0)} | "
            f"{info.get('recent_fails', 0)} | "
            f"{info.get('failed_cheating', 0)} |"
        )

    _report_render_tick_history(out, log_entries)
    _report_render_exit_events(out, log_entries)

    wh = state.get("warnings_history") or []
    if wh:
        out += ["", f"## Warnings (last {min(len(wh), 20)} of {len(wh)})", ""]
        for w in wh[-20:]:
            out.append(f"- iter {w.get('iter')}: {w.get('w', '')}")
    return out


def cmd_report(target: Path) -> int:
    """Write a human-readable Markdown summary to `.peers/REPORT.md`."""
    target = Path(target)
    pd = target / ".peers"
    err = _refuse_symlink_control_dir(pd)
    if err is not None:
        print(err, file=sys.stderr)
        return 1
    state = _report_load_state(pd)
    if isinstance(state, int):
        return state
    log_entries = _report_load_log_entries(pd / "log" / "runs.jsonl")
    if isinstance(log_entries, int):
        return log_entries
    out = _report_render(target, pd, state, log_entries)
    report_path = pd / "REPORT.md"
    err = _refuse_symlink_write_target(report_path)
    if err is not None:
        print(err, file=sys.stderr)
        return 1
    try:
        write_text_no_symlink(report_path, "\n".join(out) + "\n")
    except OSError as e:
        print(f"cannot write {report_path}: {e}", file=sys.stderr)
        return 1
    print(f"wrote {report_path}")
    return 0


def cmd_replay(target: Path, iteration: int) -> int:
    """G12: reconstruct what happened at a given iteration by reading
    the run log. Prints the matching log entry / entries plus the
    git log range that turn covered."""
    target = Path(target)
    peer_dir = target / ".peers"
    err = _refuse_symlink_control_dir(peer_dir)
    if err is not None:
        print(err, file=sys.stderr)
        return 1
    log_path = target / ".peers" / "log" / "runs.jsonl"
    if not log_path.exists():
        print(f"no run log at {log_path}", file=sys.stderr)
        return 1
    matches: list[dict] = []
    try:
        log_fp = open_text_read_no_symlink(log_path)
    except OSError as e:
        print(f"cannot read run log {log_path}: {e}", file=sys.stderr)
        return 1
    skipped_log_lines = 0
    with log_fp:
        for line_no, line in enumerate(log_fp, start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                skipped_log_lines += 1
                continue
            if not isinstance(entry, dict):
                skipped_log_lines += 1
                print(
                    f"peers replay: warning: skipped non-object JSONL entry "
                    f"at {log_path}:{line_no}",
                    file=sys.stderr,
                )
                continue
            if entry.get("iteration") == iteration:
                matches.append(entry)
    if skipped_log_lines:
        print(
            f"peers replay: warning: skipped {skipped_log_lines} malformed "
            f"runs.jsonl line(s)",
            file=sys.stderr,
        )
    if not matches:
        print(f"no log entry for iteration {iteration}", file=sys.stderr)
        return 1
    for entry in matches:
        print(json.dumps(entry, indent=2, sort_keys=True))
    return 0


def cmd_run(target: Path, max_ticks: int | None,
            dry_run: bool = False,
            max_usd: float | None = None,
            verbose: bool = False,
            without_recon: bool = False,
            without_post_convergence_skeptic: bool = False) -> int:
    target = Path(target)
    peer_dir = target / ".peers"
    err = _refuse_symlink_control_dir(peer_dir)
    if err is not None:
        print(err, file=sys.stderr)
        return 1
    cfg_path = peer_dir / "config.yaml"
    goals_path = peer_dir / "goals.yaml"
    if not cfg_path.exists():
        print("missing .peers/config.yaml — run `peers init`", file=sys.stderr)
        return 1
    # cmd_info + cmd_verify both print
    # `config error: ...` on bad YAML, but cmd_run dumped a raw
    # traceback. Mirror their handling so the UX is consistent.
    try:
        cfg = _load_config_yaml(cfg_path)
    except ValueError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"config error: cannot read {cfg_path}: {e}", file=sys.stderr)
        return 1
    err = _validate_config(cfg, cfg_path)
    if err is not None:
        print(f"config error: {err}", file=sys.stderr)
        return 1
    if max_ticks is not None:
        err = _positive_int_config(max_ticks, "--max-ticks")
        if err is not None:
            print(f"config error: {err}", file=sys.stderr)
            return 1
    if max_usd is not None:
        err = _positive_number_config(max_usd, "--max-usd")
        if err is not None:
            print(f"config error: {err}", file=sys.stderr)
            return 1
    try:
        goals = load_goals(goals_path)
    except (ValueError, OSError) as e:
        print(f"goals error: {e}", file=sys.stderr)
        return 1
    peer_specs = load_peer_specs(cfg)
    health = cfg["health"]
    goals_cfg = cfg.get("goals", {}) or {}
    cfg_budget = dict(cfg.get("budget", {}) or {})
    if max_usd is not None:
        cfg_budget["max_usd"] = max_usd
    driver = OrchestratorDriver(
        repo=target,
        peer_dir=target / ".peers",
        goals=goals,
        peer_specs=peer_specs,
        idle_timeout_s=health.get("idle_timeout_s", 15 * 60),
        absolute_max_runtime_s=health.get("absolute_max_runtime_s", 2 * 3600),
        error_patterns=health.get("error_patterns", []),
        halt_patterns=health.get("halt_patterns", []),
        cfg_budget=cfg_budget,
        dry_run=dry_run,
        comm_variant=cfg.get("comm", "git"),
        buf_cap_bytes=int(health.get("buf_cap_bytes", 2 * 1024 * 1024)),
        goals_timeout_s=int(goals_cfg.get("timeout_s", 120)),
        verbose=verbose,
        recon_enabled=not without_recon,
        auto_skeptic_enabled=not without_post_convergence_skeptic,
    )
    result = driver.run(max_ticks=max_ticks)
    print(f"Stopped: {result['reason']}")
    return 0 if result["reason"] in ("complete", "max_ticks") else 1


def cmd_run_check(target: Path, name: str) -> int:
    """Resolve and invoke a check script by name.

    Used by `cmd:` strings in scaffolded goals.yaml so they don't have
    to spell out an internal package path. Resolution order:

    1. If `name` is `mode:check_name`, only that mode is searched:
       a. `<install>/peers/templates/modes/<mode>/checks/<check_name>.py`
       b. `<project>/.peers/checks/<check_name>.py` (back-compat for
          users who hand-edited a single check)
    2. Otherwise (unqualified `name`):
       a. `<project>/.peers/checks/<name>.py` (most common — checks
          were copied to the project at scaffold time)
       b. Otherwise walk all discovered modes and look for
          `<mode>/checks/<name>.py`. If more than one mode supplies
          the same name => exit 1, suggest `mode:name`.
    3. Once resolved => invoke via `python3 <resolved-path>` as a
       subprocess; forward exit code and stdout/stderr.
    4. Not found anywhere => exit 1, stderr lists all available check
       names (with mode prefix where applicable, sorted, deduped).

    Only top-level `.py` files in each mode's `checks/` dir are
    considered — lang-specific shell scripts (`checks/lang_<lang>/`)
    are invoked through their own `cmd:` strings (`bash .peers/...`),
    not through this shim.
    """
    from peers.modes import discover

    target = Path(target)
    proj_checks = target / ".peers" / "checks"
    modes = discover()

    resolved: Path | None = None
    if ":" in name:
        mode_name, _, check_name = name.partition(":")
        if not mode_name or not check_name:
            print(
                f"peers run-check: invalid name {name!r}; expected "
                "`name` or `mode:name`",
                file=sys.stderr,
            )
            return 1
        mode = modes.get(mode_name)
        if mode is not None:
            cand = mode.path / "checks" / f"{check_name}.py"
            if cand.is_file():
                resolved = cand
        if resolved is None:
            cand = proj_checks / f"{check_name}.py"
            if cand.is_file():
                resolved = cand
        if resolved is None:
            print(
                f"peers run-check: no such check {name!r}",
                file=sys.stderr,
            )
            _print_available_checks(modes, proj_checks)
            return 1
    else:
        # Unqualified: project's .peers/checks/ first.
        cand = proj_checks / f"{name}.py"
        if cand.is_file():
            resolved = cand
        else:
            hits: list[tuple[str, Path]] = []
            for mode_name, mode in modes.items():
                mcand = mode.path / "checks" / f"{name}.py"
                if mcand.is_file():
                    hits.append((mode_name, mcand))
            if len(hits) == 1:
                resolved = hits[0][1]
            elif len(hits) > 1:
                suggestions = ", ".join(
                    f"{m}:{name}" for m, _ in sorted(hits)
                )
                print(
                    f"peers run-check: ambiguous check {name!r} — "
                    f"defined in multiple modes; use one of: "
                    f"{suggestions}",
                    file=sys.stderr,
                )
                return 1
            else:
                print(
                    f"peers run-check: no such check {name!r}",
                    file=sys.stderr,
                )
                _print_available_checks(modes, proj_checks)
                return 1

    # Forward stdout/stderr and exit code by inheriting them.
    try:
        r = subprocess.run(
            [sys.executable, str(resolved)],
            cwd=str(target),
        )
    except OSError as e:
        print(
            f"peers run-check: failed to invoke {resolved}: {e}",
            file=sys.stderr,
        )
        return 1
    return r.returncode


def _print_available_checks(modes: dict, proj_checks: Path) -> None:
    """Helper: print sorted, deduped list of available check names to
    stderr. Used when resolution fails so the operator knows what's
    actually on offer.
    """
    by_name: dict[str, set[str]] = {}
    if proj_checks.is_dir():
        for f in proj_checks.iterdir():
            if f.is_file() and f.suffix == ".py":
                by_name.setdefault(f.stem, set()).add("project")
    for mode_name, mode in modes.items():
        cdir = mode.path / "checks"
        if not cdir.is_dir():
            continue
        for f in cdir.iterdir():
            if f.is_file() and f.suffix == ".py":
                by_name.setdefault(f.stem, set()).add(mode_name)
    if not by_name:
        print("  (no checks available)", file=sys.stderr)
        return
    print("  available:", file=sys.stderr)
    for cname in sorted(by_name):
        sources = sorted(by_name[cname])
        if sources == ["project"]:
            print(f"    {cname}  (from .peers/checks/)", file=sys.stderr)
        else:
            non_proj = [s for s in sources if s != "project"]
            print(
                f"    {cname}  (modes: {', '.join(non_proj)})",
                file=sys.stderr,
            )


_HELP_MAN_HINT = "\n(use --help-man for detailed docs + examples)"


def _add_help_man_subparser(sub, name: str, help_text: str | None = None,
                            **kwargs):
    """Add a subparser with the --help-man discovery hint appended to
    its `description=` AND the help-man + lang flags attached. Keeps
    `main()` readable now that every subparser carries the same trio
    of flags."""
    description = (help_text or "") + _HELP_MAN_HINT
    p = sub.add_parser(name, help=help_text, description=description,
                       **kwargs)
    attach_help_man_flags(p)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    from peers import __version__ as _peers_version
    parser = argparse.ArgumentParser(
        prog="peers",
        description=(
            "Multi-peer orchestration substrate for LLM coding agents."
            + _HELP_MAN_HINT
        ),
    )
    parser.add_argument(
        "--version", action="version",
        version=f"peers {_peers_version}",
    )
    parser.add_argument(
        "-C", "--target", default=".", type=Path,
        help="target project directory (default: cwd)",
    )
    attach_help_man_flags(parser)
    # `required=True` would reject `peers --help-man` (no subcommand).
    # Make `cmd` optional and check it AFTER parsing so the bare
    # `--help-man` path works.
    sub = parser.add_subparsers(dest="cmd", required=False)

    p_init = _add_help_man_subparser(
        sub, "init",
        help_text="bootstrap a .peers/ control plane in a target dir",
    )
    p_init.add_argument("--force", action="store_true")
    p_init.add_argument(
        "--driver", choices=("orchestrator", "hooks", "sessions"),
        default="orchestrator",
        help="default driver to scaffold; hooks writes "
             ".peers/hooks/ snippets for claude/codex Stop-hooks.",
    )
    p_init.add_argument(
        "--install", action="store_true",
        help="(with --driver=hooks) merge the Stop-hook directly into "
             "~/.claude/settings.json and ~/.codex/config.toml, with "
             "timestamped backups. Idempotent and safe to re-run.",
    )
    p_init.add_argument(
        "--modes",
        default=None,
        help="comma-separated mode names (e.g. audit,security). "
             "Run `peers-ctl modes list` for the available set.",
    )
    p_init.add_argument(
        "--audit-templates", action="store_true",
        help="DEPRECATED alias for --modes=audit. Use --modes going "
             "forward; this flag will be removed in a future release.",
    )
    p_init.add_argument(
        "--lang", default="python",
        help=(
            "audit-template language: python, js, rust, or go; "
            "unknown falls back"
        ),
    )

    _add_help_man_subparser(
        sub, "status",
        help_text="print iteration, next-up peer, lock + goals status",
    )

    p_run = _add_help_man_subparser(
        sub, "run",
        help_text="run the peer loop until a stop reason is reached",
    )
    p_run.add_argument("--max-ticks", type=int, default=None)
    p_run.add_argument(
        "--max-usd", type=float, default=None,
        help="override budget.max_usd for this run",
    )
    p_run.add_argument(
        "--dry-run", action="store_true",
        help="run the loop but revert any peer commits at end of each "
             "tick — useful for testing the substrate / observing "
             "what peers would do without changing the repo.",
    )
    p_run.add_argument(
        "-v", "--verbose", action="store_true",
        help="after each tick, echo the last 50 lines of peer stdout "
             "and last 25 lines of peer stderr to the substrate's "
             "stderr (still also written in full to "
             ".peers/log/peers/tick-*).",
    )
    p_run.add_argument(
        "--without-recon", action="store_true",
        help="skip the substrate pre-tick recon step that writes "
             ".peers/recon.md with a static project digest. Recon is "
             "free and fast (no LLM call) and helps peers know what "
             "the project IS without burning tick 1 figuring it out; "
             "only opt out if recon.md was hand-prepared or is "
             "explicitly unwanted.",
    )
    p_run.add_argument(
        "--without-post-convergence-skeptic", action="store_true",
        help="skip the auto-skeptic re-audit tick that fires when "
             "convergence-reached is about to declare terminal "
             "success. By default the substrate runs ONE extra tick "
             "with a critical-re-audit prompt — if it surfaces a new "
             "blocking bug the counter resets, otherwise terminal "
             "exit. Opt out for runs where false-convergence is "
             "acceptable (e.g. CI).",
    )

    p_replay = _add_help_man_subparser(
        sub, "replay",
        help_text="print log entries for a given iteration as JSON",
    )
    p_replay.add_argument("iteration", type=int)

    _add_help_man_subparser(
        sub, "report",
        help_text=(
            "write .peers/REPORT.md — human-readable Markdown "
            "summary of state + recent ticks + warnings."
        ),
    )

    _add_help_man_subparser(
        sub, "info",
        help_text=(
            "print configured peers, goals, budget, and health "
            "without running anything."
        ),
    )

    _add_help_man_subparser(
        sub, "verify",
        help_text=(
            "re-run all hard goals (and any `verify.commands`) against "
            "the current project state, without involving any peer. "
            "Writes .peers/VERIFY.md; exit 0 iff every check passed."
        ),
    )

    p_tick = _add_help_man_subparser(
        sub, "tick",
        help_text="run exactly ONE tick and exit (for hook-driven mode)",
    )
    p_tick.add_argument(
        "--dry-run", action="store_true",
        help="reverts peer commit at end of the tick",
    )
    p_tick.add_argument(
        "--after", default=None,
        help="(informational) name of the peer that just finished; "
             "the next tick will pick up via state.turn_index. "
             "Useful for hook-driver chains.",
    )

    p_watch = _add_help_man_subparser(
        sub, "watch",
        help_text=(
            "watch .peers/comms/<from>-to-<receiver>/ and print new "
            "messages as they arrive (for sessions-driven mode where "
            "each peer is a long-lived tmux session). Runs until "
            "interrupted."
        ),
    )
    p_watch.add_argument(
        "receiver", help="peer name to watch the inbox FOR "
                         "(e.g. 'claude' to see codex->claude messages)",
    )
    p_watch.add_argument(
        "--poll-s", type=float, default=1.0,
        help="filesystem poll interval in seconds (default 1.0)",
    )

    p_run_check = _add_help_man_subparser(
        sub, "run-check",
        help_text=(
            "resolve and invoke a check script by name. Used by "
            "`cmd:` lines in scaffolded goals.yaml so they don't "
            "depend on the internal `python3 -m peers.templates.X` "
            "package layout. Accepts `<name>` or `<mode>:<name>`."
        ),
    )
    p_run_check.add_argument(
        "name",
        help="check name (e.g. `verify_self_review`) or qualified "
             "`mode:name` (e.g. `audit:verify_self_review`).",
    )

    # G3: tmux session wrappers.
    p_tmux = _add_help_man_subparser(
        sub, "tmux",
        help_text="tmux session wrappers for the sessions-driver",
    )
    tmux_sub = p_tmux.add_subparsers(dest="tmux_cmd", required=False)
    tmux_sub.add_parser("up", help="create tmux session with one pane per peer")
    tmux_sub.add_parser("down", help="kill the peers tmux session")
    tmux_sub.add_parser("attach", help="attach to the peers tmux session")

    args = parser.parse_args(argv)

    # Dispatch --help-man BEFORE any normal cmd handling so it works
    # even when no subcommand was provided.
    if getattr(args, "help_man", False):
        subcmd = None
        if args.cmd == "tmux":
            subcmd = getattr(args, "tmux_cmd", None)
        return print_help_man("peers", args.cmd, subcmd, pick_lang(args))

    # --help-man was the only way to invoke `peers` without a sub-cmd.
    # Restore the "subcommand required" behaviour for normal calls.
    if args.cmd is None:
        parser.error("the following arguments are required: cmd")
    if args.cmd == "init":
        modes_arg = getattr(args, "modes", None)
        if modes_arg is not None:
            modes = [m.strip() for m in modes_arg.split(",") if m.strip()]
            if not modes:
                print(f"peers: --modes value {modes_arg!r} parsed to an "
                      "empty list (only whitespace/commas?); did you mean to "
                      "pass at least one mode name?", file=sys.stderr)
                return 2
        else:
            modes = None
        return cmd_init(args.target, args.force, driver=args.driver,
                        install_hooks=getattr(args, "install", False),
                        modes=modes,
                        audit_templates=args.audit_templates,
                        lang=args.lang)
    if args.cmd == "status":
        return cmd_status(args.target)
    if args.cmd == "run":
        return cmd_run(args.target, args.max_ticks, args.dry_run,
                       args.max_usd, verbose=args.verbose,
                       without_recon=args.without_recon,
                       without_post_convergence_skeptic=(
                           args.without_post_convergence_skeptic
                       ))
    if args.cmd == "replay":
        return cmd_replay(args.target, args.iteration)
    if args.cmd == "report":
        return cmd_report(args.target)
    if args.cmd == "info":
        return cmd_info(args.target)
    if args.cmd == "verify":
        return cmd_verify(args.target)
    if args.cmd == "tick":
        return cmd_run(args.target, max_ticks=1, dry_run=args.dry_run)
    if args.cmd == "watch":
        return cmd_watch(args.target, args.receiver, args.poll_s)
    if args.cmd == "tmux":
        if not getattr(args, "tmux_cmd", None):
            parser.error("tmux: choose one of: up, down, attach "
                         "(or use --help-man)")
        return cmd_tmux(args.target, args.tmux_cmd)
    if args.cmd == "run-check":
        return cmd_run_check(args.target, args.name)
    return 2


def cmd_watch(target: Path, receiver: str, poll_s: float) -> int:
    """G3 (sessions-driver helper): tail .peers/comms/*-to-<receiver>/
    for new files and print their content. Lets a long-lived peer
    session pick up messages via filesystem polling without a daemon.
    """
    import time as _time
    target = Path(target)
    comms = target / ".peers" / "comms"
    if not is_valid_peer_name(receiver):
        print(
            f"peers watch: invalid receiver name: {receiver!r}",
            file=sys.stderr,
        )
        return 2
    err = _refuse_symlink_control_dir(target / ".peers")
    if err is not None:
        print(err, file=sys.stderr)
        return 1
    seen: set[Path] = set()
    try:
        while True:
            if comms.exists():
                for inbox in comms.glob(f"*-to-{receiver}"):
                    for msg in sorted(inbox.glob("[0-9][0-9][0-9][0-9]-*.md")):
                        if msg in seen:
                            continue
                        seen.add(msg)
                        try:
                            print(f"--- {msg} ---")
                            text = read_text_no_symlink(
                                msg, max_bytes=64 * 1024 + 1
                            )
                            if len(text) > 64 * 1024:
                                print(text[:64 * 1024])
                                print("--- truncated ---")
                            else:
                                print(text)
                            print("--- end ---")
                            sys.stdout.flush()
                        except OSError as e:
                            print(
                                f"peers watch: warning: cannot read "
                                f"{msg}: {e}",
                                file=sys.stderr,
                            )
            _time.sleep(poll_s)
    except KeyboardInterrupt:
        return 0


def cmd_tmux(target: Path, subcmd: str) -> int:
    """G3: tmux session wrappers.

    `peers tmux up`   creates a session named `peers-<basename>` with
                     one window per peer (running their CLI with the
                     appropriate `--continue` / `resume` flag) plus a
                     watcher window for each peer's inbox.
    `peers tmux down` kills the session.
    `peers tmux attach` runs `tmux attach -t peers-<basename>`.
    """
    target = Path(target).resolve()
    peer_dir = target / ".peers"
    err = _refuse_symlink_control_dir(peer_dir)
    if err is not None:
        print(err, file=sys.stderr)
        return 1
    cfg_path = peer_dir / "config.yaml"
    if not cfg_path.exists():
        print(f"missing {cfg_path}; run `peers init` first",
              file=sys.stderr)
        return 1
    if shutil.which("tmux") is None:
        print("tmux not on PATH; install it or use a different driver",
              file=sys.stderr)
        return 1
    try:
        cfg = _load_config_yaml(cfg_path)
        peer_specs = load_peer_specs(cfg)
    except (OSError, ValueError) as e:
        print(f"cannot load peers from config: {e}", file=sys.stderr)
        return 1

    session = f"peers-{target.name}"

    if subcmd == "down":
        subprocess.run(["tmux", "kill-session", "-t", session],
                       check=False)
        return 0
    if subcmd == "attach":
        return subprocess.call(["tmux", "attach", "-t", session])

    # up
    # Avoid stacking sessions: if it already exists, refuse.
    has = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True,
    )
    if has.returncode == 0:
        print(f"session {session} already exists; use "
              f"`peers tmux down` first or `peers tmux attach`")
        return 1
    # Shell-quote the target path before embedding into tmux command
    # strings (tmux passes them to a shell). This guards against
    # surprising target paths.
    import shlex
    qt = shlex.quote(str(target))
    # First window: first peer.
    first = peer_specs[0]
    rc = subprocess.run([
        "tmux", "new-session", "-d", "-s", session,
        "-n", first.name, "-c", str(target),
        _continue_cmd(first),
    ]).returncode
    if rc != 0:
        return rc
    for spec in peer_specs[1:]:
        subprocess.run([
            "tmux", "new-window", "-t", session,
            "-n", spec.name, "-c", str(target),
            _continue_cmd(spec),
        ], check=False)
    # Watcher window — runs `peers watch` for each peer's inbox using
    # split panes so all watchers are visible together.
    qn0 = shlex.quote(peer_specs[0].name)
    subprocess.run([
        "tmux", "new-window", "-t", session, "-n", "watch",
        "-c", str(target),
        f"peers -C {qt} watch {qn0}",
    ], check=False)
    for spec in peer_specs[1:]:
        qn = shlex.quote(spec.name)
        subprocess.run([
            "tmux", "split-window", "-t", f"{session}:watch",
            "-c", str(target),
            f"peers -C {qt} watch {qn}",
        ], check=False)
    subprocess.run(["tmux", "select-layout", "-t",
                    f"{session}:watch", "tiled"], check=False)
    print(f"tmux session {session} created. Attach with: "
          f"peers -C {qt} tmux attach")
    return 0


def _continue_cmd(spec) -> str:
    """Construct a shell command that runs the peer in 'continue an
    existing session' mode. Falls back to the configured argv if the
    tool isn't claude/codex."""
    if spec.tool == "claude":
        return "claude --continue || claude"
    if spec.tool == "codex":
        return "codex resume || codex"
    # Generic fallback — just spawn a login shell so the user can
    # invoke the tool by hand.
    return "bash -l"


if __name__ == "__main__":
    sys.exit(main())
