"""Pure, textual-free helpers for the launch wizard (Unit I).

These are the parsing / capability-derivation helpers the :mod:`wizard` modal
needs but which carry no Textual dependency — so they live here and are tested
under the default python interpreter (no ``[tui]`` extra), happy/sad/edge.

Nothing here reimplements write logic; it only *parses the output* of the
existing read-only verbs (``peers-ctl modes list`` and ``peers-ctl doctor``) so
the modal can populate its controls and gate its buttons. The actual scaffold /
launch still goes through ``actions.build_new_argv`` + ``build_start_argv`` +
``run_verb``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: A safe default mode list, used when ``peers-ctl modes list`` cannot be run /
#: parsed (fail-soft). Deliberately small + uncontroversial.
DEFAULT_MODES: tuple[str, ...] = ("audit", "implement", "document", "describe")

#: A mode-table row starts with the mode NAME token, then whitespace, then a
#: version token ``v<digits>``. The multi-line DESCRIPTION wraps onto following
#: lines that do NOT match this shape, and the header row (``NAME  VER ...``)
#: has ``VER`` (no leading ``v<digit>``), so neither is mistaken for a mode.
_MODE_ROW = re.compile(r"^(?P<name>\S+)\s+v\d")


def parse_modes_list(output: str) -> list[str]:
    """Parse ``peers-ctl modes list`` stdout into a sorted, de-duplicated list.

    The verb prints a ``NAME  VER  SOURCE  DESCRIPTION`` table whose description
    column can wrap across multiple lines. A real mode row is the only kind of
    line that begins with a non-space token immediately followed by a ``v<n>``
    version token, so we key on that and take the first token as the mode name.

    Fail-soft: empty / unparseable / "(no modes available)" output yields ``[]``
    (the caller falls back to :data:`DEFAULT_MODES`). Never raises.
    """
    if not output:
        return []
    names: list[str] = []
    seen: set[str] = set()
    for line in output.splitlines():
        m = _MODE_ROW.match(line)
        if m is None:
            continue
        name = m.group("name")
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return sorted(names)


@dataclass(frozen=True)
class HostCapabilities:
    """What the wizard learned from ``peers-ctl doctor`` about this host.

    Drives the host-vs-container gating + the launch button. ``ok`` mirrors the
    doctor exit code (all REQUIRED probes passed). ``peers_present`` /
    ``podman_present`` are derived from the per-probe ``[OK]/[WARN]/[MISS]``
    lines so the wizard can independently disable the host or container choice
    even when the overall run is non-zero (e.g. a soft WARN elsewhere).
    """

    ok: bool
    peers_present: bool
    podman_present: bool
    summary: str


#: A probe line looks like ``  [OK]    podman                  5.8.2``. We pull
#: the status token and the label so we can answer "is podman/peers present?".
_PROBE_LINE = re.compile(r"\[(?P<status>OK|WARN|MISS)\]\s+(?P<label>\S.*?)\s{2,}")
#: fallback when the value column is absent (label runs to end of line).
_PROBE_LINE_NOVAL = re.compile(r"\[(?P<status>OK|WARN|MISS)\]\s+(?P<label>\S.*?)\s*$")


def _probe_status(lines: list[str], needle: str) -> str | None:
    """Return the status (OK/WARN/MISS) of the first probe whose label contains
    ``needle`` (case-insensitive), or ``None`` if no such probe line is found."""
    needle = needle.lower()
    for line in lines:
        m = _PROBE_LINE.search(line) or _PROBE_LINE_NOVAL.search(line)
        if m is None:
            continue
        label = m.group("label").lower()
        if needle in label:
            return m.group("status")
    return None


def doctor_capabilities(ok: bool, lines: list[str]) -> HostCapabilities:
    """Derive host capabilities from a ``DoctorResult`` (``ok`` + ``lines``).

    ``podman_present`` is True unless the ``podman`` probe explicitly MISS'd (a
    WARN — e.g. a missing optional image — still leaves podman itself usable).
    ``peers_present`` is True unless the ``peers version`` probe MISS'd. A probe
    that is entirely absent from the output is treated as present (fail-soft:
    we do not block the operator on a doctor we couldn't parse), EXCEPT we honor
    a definitive MISS. The summary is the doctor's own ``Summary:`` line if
    present, else a short synthetic one.
    """
    podman_status = _probe_status(lines, "podman")
    peers_status = _probe_status(lines, "peers version")
    if peers_status is None:
        # older/alternate doctors may just label it "peers".
        peers_status = _probe_status(lines, "peers")
    podman_present = podman_status != "MISS"
    peers_present = peers_status != "MISS"
    summary = ""
    for line in lines:
        if line.strip().lower().startswith("summary:"):
            summary = line.strip()
            break
    if not summary:
        state = "ok" if ok else "issues"
        summary = f"doctor: {state}"
    return HostCapabilities(
        ok=ok,
        peers_present=peers_present,
        podman_present=podman_present,
        summary=summary,
    )
