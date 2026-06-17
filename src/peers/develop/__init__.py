"""Agentic-OS Stage 1 — ``develop`` mode (the first ModeFrontend).

``develop`` discovers its own work over a target tool: AUDIT → adversarial
VERIFY → AUTHOR a frozen implement contract → IMPLEMENT → re-AUDIT, driven by
the Stage-0 spine's :func:`peers.spine.mode_run.drive` with
``landing=branch-pr`` and stop-on-dry. The capability work (the real auditor,
the real implement convergence) is reached through injected **ports**
(Protocols) so the orchestration is deterministically testable with fakes.
"""
