"""Live dashboard loader and redraw tests."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import peers_ctl.dashboard_live as dashboard_live
from peers_ctl.dashboard_live import DashboardRow, load_dashboard_rows
from peers_ctl.store import Project, Store


def _register_project(config_dir: Path, root: Path, name: str) -> Path:
    project_path = root / name
    log_dir = project_path / ".peers" / "log"
    log_dir.mkdir(parents=True)
    (log_dir / "runs.jsonl").write_text(
        json.dumps({
            "ts": f"2026-05-27T00:00:0{len(name)}Z",
            "iteration": 1,
            "peer": "claude",
        }) + "\n",
        encoding="utf-8",
    )
    Store(config_dir).add(Project(name=name, path=str(project_path)))
    return project_path


def test_load_dashboard_rows_lists_registered_projects(tmp_path):
    config_dir = tmp_path / "ctl"
    _register_project(config_dir, tmp_path, "alpha")
    _register_project(config_dir, tmp_path, "beta")

    rows = load_dashboard_rows(config_dir, reconcile_first=False)

    assert {row.name for row in rows} == {"alpha", "beta"}
    assert all(row.ticks == 1 for row in rows)


def test_live_run_renders_one_iteration(monkeypatch, tmp_path):
    rows = [
        DashboardRow(
            name="alpha",
            state="running",
            ticks=7,
            hard_open="1",
            soft_open="0",
            blocking=2,
            container="peers-ctl_alpha",
            last="2026-05-27T12:00:00Z",
            alert="DEGRADED",
            event="12:00:00 assistant TEXT: still working",
        )
    ]
    monkeypatch.setattr(
        dashboard_live,
        "load_dashboard_rows",
        lambda config_dir, include_events=False: rows,
    )
    out = io.StringIO()

    rc = dashboard_live.run(
        tmp_path, refresh_s=0.01, iterations=1, output=out,
    )

    assert rc == 0
    rendered = out.getvalue()
    assert "peers-ctl dashboard --live" in rendered
    assert "ALERT" in rendered
    assert "DEGRADED" in rendered
    assert "still working" in rendered


def test_project_detail_renders_runs_and_bug_drilldown(monkeypatch, tmp_path):
    config_dir = tmp_path / "ctl"
    project_path = _register_project(config_dir, tmp_path, "alpha")
    (project_path / ".peers" / "log" / "runs.jsonl").write_text(
        "\n".join([
            json.dumps({
                "ts": "2026-05-27T12:00:00Z",
                "iteration": 2,
                "peer": "claude",
                "classification": "success",
                "success": True,
            }),
            json.dumps({
                "event": "exit",
                "reason": "budget:max_runtime",
                "ticks_in_run": 2,
            }),
        ]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        dashboard_live,
        "_bug_report_lines",
        lambda repo: ["- BUG-001 [high/open] overflow @ parser.c:10"],
    )

    rendered = dashboard_live.render_project_detail(
        config_dir, "alpha", reconciler=lambda store: None,
    )

    assert "dashboard detail  alpha" in rendered
    assert "Recent runs" in rendered
    assert "iter=2" in rendered
    assert "reason=budget:max_runtime" in rendered
    assert "Bug reports" in rendered
    assert "BUG-001" in rendered


def test_project_detail_missing_project_raises(tmp_path):
    config_dir = tmp_path / "ctl"
    Store(config_dir)

    try:
        dashboard_live.render_project_detail(
            config_dir, "missing", reconciler=lambda store: None,
        )
    except dashboard_live.DashboardProjectNotFound as e:
        assert "unknown project 'missing'" in str(e)
    else:
        raise AssertionError("missing project should fail the drilldown")


def test_recent_run_lines_mark_unknown_success_state(tmp_path):
    repo = tmp_path / "repo"
    log_dir = repo / ".peers" / "log"
    log_dir.mkdir(parents=True)
    (log_dir / "runs.jsonl").write_text(
        json.dumps({
            "iteration": 1,
            "peer": "claude",
            "classification": "success",
        }) + "\n",
        encoding="utf-8",
    )

    lines = dashboard_live._recent_run_lines(repo)

    assert any("status=unknown" in line for line in lines)


def test_dashboard_live_cli_wires_to_runner(monkeypatch, tmp_path):
    import peers_ctl.cli as cli

    called = {}

    def fake_run(config_dir, *, refresh_s, project_name=None,
                 iterations=None):
        called["config_dir"] = config_dir
        called["refresh_s"] = refresh_s
        called["project_name"] = project_name
        called["iterations"] = iterations
        return 0

    monkeypatch.setattr(dashboard_live, "run", fake_run)

    rc = cli.main([
        "--config-dir", str(tmp_path),
        "dashboard", "--live", "--refresh-s", "0.25",
    ])

    assert rc == 0
    assert called == {
        "config_dir": tmp_path,
        "refresh_s": 0.25,
        "project_name": None,
        "iterations": None,
    }


def test_dashboard_project_cli_renders_detail(monkeypatch, tmp_path, capsys):
    import peers_ctl.cli as cli

    monkeypatch.setattr(
        dashboard_live,
        "render_project_detail",
        lambda config_dir, project: f"detail:{project}:{config_dir}",
    )

    rc = cli.main([
        "--config-dir", str(tmp_path),
        "dashboard", "--project", "alpha",
    ])

    assert rc == 0
    assert f"detail:alpha:{tmp_path}" in capsys.readouterr().out


def test_dashboard_project_cli_missing_project_exits_nonzero(tmp_path, capsys):
    import peers_ctl.cli as cli

    Store(tmp_path)

    rc = cli.main([
        "--config-dir", str(tmp_path),
        "dashboard", "--project", "missing",
    ])

    assert rc == 1
    assert "unknown project 'missing'" in capsys.readouterr().err


def test_dashboard_live_project_cli_wires_to_runner(monkeypatch, tmp_path):
    import peers_ctl.cli as cli

    called = {}

    def fake_run(config_dir, *, refresh_s, project_name=None,
                 iterations=None):
        called["config_dir"] = config_dir
        called["refresh_s"] = refresh_s
        called["project_name"] = project_name
        called["iterations"] = iterations
        return 0

    monkeypatch.setattr(dashboard_live, "run", fake_run)

    rc = cli.main([
        "--config-dir", str(tmp_path),
        "dashboard", "--live", "--project", "alpha", "--refresh-s", "0.5",
    ])

    assert rc == 0
    assert called == {
        "config_dir": tmp_path,
        "refresh_s": 0.5,
        "project_name": "alpha",
        "iterations": None,
    }


def test_dashboard_live_project_missing_exits_nonzero(tmp_path, capsys):
    rc = dashboard_live.run(
        tmp_path, refresh_s=0.01, iterations=1,
        output=io.StringIO(), project_name="missing",
    )

    assert rc == 1
    assert "unknown project 'missing'" in capsys.readouterr().err


def test_render_snapshot_contains_column_headers():
    """Non-blocking sanity: the snapshot table renders all 8 headers
    without invoking the redraw loop (which would block)."""
    rows = [
        DashboardRow(
            name="alpha",
            state="running",
            ticks=3,
            hard_open="0",
            soft_open="0",
            blocking=0,
            container="-",
            last="-",
        )
    ]

    rendered = dashboard_live.render_snapshot(rows)

    for header in (
        "NAME", "STATE", "TICKS", "HARD_OPEN",
        "SOFT_OPEN", "BLOCKING", "CONTAINER", "LAST",
    ):
        assert header in rendered, f"missing header {header!r}"
    # Snapshot must NOT carry the live-only columns.
    assert "ALERT" not in rendered
    assert "EVENT" not in rendered


def test_render_live_contains_alert_and_event_columns():
    """Non-blocking sanity: the live frame adds ALERT + EVENT columns
    on top of the snapshot table without entering the redraw loop."""
    rows = [
        DashboardRow(
            name="alpha", state="running", ticks=3,
            hard_open="0", soft_open="0", blocking=0,
            container="-", last="-",
            alert="HALTED", event="12:00 assistant TEXT",
        )
    ]

    rendered = dashboard_live.render_live(rows)

    for header in (
        "NAME", "STATE", "TICKS", "HARD_OPEN", "SOFT_OPEN",
        "BLOCKING", "CONTAINER", "LAST", "ALERT", "EVENT",
    ):
        assert header in rendered, f"missing header {header!r}"
    assert "peers-ctl dashboard --live" in rendered
    assert "HALTED" in rendered


def test_render_snapshot_appends_live_hint_when_requested():
    """The snapshot includes a discoverability hint pointing at
    `--live` only when the caller asks for it (TTY path)."""
    rows = [
        DashboardRow(
            name="alpha", state="running", ticks=1,
            hard_open="0", soft_open="0", blocking=0,
            container="-", last="-",
        )
    ]

    plain = dashboard_live.render_snapshot(rows)
    hinted = dashboard_live.render_snapshot(rows, include_live_hint=True)

    assert dashboard_live.LIVE_HINT not in plain
    assert dashboard_live.LIVE_HINT in hinted
    assert "--live" in dashboard_live.LIVE_HINT
    # Hint sits after the table, not before.
    assert hinted.index("alpha") < hinted.index(dashboard_live.LIVE_HINT)


def test_render_snapshot_empty_with_hint_still_renders():
    """The empty-registry snapshot still appends the hint when on a
    TTY, so a fresh install knows about --live."""
    hinted = dashboard_live.render_snapshot([], include_live_hint=True)

    assert "(no projects registered)" in hinted
    assert dashboard_live.LIVE_HINT in hinted


def test_cmd_dashboard_frames_requires_live(tmp_path, capsys):
    """--frames without --live is rejected with exit code 2 so users
    don't expect the snapshot to render N times."""
    import peers_ctl.cli as cli

    rc = cli.main([
        "--config-dir", str(tmp_path),
        "dashboard", "--frames", "1",
    ])

    assert rc == 2
    assert "--frames requires --live" in capsys.readouterr().err


def test_cmd_dashboard_frames_rejects_zero(tmp_path, capsys):
    """--frames 0 makes no sense; reject before reaching the loop."""
    import peers_ctl.cli as cli

    rc = cli.main([
        "--config-dir", str(tmp_path),
        "dashboard", "--live", "--frames", "0",
    ])

    assert rc == 2
    assert "--frames must be >= 1" in capsys.readouterr().err


def test_cmd_dashboard_frames_wires_iterations(monkeypatch, tmp_path):
    """--frames N is forwarded to dashboard_live.run as iterations=N
    so the redraw loop exits after N frames."""
    import peers_ctl.cli as cli

    called = {}

    def fake_run(config_dir, *, refresh_s, project_name=None,
                 iterations=None):
        called["iterations"] = iterations
        return 0

    monkeypatch.setattr(dashboard_live, "run", fake_run)

    rc = cli.main([
        "--config-dir", str(tmp_path),
        "dashboard", "--live", "--frames", "3", "--refresh-s", "0.01",
    ])

    assert rc == 0
    assert called["iterations"] == 3


def test_cmd_dashboard_snapshot_tip_shown_on_tty(monkeypatch, tmp_path,
                                                 capsys):
    """The discoverability tip only appears when stdout is a TTY so
    scripts piping the snapshot keep getting clean tables."""
    import peers_ctl.cli as cli

    monkeypatch.setattr(
        dashboard_live,
        "load_dashboard_rows",
        lambda config_dir, reconciler=None: [
            DashboardRow(
                name="alpha", state="running", ticks=1,
                hard_open="0", soft_open="0", blocking=0,
                container="-", last="-",
            ),
        ],
    )
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)

    rc = cli.main([
        "--config-dir", str(tmp_path),
        "dashboard",
    ])

    assert rc == 0
    out = capsys.readouterr().out
    assert dashboard_live.LIVE_HINT in out


def test_cmd_dashboard_snapshot_tip_hidden_when_piped(monkeypatch,
                                                     tmp_path, capsys):
    """Piped/captured snapshots stay parseable: no hint when stdout
    is not a TTY (which is the default under pytest's capsys)."""
    import peers_ctl.cli as cli

    monkeypatch.setattr(
        dashboard_live,
        "load_dashboard_rows",
        lambda config_dir, reconciler=None: [
            DashboardRow(
                name="alpha", state="running", ticks=1,
                hard_open="0", soft_open="0", blocking=0,
                container="-", last="-",
            ),
        ],
    )

    rc = cli.main([
        "--config-dir", str(tmp_path),
        "dashboard",
    ])

    assert rc == 0
    out = capsys.readouterr().out
    assert dashboard_live.LIVE_HINT not in out


def test_dashboard_soft_goal_passed_tolerates_non_list_history_BUG_205():
    """BUG-205 reproducer: _dashboard_soft_goal_passed did
    ``status.get('history', [])[-quorum_den:]`` with no isinstance check. A
    soft_status[gid] value whose ``history`` is a non-list (serialization
    corruption or a peer-written dict-shaped stub) made the slice raise
    TypeError, which propagated out of _dashboard_goal_counts() and killed
    the dashboard for ALL registered projects. Expected: guard with
    isinstance(history, list) and fall back to insufficient quorum."""
    from peers.goals import Goal

    goal = Goal(
        id="soft-quorum",
        type="soft",
        reviewer="quorum",
        quorum_num=2,
        quorum_den=3,
    )
    # history is a dict, not a list — must not raise.
    status = {"history": {"r1": {"pass": True}, "r2": {"pass": True}}}

    assert dashboard_live._dashboard_soft_goal_passed(goal, status, 2) is False


def test_dashboard_goal_counts_survives_corrupt_soft_history_BUG_205(tmp_path):
    """End-to-end: a corrupt non-list history in state.json must not crash
    the whole-registry dashboard render (load_dashboard_rows)."""
    config_dir = tmp_path / "ctl"
    project_path = _register_project(config_dir, tmp_path, "gamma")
    peers_dir = project_path / ".peers"
    (peers_dir / "goals.yaml").write_text(
        "goals:\n"
        "  - id: soft-quorum\n"
        "    description: quorum soft goal\n"
        "    type: soft\n"
        "    reviewer: quorum\n"
        "    quorum: 2/3\n"
        "    pass_when: \"true\"\n",
        encoding="utf-8",
    )
    (peers_dir / "state.json").write_text(
        json.dumps({
            "soft_status": {
                "soft-quorum": {"history": {"bad": "shape"}},
            },
            "peer_order": ["claude", "codex"],
        }),
        encoding="utf-8",
    )

    rows = load_dashboard_rows(config_dir, reconcile_first=False)

    assert any(row.name == "gamma" for row in rows), (
        "BUG-205: corrupt soft-goal history aborted the whole-registry render"
    )
