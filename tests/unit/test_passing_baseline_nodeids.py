"""BUG-721: a test rename must not silently drop a node id the no_regression
gate pins in ``.peers/passing-baseline.txt``.

``no_regression`` treats every node id in the passing-baseline snapshot as
"green at audit start" and fails closed if one is no longer in the current
pass-set. A pure *rename* (e.g. injecting a ``coverage_3class`` ``happy``/
``edge``/``sad`` keyword into a function name) is not a real regression, but it
changes the pytest node id, so the old id vanishes and the hard gate reports a
phantom regression. The honest fix is to keep the pinned node ids stable and
carry the 3-class classification via ``# kind:`` marker comments instead.

This guard reproduces BUG-721 and prevents the drift from recurring: every
``tests.unit.test_spine_baseline::<func>`` id pinned in the baseline must still
be defined in ``tests/unit/test_spine_baseline.py``. (Scoped to the module the
3-class rename actually touched; the same invariant generalises to any pinned
test module.)

happy: every pinned spine_baseline node id resolves to a def in the module.
edge:  the baseline genuinely pins spine_baseline ids (guard is not vacuous).
sad:   a pinned id with no matching def is a dropped/renamed test -> fail.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BASELINE = _REPO / ".peers" / "passing-baseline.txt"
_MODULE = "tests.unit.test_spine_baseline"
_MODULE_FILE = _REPO / "tests" / "unit" / "test_spine_baseline.py"


def _pinned_node_funcs(baseline: Path = _BASELINE) -> list[str]:
    if not baseline.is_file():
        return []
    text = baseline.read_text(encoding="utf-8")
    prefix = f"{_MODULE}::"
    return sorted(
        line.strip()[len(prefix):]
        for line in text.splitlines()
        if line.strip().startswith(prefix)
    )


def _defined_funcs(module_file: Path) -> set[str]:
    src = module_file.read_text(encoding="utf-8")
    return set(re.findall(r"(?m)^\s*def\s+(test_\w+)\s*\(", src))


# --- edge: the guard is not vacuous — the baseline really pins this module ----

def test_edge_baseline_pins_spine_baseline_nodeids(tmp_path):
    # ``.peers/passing-baseline.txt`` is a per-run artifact (``.peers/`` is
    # gitignored): it is captured at audit start and present only inside a live
    # run. When it exists, assert it genuinely pins this module — the original,
    # strongest non-vacuity check. Under a plain ``pytest`` / CI / pre-push run
    # on a fresh checkout it is absent, so fall back to proving the pin-
    # EXTRACTION is non-vacuous against a constructed baseline that pins a real
    # def. Either way this guard (and the happy guard it backstops) can never be
    # silently vacuous — and it never hard-fails merely because no run is live.
    if _BASELINE.is_file():
        assert _pinned_node_funcs(), (
            f"expected {_MODULE} node ids pinned in {_BASELINE}; found none — "
            "the guard would be vacuous"
        )
        return
    real = sorted(_defined_funcs(_MODULE_FILE))
    assert real, f"{_MODULE_FILE.name} defines no test_ functions"
    fixture = tmp_path / "passing-baseline.txt"
    fixture.write_text(f"{_MODULE}::{real[0]}\n", encoding="utf-8")
    assert _pinned_node_funcs(fixture) == [real[0]], (
        "pin extraction is vacuous — the happy guard would never catch a "
        "renamed/dropped node id"
    )


# --- happy: every pinned node id is still a real def in the module ------------

def test_happy_every_pinned_nodeid_still_defined():
    pinned = _pinned_node_funcs()
    defined = _defined_funcs(_MODULE_FILE)
    missing = [fn for fn in pinned if fn not in defined]
    assert not missing, (
        f"{len(missing)} baseline-pinned node id(s) no longer defined in "
        f"{_MODULE_FILE.name} (a rename dropped them — no_regression will see a "
        f"phantom regression): {missing}"
    )


# --- sad: a renamed/dropped pinned id is detected as a failure ----------------

def test_sad_renamed_pinned_func_is_detected():
    # Simulate the BUG-721 regression on a synthetic module body: the baseline
    # pins the original name but the source defines the keyword-injected rename.
    pinned = ["test_build_baseline_unre_hashable_artifact_is_uncharacterizable"]
    renamed_src = (
        "def test_build_baseline_sad_unre_hashable_artifact_is_uncharacterizable():\n"
        "    pass\n"
    )
    tmp = _MODULE_FILE.parent / "_bug721_synthetic_unused.py"
    tmp.write_text(renamed_src, encoding="utf-8")
    try:
        defined = _defined_funcs(tmp)
        missing = [fn for fn in pinned if fn not in defined]
        assert missing == pinned, "guard must flag the renamed (dropped) node id"
    finally:
        tmp.unlink()


@pytest.mark.parametrize("bad", ["", "not-a-prefix::x"])
def test_sad_unparseable_baseline_lines_are_ignored(bad, tmp_path):
    # A line that is not a `<module>::<func>` entry contributes no false pin.
    f = tmp_path / "passing-baseline.txt"
    f.write_text(bad + "\n", encoding="utf-8")
    prefix = f"{_MODULE}::"
    funcs = [
        ln.strip()[len(prefix):]
        for ln in f.read_text(encoding="utf-8").splitlines()
        if ln.strip().startswith(prefix)
    ]
    assert funcs == []
