"""Tests for `peers-ctl new` (one-shot scaffold) and the
`peers-ctl start --container` path."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent


# --- peers-ctl new ----------------------------------------------------

def test_new_scaffolds_fresh_directory(tmp_path: Path):
    from peers_ctl.cli import cmd_new
    from peers_ctl.store import Store
    cfg = tmp_path / "ctl"
    target = tmp_path / "newproj"
    rc = cmd_new(target, name="newproj", config_dir=cfg)
    assert rc == 0
    # git init happened
    assert (target / ".git").is_dir()
    assert (target / "README.md").read_text().startswith("# newproj")
    # peers init happened
    assert (target / ".peers" / "config.yaml").exists()
    assert (target / ".peers" / "goals.yaml").exists()
    assert (target / ".peers" / "log" / "runs.jsonl").is_file()
    # registered
    store = Store(cfg)
    assert store.get("newproj") is not None
    assert store.log_path_for("newproj").is_file()


def test_new_with_spec_writes_SPEC_md(tmp_path: Path):
    from peers_ctl.cli import cmd_new
    target = tmp_path / "withspec"
    rc = cmd_new(target, name="withspec",
                 spec="# Spec\n\nDo X, then Y.", config_dir=tmp_path / "ctl")
    assert rc == 0
    assert (target / "SPEC.md").read_text().startswith("# Spec")


def test_new_peer_flags_patch_generated_config(tmp_path: Path):
    import yaml
    from peers_ctl.cli import cmd_new

    target = tmp_path / "with-peer-flags"
    rc = cmd_new(
        target,
        name="with-peer-flags",
        config_dir=tmp_path / "ctl",
        peer_model=["claude=opus", "codex=~openai/gpt-latest"],
        peer_reasoning=["codex=xhigh"],
        peer_provider=["codex=openrouter"],
    )

    assert rc == 0
    cfg = yaml.safe_load((target / ".peers" / "config.yaml").read_text())
    peers = {peer["name"]: peer for peer in cfg["peers"]}
    assert peers["claude"]["model"] == "opus"
    assert peers["codex"]["model"] == "~openai/gpt-latest"
    assert peers["codex"]["reasoning"] == "xhigh"
    assert peers["codex"]["provider"] == "openrouter"


def test_new_invalid_peer_flags_leave_no_target(tmp_path: Path):
    from peers_ctl.cli import cmd_new

    target = tmp_path / "bad-peer-flags"
    rc = cmd_new(
        target,
        name="bad-peer-flags",
        config_dir=tmp_path / "ctl",
        peer_provider=["claude=openai"],
    )

    assert rc == 2
    assert not target.exists()


def test_new_with_spec_file_path(tmp_path: Path):
    from peers_ctl.cli import cmd_new
    spec_file = tmp_path / "spec.md"
    spec_file.write_text("# From file\n")
    target = tmp_path / "fromfile"
    rc = cmd_new(target, name="fromfile",
                 spec=str(spec_file), config_dir=tmp_path / "ctl")
    assert rc == 0
    assert (target / "SPEC.md").read_text() == "# From file\n"


def test_new_rejects_missing_path_like_spec(tmp_path: Path, capsys):
    from peers_ctl.cli import cmd_new
    target = tmp_path / "missing-spec"

    rc = cmd_new(
        target, name="missing-spec",
        spec="./typo.md", config_dir=tmp_path / "ctl",
    )

    assert rc == 2
    assert "--spec path does not exist" in capsys.readouterr().err
    assert not target.exists()


def test_new_refuses_nonempty_dir_without_force(tmp_path: Path):
    from peers_ctl.cli import cmd_new
    target = tmp_path / "existing"
    target.mkdir()
    (target / "something.txt").write_text("x")
    rc = cmd_new(target, name="existing", config_dir=tmp_path / "ctl")
    assert rc == 2  # refused


def test_new_creates_initial_commit(tmp_path: Path):
    from peers_ctl.cli import cmd_new
    target = tmp_path / "initcommit"
    rc = cmd_new(target, name="initcommit", config_dir=tmp_path / "ctl")
    assert rc == 0
    # peers init's .gitignore commit + initial scaffold commit
    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=target, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert len(log) >= 1, "expected at least one commit"


def test_new_idempotent_with_force(tmp_path: Path):
    from peers_ctl.cli import cmd_new
    from peers_ctl.store import Store
    cfg = tmp_path / "ctl"
    target = tmp_path / "again"
    cmd_new(target, name="again", config_dir=cfg)
    # Second invocation refuses without --force.
    rc = cmd_new(target, name="again", config_dir=cfg)
    assert rc != 0
    # With --force, succeeds.
    rc = cmd_new(target, name="again", force=True, config_dir=cfg)
    assert rc == 0
    assert Store(cfg).get("again") is not None


def test_new_existing_git_repo_gets_readme_and_logs(tmp_path: Path):
    from peers_ctl.cli import cmd_new
    from peers_ctl.store import Store

    cfg = tmp_path / "ctl"
    target = tmp_path / "existing-git"
    target.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"],
                   cwd=target, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"],
                   cwd=target, check=True)
    subprocess.run(["git", "config", "user.name", "t"],
                   cwd=target, check=True)
    (target / "app.txt").write_text("hello\n")
    subprocess.run(["git", "add", "app.txt"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"],
                   cwd=target, check=True)

    rc = cmd_new(target, name="existinggit", force=True, config_dir=cfg)

    assert rc == 0
    assert (target / "README.md").read_text().startswith("# existinggit")
    assert (target / ".peers" / "log" / "runs.jsonl").is_file()
    assert Store(cfg).log_path_for("existinggit").is_file()


# --- --container path -------------------------------------------------

def test_build_container_argv_shape(tmp_path: Path):
    """The podman argv must mount target, ~/.claude, ~/.codex, run
    peers:dev with `run` and the requested max-ticks."""
    from peers_ctl.runner import _build_container_argv
    from peers_ctl.store import Project
    p = Project(name="x", path=str(tmp_path / "tgt"))
    (tmp_path / "tgt").mkdir()
    argv = _build_container_argv(p, max_ticks=7, extra_args=("--dry-run",))
    assert argv[0] == "podman"
    assert "--rm" in argv
    # In full isolation the main container shares the egress-proxy's
    # user namespace (which IS keep-id) rather than minting its own,
    # so it owns the shared netns and can mount sysfs (BUG: see
    # test_build_container_argv_shares_proxy_userns_for_sysfs).
    assert "--userns=container:peers-egress-proxy_x" in argv
    assert "--userns=keep-id" not in argv
    # Image is "peers:dev", followed by the entrypoint args
    # (entrypoint is `peers`, so we just pass `run [...]`).
    assert "peers:dev" in argv
    img_idx = argv.index("peers:dev")
    # After the image: the subcommand "run", then "--max-ticks 7",
    # then any extra args.
    after = argv[img_idx + 1:]
    assert after[0] == "run"
    assert "--max-ticks" in after
    idx = after.index("--max-ticks")
    assert after[idx + 1] == "7"
    # extra_args appended
    assert "--dry-run" in after
    # target volume mount
    assert any(f"{tmp_path / 'tgt'}:/work" in a for a in argv)


def test_build_container_argv_carries_pids_limit(tmp_path: Path):
    """podman's default pids cgroup
    is 2048, which gets exhausted by claude/codex orphan grandchildren
    after ~3-10 ticks. The in-container reaper handles the steady-state,
    but the container also gets a raised pids cap as belt-and-suspenders
    for long thorough-stack runs."""
    from peers_ctl.runner import _build_container_argv
    from peers_ctl.store import Project
    p = Project(name="x", path=str(tmp_path / "tgt"))
    (tmp_path / "tgt").mkdir()
    argv = _build_container_argv(p, max_ticks=1, extra_args=())
    # pids-limit must be present and well above podman default (2048)
    pids_flags = [a for a in argv if a.startswith("--pids-limit")]
    assert pids_flags, f"--pids-limit missing from podman argv: {argv}"
    # Single flag with value attached via `=`, easy to parse
    val = int(pids_flags[0].split("=", 1)[1])
    assert val >= 4096, (
        f"--pids-limit={val} too low; podman default is 2048, raises "
        f"this so multi-hour runs don't exhaust the cgroup."
    )


def test_build_container_argv_uses_read_only_rootfs(tmp_path: Path):
    """Phase-2 hardening B1 (post-v9 audit synthesis): the container
    rootfs must be `--read-only` so a prompt-injection at the LLM
    layer cannot persist malicious binaries / scripts on the image
    overlay. Combined with `cap-drop=ALL + no-new-privileges` this
    closes the persistence-attack class. Writable paths must be
    explicit `--tmpfs` mounts (see next test)."""
    from peers_ctl.runner import _build_container_argv
    from peers_ctl.store import Project
    p = Project(name="x", path=str(tmp_path / "tgt"))
    (tmp_path / "tgt").mkdir()
    argv = _build_container_argv(p, max_ticks=1, extra_args=())
    assert "--read-only" in argv, (
        f"--read-only missing from podman argv: {argv}"
    )


def test_build_container_argv_provides_tmpfs_for_writable_paths(
    tmp_path: Path,
) -> None:
    """Phase-2 hardening B1: with `--read-only` rootfs the container
    needs explicit `--tmpfs` mounts for paths that real workloads
    write to. Without these, claude/codex (npm, pip, /tmp scratch)
    fail with EROFS mid-tick. Cover at minimum:
      - /tmp                  (scratch for shell tools, codex sessions)
      - ~/.cache     (pip cache, generic xdg cache)
      - ~/.npm       (npm install cache)
    """
    from peers_ctl.runner import _build_container_argv
    from peers_ctl.store import Project
    p = Project(name="x", path=str(tmp_path / "tgt"))
    (tmp_path / "tgt").mkdir()
    argv = _build_container_argv(p, max_ticks=1, extra_args=())
    # Collect all `--tmpfs <spec>` pairs.
    tmpfs_specs: list[str] = []
    for i, a in enumerate(argv):
        if a == "--tmpfs" and i + 1 < len(argv):
            tmpfs_specs.append(argv[i + 1])
    targets = {spec.split(":", 1)[0] for spec in tmpfs_specs}
    for required in ("/tmp", "~/.cache", "~/.npm"):
        assert required in targets, (
            f"missing --tmpfs {required} in argv (tmpfs_specs="
            f"{tmpfs_specs!r}, argv={argv!r})"
        )
    # Defense-in-depth: tmpfs specs must carry nosuid+nodev to prevent
    # mode-escalation if rootfs is later widened.
    for spec in tmpfs_specs:
        assert "nosuid" in spec and "nodev" in spec, (
            f"tmpfs spec {spec!r} missing nosuid/nodev flags"
        )


# --- Phase-2 hardening B2: egress proxy sidecar -----------------------

def test_proxy_container_name_is_derived_from_project_name(
    tmp_path: Path,
) -> None:
    """The sidecar's container must have a deterministic name derived
    from the project name so peers-ctl can stop it cleanly later."""
    from peers_ctl.runner import _proxy_container_name
    from peers_ctl.store import Project
    p = Project(name="foo", path=str(tmp_path / "p"))
    name = _proxy_container_name(p)
    assert name == "peers-egress-proxy_foo", name


def test_build_proxy_argv_shape(tmp_path: Path) -> None:
    """The egress-proxy podman argv must:
      - use the peers-egress-proxy:dev image
      - run detached with --rm
      - keep cap-drop=ALL + no-new-privs + read-only + nosuid tmpfs
        (the proxy is a security component; harden it like the main
        container)
      - not mount any host paths (proxy must be self-contained)
      - publish nothing — it's reached via container-network-sharing
    """
    from peers_ctl.runner import _build_proxy_argv
    from peers_ctl.store import Project
    p = Project(name="x", path=str(tmp_path / "tgt"))
    (tmp_path / "tgt").mkdir()
    argv = _build_proxy_argv(p)
    assert argv[0] == "podman"
    assert "run" in argv and "-d" in argv and "--rm" in argv
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges" in argv
    assert "--read-only" in argv
    assert "peers-egress-proxy:dev" in argv
    # No host path mounts — proxy has no business reading host files.
    bind_mounts = [argv[i + 1] for i, a in enumerate(argv)
                   if a == "-v" and i + 1 < len(argv)]
    assert bind_mounts == [], (
        f"proxy must not bind-mount host paths, got: {bind_mounts}"
    )


def test_build_container_argv_uses_proxy_network_namespace(
    tmp_path: Path,
) -> None:
    """Phase-2 hardening B2: the peers container shares the proxy's
    network namespace so it cannot reach the outside world directly.
    Egress goes through the proxy on 127.0.0.1:3128 (allow-listed).
    """
    from peers_ctl.runner import _build_container_argv
    from peers_ctl.store import Project
    p = Project(name="x", path=str(tmp_path / "tgt"))
    (tmp_path / "tgt").mkdir()
    argv = _build_container_argv(p, max_ticks=1, extra_args=())
    flags = [a for a in argv if a.startswith("--network=")]
    assert any(f == "--network=container:peers-egress-proxy_x"
               for f in flags), (
        f"main container must use --network=container:<proxy_name>, "
        f"got network flags: {flags}"
    )


def test_build_container_argv_sets_proxy_env_vars(tmp_path: Path) -> None:
    """Phase-2 hardening B2: the peers container must have
    HTTPS_PROXY/HTTP_PROXY pointing at the sidecar so claude/codex
    SDKs route LLM traffic through it. NO_PROXY=localhost,127.0.0.1
    so loopback (peer<->proxy) is direct."""
    from peers_ctl.runner import _build_container_argv
    from peers_ctl.store import Project
    p = Project(name="x", path=str(tmp_path / "tgt"))
    (tmp_path / "tgt").mkdir()
    argv = _build_container_argv(p, max_ticks=1, extra_args=())
    env_specs = [argv[i + 1] for i, a in enumerate(argv)
                 if a in ("-e", "--env") and i + 1 < len(argv)]
    proxy_url = "http://127.0.0.1:3128"
    assert f"HTTPS_PROXY={proxy_url}" in env_specs, env_specs
    assert f"HTTP_PROXY={proxy_url}" in env_specs, env_specs
    assert any(s.startswith("NO_PROXY=") and "127.0.0.1" in s
               for s in env_specs), env_specs


def test_auth_proxy_container_name_is_derived_from_project_name(
    tmp_path: Path,
) -> None:
    from peers_ctl.runner import _auth_proxy_container_name
    from peers_ctl.store import Project
    p = Project(name="foo", path=str(tmp_path / "p"))
    assert _auth_proxy_container_name(p) == "peers-auth-proxy_foo"


def test_build_auth_proxy_argv_owns_claude_json_mount(
    tmp_path: Path,
) -> None:
    from peers_ctl.runner import _build_auth_proxy_argv
    from peers_ctl.store import Project
    (tmp_path / ".claude.json").write_text("{}")
    p = Project(name="x", path=str(tmp_path / "tgt"))

    argv = _build_auth_proxy_argv(p, home=tmp_path)

    assert "peers-auth-proxy:dev" in argv
    assert "--name" in argv
    assert "peers-auth-proxy_x" in argv
    assert f"{tmp_path / '.claude.json'}:/auth/.claude.json" in argv
    assert "/auth:rw,nosuid,nodev,size=4m,mode=700" in argv
    assert "--network=container:peers-egress-proxy_x" in argv
    assert "--read-only" in argv


def test_build_container_argv_uses_auth_proxy_without_claude_json_mount(
    tmp_path: Path, monkeypatch,
) -> None:
    import peers_ctl.runner as r
    from peers_ctl.store import Project

    (tmp_path / ".claude.json").write_text("{}")
    (tmp_path / "tgt").mkdir()
    monkeypatch.setattr(r.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(r, "AUTH_PROXY_DISABLED", False)
    p = Project(name="x", path=str(tmp_path / "tgt"))

    argv = r._build_container_argv(p, max_ticks=1, extra_args=())

    env_specs = [argv[i + 1] for i, a in enumerate(argv)
                 if a in ("-e", "--env") and i + 1 < len(argv)]
    assert "ANTHROPIC_BASE_URL=http://127.0.0.1:8080" in env_specs
    assert not any(".claude.json:~/.claude.json" in a for a in argv)


def _write_openrouter_config(target: Path, *, legacy: bool = False) -> None:
    peer_dir = target / ".peers"
    peer_dir.mkdir(parents=True, exist_ok=True)
    if legacy:
        body = """
driver: orchestrator
comm: git
tools:
  claude:
    argv: ["claude", "-p", "{PROMPT}"]
    prompt_mode: argv-substitute
    provider: openrouter
  codex:
    argv: ["codex", "exec", "{PROMPT}"]
    prompt_mode: argv-substitute
budget: {max_iterations: 1, max_runtime_s: 60, max_consecutive_failures: 1}
health: {idle_timeout_s: 5, absolute_max_runtime_s: 10}
"""
    else:
        body = """
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
"""
    (peer_dir / "config.yaml").write_text(body)


def _write_codex_openrouter_custom_env_config(
    target: Path,
    env_key: str = "CUSTOM_OR_KEY",
) -> None:
    peer_dir = target / ".peers"
    peer_dir.mkdir(parents=True, exist_ok=True)
    (peer_dir / "config.yaml").write_text(f"""
driver: orchestrator
comm: git
peers:
  - name: claude
    tool: claude
    argv: ["claude", "-p", "{{PROMPT}}"]
    prompt_mode: argv-substitute
  - name: codex
    tool: codex
    argv:
      - codex
      - exec
      - -c
      - model_provider = "openrouter"
      - -c
      - model_providers.openrouter.env_key = "{env_key}"
      - "{{PROMPT}}"
    prompt_mode: argv-substitute
    provider: openrouter
budget: {{max_iterations: 1, max_runtime_s: 60, max_consecutive_failures: 1}}
health: {{idle_timeout_s: 5, absolute_max_runtime_s: 10}}
""")


def _write_invalid_peer_config(target: Path) -> None:
    peer_dir = target / ".peers"
    peer_dir.mkdir(parents=True, exist_ok=True)
    (peer_dir / "config.yaml").write_text("""
driver: orchestrator
comm: git
peers:
  - name: claude
    tool: claude
    argv: ["claude", "-p", "{PROMPT}"]
    prompt_mode: argv-substitute
    provider: openai
budget: {max_iterations: 1, max_runtime_s: 60, max_consecutive_failures: 1}
health: {idle_timeout_s: 5, absolute_max_runtime_s: 10}
""")


def _write_minimal_peer_config(target: Path) -> None:
    peer_dir = target / ".peers"
    peer_dir.mkdir(parents=True, exist_ok=True)
    (peer_dir / "config.yaml").write_text("""
driver: orchestrator
comm: git
peers:
  - name: claude
    tool: claude
    argv: ["claude", "-p", "{PROMPT}"]
    prompt_mode: argv-substitute
budget: {max_iterations: 1, max_runtime_s: 60, max_consecutive_failures: 1}
health: {idle_timeout_s: 5, absolute_max_runtime_s: 10}
""")


def _env_specs(argv: list[str]) -> list[str]:
    return [
        argv[i + 1] for i, value in enumerate(argv)
        if value in ("-e", "--env") and i + 1 < len(argv)
    ]


def test_build_proxy_argv_adds_openrouter_runtime_allowlist(tmp_path: Path):
    from peers_ctl.runner import _build_proxy_argv
    from peers_ctl.store import Project

    target = tmp_path / "tgt"
    target.mkdir()
    _write_openrouter_config(target)
    argv = _build_proxy_argv(Project(name="x", path=str(target)))

    assert "PEERS_EGRESS_EXTRA_HOSTS=^openrouter\\.ai$" in _env_specs(argv)


def test_build_proxy_argv_detects_openrouter_in_legacy_tools_shape(
    tmp_path: Path,
):
    from peers_ctl.runner import _build_proxy_argv
    from peers_ctl.store import Project

    target = tmp_path / "tgt"
    target.mkdir()
    _write_openrouter_config(target, legacy=True)
    argv = _build_proxy_argv(Project(name="x", path=str(target)))

    assert "PEERS_EGRESS_EXTRA_HOSTS=^openrouter\\.ai$" in _env_specs(argv)


def test_build_proxy_argv_rejects_invalid_peer_config(tmp_path: Path):
    from peers_ctl.runner import _build_proxy_argv
    from peers_ctl.store import Project

    target = tmp_path / "tgt"
    target.mkdir()
    _write_invalid_peer_config(target)

    with pytest.raises(ValueError, match="invalid peer config"):
        _build_proxy_argv(Project(name="x", path=str(target)))


def test_proxy_image_runtime_filter_files_are_wired_together():
    root = ROOT / "proxy"
    entrypoint = (root / "entrypoint.sh").read_text()
    tinyproxy = (root / "tinyproxy.conf").read_text()
    containerfile = (root / "Containerfile.proxy").read_text()

    assert "PEERS_EGRESS_EXTRA_HOSTS" in entrypoint
    assert 'Filter "/tmp/tinyproxy-filter"' in tinyproxy
    assert "entrypoint.sh" in containerfile
    assert "peers-egress-entrypoint" in containerfile


def test_build_container_argv_passes_openrouter_key_name(
    tmp_path: Path,
    monkeypatch,
):
    import peers_ctl.runner as r
    from peers_ctl.store import Project

    target = tmp_path / "tgt"
    target.mkdir()
    _write_openrouter_config(target)
    monkeypatch.setattr(r.Path, "home", classmethod(lambda cls: tmp_path))
    argv = r._build_container_argv(
        Project(name="x", path=str(target)), max_ticks=1, extra_args=(),
    )

    env_specs = _env_specs(argv)
    assert "OPENROUTER_API_KEY" in env_specs
    assert not any(s.startswith("OPENROUTER_API_KEY=") for s in env_specs)


def test_build_container_argv_detects_openrouter_in_legacy_tools_shape(
    tmp_path: Path,
    monkeypatch,
):
    import peers_ctl.runner as r
    from peers_ctl.store import Project

    target = tmp_path / "tgt"
    target.mkdir()
    _write_openrouter_config(target, legacy=True)
    monkeypatch.setattr(r.Path, "home", classmethod(lambda cls: tmp_path))
    argv = r._build_container_argv(
        Project(name="x", path=str(target)), max_ticks=1, extra_args=(),
    )

    assert "OPENROUTER_API_KEY" in _env_specs(argv)


def test_build_container_argv_passes_custom_openrouter_env_key(
    tmp_path: Path,
    monkeypatch,
):
    import peers_ctl.runner as r
    from peers_ctl.store import Project

    target = tmp_path / "tgt"
    target.mkdir()
    _write_codex_openrouter_custom_env_config(target)
    monkeypatch.setattr(r.Path, "home", classmethod(lambda cls: tmp_path))

    argv = r._build_container_argv(
        Project(name="x", path=str(target)), max_ticks=1, extra_args=(),
    )

    env_specs = _env_specs(argv)
    assert "CUSTOM_OR_KEY" in env_specs
    assert "OPENROUTER_API_KEY" not in env_specs


def test_start_project_container_requires_openrouter_key(
    tmp_path: Path,
    monkeypatch,
):
    from peers_ctl.runner import _start_project_container
    from peers_ctl.store import Project, Store

    target = tmp_path / "tgt"
    target.mkdir()
    _write_openrouter_config(target)
    store = Store(tmp_path / "ctl")
    project = Project(name="x", path=str(target))
    store.add(project)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        _start_project_container(
            store, project, tmp_path / "log.txt", 1, None, (),
        )


def test_start_project_container_requires_custom_openrouter_key(
    tmp_path: Path,
    monkeypatch,
):
    from peers_ctl.runner import _start_project_container
    from peers_ctl.store import Project, Store

    target = tmp_path / "tgt"
    target.mkdir()
    _write_codex_openrouter_custom_env_config(target)
    store = Store(tmp_path / "ctl")
    project = Project(name="x", path=str(target))
    store.add(project)
    monkeypatch.delenv("CUSTOM_OR_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-present")

    with pytest.raises(ValueError, match="CUSTOM_OR_KEY"):
        _start_project_container(
            store, project, tmp_path / "log.txt", 1, None, (),
        )


def test_start_project_container_rejects_invalid_peer_config_before_podman(
    tmp_path: Path,
    monkeypatch,
):
    import peers_ctl.runner as r
    from peers_ctl.store import Project, Store

    target = tmp_path / "tgt"
    target.mkdir()
    _write_invalid_peer_config(target)
    store = Store(tmp_path / "ctl")
    project = Project(name="x", path=str(target))
    store.add(project)
    monkeypatch.setattr(r, "_container_running", lambda _name: False)
    monkeypatch.setattr(
        r, "enforce_container_drift_for_modes", lambda _modes: ("ok", ""),
    )
    cleanup_called = False

    def fake_cleanup(_name: str) -> None:
        nonlocal cleanup_called
        cleanup_called = True

    monkeypatch.setattr(r, "_cleanup_stale_container", fake_cleanup)

    with pytest.raises(ValueError, match="invalid peer config"):
        r._start_project_container(
            store, project, tmp_path / "log.txt", 1, None, (),
        )

    assert cleanup_called is False


def test_egress_proxy_can_be_disabled_via_env(
    tmp_path: Path, monkeypatch,
) -> None:
    """Escape hatch: PEERS_CTL_NO_EGRESS_PROXY=1 disables the sidecar
    pattern so the operator can debug network issues directly. The
    main container falls back to the legacy --network behaviour
    (PODMAN_NETWORK env or default slirp4netns).

    Implementation note: rather than reload the module under env
    (xdist-unsafe), we patch the module-level constant directly.
    The runtime path queries `EGRESS_PROXY_DISABLED` each call to
    `_build_container_argv`, so a setattr on the live module is
    enough."""
    import peers_ctl.runner as r
    monkeypatch.setattr(r, "EGRESS_PROXY_DISABLED", True)
    from peers_ctl.store import Project
    p = Project(name="x", path=str(tmp_path / "tgt"))
    (tmp_path / "tgt").mkdir()
    argv = r._build_container_argv(p, max_ticks=1, extra_args=())
    net_flags = [a for a in argv if a.startswith("--network=")]
    assert not any("container:peers-egress-proxy_" in f
                   for f in net_flags), (
        f"with NO_EGRESS_PROXY=1, must not attach to proxy ns; "
        f"got {net_flags}"
    )
    env_specs = [argv[i + 1] for i, a in enumerate(argv)
                 if a in ("-e", "--env") and i + 1 < len(argv)]
    assert not any(s.startswith("HTTPS_PROXY=")
                   for s in env_specs), env_specs


def test_build_proxy_argv_shares_keep_id_userns(tmp_path: Path) -> None:
    """Root cause of the full-isolation `runc rc=126: mounting sysfs to
    /sys: operation not permitted`: the kernel only lets a process mount
    a fresh sysfs if its user namespace OWNS the network namespace. The
    egress-proxy is the netns owner for the whole chain (auth + main join
    it via --network=container:<proxy>). It therefore must carry
    --userns=keep-id so that the shared userns it creates is the same
    keep-id mapping the main container needs for /work FS-perm alignment;
    auth + main then join THIS userns and so own the netns they mount
    sysfs into."""
    from peers_ctl.runner import _build_proxy_argv
    from peers_ctl.store import Project
    p = Project(name="x", path=str(tmp_path / "tgt"))
    (tmp_path / "tgt").mkdir()
    argv = _build_proxy_argv(p)
    assert "--userns=keep-id" in argv, argv


def test_build_auth_proxy_argv_shares_proxy_userns(tmp_path: Path) -> None:
    """The auth-proxy joins the egress-proxy's netns; to mount sysfs it
    must also join the egress-proxy's USER namespace. --userns=container:
    and --network=container: must point at the SAME owner."""
    from peers_ctl.runner import _build_auth_proxy_argv
    from peers_ctl.store import Project
    (tmp_path / ".claude.json").write_text("{}")
    p = Project(name="x", path=str(tmp_path / "tgt"))
    argv = _build_auth_proxy_argv(p, home=tmp_path)
    assert "--userns=container:peers-egress-proxy_x" in argv, argv
    assert "--network=container:peers-egress-proxy_x" in argv, argv


def test_build_auth_proxy_argv_owns_userns_when_egress_disabled(
    tmp_path: Path, monkeypatch,
) -> None:
    """With egress disabled but auth enabled, the auth-proxy becomes the
    head of the chain (main joins ITS netns). It must own a keep-id
    userns so the main container can share it and mount sysfs."""
    import peers_ctl.runner as r
    from peers_ctl.store import Project
    monkeypatch.setattr(r, "EGRESS_PROXY_DISABLED", True)
    (tmp_path / ".claude.json").write_text("{}")
    p = Project(name="x", path=str(tmp_path / "tgt"))
    argv = r._build_auth_proxy_argv(p, home=tmp_path)
    assert "--userns=keep-id" in argv, argv
    assert not any(a.startswith("--userns=container:") for a in argv), argv


def test_build_container_argv_shares_proxy_userns_for_sysfs(
    tmp_path: Path,
) -> None:
    """Full isolation: the main container must share the egress-proxy's
    userns+netns (NOT mint its own keep-id userns), otherwise it doesn't
    own the joined netns and `runc create` fails mounting sysfs to /sys
    (rc=126). The userns and network owners must match."""
    from peers_ctl.runner import _build_container_argv
    from peers_ctl.store import Project
    p = Project(name="x", path=str(tmp_path / "tgt"))
    (tmp_path / "tgt").mkdir()
    argv = _build_container_argv(p, max_ticks=1, extra_args=())
    assert "--userns=container:peers-egress-proxy_x" in argv, argv
    assert "--network=container:peers-egress-proxy_x" in argv, argv
    # Must NOT also carry a standalone keep-id (that mints a separate
    # userns and reintroduces the bug).
    assert "--userns=keep-id" not in argv, argv


def test_build_container_argv_shares_auth_userns_when_egress_disabled(
    tmp_path: Path, monkeypatch,
) -> None:
    """Egress disabled + auth enabled: main joins the auth-proxy's netns,
    so it must share the auth-proxy's userns too (same sysfs rule)."""
    import peers_ctl.runner as r
    from peers_ctl.store import Project
    monkeypatch.setattr(r, "EGRESS_PROXY_DISABLED", True)
    monkeypatch.setattr(r.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(r, "AUTH_PROXY_DISABLED", False)
    (tmp_path / ".claude.json").write_text("{}")
    (tmp_path / "tgt").mkdir()
    p = Project(name="x", path=str(tmp_path / "tgt"))
    argv = r._build_container_argv(p, max_ticks=1, extra_args=())
    assert "--userns=container:peers-auth-proxy_x" in argv, argv
    assert "--network=container:peers-auth-proxy_x" in argv, argv
    assert "--userns=keep-id" not in argv, argv


def test_build_container_argv_keeps_keep_id_in_full_bypass(
    tmp_path: Path, monkeypatch,
) -> None:
    """Egress disabled + auth disabled: the main container owns its own
    netns (PODMAN_NETWORK or default slirp), so a self-minted keep-id
    userns DOES own that netns — sysfs mounts fine. Keep keep-id here."""
    import peers_ctl.runner as r
    from peers_ctl.store import Project
    monkeypatch.setattr(r, "EGRESS_PROXY_DISABLED", True)
    monkeypatch.setattr(r, "AUTH_PROXY_DISABLED", True)
    monkeypatch.setattr(r.Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "tgt").mkdir()
    p = Project(name="x", path=str(tmp_path / "tgt"))
    argv = r._build_container_argv(p, max_ticks=1, extra_args=())
    assert "--userns=keep-id" in argv, argv
    assert not any(a.startswith("--userns=container:") for a in argv), argv


def test_parse_truthy_env_recognizes_off_variants() -> None:
    """`_parse_truthy_env` must treat all common falsy spellings as
    'not set' for env-flag parsing (post-review I1)."""
    from peers_ctl.runner import _parse_truthy_env
    for falsy in ("", "0", "false", "False", "FALSE",
                  "no", "No", "off", "Off", "  off  "):
        assert _parse_truthy_env(falsy) is False, falsy
    for truthy in ("1", "true", "yes", "on", "any-other"):
        assert _parse_truthy_env(truthy) is True, truthy


def test_proxy_argv_does_not_inherit_podman_network(
    tmp_path: Path, monkeypatch,
) -> None:
    """Code-review C1: the proxy must NOT inherit
    PEERS_CTL_PODMAN_NETWORK. If the operator set it to `host`
    (because /dev/net/tun is missing), the proxy would otherwise end
    up on the HOST's loopback — readable by every other host user.
    """
    import peers_ctl.runner as r
    monkeypatch.setattr(r, "PODMAN_NETWORK", "host")
    monkeypatch.setattr(r, "EGRESS_PROXY_NETWORK", "")
    from peers_ctl.store import Project
    p = Project(name="x", path=str(tmp_path / "tgt"))
    argv = r._build_proxy_argv(p)
    assert "--network=host" not in argv, (
        f"proxy must not inherit host network when PODMAN_NETWORK=host; "
        f"got argv: {argv}"
    )
    # And when EGRESS_PROXY_NETWORK is set explicitly, that wins
    monkeypatch.setattr(r, "EGRESS_PROXY_NETWORK", "slirp4netns")
    argv2 = r._build_proxy_argv(p)
    assert "--network=slirp4netns" in argv2, argv2


def test_container_name_hashes_long_common_prefixes(tmp_path: Path):
    from peers_ctl.runner import _container_name
    from peers_ctl.store import Project

    p1 = Project(name=("a" * 45) + "x", path=str(tmp_path / "one"))
    p2 = Project(name=("a" * 45) + "y", path=str(tmp_path / "two"))

    assert _container_name(p1) != _container_name(p2)
    assert len(_container_name(p1).removeprefix("peers-ctl_")) <= 40


def test_start_project_container_writes_starttime(tmp_path: Path,
                                                    monkeypatch):
    """Smoke: a container-mode start with a stub `podman` records the
    starttime and notes container=1."""
    from peers_ctl.store import Project, Store

    cfg = tmp_path / "ctl"
    store = Store(cfg)
    target = tmp_path / "tgt"
    target.mkdir()
    _write_minimal_peer_config(target)
    store.add(Project(name="x", path=str(target)))

    # Stub podman: Phase-3i uses `podman run -d` (exits ~immediately
    # with the container ID on stdout) + `podman logs -f <cid>` (long
    # lived). The stub recognises both subcommands.
    stub = tmp_path / "podman_stub.sh"
    stub.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  run) echo deadbeefcafe1234 ;;\n"
        "  logs) sleep 30 ;;\n"
        "  rm) ;;\n"
        "  stop) ;;\n"
        "  ps) ;;\n"
        "  *) ;;\n"
        "esac\n"
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PEERS_CTL_PODMAN_BIN", str(stub))

    import importlib
    import peers_ctl.runner as runner_mod
    importlib.reload(runner_mod)
    monkeypatch.setattr(
        runner_mod, "check_container_version_drift", lambda: ("ok", "")
    )

    pid = runner_mod.start_project(
        store, store.get("x"), max_ticks=1, container=True,
    )
    assert pid > 0
    p = store.get("x")
    assert "container=1" in (p.notes or "")
    assert "container_id=deadbeefcafe" in (p.notes or "")
    assert "container_name=peers-ctl_x" in (p.notes or "")
    # Cleanup: stop the streamer.
    runner_mod.stop_project(store, store.get("x"), grace_s=1)


def test_start_project_container_refuses_running_same_name(
    tmp_path: Path, monkeypatch
):
    from peers_ctl.store import Project, Store

    cfg = tmp_path / "ctl"
    store = Store(cfg)
    target = tmp_path / "tgt"
    target.mkdir()
    _write_minimal_peer_config(target)
    store.add(Project(name="x", path=str(target)))

    stub = tmp_path / "podman_stub.sh"
    stub.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" >> {tmp_path / 'podman.args'}\n"
        "case \"$1\" in\n"
        "  ps) echo peers-ctl_x ;;\n"
        "  rm) exit 99 ;;\n"
        "  run) echo deadbeef ;;\n"
        "esac\n"
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PEERS_CTL_PODMAN_BIN", str(stub))

    import importlib
    import peers_ctl.runner as runner_mod
    importlib.reload(runner_mod)

    with pytest.raises(ValueError, match="running container"):
        runner_mod.start_project(store, store.get("x"), container=True)

    log = (tmp_path / "podman.args").read_text()
    assert "\nrm\n" not in log


def test_container_version_drift_levels(monkeypatch):
    import peers_ctl.runner as runner_mod

    monkeypatch.setattr(runner_mod, "_host_peers_version", lambda: "1.4.0")
    monkeypatch.setattr(runner_mod, "_image_peers_version", lambda: "1.3.0")
    level, msg = runner_mod.check_container_version_drift()
    assert level == "warn"
    assert "1.3.0" in msg and "1.4.0" in msg

    monkeypatch.setattr(runner_mod, "_image_peers_version", lambda: "0.9.0")
    level, msg = runner_mod.check_container_version_drift()
    assert level == "error"
    assert "make build" in msg

    monkeypatch.setattr(runner_mod, "_image_peers_version", lambda: None)
    assert runner_mod.check_container_version_drift()[0] == "skipped"


def test_start_project_container_refuses_major_version_drift(
    tmp_path: Path, monkeypatch
):
    from peers_ctl.store import Project, Store

    cfg = tmp_path / "ctl"
    store = Store(cfg)
    target = tmp_path / "tgt"
    target.mkdir()
    _write_minimal_peer_config(target)
    store.add(Project(name="x", path=str(target)))

    import peers_ctl.runner as runner_mod

    monkeypatch.setattr(runner_mod, "_container_running", lambda _name: False)
    monkeypatch.setattr(runner_mod, "check_container_version_drift",
                        lambda: ("error", "major-version drift"))

    with pytest.raises(RuntimeError, match="major-version drift"):
        runner_mod.start_project(store, store.get("x"), container=True)


def test_start_project_container_warns_on_minor_version_drift(
    tmp_path: Path, monkeypatch, capsys
):
    from peers_ctl.store import Project, Store

    cfg = tmp_path / "ctl"
    store = Store(cfg)
    target = tmp_path / "tgt"
    target.mkdir()
    _write_minimal_peer_config(target)
    store.add(Project(name="x", path=str(target)))

    stub = tmp_path / "podman_stub.sh"
    stub.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  run) echo deadbeefcafe1234 ;;\n"
        "  logs) sleep 30 ;;\n"
        "  ps) ;;\n"
        "  rm) ;;\n"
        "  stop) ;;\n"
        "esac\n"
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PEERS_CTL_PODMAN_BIN", str(stub))

    import importlib
    import peers_ctl.runner as runner_mod
    importlib.reload(runner_mod)
    monkeypatch.setattr(runner_mod, "check_container_version_drift",
                        lambda: ("warn", "container peers=1.3.0"))

    pid = runner_mod.start_project(store, store.get("x"), container=True)
    try:
        captured = capsys.readouterr()
        assert pid > 0
        assert "warning: container peers=1.3.0" in captured.err
        assert store.get("x").state == "running"
    finally:
        runner_mod.stop_project(store, store.get("x"), grace_s=1)


def test_start_project_container_cleans_up_when_registry_update_fails(
    tmp_path: Path, monkeypatch
):
    from peers_ctl.store import Project, Store

    cfg = tmp_path / "ctl"
    store = Store(cfg)
    target = tmp_path / "tgt"
    target.mkdir()
    _write_minimal_peer_config(target)
    store.add(Project(name="x", path=str(target)))

    stub = tmp_path / "podman_stub.sh"
    stub.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  run) echo deadbeefcafe1234 ;;\n"
        "  logs) sleep 30 ;;\n"
        "  ps) ;;\n"
        "  rm) ;;\n"
        "  stop) ;;\n"
        "esac\n"
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PEERS_CTL_PODMAN_BIN", str(stub))

    import importlib
    import peers_ctl.runner as runner_mod
    importlib.reload(runner_mod)
    monkeypatch.setattr(
        runner_mod, "check_container_version_drift", lambda: ("ok", "")
    )

    cleaned: dict[str, list] = {"containers": [], "streamers": []}

    def fail_update(*_args, **_kwargs):
        raise RuntimeError("registry write failed")

    def record_stop(name, grace_s=1.0):
        cleaned["containers"].append(name)

    def record_streamer(proc):
        cleaned["streamers"].append(proc.pid)
        proc.kill()
        proc.wait(timeout=1)

    monkeypatch.setattr(store, "update", fail_update)
    monkeypatch.setattr(runner_mod, "_stop_container_best_effort", record_stop)
    monkeypatch.setattr(runner_mod, "_terminate_spawned_process", record_streamer)

    with pytest.raises(RuntimeError, match="registry write failed"):
        runner_mod.start_project(store, store.get("x"), container=True)

    assert cleaned["containers"] == ["peers-ctl_x"]
    assert cleaned["streamers"]


def test_container_run_in_reports_missing_runtime(
    tmp_path: Path, monkeypatch, capsys
):
    import peers_ctl.cli as cli_mod

    def missing_runtime(*args, **kwargs):
        raise FileNotFoundError("podman")

    monkeypatch.setattr(cli_mod.subprocess, "call", missing_runtime)

    rc = cli_mod._container_run_in(tmp_path, "init")

    assert rc == 127
    assert "container runtime not found" in capsys.readouterr().err


def test_container_run_in_uses_explicit_network(tmp_path: Path, monkeypatch):
    import peers_ctl.cli as cli_mod

    calls: list[list[str]] = []

    def fake_call(argv):
        calls.append(list(argv))
        return 0

    monkeypatch.setattr(cli_mod.subprocess, "call", fake_call)
    monkeypatch.setattr("peers_ctl.runner.PODMAN_NETWORK", "")

    assert cli_mod._container_run_in(tmp_path, "init") == 0
    assert "--network=none" in calls[-1]

    monkeypatch.setattr("peers_ctl.runner.PODMAN_NETWORK", "host")

    assert cli_mod._container_run_in(tmp_path, "init") == 0
    assert "--network=host" in calls[-1]


# --- doctor: container warnings ---------------------------------------

def test_doctor_warns_when_image_missing(tmp_path: Path, capsys,
                                           monkeypatch):
    """If podman is on PATH but the peers:dev image isn't built,
    doctor warns the user before they try --container."""
    import peers_ctl.cli as cli_mod

    # Pretend podman exists and image doesn't.
    def fake_which(name):
        return "/usr/bin/" + name if name in ("peers", "git", "podman") \
            else None
    monkeypatch.setattr(cli_mod._shutil if hasattr(cli_mod, "_shutil")
                        else __import__("shutil"),
                        "which", fake_which)

    import shutil as _shutil_mod
    monkeypatch.setattr(_shutil_mod, "which", fake_which)

    real_run = cli_mod.subprocess.run

    def fake_subprocess_run(args, *a, **kw):
        if args[:3] == ["/usr/bin/podman", "image", "exists"]:
            class R:
                returncode = 1
            return R()
        return real_run(args, *a, **kw)

    monkeypatch.setattr(cli_mod.subprocess, "run", fake_subprocess_run)

    cli_mod.cmd_doctor(tmp_path / "ctl")
    out = capsys.readouterr().out
    # We don't make a hard assertion about the exact text, but
    # "peers:dev" or "not built" should be in the warnings.
    assert "peers:dev" in out or "not built" in out or "image" in out
