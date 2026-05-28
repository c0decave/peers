"""Live dashboard loader and redraw tests."""
from __future__ import annotations

import io
import json
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

    def fake_run(config_dir, *, refresh_s, project_name=None):
        called["config_dir"] = config_dir
        called["refresh_s"] = refresh_s
        called["project_name"] = project_name
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

    def fake_run(config_dir, *, refresh_s, project_name=None):
        called["config_dir"] = config_dir
        called["refresh_s"] = refresh_s
        called["project_name"] = project_name
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
    }


def test_dashboard_live_project_missing_exits_nonzero(tmp_path, capsys):
    rc = dashboard_live.run(
        tmp_path, refresh_s=0.01, iterations=1,
        output=io.StringIO(), project_name="missing",
    )

    assert rc == 1
    assert "unknown project 'missing'" in capsys.readouterr().err
