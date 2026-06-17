"""Konsens / Review panel: commit cards with a substrate attestation badge.

Pure renderer over ``list[CommitReviewRow]`` (Wave-1a ``reader.commit_review_view``).
Holds no file I/O. Each card shows the commit subject, the (FORGEABLE) ``Peer:``
trailer, and an attestation badge derived from the substrate-attested note:

* ✓ match  — a peers-attest note EXISTS and equals the trailer (green, trustworthy).
* ⚠ MISMATCH — a note exists but DISAGREES with the trailer (red: the forgery
  signal). This is the load-bearing case the badge exists to surface.
* ·  none   — no attestation present (dim). Absence is NOT a forgery alarm — an
  un-attested commit simply carries no substrate identity yet.

(Soft-review consensus *history* is a later add — the Wave-1a reader exposes the
per-commit attestation, not the running soft-gate consensus tally.)
"""
from __future__ import annotations

from textual.containers import VerticalScroll
from textual.widgets import Label, Static

from peers_ctl.tui.snapshots import CommitReviewRow


def attestation_badge(row: CommitReviewRow) -> tuple[str, str]:
    """Return ``(glyph_text, state_class)`` for a commit's attestation badge.

    Mismatch (a present attestation that disagrees with the trailer) is the
    forgery signal and reads RED; a clean match reads green; absence is dim."""
    attested = getattr(row, "attested_peer", None)
    match = bool(getattr(row, "attest_match", False))
    trailer = getattr(row, "trailer_peer", None)
    if match:
        return (f"✓ attested:{attested}", "state-pass")
    if attested is not None:
        # a note exists but != the trailer -> forgery signal.
        return (f"⚠ MISMATCH note:{attested} vs trailer:{trailer}", "state-fail")
    return ("· no attestation", "state-dim")


class ReviewPanel(Static):
    """The Konsens / Review cockpit panel."""

    can_focus = True  # so the panel can be focused + popped out as a Window.

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = "Konsens / Review"

    def compose(self):
        yield Static("", id="review-header", classes="title-row")
        yield VerticalScroll(id="review-body")

    def render_reviews(self, rows: list[CommitReviewRow]) -> None:
        """Rebuild the review cards (pure: no I/O). Safe on None/garbage."""
        rows = [r for r in (rows or []) if isinstance(r, CommitReviewRow)]
        try:
            header = self.query_one("#review-header", Static)
            body = self.query_one("#review-body", VerticalScroll)
        except Exception:
            return
        mismatches = sum(
            1 for r in rows
            if getattr(r, "attested_peer", None) is not None
            and not getattr(r, "attest_match", False)
        )
        header.remove_class("state-alert")
        head = f"{len(rows)} commits"
        if mismatches:
            head += f"  ·  {mismatches} ⚠ forgery"
            header.add_class("state-alert")
        header.update(head)
        body.remove_children()
        if not rows:
            body.mount(Label(
                "no commits to review — repo empty or unreadable",
                classes="empty-state"))
            return
        for r in rows:
            badge_text, badge_cls = attestation_badge(r)
            subject = str(getattr(r, "subject", "") or "")
            sha = str(getattr(r, "sha", "") or "")[:8]
            trailer = getattr(r, "trailer_peer", None)
            line1 = Label(f"{sha}  {subject}")
            line1.add_class("review-subject")
            peer_str = f"Peer: {trailer}" if trailer else "Peer: (none)"
            line2 = Label(f"  {peer_str}", classes="muted")
            badge = Label(f"  {badge_text}")
            badge.add_class("review-badge")
            badge.add_class(badge_cls)
            body.mount(line1)
            body.mount(line2)
            body.mount(badge)
