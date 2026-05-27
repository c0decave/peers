"""peers-ctl dashboard: read-only rollup across registered projects."""
from __future__ import annotations

import json

from peers_ctl.cli import cmd_dashboard
from peers_ctl.store import Project, Store


def test_dashboard_empty_registry_prints_nothing_actionable(capsys, tmp_path):
    rc = cmd_dashboard(config_dir=tmp_path)

    assert rc == 0
    assert "no projects" in capsys.readouterr().out.lower()


def test_dashboard_shows_each_project_state_and_goals(capsys, tmp_path):
    store = Store(tmp_path / "ctl")
    for name in ("alpha", "beta"):
        project_path = tmp_path / name
        log_dir = project_path / ".peers" / "log"
        log_dir.mkdir(parents=True)
        (log_dir / "runs.jsonl").write_text(
            json.dumps({"ts": f"2026-05-22T00:00:0{name == 'beta'}Z",
                        "iteration": 1, "peer": "claude"}) + "\n"
        )
        store.add(Project(name=name, path=str(project_path)))

    rc = cmd_dashboard(config_dir=tmp_path / "ctl")

    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "beta" in out
    assert "STATE" in out
    assert "TICKS" in out
    assert "HARD_OPEN" in out
    assert "SOFT_OPEN" in out
    assert "CONTAINER" in out


def test_dashboard_skips_malformed_jsonl_lines(capsys, tmp_path):
    store = Store(tmp_path / "ctl")
    project_path = tmp_path / "alpha"
    log_dir = project_path / ".peers" / "log"
    log_dir.mkdir(parents=True)
    (log_dir / "runs.jsonl").write_text(
        "\n".join([
            json.dumps({"ts": "2026-05-22T00:00:00Z", "peer": "claude"}),
            "{not json}",
            "[]",
            json.dumps({"event": "exit", "ts": "2026-05-22T00:01:00Z"}),
            json.dumps({"ts": "2026-05-22T00:02:00Z", "peer": "codex"}),
            "",
        ])
    )
    store.add(Project(name="alpha", path=str(project_path)))

    rc = cmd_dashboard(config_dir=tmp_path / "ctl")

    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha" in out
    assert "2" in out
    assert "2026-05-22T00:02:00Z" in out


def test_dashboard_shows_goal_counts_and_container_name(
    capsys, tmp_path, monkeypatch
):
    import peers_ctl.cli as cli_mod

    monkeypatch.setattr(cli_mod, "reconcile", lambda store: None)
    store = Store(tmp_path / "ctl")
    project_path = tmp_path / "alpha"
    peers = project_path / ".peers"
    (peers / "log").mkdir(parents=True)
    (peers / "goals.yaml").write_text(
        "goals:\n"
        "  - id: hard-pass\n"
        "    type: hard\n"
        "    cmd: 'true'\n"
        "    pass_when: 'exit_code == 0'\n"
        "  - id: hard-open\n"
        "    type: hard\n"
        "    cmd: 'false'\n"
        "    pass_when: 'exit_code == 0'\n"
        "  - id: soft-open\n"
        "    type: soft\n"
        "    reviewer: both\n"
        "    consensus_needed: 1\n"
        "    prompt: 'review'\n"
    )
    (peers / "state.json").write_text(json.dumps({
        "peer_order": ["claude", "codex"],
        "goals_status": {
            "hard-pass": {"state": "pass"},
            "hard-open": {"state": "fail"},
        },
        "soft_status": {
            "soft-open": {
                "per_peer": {"claude": {"consensus_count": 1}},
            },
        },
    }))
    store.add(Project(
        name="alpha", path=str(project_path), state="running",
        notes="container=1 container_name=peers-ctl_alpha",
    ))

    rc = cmd_dashboard(config_dir=tmp_path / "ctl")

    assert rc == 0
    out = capsys.readouterr().out
    row = next(line for line in out.splitlines() if line.startswith("alpha"))
    cells = row.split()
    assert cells[3] == "1"  # HARD_OPEN
    assert cells[4] == "1"  # SOFT_OPEN
    assert "peers-ctl_alpha" in row
