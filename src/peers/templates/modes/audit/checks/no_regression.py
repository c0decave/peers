#!/usr/bin/env python3
"""Fail if a test that was green at audit start is now red."""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from peers.safe_io import (
    atomic_write_text_in_dir_no_symlink,
    read_text_no_symlink,
)


def _refuse_if_linked(path: Path) -> str | None:
    """Return an error message if ``path`` exists as a symlink or hardlink.

    Returns ``None`` when the path is missing or a plain regular file.
    Used pre-write so --snapshot does not atomically destroy a pre-planted
    symlink the way ``atomic_write_text_in_dir_no_symlink`` otherwise would.

    BUG-177: leaf lstat alone misses a symlinked PARENT component (e.g. a
    pre-planted ``.peers/`` directory symlink) — the leaf resolves through
    it to a regular file at the real location. Walk the ancestor chain and
    refuse any intermediate symlink (mirrors the no-follow ancestor guard
    in safe_io / HybridCommLayer, BUG-175/185). Shadowed by the driver's
    _verify_peer_dir_identity in the loop, but standalone gate-script
    invocations are not otherwise protected.
    """
    for _parent in path.parents:
        if _parent == _parent.parent:  # stop at '.' (cwd) / '/' (fs root)
            break
        try:
            _pst = os.lstat(_parent)
        except FileNotFoundError:
            continue
        except OSError as e:
            return f"cannot stat {_parent}: {e}"
        if stat.S_ISLNK(_pst.st_mode):
            return f"refusing symlinked parent: {_parent}"
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        return f"cannot stat {path}: {e}"
    if stat.S_ISLNK(st.st_mode):
        return f"refusing symlinked leaf: {path}"
    if stat.S_ISREG(st.st_mode) and st.st_nlink != 1:
        return f"refusing hard-linked leaf: {path}"
    return None


BASELINE = Path(".peers/passing-baseline.txt")

# BUG-006 defense-in-depth (resource-cap + audit-log layer): a single
# flake-tolerated test is the common case. But when MANY baseline-
# green tests "flake" on the first run and all pass on retry, the
# uniform-flake explanation is suspicious — more likely a load spike,
# a shared-state race, or an actual intermittent regression masked as
# noise. We still don't strict-fail (the retry was clean and we can't
# prove regression), but the substrate log gets a clear FLAKE STORM
# audit line so operators can graph it over time and intervene before
# the intermittent issue surfaces as a hard regression.
FLAKE_STORM_THRESHOLD = 5


def collect_passing() -> set[str] | None:
    """Run pytest, return the set of passing testcase ids, or ``None`` when
    pytest could not run / produce parseable JUnit XML.

    Returning ``None`` (instead of crashing) is important because the
    no-prior-regression gate is a hard gate — a ParseError here used to
    take down the whole goal evaluation cycle.
    """
    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tf:
        xml_path = tf.name
    proc = subprocess.run(
        ["python3", "-m", "pytest", "-q", "--no-header", f"--junitxml={xml_path}", "--tb=no"],
        capture_output=True, text=True, check=False,
    )
    try:
        xml_file = Path(xml_path)
        if not xml_file.exists() or xml_file.stat().st_size == 0:
            sys.stderr.write(
                "no_regression: pytest produced no JUnit XML "
                f"(exit={proc.returncode}); stderr tail: "
                f"{(proc.stderr or '')[-400:]}\n"
            )
            return None
        try:
            # XML is generated locally by pytest in this process, not
            # supplied by an external actor.
            tree = ET.parse(xml_path)  # nosec B314
        except ET.ParseError as exc:
            sys.stderr.write(
                f"no_regression: junit XML at {xml_path} is unparseable ({exc}); "
                f"pytest exit={proc.returncode}; stderr tail: "
                f"{(proc.stderr or '')[-400:]}\n"
            )
            return None
    finally:
        Path(xml_path).unlink(missing_ok=True)
    passing: set[str] = set()
    for tc in tree.getroot().iter("testcase"):
        if any(child.tag in ("failure", "error", "skipped") for child in tc):
            continue
        cls = tc.attrib.get("classname", "")
        name = tc.attrib.get("name", "")
        passing.add(f"{cls}::{name}" if cls else name)
    return passing


def collect_passing_with_retry(attempts: int = 2) -> set[str] | None:
    """`collect_passing`, retried up to `attempts` times.

    A single empty/unparseable JUnit XML is usually a transient infra hiccup
    (load spike, a test racing the gate's wall-clock timeout, a momentary
    spawn refusal), not a real signal. Retrying once absorbs that so the gate
    does not accumulate the convergence-wall stuck-counter on noise. Returns
    the first parseable result, or None only if EVERY attempt failed to
    produce XML (then the caller fails closed — see calc v2 diagnostic).
    """
    for _ in range(max(1, attempts)):
        result = collect_passing()
        if result is not None:
            return result
    return None


def main() -> int:
    if "--snapshot" in sys.argv:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        # refuse a symlinked or hard-linked leaf before writing.
        # atomic_write_text_in_dir_no_symlink alone would just replace the
        # symlink with a regular file (safe — does not clobber the target)
        # but a pre-planted symlink is itself a tamper signal we want
        # surfaced to the operator, not silently overwritten.
        err = _refuse_if_linked(BASELINE)
        if err:
            print(f"no_regression FAIL: {err}")
            return 1
        passing = collect_passing()
        if passing is None:
            print("no_regression: cannot snapshot — pytest did not run cleanly")
            return 1
        try:
            atomic_write_text_in_dir_no_symlink(
                BASELINE, "\n".join(sorted(passing)) + "\n",
            )
        except OSError as e:
            print(f"no_regression: refusing snapshot of {BASELINE}: {e}")
            return 1
        print(f"no_regression: snapshot saved to {BASELINE} ({len(passing)} tests)")
        return 0
    # use no-follow read so a symlinked baseline does not become
    # an attacker-controlled comparison set. O_NOFOLLOW raises ELOOP on
    # the symlink open itself, surfacing as OSError to the caller.
    err = _refuse_if_linked(BASELINE)
    if err:
        print(f"no_regression FAIL: {err}")
        return 1
    try:
        baseline_text = read_text_no_symlink(BASELINE)
    except FileNotFoundError:
        print(f"no_regression: missing {BASELINE}; run once with --snapshot")
        return 1
    except OSError as e:
        print(f"no_regression: refusing to read {BASELINE}: {e}")
        return 1
    expected = set(baseline_text.splitlines()) - {""}
    if not expected:
        # Fix A (calc v2): an empty baseline (0 tests green at run start, e.g.
        # a greenfield build from zero) has nothing that CAN regress. Pass
        # WITHOUT running pytest at all, so the gate is never exposed to the
        # no-XML / gate-timeout failure mode for zero benefit.
        print(
            "no_regression: empty baseline (0 tests green at run start) — "
            "nothing to regress against; clean"
        )
        return 0
    current = collect_passing_with_retry()
    if current is None:
        # Fix B (safe variant): retried and still no parseable XML. This is an
        # INFRASTRUCTURE failure (pytest could not run / a test hangs past the
        # gate timeout / spawn refused), not proof of a regression — but we
        # fail closed because we genuinely cannot measure, and passing here
        # could mask a full-suite test that the acceptance subset never runs.
        print(
            "no_regression FAIL: INFRA — pytest produced no parseable JUnit "
            "XML after retries; cannot measure regression (failing closed). "
            "Likely a missing pytest, a sandbox spawn refusal, or a test that "
            "hangs/exceeds the per-gate timeout."
        )
        return 1
    regressed = expected - current
    if not regressed:
        print(f"no_regression: clean ({len(expected)} baseline-green still green)")
        return 0
    # a single flake (timing, system load, fd-count noise) used
    # to fail the hard gate immediately. Re-run pytest once and confirm
    # the same tests regress on both runs before failing closed.
    print(
        f"no_regression: {len(regressed)} apparent regressions on first run; "
        "re-running pytest once to rule out flakes...",
        flush=True,
    )
    second = collect_passing_with_retry()
    if second is None:
        print(
            "no_regression FAIL: INFRA — confirm-run produced no parseable "
            "JUnit XML after retries; failing closed (cannot tell a flake from "
            "a real regression)"
        )
        return 1
    persistent = expected - second
    confirmed = regressed & persistent
    if confirmed:
        print(
            f"no_regression FAIL: {len(confirmed)} previously-green tests "
            "are red on BOTH runs (retry did not mask real regression):"
        )
        for nodeid in sorted(confirmed)[:30]:
            print(f"  {nodeid}")
        return 1
    flaked = regressed - persistent
    print(
        f"no_regression: flake-tolerated — {len(flaked)} test(s) flaked once "
        f"but passed on retry; {len(expected)} baseline-green still green:"
    )
    for nodeid in sorted(flaked)[:30]:
        print(f"  flake: {nodeid}")
    if len(flaked) >= FLAKE_STORM_THRESHOLD:
        # Defense-in-depth audit signal: many uniform "flakes" on a
        # single tick is the canary for a load spike, shared-state
        # race, or an intermittent regression masked as noise. We
        # still pass (the retry was clean — strict-failing here
        # would re-introduce the flake-flicker BUG-006 fixed), but
        # operators can grep `FLAKE STORM` in the substrate log and
        # graph the rate over time.
        print(
            f"no_regression: FLAKE STORM — {len(flaked)} flakes in a single "
            f"tick exceeds threshold {FLAKE_STORM_THRESHOLD}; investigate "
            "load spikes, shared-state races, or intermittent regressions"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
