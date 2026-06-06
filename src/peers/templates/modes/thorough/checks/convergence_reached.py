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

from peers.safe_io import read_text_no_symlink

DEFAULT_N = 3
_MAX_CONFIG_BYTES = 512 * 1024


def main(root: str = ".") -> int:
    state_path = Path(root) / ".peers" / "state.json"
    cfg_path = Path(root) / ".peers" / "config.yaml"
    if not state_path.is_file():
        print("convergence_reached: no state.json yet (no ticks ran)")
        return 1
    try:
        # BUG-102/103: read via safe_io — refuse a symlinked state.json
        # (CWE-59) and decode with replacement so non-UTF-8 bytes fail the
        # gate via JSONDecodeError instead of an uncaught UnicodeDecodeError.
        state = json.loads(read_text_no_symlink(state_path))
    except (OSError, json.JSONDecodeError) as e:
        print(f"convergence_reached FAIL: state.json unreadable: {e}")
        return 1
    n_needed = DEFAULT_N
    if cfg_path.is_file():
        try:
            cfg = yaml.safe_load(
                read_text_no_symlink(cfg_path, max_bytes=_MAX_CONFIG_BYTES)
            ) or {}
        except (OSError, yaml.YAMLError) as e:
            print(f"convergence_reached FAIL: config.yaml unreadable: {e}")
            return 1
        raw_n = (cfg.get("goals") or {}).get("convergence_n", DEFAULT_N)
        try:
            n_needed = int(raw_n)
        except (ValueError, TypeError):
            print(
                "convergence_reached FAIL: goals.convergence_n is not an "
                f"integer: {raw_n!r}"
            )
            return 1
        if n_needed < 0:
            print(
                "convergence_reached FAIL: goals.convergence_n must be "
                f">= 0 (got {raw_n!r})"
            )
            return 1
    n_have = int(state.get("consecutive_clean_ticks", 0))
    if n_have >= n_needed:
        print(f"convergence_reached: clean ({n_have}/{n_needed})")
        return 0
    print(f"convergence_reached FAIL: {n_have}/{n_needed} consecutive clean ticks")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
