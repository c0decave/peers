"""Wiring: assemble an operator-runnable research ``ResearchFrontend`` from the
real adapters (the `peers research` CLI + the fleet builder both call this).

``run_agent`` (one-shot ``prompt -> text``) drives DECOMPOSE, the claim refuter,
and the report renderer; ``web_search``/``fetch`` (optional) enable the Sweeper's
web modality. All injected so the assembly is deterministic in tests; production
wires them to the configured peer spec / a real fetcher.
"""
from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path

from peers.research.adapters import (
    CodebaseSweeper,
    DeterministicCompletenessCritic,
    LLMClaimRefuter,
    LLMDecomposer,
    ReportCommitter,
    ReportSynthesizer,
    RunAgent,
)
from peers.research.frontend import ResearchFrontend

_CMD_TIMEOUT_S = 600.0


def _default_run_tests(repo: Path) -> Callable[[str], "tuple[int, str] | None"]:
    def _run_tests(cmd: str) -> "tuple[int, str] | None":
        try:
            r = subprocess.run(shlex.split(cmd), cwd=str(repo), capture_output=True,
                               text=True, timeout=_CMD_TIMEOUT_S, check=False)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        return (r.returncode, (r.stdout or "") + (r.stderr or ""))

    return _run_tests


def _claim_urls(claims) -> list[str]:
    """Distinct http(s) source URLs across the confirmed claims' witnesses — the
    citations check_report's CITED floor counts. code-location witnesses
    (file:line) are NOT URLs, so a codebase-only round yields none (an honest dry
    round upstream — a URL-cited report cannot be written from code grep alone)."""
    urls: list[str] = []
    for c in claims:
        for w in getattr(c, "witnesses", []) or []:
            uri = getattr(w, "uri", "") or ""
            if (uri.startswith("http://") or uri.startswith("https://")) and uri not in urls:
                urls.append(uri)
    return urls


def _report_renderer(run_agent: RunAgent) -> Callable[[list, list], str]:
    """Build the Synthesizer's ``write_report(claims, gaps) -> markdown``.

    The LLM writes the narrative, but the STRUCTURAL floors check_report enforces
    are emitted DETERMINISTICALLY: a ``## Sources`` section listing the confirmed
    claims' real http(s) URLs (the CITED floor counts these), and a non-empty
    ``## Gaps`` section. The Synthesizer still gates the whole file with
    check_report and is the sole writer, so a renderer that cannot cite (no URLs)
    or an LLM narrative that smuggles an overclaim still fails closed to a dry
    round — never a forged/uncited report."""
    def write_report(claims, gaps) -> str:
        claims = list(claims)
        gaps = list(gaps)
        urls = _claim_urls(claims)
        cited = "\n".join(
            f"- [{getattr(c, 'id', '')}] {getattr(c, 'text', '')}" for c in claims)
        prompt = (
            "Write the NARRATIVE body (markdown prose, no headings) of a research "
            "report grounding these confirmed claims in their sources. Do NOT "
            "claim completeness/exhaustiveness.\n\nConfirmed claims:\n" + cited
        )
        try:
            narrative = run_agent(prompt)
        except Exception:  # noqa: BLE001 — a failed narrative still yields the structural body
            narrative = ""
        parts = [
            "# Research Report", "",
            narrative.strip() if isinstance(narrative, str) else "", "",
            "## Confirmed", cited or "- (none)", "",
            "## Sources",
            *(f"- {u}" for u in urls), "",
            "## Gaps",
        ]
        if gaps:
            parts += [f"- {getattr(g, 'text', '')}" for g in gaps]
        else:
            parts.append(
                "No further claims were corroborated this round; modalities not "
                "run may hold additional evidence.")
        return "\n".join(parts)

    return write_report


def make_research_frontend(
    repo: str | Path,
    *,
    run_agent: RunAgent,
    modalities: list[str],
    run_tests: Callable[[str], "tuple[int, str] | None"] | None = None,
    web_search: Callable[[str], "list[str]"] | None = None,
    fetch: Callable[[str], "tuple[bytes, str] | None"] | None = None,
    k: int = 2,
    attest_peer: str = "research",
) -> ResearchFrontend:
    """Assemble a ResearchFrontend with the real research adapters + a real
    (non-stub) claim refuter."""
    repo = Path(repo)
    decomposer = LLMDecomposer(run_agent=run_agent)
    sweeper = CodebaseSweeper(web_search=web_search, fetch=fetch)
    synthesizer = ReportSynthesizer(write_report=_report_renderer(run_agent))
    committer = ReportCommitter(attest_peer=attest_peer)
    critic = DeterministicCompletenessCritic()
    refuter = LLMClaimRefuter(run_agent=run_agent)
    return ResearchFrontend(
        decomposer, sweeper, synthesizer, committer, critic,
        modalities=list(modalities),
        run_tests=run_tests or _default_run_tests(repo),
        k=k,
        refuter_factory=refuter.refuter_factory,
    )
