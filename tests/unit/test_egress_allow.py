"""Operator-declared egress allow-list: config `egress_allow:` (host regexes)
threads into the egress-proxy's PEERS_EGRESS_EXTRA_HOSTS. Fail-CLOSED on a
malformed value (never silently widens egress); injection-guarded so one entry
can't smuggle extra tinyproxy filter lines via comma/newline."""
from pathlib import Path

import pytest

from peers_ctl import runner
from peers_ctl.cli import build_parser
from peers_ctl.store import Project
from peers_ctl.store import Store


def _project(tmp_path, body):
    (tmp_path / ".peers").mkdir(exist_ok=True)
    (tmp_path / ".peers" / "config.yaml").write_text(body)
    return Project(name="p", path=str(tmp_path))


def test_config_egress_allow_reads_list(tmp_path):
    p = _project(
        tmp_path,
        "egress_allow:\n  - '^rfc-editor\\.org$'\n  - '^datatracker\\.ietf\\.org$'\n")
    assert runner._config_egress_allow(p) == (
        "^rfc-editor\\.org$", "^datatracker\\.ietf\\.org$")


def test_config_egress_allow_missing_is_empty(tmp_path):
    p = _project(tmp_path, "driver: orchestrator\n")
    assert runner._config_egress_allow(p) == ()


def test_config_egress_allow_malformed_fails_closed(tmp_path):
    p = _project(tmp_path, "egress_allow: not-a-list\n")
    assert runner._config_egress_allow(p) == ()


def test_config_egress_allow_skips_injection_and_nonstrings(tmp_path):
    p = _project(
        tmp_path,
        "egress_allow:\n  - '^ok\\.com$'\n  - 'a,b'\n  - \"line\\nbreak\"\n"
        "  - 123\n  - ''\n")
    # comma/newline entries (would smuggle extra filter lines) + non-string +
    # empty are all dropped; only the clean host survives
    assert runner._config_egress_allow(p) == ("^ok\\.com$",)


def test_config_egress_allow_caps_count(tmp_path):
    many = "\n".join(f"  - h{i}" for i in range(200))
    p = _project(tmp_path, f"egress_allow:\n{many}\n")
    assert len(runner._config_egress_allow(p)) <= 64


def test_egress_extra_allow_hosts_includes_config(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_project_uses_openrouter", lambda _p: False)
    p = _project(tmp_path, "egress_allow:\n  - '^rfc-editor\\.org$'\n")
    assert "^rfc-editor\\.org$" in runner._egress_extra_allow_hosts(p)


def test_egress_extra_allow_hosts_keeps_openrouter_plus_config(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_project_uses_openrouter", lambda _p: True)
    p = _project(tmp_path, "egress_allow:\n  - '^example\\.test$'\n")
    out = runner._egress_extra_allow_hosts(p)
    assert runner.OPENROUTER_EXTRA_HOST_RE in out
    assert "^example\\.test$" in out


def _registered_project(tmp_path: Path, body: str) -> tuple[Store, Project]:
    project_dir = tmp_path / "project"
    (project_dir / ".peers").mkdir(parents=True)
    (project_dir / ".peers" / "config.yaml").write_text(body)
    store = Store(tmp_path / "ctl")
    store.add(Project(name="p", path=str(project_dir)))
    project = store.get("p")
    assert project is not None
    return store, project


def test_container_start_refuses_untrusted_egress_allow_before_proxy_launch(
    tmp_path, monkeypatch,
):
    store, project = _registered_project(
        tmp_path,
        "driver: orchestrator\n"
        "egress_allow:\n"
        "  - '^attacker\\.example$'\n",
    )
    log_path = store.ensure_controller_log_file(project)

    monkeypatch.setattr(runner, "_container_running", lambda _name: False)
    monkeypatch.setattr(runner, "_read_project_modes_applied", lambda _p: [])
    monkeypatch.setattr(
        runner, "enforce_container_drift_for_modes", lambda _m: ("ok", "")
    )
    monkeypatch.setattr(
        runner, "_require_openrouter_env_for_container", lambda _p: None
    )
    monkeypatch.setattr(runner, "_cleanup_stale_container", lambda _name: None)

    def _unexpected_proxy_start(_project):
        raise AssertionError("egress proxy launch should be gated first")

    monkeypatch.setattr(
        runner, "_ensure_egress_proxy_running", _unexpected_proxy_start
    )

    with pytest.raises(ValueError, match="egress_allow.*review"):
        runner._start_project_container(
            store, project, log_path, None, None, ()
        )


def test_budget_force_does_not_trust_egress_allow_digest(
    tmp_path,
):
    store, project = _registered_project(
        tmp_path,
        "driver: orchestrator\n"
        "egress_allow:\n"
        "  - '^attacker\\.example$'\n",
    )

    with pytest.raises(ValueError, match="egress_allow.*review"):
        runner._ensure_config_trusted_for_egress(
            store, project, force=True
        )
    refreshed = store.get("p")
    assert refreshed is not None
    assert "egress_allow_sha256=" not in (refreshed.notes or "")


def test_dedicated_trust_flag_trusts_exact_reviewed_egress_allow_digest(
    tmp_path,
):
    store, project = _registered_project(
        tmp_path,
        "driver: orchestrator\n"
        "egress_allow:\n"
        "  - '^rfc-editor\\.org$'\n",
    )

    trusted = runner._ensure_config_trusted_for_egress(
        store, project, trust_egress_allow=True
    )
    assert "egress_allow_sha256=" in trusted.notes

    # Same allow-list: accepted without the trust flag after the host-side
    # trust record.
    same = runner._ensure_config_trusted_for_egress(
        store, trusted, trust_egress_allow=False
    )
    assert same.notes == trusted.notes

    config = Path(project.path) / ".peers" / "config.yaml"
    config.write_text(
        "driver: orchestrator\n"
        "egress_allow:\n"
        "  - '^attacker\\.example$'\n"
    )

    with pytest.raises(ValueError, match="egress_allow.*changed"):
        runner._ensure_config_trusted_for_egress(
            store, store.get("p"), trust_egress_allow=False
        )


def test_cli_force_and_trust_egress_allow_are_separate_flags():
    parser = build_parser()

    force_start = parser.parse_args(["start", "p", "--container", "--force"])
    trust_start = parser.parse_args(
        ["start", "p", "--container", "--trust-egress-allow"]
    )
    force_resume = parser.parse_args(
        ["resume", "p", "--start", "--container", "--force"]
    )
    trust_resume = parser.parse_args(
        ["resume", "p", "--start", "--container", "--trust-egress-allow"]
    )

    assert force_start.force is True
    assert force_start.trust_egress_allow is False
    assert trust_start.force is False
    assert trust_start.trust_egress_allow is True
    assert force_resume.force is True
    assert force_resume.trust_egress_allow is False
    assert trust_resume.force is False
    assert trust_resume.trust_egress_allow is True
