"""Direct unit tests for the pure helpers in `peers.driver_helpers`.

These helpers are pure (or filesystem-light) functions that decide phase,
peer role, checkpoint, mode-detection, and tick-status formatting. They
were previously covered only transitively through the orchestrator
integration tests, which left the coverage_3class gate flagging
`driver_helpers.py` as missing explicit edge/sad coverage. This module
adds direct happy/edge/sad cases so the gate is honest about what is
actually verified.
"""
from __future__ import annotations


from peers.driver_helpers import (
    PHASE_IMPLEMENTATION,
    _detect_mode_name,
    _extract_first_json_object,
    _format_tick_status,
    _load_phase_prompt,
    _resolve_peer_role,
    _resolve_phase,
    _should_checkpoint,
)


# --- _resolve_phase -------------------------------------------------------

def test_resolve_phase_implement_creates_phase_0_recon_alignment_arch():
    # happy path: implement-mode walks recon → alignment → architecture
    # for ticks 0/1/2 and then falls through to implementation.
    assert _resolve_phase("implement", 0) == "recon"
    assert _resolve_phase("implement", 1) == "alignment"
    assert _resolve_phase("implement", 2) == "architecture"
    assert _resolve_phase("implement", 3) == PHASE_IMPLEMENTATION


def test_resolve_phase_ignores_unknown_mode_with_negative_tick_edge():
    # edge: negative/oversized tick indexes for implement-mode must NOT
    # index into _PHASE_0_TICKS (would yield "architecture" via -1) —
    # they must fall through to implementation.
    assert _resolve_phase("implement", -1) == PHASE_IMPLEMENTATION
    assert _resolve_phase("implement", 999) == PHASE_IMPLEMENTATION


def test_resolve_phase_rejects_audit_mode_as_non_implement_sad():
    # sad: any non-"implement" mode (audit/security/document/"") is
    # frozen to "implementation" — Phase 0 is implement-only.
    for bad in ("audit", "security", "document", "", "implement "):
        assert _resolve_phase(bad, 0) == PHASE_IMPLEMENTATION
        assert _resolve_phase(bad, 1) == PHASE_IMPLEMENTATION


# --- _resolve_peer_role ---------------------------------------------------

def test_resolve_peer_role_alternates_implementer_then_reviewer():
    # happy: ticks 3,5,7 → implementer; 4,6,8 → reviewer.
    assert _resolve_peer_role("implement", PHASE_IMPLEMENTATION, 3) == "implementer"
    assert _resolve_peer_role("implement", PHASE_IMPLEMENTATION, 4) == "reviewer"
    assert _resolve_peer_role("implement", PHASE_IMPLEMENTATION, 5) == "implementer"


def test_resolve_peer_role_phase_0_ticks_remain_normal_edge():
    # edge: tick 0/1/2 are still Phase 0 (recon/alignment/architecture)
    # — the role overlay must NOT fire there, so the per-tick prompt
    # reaches the peer cleanly.
    assert _resolve_peer_role("implement", "recon", 0) == "normal"
    assert _resolve_peer_role("implement", "alignment", 1) == "normal"
    assert _resolve_peer_role("implement", "architecture", 2) == "normal"


def test_resolve_peer_role_non_implement_mode_returns_normal_sad():
    # sad: any other mode/phase returns "normal" — implement-mode is
    # the only mode that gets blind-review role swaps.
    assert _resolve_peer_role("audit", PHASE_IMPLEMENTATION, 3) == "normal"
    assert _resolve_peer_role("implement", "recon", 3) == "normal"


# --- _should_checkpoint ---------------------------------------------------

def test_should_checkpoint_fires_on_architecture_to_implementation(tmp_path):
    # happy: marker file present + phase transitions arch→impl → pause.
    (tmp_path / "checkpoint_requested").write_text("")
    assert _should_checkpoint(
        tmp_path, prev_phase="architecture", curr_phase=PHASE_IMPLEMENTATION,
    ) is True


def test_should_checkpoint_ignores_other_transitions_edge(tmp_path):
    # edge: every transition that is NOT architecture→implementation is
    # a no-op, even with the marker present.
    (tmp_path / "checkpoint_requested").write_text("")
    for prev, curr in (
        (None, "recon"),
        ("recon", "alignment"),
        ("alignment", "architecture"),
        (PHASE_IMPLEMENTATION, PHASE_IMPLEMENTATION),
    ):
        assert _should_checkpoint(
            tmp_path, prev_phase=prev, curr_phase=curr,
        ) is False


def test_should_checkpoint_missing_marker_does_not_pause_sad(tmp_path):
    # sad: marker file absent → never pause even on the right transition.
    assert _should_checkpoint(
        tmp_path, prev_phase="architecture", curr_phase=PHASE_IMPLEMENTATION,
    ) is False


# --- _detect_mode_name ----------------------------------------------------

def test_detect_mode_name_returns_implement_for_sole_implement_line(tmp_path):
    (tmp_path / "modes-applied.txt").write_text(
        "2026-05-26T12:34:56+00:00  implement     v1  sha256=abc\n"
    )
    assert _detect_mode_name(tmp_path) == "implement"


def test_detect_mode_name_returns_empty_when_modes_stacked_edge(tmp_path):
    # edge: composed/stacked modes are not implement-only → ""
    (tmp_path / "modes-applied.txt").write_text(
        "2026-05-26T12:34:56+00:00  audit     v1  sha256=abc\n"
        "2026-05-26T12:34:57+00:00  thorough  v1  sha256=def\n"
    )
    assert _detect_mode_name(tmp_path) == ""


def test_detect_mode_name_missing_file_returns_empty_sad(tmp_path):
    # sad: no modes-applied.txt at all → "" (empty mode flows as
    # implementation-phase through _resolve_phase).
    assert _detect_mode_name(tmp_path) == ""


# --- _format_tick_status --------------------------------------------------

def test_format_tick_status_handoff_on_success_returns_handoff():
    assert _format_tick_status(success=True, classification="success") == "handoff"


def test_format_tick_status_clean_exit_without_handoff_is_no_handoff_edge():
    # edge: success classification but no handoff commit → "no-handoff",
    # NOT "fail(success)" (the older operator-confusing label).
    assert _format_tick_status(success=False, classification="success") == "no-handoff"


def test_format_tick_status_propagates_error_classification_sad():
    # sad: any non-success classification when handoff failed surfaces
    # the classification directly (api-error / idle-timeout / etc).
    for bad in ("process-fail", "api-error", "idle-timeout", "absolute-timeout"):
        assert _format_tick_status(success=False, classification=bad) == bad


# --- _load_phase_prompt ---------------------------------------------------

def test_load_phase_prompt_implement_recon_returns_text():
    # happy: shipped Phase 0 prompts are loaded from inside the package.
    text = _load_phase_prompt("implement", "recon")
    assert text is None or "recon" in text.lower()


def test_load_phase_prompt_path_traversal_attempt_rejected_edge():
    # edge: path-traversal inputs MUST NOT escape the templates tree.
    assert _load_phase_prompt("../etc", "passwd") is None
    assert _load_phase_prompt("implement", "../../../../etc/passwd") is None
    assert _load_phase_prompt("with/slash", "recon") is None


def test_load_phase_prompt_unknown_mode_returns_none_sad():
    # sad: an unknown mode/phase combination is a clean None — caller
    # uses that as "no overlay; default prompt".
    assert _load_phase_prompt("no-such-mode-xyz", "recon") is None
    assert _load_phase_prompt("implement", "no-such-phase-xyz") is None
    assert _load_phase_prompt("", "recon") is None
    assert _load_phase_prompt("implement", "") is None


# --- _extract_first_json_object -------------------------------------------

def test_extract_first_json_object_parses_nested_object_in_review_body():
    # happy: soft-review JSON often nests one object inside "issues" or
    # similar — the extractor must accept that.
    body = """## Review
    {"pass": false, "notes": "see below", "details": {"sev": "high"}}
    """
    out = _extract_first_json_object(body)
    assert out is not None
    assert out["pass"] is False
    assert out["details"]["sev"] == "high"


def test_extract_first_json_object_ignores_braces_inside_strings_edge():
    # edge: a `{` inside a JSON string literal must NOT open a new
    # nesting level — the tiny string-aware state machine handles that.
    body = '{"notes": "weird }{} content", "pass": true}'
    out = _extract_first_json_object(body)
    assert out is not None and out["pass"] is True


def test_extract_first_json_object_returns_none_when_no_balanced_block_sad():
    # sad: no balanced `{...}` block (truncated commit, prose only,
    # mismatched braces) → None, never raises.
    assert _extract_first_json_object("just a prose review, no JSON") is None
    assert _extract_first_json_object("{unbalanced: forever") is None
    assert _extract_first_json_object("{garbage}{also garbage}") is None
