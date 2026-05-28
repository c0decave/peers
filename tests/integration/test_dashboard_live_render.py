"""Integration smoke test for the live dashboard renderer."""
from __future__ import annotations

import io
import json

from peers_ctl.dashboard_live import run
from peers_ctl.store import Project, Store


def test_dashboard_live_renders_registered_project(tmp_path):
    config_dir = tmp_path / "ctl"
    project_path = tmp_path / "alpha"
    log_dir = project_path / ".peers" / "log"
    log_dir.mkdir(parents=True)
    (log_dir / "runs.jsonl").write_text(
        json.dumps({
            "ts": "2026-05-27T12:00:00Z",
            "iteration": 1,
            "peer": "claude",
        }) + "\n",
        encoding="utf-8",
    )
    Store(config_dir).add(Project(name="alpha", path=str(project_path)))
    out = io.StringIO()

    rc = run(config_dir, refresh_s=0.01, iterations=1, output=out)

    assert rc == 0
    rendered = out.getvalue()
    assert "peers-ctl dashboard --live" in rendered
    assert "alpha" in rendered
    assert "ALERT" in rendered
    assert "EVENT" in rendered
