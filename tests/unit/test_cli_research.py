"""The `peers research` operator verb — makes research operator-reachable
(SPEC-02/SPEC-05). Mirrors `peers bring-up`: the research brief is the
operator-authored TOPIC.md (Scope+Questions); a missing/invalid brief fails
CLOSED.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from peers.cli import (
    _build_research_frontend_from_config,
    build_parser,
    cmd_research,
)
from peers.research.adapters import LLMDecomposer
from peers.research.assembly import make_research_frontend
from peers.research.frontend import ResearchFrontend

_VALID_CONFIG = """\
driver: orchestrator
comm: git
peers:
  - name: claude
    tool: claude
    argv: ["claude", "-p", "{PROMPT}"]
    prompt_mode: argv-substitute
budget: {max_iterations: 1, max_runtime_s: 60, max_consecutive_failures: 1}
"""

TOPIC = (
    "# T\n\n## Scope\n"
    "Investigate how the authentication subsystem verifies tokens and where "
    "signature checking happens across the codebase modules.\n\n"
    "## Questions\n"
    "- How does the authenticate function verify a token end to end?\n"
    "- Which module implements verify_signature and what does it return?\n"
)


def _git(repo: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True,
                          text=True, check=True).stdout.strip()


def _repo(tmp_path: Path, *, topic: bool = True) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "code.py").write_text("def authenticate():\n    return True\n", encoding="utf-8")
    if topic:
        (repo / "TOPIC.md").write_text(TOPIC, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def test_happy_research_drives_real_frontend_and_writes_ledger(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    def fake_make(r: Path):
        # real ResearchFrontend, empty decompose -> clean dry termination
        return make_research_frontend(
            r, run_agent=lambda p: "[]", modalities=["codebase"],
            run_tests=lambda c: (0, "ok"))

    rc = cmd_research(repo, modalities=["codebase"], _make_frontend=fake_make)
    assert rc == 0
    assert (repo / ".peers" / "run.jsonl").exists()


def test_sad_missing_topic_fails_closed(tmp_path: Path) -> None:
    repo = _repo(tmp_path, topic=False)
    rc = cmd_research(repo, modalities=["codebase"])
    assert rc == 1


def test_sad_missing_repo_fails_closed(tmp_path: Path) -> None:
    rc = cmd_research(tmp_path / "nope", modalities=["codebase"])
    assert rc == 1


def test_live_config_path_builds_real_wired_frontend(tmp_path: Path) -> None:
    # The production (non-injected) build: load a valid .peers/config.yaml, select
    # the first peer, wire run_agent via agent_runner_from_spec, assemble a real
    # ResearchFrontend -- WITHOUT a live model. Closes the LOW live-config test gap;
    # the existing tests only inject _make_frontend or hit the missing-config path.
    repo = _repo(tmp_path)
    (repo / ".peers").mkdir()
    (repo / ".peers" / "config.yaml").write_text(_VALID_CONFIG, encoding="utf-8")

    fe = _build_research_frontend_from_config(repo, modalities=["codebase"], peer=None)

    assert isinstance(fe, ResearchFrontend)
    assert isinstance(fe.decomposer, LLMDecomposer)   # real adapter, not a stub


_WEB_CONFIG = _VALID_CONFIG + """\
research:
  web:
    enabled: true
    allow: ["example\\\\.com"]
    seed_urls: ["https://example.com/doc", "https://example.com/spec"]
"""


def test_web_modality_off_by_default_no_fetcher_wired(tmp_path: Path) -> None:
    # deny-by-default: without a research.web config block the web modality has NO
    # fetcher/searcher -> the sweeper skips web honestly (codebase-only stays dry).
    repo = _repo(tmp_path)
    (repo / ".peers").mkdir()
    (repo / ".peers" / "config.yaml").write_text(_VALID_CONFIG, encoding="utf-8")
    fe = _build_research_frontend_from_config(repo, modalities=["codebase", "web"], peer=None)
    assert fe.sweeper.web_search is None and fe.sweeper.fetch is None


def test_web_modality_wires_fetcher_when_configured_and_requested(tmp_path: Path) -> None:
    # opt-in: research.web.enabled + allow + seed_urls + the 'web' modality ->
    # the sweeper gets a real web_search (seed URLs) + an allowlisted fetcher.
    repo = _repo(tmp_path)
    (repo / ".peers").mkdir()
    (repo / ".peers" / "config.yaml").write_text(_WEB_CONFIG, encoding="utf-8")
    fe = _build_research_frontend_from_config(repo, modalities=["codebase", "web"], peer=None)
    assert fe.sweeper.web_search is not None and fe.sweeper.fetch is not None
    assert fe.sweeper.web_search("anything") == [
        "https://example.com/doc", "https://example.com/spec"]


def test_web_config_present_but_modality_not_requested_stays_off(tmp_path: Path) -> None:
    # even with research.web configured, a codebase-only run wires NO fetcher
    # (the modality the operator asked for governs).
    repo = _repo(tmp_path)
    (repo / ".peers").mkdir()
    (repo / ".peers" / "config.yaml").write_text(_WEB_CONFIG, encoding="utf-8")
    fe = _build_research_frontend_from_config(repo, modalities=["codebase"], peer=None)
    assert fe.sweeper.web_search is None and fe.sweeper.fetch is None


def test_non_dict_research_config_fails_closed(tmp_path: Path) -> None:
    # S4 review (LOW): a truthy non-dict `research:` value must fail CLOSED with a
    # clean ValueError (cmd_research maps it to rc 1), not an uncaught AttributeError.
    import pytest
    repo = _repo(tmp_path)
    (repo / ".peers").mkdir()
    (repo / ".peers" / "config.yaml").write_text(
        _VALID_CONFIG + "research: oops-a-string\n", encoding="utf-8")
    with pytest.raises(ValueError):
        _build_research_frontend_from_config(repo, modalities=["web"], peer=None)


def test_web_config_disabled_stays_off(tmp_path: Path) -> None:
    # research.web present but enabled:false -> deny (no fetcher) even with --modalities web.
    repo = _repo(tmp_path)
    (repo / ".peers").mkdir()
    cfg = _WEB_CONFIG.replace("enabled: true", "enabled: false")
    (repo / ".peers" / "config.yaml").write_text(cfg, encoding="utf-8")
    fe = _build_research_frontend_from_config(repo, modalities=["web"], peer=None)
    assert fe.sweeper.web_search is None and fe.sweeper.fetch is None


def test_live_config_path_unknown_peer_fails_closed(tmp_path: Path) -> None:
    import pytest
    repo = _repo(tmp_path)
    (repo / ".peers").mkdir()
    (repo / ".peers" / "config.yaml").write_text(_VALID_CONFIG, encoding="utf-8")
    with pytest.raises(ValueError):
        _build_research_frontend_from_config(repo, modalities=["codebase"], peer="ghost")


def test_edge_research_subcommand_registered() -> None:
    ns = build_parser().parse_args(["research", "/some/repo", "--modalities", "codebase,web"])
    assert ns.cmd == "research"
    assert ns.repo == "/some/repo"
    assert ns.modalities == "codebase,web"
