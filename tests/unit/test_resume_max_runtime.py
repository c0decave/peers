import json
from pathlib import Path

from peers_ctl.cli import cmd_resume
from peers_ctl.store import Project, Store


def _project(tmp_path: Path) -> tuple[Path, Path]:
    cfg = tmp_path / "ctl"
    repo = tmp_path / "proj"
    peers = repo / ".peers"
    peers.mkdir(parents=True)
    (peers / "config.yaml").write_text("driver: orchestrator\n")
    (peers / "checkpoint_requested").write_text("pause\n")
    (peers / "state.json").write_text(json.dumps({
        "budget": {
            "max_runtime_s": 3600,
            "spent_runtime_s": 3600,
            "spent_iterations": 4,
            "spent_tokens": 100,
            "spent_usd": 1.25,
            "wasted_runtime_s": 120,
            "consecutive_failures": 2,
        }
    }))
    Store(cfg).add(Project(name="proj", path=str(repo)))
    return cfg, repo


def test_resume_accepts_additive_max_runtime(tmp_path):
    cfg, repo = _project(tmp_path)

    rc = cmd_resume("proj", max_runtime="+2h", config_dir=cfg)

    assert rc == 0
    state = json.loads((repo / ".peers" / "state.json").read_text())
    assert state["budget"]["max_runtime_s"] == 3600 + 7200
    assert not (repo / ".peers" / "checkpoint_requested").exists()


def test_resume_reset_budget(tmp_path):
    cfg, repo = _project(tmp_path)

    rc = cmd_resume("proj", reset_budget=True, config_dir=cfg)

    assert rc == 0
    budget = json.loads((repo / ".peers" / "state.json").read_text())["budget"]
    assert budget["spent_runtime_s"] == 0
    assert budget["spent_iterations"] == 0
    assert budget["spent_tokens"] == 0
    assert budget["spent_usd"] == 0.0


def test_resume_replaces_malformed_budget_on_reset_and_cap_BUG_233(tmp_path):
    cfg, repo = _project(tmp_path)
    state_path = repo / ".peers" / "state.json"
    state_path.write_text(json.dumps({"budget": [1], "iteration": 7}))

    rc = cmd_resume(
        "proj", reset_budget=True, max_runtime="2h", config_dir=cfg,
    )

    assert rc == 0
    state = json.loads(state_path.read_text())
    assert state["iteration"] == 7
    assert state["budget"]["max_runtime_s"] == 7200
    assert state["budget"]["spent_runtime_s"] == 0
    assert state["budget"]["spent_iterations"] == 0
    assert state["budget"]["spent_tokens"] == 0
    assert state["budget"]["spent_usd"] == 0.0
    assert state["budget"]["wasted_runtime_s"] == 0
    assert state["budget"]["consecutive_failures"] == 0


def test_resume_refuses_symlinked_peers_dir_BUG_223(tmp_path):
    cfg = tmp_path / "ctl"
    repo = tmp_path / "proj"
    repo.mkdir()
    outside = tmp_path / "outside-control"
    outside.mkdir()
    (outside / "checkpoint_requested").write_text("do not remove\n")
    (outside / "state.json").write_text(json.dumps({
        "budget": {
            "max_runtime_s": 3600,
            "spent_runtime_s": 3600,
            "spent_iterations": 4,
            "spent_tokens": 100,
            "spent_usd": 1.25,
            "wasted_runtime_s": 120,
            "consecutive_failures": 2,
        }
    }))
    (repo / ".peers").symlink_to(outside, target_is_directory=True)
    Store(cfg).add(Project(name="proj", path=str(repo)))

    rc = cmd_resume("proj", reset_budget=True, config_dir=cfg)

    assert rc != 0
    assert (outside / "checkpoint_requested").read_text() == "do not remove\n"
    budget = json.loads((outside / "state.json").read_text())["budget"]
    assert budget["spent_runtime_s"] == 3600
