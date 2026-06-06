"""`peers-ctl doctor` — host environment preflight (Item 9).

`peers-ctl start` silently depends on a handful of host-side things:
podman, /dev/net/tun (default pasta rootless network), three container
images (peers:dev, peers-egress-proxy:dev, peers-auth-proxy:dev), a
host-vs-image version match, claude OAuth or ANTHROPIC_API_KEY, and
git. When any of these is missing the failure mode is usually a
cryptic podman / pasta / claude-cli error in the project log.

Doctor probes each one in isolation and prints a tabular status
report. Each probe returns a :class:`ProbeResult` so the orchestrator
can render the same format for every check and decide the exit code
from the structured results (rather than scraping its own output).

Probes are designed to be monkeypatched cleanly: each external
boundary (`shutil.which`, `subprocess.run`, the path to /dev/net/tun,
the path to ~/.claude.json) is encapsulated in a module-level
constant or thin wrapper so unit tests don't need to actually have
podman installed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from peers_ctl.runner import (
    AUTH_PROXY_IMAGE,
    CONTAINER_IMAGE as PEERS_IMAGE,
    EGRESS_PROXY_IMAGE,
    PODMAN_CMD,
    _ensure_auth_proxy_running,
    _ensure_egress_proxy_running,
    _host_peers_version,
    _image_peers_version,
    _peer_container_runtime_flags,
    _stop_auth_proxy_best_effort,
    _stop_egress_proxy_best_effort,
)
from peers_ctl.store import Project


# Module-level constants are exposed so tests can monkeypatch them
# without reaching into private helpers. The defaults are the real
# production values.
DEV_NET_TUN_PATH: Path = Path("/dev/net/tun")

_WORKAROUND_NO_TUN = (
    "Set PEERS_CTL_NO_EGRESS_PROXY=1 PEERS_CTL_NO_AUTH_PROXY=1 "
    "PEERS_CTL_PODMAN_NETWORK=host to bypass the pasta-network "
    "requirement."
)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of one preflight probe.

    Attributes:
        status: One of ``"OK"``, ``"WARN"``, ``"MISS"``. ``MISS`` means
            the check is hard-failed; ``WARN`` is soft (operator may
            still proceed, e.g. with a bypass env var).
        label: Short human-readable name for the probe (column 2).
        value: Specific finding for the probe (column 3) — e.g. the
            podman version, the image tag, "present" / "not built".
        hint: Optional remediation hint printed after ``value``;
            empty string means no hint.
        required: True if a non-OK result for this probe must drive
            the doctor exit code to 1. False if the probe is purely
            advisory.
    """

    status: str
    label: str
    value: str
    hint: str
    required: bool


# ---------------------------------------------------------------------------
# Thin wrappers around external boundaries (so tests can patch one place)
# ---------------------------------------------------------------------------


def _host_peers_version_safe() -> str | None:
    """Wrap runner._host_peers_version() so tests can patch via
    `peers_ctl.doctor` without touching runner's module-private name."""
    return _host_peers_version()


def _image_peers_version_safe() -> str | None:
    return _image_peers_version()


def _podman_image_exists(image: str) -> bool:
    """Return True iff `podman image exists IMAGE` returns 0.

    Returns False on missing podman, timeout, or any other error —
    the doctor renders "image absent" rather than "doctor crashed",
    which is the right operator UX.
    """
    if shutil.which(PODMAN_CMD) is None:
        return False
    try:
        r = subprocess.run(
            [PODMAN_CMD, "image", "exists", image],
            capture_output=True, timeout=10, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == 0


def _claude_json_path() -> Path:
    """Resolve ``~/.claude.json``. Indirect so tests can patch it."""
    return Path.home() / ".claude.json"


def _podman_version_string() -> str:
    """Return ``podman --version`` output trimmed to the version
    token (e.g. ``"4.9.0"``), or ``"unknown"`` on probe failure."""
    try:
        r = subprocess.run(
            [PODMAN_CMD, "--version"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return "unknown"
    if r.returncode != 0:
        return "unknown"
    # Output looks like "podman version 4.9.0".
    parts = (r.stdout or "").strip().split()
    return parts[-1] if parts else "unknown"


# ---------------------------------------------------------------------------
# Individual probes
# ---------------------------------------------------------------------------


def probe_podman() -> ProbeResult:
    if shutil.which(PODMAN_CMD) is None:
        return ProbeResult(
            status="MISS",
            label="podman",
            value="not found",
            hint="install podman; required for peers-ctl start",
            required=True,
        )
    return ProbeResult(
        status="OK",
        label="podman",
        value=_podman_version_string(),
        hint="",
        required=True,
    )


def probe_dev_net_tun() -> ProbeResult:
    # Use exists() not is_char_device() — the test stubs use a regular
    # file as a stand-in. In production the real /dev/net/tun is a
    # character device but a missing/wrong path still triggers the
    # same pasta-rootless failure mode, so plain exists() is the right
    # probe for "would podman default rootless network come up here?".
    if DEV_NET_TUN_PATH.exists():
        return ProbeResult(
            status="OK",
            label="/dev/net/tun",
            value="present",
            hint="",
            required=True,
        )
    return ProbeResult(
        status="WARN",
        label="/dev/net/tun",
        value="missing",
        hint=_WORKAROUND_NO_TUN,
        required=True,
    )


def probe_peers_image() -> ProbeResult:
    if _podman_image_exists(PEERS_IMAGE):
        version = _image_peers_version_safe() or "unknown"
        return ProbeResult(
            status="OK",
            label="peers:dev image",
            value=version,
            hint="",
            required=True,
        )
    return ProbeResult(
        status="MISS",
        label="peers:dev image",
        value="not built",
        hint="run `make build` to build the peers:dev image",
        required=True,
    )


def probe_egress_proxy_image() -> ProbeResult:
    if _podman_image_exists(EGRESS_PROXY_IMAGE):
        return ProbeResult(
            status="OK",
            label="peers-egress-proxy:dev",
            value="present",
            hint="",
            required=False,
        )
    # Optional: operator may bypass via PEERS_CTL_NO_EGRESS_PROXY=1.
    return ProbeResult(
        status="WARN",
        label="peers-egress-proxy:dev",
        value="not built",
        hint=(
            "run `make proxy-build` (only needed if you do NOT set "
            "PEERS_CTL_NO_EGRESS_PROXY=1)"
        ),
        required=False,
    )


def probe_auth_proxy_image() -> ProbeResult:
    if _podman_image_exists(AUTH_PROXY_IMAGE):
        return ProbeResult(
            status="OK",
            label="peers-auth-proxy:dev",
            value="present",
            hint="",
            required=False,
        )
    return ProbeResult(
        status="WARN",
        label="peers-auth-proxy:dev",
        value="not built",
        hint=(
            "run `make proxy-build` (only needed if you do NOT set "
            "PEERS_CTL_NO_AUTH_PROXY=1)"
        ),
        required=False,
    )


def probe_version_drift() -> ProbeResult:
    """Compare host vs container peers version.

    Not required (the start path enforces its own drift policy via
    `enforce_container_drift_for_modes`) — surfaced here purely so the
    operator notices drift before it bites them at start time. Major
    drift, minor drift, or "image unavailable" all surface as WARN
    so the operator investigates without doctor refusing to run.
    """
    host = _host_peers_version_safe()
    image = _image_peers_version_safe()
    if host and image and host == image:
        return ProbeResult(
            status="OK",
            label="peers version",
            value=f"host={host} container={image}",
            hint="",
            required=False,
        )
    host_str = host or "unknown"
    image_str = image or "unavailable"
    hint = (
        "rebuild the container image with `make build`"
        if host and image
        else "could not query container peers --version "
        "(image missing or podman absent)"
    )
    return ProbeResult(
        status="WARN",
        label="peers version",
        value=f"host={host_str} container={image_str}",
        hint=hint,
        required=False,
    )


def probe_oauth_or_apikey() -> ProbeResult:
    """Either ~/.claude.json (OAuth) or ANTHROPIC_API_KEY must exist.

    The peers loop drives `claude` via OAuth by default
    (~/.claude.json holds the refresh token), but pure-API-key flows
    are also supported through the auth-proxy. One of the two must
    be present or every claude turn will fail.
    """
    cj = _claude_json_path()
    if cj.exists():
        return ProbeResult(
            status="OK",
            label="claude OAuth",
            value="~/.claude.json present",
            hint="",
            required=True,
        )
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ProbeResult(
            status="OK",
            label="claude credentials",
            value="ANTHROPIC_API_KEY set",
            hint="",
            required=True,
        )
    return ProbeResult(
        status="MISS",
        label="claude credentials",
        value="no ~/.claude.json and no ANTHROPIC_API_KEY",
        hint=(
            "run `claude login` to seed OAuth, or export "
            "ANTHROPIC_API_KEY=sk-..."
        ),
        required=True,
    )


def probe_git() -> ProbeResult:
    if shutil.which("git") is None:
        return ProbeResult(
            status="MISS",
            label="git",
            value="not found",
            hint="install git; peers commits each tick",
            required=True,
        )
    return ProbeResult(
        status="OK",
        label="git",
        value="present",
        hint="",
        required=True,
    )


# ---------------------------------------------------------------------------
# Live claude smoke probe (opt-in: `peers-ctl doctor --claude-smoke`)
# ---------------------------------------------------------------------------
#
# A throwaway `claude -p` inside the REAL peer container is the only probe that
# actually exercises the startup path that hung under claude-code 2.1.145
# (read-only home + missing writable ~/.claude.json). It is opt-in because,
# unlike every other probe, it needs the image built + auth + network, makes
# one tiny real API call, and brings the auth/egress sidecars up and down.


SMOKE_CONTAINER_NAME = "peers-doctor-claude-smoke"
_SMOKE_PROMPT = "Reply with the single word: OK"
_DEFAULT_SMOKE_TIMEOUT_S = 90.0
_CONFIG_HANG_DOC = "docs/2026-06-06-claude-2.1.145-config-hang.md"
_CONFIG_NOT_FOUND_SIG = "configuration file not found"


@dataclass(frozen=True)
class SmokeOutcome:
    """Result of one `claude -p` run inside the throwaway peer container.

    Attributes:
        returncode: claude's exit code, or ``None`` when the run was
            killed because it exceeded the timeout (the startup-hang case).
        stdout / stderr: captured output.
        timed_out: True iff the run was killed at the deadline.
        duration_s: wall-clock seconds the run took.
    """

    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float


def _smoke_timeout_s() -> float:
    """Live-smoke deadline. A hung claude idles forever, so any finite
    timeout detects it; 90 s is generous for a cold container + first
    model call. Override with ``PEERS_CTL_SMOKE_TIMEOUT_S``."""
    raw = os.environ.get("PEERS_CTL_SMOKE_TIMEOUT_S", "")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_SMOKE_TIMEOUT_S
    return val if val > 0 else _DEFAULT_SMOKE_TIMEOUT_S


def _smoke_project() -> Project:
    """A throwaway, unpersisted Project so the smoke reuses the real
    runner wiring (container/sidecar names + the exact mount layout).
    Doctor is host-level, so there is no real project — cwd stands in
    for /work."""
    return Project(name="doctor-claude-smoke", path=str(Path.cwd()))


def _ensure_smoke_sidecars(project: Project) -> None:
    """Bring up whatever auth/egress sidecars the configured mode needs
    (each a no-op when its mode is disabled) so the smoke routes auth and
    egress exactly like a real peer turn."""
    _ensure_egress_proxy_running(project)
    _ensure_auth_proxy_running(project)


def _stop_smoke_sidecars(project: Project) -> None:
    """Best-effort teardown of whatever :func:`_ensure_smoke_sidecars`
    started. Always run, even when the smoke raised or timed out."""
    _stop_auth_proxy_best_effort(project)
    _stop_egress_proxy_best_effort(project)


def _podman_rm_force(name: str) -> None:
    """`podman rm -f NAME`, swallowing every error — used to pre-clean a
    stale smoke container and to reap one the timeout left behind (a
    foreground `--rm` does not fire when we kill podman at the deadline)."""
    if shutil.which(PODMAN_CMD) is None:
        return
    try:
        subprocess.run(
            [PODMAN_CMD, "rm", "-f", name],
            capture_output=True, timeout=15, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass


def _as_text(val: object) -> str:
    if val is None:
        return ""
    if isinstance(val, bytes):
        return val.decode("utf-8", "replace")
    return str(val)


def _run_claude_smoke_container(project: Project,
                                timeout_s: float) -> SmokeOutcome:
    """Run `claude -p` once in a throwaway peer container, captured and
    time-bounded.

    The image is ``ENTRYPOINT ["peers"]``, so ``--entrypoint claude``
    overrides it. There is deliberately **no** ``--bare``: we want the
    real hooks/plugins/config startup path, because that is exactly what
    hangs. The container is wired by :func:`_peer_container_runtime_flags`,
    so its ``--read-only`` + mount + auth/netns layout matches a real turn.
    """
    _podman_rm_force(SMOKE_CONTAINER_NAME)
    argv = [
        PODMAN_CMD, "run", "--rm", "--name", SMOKE_CONTAINER_NAME,
        *_peer_container_runtime_flags(project),
        "--entrypoint", "claude",
        PEERS_IMAGE, "-p", _SMOKE_PROMPT,
    ]
    start = time.monotonic()
    try:
        r = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _podman_rm_force(SMOKE_CONTAINER_NAME)
        return SmokeOutcome(
            returncode=None,
            stdout=_as_text(exc.stdout),
            stderr=_as_text(exc.stderr),
            timed_out=True,
            duration_s=time.monotonic() - start,
        )
    return SmokeOutcome(
        returncode=r.returncode,
        stdout=r.stdout or "",
        stderr=r.stderr or "",
        timed_out=False,
        duration_s=time.monotonic() - start,
    )


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _smoke_result(outcome: SmokeOutcome, timeout_s: float) -> ProbeResult:
    """Map a :class:`SmokeOutcome` to a :class:`ProbeResult`.

    OK only when claude produced real model output; every failure is a
    required MISS whose hint distinguishes a startup hang (the 2.1.145
    config-hang class) from other breakage.
    """
    out = (outcome.stdout or "").strip()
    if outcome.timed_out:
        return ProbeResult(
            status="MISS", label="claude smoke",
            value=f"no output in {int(timeout_s)}s — possible startup hang",
            hint=f"claude produced no model output; see {_CONFIG_HANG_DOC}",
            required=True,
        )
    if outcome.returncode == 0 and out:
        return ProbeResult(
            status="OK", label="claude smoke",
            value=f"claude replied ({len(out)} chars in "
                  f"{outcome.duration_s:.1f}s)",
            hint="", required=True,
        )
    if _CONFIG_NOT_FOUND_SIG in (outcome.stderr or "").lower():
        hint = (f"claude-code '{_CONFIG_NOT_FOUND_SIG}' loop — the 2.1.145 "
                f"config-hang class; see {_CONFIG_HANG_DOC}")
    else:
        hint = f"claude -p exited rc={outcome.returncode} with no model output"
    detail = (_first_line(outcome.stderr) or _first_line(outcome.stdout)
              or "no model output")
    return ProbeResult(
        status="MISS", label="claude smoke", value=detail[:80],
        hint=hint, required=True,
    )


def probe_claude_smoke(timeout_s: float | None = None) -> ProbeResult:
    """Live preflight: run a real `claude -p` in a throwaway peer
    container and fail fast if no model output comes back.

    Catches the claude-code startup-hang regression class (and, in
    hardened mode, the auth-proxy/egress path) before a multi-hour run
    silently wastes itself on a claude that never produces output. The
    sidecars are always torn down, and any unexpected error degrades to
    a MISS rather than crashing the doctor report.
    """
    deadline = _smoke_timeout_s() if timeout_s is None else timeout_s
    project = _smoke_project()
    _ensure_smoke_sidecars(project)
    try:
        try:
            outcome = _run_claude_smoke_container(project, deadline)
        except Exception as exc:  # never crash the report
            return ProbeResult(
                status="MISS", label="claude smoke",
                value="smoke probe error",
                hint=f"{type(exc).__name__}: {exc}",
                required=True,
            )
    finally:
        _stop_smoke_sidecars(project)
    return _smoke_result(outcome, deadline)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


# Order matters for the tabular output — keep the most-likely-broken
# things first so the operator sees the actionable hint near the top.
_PROBES: tuple[Callable[[], ProbeResult], ...] = (
    probe_podman,
    probe_dev_net_tun,
    probe_peers_image,
    probe_egress_proxy_image,
    probe_auth_proxy_image,
    probe_version_drift,
    probe_oauth_or_apikey,
    probe_git,
)


def _format_row(result: ProbeResult, label_width: int, value_width: int) -> str:
    """Single row of the tabular output."""
    status_cell = f"[{result.status}]"
    # Pad status to 7 chars so [OK], [WARN], [MISS] line up.
    line = f"  {status_cell:<7} {result.label:<{label_width}}  {result.value:<{value_width}}"
    if result.hint:
        line += f"  {result.hint}"
    return line.rstrip()


def run_doctor(probes: tuple[Callable[[], ProbeResult], ...] | None = None,
               *, claude_smoke: bool = False) -> int:
    """Run every probe, print the tabular report, return the exit code.

    Returns 0 iff every probe with ``required=True`` returned ``OK``;
    1 otherwise. WARN on a non-required probe is informational and
    does not affect the exit code.

    Caller can inject a custom probe tuple for testing — production
    uses :data:`_PROBES`. When ``claude_smoke`` is set, the opt-in live
    :func:`probe_claude_smoke` is appended (it is never in the default
    set because it launches a real container + makes an API call).
    """
    if probes is None:
        probes = _PROBES
    if claude_smoke:
        probes = tuple(probes) + (probe_claude_smoke,)
    results = [probe() for probe in probes]

    label_width = max((len(r.label) for r in results), default=8)
    value_width = max((len(r.value) for r in results), default=8)

    print("peers-ctl doctor — environment preflight")
    print()
    for result in results:
        print(_format_row(result, label_width, value_width))
    print()

    n_ok = sum(1 for r in results if r.status == "OK")
    n_warn = sum(1 for r in results if r.status == "WARN")
    n_miss = sum(1 for r in results if r.status == "MISS")
    required_failed = [
        r for r in results if r.required and r.status != "OK"
    ]
    rc = 1 if required_failed else 0

    summary = f"Summary: {n_ok} ok, {n_warn} warn, {n_miss} miss."
    if rc != 0:
        summary += (
            " Refusing — set the missing requirements then retry."
        )
    print(summary)
    return rc


__all__ = [
    "ProbeResult",
    "SmokeOutcome",
    "PEERS_IMAGE",
    "EGRESS_PROXY_IMAGE",
    "AUTH_PROXY_IMAGE",
    "DEV_NET_TUN_PATH",
    "probe_podman",
    "probe_dev_net_tun",
    "probe_peers_image",
    "probe_egress_proxy_image",
    "probe_auth_proxy_image",
    "probe_version_drift",
    "probe_oauth_or_apikey",
    "probe_git",
    "probe_claude_smoke",
    "run_doctor",
]
