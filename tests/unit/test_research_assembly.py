"""R5 wiring: make_research_frontend assembles the real research adapters into
an operator-runnable ResearchFrontend (the `peers research` CLI + the fleet
builder call this). Agents/fetchers injected for determinism."""
from __future__ import annotations

from pathlib import Path

from peers.research.adapters import (
    CodebaseSweeper,
    DeterministicCompletenessCritic,
    LLMDecomposer,
    ReportCommitter,
    ReportSynthesizer,
)
from peers.research.assembly import make_research_frontend
from peers.research.frontend import ResearchFrontend
from peers.research.ports import Claim, Witness


def _confirmed_with_urls():
    return [Claim(id="c1", text="X is true", status="confirmed", load_bearing=True,
                  witnesses=[
                      Witness(kind="fetched-source", uri="https://a.example/1",
                              content_hash="h1", resolved_origin="a.example"),
                      Witness(kind="fetched-source", uri="https://b.example/2",
                              content_hash="h2", resolved_origin="b.example")])]


def test_happy_assembles_the_real_adapter_types() -> None:
    fe = make_research_frontend(
        Path("/repo"), run_agent=lambda p: "[]",
        modalities=["codebase"], run_tests=lambda c: (0, "ok"))
    assert isinstance(fe, ResearchFrontend)
    assert isinstance(fe.decomposer, LLMDecomposer)
    assert isinstance(fe.sweeper, CodebaseSweeper)
    assert isinstance(fe.synthesizer, ReportSynthesizer)
    assert isinstance(fe.committer, ReportCommitter)
    assert isinstance(fe.critic, DeterministicCompletenessCritic)
    assert fe.modalities == ["codebase"]


def test_happy_wires_a_real_claim_refuter_not_the_stub() -> None:
    # mirrors develop HS-04: the wired frontend must use a REAL refuter.
    fe = make_research_frontend(
        Path("/repo"), run_agent=lambda p: '{"refuted": false}',
        modalities=["codebase"], run_tests=lambda c: (0, "ok"))
    c = Claim(id="c1", text="X causes Y", status="", witnesses=[], load_bearing=True)
    assert fe.refuter_factory(c)(0) is False   # a confirming model -> claim survives


def test_happy_synthesizer_with_url_witnesses_produces_a_cited_report(tmp_path) -> None:
    # RW-01 (HIGH): the wired renderer must embed the confirmed claims' source
    # URLs so check_report's CITED floor (>=2 http URLs) can pass — otherwise
    # synthesize always returns None and research can never commit a report.
    fe = make_research_frontend(
        tmp_path, run_agent=lambda p: "A grounded narrative of the findings.",
        modalities=["web"], run_tests=lambda c: (0, "ok"))
    art = fe.synthesizer.synthesize(_confirmed_with_urls(), [], tmp_path)
    assert art is not None                       # was None (inert) before the fix
    body = (tmp_path / "RESEARCH.md").read_text(encoding="utf-8")
    assert "https://a.example/1" in body and "https://b.example/2" in body


def test_edge_codebase_only_claims_cannot_cite_so_synthesize_is_honest_dry(tmp_path) -> None:
    # codebase code-locations are file:line, not http URLs -> the report cannot
    # satisfy the URL-citation floor -> honest dry (None), never a forged report.
    fe = make_research_frontend(
        tmp_path, run_agent=lambda p: "prose", modalities=["codebase"],
        run_tests=lambda c: (0, "ok"))
    claims = [Claim(id="c1", text="X", status="confirmed", load_bearing=True,
                    witnesses=[Witness(kind="code-location", uri="a.py:1",
                                       content_hash="h", resolved_origin="a.py")])]
    assert fe.synthesizer.synthesize(claims, [], tmp_path) is None


def test_edge_web_modality_fetcher_threaded_to_sweeper() -> None:
    seen = {}

    def fake_search(q):
        seen["searched"] = q
        return []

    fe = make_research_frontend(
        Path("/repo"), run_agent=lambda p: "[]", modalities=["web"],
        run_tests=lambda c: (0, "ok"), web_search=fake_search, fetch=lambda u: None)
    fe.sweeper.sweep("the question", Path("/repo"), ["web"])
    assert seen.get("searched") == "the question"
