"""peers-ctl CLI entrypoint."""
from __future__ import annotations

import argparse
import hashlib
from collections import deque
import datetime as _dt
import json
import os
import re
import shutil
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
    reconcile, reconcile_one,
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


_KNOWN_TEMPLATES = ("internal testing",)

_PLACEHOLDER_SELF_AUDIT_SPEC = """\
# peers — Specification (Self-Audit Snapshot, placeholder)

This SPEC.md is the **root-level** anchor required by the
`threat-model-present` hard gate. It was generated as a placeholder
by `peers-ctl new --template internal testing` because no previous
`peers-internal testing-v*` sibling directory was found to copy from.

Replace this body with the real spec before the first audit tick — the
substrate internal testing needs an honest threat model to do useful work.
The expected sections are:

- Purpose, functional scope, non-goals
- Threat model (actors + capabilities, trust boundaries, mitigations)
- A link to `docs/ATTACK-SURFACE.md` for the per-surface analysis
- A link to `docs/SECURITY.md` for the runtime defense playbook
"""

_PLACEHOLDER_SELF_AUDIT_ATTACK_SURFACE = """\
# peers — Attack Surface (placeholder)

This file is the anchor for the `attack-surface-enumerated` hard gate.
It was generated as a placeholder by `peers-ctl new --template
internal testing` because no previous `peers-internal testing-v*` sibling
directory was found to copy from.

Each `##` section below should describe one entry-point:
- Where attacker-controlled input enters the system
- Which trust boundary it crosses
- What defense is in place today
- Known gaps with links to tracking items

Suggested initial sections (replace the bodies with the real analysis
before the first audit tick):

## peers-ctl CLI (host)
TODO: untrusted input, trust boundary, defenses, known gaps.

## peers in-container orchestrator
TODO: untrusted input, trust boundary, defenses, known gaps.

## podman container + egress proxy
TODO: untrusted input, trust boundary, defenses, known gaps.
"""


def _find_latest_self_audit_anchor(target: Path) -> Path | None:
    """Find the highest-versioned `peers-internal testing-vN` sibling of
    ``target`` that has a SPEC.md (and is therefore usable as anchor
    source). Returns ``None`` if nothing eligible is found.

    The version compare is integer-on-N rather than string-on-name so
    `v10` sorts above `v9`.
    """
    parent = target.parent
    if not parent.is_dir():
        return None
    rx = re.compile(r"^peers-internal testing-v(\d+)$")
    candidates: list[tuple[int, Path]] = []
    try:
        for entry in parent.iterdir():
            if not entry.is_dir():
                continue
            m = rx.match(entry.name)
            if not m:
                continue
            # Skip if we're looking AT the target itself.
            if entry.resolve() == target.resolve():
                continue
            # Only consider as anchor source if it actually has SPEC.md.
            if not (entry / "SPEC.md").is_file():
                continue
            candidates.append((int(m.group(1)), entry))
    except OSError:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0], reverse=True)
    return candidates[0][1]


def _substrate_default_source() -> Path:
    """Return the operator's substrate-repo path for the internal testing
    template's default `--from`. The convention is whatever directory
    contains the running peers_ctl package — i.e. the same checkout
    the operator installed peers-ctl from."""
    return Path(__file__).resolve().parent.parent.parent


def _apply_self_audit_template(
    target: Path,
    *,
    template_from: Path | None,
    anchors_from: Path | None,
    force: bool,
) -> tuple[int, list[str] | None]:
    """Bootstrap the substrate internal testing project layout into ``target``.

    Performed BEFORE the existing `cmd_new` scaffold flow runs (so the
    cloned ``.git`` + branch + anchors are in place when `peers init`
    fires inside the directory).

    Returns ``(rc, implied_modes)``:
      - ``rc=0`` on success; ``implied_modes`` is the default mode
        list (``["audit", "thorough"]``) the caller should use unless
        the operator passed an explicit ``--modes``.
      - non-zero ``rc`` on failure; ``implied_modes`` is ``None`` and
        the error is already printed to stderr.
    """
    src = (template_from or _substrate_default_source()).expanduser().resolve()
    if not (src / ".git").is_dir():
        print(
            f"peers-ctl: --template internal testing: source {src} is not a "
            "git repo (use --from to point at the substrate checkout)",
            file=sys.stderr,
        )
        return 1, None

    if target.exists():
        try:
            existing = list(target.iterdir())
        except OSError as e:
            print(f"peers-ctl: cannot read {target}: {e}", file=sys.stderr)
            return 1, None
        if existing and not force:
            print(
                f"peers-ctl: --template internal testing: target {target} is "
                "non-empty (use --force to override)",
                file=sys.stderr,
            )
            return 2, None
        if existing and force:
            # The git-clone below refuses to write into a non-empty
            # destination — clear it first. We only delete entries we
            # owned; symlinks are explicitly refused (no surprise
            # follow-outside) and an error short-circuits the clone.
            for entry in existing:
                if entry.is_symlink():
                    print(
                        f"peers-ctl: refusing to remove symlink under "
                        f"target before clone: {entry}",
                        file=sys.stderr,
                    )
                    return 1, None
                try:
                    if entry.is_dir():
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
                except OSError as e:
                    print(
                        f"peers-ctl: cannot clear {entry}: {e}",
                        file=sys.stderr,
                    )
                    return 1, None
    else:
        target.parent.mkdir(parents=True, exist_ok=True)

    # 1. Clone substrate into target.
    clone = subprocess.run(
        ["git", "clone", "--quiet", str(src), str(target)],
        capture_output=True, text=True,
    )
    if clone.returncode != 0:
        print(
            f"peers-ctl: --template internal testing: git clone failed: "
            f"{clone.stderr.strip()}",
            file=sys.stderr,
        )
        return 1, None

    # 2. Checkout a branch named after the target dir basename.
    branch_name = target.name
    if branch_name.startswith("peers-internal testing-"):
        branch_name = "internal testing-" + branch_name[len("peers-internal testing-"):]
    co = subprocess.run(
        ["git", "checkout", "-q", "-b", branch_name],
        cwd=target, capture_output=True, text=True,
    )
    if co.returncode != 0:
        print(
            f"peers-ctl: --template internal testing: git checkout -b "
            f"{branch_name} failed: {co.stderr.strip()}",
            file=sys.stderr,
        )
        return 1, None

    # 3. Resolve anchor source and copy SPEC.md + docs/ATTACK-SURFACE.md.
    anchor_dir: Path | None = None
    if anchors_from is not None:
        anchor_dir = anchors_from.expanduser().resolve()
        if not anchor_dir.is_dir():
            print(
                f"peers-ctl: --anchors-from {anchor_dir} is not a directory",
                file=sys.stderr,
            )
            return 1, None
    else:
        anchor_dir = _find_latest_self_audit_anchor(target)

    spec_path = target / "SPEC.md"
    attack_path = target / "docs" / "ATTACK-SURFACE.md"
    attack_path.parent.mkdir(parents=True, exist_ok=True)

    if anchor_dir is not None and (anchor_dir / "SPEC.md").is_file():
        # safe_io's read_text_no_symlink + write_text_no_symlink keep
        # the symlink-TOCTOU defenses consistent with the rest of
        # cmd_new's scaffold path.
        try:
            spec_text = read_text_no_symlink(anchor_dir / "SPEC.md")
        except OSError as e:
            print(
                f"peers-ctl: cannot read anchor SPEC.md: {e}",
                file=sys.stderr,
            )
            return 1, None
        attack_src = anchor_dir / "docs" / "ATTACK-SURFACE.md"
        attack_text: str
        if attack_src.is_file():
            try:
                attack_text = read_text_no_symlink(attack_src)
            except OSError as e:
                print(
                    f"peers-ctl: cannot read anchor ATTACK-SURFACE.md: {e}",
                    file=sys.stderr,
                )
                return 1, None
        else:
            attack_text = _PLACEHOLDER_SELF_AUDIT_ATTACK_SURFACE
    else:
        spec_text = _PLACEHOLDER_SELF_AUDIT_SPEC
        attack_text = _PLACEHOLDER_SELF_AUDIT_ATTACK_SURFACE

    try:
        write_text_no_symlink(spec_path, spec_text)
        write_text_no_symlink(attack_path, attack_text)
    except OSError as e:
        print(
            f"peers-ctl: cannot write internal testing anchors safely: {e}",
            file=sys.stderr,
        )
        return 1, None

    # 4. Stage + commit the anchors on the new branch.
    subprocess.run(
        ["git", "add", "SPEC.md", "docs/ATTACK-SURFACE.md"],
        cwd=target, capture_output=True, check=False,
    )
    # Ensure local commit identity if the operator's gitconfig is absent.
    for k, v in (("user.email", "you@local"), ("user.name", "you")):
        cur = subprocess.run(
            ["git", "config", "--get", k],
            cwd=target, capture_output=True, text=True,
        )
        if cur.returncode != 0:
            subprocess.run(
                ["git", "config", k, v],
                cwd=target, capture_output=True, check=False,
            )
    commit = subprocess.run(
        ["git", "commit", "-q", "-m",
         f"chore({branch_name}): seed internal testing anchors"],
        cwd=target, capture_output=True, text=True,
    )
    if commit.returncode != 0:
        # If there's literally nothing to commit (e.g. anchors copied
        # bit-identical to what was already on disk from clone), the
        # commit step is a soft no-op — don't fail the template.
        if "nothing to commit" not in commit.stdout + commit.stderr:
            print(
                f"peers-ctl: --template internal testing: anchor commit failed: "
                f"{commit.stderr.strip()}",
                file=sys.stderr,
            )
            return 1, None

    return 0, ["audit", "thorough"]


def cmd_new(path: Path, name: str | None = None,
            spec: str | None = None, force: bool = False,
            driver: str = "orchestrator",
            container: bool = False,
            modes: list[str] | None = None,
            audit_templates: bool = False,   # legacy alias
            lang: str = "python",
            plan: str | None = None,
            template: str | None = None,
            template_from: Path | None = None,
            anchors_from: Path | None = None,
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
    if template is not None and template not in _KNOWN_TEMPLATES:
        print(
            f"peers-ctl: unknown --template {template!r}; "
            f"known: {', '.join(_KNOWN_TEMPLATES)}",
            file=sys.stderr,
        )
        return 2
    # --template internal testing: bootstrap the substrate internal testing layout
    # (clone + branch + anchors + commit) BEFORE the rest of the
    # scaffold runs. After the helper returns, the rest of cmd_new sees
    # a non-empty git repo at `target` and must proceed with --force.
    if template == "internal testing":
        rc, implied_modes = _apply_self_audit_template(
            target,
            template_from=template_from,
            anchors_from=anchors_from,
            force=force,
        )
        if rc != 0:
            return rc
        if implied_modes and not modes:
            modes = implied_modes
        # `peers init` runs after this; treat the cloned dir as
        # "force-re-scaffold" so the non-empty-dir check downstream
        # doesn't refuse.
        force = True
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
        # Refuse minor/patch drift for audit-integrity modes BEFORE init —
        # `peers init` inside an outdated image silently writes stale config
        # (Bug D — v12 hit this: image 1.5.0, host 1.6.0, stream-json default lost).
        try:
            from peers_ctl.runner import enforce_container_drift_for_modes
            level, drift_msg = enforce_container_drift_for_modes(init_modes)
            if level == "warn" and drift_msg:
                print(f"peers-ctl: warning: {drift_msg}", file=sys.stderr)
        except RuntimeError as e:
            print(f"peers-ctl new: {e}", file=sys.stderr)
            return 1
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


def _resolve_project_dir(name: str, config_dir: Path | None = None) -> Path:
    project = _store(config_dir).get(name)
    if project is not None:
        return Path(project.path)
    return projects_root() / name


def _read_project_budget_cap(proj_dir: Path) -> int:
    state_path = proj_dir / ".peers" / "state.json"
    if not state_path.is_file():
        return 0
    try:
        data = json.loads(state_path.read_text())
    except (OSError, ValueError):
        return 0
    budget = data.get("budget") if isinstance(data, dict) else None
    if not isinstance(budget, dict):
        return 0
    cap = budget.get("max_runtime_s")
    return cap if isinstance(cap, int) and cap > 0 else 0


def _apply_resume_budget(
    proj_dir: Path,
    *,
    max_runtime: str | None = None,
    reset_budget: bool = False,
) -> int | None:
    if max_runtime is None and not reset_budget:
        return None
    state_path = proj_dir / ".peers" / "state.json"
    if not state_path.is_file():
        return None
    try:
        state = json.loads(read_text_no_symlink(state_path))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    budget = state.setdefault("budget", {})
    if not isinstance(budget, dict):
        return None
    if reset_budget:
        for key in (
            "spent_runtime_s", "spent_iterations", "spent_tokens",
            "wasted_runtime_s", "consecutive_failures",
        ):
            budget[key] = 0
        budget["spent_usd"] = 0.0
    new_cap: int | None = None
    if max_runtime is not None:
        delta_s, additive = parse_runtime_duration(max_runtime)
        current = _read_project_budget_cap(proj_dir) if additive else 0
        new_cap = current + delta_s if additive else delta_s
        budget["max_runtime_s"] = new_cap
    write_text_no_symlink(state_path, json.dumps(state, indent=2,
                                                sort_keys=True))
    return new_cap


def cmd_resume(
    name: str,
    *,
    max_runtime: str | None = None,
    reset_budget: bool = False,
    force: bool = False,
    start_run: bool = False,
    container: bool = False,
    config_dir: Path | None = None,
) -> int:
    """Task 4.5: clear a project's Phase-0 checkpoint marker.

    Removes `.peers/checkpoint_requested` and `.peers/awaiting_user`
    so that the next `peers-ctl start <name>` proceeds past Phase 0
    normally. Idempotent: succeeds even when no markers exist
    (operator can run it defensively before any start).

    By default this command only clears markers; optional budget flags
    can extend/reset the run before the next start, and --start performs
    the explicit relaunch in one command.
    """
    try:
        validate_project_name(name)
    except ValueError as e:
        print(f"peers-ctl: {e}", file=sys.stderr)
        return 2
    proj_dir = _resolve_project_dir(name, config_dir)
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
    if (max_runtime is not None or reset_budget) and not start_run:
        try:
            new_cap = _apply_resume_budget(
                proj_dir, max_runtime=max_runtime, reset_budget=reset_budget,
            )
        except ValueError as e:
            print(f"peers-ctl: --max-runtime: {e}", file=sys.stderr)
            return 1
        if new_cap is not None:
            print(f"peers-ctl: max_runtime_s now {new_cap}")
        elif reset_budget:
            print("peers-ctl: budget counters reset")
    if start_run:
        return cmd_start(
            name,
            max_runtime=max_runtime,
            reset_budget=reset_budget,
            force=force,
            container=container,
            config_dir=config_dir,
        )
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


def cmd_list(config_dir: Path | None = None,
             no_reconcile: bool = False) -> int:
    store = _store(config_dir)
    if not no_reconcile:
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


def cmd_dashboard(
    config_dir: Path | None = None,
    *,
    live: bool = False,
    refresh_s: float = 2.0,
    project: str | None = None,
    frames: int | None = None,
) -> int:
    if project is not None:
        try:
            validate_project_name(project)
        except ValueError as e:
            print(f"peers-ctl: {e}", file=sys.stderr)
            return 2
    if frames is not None and frames < 1:
        print(
            "peers-ctl dashboard: --frames must be >= 1",
            file=sys.stderr,
        )
        return 2
    if live:
        from peers_ctl.dashboard_live import run
        return run(
            config_dir, refresh_s=refresh_s, project_name=project,
            iterations=frames,
        )
    if frames is not None:
        print(
            "peers-ctl dashboard: --frames requires --live",
            file=sys.stderr,
        )
        return 2
    from peers_ctl.dashboard_live import (
        DashboardProjectNotFound,
        load_dashboard_rows,
        render_project_detail,
        render_snapshot,
    )
    if project is not None:
        try:
            print(render_project_detail(config_dir, project))
        except DashboardProjectNotFound as e:
            print(e, file=sys.stderr)
            return e.exit_code
        return 0
    # Show the --live hint only on a TTY so piped/captured snapshots
    # (e.g. `peers-ctl dashboard | grep ...`) stay parseable.
    show_hint = bool(getattr(sys.stdout, "isatty", lambda: False)())
    print(render_snapshot(
        load_dashboard_rows(config_dir, reconciler=reconcile),
        include_live_hint=show_hint,
    ))
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
               config_dir: Path | None = None,
               output_format: str = "text") -> int:
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
    if output_format == "json":
        print(json.dumps(_controller_report_json(store, projects, name),
                         indent=2, sort_keys=True))
        return 0
    if output_format != "text":
        print(f"peers-ctl: unsupported report format: {output_format}",
              file=sys.stderr)
        return 2
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


def _state_budget_summary(state: dict) -> dict:
    b = state.get("budget") if isinstance(state, dict) else {}
    if not isinstance(b, dict):
        b = {}
    return {
        "iterations": {
            "used": b.get("spent_iterations", 0),
            "max": b.get("max_iterations"),
        },
        "runtime_s": {
            "used": b.get("spent_runtime_s", 0),
            "max": b.get("max_runtime_s"),
        },
        "tokens": {"total": b.get("spent_tokens", 0)},
        "usd": {"total": b.get("spent_usd", 0.0)},
    }


def _load_project_state(repo: Path) -> dict:
    path = repo / ".peers" / "state.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(read_text_no_symlink(path))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_project_ticks(repo: Path) -> list[dict]:
    path = repo / ".peers" / "log" / "runs.jsonl"
    if not path.is_file():
        return []
    out: list[dict] = []
    try:
        with open_text_read_no_symlink(path) as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    out.append(entry)
    except OSError:
        return []
    return out


def _controller_report_json(
    store: Store,
    projects: list[Project],
    scope: str | None,
) -> dict:
    exported_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
    out_projects: list[dict] = []
    for project in projects:
        repo = Path(project.path)
        state = _load_project_state(repo)
        ticks = _load_project_ticks(repo)
        stop_reason = None
        for entry in reversed(ticks):
            if entry.get("event") == "exit":
                stop_reason = entry.get("reason")
                break
        try:
            from peers.bug_hunt import summary_dict
            bugs = summary_dict(repo)
        except Exception:
            bugs = {
                "total": 0,
                "open_blocking": 0,
                "by_severity": {},
                "by_cwe": {},
                "reports": [],
                "warnings": [],
            }
        goals_status = state.get("goals_status") or {}
        soft_status = state.get("soft_status") or {}
        out_projects.append({
            "project": project.name,
            "path": project.path,
            "state": project.state,
            "stop_reason": stop_reason,
            "budget": _state_budget_summary(state),
            "goals": {
                "hard": [
                    {"id": gid, **info}
                    for gid, info in goals_status.items()
                    if isinstance(info, dict)
                ],
                "soft": [
                    {"id": gid, **info}
                    for gid, info in soft_status.items()
                    if isinstance(info, dict)
                ],
            },
            "bugs": bugs,
            "ticks": ticks,
        })
    return {
        "version": 1,
        "exported_at": exported_at,
        "scope": scope or "all projects",
        "config_dir": str(store.config_dir),
        "projects": out_projects,
    }


def cmd_peek(
    name: str,
    *,
    session: str | None = None,
    no_follow: bool = False,
    last: int | None = None,
    config_dir: Path | None = None,
) -> int:
    store = _store(config_dir)
    project = store.get(name)
    if project is None:
        print(f"peers-ctl: no such project: {name}", file=sys.stderr)
        return 1
    from peers.health_guard import claude_session_jsonl_path
    from peers.peek import newest_session_jsonl, tail_session

    cwd = "/work" if "container=1" in (project.notes or "") else project.path
    jsonl_dir = claude_session_jsonl_path(cwd)
    if jsonl_dir is None:
        print("peers-ctl peek: HOME unset or project cwd is not absolute",
              file=sys.stderr)
        return 1
    jsonl = jsonl_dir / f"{session}.jsonl" if session else (
        newest_session_jsonl(jsonl_dir)
    )
    if jsonl is None or not jsonl.exists():
        print(f"peers-ctl peek: no session jsonl in {jsonl_dir}",
              file=sys.stderr)
        return 1
    try:
        for line in tail_session(jsonl, follow=not no_follow, last=last):
            print(line, flush=True)
    except KeyboardInterrupt:
        return 130
    return 0


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
               config_dir: Path | None = None,
               no_reconcile: bool = False) -> int:
    store = _store(config_dir)
    if name is None:
        if not no_reconcile:
            reconcile(store)
        return cmd_list(config_dir, no_reconcile=True)
    p = store.get(name) if no_reconcile else reconcile_one(store, name)
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
    """Pre-flight check (Item 9): verify the host has everything
    ``peers-ctl start`` needs.

    Delegates to :func:`peers_ctl.doctor.run_doctor`, which prints a
    tabular ``[OK]``/``[WARN]``/``[MISS]`` report and returns 0 iff
    every REQUIRED probe passed. The ``config_dir`` argument is
    accepted for symmetry with the other dispatchers but is not
    currently used — doctor's scope is the host environment, not
    per-project state (see ``peers-ctl status`` / ``peers-ctl report``
    for the latter).
    """
    from peers_ctl.doctor import run_doctor
    return run_doctor()


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
    p_new.add_argument(
        "--template", default=None, metavar="TEMPLATE",
        choices=("internal testing",),
        help=(
            "preset bootstrap. `internal testing` clones the substrate repo "
            "into <path>, checks out a branch named after the target "
            "basename, copies SPEC.md + docs/ATTACK-SURFACE.md from "
            "the latest peers-internal testing-v* sibling (or --anchors-from), "
            "commits them, then continues with the normal "
            "`peers init --modes=audit,thorough` flow."
        ),
    )
    p_new.add_argument(
        "--from", dest="template_from", default=None, type=Path,
        metavar="PATH",
        help=(
            "with --template internal testing: source git repo to clone "
            "(default: the substrate checkout this peers-ctl was "
            "installed from)."
        ),
    )
    p_new.add_argument(
        "--anchors-from", dest="anchors_from", default=None, type=Path,
        metavar="PATH",
        help=(
            "with --template internal testing: explicit directory to copy "
            "SPEC.md + docs/ATTACK-SURFACE.md from (default: latest "
            "peers-internal testing-v* in the target's parent)."
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
    p_dashboard = _add_help_man_subparser(
        sub, "dashboard",
        help_text="rollup view across all registered projects")
    p_dashboard.add_argument(
        "--live", action="store_true",
        help="redraw the dashboard continuously until Ctrl-C "
             "(streaming view of all projects)",
    )
    p_dashboard.add_argument(
        "--refresh-s", type=float, default=2.0,
        help="seconds between --live refreshes (default: 2.0)",
    )
    p_dashboard.add_argument(
        "--frames", type=int, default=None,
        help="with --live: render N frames then exit "
             "(non-interactive smoke test; default: run until Ctrl-C)",
    )
    p_dashboard.add_argument(
        "--project", default=None,
        help="show one project's detail drilldown",
    )

    p_report = _add_help_man_subparser(
        sub, "report",
        help_text="write a controller report under the config dir or JSON to stdout",
    )
    p_report.add_argument("name", nargs="?", default=None)
    p_report.add_argument("--format", choices=("text", "json"),
                          default="text")

    p_peek = _add_help_man_subparser(
        sub, "peek",
        help_text="live-decode the newest claude session jsonl for a project",
    )
    p_peek.add_argument("name")
    p_peek.add_argument("--session", default=None,
                        help="specific claude session id without .jsonl")
    p_peek.add_argument("--no-follow", action="store_true",
                        help="print current events and exit")
    p_peek.add_argument("--last", type=int, default=None,
                        help="read only the last N raw jsonl events first")

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
    p_resume.add_argument("--max-runtime", type=str, default=None,
                          metavar="DURATION")
    p_resume.add_argument("--reset-budget", action="store_true")
    p_resume.add_argument("--force", action="store_true")
    p_resume.add_argument("--start", action="store_true",
                          help="start the project after clearing markers")
    p_resume.add_argument("--container", action="store_true",
                          help="with --start, run inside the peers container")

    p_stop = _add_help_man_subparser(
        sub, "stop", help_text="stop a project loop")
    p_stop.add_argument("name")
    p_stop.add_argument("--grace-s", type=float, default=10.0)

    p_status = _add_help_man_subparser(
        sub, "status", help_text="status of one or all projects")
    p_status.add_argument("name", nargs="?", default=None)
    p_status.add_argument("--no-reconcile", action="store_true",
                          help="print registry state without probing liveness")

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

    p_compare = _add_help_man_subparser(
        sub, "compare",
        help_text=(
            "side-by-side metrics for 2+ projects (iterations, runtime, "
            "wasted budget, bugs by severity, tick classes, "
            "ticks-to-convergence, stop reason)."
        ),
    )
    p_compare.add_argument(
        "names", nargs="+",
        help="project names to compare (need at least 2)",
    )

    from peers_ctl.replay import register_subparser as _register_replay
    _register_replay(sub)

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
                       template=args.template,
                       template_from=args.template_from,
                       anchors_from=args.anchors_from,
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
        return cmd_dashboard(
            cd, live=args.live, refresh_s=args.refresh_s,
            project=args.project, frames=args.frames,
        )
    if args.cmd == "report":
        return cmd_report(args.name, cd, output_format=args.format)
    if args.cmd == "peek":
        return cmd_peek(
            args.name, session=args.session, no_follow=args.no_follow,
            last=args.last, config_dir=cd,
        )
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
        return cmd_resume(
            args.project_name,
            max_runtime=args.max_runtime,
            reset_budget=args.reset_budget,
            force=args.force,
            start_run=args.start,
            container=args.container,
            config_dir=cd,
        )
    if args.cmd == "stop":
        return cmd_stop(args.name, args.grace_s, cd)
    if args.cmd == "status":
        return cmd_status(args.name, cd, no_reconcile=args.no_reconcile)
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
    if args.cmd == "compare":
        return cmd_compare(list(args.names), cd)
    if args.cmd == "replay":
        from peers_ctl.replay import cmd_replay
        return cmd_replay(
            args.name,
            show_prompts=getattr(args, "show_prompts", False),
            show_diffs=getattr(args, "show_diffs", False),
            from_tick=getattr(args, "from_tick", None),
            to_tick=getattr(args, "to_tick", None),
            config_dir=cd,
        )
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
