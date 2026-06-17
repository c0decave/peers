"""bring-up mode — corpus-driven observe-and-harden meta-harness.

The bring-up mode hands an external tool-under-test a task defined by an imported
test-corpus, observes its execution, and runs a file -> analyse -> fix -> land
loop until the whole corpus runs clean. See
``docs/plans/2026-06-12-bring-up-mode-design.md``.

Phase 0 ships the run-intake primitives only: the ``bring-up`` op-config label,
the :class:`~peers.modes.bring_up.models.Case` corpus unit, and the
:class:`~peers.modes.bring_up.manifest.BringUpManifest` schema.
"""
