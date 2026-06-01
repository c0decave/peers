#!/usr/bin/env python3
"""Fail if a test that was green at audit start is now red."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path


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
        passing = collect_passing()
        if passing is None:
            print("no_regression: cannot snapshot — pytest did not run cleanly")
            return 1
        BASELINE.write_text("\n".join(sorted(passing)) + "\n")
        print(f"no_regression: snapshot saved to {BASELINE} ({len(passing)} tests)")
        return 0
    if not BASELINE.exists():
        print(f"no_regression: missing {BASELINE}; run once with --snapshot")
        return 1
    expected = set(BASELINE.read_text().splitlines()) - {""}
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
