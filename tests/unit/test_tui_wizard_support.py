"""Default-python tests for the launch-wizard pure helpers (Unit I).

These cover the textual-free parsing/derivation helpers used by the wizard
modal: ``parse_modes_list`` (parsing ``peers-ctl modes list`` output) and
``doctor_capabilities`` (deriving host/container gating from a doctor result).
Happy / sad / edge for each. No Textual import → runs in default ``.[dev]`` CI.
"""
from __future__ import annotations

from peers_ctl.tui.screens import wizard_support as W


# --------------------------------------------------------------------------- #
# parse_modes_list                                                             #
# --------------------------------------------------------------------------- #
_REAL_OUTPUT = (
    "NAME              VER    SOURCE    DESCRIPTION\n"
    "audit             v1     builtin   Bug-hunt + coverage + secrets gates\n"
    "implement         v1     builtin   End-to-end feature implementation from a\n"
    "  markdown PLAN.md with acceptance gates (wrapped description line)\n"
    "document          v1     builtin   Build a verified CODEMAP of the codebase.\n"
    "hunt-open-ended   v1     builtin   Open-ended bug-hunt mode: convergence is a\n"
    "progress signal, not a hard gate (wrapped, no leading mode name)\n"
)


def test_parse_modes_list_happy_extracts_names_sorted():
    modes = W.parse_modes_list(_REAL_OUTPUT)
    # the 4 real mode names, sorted, with wrapped description lines ignored.
    assert modes == ["audit", "document", "hunt-open-ended", "implement"]
    # the header row "NAME ... VER" is NOT mistaken for a mode.
    assert "NAME" not in modes
    # wrapped description lines never leak in as fake modes.
    assert "markdown" not in modes
    assert "progress" not in modes


def test_parse_modes_list_sad_empty_and_no_modes():
    # empty / whitespace / the explicit "(no modes available)" sentinel -> [].
    assert W.parse_modes_list("") == []
    assert W.parse_modes_list("   \n  \n") == []
    assert W.parse_modes_list("(no modes available)\n") == []


def test_parse_modes_list_edge_dedup_and_non_table_noise():
    noisy = (
        "audit             v1     builtin   first\n"
        "audit             v2     user      a SHADOWED duplicate name\n"
        "Traceback (most recent call last):\n"
        "  File 'x', line 1\n"
        "security-cpp-memory  v1   builtin   memory corruption modes\n"
    )
    modes = W.parse_modes_list(noisy)
    # duplicate 'audit' collapses to one; traceback noise is ignored.
    assert modes == ["audit", "security-cpp-memory"]


# --------------------------------------------------------------------------- #
# doctor_capabilities                                                          #
# --------------------------------------------------------------------------- #
_DOCTOR_OK = [
    "peers-ctl doctor — environment preflight",
    "",
    "  [OK]    podman                  5.8.2",
    "  [OK]    peers:dev image         1.6.0",
    "  [OK]    peers version           host=1.6.0 container=1.6.0",
    "  [OK]    git                     present",
    "",
    "Summary: 8 ok, 0 warn, 0 miss.",
]


def test_doctor_capabilities_happy_all_present():
    cap = W.doctor_capabilities(ok=True, lines=_DOCTOR_OK)
    assert cap.ok is True
    assert cap.podman_present is True
    assert cap.peers_present is True
    assert cap.summary == "Summary: 8 ok, 0 warn, 0 miss."


def test_doctor_capabilities_sad_podman_missing_disables_container():
    lines = [
        "  [MISS]  podman                  not found",
        "  [OK]    peers version           host=1.6.0",
        "Summary: 1 ok, 0 warn, 1 miss.",
    ]
    cap = W.doctor_capabilities(ok=False, lines=lines)
    assert cap.ok is False
    # podman MISS -> container choice must be gated off.
    assert cap.podman_present is False
    # peers itself is still present (host runs allowed).
    assert cap.peers_present is True


def test_doctor_capabilities_edge_warn_is_not_miss_and_absent_probe_present():
    # a WARN (soft) on an optional image must NOT disable podman; a probe that
    # is entirely absent is treated as present (fail-soft, don't over-block).
    lines = [
        "  [OK]    podman                  5.8.2",
        "  [WARN]  peers-egress-proxy:dev  not built",
        "Summary: 1 ok, 1 warn, 0 miss.",
    ]
    cap = W.doctor_capabilities(ok=False, lines=lines)
    assert cap.podman_present is True  # WARN elsewhere doesn't gate podman
    assert cap.peers_present is True   # 'peers version' absent -> assume present
    # no Summary parse failure: it's taken verbatim.
    assert cap.summary.startswith("Summary:")


def test_doctor_capabilities_edge_no_summary_line_synthesizes():
    cap = W.doctor_capabilities(ok=True, lines=["  [OK]    podman   5.8.2"])
    assert cap.summary == "doctor: ok"
    cap_bad = W.doctor_capabilities(ok=False, lines=[])
    assert cap_bad.summary == "doctor: issues"
    # empty lines -> nothing MISS'd -> both treated present (fail-soft).
    assert cap_bad.podman_present is True
    assert cap_bad.peers_present is True
