"""R5: a real research ``CompletenessCritic`` (``DeterministicCompletenessCritic``).

Per the spec, a round is ``finder-exhausted`` (a dry round that does NOT advance
stop-on-dry) when an enabled modality was not actually run; otherwise
``work-done``. Deterministic — no LLM — so it cannot lie about coverage.
"""
from __future__ import annotations

from peers.research.adapters import DeterministicCompletenessCritic
from peers.research.ports import CompletenessCritic, CompletenessVerdict


def test_happy_all_enabled_modalities_run_is_work_done() -> None:
    c = DeterministicCompletenessCritic()
    assert isinstance(c, CompletenessCritic)
    v = c.assess([], [], ["codebase", "web"], ["codebase", "web"])
    assert isinstance(v, CompletenessVerdict)
    assert v.state == "work-done"
    assert v.not_checked == []


def test_sad_skipped_modality_is_finder_exhausted() -> None:
    v = DeterministicCompletenessCritic().assess([], [], ["codebase"], ["codebase", "web"])
    assert v.state == "finder-exhausted"
    assert v.not_checked == ["web"]


def test_edge_no_enabled_modalities_is_work_done() -> None:
    v = DeterministicCompletenessCritic().assess([], [], [], [])
    assert v.state == "work-done"
    assert v.not_checked == []


def test_edge_run_superset_of_enabled_is_work_done() -> None:
    v = DeterministicCompletenessCritic().assess([], [], ["codebase", "web", "x"], ["codebase"])
    assert v.state == "work-done"
    assert v.not_checked == []
