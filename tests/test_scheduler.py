from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from paperqueue import scheduler
from paperqueue.config import Config
from paperqueue.db import Paper

from .conftest import NOW


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def every_day(cfg: Config, tz: str, times: list[str] | None = None) -> Config:
    cfg.schedule.timezone = tz
    cfg.schedule.days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    cfg.schedule.times = times or ["08:30"]
    return cfg


def test_tz(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    assert scheduler.tz(cfg) == ZoneInfo("Europe/Berlin")
    cfg.schedule.timezone = ""
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(scheduler.os, "readlink", lambda _p: "/var/db/timezone/zoneinfo/Asia/Tokyo")
    assert scheduler.tz(cfg) == ZoneInfo("Asia/Tokyo")
    monkeypatch.setattr(scheduler.os, "readlink", lambda _p: "/etc/localtime")
    monkeypatch.setattr(scheduler.os.path, "realpath", lambda _p: "/usr/share/zoneinfo.default/Asia/Tokyo")
    assert scheduler.tz(cfg) == ZoneInfo("Asia/Tokyo")
    monkeypatch.setattr(scheduler.os.path, "realpath", lambda _p: "/etc/localtime")
    assert scheduler.tz(cfg) == ZoneInfo("UTC")
    monkeypatch.setenv("TZ", "America/New_York")
    assert scheduler.tz(cfg) == ZoneInfo("America/New_York")


def test_slots_weekdays_only(cfg: Config) -> None:
    # Fri 20 Nov .. Tue 24 Nov 2026, Berlin (UTC+1): weekend skipped
    got = list(scheduler.slots(cfg, utc(2026, 11, 20), utc(2026, 11, 25)))
    assert got == [utc(2026, 11, 20, 7, 30), utc(2026, 11, 23, 7, 30), utc(2026, 11, 24, 7, 30)]
    assert all(s.tzinfo == UTC for s in got)


@pytest.mark.parametrize(
    ("tz", "start", "expected_utc_hours"),
    [
        # Berlin springs forward Sun 29 Mar 2026: 08:30 is 07:30Z before, 06:30Z after.
        ("Europe/Berlin", utc(2026, 3, 27), [7, 7, 6, 6]),
        # Berlin falls back Sun 25 Oct 2026.
        ("Europe/Berlin", utc(2026, 10, 23), [6, 6, 7, 7]),
        # New York springs forward Sun 8 Mar 2026: 13:30Z (EST) -> 12:30Z (EDT).
        ("America/New_York", utc(2026, 3, 6, 12), [13, 13, 12, 12]),
        # New York falls back Sun 1 Nov 2026.
        ("America/New_York", utc(2026, 10, 30, 12), [12, 12, 13, 13]),
    ],
)
def test_slots_across_dst(cfg: Config, tz: str, start: datetime, expected_utc_hours: list[int]) -> None:
    every_day(cfg, tz)
    got = list(scheduler.slots(cfg, start, start + timedelta(days=4)))
    assert [s.hour for s in got] == expected_utc_hours
    assert {s.astimezone(ZoneInfo(tz)).strftime("%H:%M") for s in got} == {"08:30"}


def test_slot_in_dst_gap(cfg: Config) -> None:
    every_day(cfg, "Europe/Berlin", ["02:30"])
    (slot,) = scheduler.slots(cfg, utc(2026, 3, 29), utc(2026, 3, 29, 12))
    assert slot == utc(2026, 3, 29, 1, 30)  # 03:30 CEST: the skipped wall time moves forward


def test_next_free(cfg: Config) -> None:
    cfg.schedule.times = ["08:30", "18:00"]
    first = utc(2026, 11, 16, 7, 30)
    assert scheduler.next_free(cfg, [], NOW, 45) == first
    busy = [(first + timedelta(minutes=30), first + timedelta(hours=1))]  # overlaps the end
    assert scheduler.next_free(cfg, busy, NOW, 45) == utc(2026, 11, 16, 17, 0)
    assert scheduler.next_free(cfg, busy, NOW, 30) == first  # back-to-back is fine
    cfg.schedule.horizon_days = 1
    assert scheduler.next_free(cfg, [(NOW, NOW + timedelta(days=2))], NOW, 45) is None


def test_parse_when(cfg: Config) -> None:
    busy: scheduler.Busy = []
    assert scheduler.parse_when(None, cfg, busy, NOW) is None
    assert scheduler.parse_when(" None ", cfg, busy, NOW) is None
    assert scheduler.parse_when("next", cfg, busy, NOW) == utc(2026, 11, 16, 7, 30)
    assert scheduler.parse_when("today", cfg, busy, NOW) == utc(2026, 11, 16, 7, 30)
    assert scheduler.parse_when("tomorrow", cfg, busy, NOW) == utc(2026, 11, 17, 7, 30)
    assert scheduler.parse_when("Thursday", cfg, busy, NOW) == utc(2026, 11, 19, 7, 30)
    assert scheduler.parse_when("sat", cfg, busy, NOW) == utc(2026, 11, 21, 7, 30)  # explicit day: any weekday
    assert scheduler.parse_when("2026-12-01", cfg, busy, NOW) == utc(2026, 12, 1, 7, 30)
    assert scheduler.parse_when("2026-12-01T19:15", cfg, busy, NOW) == utc(2026, 12, 1, 18, 15)
    assert scheduler.parse_when("2026-12-01T19:15Z", cfg, busy, NOW) == utc(2026, 12, 1, 19, 15)
    assert scheduler.parse_when("2026-12-01T19:15-05:00", cfg, busy, NOW) == utc(2026, 12, 2, 0, 15)
    later = NOW + timedelta(hours=3)
    with pytest.raises(ValueError, match="no free"):
        scheduler.parse_when("today", cfg, busy, later)
    assert scheduler.parse_when("monday", cfg, busy, later) == utc(2026, 11, 23, 7, 30)
    with pytest.raises(ValueError, match="cannot understand"):
        scheduler.parse_when("whenever", cfg, busy, NOW)
    with pytest.raises(ValueError):
        scheduler.parse_when("2026-02-30", cfg, busy, NOW)
    cfg.schedule.horizon_days = 1
    full = [(NOW, NOW + timedelta(days=3))]
    with pytest.raises(ValueError, match="no free reading slot in the next 1 days"):
        scheduler.parse_when("next", cfg, full, NOW)


def test_plan(cfg: Config) -> None:
    papers = [
        Paper(id=1, title="low", priority=3, added_at="2026-01-01T00:00:00+00:00"),
        Paper(id=2, title="high new", priority=1, added_at="2026-01-03T00:00:00+00:00"),
        Paper(id=3, title="high old", priority=1, added_at="2026-01-02T00:00:00+00:00"),
    ]
    busy = [(utc(2026, 11, 16, 7, 30), utc(2026, 11, 16, 8, 15))]
    assert scheduler.plan(papers, cfg, busy, NOW) == [
        (3, utc(2026, 11, 17, 7, 30)),
        (2, utc(2026, 11, 18, 7, 30)),
        (1, utc(2026, 11, 19, 7, 30)),
    ]
    cfg.schedule.horizon_days = 2
    assert [pid for pid, _ in scheduler.plan(papers, cfg, busy, NOW)] == [3]


def test_zone_from_path_variants():
    from paperqueue.scheduler import zone_from_path

    assert zone_from_path("/var/db/timezone/zoneinfo/Europe/Berlin") == "Europe/Berlin"
    assert zone_from_path("/usr/share/zoneinfo.default/America/New_York") == "America/New_York"
    assert zone_from_path("/etc/localtime") == ""


def test_durations(cfg: Config) -> None:
    busy = [(utc(2026, 11, 16, 9, 0), utc(2026, 11, 16, 10, 0))]  # 10:00-11:00 Berlin
    cfg.schedule.times = ["08:30", "11:00"]
    assert scheduler.parse_when("today", cfg, busy, NOW) == utc(2026, 11, 16, 7, 30)
    assert scheduler.parse_when("today", cfg, busy, NOW, duration=90) == utc(2026, 11, 16, 7, 30)  # ends 10:00
    # 120 minutes from 08:30 run into the busy hour, so the 11:00 slot is the first that fits
    assert scheduler.parse_when("today", cfg, busy, NOW, duration=120) == utc(2026, 11, 16, 10, 0)
    assert scheduler.parse_when("next", cfg, busy, NOW, duration=120) == utc(2026, 11, 16, 10, 0)

    papers = [Paper(id=1, title="long", priority=1), Paper(id=2, title="short", priority=2)]
    long_first = scheduler.plan(papers, cfg, [], NOW, duration_of=lambda p: 150 if p.id == 1 else 45)
    # the 150-minute session (08:30-11:00) leaves 11:00 free for the next one
    assert long_first == [(1, utc(2026, 11, 16, 7, 30)), (2, utc(2026, 11, 16, 10, 0))]
    clash = scheduler.plan(papers, cfg, [], NOW, duration_of=lambda p: 160 if p.id == 1 else 45)
    assert clash[1] == (2, utc(2026, 11, 17, 7, 30))
