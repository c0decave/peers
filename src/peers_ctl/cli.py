"""peers-ctl CLI entrypoint."""
from __future__ import annotations

import argparse
import hashlib
from collections import deque
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from peers.help_man import (
    attach_help_man_flags,
    pick_lang,
    print_help_man,
)
from peers.safe_io import (
    open_text_in_dir_no_symlink,
    open_text_read_no_symlink,
    read_bytes_no_symlink,
    read_text_no_symlink,
    write_text_no_symlink,
)
from peers_ctl.contracts import (
    ContractsMismatch,
    amend_acceptance,
    verify_contracts,
    write_frozen_contracts,
)
from peers_ctl.plan_parser import PlanValidationError, parse_plan
from peers_ctl.runner import start_project, stop_project
from peers_ctl.store import (
    Project, Store, prune_logs, validate_project_name,
    reconcile,
)


_DEFAULT_PROJECTS_ROOT = Path.home() / "c0de" / "peers-c0de"
_DASHBOARD_STATE_MAX_BYTES = 5 * 1024 * 1024


def projects_root() -> Path:
    """Return the directory where bare-name peers projects live.

    Resolves in this order:
    - `$PEERS_PROJECTS_ROOT` env var (expanded + resolved)
    - `~/c0de/peers-c0de/` (Phase-3i default; replaces ad-hoc `/tmp`
      and scattered `~/code` paths)

    The directory is auto-created on first use.
    """
    raw = os.environ.get("PEERS_PROJECTS_ROOT")
    if raw:
        root = Path(raw).expanduser()
    else:
        root = _DEFAULT_PROJECTS_ROOT
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def expand_project_arg(arg: Path) -> Path:
    """Resolve a `peers-ctl new`/`add` path argument.

    - Absolute path or path containing `/` → use verbatim (back-compat).
    - Bare name (no separator) → `projects_root() / name`.

    Lets users say `peers-ctl new my-thing` instead of typing the full
    `~/c0de/peers-c0de/my-thing` every time.
    """
    s = str(arg)
    if s.startswith("/") or os.sep in s or "/" in s or s in (".", "..") or s.startswith("./") or s.startswith("../"):
        return Path(s).expanduser().resolve()
    return projects_root() / s


def _store(config_dir: Path | None = None) -> Store:
    return Store(config_dir)


def _ensure_scaffold_docs(target: Path, name: str,
                          spec_text: str | None) -> None:
    """Ensure scaffolded projects have baseline human-facing docs."""
    readme = target / "README.md"
    if readme.is_symlink():
        raise OSError(f"refusing symlinked README.md: {readme}")
    if not readme.exists():
        write_text_no_symlink(
            readme,
            f"# {name}\n\nScaffolded by peers-ctl new.\n",
        )
    elif not readme.is_file():
        raise OSError(f"refusing non-file README.md: {readme}")
    if spec_text is not None:
        spec_path = target / "SPEC.md"
        if spec_path.is_symlink():
            raise OSError(f"refusing symlinked SPEC.md: {spec_path}")
        write_text_no_symlink(spec_path, spec_text)


def cmd_add(name: str | None, path: Path,
            config_dir: Path | None = None) -> int:
    target = expand_project_arg(path)
    if not target.is_dir():
        print(f"path is not a directory: {target}", file=sys.stderr)
        return 2
    name = name or target.name
    try:
        validate_project_name(name)
    except ValueError as e:
        print(f"peers-ctl: {e}", file=sys.stderr)
        return 2
    store = _store(config_dir)
    proj = Project(name=name, path=str(target))
    try:
        store.add(proj)
    except (OSError, ValueError) as e:
        print(f"peers-ctl: {e}", file=sys.stderr)
        return 1
    has_peers = (target / ".peers" / "config.yaml").exists()
    print(f"Added project '{name}' → {target}")
    if not has_peers:
        print(f"  (warning: no .peers/config.yaml yet — "
              f"run `peers -C {target} init` before `peers-ctl start {name}`)")
    return 0


def _container_run_in(target: Path, *peers_args: str) -> int:
    """Run `peers ...args` inside the peers:dev container against
    `target`. Used by `peers-ctl new --container` so the host doesn't
    need `peers` itself installed — only `podman`."""
    from peers_ctl.runner import (
        CONTAINER_IMAGE, PODMAN_CMD, PODMAN_NETWORK,
    )
    from pathlib import Path as _P
    home = _P.home()
    argv = [
        PODMAN_CMD, "run", "--rm",
        "--userns=keep-id", "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "-v", f"{target.resolve()}:/work",
    ]
    if (home / ".gitconfig").is_file():
        argv += ["-v", f"{home / '.gitconfig'}:/home/peer/.gitconfig:ro"]
    argv += [f"--network={PODMAN_NETWORK or 'none'}"]
    argv += [CONTAINER_IMAGE, *peers_args]
    try:
        return subprocess.call(argv)
    except FileNotFoundError as e:
        print(f"peers-ctl: container runtime not found: {e}", file=sys.stderr)
        return 127


def cmd_new(path: Path, name: str | None = None,
            spec: str | None = None, force: bool = False,
            driver: str = "orchestrator",
            container: bool = False,
            modes: list[str] | None = None,
            audit_templates: bool = False,   # legacy alias
            lang: str = "python",
            plan: str | None = None,
            config_dir: Path | None = None) -> int:
    """One-shot project scaffold: create the target directory, git
    init it with an empty initial commit, run `peers init` against
    it, optionally write a SPEC.md, and register the result with the
    controller. Idempotent only with --force (otherwise refuses to
    overwrite a non-empty directory).

    container=True: run `peers init` inside the peers:dev container
    instead of on the host. Lets the host get away with just
    `peers-ctl` + `podman` (no `peers` binary needed).

    After this returns, the project is ready for
    `peers-ctl start <name>`.

    Path resolution:
    - Bare name (no slash) → `$PEERS_PROJECTS_ROOT/<name>`, default
      `~/c0de/peers-c0de/<name>`. Created if missing.
    - Path with `/` or starting with `./` → used verbatim
      (backwards-compat for explicit paths).
    """
    target = expand_project_arg(path)
    name = name or target.name
    try:
        validate_project_name(name)
    except ValueError as e:
        print(f"peers-ctl: {e}", file=sys.stderr)
        return 2
    # Legacy alias: --audit-templates ⇒ --modes=audit. If both are passed,
    # explicit `modes` wins (user opt-in).
    if audit_templates and not modes:
        modes = ["audit"]
    # --plan and --spec are mutually exclusive (different scaffold paths).
    if plan is not None and spec is not None:
        print("peers-ctl: --plan and --spec are mutually exclusive "
              "(--plan drives implement-mode; --spec is for ad-hoc SPEC.md)",
              file=sys.stderr)
        return 2
    has_implement = bool(modes and "implement" in modes)
    if has_implement and plan is None:
        print("peers-ctl: --modes=implement requires --plan FILE",
              file=sys.stderr)
        return 2
    if plan is not None and not has_implement:
        print("peers-ctl: --plan requires --modes=implement",
              file=sys.stderr)
        return 2
    # Parse + validate PLAN.md and run the acceptance preflight before
    # touching the filesystem. The preflight runs in the operator's cwd
    # (the new project dir doesn't exist yet); if acceptance already
    # passes, the feature is presumed implemented — refuse unless
    # --force is set.
    plan_obj = None
    plan_md_content: str | None = None
    if plan is not None:
        plan_path = Path(plan).expanduser()
        if not plan_path.is_file():
            print(f"peers-ctl: --plan path does not exist or is not a file: "
                  f"{plan_path}", file=sys.stderr)
            return 2
        # Read PLAN.md as bytes via the no-symlink helper, then strict-
        # decode UTF-8. The substrate's read_text_no_symlink uses
        # errors="replace" which would silently corrupt non-UTF-8 input
        # before SHA-pinning into PLAN.original.md (data-integrity
        # hazard for the frozen contract).
        try:
            plan_md_bytes = read_bytes_no_symlink(plan_path)
        except OSError as e:
            print(f"peers-ctl: cannot read --plan file {plan_path}: {e}",
                  file=sys.stderr)
            return 2
        try:
            plan_md_content = plan_md_bytes.decode("utf-8")
        except UnicodeDecodeError as e:
            print(f"peers-ctl: error: PLAN.md is not valid UTF-8: {e}",
                  file=sys.stderr)
            return 1
        try:
            plan_obj = parse_plan(plan_path)
        except PlanValidationError as e:
            print(f"peers-ctl: PLAN.md validation failed: {e}",
                  file=sys.stderr)
            return 1
        # Preflight runs in the operator's current working directory.
        # The target project dir typically doesn't exist yet (this command
        # creates it), so the acceptance command must reference paths
        # the operator can see from cwd — that's the explicit contract:
        # run `peers-ctl new` from the directory whose layout the
        # acceptance command targets. Passing cwd= explicitly (vs
        # inheriting silently) documents the contract in the call site.
        # DEVNULL stdout/stderr because preflight only inspects
        # returncode and unbounded buffering of a runaway command would
        # cost memory for no benefit.
        try:
            preflight = subprocess.run(
                ["/bin/sh", "-c", plan_obj.acceptance],
                cwd=str(Path.cwd()),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            # A 60s timeout on the preflight means the command is likely
            # heavy / hangs; treat as "not yet passing" and continue.
            preflight = None
        except OSError as e:
            print(f"peers-ctl: acceptance preflight failed to launch: {e}",
                  file=sys.stderr)
            return 1
        if (preflight is not None
                and preflight.returncode == 0
                and not force):
            print(
                "peers-ctl: acceptance command already passes — feature "
                "appears to already be implemented (use --force to override)",
                file=sys.stderr,
            )
            return 1
    spec_text: str | None = None
    if spec is not None:
        spec_path = Path(spec).expanduser()
        looks_like_path = (
            spec.startswith((".", "~"))
            or "/" in spec
            or os.sep in spec
        )
        if spec_path.is_file():
            try:
                spec_text = read_text_no_symlink(spec_path)
            except OSError as e:
                print(f"cannot read --spec file {spec_path}: {e}",
                      file=sys.stderr)
                return 2
        elif looks_like_path:
            print(f"--spec path does not exist or is not a file: {spec_path}",
                  file=sys.stderr)
            return 2
        else:
            spec_text = spec
    if target.exists():
        if not target.is_dir():
            print(f"path exists but is not a directory: {target}",
                  file=sys.stderr)
            return 2
        try:
            existing = list(target.iterdir())
        except OSError as e:
            print(f"cannot read {target}: {e}", file=sys.stderr)
            return 2
        if existing and not force:
            print(f"refusing to scaffold into non-empty directory: "
                  f"{target} (use --force to override)",
                  file=sys.stderr)
            return 2
    else:
        target.mkdir(parents=True)
    try:
        _ensure_scaffold_docs(target, name, spec_text)
    except OSError as e:
        print(f"cannot scaffold project files safely: {e}",
              file=sys.stderr)
        return 1
    # 1. git init + initial commit so peers-baseline has something to tag.
    if not (target / ".git").exists():
        r = subprocess.run(
            ["git", "init", "-q", "-b", "main"],
            cwd=target, capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(f"git init failed: {r.stderr}", file=sys.stderr)
            return 1
        for k, v in (("user.email", "you@local"), ("user.name", "you")):
            # Don't override the user's already-set git config; only
            # set local config if none inherited.
            cur = subprocess.run(
                ["git", "config", "--get", k],
                cwd=target, capture_output=True, text=True,
            )
            if cur.returncode != 0:
                subprocess.run(
                    ["git", "config", k, v],
                    cwd=target, capture_output=True, check=False,
                )
        subprocess.run(
            ["git", "add", "-A"],
            cwd=target, capture_output=True, check=False,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "initial scaffold"],
            cwd=target, capture_output=True, check=False,
        )

    # 2. peers init (host or container).
    # If --modes contains `implement` but the template is not yet present
    # (Task 2.8 has not landed), drop it from the list passed to
    # `peers init` so the latter doesn't error on an unknown mode. The
    # core implement-mode files (PLAN.md + frozen contracts) are still
    # written below.
    implement_warning_pending = False
    init_modes = list(modes) if modes else None
    if init_modes and "implement" in init_modes:
        try:
            from peers.modes import discover as _discover_modes
            available = _discover_modes()
        except Exception:
            available = {}
        if "implement" not in available:
            init_modes = [m for m in init_modes if m != "implement"]
            implement_warning_pending = True
            if not init_modes:
                init_modes = None
    if container:
        # ENTRYPOINT is `peers`, target mounted at /work.
        ic_args = ["-C", "/work", "init"]
        if force:
            ic_args.append("--force")
        if driver != "orchestrator":
            ic_args += ["--driver", driver]
        if init_modes:
            ic_args += ["--modes", ",".join(init_modes)]
            ic_args += ["--lang", lang]
        rc = _container_run_in(target, *ic_args)
        if rc != 0:
            print(f"peers init (container) failed: rc={rc}",
                  file=sys.stderr)
            return 1
    else:
        init_argv = ["peers", "-C", str(target), "init"]
        if force:
            init_argv.append("--force")
        if driver != "orchestrator":
            init_argv += ["--driver", driver]
        if init_modes:
            init_argv += ["--modes", ",".join(init_modes)]
            init_argv += ["--lang", lang]
        r = subprocess.run(init_argv, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"peers init failed: {r.stderr.strip()}",
                  file=sys.stderr)
            return 1
        if r.stderr.strip():
            print(r.stderr.strip(), file=sys.stderr)

    # 2b. implement-mode: copy live PLAN.md and write frozen contracts.
    # `peers init` has already created .peers/ above; do this regardless
    # of whether the implement mode-template is present yet (Task 2.8).
    if plan_obj is not None and plan_md_content is not None:
        live_plan_path = target / "PLAN.md"
        try:
            write_text_no_symlink(live_plan_path, plan_md_content)
        except OSError as e:
            print(f"peers-ctl: cannot write PLAN.md safely: {e}",
                  file=sys.stderr)
            return 1
        try:
            write_frozen_contracts(
                target / ".peers",
                acceptance=plan_obj.acceptance,
                e2e=plan_obj.e2e,
                plan_md_content=plan_md_content,
            )
        except OSError as e:
            print(f"peers-ctl: cannot write frozen contracts: {e}",
                  file=sys.stderr)
            return 1
        if implement_warning_pending:
            print(
                "peers-ctl: implement mode template not yet present "
                "(Task 2.8); core files written",
                file=sys.stderr,
            )

    # 3. register in the controller.
    store = _store(config_dir)
    if store.get(name):
        if not force:
            print(f"peers-ctl: project {name!r} already registered "
                  f"(re-run with --force or use a different --name)",
                  file=sys.stderr)
            return 1
        store.remove(name)
    proj = Project(name=name, path=str(target))
    try:
        store.add(proj)
    except (OSError, ValueError) as e:
        print(f"peers-ctl: cannot register project safely: {e}",
              file=sys.stderr)
        return 1

    print(f"Scaffolded project {name!r} at {target}")
    if modes:
        print(
            f"  Next: $EDITOR {target / 'SPEC.md'}  "
            "# describe scope, risks, and invariants"
        )
        # Back-compat: when audit is the (only) mode, keep the legacy
        # "audit gates" phrasing so existing tooling/tests still match.
        if modes == ["audit"]:
            hint = "# review pre-wired audit gates"
        else:
            hint = f"# review pre-wired gates from modes={','.join(modes)}"
        print(
            f"        $EDITOR {target / '.peers' / 'goals.yaml'}  "
            f"{hint}"
        )
    else:
        print(
            f"  Next: $EDITOR {target / '.peers' / 'goals.yaml'}  "
            "# delete placeholder, add real gates"
        )
    print(f"        peers-ctl start {name} --max-ticks 5 --max-usd 1")
    return 0


def parse_runtime_duration(text: str) -> tuple[int, bool]:
    """Parse a ``--max-runtime`` value into ``(seconds, additive)``.

    Wraps :func:`peers_ctl.runner._parse_duration` with leading-``+``
    detection (Task 7.4). Returns:

    - ``(seconds, False)`` for an absolute value, e.g. ``"6h"``.
    - ``(seconds, True)`` for an additive value, e.g. ``"+6h"`` — the
      caller is expected to add the delta to the project's current
      ``budget.max_runtime_s`` rather than replacing it.

    The numeric portion is parsed via the existing duration grammar
    (POSITIVE_INT[s|m|h|d|w]); the only addition here is the optional
    leading ``+``. Raises ``ValueError`` on malformed input — silent
    misinterpretation of budget caps is worse than refusing to parse.
    """
    if not isinstance(text, str):
        raise ValueError(
            f"duration must be a string, got {type(text).__name__}"
        )
    s = text.strip()
    additive = s.startswith("+")
    if additive:
        s = s[1:]
    from peers_ctl.runner import _parse_duration
    seconds = _parse_duration(s)
    return seconds, additive


def cmd_ack_block(name: str, step_id: str, reason: str) -> int:
    """Task 7.3: transition a step's ``[BLOCKED]`` marker to
    ``[BLOCKED-ACK]`` in ``<project>/PLAN.md`` and append a hash-chained
    audit entry to ``<project>/.peers/blocks.log``.

    Errors (exit 1) when:
      * project name is invalid or no such project under
        ``$PEERS_PROJECTS_ROOT`` (exit 2 for name-validation failure
        to keep symmetry with the rest of the CLI).
      * STEP-N is not present in PLAN.md.
      * STEP-N is not currently in the ``[BLOCKED]`` state.

    The transition is line-oriented: it locates the unique step line
    matching ``^\\s*-\\s*\\[BLOCKED\\]\\s*\\[STEP-N\\]`` and rewrites
    only the marker substring. PLAN.md's other lines are preserved
    byte-for-byte. Mirrors the :func:`peers_ctl.contracts.amend_acceptance`
    log format: ``<chain16> <iso8601> ack-block STEP-N | reason: <text>``.
    """
    try:
        validate_project_name(name)
    except ValueError as e:
        print(f"peers-ctl: {e}", file=sys.stderr)
        return 2
    proj_dir = projects_root() / name
    if not proj_dir.is_dir():
        print(f"peers-ctl: no such project: {name}", file=sys.stderr)
        return 1
    plan_path = proj_dir / "PLAN.md"
    if not plan_path.is_file():
        print(f"peers-ctl: PLAN.md missing for project {name!r}",
              file=sys.stderr)
        return 1
    if not re.match(r"^STEP-\d+$", step_id):
        print(f"peers-ctl: invalid step id {step_id!r} "
              "(expected STEP-N)", file=sys.stderr)
        return 2
    # Read current PLAN.md and locate the target step line. We allow
    # any current state marker so that we can give a precise error
    # message ("not blocked") rather than a generic "not found".
    try:
        plan_text = plan_path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"peers-ctl: cannot read {plan_path}: {e}", file=sys.stderr)
        return 1
    step_marker_re = re.compile(
        r"^(?P<indent>\s*-\s*)\[(?P<mark>[ xX]|PARTIAL|BLOCKED|BLOCKED-ACK)\]"
        r"(?P<sep>\s*\[" + re.escape(step_id) + r"\])"
    )
    new_lines: list[str] = []
    found = False
    transitioned = False
    found_state: str | None = None
    for line in plan_text.splitlines(keepends=True):
        m = step_marker_re.match(line)
        if m:
            found = True
            found_state = m.group("mark")
            if found_state == "BLOCKED":
                new_line = (
                    m.group("indent") + "[BLOCKED-ACK]"
                    + m.group("sep") + line[m.end():]
                )
                new_lines.append(new_line)
                transitioned = True
                continue
        new_lines.append(line)
    if not found:
        print(f"peers-ctl: step {step_id} not found in {plan_path}",
              file=sys.stderr)
        return 1
    if not transitioned:
        print(f"peers-ctl: step {step_id} is not in BLOCKED state "
              f"(current marker: [{found_state}])", file=sys.stderr)
        return 1
    new_text = "".join(new_lines)
    # Write back via the safe helper to avoid symlink-swap races on
    # the operator-visible PLAN.md path.
    try:
        write_text_no_symlink(plan_path, new_text)
    except OSError as e:
        print(f"peers-ctl: cannot rewrite PLAN.md safely: {e}",
              file=sys.stderr)
        return 1
    # Append hash-chained audit entry to .peers/blocks.log. Mirrors
    # the contracts.log chain format (16-char sha256 prefix per line).
    plan_dir = proj_dir / ".peers"
    try:
        plan_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"peers-ctl: cannot create {plan_dir}: {e}", file=sys.stderr)
        return 1
    log_path = plan_dir / "blocks.log"
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    entry_text = (
        f"{timestamp} ack-block {step_id} | reason: {reason}\n"
    )
    prev = "genesis"
    if log_path.is_file():
        try:
            with log_path.open("r", encoding="utf-8") as f:
                for log_line in f:
                    log_line = log_line.rstrip("\n")
                    if not log_line:
                        continue
                    prefix, _, _ = log_line.partition(" ")
                    prev = prefix
        except OSError as e:
            print(f"peers-ctl: cannot read {log_path}: {e}", file=sys.stderr)
            return 1
    chain_prefix = hashlib.sha256(
        (prev + entry_text).encode("utf-8")
    ).hexdigest()[:16]
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"{chain_prefix} {entry_text}")
    except OSError as e:
        print(f"peers-ctl: cannot append to {log_path}: {e}",
              file=sys.stderr)
        return 1
    print(f"peers-ctl: {step_id} acknowledged "
          f"({name}/PLAN.md). Logged to .peers/blocks.log")
    return 0


def cmd_amend(name: str, acceptance: str, reason: str) -> int:
    """Legitimate-escape: re-pin the frozen acceptance command.

    For implement-mode projects only. Looks up
    ``<projects_root>/<name>/.peers/``, verifies it's an implement-mode
    project (has ``contracts.sha``), then calls
    :func:`peers_ctl.contracts.amend_acceptance` which rewrites
    ``acceptance.sh``, re-pins its SHA, and appends a hash-chained audit
    line to ``contracts.log``. After the amendment, verifies contracts
    are self-consistent.

    Errors go to stderr with exit 1; no traceback.
    """
    try:
        validate_project_name(name)
    except ValueError as e:
        print(f"peers-ctl: {e}", file=sys.stderr)
        return 2
    proj_dir = projects_root() / name
    if not proj_dir.is_dir():
        print(f"peers-ctl: no such project: {name}", file=sys.stderr)
        return 1
    plan_dir = proj_dir / ".peers"
    sha_path = plan_dir / "contracts.sha"
    if not sha_path.is_file():
        print(
            f"peers-ctl: project {name!r} is not in implement-mode "
            f"(no contracts.sha)",
            file=sys.stderr,
        )
        return 1
    try:
        amend_acceptance(plan_dir, acceptance, reason)
        verify_contracts(plan_dir)
    except ContractsMismatch as e:
        print(f"peers-ctl: contracts amend failed: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"peers-ctl: cannot amend contracts safely: {e}",
              file=sys.stderr)
        return 1
    print(f"Amended acceptance for {name}. "
          f"Audit logged to .peers/contracts.log")
    return 0


def cmd_resume(name: str) -> int:
    """Task 4.5: clear a project's Phase-0 checkpoint marker.

    Removes `.peers/checkpoint_requested` and `.peers/awaiting_user`
    so that the next `peers-ctl start <name>` proceeds past Phase 0
    normally. Idempotent: succeeds even when no markers exist
    (operator can run it defensively before any start).

    Design choice (v1): this command ONLY clears markers. It does
    not re-invoke `cmd_start` — the operator runs that explicitly
    after reviewing RECON.md + PLAN.aligned.md +
    ARCHITECTURE.intended.md. Keeps `resume` flag-agnostic (no need
    to track the original `start` flags) and makes every resume an
    explicit, fresh `start` decision.
    """
    try:
        validate_project_name(name)
    except ValueError as e:
        print(f"peers-ctl: {e}", file=sys.stderr)
        return 2
    proj_dir = projects_root() / name
    if not proj_dir.is_dir():
        print(f"peers-ctl: no such project: {name}", file=sys.stderr)
        return 1
    plan_dir = proj_dir / ".peers"
    if not plan_dir.is_dir():
        print(f"peers-ctl: {name!r} has no .peers/ directory "
              "(unscaffolded?)", file=sys.stderr)
        return 1
    cleared: list[str] = []
    for marker_name in ("checkpoint_requested", "awaiting_user"):
        marker = plan_dir / marker_name
        if marker.is_symlink():
            print(f"peers-ctl: refusing to remove symlinked marker: {marker}",
                  file=sys.stderr)
            return 1
        if marker.exists():
            try:
                marker.unlink()
                cleared.append(marker_name)
            except OSError as e:
                print(f"peers-ctl: cannot remove {marker}: {e}",
                      file=sys.stderr)
                return 1
    if cleared:
        print(f"peers-ctl: cleared marker(s): {', '.join(cleared)}")
    else:
        print(f"peers-ctl: no checkpoint markers to clear for {name!r}")
    print(f"  Next: peers-ctl start {name}  "
          "# resumes past Phase 0 into implementation")
    return 0


def cmd_remove(name: str, config_dir: Path | None = None) -> int:
    store = _store(config_dir)
    try:
        store.remove(name)
    except ValueError as e:
        print(f"peers-ctl: {e}", file=sys.stderr)
        return 1
    print(f"Removed project '{name}'")
    return 0


def cmd_list(config_dir: Path | None = None) -> int:
    store = _store(config_dir)
    reconcile(store)
    projects = store.list_projects()
    if not projects:
        print("(no projects — `peers-ctl add <path>` to register one)")
        return 0
    print(f"{'NAME':<20} {'STATE':<8} {'PID':<8} {'PATH'}")
    for p in projects:
        pid = "" if p.pid is None else str(p.pid)
        print(f"{p.name:<20} {p.state:<8} {pid:<8} {p.path}")
    return 0


def _project_rollup(repo: Path) -> tuple[int, int, str]:
    log = repo / ".peers" / "log" / "runs.jsonl"
    ticks = 0
    last = "-"
    if log.is_file():
        try:
            with open_text_read_no_symlink(log) as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("event") != "exit":
                        ticks += 1
                    if entry.get("ts"):
                        last = str(entry["ts"])
        except OSError:
            pass
    try:
        from peers.bug_hunt import summarize
        blocking = summarize(repo).open_blocking_count
    except Exception:
        blocking = 0
    return ticks, blocking, last


def _load_dashboard_state(repo: Path) -> dict:
    path = repo / ".peers" / "state.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(
            read_text_no_symlink(
                path, max_bytes=_DASHBOARD_STATE_MAX_BYTES + 1
            )
        )
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _dashboard_soft_goal_passed(goal, status: dict, n_peers: int) -> bool:
    mode = goal.reviewer or "other"
    if mode == "quorum":
        if not goal.quorum_num or not goal.quorum_den:
            return False
        recent = status.get("history", [])[-goal.quorum_den:]
        if len(recent) < goal.quorum_den:
            return False
        return sum(1 for entry in recent if entry.get("pass")) >= goal.quorum_num
    if mode == "both":
        per_peer = status.get("per_peer", {})
        reviewers_needed = max(n_peers, 1)
        sufficient = sum(
            1 for entry in per_peer.values()
            if entry.get("consensus_count", 0) >= goal.consensus_needed
        )
        return sufficient >= reviewers_needed
    return status.get("consensus_count", 0) >= goal.consensus_needed


def _dashboard_goal_counts(repo: Path) -> tuple[str, str]:
    goals_path = repo / ".peers" / "goals.yaml"
    if not goals_path.exists():
        return "-", "-"
    try:
        from peers.goals import load_goals
        goals = load_goals(goals_path)
    except Exception:
        return "?", "?"
    state = _load_dashboard_state(repo)
    goals_status = state.get("goals_status", {})
    if not isinstance(goals_status, dict):
        goals_status = {}
    soft_status = state.get("soft_status", {})
    if not isinstance(soft_status, dict):
        soft_status = {}
    peer_order = state.get("peer_order", [])
    n_peers = len(peer_order) if isinstance(peer_order, list) else 0
    hard_open = 0
    soft_open = 0
    for goal in goals:
        if goal.type == "hard":
            status = goals_status.get(goal.id, {})
            if not isinstance(status, dict) or status.get("state") != "pass":
                hard_open += 1
        elif goal.type == "soft":
            status = soft_status.get(goal.id, {})
            if not isinstance(status, dict):
                status = {}
            if not _dashboard_soft_goal_passed(goal, status, n_peers):
                soft_open += 1
    return str(hard_open), str(soft_open)


def _dashboard_container_name(project: Project) -> str:
    if project.state != "running" or not project.notes:
        return "-"
    for token in project.notes.split():
        if token.startswith("container_name="):
            return token.split("=", 1)[1] or "-"
    return "-"


def cmd_dashboard(config_dir: Path | None = None) -> int:
    store = _store(config_dir)
    reconcile(store)
    projects = store.list_projects()
    if not projects:
        print("(no projects registered)")
        return 0
    rows: list[tuple[str, ...]] = [
        (
            "NAME", "STATE", "TICKS", "HARD_OPEN", "SOFT_OPEN",
            "BLOCKING", "CONTAINER", "LAST",
        )
    ]
    for project in projects:
        repo = Path(project.path)
        ticks, blocking, last = _project_rollup(repo)
        hard_open, soft_open = _dashboard_goal_counts(repo)
        rows.append((
            project.name, project.state, str(ticks), hard_open, soft_open,
            str(blocking), _dashboard_container_name(project), last,
        ))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    return 0


def _md_cell(value: object) -> str:
    text = str(value).replace("\n", " ")
    return text.replace("|", "\\|")


def _readme_status(repo: Path) -> str:
    readme = repo / "README.md"
    if readme.is_symlink():
        return f"unsafe symlink: {readme}"
    if readme.is_file():
        return f"present: {readme}"
    return "missing"


def _render_controller_report(store: Store, projects: list[Project],
                              name: str | None) -> tuple[list[str], list[str]]:
    generated = _dt.datetime.now(_dt.timezone.utc).isoformat()
    scope = name if name is not None else "all projects"
    out = [
        "# peers-ctl report",
        "",
        f"Generated: {generated}",
        f"Config dir: {store.config_dir}",
        f"Scope: {scope}",
        "",
        "## Projects",
        "",
        "| Project | State | Ticks | Blocking | Last tick | README | Controller log |",
        "|---|---:|---:|---:|---|---|---|",
    ]
    errors: list[str] = []
    for project in projects:
        repo = Path(project.path)
        ticks, blocking, last = _project_rollup(repo)
        try:
            log_path = store.ensure_controller_log_file(project)
            log_cell = str(log_path)
        except (OSError, ValueError) as e:
            log_cell = f"unsafe: {e}"
            errors.append(f"{project.name}: {e}")
        out.append(
            "| "
            + " | ".join(
                _md_cell(v) for v in (
                    project.name,
                    project.state,
                    ticks,
                    blocking,
                    last,
                    _readme_status(repo),
                    log_cell,
                )
            )
            + " |"
        )
    if errors:
        out += ["", "## Report warnings", ""]
        for err in errors:
            out.append(f"- {err}")
    out += [
        "",
        "## Operator notes",
        "",
        "- Project logs are controller-owned files under the config dir.",
        "- Per-tick run logs live in each project at `.peers/log/runs.jsonl`.",
        "- Missing or unsafe README entries should be fixed before handoff.",
    ]
    return out, errors


def cmd_report(name: str | None = None,
               config_dir: Path | None = None) -> int:
    """Write a controller Markdown report to the peers-ctl config dir."""
    store = _store(config_dir)
    reconcile(store)
    try:
        if name is not None:
            validate_project_name(name)
    except ValueError as e:
        print(f"peers-ctl: {e}", file=sys.stderr)
        return 2
    projects = store.list_projects()
    if name is not None:
        projects = [p for p in projects if p.name == name]
        if not projects:
            print(f"peers-ctl: no such project: {name}", file=sys.stderr)
            return 1
    if not projects:
        print("peers-ctl: no projects registered", file=sys.stderr)
        return 1
    report, errors = _render_controller_report(store, projects, name)
    filename = "REPORT.md" if name is None else f"REPORT-{name}.md"
    try:
        with open_text_in_dir_no_symlink(store.config_dir, filename, "w") as f:
            f.write("\n".join(report) + "\n")
    except (OSError, ValueError) as e:
        print(f"peers-ctl: cannot write report safely: {e}", file=sys.stderr)
        return 1
    print(f"wrote {store.config_dir / filename}")
    return 1 if errors else 0


def cmd_start(name: str, max_ticks: int | None = None,
              max_usd: float | None = None,
              max_runtime: str | None = None,
              reset_budget: bool = False,
              force: bool = False,
              container: bool = False,
              config_dir: Path | None = None,
              without_recon: bool = False,
              without_post_convergence_skeptic: bool = False,
              checkpoint: bool = False) -> int:
    store = _store(config_dir)
    reconcile(store)
    p = store.get(name)
    if p is None:
        print(f"peers-ctl: no such project: {name}", file=sys.stderr)
        return 1
    # Task 4.5: --checkpoint writes a marker the driver picks up to
    # pause after Phase 0 (architecture → implementation transition).
    # Done before start_project() so the marker is in place by the
    # time the first tick fires. Best-effort — if `.peers/` doesn't
    # exist (mis-scaffolded project), let start_project() fail with
    # its own clearer error rather than masking it here.
    if checkpoint:
        plan_dir = Path(p.path) / ".peers"
        if plan_dir.is_dir():
            try:
                write_text_no_symlink(
                    plan_dir / "checkpoint_requested",
                    f"requested via `peers-ctl start {name} --checkpoint`\n"
                    "driver will exit after Phase 0 architecture tick; "
                    "clear with `peers-ctl resume <project>`.\n",
                )
            except OSError as e:
                print(f"peers-ctl: cannot write checkpoint marker: {e}",
                      file=sys.stderr)
                return 1
    extras: list[str] = []
    if without_recon:
        extras.append("--without-recon")
    if without_post_convergence_skeptic:
        extras.append("--without-post-convergence-skeptic")
    extra_args: tuple[str, ...] = tuple(extras)
    # `--max-runtime DURATION` is parsed at the CLI boundary so the
    # operator sees the flag name in the error, not an internal helper.
    # Leading `+` (Task 7.4) means "add to current max_runtime_s"
    # rather than "replace"; the additive computation runs here so
    # the value handed to start_project() is already the absolute
    # target, keeping the runner's contract unchanged.
    max_runtime_s: int | None = None
    if max_runtime is not None:
        try:
            delta_s, additive = parse_runtime_duration(max_runtime)
        except ValueError as e:
            print(f"peers-ctl: --max-runtime: {e}", file=sys.stderr)
            return 1
        if additive:
            current = 0
            state_path = Path(p.path) / ".peers" / "state.json"
            if state_path.is_file():
                try:
                    state_data = json.loads(state_path.read_text())
                except (OSError, ValueError):
                    state_data = {}
                if isinstance(state_data, dict):
                    budget = state_data.get("budget") or {}
                    if isinstance(budget, dict):
                        cur_val = budget.get("max_runtime_s")
                        if isinstance(cur_val, int) and cur_val > 0:
                            current = cur_val
            max_runtime_s = current + delta_s
        else:
            max_runtime_s = delta_s
    try:
        pid = start_project(store, p, max_ticks=max_ticks,
                            max_usd=max_usd,
                            max_runtime_s=max_runtime_s,
                            reset_budget=reset_budget,
                            force=force,
                            container=container,
                            extra_args=extra_args)
    except (RuntimeError, ValueError) as e:
        print(f"peers-ctl: {e}", file=sys.stderr)
        return 1
    mode = "container" if container else "host"
    print(f"Started '{name}' ({mode}, pid {pid}); log → {p.log_path}")
    return 0


def cmd_stop(name: str, grace_s: float = 10.0,
             config_dir: Path | None = None) -> int:
    store = _store(config_dir)
    reconcile(store)
    p = store.get(name)
    if p is None:
        print(f"peers-ctl: no such project: {name}", file=sys.stderr)
        return 1
    try:
        stop_project(store, p, grace_s=grace_s)
    except (RuntimeError, ValueError, OSError) as e:
        print(f"peers-ctl: {e}", file=sys.stderr)
        return 1
    print(f"Stopped '{name}'")
    return 0


def cmd_status(name: str | None = None,
               config_dir: Path | None = None) -> int:
    store = _store(config_dir)
    reconcile(store)
    if name is None:
        return cmd_list(config_dir)
    p = store.get(name)
    if p is None:
        print(f"peers-ctl: no such project: {name}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(p), indent=2, sort_keys=True))
    # Also surface the embedded `peers status` if .peers/ is set up.
    inner = Path(p.path) / ".peers"
    if (inner / "state.json").exists():
        print()
        print("--- peers status ---")
        subprocess.run(
            ["peers", "-C", p.path, "status"],
            check=False,
        )
    return 0


def cmd_review(name: str, config_dir: Path | None = None) -> int:
    """Print the latest handoff commit's Self-Review section."""
    store = _store(config_dir)
    p = store.get(name)
    if p is None:
        print(f"peers-ctl: no such project: {name}", file=sys.stderr)
        return 1
    repo = Path(p.path)
    r = subprocess.run(
        ["git", "-C", str(repo), "log",
         "--grep=^Peer-Status: handoff$",
         "-n", "1", "--format=%H%n%s%n---%n%b"],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        print(f"peers-ctl: no handoff commit found in {repo}")
        return 1
    print(r.stdout)
    return 0


def cmd_logs(name: str, lines: int = 50,
             config_dir: Path | None = None) -> int:
    store = _store(config_dir)
    p = store.get(name)
    if p is None or not p.log_path:
        print(f"peers-ctl: no log for {name}", file=sys.stderr)
        return 1
    try:
        log = store.safe_log_path_for(p)
    except ValueError as e:
        print(f"peers-ctl: cannot read log: {e}", file=sys.stderr)
        return 1
    if not log.exists():
        print(f"peers-ctl: log not yet written: {log}", file=sys.stderr)
        return 1
    if lines <= 0:
        print("peers-ctl: --lines must be positive", file=sys.stderr)
        return 2
    try:
        with open_text_read_no_symlink(log) as f:
            tail = deque(f, maxlen=lines)
    except OSError as e:
        print(f"peers-ctl: cannot read log: {e}", file=sys.stderr)
        return 1
    print("".join(tail), end="" if tail else "\n")
    return 0


def cmd_tail(name: str, config_dir: Path | None = None) -> int:
    store = _store(config_dir)
    p = store.get(name)
    if p is None or not p.log_path:
        print(f"peers-ctl: no log for {name}", file=sys.stderr)
        return 1
    try:
        log = store.safe_log_path_for(p)
    except ValueError as e:
        print(f"peers-ctl: cannot read log: {e}", file=sys.stderr)
        return 1
    if not log.exists():
        print(f"peers-ctl: log not yet written: {log}", file=sys.stderr)
        return 1
    try:
        with open_text_read_no_symlink(log) as f:
            tail = deque(f, maxlen=20)
            if tail:
                print("".join(tail), end="")
                sys.stdout.flush()
            while True:
                line = f.readline()
                if line:
                    print(line, end="")
                    sys.stdout.flush()
                else:
                    time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    except OSError as e:
        print(f"peers-ctl: cannot read log: {e}", file=sys.stderr)
        return 1


def cmd_prune(older_than_days: int = 7,
              config_dir: Path | None = None) -> int:
    store = _store(config_dir)
    reconcile(store)
    n = prune_logs(store, older_than_days=older_than_days)
    print(f"peers-ctl: pruned {n} log files older than "
          f"{older_than_days} days")
    return 0


def cmd_modes_list() -> int:
    """Tabular list of all modes visible via `peers.modes.discover()`.

    Read-only; mirrors `peers-ctl list` style (NAME / VER / SOURCE /
    DESCRIPTION columns). User modes shadow built-in modes by name,
    so the source column tells you which copy `peers init --modes=...`
    will pick up.
    """
    from peers.modes import discover
    modes = discover()
    if not modes:
        print("(no modes available)")
        return 0
    print(f"{'NAME':16}  {'VER':5}  {'SOURCE':8}  DESCRIPTION")
    for name in sorted(modes):
        m = modes[name]
        print(f"{m.name:16}  v{m.version:<4}  {m.source:8}  {m.description}")
    return 0


def cmd_modes_show(name: str) -> int:
    """Dump `<mode>/mode.yaml`, `goals.yaml`, and list `checks/`.

    Debug-print only — used to inspect what `peers init --modes=<name>`
    would copy in. Reads come from inside the package or from
    `$PEERS_MODES_DIR`, both trusted, so plain `read_text()` is fine.
    """
    from peers.modes import discover
    modes = discover()
    m = modes.get(name)
    if m is None:
        print(f"peers-ctl: unknown mode {name!r}; available: "
              f"{sorted(modes)}", file=sys.stderr)
        return 1
    print(f"# {m.path}")
    print(f"# source: {m.source}")
    print((m.path / "mode.yaml").read_text())
    print("---")
    print((m.path / "goals.yaml").read_text())
    cdir = m.path / "checks"
    if cdir.is_dir():
        print(f"# checks/: "
              f"{sorted(p.name for p in cdir.iterdir() if p.is_file())}")
    return 0


def cmd_doctor(config_dir: Path | None = None) -> int:
    """Pre-flight check: verify the host has everything `peers-ctl
    start` needs, and that each registered project's config can be
    loaded. Exits 0 on a clean bill of health, 1 otherwise.
    """
    import shutil as _shutil
    problems: list[str] = []
    warnings: list[str] = []

    # Toolchain — the substrate itself
    if _shutil.which("peers") is None:
        problems.append(
            "`peers` is not on PATH — `peers-ctl start` will fail. "
            "Install the substrate (`pip install -e .` from the repo)."
        )
    if _shutil.which("git") is None:
        problems.append("`git` is not on PATH — the peers loop needs it.")

    # Peer CLIs — we can only WARN here because they're configured
    # per-project. Surface the most common ones.
    for name in ("claude", "codex"):
        if _shutil.which(name) is None:
            # codex often ships with the VSCode ChatGPT extension and
            # isn't on PATH; look there as a fallback so the warning
            # can suggest a concrete fix.
            hint = ""
            if name == "codex":
                from pathlib import Path as _P
                for ext in (_P.home() / ".vscode-oss" / "extensions",
                            _P.home() / ".vscode" / "extensions"):
                    if not ext.is_dir():
                        continue
                    try:
                        matches = sorted(ext.glob(
                            "openai.chatgpt-*/bin/linux-x86_64/codex"
                        )) + sorted(ext.glob(
                            "openai.chatgpt-*/bin/darwin-arm64/codex"
                        ))
                    except OSError:
                        matches = []
                    if matches:
                        hint = (
                            f" Found {matches[-1]} — point "
                            "config.yaml's codex argv at it."
                        )
                        break
            warnings.append(
                f"`{name}` is not on PATH. If any project uses it, "
                f"either add it to PATH or set the full path in "
                f"that project's .peers/config.yaml — or use "
                f"`peers-ctl start --container` to run the loop "
                f"inside the peers:dev image." + hint
            )

    # Container path — not a problem if you're not using --container,
    # but flag the state so users know.
    podman = _shutil.which("podman")
    if podman is None:
        warnings.append(
            "`podman` is not on PATH; `peers-ctl start --container` "
            "won't work. Install podman or stick to the host path."
        )
    else:
        # Check if the image is present (best-effort).
        from peers_ctl.runner import CONTAINER_IMAGE
        try:
            r = subprocess.run(
                [podman, "image", "exists", CONTAINER_IMAGE],
                capture_output=True, timeout=10,
            )
            if r.returncode != 0:
                warnings.append(
                    f"`podman` is available but image "
                    f"{CONTAINER_IMAGE!r} is not built yet. "
                    f"Run `make build` (or `podman build -t "
                    f"{CONTAINER_IMAGE} .`) before "
                    f"`peers-ctl start --container`."
                )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    # Per-project config + goals can be loaded
    store = _store(config_dir)
    reconcile(store)
    projects = store.list_projects()
    print(f"peers-ctl doctor — {len(projects)} project(s) registered, "
          f"config dir {store.config_dir}")
    root = projects_root()
    root_default = root == _DEFAULT_PROJECTS_ROOT.resolve()
    print(
        f"  projects root: {root}"
        + ("" if root_default
           else " (via PEERS_PROJECTS_ROOT)")
    )
    print()
    for p in projects:
        ok = True
        msgs: list[str] = []
        cfg_path = Path(p.path) / ".peers" / "config.yaml"
        goals_path = Path(p.path) / ".peers" / "goals.yaml"
        if not cfg_path.exists():
            ok = False
            msgs.append(f"missing {cfg_path}")
        if not goals_path.exists():
            ok = False
            msgs.append(f"missing {goals_path}")
        if ok:
            try:
                from peers.cli import _load_config_yaml, _validate_config
                from peers.peer_spec import load_peer_specs
                from peers.goals import load_goals
                cfg = _load_config_yaml(cfg_path)
                err = _validate_config(cfg, cfg_path)
                if err is not None:
                    raise ValueError(err)
                specs = load_peer_specs(cfg)
                goals = load_goals(goals_path)
                msgs.append(
                    f"{len(specs)} peer(s), {len(goals)} goal(s)"
                )
            except Exception as e:
                ok = False
                msgs.append(f"config/goals load error: {e}")
        sym = "ok" if ok else "FAIL"
        print(f"  [{sym}] {p.name:<20} {p.path}")
        for m in msgs:
            print(f"           {m}")

    if warnings:
        print()
        print("Warnings:")
        for w in warnings:
            print(f"  - {w}")
    if problems:
        print()
        print("Problems:")
        for p_ in problems:
            print(f"  - {p_}")
        return 1
    return 0


_HELP_MAN_HINT = "\n(use --help-man for detailed docs + examples)"


def _add_help_man_subparser(sub, name: str, help_text: str | None = None,
                            **kwargs):
    description = (help_text or "") + _HELP_MAN_HINT
    p = sub.add_parser(name, help=help_text, description=description,
                       **kwargs)
    attach_help_man_flags(p)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    from peers_ctl import __version__ as _peers_ctl_version
    parser = argparse.ArgumentParser(
        prog="peers-ctl",
        description=(
            "Multi-project controller for peers loops." + _HELP_MAN_HINT
        ),
    )
    parser.add_argument(
        "--version", action="version",
        version=f"peers-ctl {_peers_ctl_version}",
    )
    parser.add_argument(
        "--config-dir", type=Path, default=None,
        help="alternate config directory (default: "
             "$XDG_CONFIG_HOME/peers-ctl or ~/.config/peers-ctl)",
    )
    attach_help_man_flags(parser)
    sub = parser.add_subparsers(dest="cmd", required=False)

    p_add = _add_help_man_subparser(
        sub, "add", help_text="register a target project")
    p_add.add_argument("path", type=Path)
    p_add.add_argument("--name", default=None,
                       help="override name (default: directory basename)")

    p_new = _add_help_man_subparser(
        sub, "new",
        help_text=(
            "one-shot scaffold: mkdir + git init + peers init + register. "
            "After this, `peers-ctl start <name>` works immediately."
        ),
    )
    p_new.add_argument("path", type=Path,
                       help="target directory (created if missing)")
    p_new.add_argument("--name", default=None,
                       help="project name (default: directory basename)")
    p_new.add_argument("--spec", default=None,
                       help="SPEC.md content or path to a file with that content")
    p_new.add_argument("--driver", choices=("orchestrator", "hooks", "sessions"),
                       default="orchestrator")
    p_new.add_argument("--force", action="store_true",
                       help="scaffold into a non-empty dir / overwrite registry entry")
    p_new.add_argument(
        "--container", action="store_true",
        help="run `peers init` inside the peers:dev container "
             "(host needs only podman; no `peers` binary required)",
    )
    p_new.add_argument(
        "--modes",
        default=None,
        help="comma-separated mode names (e.g. audit,security); see "
             "`peers-ctl modes list`",
    )
    p_new.add_argument(
        "--audit-templates", action="store_true",
        help="DEPRECATED: alias for --modes=audit. "
             "Install audit check-scripts and pre-wired audit goals.",
    )
    p_new.add_argument(
        "--lang", default="python",
        help=(
            "audit-template language: python, js, rust, or go; "
            "unknown falls back"
        ),
    )
    p_new.add_argument(
        "--plan", default=None,
        help=(
            "PLAN.md file for implement-mode (Task 1.3). Required when "
            "--modes contains `implement`; mutually exclusive with --spec. "
            "The file is validated, its acceptance command is run as a "
            "preflight (must currently FAIL — use --force to override), "
            "and the file is copied to <project>/PLAN.md plus frozen as "
            "<project>/.peers/PLAN.original.md with SHA-pinned acceptance.sh."
        ),
    )

    p_ack = _add_help_man_subparser(
        sub, "ack-block",
        help_text=(
            "implement-mode (Task 7.3): transition a step's "
            "`[BLOCKED]` marker to `[BLOCKED-ACK]` in PLAN.md. "
            "Operator-supervised escape valve for steps the loop "
            "marked blocked but the operator has decided to skip "
            "or defer. Logs a hash-chained audit entry to "
            ".peers/blocks.log."
        ),
    )
    p_ack.add_argument("project_name",
                       help="implement-mode project name (bare name "
                            "under $PEERS_PROJECTS_ROOT)")
    p_ack.add_argument("step_id", metavar="STEP-N",
                       help="step id to acknowledge (must currently "
                            "carry the `[BLOCKED]` marker)")
    p_ack.add_argument("--reason", required=True, metavar="TEXT",
                       help="human-readable reason for acknowledging "
                            "the block (recorded verbatim in blocks.log)")

    p_amend = _add_help_man_subparser(
        sub, "amend",
        help_text=(
            "implement-mode: re-pin the frozen acceptance command "
            "(operator-supervised legitimate escape). Appends a "
            "hash-chained audit entry to .peers/contracts.log."
        ),
    )
    p_amend.add_argument("project_name",
                         help="implement-mode project name (bare name "
                              "under $PEERS_PROJECTS_ROOT)")
    p_amend.add_argument("--acceptance", required=True,
                         metavar="COMMAND",
                         help="new shell command to pin as the "
                              "acceptance gate (replaces the body of "
                              ".peers/contracts/acceptance.sh)")
    p_amend.add_argument("--reason", required=True,
                         metavar="TEXT",
                         help="human-readable reason for the change "
                              "(recorded verbatim in contracts.log)")

    p_rm = _add_help_man_subparser(
        sub, "remove", help_text="unregister a project")
    p_rm.add_argument("name")

    _add_help_man_subparser(
        sub, "list", help_text="list all projects + state")
    _add_help_man_subparser(
        sub, "dashboard",
        help_text="rollup view across all registered projects")

    p_report = _add_help_man_subparser(
        sub, "report",
        help_text="write a Markdown controller report under the config dir",
    )
    p_report.add_argument("name", nargs="?", default=None)

    p_start = _add_help_man_subparser(
        sub, "start", help_text="start a project loop")
    p_start.add_argument("name")
    p_start.add_argument("--max-ticks", type=int, default=None)
    p_start.add_argument("--max-usd", type=float, default=None)
    p_start.add_argument(
        "--max-runtime", type=str, default=None,
        metavar="DURATION",
        help="override budget.max_runtime_s for this session. "
             "Accepts a bare integer (seconds) or a unit suffix: "
             "300s, 90m, 6h, 2d, 1w. A leading `+` (e.g. `+6h`) "
             "ADDS the duration to the project's current "
             "max_runtime_s instead of replacing it — useful for "
             "extending a near-exhausted run. Written to "
             ".peers/state.json before the loop starts; persists "
             "until changed again or until .peers/config.yaml is "
             "re-applied.",
    )
    p_start.add_argument(
        "--reset-budget", action="store_true",
        help="zero out spent_runtime_s / spent_iterations / "
             "spent_tokens / spent_usd / wasted_runtime_s / "
             "consecutive_failures in .peers/state.json before "
             "starting — semantically a 'fresh session' on top "
             "of the existing project state. Combines with "
             "--max-runtime to also bump the cap.",
    )
    p_start.add_argument(
        "--force", action="store_true",
        help="skip the pre-flight 'budget already exhausted' "
             "abort. The loop will exit after 0 ticks with the "
             "`budget:max_runtime` sentinel — useful when the "
             "operator wants to record that sentinel state but "
             "knows the project is done.",
    )
    p_start.add_argument(
        "--container", action="store_true",
        help="run the loop inside the peers:dev podman image "
             "(mounts target + ~/.claude + ~/.codex). Use when "
             "codex isn't installed on the host but is inside "
             "the image.",
    )
    p_start.add_argument(
        "--without-recon", action="store_true",
        help="skip the substrate pre-tick recon step that writes "
             ".peers/recon.md with a static project digest. Recon "
             "is free and fast (substrate-only, no LLM call) and is "
             "on by default; only opt out if recon.md is hand-prepared "
             "or explicitly unwanted.",
    )
    p_start.add_argument(
        "--without-post-convergence-skeptic", action="store_true",
        help="skip the auto-skeptic re-audit tick that fires when "
             "convergence-reached is about to declare terminal "
             "success. By default the substrate runs ONE extra "
             "tick with a critical-re-audit prompt; opt out for runs "
             "where false-convergence is acceptable (e.g. CI).",
    )
    p_start.add_argument(
        "--checkpoint", action="store_true",
        help="implement-mode only (Task 4.5): pause the loop after "
             "Phase 0 prep (recon → alignment → architecture) so the "
             "operator can review RECON.md + PLAN.aligned.md + "
             "ARCHITECTURE.intended.md before any implementation "
             "commits land. Writes a .peers/checkpoint_requested "
             "marker the driver picks up; exit sentinel is "
             "`checkpoint:phase-0-complete`. Clear with "
             "`peers-ctl resume <project>` then re-launch with "
             "`peers-ctl start <project>`. Default: OFF (autonomous).",
    )

    p_resume = _add_help_man_subparser(
        sub, "resume",
        help_text=(
            "implement-mode (Task 4.5): clear a project's Phase-0 "
            "checkpoint marker so the next `peers-ctl start <project>` "
            "proceeds past Phase 0 into implementation. Idempotent."
        ),
    )
    p_resume.add_argument(
        "project_name",
        help="implement-mode project name (bare name under "
             "$PEERS_PROJECTS_ROOT)",
    )

    p_stop = _add_help_man_subparser(
        sub, "stop", help_text="stop a project loop")
    p_stop.add_argument("name")
    p_stop.add_argument("--grace-s", type=float, default=10.0)

    p_status = _add_help_man_subparser(
        sub, "status", help_text="status of one or all projects")
    p_status.add_argument("name", nargs="?", default=None)

    p_review = _add_help_man_subparser(
        sub, "review", help_text="show latest handoff self-review")
    p_review.add_argument("name")

    p_logs = _add_help_man_subparser(
        sub, "logs", help_text="print last N log lines")
    p_logs.add_argument("name")
    p_logs.add_argument("-n", "--lines", type=int, default=50)

    p_tail = _add_help_man_subparser(
        sub, "tail", help_text="follow the project log")
    p_tail.add_argument("name")

    p_prune = _add_help_man_subparser(
        sub, "prune", help_text="delete old log files")
    p_prune.add_argument("--older-than-days", type=int, default=7)

    _add_help_man_subparser(
        sub, "doctor",
        help_text=(
            "pre-flight check: verify peers + git + peer CLIs are "
            "on PATH and each registered project's config loads."
        ),
    )

    p_modes = _add_help_man_subparser(
        sub, "modes", help_text="inspect available audit modes")
    modes_sub = p_modes.add_subparsers(dest="modes_cmd", required=False)
    modes_sub.add_parser("list", help="list available modes")
    p_modes_show = modes_sub.add_parser(
        "show", help="show a mode's mode.yaml + goals.yaml + checks/")
    p_modes_show.add_argument("name")

    args = parser.parse_args(argv)

    # Dispatch --help-man before any normal cmd handling.
    if getattr(args, "help_man", False):
        subcmd = None
        if args.cmd == "modes":
            subcmd = getattr(args, "modes_cmd", None)
        return print_help_man("peers-ctl", args.cmd, subcmd,
                              pick_lang(args))

    if args.cmd is None:
        parser.error("the following arguments are required: cmd")
    cd = args.config_dir
    if args.cmd == "add":
        return cmd_add(args.name, args.path, cd)
    if args.cmd == "new":
        if args.modes is not None:
            modes = [m.strip() for m in args.modes.split(",") if m.strip()]
            if not modes:
                print(f"peers-ctl: --modes value {args.modes!r} parsed to an "
                      "empty list (only whitespace/commas?); did you mean to "
                      "pass at least one mode name?", file=sys.stderr)
                return 2
        else:
            modes = None
        return cmd_new(args.path, args.name, args.spec,
                       args.force, args.driver, args.container,
                       modes=modes,
                       audit_templates=args.audit_templates,
                       lang=args.lang,
                       plan=args.plan,
                       config_dir=cd)
    if args.cmd == "ack-block":
        return cmd_ack_block(args.project_name, args.step_id, args.reason)
    if args.cmd == "amend":
        return cmd_amend(args.project_name, args.acceptance, args.reason)
    if args.cmd == "remove":
        return cmd_remove(args.name, cd)
    if args.cmd == "list":
        return cmd_list(cd)
    if args.cmd == "dashboard":
        return cmd_dashboard(cd)
    if args.cmd == "report":
        return cmd_report(args.name, cd)
    if args.cmd == "start":
        return cmd_start(args.name, args.max_ticks, args.max_usd,
                         max_runtime=args.max_runtime,
                         reset_budget=args.reset_budget,
                         force=args.force,
                         container=args.container,
                         config_dir=cd,
                         without_recon=args.without_recon,
                         without_post_convergence_skeptic=(
                             args.without_post_convergence_skeptic
                         ),
                         checkpoint=args.checkpoint)
    if args.cmd == "resume":
        return cmd_resume(args.project_name)
    if args.cmd == "stop":
        return cmd_stop(args.name, args.grace_s, cd)
    if args.cmd == "status":
        return cmd_status(args.name, cd)
    if args.cmd == "review":
        return cmd_review(args.name, cd)
    if args.cmd == "logs":
        return cmd_logs(args.name, args.lines, cd)
    if args.cmd == "tail":
        return cmd_tail(args.name, cd)
    if args.cmd == "prune":
        return cmd_prune(args.older_than_days, cd)
    if args.cmd == "doctor":
        return cmd_doctor(cd)
    if args.cmd == "modes":
        if not getattr(args, "modes_cmd", None):
            parser.error("modes: choose one of: list, show "
                         "(or use --help-man)")
        if args.modes_cmd == "list":
            return cmd_modes_list()
        if args.modes_cmd == "show":
            return cmd_modes_show(args.name)
    return 2


if __name__ == "__main__":
    sys.exit(main())
