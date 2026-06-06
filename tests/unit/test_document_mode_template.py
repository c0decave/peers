from __future__ import annotations

from pathlib import Path

import yaml

import peers
from peers.driver_helpers import _load_phase_prompt
from peers.goals import load_goals
from peers.modes import discover

_MODE_DIR = Path(peers.__file__).parent / "templates" / "modes" / "document"


def test_document_mode_discovered():
    modes = discover()
    assert "document" in modes
    assert modes["document"].name == "document"


def test_document_check_scripts_present():
    checks = _MODE_DIR / "checks"
    for name in ("grounded", "signature_match", "complete", "summaries_complete",
                 "agents_in_sync"):
        assert (checks / f"{name}.py").is_file(), name


def test_document_goals_wire_the_full_runnable_gate_set():
    goals = yaml.safe_load((_MODE_DIR / "goals.yaml").read_text())["goals"]
    by_id = {g["id"]: g for g in goals}
    # self-review + CODEMAP gates + agents-in-sync are hard; cross-review is soft.
    hard = {"self-review-on-handoff", "codemap-grounded", "codemap-signature-match",
            "codemap-complete", "codemap-summaries-complete", "agents-in-sync"}
    assert hard <= set(by_id)
    assert all(by_id[i]["type"] == "hard" for i in hard)
    assert by_id["summaries-cross-review"]["type"] == "soft"
    assert by_id["summaries-cross-review"]["reviewer"] == "other"
    # the CODEMAP gates resolve via run-check (mode-template-relative)
    for i in ("codemap-grounded", "codemap-signature-match", "codemap-complete",
              "codemap-summaries-complete", "agents-in-sync"):
        assert "run-check" in by_id[i]["cmd"]


def test_document_goals_validate_through_the_real_loader(tmp_path):
    # The reworked goals.yaml must actually load+validate through the substrate
    # goals loader — i.e. a `--modes=document` project would scaffold runnable.
    dst = tmp_path / "goals.yaml"
    dst.write_text((_MODE_DIR / "goals.yaml").read_text(), encoding="utf-8")
    loaded = load_goals(dst)
    ids = {g.id for g in loaded}
    assert "codemap-summaries-complete" in ids
    assert "summaries-cross-review" in ids


def test_document_implementation_prompt_loads_every_tick():
    # document resolves to the "implementation" phase on every tick, and the
    # generic _load_phase_prompt loads prompts/implementation.md as the overlay.
    prompt = _load_phase_prompt("document", "implementation")
    assert prompt is not None
    low = prompt.lower()
    assert "codemap" in low and "summary" in low
    # the task brief must warn against editing the structural fields
    assert "signature" in low
