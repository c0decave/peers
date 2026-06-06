import os
import subprocess as _sp
from pathlib import Path

from peers.cli import cmd_init, cmd_run


def test_init_creates_peers_dir(tmp_path: Path):
    rc = cmd_init(target=tmp_path, force=False)
    assert rc == 0
    assert (tmp_path / ".peers" / "config.yaml").exists()
    assert (tmp_path / ".peers" / "goals.yaml").exists()
    assert (tmp_path / ".peers" / "checks" / "verify_self_review.py").exists()
    run_log = tmp_path / ".peers" / "log" / "runs.jsonl"
    assert run_log.is_file()
    assert run_log.read_text() == ""


def test_init_does_not_scaffold_container_files(tmp_path: Path):
    """The container is built from the peers repo, not scaffolded
    into the target (the target has no peers source to COPY)."""
    cmd_init(target=tmp_path, force=False)
    assert not (tmp_path / "Containerfile").exists()
    assert not (tmp_path / "compose.yaml").exists()


def test_init_refuses_overwrite(tmp_path: Path):
    cmd_init(target=tmp_path, force=False)
    rc = cmd_init(target=tmp_path, force=False)
    assert rc != 0


def test_init_force_overwrites(tmp_path: Path):
    cmd_init(target=tmp_path, force=False)
    (tmp_path / ".peers" / "config.yaml").write_text("garbage")
    rc = cmd_init(target=tmp_path, force=True)
    assert rc == 0
    assert "driver" in (tmp_path / ".peers" / "config.yaml").read_text()


def test_init_makes_check_script_executable(tmp_path: Path):
    cmd_init(target=tmp_path, force=False)
    p = tmp_path / ".peers" / "checks" / "verify_self_review.py"
    assert os.access(p, os.X_OK)


def test_init_rejects_root_target():
    rc = cmd_init(target=Path("/"), force=True)
    assert rc != 0


def test_init_rejects_home_target():
    rc = cmd_init(target=Path.home(), force=True)
    assert rc != 0


def test_init_rejects_nonexistent_target(tmp_path: Path):
    rc = cmd_init(target=tmp_path / "no" / "such" / "dir", force=False)
    assert rc != 0


def test_init_refuses_to_follow_symlinked_peers_dir(tmp_path: Path):
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "important_file").write_text("KEEP ME")
    target = tmp_path / "target"
    target.mkdir()
    (target / ".peers").symlink_to(decoy)
    rc = cmd_init(target=target, force=True)
    assert rc != 0
    assert (decoy / "important_file").exists(), "symlink target was deleted"


def test_init_refuses_to_modify_symlinked_gitignore(tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir()
    bait = tmp_path / "bait.gitignore"
    bait.write_text("keep me\n")
    (target / ".gitignore").symlink_to(bait)

    rc = cmd_init(target=target, force=False)

    assert rc == 2
    assert bait.read_text() == "keep me\n"
    assert not (target / ".peers").exists()


def test_init_force_refuses_plain_file_peers_path(tmp_path: Path, capsys):
    (tmp_path / ".peers").write_text("not a dir")

    rc = cmd_init(target=tmp_path, force=True)

    assert rc == 2
    assert "not a directory" in capsys.readouterr().err
    assert (tmp_path / ".peers").read_text() == "not a dir"


def test_default_goals_includes_placeholder_replace_me(tmp_path: Path):
    cmd_init(target=tmp_path, force=False)
    text = (tmp_path / ".peers" / "goals.yaml").read_text()
    assert "placeholder-replace-me" in text


def test_init_writes_goals_sha256_snapshot(tmp_path: Path):
    """G7: hash snapshot is recorded so mid-run mutation can be caught."""
    cmd_init(target=tmp_path, force=False)
    snap = tmp_path / ".peers" / "goals.sha256"
    assert snap.exists()
    content = snap.read_text().strip()
    assert len(content) == 64  # sha256 hex


def test_init_creates_peers_baseline_tag_in_git_repo(tmp_path: Path):
    """G10: in a git repo, init tags HEAD as peers-baseline for rollback."""
    _sp.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    _sp.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    _sp.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "README").write_text("x")
    _sp.run(["git", "add", "README"], cwd=tmp_path, check=True)
    _sp.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    cmd_init(target=tmp_path, force=False)
    r = _sp.run(["git", "tag", "--list", "peers-baseline"],
                cwd=tmp_path, capture_output=True, text=True)
    assert "peers-baseline" in r.stdout


# --- Fix 21: CLI config validation ---------------------------------------

ROOT_FOR_CLI = Path(__file__).parent.parent.parent


def _init_target_repo(path: Path) -> Path:
    path.mkdir()
    _sp.run(["git", "init", "-q", "-b", "main"], cwd=path,
            check=True, capture_output=True)
    _sp.run(["git", "config", "user.email", "t@t"], cwd=path,
            check=True, capture_output=True)
    _sp.run(["git", "config", "user.name", "t"], cwd=path,
            check=True, capture_output=True)
    (path / "README").write_text("z\n")
    _sp.run(["git", "add", "README"], cwd=path,
            check=True, capture_output=True)
    _sp.run(["git", "commit", "-q", "-m", "init"], cwd=path,
            check=True, capture_output=True)
    return path


def _write_config(target: Path, body: str) -> None:
    cmd_init(target=target, force=False)
    (target / ".peers" / "config.yaml").write_text(body)


def test_cmd_run_rejects_missing_argv(tmp_path: Path, capsys):
    target = _init_target_repo(tmp_path / "t")
    _write_config(target, """
driver: orchestrator
comm: git
tools:
  claude: {}
  codex: {argv: ["codex"]}
budget: {max_iterations: 1, max_runtime_s: 60, max_consecutive_failures: 1}
health: {idle_timeout_s: 5, absolute_max_runtime_s: 10}
""")
    rc = cmd_run(target=target, max_ticks=1)
    assert rc != 0
    err = capsys.readouterr().err
    assert "argv" in err.lower()


def test_cmd_run_rejects_non_list_argv(tmp_path: Path, capsys):
    target = _init_target_repo(tmp_path / "t")
    _write_config(target, """
driver: orchestrator
comm: git
tools:
  claude: {argv: "claude -p"}
  codex: {argv: ["codex"]}
budget: {max_iterations: 1, max_runtime_s: 60, max_consecutive_failures: 1}
health: {idle_timeout_s: 5, absolute_max_runtime_s: 10}
""")
    rc = cmd_run(target=target, max_ticks=1)
    assert rc != 0
    assert "list" in capsys.readouterr().err.lower()


def test_cmd_run_rejects_missing_tools_block(tmp_path: Path, capsys):
    target = _init_target_repo(tmp_path / "t")
    _write_config(target, """
driver: orchestrator
budget: {max_iterations: 1, max_runtime_s: 60, max_consecutive_failures: 1}
health: {idle_timeout_s: 5, absolute_max_runtime_s: 10}
""")
    rc = cmd_run(target=target, max_ticks=1)
    assert rc != 0
    assert "tools" in capsys.readouterr().err.lower()


def test_cmd_run_rejects_missing_openrouter_key(
    tmp_path: Path,
    capsys,
    monkeypatch,
):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    target = _init_target_repo(tmp_path / "t")
    _write_config(target, """
driver: orchestrator
comm: git
peers:
  - name: claude
    tool: claude
    argv: ["claude", "-p", "{PROMPT}"]
    prompt_mode: argv-substitute
    provider: openrouter
  - name: codex
    tool: codex
    argv: ["codex", "exec", "{PROMPT}"]
    prompt_mode: argv-substitute
budget: {max_iterations: 1, max_runtime_s: 60, max_consecutive_failures: 1}
health: {idle_timeout_s: 5, absolute_max_runtime_s: 10}
""")

    rc = cmd_run(target=target, max_ticks=1)

    assert rc == 1
    err = capsys.readouterr().err
    assert "runtime error" in err
    assert "OPENROUTER_API_KEY" in err


def test_cmd_run_max_usd_overrides_config_budget(tmp_path: Path, monkeypatch):
    import peers.cli as cli_mod

    target = _init_target_repo(tmp_path / "t")
    _write_config(target, """
driver: orchestrator
comm: git
peers:
  - name: claude
    tool: claude
    argv: ["true"]
    prompt_mode: argv-substitute
  - name: codex
    tool: codex
    argv: ["true"]
    prompt_mode: argv-substitute
budget: {max_iterations: 1, max_runtime_s: 60, max_consecutive_failures: 1,
         max_usd: 99}
health: {idle_timeout_s: 5, absolute_max_runtime_s: 10}
""")
    captured: dict = {}

    class FakeDriver:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self, max_ticks=None):
            captured["max_ticks"] = max_ticks
            return {"reason": "max_ticks", "state": {}}

    monkeypatch.setattr(cli_mod, "OrchestratorDriver", FakeDriver)

    rc = cmd_run(target=target, max_ticks=3, max_usd=1.25)

    assert rc == 0
    assert captured["cfg_budget"]["max_usd"] == 1.25
    assert captured["max_ticks"] == 3


def test_main_run_accepts_max_usd(tmp_path: Path, monkeypatch):
    import peers.cli as cli_mod

    calls: list[tuple[Path, int | None, bool, float | None]] = []

    def fake_cmd_run(target, max_ticks, dry_run=False, max_usd=None,
                     verbose=False, without_recon=False, no_codemap=False,
                     without_post_convergence_skeptic=False):
        calls.append((target, max_ticks, dry_run, max_usd))
        return 0

    monkeypatch.setattr(cli_mod, "cmd_run", fake_cmd_run)

    rc = cli_mod.main([
        "-C", str(tmp_path), "run", "--max-ticks", "2", "--max-usd", "1.5",
    ])

    assert rc == 0
    assert calls == [(tmp_path, 2, False, 1.5)]


def test_cmd_run_rejects_non_positive_max_ticks(tmp_path: Path, capsys):
    target = _init_target_repo(tmp_path / "t")
    _write_config(target, """
driver: orchestrator
comm: git
peers:
  - name: claude
    tool: claude
    argv: ["true"]
    prompt_mode: argv-substitute
  - name: codex
    tool: codex
    argv: ["true"]
    prompt_mode: argv-substitute
health: {idle_timeout_s: 5, absolute_max_runtime_s: 10}
""")

    rc = cmd_run(target=target, max_ticks=0)

    assert rc == 1
    assert "--max-ticks" in capsys.readouterr().err


def test_cmd_run_rejects_nan_max_usd(tmp_path: Path, capsys):
    target = _init_target_repo(tmp_path / "t")
    _write_config(target, """
driver: orchestrator
comm: git
peers:
  - name: claude
    tool: claude
    argv: ["true"]
    prompt_mode: argv-substitute
  - name: codex
    tool: codex
    argv: ["true"]
    prompt_mode: argv-substitute
health: {idle_timeout_s: 5, absolute_max_runtime_s: 10}
""")

    rc = cmd_run(target=target, max_ticks=1, max_usd=float("nan"))

    assert rc == 1
    assert "--max-usd" in capsys.readouterr().err
