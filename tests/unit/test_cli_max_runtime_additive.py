"""Test --max-runtime additive syntax (Task 7.4).

`peers-ctl start <project> --max-runtime +Xh` adds X hours to the
current `budget.max_runtime_s` instead of replacing it. The leading
`+` triggers additive semantics; without it the value is absolute.

Pure-function tests on the duration parser (parse_runtime_duration).
"""
from __future__ import annotations

import json

from peers_ctl.cli import parse_runtime_duration
from peers_ctl.store import Project, Store


def test_absolute_hours_returns_value():
    delta, additive = parse_runtime_duration("6h")
    assert delta == 6 * 3600
    assert additive is False


def test_additive_hours_with_plus():
    delta, additive = parse_runtime_duration("+6h")
    assert delta == 6 * 3600
    assert additive is True


def test_absolute_minutes():
    delta, additive = parse_runtime_duration("30m")
    assert delta == 30 * 60
    assert additive is False


def test_additive_minutes():
    delta, additive = parse_runtime_duration("+30m")
    assert delta == 30 * 60
    assert additive is True


def _project_with_state(tmp_path, state_text: str):
    target = tmp_path / "target"
    peers = target / ".peers"
    peers.mkdir(parents=True)
    (peers / "config.yaml").write_text("driver: orchestrator\n")
    (peers / "state.json").write_text(state_text)
    cfg = tmp_path / "ctl"
    Store(cfg).add(Project(name="snake", path=str(target)))
    return cfg, target


def test_read_project_budget_cap_happy_regular_state(tmp_path):
    from peers_ctl.cli import _read_project_budget_cap

    _cfg, target = _project_with_state(
        tmp_path,
        json.dumps({"budget": {"max_runtime_s": 7200}}),
    )

    assert _read_project_budget_cap(target) == 7200


def test_read_project_budget_cap_malformed_state_is_zero(tmp_path):
    from peers_ctl.cli import _read_project_budget_cap

    _cfg, target = _project_with_state(tmp_path, "{not-json")

    assert _read_project_budget_cap(target) == 0


def test_read_project_budget_cap_deep_malformed_state_is_zero_BUG_516(
    tmp_path,
):
    from peers_ctl.cli import _read_project_budget_cap

    _cfg, target = _project_with_state(tmp_path, "[" * 10000)

    assert _read_project_budget_cap(target) == 0


def test_cmd_start_additive_ignores_symlinked_state_BUG_221(
    tmp_path, monkeypatch,
):
    """BUG-221: additive --max-runtime must not follow a peer-planted
    .peers/state.json symlink and inflate the operator's intended cap."""
    from peers_ctl import cli as cli_mod

    cfg, target = _project_with_state(
        tmp_path,
        json.dumps({"budget": {"max_runtime_s": 1800}}),
    )
    state_path = target / ".peers" / "state.json"
    state_path.unlink()
    attacker_state = tmp_path / "attacker-state.json"
    attacker_state.write_text(
        json.dumps({"budget": {"max_runtime_s": 999999}})
    )
    state_path.symlink_to(attacker_state)

    seen: list[int | None] = []

    def fake_start(
        store, project, max_ticks=None, max_usd=None,
        max_runtime_s=None, reset_budget=False, force=False,
        trust_egress_allow=False, skip_claude_smoke=False,
        extra_args=(), container=False,
    ):
        assert trust_egress_allow is False
        seen.append(max_runtime_s)
        store.update(project.name, state="running", pid=12345)
        return 12345

    monkeypatch.setattr(cli_mod, "start_project", fake_start)

    rc = cli_mod.cmd_start("snake", max_runtime="+1h", config_dir=cfg)

    assert rc == 0
    assert seen == [3600]
