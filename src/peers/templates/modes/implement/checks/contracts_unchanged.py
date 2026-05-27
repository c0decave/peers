#!/usr/bin/env python3
"""Exit 1 if frozen contracts in .peers/contracts/ have been tampered with.

Sixth hard gate for implement-mode. Frozen contracts are the
``acceptance.sh`` / optional ``e2e.sh`` scripts plus ``PLAN.original.md``
written at project init by ``peers-ctl new --modes=implement``. Their
sha256 fingerprints live in ``<project>/.peers/contracts.sha`` and are
the substrate's tamper-evident anchor: a peer rewriting the acceptance
script (to make ``acceptance-pass`` trivially green) or silently
mutating ``PLAN.original.md`` (to evade ``plan-original-preserved``)
will be caught here.

Pass (exit 0) when ``verify_contracts`` succeeds — every pinned file is
present and its sha256 matches its recorded value.

Fail (exit 1) on any :class:`ContractsMismatch`: missing
``contracts.sha``, malformed JSON in the pin file, an unknown logical
key, a missing pinned file, or a content sha mismatch. The exception's
message is surfaced verbatim in the FAIL line so the operator can see
exactly which file (and which class of failure) tripped the gate.

This is a thin wrapper around :func:`peers_ctl.contracts.verify_contracts`;
all integrity logic (including which keys are valid, where each filename
maps on disk, and the JSON shape contract) lives there.
"""
from __future__ import annotations

import sys
from pathlib import Path

from peers_ctl.contracts import ContractsMismatch, verify_contracts


def main(project_dir: str = ".") -> int:
    plan_dir = Path(project_dir) / ".peers"
    try:
        verify_contracts(plan_dir)
    except ContractsMismatch as e:
        print(f"contracts-unchanged FAIL: {e}")
        return 1
    print("contracts-unchanged: clean")
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
