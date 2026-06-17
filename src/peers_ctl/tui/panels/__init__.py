"""Pure-renderer TUI panels (Wave 1b). Each widget renders from a Wave-1a
snapshot/list and attaches ``.state-*`` CSS classes — never touches the
filesystem and never crashes on missing/None data."""
from __future__ import annotations

from peers_ctl.tui.panels.autonomy import (
    AutonomyFeedPanel,
    AutonomyLedgerPanel,
    EscalationBannerPanel,
    PropagationsPanel,
    SpineGatesPanel,
)
from peers_ctl.tui.panels.budget import BudgetPanel
from peers_ctl.tui.panels.bugs import BugsPanel
from peers_ctl.tui.panels.diff import DiffPanel
from peers_ctl.tui.panels.fleet import FleetPanel
from peers_ctl.tui.panels.gates import GatesPanel
from peers_ctl.tui.panels.live import LivePanel
from peers_ctl.tui.panels.log import LogPanel
from peers_ctl.tui.panels.peers import PeersPanel
from peers_ctl.tui.panels.review import ReviewPanel
from peers_ctl.tui.panels.tasks import TasksPanel
from peers_ctl.tui.panels.ticks import TicksPanel

__all__ = [
    "AutonomyFeedPanel",
    "AutonomyLedgerPanel",
    "BudgetPanel",
    "BugsPanel",
    "DiffPanel",
    "EscalationBannerPanel",
    "FleetPanel",
    "GatesPanel",
    "LivePanel",
    "LogPanel",
    "PeersPanel",
    "PropagationsPanel",
    "ReviewPanel",
    "SpineGatesPanel",
    "TasksPanel",
    "TicksPanel",
]
