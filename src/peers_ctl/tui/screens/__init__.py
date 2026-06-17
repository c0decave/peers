"""Modal screens for the peers-ctl TUI (Wave 1b).

Unit H ships :class:`~peers_ctl.tui.screens.help.HelpScreen`; Unit I adds the
launch :class:`~peers_ctl.tui.screens.wizard.LaunchWizardScreen` and the
:mod:`~peers_ctl.tui.screens.intervene` intervention modals.

The Textual-dependent screens (help / wizard / intervene) are imported **lazily**
via ``__getattr__`` so the *textual-free* helper modules in this package
(:mod:`wizard_support`) stay importable under the default interpreter (no
``[tui]`` extra) — the screens themselves only resolve when actually referenced
inside ``cmd_tui``/the app, which already requires textual.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

__all__ = [
    "HELP_GROUPS",
    "HelpScreen",
    "LaunchWizardScreen",
    "AckBlockScreen",
    "AmendScreen",
    "ResumeScreen",
    "StopScreen",
]

if TYPE_CHECKING:  # pragma: no cover - import-time typing only
    from peers_ctl.tui.screens.help import HELP_GROUPS, HelpScreen
    from peers_ctl.tui.screens.intervene import (
        AckBlockScreen,
        AmendScreen,
        ResumeScreen,
        StopScreen,
    )
    from peers_ctl.tui.screens.wizard import LaunchWizardScreen


#: which textual-dependent symbol lives in which submodule (lazy resolution).
_LAZY = {
    "HELP_GROUPS": "peers_ctl.tui.screens.help",
    "HelpScreen": "peers_ctl.tui.screens.help",
    "LaunchWizardScreen": "peers_ctl.tui.screens.wizard",
    "AckBlockScreen": "peers_ctl.tui.screens.intervene",
    "AmendScreen": "peers_ctl.tui.screens.intervene",
    "ResumeScreen": "peers_ctl.tui.screens.intervene",
    "StopScreen": "peers_ctl.tui.screens.intervene",
}


def __getattr__(name: str):
    """Resolve a textual-dependent screen symbol lazily (PEP 562).

    Keeps ``import peers_ctl.tui.screens.wizard_support`` (textual-free) working
    without dragging in textual; the screens import only when referenced."""
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    mod = importlib.import_module(target)
    return getattr(mod, name)
