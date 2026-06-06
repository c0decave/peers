"""Item 11: cross-run baseline tracking via `peers-ctl compare`.

Aggregates and side-by-side prints key metrics from N peers projects so
operators can see whether v12 actually converges faster than v11, whether
a substrate tweak improved the BUG-rate, etc. Reads `.peers/state.json`
and `.peers/log/runs.jsonl` directly — no LLM calls, no container starts.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from peers.safe_io import read_text_no_symlink

# bound each project-controlled read so a malicious project
# cannot make the operator's compare command consume unbounded memory.
# state.json and per-bug files are kilobytes in practice; runs.jsonl is
# the heaviest log but stays in the low MB range even after long runs.
_MAX_STATE_BYTES = 1 * 1024 * 1024  # 1 MB
_MAX_STOP_REASON_BYTES = 64 * 1024  # 64 KB
_MAX_BUG_HEAD_BYTES = 8 * 1024  # 8 KB header per BUG file
_MAX_RUNS_BYTES = 32 * 1024 * 1024  # 32 MB runs.jsonl cap


@dataclass
class ProjectMetrics:
    """One project's snapshot, ready to render in a comparison table."""
    name: str
    path: Path
    iteration: int = 0
    spent_runtime_s: int = 0
    spent_iterations: int = 0
    max_runtime_s: int | None = None
    wasted_runtime_s: int = 0
    spent_tokens: int = 0
    spent_usd: float = 0.0
    consecutive_clean_ticks: int = 0
    stop_reason: str = ""
    bugs_total: int = 0
    bugs_by_severity: dict[str, int] = field(default_factory=dict)
    ticks_to_convergence: int | None = None
    idle_timeouts: int = 0
    no_handoffs: int = 0
    api_errors: int = 0
    degraded_events: int = 0
    success_ticks: int = 0
    notes: list[str] = field(default_factory=list)


def _read_state(project_path: Path) -> dict | None:
    # read_text_no_symlink refuses symlinked / hardlinked /
    # non-regular files and caps the read at _MAX_STATE_BYTES so a
    # malicious project cannot disclose a same-user-readable file or
    # exhaust the operator's memory.
    p = project_path / ".peers" / "state.json"
    try:
        raw = read_text_no_symlink(p, max_bytes=_MAX_STATE_BYTES)
        return json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None


def _read_runs_jsonl(project_path: Path) -> list[dict]:
    p = project_path / ".peers" / "log" / "runs.jsonl"
    out: list[dict] = []
    try:
        # bounded no-follow read for the runs log too. Truncation
        # at the cap is harmless: we'd just lose tail entries (compare is
        # a forensic snapshot, not transactional). The last partial line is
        # filtered by the json.loads try/except.
        raw = read_text_no_symlink(p, max_bytes=_MAX_RUNS_BYTES)
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out


def _read_stop_reason(project_path: Path) -> str:
    p = project_path / ".peers" / "last-stop-reason.txt"
    try:
        # stop-reason is a single token; cap at a small bound
        # so we don't even page in a giant attacker-planted file before
        # discovering it has no token.
        text = read_text_no_symlink(p, max_bytes=_MAX_STOP_REASON_BYTES).strip()
    except OSError:
        return ""
    return text.split()[0] if text else ""


def _read_bug_counts(project_path: Path) -> tuple[int, dict[str, int]]:
    """Best-effort scrape of BUG-NNN counts.

    Looks at git log of the project + .peers/bugs/ directory listings.
    """
    bugs_dir = project_path / ".peers" / "bugs"
    by_severity: Counter[str] = Counter()
    total = 0
    if bugs_dir.is_dir() and not bugs_dir.is_symlink():
        for f in bugs_dir.iterdir():
            # refuse symlinks at the leaf so an attacker cannot
            # use the bug-file scan to disclose an arbitrary same-user
            # file through the operator's compare output.
            if f.is_symlink() or not f.is_file():
                continue
            if not f.name.startswith("BUG-"):
                continue
            total += 1
            try:
                head = read_text_no_symlink(f, max_bytes=_MAX_BUG_HEAD_BYTES)
            except OSError:
                continue
            # JSON header line carries severity
            try:
                meta = json.loads(head.splitlines()[0]) if head else {}
                sev = str(meta.get("severity", "unknown")).lower()
                by_severity[sev] += 1
            except (json.JSONDecodeError, IndexError):
                by_severity["unknown"] += 1
    return total, dict(by_severity)


def _safe_int(value: object, default: int | None = 0) -> int | None:
    """Coerce ``value`` to int, returning ``default`` on TypeError/ValueError.

    BUG-403: a corrupted or hand-edited ``state.json`` can have a non-numeric
    string / list / dict in a field that ``collect_project_metrics`` casts
    via ``int()``. Without this guard one bad project poisons the whole
    cross-run report. Mirrors the existing TypeError/ValueError guard on
    ``spent_usd``.
    """
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def collect_project_metrics(name: str, path: Path) -> ProjectMetrics:
    m = ProjectMetrics(name=name, path=path)
    state = _read_state(path) or {}
    m.iteration = _safe_int(state.get("iteration", 0)) or 0
    m.consecutive_clean_ticks = _safe_int(
        state.get("consecutive_clean_ticks", 0)
    ) or 0
    budget = state.get("budget", {}) or {}
    m.spent_runtime_s = _safe_int(budget.get("spent_runtime_s", 0)) or 0
    m.spent_iterations = _safe_int(budget.get("spent_iterations", 0)) or 0
    raw_max = budget.get("max_runtime_s")
    m.max_runtime_s = _safe_int(raw_max, default=None) if raw_max is not None else None
    m.wasted_runtime_s = _safe_int(budget.get("wasted_runtime_s", 0)) or 0
    m.spent_tokens = _safe_int(budget.get("spent_tokens", 0)) or 0
    try:
        m.spent_usd = float(budget.get("spent_usd", 0.0))
    except (TypeError, ValueError):
        m.spent_usd = 0.0

    m.stop_reason = _read_stop_reason(path)
    m.bugs_total, m.bugs_by_severity = _read_bug_counts(path)

    runs = _read_runs_jsonl(path)
    for entry in runs:
        cls = entry.get("classification", "")
        if cls == "idle-timeout":
            m.idle_timeouts += 1
        elif cls == "api-error":
            m.api_errors += 1
        # success / no-handoff distinction comes from the success flag
        if entry.get("success") is True:
            m.success_ticks += 1
        elif entry.get("success") is False and cls == "success":
            m.no_handoffs += 1

    # First tick where consecutive_clean_ticks reached convergence_n
    # Approximation: walk runs ascending; the iteration whose suffix log
    # carries `convergence-reached at iter=N` would be ideal, but here
    # we treat the LAST run's consecutive_clean_ticks as a proxy if it
    # met the goals.convergence_n config (or default 3).
    cfg = state.get("config") or {}
    raw_n = (cfg.get("goals") or {}).get("convergence_n", 3)
    n_needed = _safe_int(raw_n, default=3) or 3
    if m.consecutive_clean_ticks >= n_needed:
        # The convergence first happened (iter - n_needed + 1).
        m.ticks_to_convergence = max(1, m.iteration - n_needed + 1)

    # Peers degraded events from peer_state_after
    peer_states_seen: set[tuple[str, str]] = set()
    for entry in runs:
        peer = entry.get("peer", "")
        st = entry.get("peer_state_after", "")
        if peer and st == "degraded" and (peer, st) not in peer_states_seen:
            peer_states_seen.add((peer, st))
            m.degraded_events += 1

    return m


def render_comparison(metrics_list: list[ProjectMetrics]) -> str:
    """Side-by-side text table comparing projects."""
    if not metrics_list:
        return "peers-ctl compare: no projects to compare\n"
    headers = ["metric"] + [m.name for m in metrics_list]
    rows: list[list[str]] = []

    def add_row(label: str, values: list[str]) -> None:
        rows.append([label] + values)

    def _fmt_int(v: int | None) -> str:
        return "-" if v is None else str(v)

    def _fmt_pct(used: int, cap: int | None) -> str:
        if not cap or cap <= 0:
            return f"{used}s"
        return f"{used}s ({100*used/cap:.0f}%)"

    add_row("iteration", [_fmt_int(m.iteration) for m in metrics_list])
    add_row("runtime",
            [_fmt_pct(m.spent_runtime_s, m.max_runtime_s) for m in metrics_list])
    add_row("wasted",
            [f"{m.wasted_runtime_s}s" for m in metrics_list])
    add_row("tokens", [_fmt_int(m.spent_tokens) for m in metrics_list])
    add_row("usd", [f"${m.spent_usd:.4f}" for m in metrics_list])
    add_row("bugs (total)", [_fmt_int(m.bugs_total) for m in metrics_list])
    sev_keys = ("crit", "high", "med", "low", "info", "unknown")
    for sev in sev_keys:
        if any(m.bugs_by_severity.get(sev, 0) for m in metrics_list):
            add_row(f"bugs ({sev})",
                    [str(m.bugs_by_severity.get(sev, 0)) for m in metrics_list])
    add_row("success ticks", [_fmt_int(m.success_ticks) for m in metrics_list])
    add_row("no-handoff", [_fmt_int(m.no_handoffs) for m in metrics_list])
    add_row("idle-timeouts", [_fmt_int(m.idle_timeouts) for m in metrics_list])
    add_row("api-errors", [_fmt_int(m.api_errors) for m in metrics_list])
    add_row("degraded events", [_fmt_int(m.degraded_events) for m in metrics_list])
    add_row("ticks→convergence",
            [_fmt_int(m.ticks_to_convergence) for m in metrics_list])
    add_row("clean tick streak",
            [_fmt_int(m.consecutive_clean_ticks) for m in metrics_list])
    add_row("stop reason",
            [(m.stop_reason or "-") for m in metrics_list])

    widths = [
        max(len(headers[i]), *(len(row[i]) for row in rows))
        for i in range(len(headers))
    ]

    def _format_row(row: list[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))

    out_lines: list[str] = []
    out_lines.append(_format_row(headers))
    out_lines.append("  ".join("-" * w for w in widths))
    for r in rows:
        out_lines.append(_format_row(r))
    return "\n".join(out_lines) + "\n"


def _resolve_project_dir(name: str, config_dir: Path | None) -> Path | None:
    """Resolve a project name to its on-disk directory.

    Registry first (so ``peers-ctl add /path`` projects resolve to their
    explicit path), then ``$PEERS_PROJECTS_ROOT/<name>`` for bare-name
    projects scaffolded via ``peers-ctl new``. Mirrors
    :func:`peers_ctl.replay._resolve_project_dir`.
    """
    from peers_ctl.cli import projects_root
    from peers_ctl.store import Store

    try:
        project = Store(config_dir).get(name)
    except (OSError, ValueError):
        project = None
    if project is not None:
        p = Path(project.path)
        if p.is_dir():
            return p
    candidate = projects_root() / name
    if candidate.is_dir():
        return candidate
    return None


def cmd_compare(names: list[str], config_dir: Path | None = None) -> int:
    """Thin adapter for cli.py dispatch: render a comparison table.

    Resolves each project name to its directory, collects metrics, and
    prints the side-by-side table to stdout. Returns 0 on success, 2 if
    fewer than two names resolve to existing projects (so the operator
    learns which name was wrong instead of getting an empty table).
    """
    if len(names) < 2:
        print("peers-ctl compare: need at least 2 project names",
              file=sys.stderr)
        return 2

    metrics = []
    missing = []
    for name in names:
        path = _resolve_project_dir(name, config_dir)
        if path is None:
            missing.append(name)
            continue
        metrics.append(collect_project_metrics(name, path))

    for name in missing:
        print(f"peers-ctl compare: no such project: {name}",
              file=sys.stderr)

    if len(metrics) < 2:
        print("peers-ctl compare: need at least 2 resolvable projects "
              f"(resolved {len(metrics)})", file=sys.stderr)
        return 2

    sys.stdout.write(render_comparison(metrics))
    return 0
