"""Spawn / signal detached `peers run` processes.

We avoid running a daemon. Each project's loop runs as its own
detached process. The state file remembers the PID; on next
`peers-ctl status` we check `os.kill(pid, 0)` to detect crashes.

PID-recycle defence: on `start` we capture the kernel-issued
`starttime` of the new child (field 22 of /proc/<pid>/stat on Linux).
On `stop` we re-read the starttime; if it differs, the original
process is dead and a new one is squatting on that PID, so we refuse
to signal it. The starttime is monotonic per-boot and a stable
fingerprint as long as the original process is alive.
"""
from __future__ import annotations

import datetime as _dt
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Sequence, TypeGuard

import yaml

from peers.budget_accountant import OPERATOR_BUDGET_OVERRIDE_FILE
from peers.graphify_mcp import GRAPHIFY_API_KEY_ENV, GRAPHIFY_ENDPOINT_ENV
from peers.graphify_sidecar import build_graph, new_api_key, serve_cmd
from peers.model_provider import (
    OPENROUTER_EXTRA_HOST_RE,
    required_peer_runtime_env_keys,
)
from peers.peer_spec import load_peer_specs
from peers.safe_io import (
    _ensure_private_dir,
    atomic_write_text_in_dir_no_symlink,
    open_text_in_dir_no_symlink,
    open_text_no_symlink,
    read_bytes_under_root_no_follow,
    read_text_under_root_no_follow,
)
from peers_ctl.store import Project, Store, is_pid_alive


PEERS_CMD = os.environ.get("PEERS_CTL_PEERS_BIN", "peers")
CONTAINER_IMAGE = os.environ.get("PEERS_CTL_IMAGE", "peers:dev")
PODMAN_CMD = os.environ.get("PEERS_CTL_PODMAN_BIN", "podman")
# Opt-in caged graphify MCP. A global kill-switch forces it off regardless of
# per-project config (defense in depth -- one env var disables it fleet-wide).
GRAPHIFY_DISABLED = os.environ.get("PEERS_CTL_NO_GRAPHIFY", "") not in ("", "0", "false")
_GRAPHIFY_SERVE_PORT = 8080  # host mode: in-container port, published to a free host port
# Container mode binds the SHARED egress-proxy netns loopback, where the
# auth-proxy already holds 8080 (AUTH_PROXY_PORT) -> graphify needs a distinct port.
_GRAPHIFY_CONTAINER_PORT = 8645
# On some hosts (e.g. when /dev/net/tun is missing) pasta — podman's
# default rootless network backend — fails to set up. Override via
# PEERS_CTL_PODMAN_NETWORK=host to bypass (the peers loop doesn't
# need network isolation since it shells out to the in-container
# claude/codex CLIs which talk to the public API anyway).
PODMAN_NETWORK = os.environ.get("PEERS_CTL_PODMAN_NETWORK", "")

# Phase-2 hardening B2 (post-v9 audit synthesis): the peers container
# shares the network namespace of a sidecar tinyproxy that hostname-
# allow-lists outbound HTTPS to api.anthropic.com / api.openai.com.
# Without the sidecar the LLM CLIs reach the full internet via
# slirp4netns and a prompt-injection can `curl evil.tld` exfiltrate
# OAuth tokens (audit, Showstopper Security #1). The sidecar
# is a separate image (PEERS_CTL_EGRESS_PROXY_IMAGE).
#
# Escape hatch: PEERS_CTL_NO_EGRESS_PROXY=1 reverts to legacy mode
# (PODMAN_NETWORK / default slirp4netns) so the operator can debug
# network issues directly.
EGRESS_PROXY_IMAGE = os.environ.get(
    "PEERS_CTL_EGRESS_PROXY_IMAGE", "peers-egress-proxy:dev"
)


def _parse_truthy_env(value: str) -> bool:
    """Case-insensitive truthy check for env-vars. Recognized 'false'
    values: '', '0', 'false', 'no', 'off' (and any case variant)."""
    return value.strip().lower() not in ("", "0", "false", "no", "off")


EGRESS_PROXY_DISABLED = _parse_truthy_env(
    os.environ.get("PEERS_CTL_NO_EGRESS_PROXY", "")
)
EGRESS_PROXY_PORT = 3128
EGRESS_PROXY_URL = f"http://127.0.0.1:{EGRESS_PROXY_PORT}"
# Code-review C1: the proxy MUST NOT inherit PEERS_CTL_PODMAN_NETWORK.
# If the operator set PODMAN_NETWORK=host for the main peers
# container (because /dev/net/tun is missing), reusing it for the
# proxy would put tinyproxy on the host's loopback — every other
# user on the host could `curl -x http://127.0.0.1:3128 https://
# api.anthropic.com/...` and ride our OAuth quota. The proxy gets
# its own dedicated env so the operator decides the trade-off
# explicitly. Default: empty string → podman default rootless
# (slirp4netns/pasta), which sandboxes the proxy's namespace.
EGRESS_PROXY_NETWORK = os.environ.get("PEERS_CTL_EGRESS_PROXY_NETWORK", "")

# Phase 14: Claude OAuth lives in a local auth-proxy sidecar instead of
# being bind-mounted into the workspace container as ~/.claude.json.
# The workspace talks to http://127.0.0.1:8080 (same netns), while the
# sidecar alone holds the rw token file. Escape hatch keeps legacy mode
# available for local debugging.
AUTH_PROXY_IMAGE = os.environ.get(
    "PEERS_CTL_AUTH_PROXY_IMAGE", "peers-auth-proxy:dev"
)
AUTH_PROXY_DISABLED = _parse_truthy_env(
    os.environ.get("PEERS_CTL_NO_AUTH_PROXY", "")
)
AUTH_PROXY_PORT = 8080
AUTH_PROXY_URL = f"http://127.0.0.1:{AUTH_PROXY_PORT}"
# Non-secret placeholder bearer handed to the workspace in auth-proxy mode. The
# claude CLI refuses to issue any request when it has no credential at all
# (apiKeySource "none" -> "Not logged in"), so ANTHROPIC_BASE_URL alone is inert.
# The sidecar STRIPS this header and injects the real OAuth token, so the value
# is intentionally not a real credential — podman argv is ps-visible and the
# workspace is the untrusted side.
AUTH_PROXY_PLACEHOLDER_TOKEN = "peers-auth-proxy-placeholder"


@contextmanager
def _acquire_start_lock(lock_path: Path, timeout: float = 5.0):
    """Serialize concurrent ``peers-ctl start`` calls for one project.

    BUG-210: route the lock-file open through ``open_text_no_symlink`` so
    a same-UID adversary cannot pre-plant a symlink at ``lock_path`` and
    have ``peers-ctl start`` truncate an arbitrary writable file. This
    matches the discipline ``Store.mutate`` already uses for its own lock
    inode (st_nlink==1 + O_NOFOLLOW), keeping the controller's same-UID
    threat model uniform.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fp = open_text_no_symlink(lock_path, "w")
    deadline = time.time() + timeout
    while True:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.time() >= deadline:
                fp.close()
                raise TimeoutError(
                    f"could not acquire start lock {lock_path} "
                    f"within {timeout:.1f}s"
                )
            time.sleep(0.05)
    try:
        yield fp
    finally:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        finally:
            fp.close()


def _host_peers_version() -> str | None:
    try:
        from peers import __version__
        return __version__
    except Exception:
        return None


def _image_peers_version() -> str | None:
    # Version probing does not need network access. Pin it to an explicit
    # network mode so rootless podman hosts without /dev/net/tun do not fail
    # before the `peers --version` process even starts. If the operator set
    # PEERS_CTL_PODMAN_NETWORK for actual container runs, use the same mode.
    network = PODMAN_NETWORK or "none"
    argv = [
        PODMAN_CMD, "run", "--rm", f"--network={network}",
        "--entrypoint", "peers", CONTAINER_IMAGE, "--version",
    ]
    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           timeout=15, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    parts = (r.stdout or "").strip().split()
    return parts[-1] if parts else None


def _version_major(version: str) -> int | None:
    m = re.match(r"^(\d+)", version)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def check_container_version_drift() -> tuple[str, str]:
    """Compare host package version with the peers binary in peers:dev.

    Returns (level, message): ok/warn/error/skipped. Minor or patch drift is
    survivable but worth surfacing; major drift is refused because registry,
    state, and runtime contracts may differ.
    """
    host = _host_peers_version()
    image = _image_peers_version()
    if not host or not image:
        return ("skipped", "")
    if host == image:
        return ("ok", "")
    host_major = _version_major(host)
    image_major = _version_major(image)
    msg = f"container peers={image}, host peers={host}"
    if host_major is not None and image_major is not None and host_major != image_major:
        return (
            "error",
            f"{msg}; major-version drift is unsafe. Rebuild with `make build`.",
        )
    return ("warn", f"{msg}; consider rebuilding with `make build`.")


# Modes whose audit-validity / integrity contracts require host and container
# substrate to be on the SAME minor version. Minor/patch drift escalates from
# warn to error for these modes. Operator can override via PEERS_CTL_ALLOW_DRIFT=1.
_DRIFT_REFUSE_MODES = frozenset({"audit", "thorough"})
_MODES_APPLIED_MAX_BYTES = 512 * 1024
_PROJECT_CONFIG_MAX_BYTES = 512 * 1024


def _read_project_config_text(project: Project) -> str | None:
    """Read ``<project>/.peers/config.yaml`` without following ancestors.

    ``read_text_no_symlink(project/.peers/config.yaml)`` only protects the
    final ``config.yaml`` leaf. Controller start-time decisions also trust the
    ``.peers`` ancestor, so route through the same dir-fd walker used for
    state/mode files and reject symlinked ancestors before parsing config.
    """
    project_path = Path(project.path)
    display = project_path / ".peers" / "config.yaml"
    try:
        raw = read_bytes_under_root_no_follow(
            project_path,
            [".peers", "config.yaml"],
            max_bytes=_PROJECT_CONFIG_MAX_BYTES + 1,
        )
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise ValueError(f"unsafe project config {display}: {e}") from e
    if len(raw) > _PROJECT_CONFIG_MAX_BYTES:
        raise ValueError(
            f"unsafe project config {display}: exceeds "
            f"{_PROJECT_CONFIG_MAX_BYTES} bytes"
        )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(
            f"unsafe project config {display}: invalid UTF-8: {e}"
        ) from e


def _read_project_modes_applied(project) -> list[str]:
    """Read mode names from `<project>/.peers/modes-applied.txt`.

    Returns empty list on missing file or parse failure. Each line is
    `<timestamp>  <mode>  v<n>  sha256=...` — the mode name is the
    second whitespace-separated token.
    """
    try:
        project_path = Path(project.path)
    except AttributeError:
        return []
    try:
        text = read_text_under_root_no_follow(
            project_path,
            (".peers", "modes-applied.txt"),
            max_bytes=_MODES_APPLIED_MAX_BYTES,
        )
    except FileNotFoundError:
        return []
    names: list[str] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            names.append(parts[1])
    return names


def enforce_container_drift_for_modes(modes: list[str] | None) -> tuple[str, str]:
    """Run drift check; escalate warn → error for audit-integrity modes.

    Background: `peers-ctl new --container` runs `peers init` inside the
    container image. If the image is older than the host, init writes
    `.peers/config.yaml` from the OLD template, silently propagating stale
    defaults (claude argv, prompt_mode, mode templates) into the new
    project. v12 hit exactly this — image was 1.5.0, host 1.6.0, so
    Phase-2 stream-json default never reached the project.

    Returns (level, msg). Raises RuntimeError when refusing.
    Bypass: set PEERS_CTL_ALLOW_DRIFT=1 to keep the legacy warn behavior.
    """
    level, msg = check_container_version_drift()
    if level == "error":
        raise RuntimeError(msg)
    if level != "warn":
        return (level, msg)
    if os.environ.get("PEERS_CTL_ALLOW_DRIFT", "").strip() == "1":
        return (level, msg)
    mode_set = {m.strip() for m in (modes or []) if m and m.strip()}
    if mode_set & _DRIFT_REFUSE_MODES:
        raise RuntimeError(
            f"{msg}; refuse: audit-integrity modes "
            f"({sorted(mode_set & _DRIFT_REFUSE_MODES)!r}) require "
            "aligned host and container versions. Rebuild with `make build` "
            "or override with PEERS_CTL_ALLOW_DRIFT=1."
        )
    return (level, msg)


def _terminate_spawned_process(proc: subprocess.Popen, grace_s: float = 1.0) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except ProcessLookupError:
            return
    try:
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                return
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass


def _stop_container_best_effort(name: str, grace_s: float = 1.0) -> None:
    try:
        subprocess.run(
            [PODMAN_CMD, "stop", "-t", str(int(grace_s)), name],
            capture_output=True, timeout=grace_s + 10, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass


def _proc_starttime(pid: int) -> int | None:
    """Linux: read field 22 (starttime, in clock ticks since boot) from
    /proc/<pid>/stat. Returns None if unavailable (non-Linux, dead pid,
    permission denied). The /proc/<pid>/stat format puts `comm` in
    parentheses which may itself contain spaces, so we parse from
    after the last `)`."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    rparen = data.rfind(b")")
    if rparen < 0:
        return None
    rest = data[rparen + 1:].decode("ascii", errors="replace").split()
    # Field 22 in `man 5 proc`: position 19 after `state` (which is
    # field 3, index 0 after `)`).
    try:
        return int(rest[19])
    except (IndexError, ValueError):
        return None


def _container_running(name: str) -> bool:
    """True iff a podman container with this name is currently running."""
    try:
        r = subprocess.run(
            [PODMAN_CMD, "ps", "--filter", f"name=^{re.escape(name)}$",
             "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return name in (r.stdout or "").split()


def _cleanup_stale_container(name: str) -> None:
    """Remove a stopped/exited container with this name so a fresh
    `podman run -d --name <n>` doesn't error with 'name in use'.

    Deliberately avoid `rm -f`: a stale registry must not kill a still-running
    container that happens to have the tracked name.
    """
    try:
        subprocess.run(
            [PODMAN_CMD, "rm", name],
            capture_output=True, timeout=10, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def _container_from_project(project: Project) -> str | None:
    """Parse container_name=... token from project.notes, or fall back
    to deriving it from the project name (legacy registry entries
    written before didn't store the name)."""
    if project.notes:
        for tok in project.notes.split():
            if tok.startswith("container_name="):
                return tok.split("=", 1)[1] or None
    return _container_name(project)


def _is_container_project(project: Project) -> bool:
    if not project.notes:
        return False
    return "container=1" in project.notes


def _container_name(project: Project) -> str:
    """Stable per-project container name. peers-ctl tracks
    container EXISTENCE (`podman ps --filter name=<n>`) rather than
    podman PID, so this name is the substitute for the PID-recycle
    fingerprint. Reusing the same name across restarts is intentional —
    `peers-ctl start` while a previous container is alive raises
    "already running".
    """
    # Allow only [a-z0-9_-] (podman name constraint); fall back to a
    # hash if the project name has odd chars or would otherwise collide
    # after the 40-character trim.
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "-", project.name) or "proj"
    if sanitized != project.name or len(sanitized) > 40:
        digest = hashlib.sha256(project.name.encode("utf-8")).hexdigest()[:8]
        sanitized = f"{sanitized[:31]}-{digest}"
    return f"peers-ctl_{sanitized}"


def _proxy_container_name(project: Project) -> str:
    """Stable per-project name for the egress-proxy sidecar. Mirrors
    `_container_name` but with a distinct prefix so the two containers
    never collide and `peers-ctl stop` can find both.
    """
    # Reuse the sanitization rules from _container_name by stripping
    # its prefix.
    main = _container_name(project)
    suffix = main[len("peers-ctl_"):] if main.startswith("peers-ctl_") else main
    return f"peers-egress-proxy_{suffix}"


def _auth_proxy_container_name(project: Project) -> str:
    main = _container_name(project)
    suffix = main[len("peers-ctl_"):] if main.startswith("peers-ctl_") else main
    return f"peers-auth-proxy_{suffix}"


def _auth_proxy_token_file(home: Path | None = None) -> Path | None:
    home = home or Path.home()
    relocated = home / ".claude" / ".credentials.json"
    if relocated.is_file():
        return relocated
    legacy = home / ".claude.json"
    if legacy.is_file():
        return legacy
    return None


def _auth_proxy_enabled(home: Path | None = None) -> bool:
    return (not AUTH_PROXY_DISABLED) and _auth_proxy_token_file(home) is not None


def _auth_proxy_was_used(project: Project) -> bool:
    return "auth_proxy=1" in (project.notes or "") or _auth_proxy_enabled()


def _project_uses_openrouter(project: Project) -> bool:
    specs = _load_project_peer_specs(project)
    return bool(specs) and any(spec.provider == "openrouter" for spec in specs)


def _load_project_peer_specs(project: Project):
    cfg_path = Path(project.path) / ".peers" / "config.yaml"
    raw = _read_project_config_text(project)
    if raw is None:
        return None
    try:
        cfg = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise ValueError(f"cannot parse {cfg_path}: {e}") from e
    if not isinstance(cfg, dict):
        raise ValueError(f"{cfg_path} top-level must be a mapping")
    try:
        return load_peer_specs(cfg)
    except ValueError as e:
        raise ValueError(f"invalid peer config {cfg_path}: {e}") from e


# Operator-declared egress allow-list (config `egress_allow:`). Bounded to
# limit blast radius from typos or a config that widens egress too far.
_MAX_EGRESS_ALLOW = 64
_MAX_EGRESS_HOST_LEN = 256


def _config_egress_allow(project: Project) -> tuple[str, ...]:
    """Extra egress allow-list (tinyproxy host regexes) declared by the project
    in ``.peers/config.yaml`` ``egress_allow: [..]`` -- e.g. an RFC editor or a
    research source the peers may reach. FAIL-CLOSED: a missing/malformed value
    yields no extra hosts (a parse error must never silently widen egress).
    Entries containing a comma or newline are dropped so one entry cannot
    smuggle additional filter lines through the comma-joined env var."""
    try:
        raw = _read_project_config_text(project)
        if raw is None:
            return ()
        cfg = yaml.safe_load(raw)
    except (ValueError, yaml.YAMLError):
        return ()
    if not isinstance(cfg, dict):
        return ()
    raw = cfg.get("egress_allow")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        print(
            "peers-ctl: warning: egress_allow must be a list of host-regex "
            "strings; ignoring",
            file=sys.stderr,
        )
        return ()
    hosts: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        host = entry.strip()
        if not host or len(host) > _MAX_EGRESS_HOST_LEN:
            continue
        if "," in host or "\n" in host:
            continue
        hosts.append(host)
    return tuple(hosts[:_MAX_EGRESS_ALLOW])


_EGRESS_ALLOW_TRUST_NOTE = "egress_allow_sha256"


def _notes_value(notes: str | None, key: str) -> str | None:
    prefix = f"{key}="
    for token in (notes or "").split():
        if token.startswith(prefix):
            return token.split("=", 1)[1] or None
    return None


def _notes_with_value(notes: str | None, key: str, value: str) -> str:
    prefix = f"{key}="
    tokens = [
        token for token in (notes or "").split()
        if not token.startswith(prefix)
    ]
    tokens.append(f"{key}={value}")
    return " ".join(tokens)


def _egress_allow_digest(hosts: tuple[str, ...]) -> str:
    payload = json.dumps(list(hosts), separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _egress_trust_note_suffix(project: Project) -> str:
    digest = _notes_value(project.notes, _EGRESS_ALLOW_TRUST_NOTE)
    return f" {_EGRESS_ALLOW_TRUST_NOTE}={digest}" if digest else ""


def _ensure_config_trusted_for_egress(
    store: Store,
    project: Project,
    *,
    force: bool = False,
    trust_egress_allow: bool = False,
) -> Project:
    """Require host-side review before project config widens container egress.

    `.peers/config.yaml` is deliberately project-local and gitignored, so peers
    can edit it while the egress proxy is the containment layer. Treating
    `egress_allow` as trusted merely because it appears in that file lets a
    prompt-injected peer persist a wider network policy for the next container
    start. A non-empty allow-list therefore needs an exact digest in the
    host-owned registry notes; `--trust-egress-allow` records that digest after
    the operator has reviewed the config. `--force` deliberately remains scoped
    to the budget/sentinel preflight and does not trust network policy.
    """
    _ = force
    hosts = _config_egress_allow(project)
    if not hosts:
        return project
    digest = _egress_allow_digest(hosts)
    trusted = _notes_value(project.notes, _EGRESS_ALLOW_TRUST_NOTE)
    if trusted == digest:
        return project
    if not trust_egress_allow:
        if trusted:
            raise ValueError(
                "egress_allow in .peers/config.yaml changed since the "
                "host-side review; review the current allow-list and rerun "
                "`peers-ctl start --container --trust-egress-allow` to trust it."
            )
        raise ValueError(
            "egress_allow in .peers/config.yaml is not yet trusted by the "
            "host registry; review the current allow-list and rerun "
            "`peers-ctl start --container --trust-egress-allow` to trust it."
        )
    return store.update(
        project.name,
        notes=_notes_with_value(
            project.notes, _EGRESS_ALLOW_TRUST_NOTE, digest,
        ),
    )


def _egress_extra_allow_hosts(project: Project) -> tuple[str, ...]:
    hosts: list[str] = []
    if _project_uses_openrouter(project):
        hosts.append(OPENROUTER_EXTRA_HOST_RE)
    hosts.extend(_config_egress_allow(project))
    return tuple(hosts)


def _require_openrouter_env_for_container(project: Project) -> None:
    required_keys = _project_provider_env_keys(project)
    if not required_keys:
        return
    missing = [
        key for key in required_keys
        if not os.environ.get(key, "").strip()
    ]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(
            f"project {project.name!r} uses provider: openrouter; export "
            f"{joined} before `peers-ctl start --container`"
        )


def _project_provider_env_keys(project: Project) -> tuple[str, ...]:
    specs = _load_project_peer_specs(project)
    if not specs:
        return ()
    return required_peer_runtime_env_keys(specs)


def _project_has_native_claude_peer(project: Project) -> bool:
    """True iff the project configures a peer that runs the native `claude` CLI
    (which authenticates via the in-container auth-proxy). An openrouter-claude
    peer talks to openrouter directly, NOT the proxy, so it does not count."""
    try:
        specs = _load_project_peer_specs(project)
    except ValueError:
        return False
    if not specs:
        return False
    for spec in specs:
        argv = tuple(getattr(spec, "argv", ()) or ())
        provider = (getattr(spec, "provider", None) or "anthropic").lower()
        if argv and argv[0] == "claude" and provider != "openrouter":
            return True
    return False


def _ensure_claude_can_authenticate_for_container(
    project: Project, *, skip: bool = False, _smoke=None,
) -> None:
    """Preflight: if a native-claude peer is configured for a --container run,
    prove claude actually authenticates IN the container before launching.

    The container auth-proxy path fails SILENTLY: a broken claude auth just
    degrades the peer a few ticks in and the run continues single-peer — the
    v26 internal testing lost its entire claude side this way and ran codex-solo for
    hours. A live claude smoke here turns that into a loud, pre-launch refusal.
    `--skip-claude-smoke` bypasses it (e.g. transient network)."""
    if skip:
        return
    if not _project_has_native_claude_peer(project):
        return
    if _smoke is None:
        from peers_ctl.doctor import probe_claude_smoke
        _smoke = probe_claude_smoke
    result = _smoke()
    if getattr(result, "status", None) != "OK":
        detail = getattr(result, "value", "") or "claude did not reply"
        raise ValueError(
            f"project {project.name!r} configures a claude peer but the "
            f"in-container claude smoke failed: {detail}. Fix claude auth "
            f"(check `peers-ctl doctor --claude-smoke`) or rerun "
            f"`peers-ctl start --container --skip-claude-smoke` to bypass."
        )


def _build_proxy_argv(project: Project) -> list[str]:
    """Compose a `podman run -d` invocation for the egress-proxy
    sidecar. The proxy is a security component — it has no business
    reading host paths, holding caps, or writing to a persistent
    rootfs, so it is hardened tighter than the main container:
      - cap-drop=ALL, no-new-privileges
      - read-only rootfs
      - tmpfs for /tmp (only writable path tinyproxy needs at runtime)
      - no `-v` mounts (config is baked into the image)
      - --network is whatever the host gives us (slirp default), since
        the proxy *is* the egress point — its allow-list is what
        constrains where requests go
    """
    # tinyproxy in alpine runs as uid 100 (gid 101). podman's
    # `--tmpfs` option does not accept `uid=`/`gid=`, so we use
    # mode=01777 (sticky-bit world-writable, same as host /tmp) on
    # the dirs tinyproxy needs at fork time; pidfile dir is mode=0700
    # because only one principal writes it.
    #
    # `pids-limit=128`: tinyproxy is fork-per-client with
    # MaxClients=64; the steady-state is ~70 processes plus
    # init/reaper overhead. 128 leaves headroom for SIGKILL bursts
    # during shutdown without inviting a fork bomb to drain the
    # host cgroup.
    argv = [
        PODMAN_CMD, "run", "-d", "--rm",
        "--name", _proxy_container_name(project),
        # The egress-proxy is the network-namespace OWNER for the whole
        # sidecar chain: the auth-proxy and the main container both join
        # its netns via --network=container:<proxy>. The kernel only
        # permits mounting a fresh sysfs at /sys if the caller's USER
        # namespace owns that netns. So the proxy creates the shared
        # userns here with keep-id (the same host-uid mapping the main
        # container needs for /work FS-perm alignment); auth + main join
        # THIS userns. Without it, the main container minted its own
        # keep-id userns, did not own the joined netns, and `runc create`
        # failed with `mounting sysfs to /sys: operation not permitted`
        # (rc=126) — the full-isolation start was unusable.
        "--userns=keep-id",
        "--cap-drop=ALL",
        # NET_ADMIN: the entrypoint installs an in-netns firewall lockdown that
        #   forces ALL egress through tinyproxy. Without it the joined main
        #   container has the proxy netns's open default route and bypasses the
        #   allow-list by clearing HTTP_PROXY.
        # SETUID/SETGID: the entrypoint starts as (mapped, unprivileged) root to
        #   run iptables, then `su-exec`s down to the tinyproxy uid for the
        #   long-lived daemon. cap-drop=ALL above means these three are the ONLY
        #   capabilities this security component holds.
        "--cap-add=NET_ADMIN",
        "--cap-add=SETUID",
        "--cap-add=SETGID",
        "--security-opt=no-new-privileges",
        "--read-only",
        # B108 here is a container-internal mount destination, not a
        # host temp path. The string never opens a file on the host.
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777",  # nosec B108
        "--tmpfs", "/var/log/tinyproxy:rw,nosuid,nodev,size=16m,mode=1777",
        # mode=1777 (not 0700) because podman tmpfs is root-owned and
        # tinyproxy runs as uid 100; sticky world-writable is the only
        # way for the non-root user to write the pidfile. Safe in
        # this container because tinyproxy is the only principal.
        "--tmpfs", "/run/tinyproxy:rw,nosuid,nodev,size=4m,mode=1777",
        "--pids-limit=128",
        # Stamp the allow-list this proxy was built for. A later start compares
        # this against the current config and recreates the proxy on drift, so a
        # changed `egress_allow` is never silently ignored by a reused sidecar
        #.
        f"--label=peers.egress_allow_digest="
        f"{_egress_allow_digest(_egress_extra_allow_hosts(project))}",
    ]
    # Code-review C1: explicit dedicated network mode for the proxy.
    # NEVER inherit PEERS_CTL_PODMAN_NETWORK (which the operator may
    # have set to `host` for the main container due to /dev/net/tun
    # absence). Empty default => podman's default rootless backend
    # (slirp4netns/pasta), which gives the proxy its own netns and a
    # private loopback. The main peers container then shares THAT
    # loopback via --network=container:<proxy>, so 127.0.0.1:3128 is
    # reachable only by the pair, not other host users.
    if EGRESS_PROXY_NETWORK:
        argv.append(f"--network={EGRESS_PROXY_NETWORK}")
    extra_hosts = _egress_extra_allow_hosts(project)
    if extra_hosts:
        argv += ["-e", f"PEERS_EGRESS_EXTRA_HOSTS={','.join(extra_hosts)}"]
    argv.append(EGRESS_PROXY_IMAGE)
    return argv


def _build_auth_proxy_argv(project: Project, home: Path | None = None) -> list[str]:
    home = home or Path.home()
    # claude-code relocated the OAuth access/refresh token from ~/.claude.json
    # (which now holds only account metadata) to ~/.claude/.credentials.json
    # (key `claudeAiOauth`). Prefer the relocated file when present; fall back
    # to the legacy path for older clients. The proxy always reads it at the
    # fixed in-container path /auth/.claude.json, so only the host source moves.
    token_file = _auth_proxy_token_file(home) or home / ".claude.json"
    # Run as the invoking host uid (= the token-file owner). Under
    # --userns=keep-id the default container user is NOT the token owner, and
    # with cap-drop=ALL it lacks CAP_DAC_OVERRIDE, so it cannot read the
    # mode-600 token (the long-standing "auth-proxy 502: Permission denied on
    # /auth/.claude.json"). Running as the owner uid makes the read/refresh work
    # at least privilege. The /auth tmpfs is mode 1733 (owner rwx, others wx,
    # sticky) so that uid can traverse /auth and create the refresh temp file
    # while the token keeps its own 0600 protection.
    argv = [
        PODMAN_CMD, "run", "-d", "--rm",
        "--name", _auth_proxy_container_name(project),
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777",  # nosec B108
        "--tmpfs", "/auth:rw,nosuid,nodev,size=4m,mode=1733",  # nosec B108
        "--pids-limit=128",
        "-v", f"{token_file}:/auth/.claude.json",
    ]
    if EGRESS_PROXY_DISABLED:
        # No egress proxy → the auth-proxy is the head of the chain that
        # the main container joins. It must own a keep-id userns so the
        # main container can share it and mount sysfs (unless host net is
        # forced, in which case sysfs is bind-mounted from the host and
        # userns ownership is moot — keep-id is harmless either way).
        argv.append("--userns=keep-id")
        if PODMAN_NETWORK:
            argv.append(f"--network={PODMAN_NETWORK}")
    else:
        # Share BOTH the egress-proxy's userns and netns. They must point
        # at the same owner so the auth-proxy owns the netns it mounts
        # sysfs into (see _build_proxy_argv for the sysfs/userns rule).
        argv.append(f"--userns=container:{_proxy_container_name(project)}")
        argv.append(f"--network=container:{_proxy_container_name(project)}")
        argv += [
            "-e", f"HTTPS_PROXY={EGRESS_PROXY_URL}",
            "-e", f"HTTP_PROXY={EGRESS_PROXY_URL}",
            "-e", "NO_PROXY=localhost,127.0.0.1,::1",
        ]
    token_url = os.environ.get("AUTH_PROXY_OAUTH_TOKEN_URL")
    if token_url:
        argv += ["-e", f"AUTH_PROXY_OAUTH_TOKEN_URL={token_url}"]
    argv += [
        AUTH_PROXY_IMAGE,
        "--host", "127.0.0.1",
        "--port", str(AUTH_PROXY_PORT),
        "--token-file", "/auth/.claude.json",
    ]
    return argv


def _proxy_egress_digest(name: str) -> str | None:
    """Read the `peers.egress_allow_digest` label off a running proxy
    container, or None if absent/unreadable. Used to detect that a reused
    sidecar was built for a different (stale) egress allow-list."""
    try:
        r = subprocess.run(
            [PODMAN_CMD, "inspect", "--type", "container",
             "--format", "{{index .Config.Labels \"peers.egress_allow_digest\"}}",
             name],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    value = (r.stdout or "").strip()
    # podman prints "<no value>" for a missing label key.
    if not value or value == "<no value>":
        return None
    return value


def _run_proxy_container(project: Project) -> subprocess.CompletedProcess:
    return subprocess.run(
        _build_proxy_argv(project),
        stdin=subprocess.DEVNULL,
        capture_output=True, text=True, check=False,
    )


def _proxy_is_live(name: str) -> bool:
    """True iff the proxy container is still running shortly after launch.
    `podman run -d` returns 0 once the container is *created*, even if its
    entrypoint then exits non-zero — which is exactly what the fail-closed
    egress lockdown does when it cannot install the firewall. Poll briefly so
    a clean (slightly-delayed) start is not mistaken for a crash."""
    for _ in range(6):
        if _container_running(name):
            return True
        time.sleep(0.25)
    return False


def _ensure_egress_proxy_running(project: Project) -> None:
    """Phase-2 hardening B2: ensure the egress-proxy sidecar is up
    before launching the main peers container. The main container
    will use `--network=container:<proxy_name>` and would otherwise
    refuse to start if the proxy is missing. Idempotent on multiple
    calls. No-op when EGRESS_PROXY_DISABLED.

    A reused sidecar must match the CURRENT egress allow-list: if a running
    proxy was built for a different `egress_allow` (digest drift), it is
    stopped and recreated. Otherwise a config change would be silently ignored
    and the proxy would 403 the operator's newly-allowlisted hosts.

    Raises RuntimeError with an actionable message when the proxy
    image is missing or fails to start — better than letting the main
    container's `podman run` fail with an opaque "no such container"
    message.
    """
    if EGRESS_PROXY_DISABLED:
        return
    pname = _proxy_container_name(project)
    if _container_running(pname):
        want = _egress_allow_digest(_egress_extra_allow_hosts(project))
        if _proxy_egress_digest(pname) == want:
            return
        # Drift: the running proxy enforces a different allow-list. Recreate it
        # so the new policy actually takes effect.
        _stop_egress_proxy_best_effort(project)
    _cleanup_stale_container(pname)
    run = _run_proxy_container(project)
    if run.returncode == 0:
        if _proxy_is_live(pname):
            return
        # `podman run -d` succeeded but the container is already gone: the
        # entrypoint's fail-closed egress lockdown aborts the proxy when it
        # cannot install the firewall (e.g. the host denies rootless
        # CAP_NET_ADMIN for the proxy netns). Refuse loudly rather than let the
        # main container join a dead netns with an opaque "no such container".
        raise RuntimeError(
            f"egress proxy ({pname}) exited immediately after start — it "
            f"refuses to run without its egress lockdown (the container needs "
            f"rootless CAP_NET_ADMIN for its network namespace). Check "
            f"`podman logs {pname}`; do NOT set PEERS_CTL_NO_EGRESS_PROXY=1 "
            f"unless you accept unfiltered container egress."
        )
    # Code-review C3: between `_container_running()` and `podman run`
    # a concurrent peers-ctl start can win the race; we get "name in
    # use". Recover by re-probing — if the proxy is now running, the
    # other start succeeded and we are still healthy. If not, the
    # name is held by a stale entry; `_cleanup_stale_container`
    # already tried, so escalate.
    msg = (run.stderr or "").strip()[:300]
    msg_lower = msg.lower()
    name_collision = (
        "in use" in msg_lower
        or "already in use" in msg_lower
        or "name is already" in msg_lower
    )
    if name_collision and _container_running(pname):
        return
    hint = (
        " (build it with `make proxy-build`, or set "
        "PEERS_CTL_NO_EGRESS_PROXY=1 to bypass)"
        if "image" in msg_lower or "manifest" in msg_lower
        else ""
    )
    raise RuntimeError(
        f"failed to start egress proxy ({pname}, "
        f"rc={run.returncode}): {msg}{hint}"
    )


def _ensure_auth_proxy_running(project: Project) -> None:
    if not _auth_proxy_enabled():
        return
    aname = _auth_proxy_container_name(project)
    if _container_running(aname):
        return
    _cleanup_stale_container(aname)
    run = subprocess.run(
        _build_auth_proxy_argv(project),
        stdin=subprocess.DEVNULL,
        capture_output=True, text=True, check=False,
    )
    if run.returncode == 0:
        return
    msg = (run.stderr or "").strip()[:300]
    msg_lower = msg.lower()
    name_collision = (
        "in use" in msg_lower
        or "already in use" in msg_lower
        or "name is already" in msg_lower
    )
    if name_collision and _container_running(aname):
        return
    hint = (
        " (build it with `make auth-proxy-build`, or set "
        "PEERS_CTL_NO_AUTH_PROXY=1 to use the legacy workspace mount)"
        if "image" in msg_lower or "manifest" in msg_lower
        else ""
    )
    raise RuntimeError(
        f"failed to start auth proxy ({aname}, rc={run.returncode}): "
        f"{msg}{hint}"
    )


def _stop_egress_proxy_best_effort(project: Project) -> None:
    """Tear down the project's proxy sidecar if running. Best-effort:
    a leftover proxy is preferable to a stop-failure that breaks the
    operator's recovery path. The proxy is `--rm`, so a successful
    `podman stop` removes it; a stale entry will be reaped by
    `_cleanup_stale_container` at the next start."""
    if EGRESS_PROXY_DISABLED:
        return
    pname = _proxy_container_name(project)
    if not _container_running(pname):
        return
    try:
        subprocess.run(
            [PODMAN_CMD, "stop", "-t", "2", pname],
            capture_output=True, timeout=15, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def _stop_auth_proxy_best_effort(project: Project) -> None:
    if not _auth_proxy_was_used(project):
        return
    aname = _auth_proxy_container_name(project)
    if not _container_running(aname):
        return
    try:
        subprocess.run(
            [PODMAN_CMD, "stop", "-t", "2", aname],
            capture_output=True, timeout=15, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


# --- graphify MCP sidecar (opt-in, caged, fail-open; mirrors the proxies) ---


def _graphify_container_name(project: Project) -> str:
    """Stable per-project graphify sidecar name. Distinct prefix from the main
    + proxy containers so `peers-ctl stop` can find and reap all of them."""
    main = _container_name(project)
    suffix = main[len("peers-ctl_"):] if main.startswith("peers-ctl_") else main
    return f"peers-graphify_{suffix}"


def _graphify_enabled(project: Project) -> bool:
    """True iff the project opts into the caged graphify MCP (config.yaml
    ``graphify_mcp: true``). Off by default; PEERS_CTL_NO_GRAPHIFY forces off."""
    if GRAPHIFY_DISABLED:
        return False
    try:
        raw = _read_project_config_text(project)
        if raw is None:
            return False
        cfg = yaml.safe_load(raw)
    except (ValueError, yaml.YAMLError):
        return False
    # same bug class as BUG-760 (goals.py) — `bool(cfg.get(..., False))`
    # truth-coerces quoted `'false'` to True, silently starting the caged
    # sidecar an operator meant to keep off. Strict identity instead: only
    # the real boolean True enables the opt-in; any other scalar (string,
    # int, None) falls back to disabled. Fail-open path so we don't raise,
    # we just refuse to enable on an unrecognised value.
    if not isinstance(cfg, dict):
        return False
    return cfg.get("graphify_mcp") is True


def _free_loopback_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _ensure_graphify_serve_host(project: Project) -> tuple[str, str] | None:
    """Build the caged graph and start a host-published graphify serve sidecar.

    Returns ``(endpoint, api_key)`` for the driver's env, or ``None`` on any
    failure (FAIL-OPEN: a missing podman/image/graph must never block the run).
    The api key reaches the sidecar via the podman subprocess env, so it never
    appears in any argv (ps-invisible).
    """
    try:
        if not _graphify_enabled(project):
            return None
        out_dir = Path(project.path) / ".peers" / "graphify"
        graph = build_graph(Path(project.path), out_dir)
        if graph is None:
            return None
        name = _graphify_container_name(project)
        _cleanup_stale_container(name)
        api_key = new_api_key()
        port = _free_loopback_port()
        cmd = serve_cmd(
            graph, name=name, port=_GRAPHIFY_SERVE_PORT,
            publish=f"127.0.0.1:{port}:{_GRAPHIFY_SERVE_PORT}",
            bind_host="0.0.0.0",
        )
        run = subprocess.run(
            cmd,
            env={**os.environ, GRAPHIFY_API_KEY_ENV: api_key},
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            check=False,
        )
        if run.returncode != 0:
            print(
                "peers-ctl: warning: graphify serve sidecar failed to start "
                f"(rc={run.returncode}); continuing without graph: "
                f"{(run.stderr or '').strip()[:200]}",
                file=sys.stderr,
            )
            _stop_graphify_best_effort(project)
            return None
        return f"http://127.0.0.1:{port}/mcp", api_key
    except Exception as e:  # fail-open: never block a run on the accelerator
        print(
            "peers-ctl: warning: graphify serve host error "
            f"(continuing without graph): {e}",
            file=sys.stderr,
        )
        return None


def _stop_graphify_best_effort(project: Project) -> None:
    """Tear down the project's graphify sidecar if running. Best-effort: a
    leftover (caged, --rm) sidecar is preferable to a stop-failure that breaks
    the operator's recovery path; the next start reaps it by name."""
    name = _graphify_container_name(project)
    if not _container_running(name):
        return
    try:
        subprocess.run(
            [PODMAN_CMD, "stop", "-t", "2", name],
            capture_output=True, timeout=15, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def _peer_netns_head(project: Project) -> str | None:
    """The container whose netns the main peer container joins -- so a graphify
    sidecar must join the SAME one to be reachable on the shared loopback: the
    egress proxy in the hardened default, the auth proxy when egress is off, or
    None when the peer owns its own netns (nothing to attach to => caller
    fail-opens). Mirrors the netns block in _peer_container_runtime_flags."""
    if not EGRESS_PROXY_DISABLED:
        return _proxy_container_name(project)
    if _auth_proxy_enabled():
        return _auth_proxy_container_name(project)
    return None


def _ensure_graphify_serve_container(project: Project) -> tuple[str, str] | None:
    """Build the caged graph and start a graphify serve sidecar that JOINS the
    peer's netns chain (egress/auth proxy head), so the in-container peers reach
    it on the shared loopback -- at a port distinct from the proxies'. Returns
    ``(endpoint, api_key)`` or ``None`` (FAIL-OPEN). The api key reaches the
    sidecar via the podman subprocess env, never via argv (ps-invisible).
    """
    try:
        if not _graphify_enabled(project):
            return None
        head = _peer_netns_head(project)
        if head is None:
            print(
                "peers-ctl: warning: graphify needs a proxy netns to share in "
                "container mode; none active -> continuing without graph",
                file=sys.stderr,
            )
            return None
        if not _container_running(head):
            return None
        out_dir = Path(project.path) / ".peers" / "graphify"
        graph = build_graph(Path(project.path), out_dir)
        if graph is None:
            return None
        name = _graphify_container_name(project)
        _cleanup_stale_container(name)
        api_key = new_api_key()
        cmd = serve_cmd(
            graph, name=name, port=_GRAPHIFY_CONTAINER_PORT,
            network=f"container:{head}", userns=f"container:{head}",
            bind_host="127.0.0.1",
        )
        run = subprocess.run(
            cmd,
            env={**os.environ, GRAPHIFY_API_KEY_ENV: api_key},
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            check=False,
        )
        if run.returncode != 0:
            print(
                "peers-ctl: warning: graphify serve sidecar failed to start "
                f"(rc={run.returncode}); continuing without graph: "
                f"{(run.stderr or '').strip()[:200]}",
                file=sys.stderr,
            )
            _stop_graphify_best_effort(project)
            return None
        return f"http://127.0.0.1:{_GRAPHIFY_CONTAINER_PORT}/mcp", api_key
    except Exception as e:  # fail-open: never block a run on the accelerator
        print(
            "peers-ctl: warning: graphify serve container error "
            f"(continuing without graph): {e}",
            file=sys.stderr,
        )
        return None


def _build_container_argv(project: Project,
                          max_ticks: int | None,
                          extra_args: Sequence[str],
                          *,
                          graphify: tuple[str, str] | None = None,
                          peers_subcmd: Sequence[str] | None = None) -> list[str]:
    """Compose a `podman run -d` invocation that drives the substrate
    inside the peers:dev image. The container mounts:
      - the target repo at /work
      - ~/.claude and ~/.codex for auth (read-write so tokens refresh)
    The entrypoint is `peers`; we pass `run [--max-ticks N] ...` as
    args. `--userns=keep-id` aligns FS perms with the host user.

    `-d` (detach) is REQUIRED. Without it, the container
    inherits podman as a "foreground" parent; when peers-ctl exits
    after Popen-ing podman, conmon eventually decides the container
    is orphaned and SIGTERMs PID 1 inside (~127 s observed). With
    `-d`, conmon owns the lifecycle from the start. Container ID is
    returned on podman's stdout; peers-ctl tracks the named container.
    """
    argv = [
        PODMAN_CMD, "run", "-d", "--rm",
        "--name", _container_name(project),
    ]
    # NOTE: the user namespace is selected per network-mode inside
    # _peer_container_runtime_flags, NOT here. When the container joins a
    # sidecar's netns it must share THAT sidecar's userns (so it owns the
    # netns and can mount sysfs); only when it owns its own netns does it
    # mint keep-id.
    argv += _peer_container_runtime_flags(project, graphify=graphify)
    if peers_subcmd is not None:
        # Non-loop mode (e.g. `peers research /work ...`): run an explicit peers
        # subcommand instead of the default `peers run` loop, reusing ALL the
        # container plumbing above. The subcmd carries its own args, so neither
        # --max-ticks nor extra_args are injected.
        argv += [CONTAINER_IMAGE, *peers_subcmd]
        return argv
    argv += [CONTAINER_IMAGE, "run"]
    if max_ticks is not None:
        argv += ["--max-ticks", str(max_ticks)]
    argv.extend(extra_args)
    return argv


def _peer_container_runtime_flags(
    project: Project, *, graphify: tuple[str, str] | None = None,
) -> list[str]:
    """The shared podman flags + mounts + auth/netns wiring for a peer
    container — everything between ``podman run [-d] --rm --name N`` and
    the image reference.

    ``graphify=(endpoint, api_key)`` threads the opt-in graphify MCP env into
    the container (endpoint inlined; key INHERITED via ``-e GRAPHIFY_API_KEY``
    so it never enters argv). ``None`` => byte-identical to a no-graphify run.

    Extracted from :func:`_build_container_argv` so the ``peers-ctl
    doctor --claude-smoke`` probe can launch a throwaway claude in a
    container wired *exactly* like a real peer turn (same ``--read-only``
    + tmpfs/``~/.claude`` mount layout that triggered the 2.1.145 config
    hang, and the same auth-proxy / egress / userns mode). Behaviour-
    preserving: ``_build_container_argv`` composes the identical argv it
    did before, so the existing container-argv tests cover this helper.
    """
    home = Path.home()
    auth_proxy = _auth_proxy_enabled(home)
    flags = [
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        # raise pids cgroup limit (podman default 2048). The
        # in-container reaper (see health_guard._reap_orphans_if_pid1)
        # is the primary defense — this is belt-and-suspenders for
        # thorough-stack runs where claude/codex spawn many short-lived
        # node helpers per tick. 8192 is well below typical host
        # caps and matches the multi-hour run profile.
        "--pids-limit=8192",
        # Phase-2 hardening B1 (post-v9 audit synthesis, see
        # docs/plans/2026-05-26-peers-audit-synthesis.md): with codex's
        # internal workspace-write bubblewrap bypassed (patch),
        # the peers container IS the sandbox boundary. Read-only
        # rootfs + explicit tmpfs mounts close the persistence-attack
        # class (prompt-injection cannot drop binaries into /usr,
        # /etc, /var). tmpfs targets cover paths real workloads write:
        #   /tmp                  scratch for shell tools, codex temp
        #   ~/.cache     pip / xdg cache
        #   ~/.npm       npm install cache
        # nosuid+nodev on every tmpfs prevents mode-escalation if the
        # rootfs is later opened up.
        "--read-only",
        # B108: container-internal mount destinations, not host paths.
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m",  # nosec B108
        "--tmpfs", "~/.cache:rw,nosuid,nodev,size=256m",
        "--tmpfs", "~/.npm:rw,nosuid,nodev,size=128m",
        # opencode writes XDG state to ~/.local/state/opencode; under the
        # read-only rootfs that path needs a writable tmpfs (harmless for
        # claude/codex runs that never touch it).
        "--tmpfs", "~/.local/state:rw,nosuid,nodev,size=64m",
        "-v", f"{Path(project.path).resolve()}:/work",
        "-v", f"{home / '.codex'}:~/.codex",
    ]
    if auth_proxy:
        # current Claude Code stores the OAuth credential inside
        # ~/.claude/.credentials.json. In auth-proxy mode the workspace must
        # get an empty writable config dir, not the host credential directory.
        flags += ["--tmpfs", "~/.claude:rw,nosuid,nodev,size=16m"]
    else:
        flags += ["-v", f"{home / '.claude'}:~/.claude"]
    # Optional `opencode` peer: mount its config (model defaults / provider
    # setup) and credentials so it can authenticate inside --container, the
    # same way ~/.claude and ~/.codex are mounted. Conditional because opencode
    # is opt-in — a claude+codex run has neither dir and must not get an empty
    # mount. Read-write so OAuth token refresh persists (parity with ~/.codex).
    _opencode_config = home / ".config" / "opencode"
    _opencode_data = home / ".local" / "share" / "opencode"
    if _opencode_config.is_dir():
        flags += ["-v", f"{_opencode_config}:~/.config/opencode"]
    if _opencode_data.is_dir():
        flags += ["-v", f"{_opencode_data}:~/.local/share/opencode"]
    # Legacy mode: claude reads its main config from ~/.claude.json.
    # In the hardened default, only the auth-proxy sidecar gets that
    # rw mount and the workspace receives ANTHROPIC_BASE_URL instead.
    if (not auth_proxy) and (home / ".claude.json").is_file():
        flags += ["-v",
                  f"{home / '.claude.json'}:~/.claude.json"]
    # Per-user git identity so peer commits have a sensible author.
    if (home / ".gitconfig").is_file():
        flags += ["-v",
                  f"{home / '.gitconfig'}:~/.gitconfig:ro"]
    # Phase-2 hardening B2: route the peers container through the
    # egress-proxy sidecar by sharing its network namespace. The
    # sidecar allow-lists outbound HTTPS to LLM API hostnames; all
    # other egress is denied. Set HTTPS_PROXY/HTTP_PROXY so well-
    # behaved SDKs (Anthropic, OpenAI, requests, urllib, node-fetch)
    # route via the sidecar. NO_PROXY=localhost keeps loopback
    # direct so peer<->proxy itself doesn't loop.
    if EGRESS_PROXY_DISABLED and auth_proxy:
        # Joins the auth-proxy's netns → must share the auth-proxy's
        # userns too, so it owns the netns and can mount sysfs.
        flags += [
            f"--userns=container:{_auth_proxy_container_name(project)}",
            f"--network=container:{_auth_proxy_container_name(project)}",
        ]
    elif EGRESS_PROXY_DISABLED:
        # Owns its own netns (PODMAN_NETWORK or default slirp/pasta), so a
        # self-minted keep-id userns DOES own that netns — sysfs is fine.
        flags += ["--userns=keep-id"]
        if PODMAN_NETWORK:
            flags += [f"--network={PODMAN_NETWORK}"]
    else:
        # Full isolation: share BOTH the egress-proxy's userns and netns
        # (same owner) so the container owns the joined netns and can
        # mount sysfs. The shared userns is the proxy's keep-id mapping,
        # so /work FS-perm alignment is preserved.
        flags += [
            f"--userns=container:{_proxy_container_name(project)}",
            f"--network=container:{_proxy_container_name(project)}",
        ]
        flags += [
            "-e", f"HTTPS_PROXY={EGRESS_PROXY_URL}",
            "-e", f"HTTP_PROXY={EGRESS_PROXY_URL}",
            "-e", "NO_PROXY=localhost,127.0.0.1,::1",
        ]
    if auth_proxy:
        flags += ["-e", f"ANTHROPIC_BASE_URL={AUTH_PROXY_URL}"]
        # ANTHROPIC_BASE_URL alone is inert: the claude CLI gates on having a
        # credential before it issues any request, so without a token it prints
        # "Not logged in" and never reaches the sidecar. Hand it a NON-SECRET
        # placeholder bearer so it authenticates locally and routes through the
        # proxy, which strips this header and injects the real OAuth token.
        flags += ["-e", f"ANTHROPIC_AUTH_TOKEN={AUTH_PROXY_PLACEHOLDER_TOKEN}"]
    for env_key in _project_provider_env_keys(project):
        flags += ["-e", env_key]
    if graphify is not None:
        endpoint, _api_key = graphify
        # endpoint is not secret (inline); the key is INHERITED from the podman
        # launch env (-e GRAPHIFY_API_KEY, no value) so it never enters argv.
        flags += [
            "-e", f"{GRAPHIFY_ENDPOINT_ENV}={endpoint}",
            "-e", GRAPHIFY_API_KEY_ENV,
        ]
    return flags


_DURATION_RE = re.compile(r"^(?P<n>\d+)(?P<unit>[smhdw]?)$")
_DURATION_MULTIPLIERS = {
    "": 1, "s": 1, "m": 60, "h": 3600,
    "d": 86400, "w": 604800,
}


def _parse_duration(text: str) -> int:
    """Operator-friendly duration parser.

    Accepts a positive integer optionally followed by a single
    unit suffix:
      `300`   → 300 seconds (bare int)
      `300s`  → 300 seconds
      `90m`   → 5400 seconds
      `6h`    → 21600 seconds
      `2d`    → 172800 seconds
      `1w`    → 604800 seconds

    Whitespace is stripped. Anything else (mixed units `6h2m`,
    decimals, negative, zero, garbage) raises ValueError — silent
    misinterpretation of budget caps is worse than refusing to
    parse.
    """
    if not isinstance(text, str):
        raise ValueError(f"duration must be a string, got {type(text).__name__}")
    s = text.strip()
    m = _DURATION_RE.match(s)
    if not m:
        raise ValueError(
            f"invalid duration {text!r}: expected POSITIVE_INT[s|m|h|d|w] "
            "(e.g. `6h`, `2d`, `300s`)"
        )
    n = int(m.group("n"))
    if n <= 0:
        raise ValueError(
            f"duration {text!r} must be positive (got {n})"
        )
    return n * _DURATION_MULTIPLIERS[m.group("unit")]


def _read_state(project: Project) -> dict[str, Any] | None:
    """Best-effort load of `.peers/state.json`. Returns None if absent
    or malformed — caller treats missing state as 'fresh project'.

    BUG-198: read via the no-follow helper so a same-UID project peer
    can't symlink ``.peers/state.json`` to a forged budget file and spoof
    ``spent_runtime_s`` / ``consecutive_failures`` past the controller's
    pre-flight budget-exhausted check. Symmetric to BUG-196 (write side).
    BUG-224: ``read_text_no_symlink`` only carries ``O_NOFOLLOW`` on the
    leaf; the kernel still resolves a symlinked ``.peers`` ancestor.
    Route through ``read_bytes_under_root_no_follow`` so every ancestor
    is walked with ``O_DIRECTORY|O_NOFOLLOW`` — a symlinked ``.peers``
    raises OSError and is treated as 'no state' (fail-safe) rather than
    trusting attacker-controlled budget JSON from an outside directory.
    """
    try:
        raw = read_bytes_under_root_no_follow(
            Path(project.path), [".peers", "state.json"],
        )
        state = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return None
    return state if isinstance(state, dict) else None


def _budget_mapping(state: dict[str, Any]) -> dict[str, Any]:
    budget = state.get("budget")
    if isinstance(budget, dict):
        return budget
    budget = {}
    state["budget"] = budget
    return budget


def _budget_number(budget: dict[str, Any], key: str) -> int | float:
    value = budget.get(key)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        return 0
    return value


def _write_state(project: Project, state: dict[str, Any]) -> None:
    """Atomic-replace write of `.peers/state.json`. Mirrors the
    inner peers loop's persistence pattern so an interrupted write
    cannot leave a corrupt file.

    BUG-196: routes through ``atomic_write_text_in_dir_no_symlink`` so
    a same-UID project peer can't pre-plant ``.peers/state.json.tmp``
    or the leaf as a symlink to a same-user writable file and redirect
    the controller's pre-replace write.
    """
    path = Path(project.path) / ".peers" / "state.json"
    atomic_write_text_in_dir_no_symlink(
        path, json.dumps(state, indent=2, sort_keys=True),
    )


def _budget_override_path(project: Project) -> Path:
    return Path(project.path) / ".peers" / OPERATOR_BUDGET_OVERRIDE_FILE


def _persist_budget_override(project: Project, **caps: int) -> None:
    """Merge operator cap overrides into `.peers/budget-overrides.json`.

    This sidecar is what makes `--max-runtime` actually stick: the inner
    loop re-overlays config.yaml's caps onto state.budget on every start
    (clobbering any value we write to state.json), then re-applies this
    sidecar on top. Writing it here also fixes the first-start case —
    state.json does not exist yet on a freshly-init'd project, so a
    state-only write silently dropped the override.
    """
    path = _budget_override_path(project)
    existing: dict[str, object] = {}
    try:
        loaded = json.loads(
            read_bytes_under_root_no_follow(
                Path(project.path), [".peers", OPERATOR_BUDGET_OVERRIDE_FILE],
            )
        )
        if isinstance(loaded, dict):
            existing = loaded
    except (OSError, ValueError, RecursionError):
        # json.loads raises RecursionError (not a ValueError
        # subclass) on a sufficiently deep malformed JSON array; treat
        # it as malformed input and fall back to existing={} so a
        # pre-staged deep `.peers/budget-overrides.json` cannot crash
        # `peers-ctl start` before the operator's caps are applied.
        existing = {}
    existing.update(caps)
    path.parent.mkdir(parents=True, exist_ok=True)
    # avoid predictable ``.tmp`` symlink redirection by
    # writing through the no-follow atomic helper, same as state.json.
    atomic_write_text_in_dir_no_symlink(
        path, json.dumps(existing, indent=2, sort_keys=True),
    )


def _clear_budget_override(project: Project) -> None:
    try:
        _budget_override_path(project).unlink()
    except FileNotFoundError:
        pass


def _apply_budget_overrides(
    project: Project, max_runtime_s: int | None, reset_budget: bool,
) -> None:
    """Honour operator-supplied budget flags BEFORE the peers process is
    spawned. No-op when both flags are inactive.

    `--max-runtime` is persisted to the `.peers/budget-overrides.json`
    sidecar (so it survives the inner loop's config.yaml overlay AND works
    before state.json exists), and mirrored into state.json when present so
    the pre-flight budget-exhausted check and `peers-ctl peek` see it
    immediately. `--reset-budget` clears the sidecar (back to config
    defaults) and zeroes the spent counters in state.json.
    """
    if max_runtime_s is None and not reset_budget:
        return
    if reset_budget:
        # Returning to config defaults: drop any persisted cap override.
        _clear_budget_override(project)
    if max_runtime_s is not None:
        _persist_budget_override(project, max_runtime_s=max_runtime_s)
    state = _read_state(project)
    if state is None:
        # First start of a freshly-init'd project: state.json doesn't
        # exist yet. The sidecar above already carries the override; the
        # orchestrator applies it after building initial state.
        return
    budget = _budget_mapping(state)
    if reset_budget:
        # Spent counters → 0; preserve caps and historic metadata.
        for k in ("spent_runtime_s", "spent_iterations",
                  "spent_tokens", "wasted_runtime_s",
                  "consecutive_failures"):
            budget[k] = 0
        budget["spent_usd"] = 0.0
    if max_runtime_s is not None:
        budget["max_runtime_s"] = max_runtime_s
    _write_state(project, state)


def _check_budget_or_abort(project: Project, *, force: bool) -> None:
    """Refuse to start a project whose recorded spent_runtime_s is
    >= max_runtime_s. The inner loop would exit on the first tick
    with sentinel `budget:max_runtime` anyway — silently — and the
    operator has to read logs to figure out why. Hint at the three
    actionable recoveries (bump cap / reset counters / --force).

    `--force` is the explicit operator override for the
    "record-the-sentinel-anyway" case.
    """
    if force:
        return
    state = _read_state(project)
    if state is None:
        return
    budget = _budget_mapping(state)
    spent = _budget_number(budget, "spent_runtime_s")
    cap = _budget_number(budget, "max_runtime_s")
    if cap <= 0 or spent < cap:
        return
    pct = (spent / cap) * 100 if cap else 0
    raise ValueError(
        f"budget already exhausted for project {project.name!r}: "
        f"spent_runtime_s={spent}s ({pct:.0f}% of max_runtime_s={cap}s). "
        f"The loop would exit immediately with sentinel "
        f"`budget:max_runtime` after 0 ticks. To continue:\n"
        f"  • bump the cap:  peers-ctl start {project.name} "
        f"--max-runtime 12h\n"
        f"  • reset counters: peers-ctl start {project.name} "
        f"--reset-budget\n"
        f"  • record sentinel anyway: peers-ctl start {project.name} "
        f"--force"
    )


def _start_project_preflight(
    project: Project, max_ticks: int | None, max_usd: float | None,
    max_runtime_s: int | None = None,
    reset_budget: bool = False, force: bool = False,
) -> None:
    if is_pid_alive(project.pid):
        raise ValueError(
            f"project {project.name!r} is already running "
            f"(pid {project.pid})"
        )
    if not Path(project.path).is_dir():
        raise ValueError(
            f"project path no longer exists: {project.path}"
        )
    if not (Path(project.path) / ".peers" / "config.yaml").exists():
        raise ValueError(
            f"{project.path}/.peers/config.yaml missing; "
            f"run `peers -C {project.path} init` first"
        )
    if max_ticks is not None and max_ticks <= 0:
        raise ValueError("max_ticks must be positive when provided")
    if (max_usd is not None
            and (not math.isfinite(max_usd) or max_usd <= 0)):
        raise ValueError("max_usd must be positive when provided")
    if max_runtime_s is not None and max_runtime_s <= 0:
        raise ValueError("max_runtime_s must be positive when provided")
    # Order matters: apply overrides FIRST (a --reset-budget should
    # clear an exhausted state before the abort-check inspects it).
    _apply_budget_overrides(project, max_runtime_s, reset_budget)
    _check_budget_or_abort(project, force=force)


def _start_project_log_path(store: Store, project: Project) -> Path:
    log_path = store.safe_log_path_for(project)
    _ensure_private_dir(log_path.parent)
    if log_path.is_symlink():
        raise ValueError(
            f"refusing to write log through symlink: {log_path}"
        )
    return log_path


def _run_extra_args(max_usd: float | None, extra_args: Sequence[str]) -> list[str]:
    run_extra_args = list(extra_args)
    if max_usd is not None:
        run_extra_args += ["--max-usd", str(max_usd)]
    return run_extra_args


def _start_container_streamer(log_path: Path, cid: str) -> subprocess.Popen:
    log_fp = open_text_in_dir_no_symlink(log_path.parent, log_path.name, "a")
    try:
        return subprocess.Popen(
            [PODMAN_CMD, "logs", "-f", cid],
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        try:
            log_fp.close()
        except OSError:
            pass


def _start_project_container(
    store: Store, project: Project, log_path: Path,
    max_ticks: int | None, max_usd: float | None, extra_args: Sequence[str],
    *,
    force: bool = False,
    trust_egress_allow: bool = False,
    skip_claude_smoke: bool = False,
    peers_subcmd: Sequence[str] | None = None,
) -> int:
    cname = _container_name(project)
    if _container_running(cname):
        raise ValueError(
            f"project {project.name!r} already has a running container ({cname})"
        )
    # Escalate warn to error for audit-integrity modes (Bug D): the project
    # already has a stale config if init ran against the old image.
    project_modes = _read_project_modes_applied(project)
    drift_level, drift_msg = enforce_container_drift_for_modes(project_modes)
    if drift_level == "warn" and drift_msg:
        print(f"peers-ctl: warning: {drift_msg}", file=sys.stderr)
    _require_openrouter_env_for_container(project)
    project = _ensure_config_trusted_for_egress(
        store, project, force=force, trust_egress_allow=trust_egress_allow,
    )
    # v26 lesson: a claude peer that cannot authenticate in-container degrades
    # silently and the run continues codex-solo. Prove it can auth BEFORE we
    # spin up the real sidecars (the smoke uses its own throwaway sidecars).
    _ensure_claude_can_authenticate_for_container(
        project, skip=skip_claude_smoke,
    )
    _cleanup_stale_container(cname)
    try:
        # Phase-2 hardening B2: bring the egress-proxy sidecar up FIRST,
        # because the main container may share its network namespace.
        _ensure_egress_proxy_running(project)
        # Phase 14: auth proxy joins the same namespace and owns
        # ~/.claude.json, keeping credentials out of the workspace.
        _ensure_auth_proxy_running(project)
        # Opt-in caged graphify MCP: build + start the serve sidecar joining the
        # proxy netns chain, then thread its endpoint+key into the main
        # container (fail-open: None => no graphify, byte-identical).
        graphify = _ensure_graphify_serve_container(project)
        run = subprocess.run(
            _build_container_argv(
                project, max_ticks, _run_extra_args(max_usd, extra_args),
                graphify=graphify, peers_subcmd=peers_subcmd,
            ),
            cwd=project.path,
            env=(
                {**os.environ, GRAPHIFY_API_KEY_ENV: graphify[1]}
                if graphify is not None else None
            ),
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, check=False,
        )
    except Exception:
        _stop_graphify_best_effort(project)
        _stop_auth_proxy_best_effort(project)
        _stop_egress_proxy_best_effort(project)
        raise
    if run.returncode != 0:
        _stop_graphify_best_effort(project)
        _stop_auth_proxy_best_effort(project)
        _stop_egress_proxy_best_effort(project)
        raise RuntimeError(
            f"podman run failed (rc={run.returncode}): "
            f"{(run.stderr or '').strip()[:400]}"
        )
    cid = (run.stdout or "").strip().splitlines()[-1]
    streamer = _start_container_streamer(log_path, cid)
    starttime = _proc_starttime(streamer.pid)
    starttime_token = str(starttime) if starttime is not None else "MISSING"
    try:
        store.update(
            project.name, state="running", pid=streamer.pid,
            log_path=str(log_path),
            last_started_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
            last_exit=None,
            notes=(
                f"max_ticks={max_ticks} max_usd={max_usd} "
                f"started_by_pid={os.getpid()} starttime={starttime_token} "
                f"container=1 container_name={cname} container_id={cid[:12]} "
                f"auth_proxy={int(_auth_proxy_enabled())} "
                f"auth_proxy_name={_auth_proxy_container_name(project)}"
                + (f" peers_cmd={peers_subcmd[0]}" if peers_subcmd else "")
                + _egress_trust_note_suffix(project)
            ),
        )
    except Exception:
        _stop_container_best_effort(cname)
        _stop_auth_proxy_best_effort(project)
        _stop_egress_proxy_best_effort(project)
        _terminate_spawned_process(streamer)
        raise
    return streamer.pid


def _start_project_host(
    store: Store, project: Project, log_path: Path,
    max_ticks: int | None, max_usd: float | None, extra_args: Sequence[str],
) -> int:
    argv = [PEERS_CMD, "-C", project.path, "run"]
    if max_ticks is not None:
        argv += ["--max-ticks", str(max_ticks)]
    argv.extend(_run_extra_args(max_usd, extra_args))

    # Opt-in caged graphify MCP: build the graph + start the serve sidecar, then
    # hand the driver its endpoint + key via env (fail-open: None => no graphify).
    graphify = _ensure_graphify_serve_host(project)
    child_env = None
    if graphify is not None:
        endpoint, api_key = graphify
        child_env = {
            **os.environ,
            GRAPHIFY_ENDPOINT_ENV: endpoint,
            GRAPHIFY_API_KEY_ENV: api_key,
        }

    log_fp = open_text_in_dir_no_symlink(log_path.parent, log_path.name, "a")
    try:
        proc = subprocess.Popen(
            argv,
            cwd=project.path,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # setsid()
            close_fds=True,
        )
    except Exception:
        # The graphify sidecar started above; don't orphan it if the driver
        # process fails to spawn (symmetric with _start_project_container).
        _stop_graphify_best_effort(project)
        raise
    finally:
        try:
            log_fp.close()
        except OSError:
            pass

    starttime = _proc_starttime(proc.pid)
    # if starttime can't be captured
    # (non-Linux host, no /proc, transient EAGAIN), persist the
    # explicit sentinel "MISSING" instead of the literal "None" string.
    # The matching helper recognises this and refuses to grant a
    # permissive default; the operator gets a stderr warning instead
    # of silently losing the PID-recycle defence.
    if starttime is None:
        starttime_token = "MISSING"
        print(
            f"peers-ctl: warning: could not capture starttime for "
            f"pid {proc.pid} (project {project.name!r}); "
            "future peers-ctl processes will refuse to signal this PID "
            "because it cannot be verified safely. The current Python "
            "process can still stop its direct child. This usually means "
            "/proc/<pid>/stat is unavailable (non-Linux, sandboxed, or "
            "container without /proc).",
            file=sys.stderr,
        )
    else:
        starttime_token = str(starttime)
    try:
        store.update(
            project.name,
            state="running",
            pid=proc.pid,
            log_path=str(log_path),
            last_started_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
            last_exit=None,
            notes=(
                f"max_ticks={max_ticks} max_usd={max_usd} "
                f"started_by_pid={os.getpid()} starttime={starttime_token} "
                f"container=0{_egress_trust_note_suffix(project)}"
            ),
        )
    except Exception:
        _terminate_spawned_process(proc)
        _stop_graphify_best_effort(project)
        raise
    return proc.pid


def start_project(store: Store, project: Project,
                  max_ticks: int | None = None,
                  max_usd: float | None = None,
                  max_runtime_s: int | None = None,
                  reset_budget: bool = False,
                  force: bool = False,
                  trust_egress_allow: bool = False,
                  skip_claude_smoke: bool = False,
                  extra_args: Sequence[str] = (),
                  container: bool = False,
                  research_subcmd: Sequence[str] | None = None,
                  ) -> int:
    """Launch `peers run` in a detached host process or container.

    ``research_subcmd`` (e.g. ``["research", "/work", "--modalities",
    "codebase,web"]``) runs that explicit peers subcommand inside the container
    instead of the default ``peers run`` loop, reusing the full sidecar / auth /
    egress / claude-smoke plumbing. It implies ``--container`` (the isolation +
    egress allow-list are the point) and ignores max_ticks/max_usd.

    `max_runtime_s` (operator CLI: `--max-runtime DURATION`) overrides
    `budget.max_runtime_s` in `.peers/state.json` BEFORE the loop
    starts — useful when an existing project hit its cap and the
    operator wants to give it more time.

    `reset_budget` (operator CLI: `--reset-budget`) zeroes the
    `spent_*` counters in state.json, semantically a 'fresh session'
    on top of the existing project state.

    `force` (operator CLI: `--force`) skips the pre-flight
    `budget already exhausted` abort — the operator explicitly
    accepts that the loop will exit after 0 ticks with the
    `budget:max_runtime` sentinel (useful for recording terminal
    state after a clean external stop).

    `trust_egress_allow` (operator CLI: `--trust-egress-allow`) records the
    exact digest of a non-empty `.peers/config.yaml` egress_allow list after
    host-side review. It is intentionally separate from `force` because
    egress policy is peer-writable and crosses a network trust boundary.
    """
    lock_path = store.config_dir / "locks" / f"{project.name}.start.lock"
    try:
        with _acquire_start_lock(lock_path):
            # full-depth-analysis #8: re-read the AUTHORITATIVE record UNDER the
            # start-lock. The `project` snapshot was read by cmd_start BEFORE the
            # lock, so two concurrent host starts both saw the pre-lock pid (often
            # None) and the second clobbered the first's registry pid (orphaning the
            # live loop). store.get reads fresh from disk and the lock serializes the
            # spawn, so the 2nd start now observes the 1st's persisted pid and the
            # preflight's is_pid_alive guard raises "already running".
            fresh = store.get(project.name)
            if fresh is None:
                raise ValueError(f"project {project.name!r} is no longer registered")
            project = fresh
            _start_project_preflight(
                project, max_ticks, max_usd,
                max_runtime_s=max_runtime_s,
                reset_budget=reset_budget,
                force=force,
            )
            log_path = _start_project_log_path(store, project)
            if research_subcmd is not None or container:
                return _start_project_container(
                    store, project, log_path, max_ticks, max_usd, extra_args,
                    force=force,
                    trust_egress_allow=trust_egress_allow,
                    skip_claude_smoke=skip_claude_smoke,
                    peers_subcmd=research_subcmd,
                )
            return _start_project_host(
                store, project, log_path, max_ticks, max_usd, extra_args
            )
    except TimeoutError as e:
        raise ValueError(str(e)) from e


def stop_project(store: Store, project: Project,
                 grace_s: float = 10.0) -> int:
    """Signal SIGTERM to the project's process group, wait up to
    `grace_s`, then SIGKILL. Returns the final exit status (or 0 if
    the process was already gone).

    container-mode projects delegate to `podman stop` which
    sends SIGTERM into the container, waits `grace_s`, then SIGKILLs.
    The substrate's existing SIGTERM handler routes through
    KeyboardInterrupt so state.save() + lock-release still run.
    """
    # Tear down the opt-in graphify MCP sidecar for BOTH modes (no-op when
    # absent). At the top so it runs even if the main stop below raises.
    _stop_graphify_best_effort(project)
    if _is_container_project(project):
        cname = _container_from_project(project)
        if cname and _container_running(cname):
            # podman stop -t <grace> sends SIGTERM then SIGKILL,
            # but if podman is missing, times out, or returns non-zero, we
            # must fail closed instead of marking the project stopped and
            # tearing down sidecars while the container keeps running.
            try:
                stop_proc = subprocess.run(
                    [PODMAN_CMD, "stop", "-t", str(int(grace_s)), cname],
                    capture_output=True, timeout=grace_s + 30, check=False,
                )
            except FileNotFoundError as e:
                raise RuntimeError(
                    f"refusing to mark {project.name!r} stopped: "
                    f"{PODMAN_CMD!r} is not available, so the container "
                    f"{cname!r} cannot be stopped."
                ) from e
            except subprocess.TimeoutExpired as e:
                raise RuntimeError(
                    f"refusing to mark {project.name!r} stopped: "
                    f"`podman stop {cname}` timed out after "
                    f"{grace_s + 30:.0f}s; container may still be running."
                ) from e
            if stop_proc.returncode != 0 or _container_running(cname):
                err_tail = (stop_proc.stderr or b"").decode(
                    "utf-8", errors="replace"
                ).strip().splitlines()[-1:] or [""]
                raise RuntimeError(
                    f"refusing to mark {project.name!r} stopped: "
                    f"`podman stop {cname}` returned {stop_proc.returncode}"
                    f" and container is still running. "
                    f"podman stderr: {err_tail[0]!r}"
                )
        # Tear sidecars down after the main container exits. Stopping
        # them first would cut the LLM CLI's network/auth mid-tick.
        _stop_auth_proxy_best_effort(project)
        _stop_egress_proxy_best_effort(project)
        # Reap the log-streamer pid too if still alive.
        #
        # this branch used to signal whenever the PID was alive,
        # unlike the host branch which gates on _pid_still_matches_startup.
        # A recycled PID in the registry (notes still hold the original
        # streamer starttime) would get SIGTERM'd even though it belongs to
        # an unrelated same-user process. Apply the same starttime check
        # here; if the starttime doesn't match, the streamer is already gone
        # and we just drop the registry entry.
        streamer_pid = project.pid
        if streamer_pid and is_pid_alive(streamer_pid):
            if _pid_still_matches_startup(project):
                try:
                    os.kill(streamer_pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            else:
                print(
                    f"peers-ctl: refusing to signal container log-streamer "
                    f"pid {streamer_pid} for {project.name!r} — starttime "
                    "mismatch (PID was recycled). Skipping.",
                    file=sys.stderr,
                )
        store.update(project.name, state="stopped", pid=None,
                     last_stopped_at=_dt.datetime.now(
                         _dt.timezone.utc
                     ).isoformat())
        return 0

    pid_raw = project.pid
    if (
        isinstance(pid_raw, bool)
        or not isinstance(pid_raw, int)
        or pid_raw <= 0
    ):
        store.update(project.name, state="stopped", pid=None,
                     last_stopped_at=_dt.datetime.now(
                         _dt.timezone.utc
                     ).isoformat())
        return 0
    pid = pid_raw
    if not is_pid_alive(pid):
        store.update(project.name, state="stopped", pid=None,
                     last_stopped_at=_dt.datetime.now(
                         _dt.timezone.utc
                     ).isoformat())
        return 0
    # PID-recycle defence: starttime captured at start (in
    # project.notes) must still match. Otherwise the original loop is
    # dead and a new (unrelated) process owns this PID — refuse to
    # signal it. If starttime was never available but the process is
    # still our direct child, the kernel has already proved ownership to
    # this Python process, so same-process runner users can still stop
    # the child instead of orphaning it.
    if not _pid_still_matches_startup(project):
        if _has_missing_starttime_sentinel(project):
            if not _is_current_child(pid):
                raise RuntimeError(
                    f"refusing to signal pid {pid} for {project.name!r}: "
                    "starttime was unavailable at launch and the process "
                    "is not a child of this peers-ctl process. The PID "
                    "cannot be verified safely; inspect/kill it manually."
                )
            print(
                f"peers-ctl: warning: starttime unavailable for "
                f"{project.name!r}; signaling current child pid {pid} "
                "using same-process ownership.",
                file=sys.stderr,
            )
        else:
            store.update(project.name, state="stopped", pid=None,
                         last_stopped_at=_dt.datetime.now(
                             _dt.timezone.utc
                         ).isoformat(),
                         notes=(project.notes or "") + " stale_pid")
            print(f"peers-ctl: refusing to signal pid {pid} for "
                  f"{project.name!r} — starttime mismatch (PID was "
                  "recycled). Marked stopped.")
            return 0
    pgid = _safe_getpgid(pid)
    _signal(pid, pgid, signal.SIGTERM)

    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        # Reap own-child zombies inline. is_pid_alive (kill(0)) keeps
        # returning True for an exited-but-unreaped child, which would
        # otherwise spin the whole grace_s wall-clock on a process
        # that died milliseconds after SIGTERM. ECHILD means the
        # process isn't ours (e.g. fresh peers-ctl reading the PID
        # from the registry); is_pid_alive stays authoritative there.
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass
        if not _alive_via_pgid_or_pid(pid, pgid):
            break
        time.sleep(0.2)

    if _alive_via_pgid_or_pid(pid, pgid):
        # on Linux the kernel keeps the leader PID allocated for
        # as long as its pgid is referenced by any live group member, so
        # while `_process_group_has_live_members(pgid)` is True the leader
        # PID cannot have been recycled to an unrelated process. In that
        # case killpg targets ONLY the original group and is safe even
        # though `_pid_still_matches_startup` could trip on a transient
        # /proc read race (e.g. reaped leader, stat briefly empty). Only
        # the single-PID fallback needs the BUG-214 recycle gate.
        if _valid_pgid(pgid) and _process_group_has_live_members(pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError as e:
                print(
                    f"peers-ctl: cannot SIGKILL pgid {pgid}: {e}. "
                    f"Process group may still be running.",
                    file=sys.stderr,
                )
        elif _pid_safe_for_post_grace_signal(project, pid):
            _signal(pid, pgid, signal.SIGKILL)
        else:
            print(
                f"peers-ctl: refusing to escalate SIGKILL for pid {pid} "
                f"for {project.name!r} — starttime mismatch after grace "
                "(PID may have been recycled). Marking stopped.",
                file=sys.stderr,
            )
        # Brief sleep to let the kernel reap.
        time.sleep(0.2)

    # If the child was OURS (started by this process), reap it so it
    # doesn't linger as a zombie. ECHILD means it isn't ours (e.g.
    # this is a fresh peers-ctl invocation reading a PID from the
    # registry); that's fine, init will reap.
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass

    store.update(project.name, state="stopped", pid=None,
                 last_stopped_at=_dt.datetime.now(
                     _dt.timezone.utc
                 ).isoformat())
    return 0


def _pid_safe_for_post_grace_signal(project: Project, pid: int) -> bool:
    if _pid_still_matches_startup(project):
        return True
    if _has_missing_starttime_sentinel(project) and _is_current_child(pid):
        print(
            f"peers-ctl: warning: starttime unavailable for "
            f"{project.name!r}; escalating current child pid {pid} "
            "using same-process ownership.",
            file=sys.stderr,
        )
        return True
    return False


def _safe_getpgid(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return None


def _valid_pgid(pgid: object) -> TypeGuard[int]:
    return isinstance(pgid, int) and not isinstance(pgid, bool) and pgid > 0


def _alive_via_pgid_or_pid(pid: int, pgid: int | None) -> bool:
    """Liveness dispatch shared by stop_project's grace loop and
    post-grace recheck. Uses the process-group probe when pgid is a
    real positive int; falls back to single-PID kill(0) otherwise.

    BUG-138: the dispatch must mirror `_valid_pgid` so that a leaked
    pgid of 0 / negative / bool does not take the group-check branch
    and trust the False that `_process_group_has_live_members` now
    returns for any invalid pgid. Without this, an invalid
    pgid would prematurely conclude the process is dead and skip the
    SIGKILL escalation even though the actual PID is still alive.
    """
    # Review W2: OR in the direct PID probe. The group scan adds detection
    # of surviving group children, but if the target re-setpgid's
    # out of the captured pgid the scan finds no members for the stale group
    # and would wrongly declare a still-alive leader dead, skipping SIGKILL.
    # The inline zombie reap in stop_project's grace loop runs before this,
    # so is_pid_alive(pid) does not reintroduce BUG-134 (the zombie is reaped
    # → kill(0) → ProcessLookupError → False).
    if _valid_pgid(pgid):
        return _process_group_has_live_members(pgid) or is_pid_alive(pid)
    return is_pid_alive(pid)


def _process_group_alive(pgid: int | None) -> bool:
    # reject pgid<=0 — killpg(0, sig) targets the CALLER's
    # process group, so escalating to SIGKILL on a leaked 0 would
    # broadcast SIGKILL to peers-ctl itself and any sibling
    # processes sharing its pgrp.
    if not _valid_pgid(pgid):
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _process_group_has_live_members(pgid: int | None) -> bool:
    """True when pgid has at least one non-zombie member.

    ``killpg(pgid, 0)`` reports success for zombie-only groups. That is
    useful for permissions probing but wrong for the stop grace loop:
    zombies cannot consume CPU, hold locks, or receive SIGKILL, and waiting
    for PID 1 to reap them recreates BUG-134's wasted grace window.
    """
    # same caller-pgrp hazard as _process_group_alive; kernel
    # threads have pgid==0 in /proc, so a leaked 0 would also match many
    # entries here and falsely report liveness.
    if not _valid_pgid(pgid):
        return False
    proc_dir = Path("/proc")
    try:
        entries = tuple(proc_dir.iterdir())
    except OSError:
        return _process_group_alive(pgid)

    saw_member = False
    # Review W1: a permission-unreadable /proc entry might be a LIVE group
    # member we can't classify. If any exists, we must not let a visible
    # zombie sibling's `saw_member` short-circuit us into "dead" — fall back
    # to the killpg probe (conservative: biases toward alive → SIGKILL).
    # FileNotFoundError means the process exited (not a member) → skip.
    saw_unreadable = False
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            data = (entry / "stat").read_bytes()
        except FileNotFoundError:
            continue
        except OSError:
            saw_unreadable = True
            continue
        rparen = data.rfind(b")")
        if rparen < 0:
            continue
        rest = data[rparen + 1:].split()
        if len(rest) < 3:
            continue
        try:
            member_pgid = int(rest[2])
        except ValueError:
            continue
        if member_pgid != pgid:
            continue
        saw_member = True
        if rest[0] != b"Z":
            return True
    if saw_member and not saw_unreadable:
        return False
    return _process_group_alive(pgid)


def _has_missing_starttime_sentinel(project: Project) -> bool:
    if not project.notes:
        return False
    for tok in project.notes.split():
        if tok == "starttime=MISSING":
            return True
    return False


def _is_current_child(pid: int | None) -> bool:
    """True when ``pid`` is/was a child of this Python process."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        waited_pid, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return False
    except OSError:
        return False
    return waited_pid in (0, pid)


def _pid_still_matches_startup(project: Project) -> bool:
    """Parse the `starttime=<N>` token from project.notes and compare
    to the current /proc/<pid>/stat starttime. Returns True if they
    match (so we can safely signal the PID) OR if we cannot read
    either side (in which case we fall back to the looser
    "is_pid_alive" check that the caller already performed)."""
    if project.pid is None:
        return False
    if not project.notes:
        return True  # no fingerprint recorded — best-effort signal
    expected: int | None = None
    # distinguish "no token at all"
    # (legacy / external registration) from "token says MISSING"
    # (starttime capture FAILED at start_project time — operator was
    # warned, defence is intentionally off-by-default).
    has_token = False
    is_missing_sentinel = False
    for tok in project.notes.split():
        if tok.startswith("starttime="):
            has_token = True
            value = tok.split("=", 1)[1]
            if value == "MISSING":
                is_missing_sentinel = True
                break
            try:
                expected = int(value)
            except ValueError:
                expected = None
            break
    if is_missing_sentinel:
        # Be CONSERVATIVE here: PID-recycle is the whole point of the
        # check, and the operator was warned at start_project. Refuse
        # to signal so a recycled PID can't be killed by accident.
        return False
    if not has_token:
        return True
    if expected is None:
        return True
    current = _proc_starttime(project.pid)
    if current is None:
        # /proc not readable — be permissive.
        return True
    return current == expected


def _signal(pid: int, pgid: int | None, sig: int) -> None:
    """Best-effort: signal the whole process group first; fall back to
    the lone PID if the group call fails. ProcessLookupError means the
    process is already gone (fine). PermissionError means we cannot
    signal (e.g. uid mismatch) — that's a real divergence: the
    registry thinks we own this PID but the kernel disagrees, so we
    surface it on stderr.
    """
    if _valid_pgid(pgid):
        try:
            os.killpg(pgid, sig)
            return
        except ProcessLookupError:
            pass
        except PermissionError as e:
            print(
                f"peers-ctl: cannot signal pgid {pgid} ({sig}): {e}. "
                f"Falling back to single-PID signal.",
                file=sys.stderr,
            )
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError as e:
        print(
            f"peers-ctl: cannot signal pid {pid} ({sig}): {e}. "
            f"Process may still be running — verify with "
            f"`ps -p {pid}` or rerun as the owning user.",
            file=sys.stderr,
        )
