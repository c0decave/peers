#!/usr/bin/env python3
"""Exit 0 if state.consecutive_clean_ticks >= N (default 3, override
via .peers/config.yaml -> goals.convergence_n).

Clean tick = tick where no Bug-Report at severity crit/high/med was
filed AND no `weak-fix:` or `shallow-fix:` flag-bug was filed. Info/
low severity bugs do NOT reset the counter (otherwise the loop runs
forever on "info: missing docstring").

Classification (what counts as "blocking") is performed by
`peers.bug_hunt.count_new_blocking_or_flag_bug_reports`; this
script only reads the resulting counter.

This script is a HARD gate, so on any unreadable/malformed input it
fails CLOSED: print a single-line `convergence_reached FAIL: ...`
diagnostic and return 1 rather than crashing the loop or silently
falling back to defaults the operator did not intend."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml


# When this template script is executed directly from a source checkout,
# prefer the checkout package over any older globally installed `peers`.
for _parent in Path(__file__).resolve().parents:
    if (_parent / "peers" / "__init__.py").is_file():
        sys.path.insert(0, str(_parent))
        break

from peers.safe_io import read_text_under_root_no_follow  # noqa: E402

DEFAULT_N = 3
_MAX_CONFIG_BYTES = 512 * 1024


def _read_non_negative_int(value: object, label: str) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        print(
            f"convergence_reached FAIL: {label} must be a non-negative "
            f"integer: {value!r}"
        )
        return None
    return value


def main(root: str = ".") -> int:
    root_path = Path(root)
    try:
        # BUG-102/103/257: read via safe_io — refuse a symlinked state.json
        # (CWE-59) and fail closed on invalid UTF-8 before JSON parsing.
        # walk every component under <root> with O_DIRECTORY|
        # O_NOFOLLOW so a symlinked ``.peers`` ancestor (BUG-185 family) is
        # rejected too — leaf-only O_NOFOLLOW let a same-UID peer redirect
        # the gate to attacker-staged state.json by swapping the .peers
        # directory itself.
        raw_state = read_text_under_root_no_follow(
            root_path, (".peers", "state.json"),
        )
    except FileNotFoundError:
        print("convergence_reached: no state.json yet (no ticks ran)")
        return 1
    except (OSError, ValueError) as e:
        print(f"convergence_reached FAIL: state.json unreadable: {e}")
        return 1
    try:
        state = json.loads(raw_state)
    except json.JSONDecodeError as e:
        print(f"convergence_reached FAIL: state.json unreadable: {e}")
        return 1
    if not isinstance(state, dict):
        print(
            "convergence_reached FAIL: state.json root is not a mapping "
            f"(got {type(state).__name__})"
        )
        return 1
    n_needed = DEFAULT_N
    try:
        cfg_text = read_text_under_root_no_follow(
            root_path, (".peers", "config.yaml"),
            max_bytes=_MAX_CONFIG_BYTES,
        )
    except FileNotFoundError:
        cfg_text = None
    except (OSError, ValueError) as e:
        print(f"convergence_reached FAIL: config.yaml unreadable: {e}")
        return 1
    if cfg_text is not None:
        try:
            cfg = yaml.safe_load(cfg_text) or {}
        except yaml.YAMLError as e:
            print(f"convergence_reached FAIL: config.yaml unreadable: {e}")
            return 1
        if cfg is None:
            cfg = {}
        if not isinstance(cfg, dict):
            print(
                "convergence_reached FAIL: config.yaml root is not a "
                f"mapping (got {type(cfg).__name__})"
            )
            return 1
        goals = cfg.get("goals", {})
        if goals is None:
            goals = {}
        if not isinstance(goals, dict):
            print(
                "convergence_reached FAIL: goals config is not a mapping "
                f"(got {type(goals).__name__})"
            )
            return 1
        raw_n = goals.get("convergence_n", DEFAULT_N)
        parsed_n = _read_non_negative_int(raw_n, "goals.convergence_n")
        if parsed_n is None:
            return 1
        n_needed = parsed_n
    parsed_have = _read_non_negative_int(
        state.get("consecutive_clean_ticks", 0),
        "state.consecutive_clean_ticks",
    )
    if parsed_have is None:
        return 1
    n_have = parsed_have
    if n_have >= n_needed:
        print(f"convergence_reached: clean ({n_have}/{n_needed})")
        return 0
    print(f"convergence_reached FAIL: {n_have}/{n_needed} consecutive clean ticks")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
