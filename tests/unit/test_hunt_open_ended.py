from pathlib import Path

import yaml


MODE_DIR = Path("src/peers/templates/modes/hunt-open-ended")


def test_mode_yaml_declares_open_ended():
    cfg = yaml.safe_load((MODE_DIR / "mode.yaml").read_text())

    assert cfg["name"] == "hunt-open-ended"
    assert cfg["terminate_on_convergence"] is False
    assert cfg["composable_with"]


def test_goals_have_no_convergence_gate():
    cfg = yaml.safe_load((MODE_DIR / "goals.yaml").read_text())
    hard = [g for g in cfg["goals"] if g["type"] == "hard"]
    soft = [g for g in cfg["goals"] if g["type"] == "soft"]

    assert not any(g["id"] == "convergence-reached" for g in hard)
    assert len(soft) >= 3
