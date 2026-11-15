"""Reading-slot arithmetic. Pure: no I/O except reading the system timezone name.

All datetimes going in and out are timezone-aware; results are UTC. Slots are defined in
the configured IANA timezone, so "08:30" stays 08:30 local across DST changes."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import WEEKDAYS, Config

if TYPE_CHECKING:
    from .db import Paper

Busy = list[tuple[datetime, datetime]]

_DAY_NAMES = {name: i for i, name in enumerate(WEEKDAYS)} | {
    full: i
    for i, full in enumerate(
        ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    )
}


def system_timezone() -> ZoneInfo:
    """The OS timezone from the /etc/localtime symlink, else UTC."""
    candidates = [os.environ.get("TZ", "").lstrip(":")]
    for resolve in (os.readlink, os.path.realpath):
        try:
            candidates.append(zone_from_path(resolve("/etc/localtime")))
        except OSError:
            pass
    for name in candidates:
        if not name:
            continue
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo("UTC")


def zone_from_path(path: str) -> str:
    """'/var/db/timezone/zoneinfo/Europe/Berlin' or '/usr/share/zoneinfo.default/Europe/Berlin' -> 'Europe/Berlin'."""
    m = re.search(r"/zoneinfo[^/]*/(.+)$", path)
    return m.group(1) if m else ""


def tz(cfg: Config) -> ZoneInfo:
    return ZoneInfo(cfg.schedule.timezone) if cfg.schedule.timezone else system_timezone()


def _times(cfg: Config) -> list[time]:
    return sorted(time.fromisoformat(t) for t in cfg.schedule.times)


def _local_slot(day: date, at: time, zone: ZoneInfo) -> datetime:
    """`day` at `at` in `zone`, as UTC. A wall time skipped by a DST jump lands just after it."""
    local = datetime.combine(day, at, tzinfo=zone)
    return local.astimezone(UTC)


def _day_slots(cfg: Config, day: date, zone: ZoneInfo) -> list[datetime]:
    return [_local_slot(day, t, zone) for t in _times(cfg)]


def slots(cfg: Config, start: datetime, end: datetime) -> Iterator[datetime]:
    """Configured slot start times in [start, end), ascending, as aware UTC datetimes."""
    zone = tz(cfg)
    days = {_DAY_NAMES[d] for d in cfg.schedule.days}
    day = start.astimezone(zone).date() - timedelta(days=1)
    last = end.astimezone(zone).date() + timedelta(days=1)
    while day <= last:
        if day.weekday() in days:
            for slot in _day_slots(cfg, day, zone):
                if start <= slot < end:
                    yield slot
        day += timedelta(days=1)


def _overlaps(start: datetime, minutes: int, busy: Iterable[tuple[datetime, datetime]]) -> bool:
    end = start + timedelta(minutes=minutes)
    return any(start < b_end and b_start < end for b_start, b_end in busy)


def is_free(start: datetime, minutes: int, busy: Busy) -> bool:
    return not _overlaps(start, minutes, busy)


def next_free(cfg: Config, busy: Busy, after: datetime, duration_min: int) -> datetime | None:
    """Earliest configured slot at or after `after`, within the horizon, not overlapping `busy`."""
    horizon = after + timedelta(days=cfg.schedule.horizon_days)
    for slot in slots(cfg, after, horizon):
        if not _overlaps(slot, duration_min, busy):
            return slot
    return None


def _first_free_on(cfg: Config, day: date, busy: Busy, now: datetime, duration: int) -> datetime | None:
    for slot in _day_slots(cfg, day, tz(cfg)):
        if slot >= now and not _overlaps(slot, duration, busy):
            return slot
    return None


def parse_when(
    text: str | None, cfg: Config, busy: Busy, now: datetime, duration: int | None = None
) -> datetime | None:
    """Parse a scheduling request into an aware UTC start time, or None for "unschedule".

    Accepts an ISO datetime (naive = configured timezone), YYYY-MM-DD / today / tomorrow /
    a weekday name (first free configured time that day), "next" (next free slot), or
    "none"/empty. A slot is free when `duration` minutes (default: the configured session
    length) fit. Raises ValueError when the text is not understood or no slot is free."""
    minutes = duration or cfg.schedule.duration_minutes
    s = (text or "").strip().lower()
    if s in ("", "none", "unschedule", "never"):
        return None
    zone = tz(cfg)
    today = now.astimezone(zone).date()
    if s in ("next", "next free", "next free slot", "soon", "asap"):
        slot = next_free(cfg, busy, now, minutes)
        if slot is None:
            raise ValueError(f"no free reading slot in the next {cfg.schedule.horizon_days} days")
        return slot

    day: date | None = None
    if s == "today":
        day = today
    elif s == "tomorrow":
        day = today + timedelta(days=1)
    elif s.removeprefix("next ") in _DAY_NAMES:
        weekday = _DAY_NAMES[s.removeprefix("next ")]
        for offset in range(14):
            candidate = today + timedelta(days=offset)
            if candidate.weekday() == weekday and _first_free_on(cfg, candidate, busy, now, minutes):
                day = candidate
                break
        if day is None:
            raise ValueError(f"no free reading slot on the next two {s.removeprefix('next ')}s")
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        day = date.fromisoformat(s)
    else:
        try:
            parsed = datetime.fromisoformat((text or "").strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"cannot understand when={text!r}; use an ISO date/time, today, tomorrow, "
                "a weekday, next or none"
            ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=zone)
        return parsed.astimezone(UTC)

    slot = _first_free_on(cfg, day, busy, now, minutes)
    if slot is None:
        raise ValueError(f"no free reading slot left on {day.isoformat()}")
    return slot


def plan(
    papers: Iterable[Paper],
    cfg: Config,
    busy: Busy,
    now: datetime,
    duration_of: Callable[[Paper], int] | None = None,
) -> list[tuple[int, datetime]]:
    """Assign papers, best priority and oldest first, to successive free slots.
    A paper's session lasts its `duration_minutes`, else `duration_of(paper)`, else the
    configured length. Papers that do not fit in the horizon are left out of the result."""
    taken = list(busy)
    out: list[tuple[int, datetime]] = []
    ordered = sorted(papers, key=lambda p: (p.priority, p.added_at, p.id or 0))
    for paper in ordered:
        if paper.id is None:
            continue
        duration = paper.duration_minutes or (
            duration_of(paper) if duration_of else cfg.schedule.duration_minutes
        )
        slot = next_free(cfg, taken, now, duration)
        if slot is None:
            continue
        taken.append((slot, slot + timedelta(minutes=duration)))
        out.append((paper.id, slot))
    return out
