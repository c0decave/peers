import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
FAKE = ROOT / "tests" / "fixtures" / "fake_peer.py"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_target(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    _git(target, "init", "-q", "-b", "main")
    _git(target, "config", "user.email", "x@y")
    _git(target, "config", "user.name", "x")
    (target / "README").write_text("z\n")
    _git(target, "add", "README")
    _git(target, "commit", "-q", "-m", "init")
    return target


def _run_peers(cwd: Path, *args: str, env: dict | None = None
               ) -> subprocess.CompletedProcess:
    full_env = os.environ.copy()
    full_env["PYTHONPATH"] = str(ROOT / "src")
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "peers", "-C", str(cwd), *args],
        capture_output=True, text=True, env=full_env,
    )


def test_init_then_run_drives_loop_to_a_few_ticks(tmp_path: Path):
    target = _init_target(tmp_path)
    r = _run_peers(target, "init")
    assert r.returncode == 0, r.stderr

    cfg = target / ".peers" / "config.yaml"
    cfg.write_text(
        "driver: orchestrator\n"
        "comm: git\n"
        "peers:\n"
        f"  - {{name: claude, tool: claude, argv: ['{sys.executable}', '{FAKE}']}}\n"
        f"  - {{name: codex,  tool: codex,  argv: ['{sys.executable}', '{FAKE}']}}\n"
        "budget: {max_iterations: 4, max_runtime_s: 60,"
        " max_consecutive_failures: 5}\n"
        "health: {idle_timeout_s: 30, absolute_max_runtime_s: 60}\n"
    )

    r = _run_peers(target, "run", "--max-ticks", "4")
    assert r.returncode == 0, f"stderr: {r.stderr}\nstdout: {r.stdout}"

    state = json.loads((target / ".peers" / "state.json").read_text())
    assert state["iteration"] >= 1
    # Schema v2: peer_order + turn_index instead of whose_turn.
    assert state["peer_order"][state["turn_index"]] in ("claude", "codex")
    assert "self-review-on-handoff" in state["goals_status"]
