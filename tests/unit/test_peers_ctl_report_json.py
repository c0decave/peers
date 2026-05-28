import json
from pathlib import Path

from peers_ctl.cli import cmd_report
from peers_ctl.store import Project, Store


def test_controller_report_json_prints_valid_shape(tmp_path: Path, capsys):
    cfg = tmp_path / "ctl"
    repo = tmp_path / "repo"
    (repo / ".peers" / "log").mkdir(parents=True)
    (repo / ".peers" / "config.yaml").write_text("driver: orchestrator\n")
    (repo / ".peers" / "state.json").write_text(json.dumps({
        "budget": {"spent_iterations": 1, "max_iterations": 200},
        "goals_status": {"tests-pass": {"state": "pass"}},
    }))
    (repo / ".peers" / "log" / "runs.jsonl").write_text(
        '{"iteration":1,"peer":"claude","classification":"success"}\n'
    )
    Store(cfg).add(Project(name="repo", path=str(repo)))

    rc = cmd_report("repo", config_dir=cfg, output_format="json")

    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["version"] == 1
    assert parsed["projects"][0]["project"] == "repo"
    assert parsed["projects"][0]["ticks"][0]["iteration"] == 1
