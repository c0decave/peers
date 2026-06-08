"""Live and snapshot dashboard rendering for peers-ctl."""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TextIO

from peers.safe_io import (
    read_bytes_under_root_no_follow,
    read_text_under_root_no_follow,
)
from peers_ctl.store import Project, Store, reconcile


_DASHBOARD_STATE_MAX_BYTES = 5 * 1024 * 1024
_DASHBOARD_RUNS_MAX_BYTES = 32 * 1024 * 1024


class DashboardProjectNotFound(ValueError):
    """Raised when a dashboard drilldown targets an unknown project."""

    exit_code = 1


@dataclass(frozen=True)
class DashboardRow:
    name: str
    state: str
    ticks: int
    hard_open: str
    soft_open: str
    blocking: int
    container: str
    last: str
    alert: str = "-"
    event: str = "-"


def _project_rollup(repo: Path) -> tuple[int, int, str]:
    ticks = 0
    last = "-"
    try:
        raw = read_text_under_root_no_follow(
            repo, (".peers", "log", "runs.jsonl"),
            max_bytes=_DASHBOARD_RUNS_MAX_BYTES,
        )
    except (OSError, ValueError):
        raw = ""
    for line in raw.splitlines():
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
    try:
        from peers.bug_hunt import summarize
        blocking = summarize(repo).open_blocking_count
    except Exception:
        blocking = 0
    return ticks, blocking, last


def _load_dashboard_state(repo: Path) -> dict:
    try:
        raw = read_text_under_root_no_follow(
            repo, (".peers", "state.json"),
            max_bytes=_DASHBOARD_STATE_MAX_BYTES + 1,
        )
        data = json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _dashboard_soft_goal_passed(goal, status: dict, n_peers: int) -> bool:
    def consensus_reached(entry: object) -> bool:
        if not isinstance(entry, dict):
            return False
        count = entry.get("consensus_count", 0)
        return (
            isinstance(count, int)
            and not isinstance(count, bool)
            and count >= goal.consensus_needed
        )

    mode = goal.reviewer or "other"
    if mode == "quorum":
        if not goal.quorum_num or not goal.quorum_den:
            return False
        history = status.get("history", [])
        if not isinstance(history, list):
            history = []
        recent = history[-goal.quorum_den:]
        if len(recent) < goal.quorum_den:
            return False
        return (
            sum(
                1 for entry in recent
                if isinstance(entry, dict) and entry.get("pass") is True
            )
            >= goal.quorum_num
        )
    if mode == "both":
        per_peer = status.get("per_peer", {})
        if not isinstance(per_peer, dict):
            per_peer = {}
        reviewers_needed = max(n_peers, 1)
        sufficient = sum(
            1 for entry in per_peer.values()
            if consensus_reached(entry)
        )
        return sufficient >= reviewers_needed
    return consensus_reached(status)


def _dashboard_goal_counts(repo: Path) -> tuple[str, str]:
    try:
        from peers.goals import _GOALS_YAML_MAX_BYTES, _parse_goals_yaml_bytes
        raw = read_bytes_under_root_no_follow(
            repo, (".peers", "goals.yaml"),
            max_bytes=_GOALS_YAML_MAX_BYTES + 1,
        )
    except (OSError, ValueError):
        return "-", "-"
    try:
        goals = _parse_goals_yaml_bytes(raw)
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


def _read_last_stop_reason(repo: Path) -> str:
    try:
        text = read_text_under_root_no_follow(
            repo, (".peers", "last-stop-reason.txt"),
            max_bytes=4096,
        )
    except (OSError, ValueError):
        return ""
    return text.strip().split(None, 1)[0] if text.strip() else ""


def _dashboard_alert(repo: Path, project: Project, state: dict) -> str:
    if project.state in {"crashed", "unknown"}:
        return project.state.upper()
    try:
        read_bytes_under_root_no_follow(
            repo, (".peers", "HALTED.md"), max_bytes=1,
        )
    except (OSError, ValueError):
        pass
    else:
        return "HALTED"
    reason = _read_last_stop_reason(repo)
    if reason.startswith("budget:"):
        return "BUDGET"
    peers = state.get("peers", {})
    if isinstance(peers, dict):
        for value in peers.values():
            if isinstance(value, dict) and value.get("state") == "degraded":
                return "DEGRADED"
    warnings = state.get("warnings", [])
    if isinstance(warnings, list) and warnings:
        return "WARN"
    return "-"


def _latest_session_event(project: Project, repo: Path) -> str:
    try:
        from peers.health_guard import claude_session_jsonl_path
        from peers.peek import newest_session_jsonl, tail_session
    except Exception:
        return "-"
    cwd = "/work" if "container=1" in (project.notes or "") else str(repo)
    jsonl_dir = claude_session_jsonl_path(cwd)
    if jsonl_dir is None:
        return "-"
    jsonl = newest_session_jsonl(jsonl_dir)
    if jsonl is None:
        return "-"
    try:
        lines = list(tail_session(jsonl, follow=False, last=20))
    except OSError:
        return "-"
    return _squash(lines[-1], 80) if lines else "-"


def _squash(text: object, limit: int) -> str:
    s = str(text).replace("\n", " ").replace("\r", " ")
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 3)] + "..."


def load_dashboard_rows(
    config_dir: Path | None = None,
    *,
    reconcile_first: bool = True,
    include_events: bool = False,
    reconciler: Callable[[Store], None] = reconcile,
) -> list[DashboardRow]:
    store = Store(config_dir)
    if reconcile_first:
        reconciler(store)
    rows: list[DashboardRow] = []
    for project in store.list_projects():
        repo = Path(project.path)
        state = _load_dashboard_state(repo)
        ticks, blocking, last = _project_rollup(repo)
        hard_open, soft_open = _dashboard_goal_counts(repo)
        rows.append(DashboardRow(
            name=project.name,
            state=project.state,
            ticks=ticks,
            hard_open=hard_open,
            soft_open=soft_open,
            blocking=blocking,
            container=_dashboard_container_name(project),
            last=last,
            alert=_dashboard_alert(repo, project, state),
            event=(
                _latest_session_event(project, repo)
                if include_events else "-"
            ),
        ))
    return rows


def _render_table(rows: list[DashboardRow], *, include_live: bool) -> str:
    columns = [
        ("NAME", "name"),
        ("STATE", "state"),
        ("TICKS", "ticks"),
        ("HARD_OPEN", "hard_open"),
        ("SOFT_OPEN", "soft_open"),
        ("BLOCKING", "blocking"),
        ("CONTAINER", "container"),
        ("LAST", "last"),
    ]
    if include_live:
        columns.extend([("ALERT", "alert"), ("EVENT", "event")])
    table: list[tuple[str, ...]] = [tuple(header for header, _ in columns)]
    for row in rows:
        table.append(tuple(_squash(getattr(row, attr), 80)
                           for _, attr in columns))
    widths = [max(len(row[i]) for row in table) for i in range(len(table[0]))]
    return "\n".join(
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))
        for row in table
    )


#: Hint appended to the snapshot dashboard so operators discover --live.
LIVE_HINT = (
    "Tip: use --live for a streaming view of all projects "
    "(redraws every --refresh-s seconds; --frames N to render N "
    "frames and exit)."
)


def render_snapshot(
    rows: list[DashboardRow], *, include_live_hint: bool = False,
) -> str:
    if not rows:
        body = "(no projects registered)"
    else:
        body = _render_table(rows, include_live=False)
    if include_live_hint:
        return f"{body}\n\n{LIVE_HINT}"
    return body


def render_live(rows: list[DashboardRow]) -> str:
    header = time.strftime("peers-ctl dashboard --live  %Y-%m-%d %H:%M:%S")
    body = render_snapshot(rows) if not rows else _render_table(
        rows, include_live=True,
    )
    return f"{header}\n{body}"


def _recent_run_lines(repo: Path, *, limit: int = 8) -> list[str]:
    entries: list[dict] = []
    try:
        raw = read_text_under_root_no_follow(
            repo, (".peers", "log", "runs.jsonl"),
            max_bytes=_DASHBOARD_RUNS_MAX_BYTES,
        )
    except (OSError, ValueError):
        return ["- no runs recorded"]
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    if not entries:
        return ["- no runs recorded"]
    lines: list[str] = []
    for entry in entries[-limit:]:
        if entry.get("event") == "exit":
            lines.append(
                "- exit "
                f"reason={_squash(entry.get('reason', '-'), 60)} "
                f"ticks={entry.get('ticks_in_run', '-')}"
            )
            continue
        success = entry.get("success")
        status = (
            "ok" if success is True
            else "fail" if success is False
            else "unknown"
        )
        bits = [
            f"iter={entry.get('iteration', '-')}",
            f"peer={entry.get('peer', '-')}",
            f"class={entry.get('classification', '-')}",
            f"status={status}",
        ]
        reason = entry.get("soft_fail_reason")
        if reason:
            bits.append(f"reason={_squash(reason, 80)}")
        matched = entry.get("matched_error_pattern")
        if matched:
            bits.append(f"pattern={_squash(matched, 80)}")
        lines.append("- " + " ".join(str(bit) for bit in bits))
    return lines


def _bug_report_lines(repo: Path, *, limit: int = 12) -> list[str]:
    try:
        from peers.bug_hunt import summary_dict
        summary = summary_dict(repo)
    except Exception as e:
        return [f"- unable to summarize bug reports: {e}"]
    reports = summary.get("reports", [])
    if not isinstance(reports, list) or not reports:
        return ["- no bug reports"]
    lines: list[str] = []
    for report in reports[:limit]:
        if not isinstance(report, dict):
            continue
        title = _squash(report.get("title") or "-", 80)
        location = _squash(report.get("location") or "-", 80)
        lines.append(
            "- "
            f"{report.get('id', '?')} "
            f"[{report.get('severity', '?')}/{report.get('status', '?')}] "
            f"{title} @ {location}"
        )
        desc = report.get("description")
        if desc:
            lines.append(f"  {_squash(desc, 120)}")
    if len(reports) > limit:
        lines.append(f"- ... {len(reports) - limit} more report(s)")
    return lines or ["- no bug reports"]


def render_project_detail(
    config_dir: Path | None,
    project_name: str,
    *,
    include_events: bool = True,
    reconciler: Callable[[Store], None] = reconcile,
) -> str:
    store = Store(config_dir)
    reconciler(store)
    project = store.get(project_name)
    if project is None:
        raise DashboardProjectNotFound(
            f"peers-ctl dashboard: unknown project {project_name!r}"
        )
    repo = Path(project.path)
    state = _load_dashboard_state(repo)
    ticks, blocking, last = _project_rollup(repo)
    hard_open, soft_open = _dashboard_goal_counts(repo)
    row = DashboardRow(
        name=project.name,
        state=project.state,
        ticks=ticks,
        hard_open=hard_open,
        soft_open=soft_open,
        blocking=blocking,
        container=_dashboard_container_name(project),
        last=last,
        alert=_dashboard_alert(repo, project, state),
        event=_latest_session_event(project, repo) if include_events else "-",
    )
    header = (
        f"peers-ctl dashboard detail  {project.name}  "
        f"{time.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    lines = [
        header,
        "",
        _render_table([row], include_live=include_events),
        "",
        "Project",
        f"- path: {repo}",
        f"- notes: {_squash(project.notes or '-', 160)}",
        "",
        "Recent runs",
        *_recent_run_lines(repo),
        "",
        "Bug reports",
        *_bug_report_lines(repo),
    ]
    return "\n".join(lines)


def _load_rows(config_dir: Path | None = None) -> list[DashboardRow]:
    return load_dashboard_rows(config_dir)


def run(
    config_dir: Path | None = None,
    *,
    refresh_s: float = 2.0,
    iterations: int | None = None,
    output: TextIO | None = None,
    project_name: str | None = None,
) -> int:
    if refresh_s <= 0:
        print("peers-ctl dashboard: --refresh-s must be > 0", file=sys.stderr)
        return 2
    out = output or sys.stdout
    count = 0
    try:
        while iterations is None or count < iterations:
            if project_name is None:
                content = render_live(
                    load_dashboard_rows(config_dir, include_events=True),
                )
            else:
                content = render_project_detail(
                    config_dir, project_name, include_events=True,
                )
            if count or getattr(out, "isatty", lambda: False)():
                out.write("\x1b[2J\x1b[H")
            out.write(content)
            out.write("\n")
            out.flush()
            count += 1
            if iterations is not None and count >= iterations:
                break
            time.sleep(refresh_s)
    except DashboardProjectNotFound as e:
        print(e, file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:
        return 130
    return 0
