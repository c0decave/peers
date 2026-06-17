"""BRAIN-09 regression: ``budget.max_usd`` / ``budget.max_tokens`` caps are
enforced from parsed token/cost output. The parsers only read the structured
``--output-format json/stream-json`` (claude), ``--json`` (codex) and
``--format json`` (opencode) shapes; a plain ``claude -p {PROMPT}`` argv yields
(0, 0.0) every tick, so the cap is silently inert. ``budget_argv_warning``
gives the operator a preflight heads-up instead of a silently unenforced cap.
"""
from __future__ import annotations

from dataclasses import dataclass

from peers.budget_accountant import budget_argv_warning, budget_argv_warnings


@dataclass(frozen=True)
class _Spec:
    name: str
    tool: str
    argv: tuple[str, ...]


# --- happy path: a JSON-emitting argv with a cap -> no warning ---------------
def test_happy_claude_stream_json_with_cap_emits_no_warning() -> None:
    argv = ["claude", "-p", "--output-format", "stream-json", "--verbose", "{PROMPT}"]
    assert budget_argv_warning("claude", argv, max_usd=10.0) is None


def test_happy_no_cap_set_never_warns_even_on_plain_argv() -> None:
    # nothing to enforce -> no noise.
    argv = ["claude", "-p", "{PROMPT}"]
    assert budget_argv_warning("claude", argv, max_tokens=None, max_usd=None) is None


# --- sad path: a cap set but the argv cannot produce accounting ---------------
def test_sad_plain_claude_argv_with_max_usd_warns() -> None:
    argv = ["claude", "-p", "--dangerously-skip-permissions", "{PROMPT}"]
    warn = budget_argv_warning("claude", argv, max_usd=10.0)
    assert warn is not None
    assert "max_usd" in warn
    assert "output-format" in warn or "json" in warn.lower()


def test_sad_plain_claude_argv_with_max_tokens_warns() -> None:
    argv = ["claude", "-p", "{PROMPT}"]
    warn = budget_argv_warning("claude", argv, max_tokens=500_000)
    assert warn is not None
    assert "max_tokens" in warn


# --- edge cases ---------------------------------------------------------------
def test_edge_codex_json_and_opencode_format_json_recognized() -> None:
    assert budget_argv_warning(
        "codex", ["codex", "exec", "--json", "{PROMPT}"], max_usd=5.0
    ) is None
    assert budget_argv_warning(
        "opencode", ["opencode", "run", "--format", "json", "{PROMPT}"], max_tokens=1000
    ) is None


def test_edge_output_format_equals_form_recognized() -> None:
    argv = ["claude", "-p", "--output-format=json", "{PROMPT}"]
    assert budget_argv_warning("claude", argv, max_usd=1.0) is None


def test_edge_unknown_tool_does_not_false_warn() -> None:
    # no parser for the tool -> we cannot judge accounting; stay silent.
    assert budget_argv_warning(
        "mystery", ["mystery", "{PROMPT}"], max_usd=10.0
    ) is None


def test_edge_zero_cap_is_treated_as_unset() -> None:
    assert budget_argv_warning(
        "claude", ["claude", "-p", "{PROMPT}"], max_usd=0.0, max_tokens=0
    ) is None


# --- multi-spec preflight (the CLI surface) ----------------------------------
def test_happy_all_specs_json_no_warnings() -> None:
    specs = [
        _Spec("claude", "claude", ("claude", "-p", "--output-format", "stream-json", "{PROMPT}")),
        _Spec("codex", "codex", ("codex", "exec", "--json", "{PROMPT}")),
    ]
    assert budget_argv_warnings(specs, max_usd=10.0) == []


def test_sad_plain_spec_yields_named_warning() -> None:
    specs = [_Spec("claude", "claude", ("claude", "-p", "{PROMPT}"))]
    warns = budget_argv_warnings(specs, max_usd=10.0)
    assert len(warns) == 1
    assert warns[0].startswith("peer 'claude':")


def test_edge_only_offending_specs_warn_and_no_cap_is_silent() -> None:
    specs = [
        _Spec("claude", "claude", ("claude", "-p", "{PROMPT}")),  # plain -> warns
        _Spec("codex", "codex", ("codex", "exec", "--json", "{PROMPT}")),  # ok
    ]
    warns = budget_argv_warnings(specs, max_tokens=1000)
    assert len(warns) == 1 and "peer 'claude'" in warns[0]
    # no cap -> silent regardless of argv
    assert budget_argv_warnings(specs, max_tokens=None, max_usd=None) == []
