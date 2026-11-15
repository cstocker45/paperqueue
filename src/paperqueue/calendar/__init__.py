"""Calendar backends. Only Apple Calendar (Calendar.app via JXA) exists in v0.1; CalDAV is planned."""

from __future__ import annotations

import sys

from ..types import CalendarBackend
from .apple import AppleCalendar

__all__ = ["AppleCalendar", "get_backend"]


def get_backend(cfg) -> CalendarBackend | None:
    """The configured calendar backend, or None when calendar sync is disabled/unsupported."""
    cal = cfg.calendar
    if cal.backend != "apple" or sys.platform != "darwin":
        return None
    return AppleCalendar(cal.apple_calendar)
