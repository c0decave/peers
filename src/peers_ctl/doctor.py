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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from peers_ctl.runner import (
    AUTH_PROXY_IMAGE,
    CONTAINER_IMAGE as PEERS_IMAGE,
    EGRESS_PROXY_IMAGE,
    PODMAN_CMD,
    _host_peers_version,
    _image_peers_version,
)


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


def run_doctor(probes: tuple[Callable[[], ProbeResult], ...] | None = None
               ) -> int:
    """Run every probe, print the tabular report, return the exit code.

    Returns 0 iff every probe with ``required=True`` returned ``OK``;
    1 otherwise. WARN on a non-required probe is informational and
    does not affect the exit code.

    Caller can inject a custom probe tuple for testing — production
    uses :data:`_PROBES`.
    """
    if probes is None:
        probes = _PROBES
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
    "run_doctor",
]
