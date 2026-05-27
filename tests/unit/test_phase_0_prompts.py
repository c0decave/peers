"""Test Phase 0 prompts load + integrate (Tasks 4.2/4.3/4.4).

Three prompt templates ship under
`src/peers/templates/modes/implement/prompts/` and get applied as a
prompt overlay by the driver during implement-mode ticks 0/1/2
(recon → alignment → architecture).
"""
from __future__ import annotations

from pathlib import Path

from peers.driver_orchestrator import _load_phase_prompt


_REPO = Path(__file__).resolve().parents[2]
_PROMPTS = _REPO / "src/peers/templates/modes/implement/prompts"


def test_recon_prompt_exists():
    p = _PROMPTS / "recon.md"
    assert p.is_file()
    text = p.read_text()
    assert "RECON.md" in text
    assert "Module Map" in text or "module" in text.lower()


def test_alignment_prompt_exists():
    p = _PROMPTS / "alignment.md"
    assert p.is_file()
    text = p.read_text()
    assert "PLAN.aligned.md" in text
    assert "touches" in text.lower()


def test_architecture_prompt_exists():
    p = _PROMPTS / "architecture.md"
    assert p.is_file()
    text = p.read_text()
    assert "ARCHITECTURE.intended.md" in text


def test_load_phase_prompt_recon():
    prompt = _load_phase_prompt("implement", "recon")
    assert prompt is not None
    assert "RECON.md" in prompt


def test_load_phase_prompt_alignment():
    prompt = _load_phase_prompt("implement", "alignment")
    assert prompt is not None
    assert "PLAN.aligned.md" in prompt


def test_load_phase_prompt_architecture():
    prompt = _load_phase_prompt("implement", "architecture")
    assert prompt is not None
    assert "ARCHITECTURE.intended.md" in prompt


def test_load_phase_prompt_implementation_returns_none():
    # Implementation phase doesn't have a Phase-0-style prompt template
    assert _load_phase_prompt("implement", "implementation") is None


def test_load_phase_prompt_unknown_mode_returns_none():
    assert _load_phase_prompt("nonexistent-mode", "recon") is None


def test_load_phase_prompt_unknown_phase_returns_none():
    assert _load_phase_prompt("implement", "bogus_phase") is None
