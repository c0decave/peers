"""The loop-agnostic governance spine (Agentic-OS Stage 0).

A thin, additive layer on the existing peers kernel: a hash-chained,
substrate-attested ``RunLedger``; op-config intake; a ``ModeRun`` +
``ModeFrontend`` seam with a ``drive`` loop; a stop-on-dry counter; a
reusable N-vote adversarial-verify gate; a minimal direction/bar detector;
and a fail-closed gate evaluator. See ``docs/agentic-os/02-the-spine.md``
and ``docs/plans/2026-06-10-agentic-os-stage-0.md``.

No mode logic lives here — modes (develop / find-bugs / research) plug in
behind this seam in Stage 1+.
"""
