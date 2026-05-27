"""Project registry on disk: ~/.config/peers-ctl/projects.yaml plus
per-project log directory ~/.config/peers-ctl/logs/."""
from __future__ import annotations

import datetime as _dt
import fcntl
import logging
import os
import re
import shutil
import stat
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

import yaml

from peers.safe_io import (
    open_text_in_dir_no_symlink,
    open_text_no_symlink,
    read_bytes_no_symlink,
)


log = logging.getLogger(__name__)

_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_VALID_PROJECT_STATES = {
    "fresh", "stopped", "running", "crashed", "unknown",
}
_PROJECTS_REGISTRY_MAX_BYTES = 2 * 1024 * 1024


def default_config_dir() -> Path:
    """Honours $XDG_CONFIG_HOME, falls back to ~/.config."""
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "peers-ctl"
    return Path.home() / ".config" / "peers-ctl"


def is_valid_project_name(name: object) -> bool:
    return isinstance(name, str) and bool(_PROJECT_NAME_RE.match(name))


def validate_project_name(name: object) -> None:
    if not is_valid_project_name(name):
        raise ValueError(
            f"invalid project name {name!r}; expected "
            "[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
        )


def _ensure_plain_dir(path: Path, label: str, *, parents: bool = False) -> None:
    if path.is_symlink():
        raise RuntimeError(f"refusing symlinked {label}: {path}")
    path.mkdir(parents=parents, exist_ok=True, mode=0o700)
    try:
        st = path.lstat()
    except OSError as e:
        raise RuntimeError(f"cannot stat {label}: {path}: {e}") from e
    if stat.S_ISLNK(st.st_mode):
        raise RuntimeError(f"refusing symlinked {label}: {path}")
    if not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"refusing non-directory {label}: {path}")
    # narrow group/other bits if any were set (legacy dirs
    # or wide umask). Don't widen.
    if st.st_mode & 0o077:
        try:
            path.chmod(st.st_mode & ~0o077)
        except OSError:
            pass


@dataclass
class Project:
    name: str
    path: str                  # absolute path to the target repo
    # State semantics (set / inspected by reconcile()):
    #   fresh    - registered but never started; reconcile leaves alone.
    #   running  - loop is active; PID (host) or container (podman) alive.
    #   stopped  - clean exit: peers-ctl stop, or self-termination with a
    #              recognized .peers/last-stop-reason.txt sentinel
    #              (complete / max_ticks / max_iterations / budget:*).
    #   crashed  - process / container CONFIRMED dead and no clean-stop
    #              sentinel present. Hard failure path.
    #   unknown  - liveness could not be determined (e.g. podman ps probe
    #              hit TimeoutExpired or FileNotFoundError). The previous
    #              run *might* still be alive. Resolves on the next
    #              reconcile when the probe succeeds (-> running or
    #              crashed). Distinguished from `crashed` so transient
    #              probe failures don't poison the registry.
    # Defaults to "fresh" so a `peers-ctl add` / `peers-ctl new` makes
    # clear the project hasn't been driven yet.
    state: str = "fresh"
    pid: int | None = None
    log_path: str | None = None
    added_at: str = field(
        default_factory=lambda: _dt.datetime.now(
            _dt.timezone.utc
        ).isoformat()
    )
    last_started_at: str | None = None
    last_stopped_at: str | None = None
    last_exit: int | None = None
    notes: str = ""


class Store:
    """Persistent YAML store for the project registry.

    All mutations go through `mutate()` which holds an exclusive lock
    on the file across read-modify-write, so concurrent peers-ctl
    invocations don't clobber each other.
    """

    def __init__(self, config_dir: Path | None = None) -> None:
        self.config_dir = config_dir or default_config_dir()
        _ensure_plain_dir(self.config_dir, "config dir", parents=True)
        _ensure_plain_dir(self.config_dir / "logs", "logs dir")
        self.path = self.config_dir / "projects.yaml"
        if not self.path.exists():
            self._write_atomic({"projects": []})

    # --- read helpers ---------------------------------------------------

    def list_projects(self) -> list[Project]:
        return self._load_projects(self._read_raw())

    def get(self, name: str) -> Project | None:
        for p in self.list_projects():
            if p.name == name:
                return p
        return None

    def log_path_for(self, name: str) -> Path:
        validate_project_name(name)
        return self.config_dir / "logs" / f"{name}.log"

    def safe_log_path_for(self, project: Project) -> Path:
        """Return a controller-owned log path or raise ValueError.

        The registry is user-editable YAML. Never trust a persisted
        ``log_path`` blindly: otherwise `peers-ctl logs/start/prune` can be
        pointed at arbitrary files outside the controller's log directory.
        """
        validate_project_name(project.name)
        try:
            _ensure_plain_dir(self.config_dir, "config dir", parents=True)
            _ensure_plain_dir(self.config_dir / "logs", "logs dir")
        except RuntimeError as e:
            raise ValueError(str(e)) from e
        raw = (
            Path(project.log_path).expanduser()
            if project.log_path
            else self.log_path_for(project.name)
        )
        if raw.is_symlink():
            raise ValueError(f"refusing symlinked log_path: {raw}")
        logs_root = (self.config_dir / "logs").resolve(strict=False)
        resolved = raw.resolve(strict=False)
        try:
            resolved.relative_to(logs_root)
        except ValueError as e:
            raise ValueError(
                f"refusing log_path outside controller logs dir: {raw}"
            ) from e
        return raw

    def ensure_controller_log_file(self, project: Project) -> Path:
        """Create the controller-owned log file for ``project`` if needed."""
        log_path = self.safe_log_path_for(project)
        with open_text_in_dir_no_symlink(
            log_path.parent, log_path.name, "a"
        ):
            pass
        return log_path

    # --- write helpers --------------------------------------------------

    def add(self, project: Project) -> None:
        validate_project_name(project.name)
        if not isinstance(project.path, str) or not project.path:
            raise ValueError(
                f"project {project.name!r} path must be a non-empty string"
            )
        if project.state not in _VALID_PROJECT_STATES:
            raise ValueError(
                f"project {project.name!r} state must be one of "
                f"{sorted(_VALID_PROJECT_STATES)}, got {project.state!r}"
            )
        if project.pid is not None and (
            isinstance(project.pid, bool) or not isinstance(project.pid, int)
        ):
            raise ValueError(
                f"project {project.name!r} pid must be int or None, "
                f"got {type(project.pid).__name__}"
            )
        project.log_path = str(self.safe_log_path_for(project))
        with self.mutate() as projects:
            if any(p.name == project.name for p in projects):
                raise ValueError(
                    f"project {project.name!r} already exists; "
                    "use a different name or `peers-ctl remove` first"
                )
            self.ensure_controller_log_file(project)
            projects.append(project)

    def remove(self, name: str) -> None:
        with self.mutate() as projects:
            for i, p in enumerate(projects):
                if p.name == name:
                    if p.state == "running":
                        raise ValueError(
                            f"project {name!r} is still running; "
                            "stop it first with `peers-ctl stop`"
                        )
                    del projects[i]
                    return
            raise ValueError(f"no such project: {name}")

    def update(self, name: str, **fields: Any) -> Project:
        unknown = set(fields) - set(Project.__dataclass_fields__)
        if unknown:
            raise ValueError(
                f"unknown project field(s): {', '.join(sorted(unknown))}"
            )
        with self.mutate() as projects:
            for p in projects:
                if p.name == name:
                    for k, v in fields.items():
                        setattr(p, k, v)
                    validate_project_name(p.name)
                    p.log_path = str(self.safe_log_path_for(p))
                    return p
            raise ValueError(f"no such project: {name}")

    @contextmanager
    def mutate(self) -> Iterator[list[Project]]:
        """Open the registry under an exclusive lock and yield the
        mutable list of projects. On normal exit, write back to disk.
        On exception, do NOT write — the caller's mutation is dropped.
        """
        # Use a separate lock file so the YAML itself stays free of
        # opaque fcntl artefacts.
        lock_path = self.config_dir / ".lock"
        with open_text_no_symlink(lock_path, "w") as lock_fp:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
            raw = self._read_raw()
            projects = self._load_projects(raw)
            yield projects
            raw["projects"] = [asdict(p) for p in projects]
            self._write_atomic(raw)
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)

    # --- low-level ------------------------------------------------------

    def _read_raw(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"projects": []}
        try:
            raw_bytes = read_bytes_no_symlink(
                self.path, max_bytes=_PROJECTS_REGISTRY_MAX_BYTES + 1
            )
            if len(raw_bytes) > _PROJECTS_REGISTRY_MAX_BYTES:
                raise RuntimeError(
                    f"projects registry too large: {self.path}: "
                    f"max {_PROJECTS_REGISTRY_MAX_BYTES} bytes"
                )
            data = yaml.safe_load(
                raw_bytes.decode("utf-8", errors="replace")
            ) or {}
        except OSError as e:
            raise RuntimeError(
                f"projects registry unreadable or unsafe: {self.path}: {e}"
            ) from e
        except yaml.YAMLError as e:
            raise RuntimeError(
                f"projects registry corrupt: {self.path}: {e}"
            ) from e
        if not isinstance(data, dict):
            raise RuntimeError(
                f"projects registry corrupt: {self.path}: top-level "
                "value is not a mapping"
            )
        data.setdefault("projects", [])
        if not isinstance(data["projects"], list):
            raise RuntimeError(
                f"projects registry corrupt: {self.path}: "
                "`projects` is not a list"
            )
        return data

    def _load_projects(self, raw: dict[str, Any]) -> list[Project]:
        out: list[Project] = []
        for idx, entry in enumerate(raw.get("projects", [])):
            if not isinstance(entry, dict):
                log.warning(
                    "skipping malformed project registry entry %s in %s: "
                    "expected mapping, got %s",
                    idx, self.path, type(entry).__name__,
                )
                continue
            allowed = {f for f in Project.__dataclass_fields__}
            cleaned = {k: v for k, v in entry.items() if k in allowed}
            try:
                project = Project(**cleaned)
            except TypeError as e:
                # Skip malformed entries rather than crashing the CLI.
                log.warning(
                    "skipping malformed project registry entry %s in %s: %s",
                    idx, self.path, e,
                )
                continue
            normalized = self._normalize_loaded_project(project, idx)
            if normalized is not None:
                out.append(normalized)
        return out

    def _normalize_loaded_project(
        self, project: Project, idx: int,
    ) -> Project | None:
        if not is_valid_project_name(project.name):
            log.warning(
                "skipping malformed project registry entry %s in %s: "
                "invalid project name %r",
                idx, self.path, project.name,
            )
            return None
        if not isinstance(project.path, str) or not project.path:
            log.warning(
                "skipping malformed project registry entry %s in %s: "
                "path must be a non-empty string",
                idx, self.path,
            )
            return None
        if project.state not in _VALID_PROJECT_STATES:
            log.warning(
                "skipping malformed project registry entry %s in %s: "
                "invalid state %r",
                idx, self.path, project.state,
            )
            return None
        if project.pid is not None and (
            isinstance(project.pid, bool) or not isinstance(project.pid, int)
            or project.pid <= 0
        ):
            log.warning(
                "project registry entry %s in %s has invalid pid %r; "
                "marking it crashed",
                idx, self.path, project.pid,
            )
            project.pid = None
            if project.state == "running":
                project.state = "crashed"
        if project.log_path is not None and not isinstance(project.log_path, str):
            log.warning(
                "project registry entry %s in %s has non-string log_path; "
                "resetting to controller log dir",
                idx, self.path,
            )
            project.log_path = None
        try:
            project.log_path = str(self.safe_log_path_for(project))
        except ValueError as e:
            log.warning(
                "project registry entry %s in %s has unsafe log_path: %s; "
                "resetting to controller log dir",
                idx, self.path, e,
            )
            project.log_path = str(self.log_path_for(project.name))
        if project.notes is None:
            project.notes = ""
        elif not isinstance(project.notes, str):
            project.notes = str(project.notes)
        return project

    def _write_atomic(self, raw: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open_text_no_symlink(tmp, "w") as f:
            yaml.safe_dump(raw, f, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        # the rename is atomic but its
        # *metadata* isn't durable until the directory inode flushes.
        # On power loss between os.replace() and the implicit dir
        # flush, the projects.yaml update can be lost despite the
        # syscall returning success. Mirror StateStore.save's
        # parent-fsync. Suppress OSError for filesystems (FAT/NFS)
        # that don't support directory fsync.
        try:
            dir_fd = os.open(str(self.path.parent), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            try:
                os.close(dir_fd)
            except OSError:
                pass


def is_pid_alive(pid: int | None) -> bool:
    """True iff the OS still has a process with this PID."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but isn't ours — still "alive" from our POV.
        return True
    return True


_CLEAN_STOP_REASONS_PREFIXES = (
    "complete", "max_ticks", "max_iterations", "budget:",
)


def _read_stop_reason(project: Project) -> str | None:
    """Read `.peers/last-stop-reason.txt` written by the orchestrator at
    its exit. Returns the reason token (first whitespace-separated
    field) or None if absent/unreadable.

    Used by reconcile() to distinguish clean self-termination (convergence,
    budget exhausted, max_ticks) from a hard process death — fixed v6/v7
    showing up as "crashed" despite running to convergence-complete.
    """
    sentinel = Path(project.path) / ".peers" / "last-stop-reason.txt"
    try:
        text = sentinel.read_text(errors="ignore")
    except (OSError, ValueError):
        return None
    head = text.strip().split(None, 1)
    return head[0] if head else None


def _is_clean_stop_reason(reason: str | None) -> bool:
    if reason is None:
        return False
    return any(reason.startswith(p) for p in _CLEAN_STOP_REASONS_PREFIXES)


def _container_name_from_notes(notes: str | None) -> str | None:
    for tok in (notes or "").split():
        if tok.startswith("container_name="):
            return tok.split("=", 1)[1] or None
    return None


def _probe_container_alive(cname: str) -> bool | None:
    """Tri-state probe via `podman ps`.

    Returns True if podman reports the container running, False if
    podman reports it absent, and None if the probe itself failed
    (binary missing, timeout) — i.e. liveness is unknown, NOT dead.

    Pre-Phase-V8 the failure paths were silently treated as "dead",
    which let a single flaky probe falsely mark a running project
    `crashed`. See in HANDOFF.
    """
    import subprocess
    try:
        podman = (
            os.environ.get("PEERS_CTL_PODMAN_BIN")
            or shutil.which("podman")
            or "podman"
        )
        r = subprocess.run(
            [podman, "ps", "--filter",
             f"name=^{re.escape(cname)}$",
             "--format", "{{.Names}}"],
            capture_output=True, text=True,
            timeout=10, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return cname in (r.stdout or "").split()


def _probe_project_alive(p: Project) -> bool | None:
    """Tri-state liveness probe across host- and container-mode.

    Host mode uses is_pid_alive(), which is always definite (True/False).
    Container mode defers to _probe_container_alive() and may return
    None on transient probe failure.
    """
    is_container = bool(p.notes and "container=1" in p.notes)
    if is_container:
        cname = _container_name_from_notes(p.notes)
        if not cname:
            # We were told it's a container project but lost the name;
            # treat as unknown rather than dead so the operator can fix
            # the registry entry instead of finding state=crashed.
            return None
        return _probe_container_alive(cname)
    return is_pid_alive(p.pid)


def reconcile(store: Store) -> None:
    """Update each project's `state` from the current liveness probe.

    Behaviour matrix (previously):

        probe \\ state | running   | unknown   | crashed | stopped
        --------------+-----------+-----------+---------+--------
        True          | running   | running   | running | running
        False(+sentin)| stopped*  | stopped*  |   -     |   -
        False(no sen) | crashed*  | crashed*  |   -     |   -
        None          | unknown   |   -       |   -     |   -

        '*' = pid cleared and last_stopped_at stamped.
        '-' = no change at all (terminal/unknown sticky).

    `fresh` projects are skipped entirely — they have no PID/container
    to probe yet.

    Terminal-state stickiness: once `crashed` or `stopped`,
    a dead-probe outcome does NOT churn the record. Otherwise every
    dashboard refresh would (a) overwrite `last_stopped_at` with
    'now', and (b) flip a legitimately-stopped project to `crashed`
    if its old sentinel file had been cleaned up.

    Recovery: a successful `alive=True` probe flips any
    non-running state back to `running`, so a transient probe failure
    that briefly demoted state to `unknown` (or a pre-fix false
    `crashed`) self-heals on the next tick.

    step 2a still honoured: clean self-termination writes
    .peers/last-stop-reason.txt and that wins over `crashed` for the
    running -> dead transition.
    """
    with store.mutate() as projects:
        for p in projects:
            if p.state == "fresh":
                continue
            alive = _probe_project_alive(p)
            if alive is True:
                if p.state != "running":
                    p.state = "running"
                continue
            if alive is None:
                # Transient probe failure. Don't downgrade a confirmed
                # terminal state (crashed/stopped) — leave it. For
                # running, demote to `unknown` so the operator sees the
                # ambiguity. unknown stays unknown.
                if p.state == "running":
                    p.state = "unknown"
                continue
            # alive is False — container/PID confirmed dead.
            # Terminal states (stopped, crashed) are sticky: they were
            # set deliberately by stop_project or a prior reconcile,
            # and a fresh dead-probe is not new information.
            if p.state in ("stopped", "crashed"):
                continue
            reason = _read_stop_reason(p)
            if _is_clean_stop_reason(reason):
                p.state = "stopped"
            else:
                p.state = "crashed"
            p.pid = None
            p.last_stopped_at = _dt.datetime.now(
                _dt.timezone.utc
            ).isoformat()


def prune_logs(store: Store, older_than_days: int = 7) -> int:
    """Delete log files for projects that haven't run in `older_than_days`.

    Returns number of files deleted. Never deletes a log for a project
    that is currently `running`.
    """
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(
        days=older_than_days
    )
    deleted = 0
    for p in store.list_projects():
        if p.state == "running":
            continue
        try:
            lp = store.safe_log_path_for(p)
        except ValueError as e:
            log.warning("skipping unsafe log path for %s: %s", p.name, e)
            continue
        if lp is None or not lp.exists():
            continue
        try:
            st = lp.lstat()
        except OSError as e:
            log.warning("could not stat log %s: %s", lp, e)
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            log.warning("skipping unsafe log path %s", lp)
            continue
        mtime = _dt.datetime.fromtimestamp(
            st.st_mtime, tz=_dt.timezone.utc,
        )
        if mtime < cutoff:
            try:
                lp.unlink()
                deleted += 1
            except OSError as e:
                log.warning("could not prune log %s: %s", lp, e)
    return deleted
