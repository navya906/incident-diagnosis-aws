"""Investigation windows anchored at the alarm time."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from app.contracts.correlation import InvestigationWindow
from app.contracts.events import CanonicalEvent

#: Window sizes used in sweeps (BRIEF RQ6).
STANDARD_WINDOWS: tuple[int, ...] = (5, 15, 30, 60)


def investigation_window(
    anchor: datetime, minutes_before: int, minutes_after: int = 10
) -> InvestigationWindow:
    """[anchor - minutes_before, anchor + minutes_after]. Causes precede the alarm, so the
    window size is the look-back; a short fixed tail keeps effects that trail the alarm."""
    if minutes_before < 0 or minutes_after < 0:
        raise ValueError("window minutes must be >= 0")
    return InvestigationWindow(
        anchor=anchor,
        minutes_before=minutes_before,
        minutes_after=minutes_after,
        start=anchor - timedelta(minutes=minutes_before),
        end=anchor + timedelta(minutes=minutes_after),
    )


def events_in_window(
    events: Iterable[CanonicalEvent], window: InvestigationWindow
) -> list[CanonicalEvent]:
    return sorted(
        (e for e in events if window.contains(e.timestamp)), key=lambda e: (e.timestamp, e.event_id)
    )
