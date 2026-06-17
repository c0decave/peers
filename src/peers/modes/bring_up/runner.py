"""Phase-2 — the tool-driver runner: spawn the tool-under-test, observe it.

The runner renders the driver ``run_case`` template for one Case and runs the
tool as an observed subprocess. **Untrusted case fields are shell-quoted before
argv-splitting** and the command is run WITHOUT a shell, so a malicious corpus
value can never break out of the operator's intended command (the design's
prompt-injection-hygiene stance). Execution is injectable; the real defaults are
:func:`host_executor` and a podman :func:`podman_executor` (container/lab).
"""
from __future__ import annotations

import re
import shlex
import subprocess
import time
from collections import namedtuple
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .manifest import Driver
from .models import Case

#: rc returned for a timed-out run (matches the spine ``_git`` timeout convention).
_TIMEOUT_RC = 124
_PLACEHOLDER_RE = re.compile(r"\{[A-Za-z0-9_.]+\}")

ExecResult = namedtuple("ExecResult", "rc stdout stderr timed_out duration_s")


@dataclass(frozen=True)
class Observation:
    """What the runner saw for one driven case — the 'observe' layer's output."""

    case_id: str
    command: list[str]
    rc: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float


def _placeholder_map(case: Case, target: Path, work: Path, run_id: str) -> dict:
    m = {"{target}": str(target), "{work}": str(work), "{run}": run_id}
    for k, v in {"id": case.id, **case.data}.items():
        m["{case." + k + "}"] = v
    return m


def _resolve(template: str, mapping: dict, *, quote: bool) -> str:
    def repl(match: re.Match) -> str:
        ph = match.group(0)
        if ph not in mapping:
            raise ValueError(f"unresolved driver placeholder {ph}")
        val = mapping[ph]
        if val is None:
            raise ValueError(f"unresolved driver placeholder {ph} (case value is None)")
        return shlex.quote(str(val)) if quote else str(val)

    return _PLACEHOLDER_RE.sub(repl, template)


def host_executor(argv: list[str], *, cwd: str, timeout_s: int) -> ExecResult:
    """Run ``argv`` on the host with a hard timeout; never raises."""
    t0 = time.monotonic()
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout_s, check=False)
        return ExecResult(p.returncode, p.stdout, p.stderr, False,
                          time.monotonic() - t0)
    except subprocess.TimeoutExpired as exc:
        return ExecResult(_TIMEOUT_RC, exc.stdout or "", exc.stderr or "", True,
                          time.monotonic() - t0)


def build_podman_argv(image: str, inner_argv: list[str], *, target: Path,
                      network: str = "none") -> list[str]:
    """Build the ``podman run`` wrapper that executes ``inner_argv`` inside
    ``image`` with the target repo bind-mounted read-write at ``/work``."""
    return [
        "podman", "run", "--rm", f"--network={network}",
        "-v", f"{target}:/work:rw", "-w", "/work",
        image, *inner_argv,
    ]


def podman_executor(image: str, *, target: Path,
                    network: str = "none") -> Callable[..., ExecResult]:
    """An executor that runs each argv inside a disposable podman container."""

    def run(argv: list[str], *, cwd: str, timeout_s: int) -> ExecResult:
        full = build_podman_argv(image, argv, target=target, network=network)
        return host_executor(full, cwd=str(target), timeout_s=timeout_s)

    return run


class ToolRunner:
    """Renders the driver command for a Case and runs it under the resolved
    executor, returning an :class:`Observation`."""

    def __init__(self, driver: Driver, *, target: Path,
                 executor: Callable[..., ExecResult] | None = None) -> None:
        self._driver = driver
        self._target = Path(target)
        if executor is not None:
            self._executor = executor
        elif driver.sandbox == "host":
            self._executor = host_executor
        else:  # container | lab — mandatory sandbox for untrusted corpora
            if not driver.image:
                raise ValueError(f"sandbox {driver.sandbox!r} requires an image")
            self._executor = podman_executor(driver.image, target=self._target)

    def run(self, case: Case, *, work: Path, run_id: str = "") -> Observation:
        mapping = _placeholder_map(case, self._target, Path(work), run_id)
        argv = shlex.split(_resolve(self._driver.run_case, mapping, quote=True))
        cwd = _resolve(self._driver.cwd, mapping, quote=False)
        res = self._executor(argv, cwd=cwd, timeout_s=self._driver.timeout_s)
        return Observation(
            case_id=case.id, command=argv, rc=res.rc, stdout=res.stdout,
            stderr=res.stderr, timed_out=res.timed_out, duration_s=res.duration_s)
