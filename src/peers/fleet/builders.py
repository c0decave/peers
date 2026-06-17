"""In-tree fleet frontend builders (the ``PEERS_FLEET_BUILDERS`` hook contract).

Before this module the fleet registry was empty by default, so every
``peers-ctl fleet`` mode fell to ``UnsupportedFleetMode`` unless the operator
wrote an external plugin (FLEET-02 / SPEC-03 in the 2026-06-14 audit). Pointing
``PEERS_FLEET_BUILDERS`` at ``peers.fleet.builders`` (the `peers-ctl fleet` CLI
defaults to this) registers the in-tree builders via :func:`install`.

Per the hook contract this module registers ONLY from :func:`install` — merely
importing it never mutates the global registry, so the registry stays empty by
default for anyone who does not opt in.

A develop fleet spec carries its mode config under a top-level ``develop`` key::

    {"mode": "develop", "tool": "<repo>", "base_sha": ..., "run_id": ...,
     "op_config": {...},
     "develop": {"dimensions": ["correctness", ...],
                 "argv": ["claude", "-p", "--output-format", "stream-json", "{PROMPT}"],
                 "peer": "claude"}}
"""
from __future__ import annotations

from pathlib import Path

from peers.fleet.run_one import UnsupportedFleetMode, register_frontend_builder


def _build_develop(spec: dict):
    """Construct a real DevelopFrontend from a fleet spec's ``develop`` block.

    Fail-closed: a missing/empty config raises :class:`UnsupportedFleetMode`
    (the caught factory-error path) rather than building a frontend that cannot
    run."""
    from peers.agent_invoke import run_agent_once
    from peers.develop.assembly import make_develop_frontend

    cfg = spec.get("develop")
    if not isinstance(cfg, dict):
        raise UnsupportedFleetMode(
            "develop fleet spec needs a 'develop' config block "
            "(dimensions, argv); none supplied")
    dimensions = cfg.get("dimensions")
    argv = cfg.get("argv")
    if not (isinstance(dimensions, list) and dimensions and all(
            isinstance(d, str) for d in dimensions)):
        raise UnsupportedFleetMode("develop config needs a non-empty dimensions list")
    if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
        raise UnsupportedFleetMode("develop config needs a non-empty argv list")
    repo = Path(spec["tool"])
    raw_peer = cfg.get("peer")
    peer: str = raw_peer if isinstance(raw_peer, str) and raw_peer else "develop"
    budget = cfg.get("convergence_budget", 5)
    budget = budget if isinstance(budget, int) and budget >= 1 else 5
    use_stdin = cfg.get("prompt_mode") == "stdin"

    def run_agent(prompt: str) -> str:
        return run_agent_once(prompt, argv=argv, cwd=repo, stdin=use_stdin)

    def impl_run_agent(prompt: str, workdir) -> str:
        return run_agent_once(prompt, argv=argv, cwd=workdir, stdin=use_stdin)

    return make_develop_frontend(
        repo, run_agent=run_agent, impl_run_agent=impl_run_agent,
        dimensions=dimensions, convergence_budget=budget, attest_peer=peer)


def _build_research(spec: dict):
    """Construct a real ResearchFrontend from a fleet spec's ``research`` block.
    Fail-closed (UnsupportedFleetMode) on a missing/empty config."""
    from peers.agent_invoke import run_agent_once
    from peers.research.assembly import make_research_frontend

    cfg = spec.get("research")
    if not isinstance(cfg, dict):
        raise UnsupportedFleetMode(
            "research fleet spec needs a 'research' config block "
            "(modalities, argv); none supplied")
    modalities = cfg.get("modalities")
    argv = cfg.get("argv")
    if not (isinstance(modalities, list) and modalities and all(
            isinstance(m, str) for m in modalities)):
        raise UnsupportedFleetMode("research config needs a non-empty modalities list")
    if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
        raise UnsupportedFleetMode("research config needs a non-empty argv list")
    repo = Path(spec["tool"])
    raw_peer = cfg.get("peer")
    peer: str = raw_peer if isinstance(raw_peer, str) and raw_peer else "research"
    use_stdin = cfg.get("prompt_mode") == "stdin"

    def run_agent(prompt: str) -> str:
        return run_agent_once(prompt, argv=argv, cwd=repo, stdin=use_stdin)

    return make_research_frontend(
        repo, run_agent=run_agent, modalities=modalities, attest_peer=peer)


def _build_find_bugs(spec: dict):
    """Construct a real generic FindBugsFrontend from a fleet spec's ``find-bugs``
    block. Fail-closed (UnsupportedFleetMode) on a missing/empty config — incl. the
    required ``input`` seed path + ``fuzz_binary`` (the engine's chitin harness)."""
    from peers.agent_invoke import run_agent_once
    from peers.modes.find_bugs_reproduce.assembly import make_find_bugs_frontend
    from peers.modes.find_bugs_reproduce.chitin_backend import ChitinClient
    from peers.modes.find_bugs_reproduce.intake import FileInputSource

    cfg = spec.get("find-bugs")
    if not isinstance(cfg, dict):
        raise UnsupportedFleetMode(
            "find-bugs fleet spec needs a 'find-bugs' config block "
            "(input, fuzz_binary, argv); none supplied")
    seed = cfg.get("input")
    fuzz_binary = cfg.get("fuzz_binary")
    argv = cfg.get("argv")
    raw_ladder = cfg.get("ladder")
    ladder: str = raw_ladder if isinstance(raw_ladder, str) and raw_ladder in (
        "llm_assisted", "llm_free") else "llm_assisted"
    if not (isinstance(seed, str) and seed):
        raise UnsupportedFleetMode("find-bugs config needs an 'input' seed file path")
    if not (isinstance(fuzz_binary, str) and fuzz_binary):
        raise UnsupportedFleetMode("find-bugs config needs a 'fuzz_binary' (the chitin harness)")
    if ladder != "llm_free" and not (
            isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
        raise UnsupportedFleetMode(
            "find-bugs llm_assisted config needs a non-empty argv list (or ladder: llm_free)")
    repo = Path(spec["tool"])
    raw_peer = cfg.get("peer")
    peer: str = raw_peer if isinstance(raw_peer, str) and raw_peer else "find-bugs"
    use_stdin = cfg.get("prompt_mode") == "stdin"
    eff_argv = argv if (isinstance(argv, list) and argv) else ["true"]

    def run_agent(prompt: str) -> str:
        return run_agent_once(prompt, argv=eff_argv, cwd=repo, stdin=use_stdin)

    return make_find_bugs_frontend(
        repo, input_source=FileInputSource(Path(seed), bug_id=cfg.get("bug_id")),
        run_agent=run_agent, chitin=ChitinClient(), fuzz_binary=fuzz_binary,
        expected_function=cfg.get("expected_function"), ladder_profile=ladder,
        attest_peer=peer)


def install() -> None:
    """The PEERS_FLEET_BUILDERS hook: register the in-tree builders. Idempotent."""
    register_frontend_builder("develop", _build_develop)
    register_frontend_builder("research", _build_research)
    register_frontend_builder("find-bugs:reproduce", _build_find_bugs)
