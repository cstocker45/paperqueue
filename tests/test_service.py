from __future__ import annotations

import fcntl
import json
import os
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from paperqueue import service
from paperqueue.config import Config
from paperqueue.db import Paper
from paperqueue.metadata import MetadataError, PaperMeta, Resolver
from paperqueue.service import PaperQueue, event_spec, parse_priority
from paperqueue.types import LibraryItem, event_marker

from .conftest import (
    ARXIV_NOT_FOUND,
    VIDEO_ID,
    Clock,
    FakeCalendar,
    FakeLibrary,
    FakeYtDlp,
    Router,
    arxiv_atom,
    crossref_work,
    ytdlp_info,
)

PDF = b"%PDF-1.7\n" + b"0" * 500
MON = "2026-11-16T07:30:00+00:00"  # 08:30 Berlin, the first free slot after NOW
TUE = "2026-11-17T07:30:00+00:00"


def arxiv_api(router: Router, titles: dict[str, str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        arxiv_id = request.url.params["id_list"]
        if arxiv_id not in titles:
            return httpx.Response(200, text=ARXIV_NOT_FOUND)
        return httpx.Response(200, text=arxiv_atom(arxiv_id, titles[arxiv_id]))

    router.add("export.arxiv.org", "/api/query", handler)


@pytest.fixture
def papers(router: Router) -> Router:
    arxiv_api(router, {"2502.09775": "First paper", "2401.00001": "Second paper", "2401.00002": "Third paper"})
    return router


def applies(calendar: FakeCalendar) -> list[tuple[list[int], list[str]]]:
    return [args for name, args in calendar.calls if name == "apply"]


# --- add & dedupe -----------------------------------------------------------------


def test_add_and_dedupe(queue: PaperQueue, papers: Router) -> None:
    r = queue.add("2502.09775", tags=["ml", " ML "], notes="from a chat")
    assert r.created and r.warnings == []
    p = r.paper
    assert (p.title, p.arxiv_id, p.tags, p.notes, p.source) == (
        "First paper: With Line Breaks", "2502.09775", ["ml"], "from a chat", "manual")
    assert queue.store.rev() == 1

    dup = queue.add("https://arxiv.org/abs/2502.09775v2", tags=["new"], priority="high", notes="again")
    assert not dup.created
    assert (dup.paper.id, dup.paper.tags, dup.paper.priority) == (p.id, ["ml", "new"], 1)
    assert dup.paper.notes == "from a chat\n\nagain"
    assert not queue.add("10.48550/arXiv.2502.09775").created
    assert len(papers.requests) == 1  # duplicates are found before any network call
    assert queue.store.rev() == 3
    assert len(queue.list("all")) == 1


def test_dedupe_after_resolution(queue: PaperQueue, router: Router) -> None:
    router.json("api.crossref.org", "/works/10.1038/s41467-024-55707-8", crossref_work())
    page = '<meta name="citation_doi" content="10.1038/s41467-024-55707-8"><meta name="citation_title" content="x">'
    router.text("journal.example", "/article", page)
    first = queue.add("https://www.nature.com/articles/s41467-024-55707-8")
    again = queue.add("https://journal.example/article")
    assert first.created and not again.created
    assert again.paper.id == first.paper.id
    title_only = queue.add("Deep learning of cell states")
    assert not title_only.created  # title match, no network


def test_add_schedules_next_free_slot(queue: PaperQueue, papers: Router, calendar: FakeCalendar) -> None:
    a = queue.add("2502.09775", when="next").paper
    b = queue.add("2401.00001", when="next").paper
    assert (a.scheduled_start, a.duration_minutes) == (MON, 45)
    assert b.scheduled_start == TUE
    assert applies(calendar) == [([a.id], []), ([b.id], [])]
    spec = calendar.events[a.calendar_uid or ""]
    assert spec.title == "Read: First paper: With Line Breaks"
    assert spec.url == "https://arxiv.org/pdf/2502.09775"
    assert spec.end_utc - spec.start_utc == timedelta(minutes=45)
    assert spec.alert_minutes == 10
    assert spec.notes.splitlines()[-1] == event_marker(a.id or 0)
    # a duplicate add with when="next" keeps the existing session
    assert queue.add("2502.09775", when="next").paper.scheduled_start == MON


def test_add_with_unparseable_when_still_saves(queue: PaperQueue, papers: Router) -> None:
    r = queue.add("2502.09775", when="someday")
    assert r.created and r.paper.scheduled_start is None
    assert any("not scheduled" in w for w in r.warnings)


def test_minimal_record_fallbacks(queue: PaperQueue, router: Router) -> None:
    r = queue.add("https://example.org/landing")  # 404
    assert r.created and r.paper.needs_metadata
    assert (r.paper.title, r.paper.url) == ("https://example.org/landing", "https://example.org/landing")
    assert "minimal record" in r.warnings[0]

    router.text("example.org", "/files/x.pdf", PDF, "application/pdf")
    pdf = queue.add("https://example.org/files/x.pdf", title="Known title of the PDF").paper
    assert (pdf.title, pdf.pdf_url, pdf.needs_metadata) == (
        "Known title of the PDF", "https://example.org/files/x.pdf", True)

    router.add("export.arxiv.org", "/", httpx.Response(503))
    down = queue.add("2502.09775").paper
    assert (down.arxiv_id, down.url, down.needs_metadata) == ("2502.09775", "https://arxiv.org/abs/2502.09775", True)

    router.json("api.crossref.org", "/works", {"message": {"items": [{"title": ["Unrelated"], "DOI": "10.1/u"}]}})
    with pytest.raises(MetadataError) as exc:
        queue.add("some half remembered paper title")
    assert exc.value.candidates[0]["doi"] == "10.1/u"
    texty = queue.add("some half remembered paper title", title="Some Half Remembered Paper Title").paper
    assert texty.needs_metadata and texty.url is None
    with pytest.raises(ValueError):
        queue.add("   ")


def test_add_from_bookmarklet_hints(queue: PaperQueue, router: Router) -> None:
    router.text("publisher.example", "/a", "denied", status=403)
    hints = {"citation_title": "Hinted Paper Title", "citation_author": ["A One", "B Two"], "citation_date": "2024"}
    r = queue.add("https://publisher.example/a", hints=hints, source="web")
    assert (r.paper.title, r.paper.authors, r.paper.year, r.paper.source) == (
        "Hinted Paper Title", ["A One", "B Two"], 2024, "web")
    assert r.paper.needs_metadata


def test_parse_priority() -> None:
    assert [parse_priority(v) for v in (1, "2", "low", " High ")] == [1, 2, 3, 1]
    for bad in (0, 4, "urgent", True):
        with pytest.raises(ValueError):
            parse_priority(bad)  # type: ignore[arg-type]


# --- tombstones ----------------------------------------------------------------------


def test_remove_is_a_tombstone(queue: PaperQueue, papers: Router, library: FakeLibrary, cfg: Config) -> None:
    queue._library, queue._library_loaded = library, True
    cfg.zotero.push_new_papers = False
    p = queue.add("2502.09775", when="next").paper
    removed = queue.remove(p.id or 0)
    assert removed.paper.status == "removed"
    with pytest.raises(KeyError):
        queue.get(p.id or 0)
    with pytest.raises(KeyError):
        queue.remove(p.id or 0)
    assert queue.list("all") == [] and queue.status()["counts"]["to_read"] == 0

    library.items = [LibraryItem(PaperMeta(title="First paper", arxiv_id="2502.09775"), key="Z1")]
    assert queue.sync_library().created == 0
    assert queue.list("all") == []

    revived = queue.add("2502.09775")
    assert revived.created and revived.paper.id == p.id and revived.paper.status == "to_read"
    assert revived.paper.scheduled_start is None


# --- library -------------------------------------------------------------------------


@pytest.fixture
def lib_queue(queue: PaperQueue, library: FakeLibrary) -> PaperQueue:
    queue._library, queue._library_loaded = library, True
    return queue


def test_push_on_add(lib_queue: PaperQueue, papers: Router, library: FakeLibrary, cfg: Config) -> None:
    p = lib_queue.add("2502.09775", tags=["t"]).paper
    assert (p.library, p.library_key) == ("zotero", "KEY1")
    assert library.pushed[0][0].arxiv_id == "2502.09775" and library.pushed[0][1] == ["t"]

    cfg.zotero.push_new_papers = False
    assert lib_queue.add("2401.00001").paper.library_key is None
    cfg.zotero.push_new_papers = True
    library.writable = False
    assert lib_queue.add("2401.00002").paper.library_key is None
    assert len(library.pushed) == 1


def test_push_failure_is_a_warning(lib_queue: PaperQueue, papers: Router, library: FakeLibrary) -> None:
    library.fail_push = True
    r = lib_queue.add("2502.09775")
    assert r.created and r.paper.library_key is None
    assert r.warnings == ["zotero: could not add to library: HTTP 403 from library"]
    assert lib_queue.status()["library"]["ok"] is False


def test_library_source_is_not_pushed_back(lib_queue: PaperQueue, papers: Router, library: FakeLibrary) -> None:
    lib_queue.add("2502.09775", source="zotero")
    assert library.pushed == []


def test_sync_library(lib_queue: PaperQueue, papers: Router, library: FakeLibrary, cfg: Config) -> None:
    cfg.zotero.push_new_papers = False
    local = lib_queue.add("2502.09775", tags=["mine"]).paper
    library.items = [
        LibraryItem(PaperMeta(title="First paper", arxiv_id="2502.09775"), key="Z1", tags=["zot"]),
        LibraryItem(PaperMeta(title="Only in Zotero, a long title", doi="10.1000/z2"), key="Z2",
                    added_at=datetime(2026, 1, 1)),
    ]
    r = lib_queue.sync_library()
    assert (r.pulled, r.created, r.pushed, r.warnings) == (2, 1, 0, [])
    linked = lib_queue.get(local.id or 0)
    assert (linked.library_key, linked.tags) == ("Z1", ["mine", "zot"])
    new = next(p for p in lib_queue.list("all") if p.library_key == "Z2")
    assert (new.source, new.added_at) == ("zotero", "2026-01-01T00:00:00+00:00")
    assert lib_queue.sync_library().created == 0
    assert lib_queue.status()["library"]["last_sync"] is not None

    cfg.zotero.push_new_papers = True
    lib_queue.add("2401.00001", source="mcp")  # pushed on add
    library.pushed.clear()
    lib_queue.store.update(lib_queue.list("all")[0].id or 0, library=None, library_key=None)
    assert lib_queue.sync_library().pushed == 1


def test_sync_library_without_provider(queue: PaperQueue) -> None:
    assert queue.sync_library().warnings == ["No reference manager configured"]


# --- calendar reconcile --------------------------------------------------------------


def test_reconcile_is_idempotent(queue: PaperQueue, papers: Router, calendar: FakeCalendar, clock: Clock) -> None:
    queue.add("2502.09775", when="next")
    queue.add("2401.00001", when="next")
    calendar.calls.clear()
    assert queue.sync_calendar() == []  # first full sync sweeps
    assert [c[0] for c in calendar.calls] == ["list_uids"]
    calendar.calls.clear()
    assert queue.sync_calendar() == []
    assert calendar.calls == []  # nothing changed: no backend calls at all
    clock.now += timedelta(hours=25)
    queue.sync_calendar()
    assert [c[0] for c in calendar.calls] == ["list_uids"]  # daily sweep, still no writes


def test_orphans_deleted_unmarked_untouched(queue: PaperQueue, papers: Router, calendar: FakeCalendar) -> None:
    orphan = calendar.add_event(999)
    personal = calendar.add_event(None, "Dentist")
    p = queue.add("2502.09775", when="next").paper
    queue.sync_calendar(force=True)
    assert orphan not in calendar.events
    assert personal in calendar.events
    assert calendar.list_uids() == {p.calendar_uid: p.id}
    assert all(personal not in deletes for _, deletes in applies(calendar))


def test_reconcile_recreates_and_adopts(queue: PaperQueue, papers: Router, calendar: FakeCalendar) -> None:
    p = queue.add("2502.09775", when="next").paper
    del calendar.events[p.calendar_uid or ""]  # the user deleted the event in Calendar
    queue.sync_calendar(force=True)
    recreated = queue.get(p.id or 0).calendar_uid
    assert recreated and recreated != p.calendar_uid and recreated in calendar.events

    queue.store.update(p.id or 0, calendar_uid=None, calendar_hash=None)  # uid lost after a crash
    queue.sync_calendar(force=True)
    assert queue.get(p.id or 0).calendar_uid == recreated
    assert len(calendar.events) == 1


def test_mutations_update_events(queue: PaperQueue, papers: Router, calendar: FakeCalendar) -> None:
    p = queue.add("2502.09775", when="next").paper
    uid = p.calendar_uid
    queue.update(p.id or 0, title="Renamed paper")
    assert calendar.events[uid or ""].title == "Read: Renamed paper"
    queue.update(p.id or 0, tags=["x"])  # tags are not in the event: no calendar call
    assert len(applies(calendar)) == 2
    queue.unschedule(p.id or 0)
    assert uid not in calendar.events and queue.get(p.id or 0).calendar_uid is None
    queue.schedule(p.id or 0, "tomorrow")
    assert len(calendar.events) == 1
    queue.mark_read(p.id or 0)
    assert calendar.events == {}
    q = queue.add("2401.00001", when="next").paper
    queue.remove(q.id or 0)
    assert calendar.events == {}


def test_calendar_failure_is_a_warning(queue: PaperQueue, papers: Router, calendar: FakeCalendar) -> None:
    calendar.fail = True
    r = queue.add("2502.09775", when="next")
    assert r.created and r.paper.scheduled_start == MON and r.paper.calendar_uid is None
    assert r.warnings == ["calendar: Calendar.app is not responding"]
    assert queue.status()["calendar"]["ok"] is False
    calendar.fail = False
    assert queue.sync_calendar() == []
    assert queue.get(r.paper.id or 0).calendar_uid in calendar.events


def test_calendar_lock(queue: PaperQueue, papers: Router, cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "LOCK_TIMEOUT_S", 0.2)
    fd = os.open(cfg.calendar_lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        r = queue.add("2502.09775", when="next")
        assert r.created and "lock is busy" in r.warnings[0]
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert queue.sync_calendar() == []
    assert queue.get(r.paper.id or 0).calendar_uid


def test_event_spec_format(cfg: Config) -> None:
    p = Paper(
        id=5, title="A " + "very " * 40 + "long title", authors=[f"Author {i}" for i in range(10)],
        year=2024, venue="Nature", abstract="word " * 400, url="https://doi.org/10.1/x", doi="10.1/x",
        pdf_url="https://example.org/x.pdf", pdf_path="/tmp/pdfs/Author 2024 - A very.pdf",
        scheduled_start=MON, duration_minutes=30,
    )
    spec = event_spec(p, cfg)
    assert len(spec.title) <= 90 and spec.title.startswith("Read: A very") and spec.title.endswith("…")
    lines = spec.notes.splitlines()
    assert lines[0].endswith("Author 5 et al.")
    assert lines[1] == "Nature · 2024"
    assert "PDF: https://example.org/x.pdf" in lines
    assert "Offline PDF (Files app): Author 2024 - A very.pdf" in lines
    assert not any(line.startswith("DOI:") for line in lines)  # url already is the DOI link
    abstract = next(line for line in lines if line.startswith("word"))
    assert len(abstract) <= 500
    assert lines[-1] == "paperqueue:#5"
    assert spec.url == "https://example.org/x.pdf"
    assert spec.start_utc == datetime(2026, 11, 16, 7, 30, tzinfo=UTC)


# --- schedule, auto-schedule, views ----------------------------------------------------


def test_schedule(queue: PaperQueue, papers: Router, clock: Clock) -> None:
    a = queue.add("2502.09775", when="next").paper
    b = queue.add("2401.00001").paper
    r = queue.schedule(b.id or 0, "2026-11-16T08:45", duration_minutes=30)
    assert r.paper.scheduled_start == "2026-11-16T07:45:00+00:00"
    assert r.warnings == ["overlaps another scheduled reading session"]
    r = queue.schedule(b.id or 0, datetime(2026, 11, 1, 9, 0))
    assert "in the past" in r.warnings[0]
    assert queue.schedule(b.id or 0, "none").paper.scheduled_start is None
    with pytest.raises(ValueError):
        queue.schedule(b.id or 0, "whenever")
    with pytest.raises(ValueError):
        queue.schedule(b.id or 0, "next", duration_minutes=1)
    with pytest.raises(KeyError):
        queue.schedule(999, "next")
    queue.mark_read(a.id or 0)
    with pytest.raises(ValueError):
        queue.schedule(a.id or 0, "next")


def test_auto_schedule_and_views(queue: PaperQueue, papers: Router, clock: Clock) -> None:
    missed = queue.add("2502.09775", priority="low").paper
    queue.schedule(missed.id or 0, "2026-11-13T08:30")  # last Friday
    b = queue.add("2401.00001", priority="high").paper
    c = queue.add("2401.00002").paper
    assert [p.id for p in queue.list("missed")] == [missed.id]
    assert [p.id for p in queue.list("queue")] == [b.id, c.id]

    without = queue.auto_schedule(include_missed=False, limit=1)
    assert [p.id for p in without.papers] == [b.id] and without.papers[0].scheduled_start == MON
    result = queue.auto_schedule()
    assert [(p.id, p.scheduled_start) for p in result.papers] == [(c.id, TUE), (missed.id, "2026-11-18T07:30:00+00:00")]
    assert result.unplaced == [] and queue.list("missed") == []
    assert [p.id for p in queue.list("upcoming")] == [b.id, c.id, missed.id]
    assert [p.id for p in queue.list("upcoming", query="second")] == [b.id]

    assert queue.auto_schedule().papers == []
    queue.cfg.schedule.horizon_days = 1
    for p in queue.list("upcoming"):
        queue.unschedule(p.id or 0)
    tight = queue.auto_schedule()
    assert len(tight.papers) == 1 and len(tight.unplaced) == 2 and "did not fit" in tight.warnings[-1]
    with pytest.raises(ValueError):
        queue.list("later")


def test_free_slots(queue: PaperQueue, papers: Router) -> None:
    queue.add("2502.09775", when="next")
    slots = queue.free_slots(3)
    assert [s.isoformat() for s in slots] == [TUE, "2026-11-18T07:30:00+00:00", "2026-11-19T07:30:00+00:00"]


def test_update_validation(queue: PaperQueue, papers: Router) -> None:
    p = queue.add("2502.09775").paper
    u = queue.update(
        p.id or 0, priority="low", tags=["a", "A", "b"], notes=" n ", url="https://x.example/p", pdf_url=""
    )
    assert (u.priority, u.tags, u.notes, u.url, u.pdf_url) == (3, ["a", "b"], "n", "https://x.example/p", None)
    for kwargs in ({"url": "javascript:alert(1)"}, {"title": "  "}, {"priority": 9}):
        with pytest.raises(ValueError):
            queue.update(p.id or 0, **kwargs)  # type: ignore[arg-type]
    rev = queue.store.rev()
    assert queue.update(p.id or 0) == queue.get(p.id or 0)
    assert queue.store.rev() == rev  # no-op update does not bump


def test_mark_read_moves_pdf(queue: PaperQueue, papers: Router, cfg: Config) -> None:
    p = queue.add("2502.09775").paper
    cfg.pdf.dir.mkdir()
    f = cfg.pdf.dir / "Lovelace 2025 - First paper.pdf"
    f.write_bytes(PDF)
    queue.store.update(p.id or 0, pdf_path=str(f))
    r = queue.mark_read(p.id or 0)
    assert r.paper.status == "read" and r.paper.read_at
    assert r.paper.pdf_path == str(cfg.pdf.dir / "Read" / f.name) and not f.exists()
    assert [x.id for x in queue.list("read")] == [p.id]
    assert queue.mark_read(p.id or 0, read=False).paper.status == "to_read"


def test_every_mutation_bumps_rev(queue: PaperQueue, papers: Router) -> None:
    p = queue.add("2502.09775").paper
    ops = [
        lambda: queue.schedule(p.id or 0, "next"),
        lambda: queue.unschedule(p.id or 0),
        lambda: queue.update(p.id or 0, notes="x"),
        lambda: queue.auto_schedule(),
        lambda: queue.mark_read(p.id or 0),
        lambda: queue.mark_read(p.id or 0, read=False),
        lambda: queue.remove(p.id or 0),
    ]
    for op in ops:
        before = queue.store.rev()
        op()
        assert queue.store.rev() == before + 1


# --- maintenance, status, backends --------------------------------------------------


def test_maintain(queue: PaperQueue, router: Router, calendar: FakeCalendar, cfg: Config) -> None:
    router.add("export.arxiv.org", "/", httpx.Response(503))
    p = queue.add("2502.09775", when="next").paper
    assert p.needs_metadata
    arxiv_api(router, {"2502.09775": "First paper"})
    router.text("arxiv.org", "/pdf/2502.09775", PDF, "application/pdf")
    cfg.pdf.dir.mkdir()
    queue.maintain()
    fresh = queue.get(p.id or 0)
    assert fresh.title == "First paper: With Line Breaks" and not fresh.needs_metadata
    assert fresh.pdf_path and Path(fresh.pdf_path).read_bytes() == PDF
    notes = calendar.events[fresh.calendar_uid or ""].notes
    assert "Offline PDF (Files app): Lovelace 2025 - First paper With Line Breaks.pdf" in notes
    assert "First paper" in calendar.events[fresh.calendar_uid or ""].title
    requests = len(router.requests)
    queue.maintain()  # nothing left to do: no network
    assert len(router.requests) == requests


def test_status(queue: PaperQueue, papers: Router, calendar: FakeCalendar, cfg: Config) -> None:
    cfg.zotero.api_key = "ZOTERO-SECRET"
    cfg.mendeley.client_secret = "MENDELEY-SECRET"
    queue.add("2502.09775", when="next")
    s = queue.status()
    assert set(s) == {"rev", "counts", "library", "calendar", "pdf", "media", "schedule", "lan"}
    assert set(s["library"]) == {"provider", "ok", "message", "can_write", "last_sync"}
    assert set(s["calendar"]) == {"backend", "name", "ok", "message"}
    assert s["calendar"]["ok"] is True and s["library"]["provider"] == "off"
    assert s["counts"]["scheduled"] == 1
    assert s["schedule"] == {"days": ["mon", "tue", "wed", "thu", "fri"], "times": ["08:30"],
                             "duration_minutes": 45, "max_session_minutes": 90, "timezone": "Europe/Berlin"}
    assert s["lan"] == {"enabled": False, "urls": []}
    dumped = json.dumps(s)
    for secret in ("ZOTERO-SECRET", "MENDELEY-SECRET", cfg.server.token):
        assert secret not in dumped
    queue.status()
    assert [c for c in calendar.calls if c[0] == "check"] == [("check", None)]  # cached


def test_lazy_backends(cfg: Config, resolver: Resolver, clock: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg.calendar.backend = "apple"
    cfg.library.provider = "zotero"
    monkeypatch.setitem(sys.modules, "paperqueue.calendar", None)
    monkeypatch.setitem(sys.modules, "paperqueue.libraries", None)
    q = PaperQueue(cfg, resolver=resolver, clock=clock)
    assert q.calendar is None and q.library is None
    assert "unavailable" in q.status()["calendar"]["message"]
    assert "unavailable" in q.sync_calendar()[0]

    fake_cal, fake_lib = FakeCalendar(), FakeLibrary()
    seen: list[object] = []
    cal_mod = types.ModuleType("paperqueue.calendar")
    cal_mod.get_backend = lambda c: seen.append(c) or fake_cal  # type: ignore[attr-defined]
    lib_mod = types.ModuleType("paperqueue.libraries")
    lib_mod.get_provider = lambda c, home: seen.append(home) or fake_lib  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "paperqueue.calendar", cal_mod)
    monkeypatch.setitem(sys.modules, "paperqueue.libraries", lib_mod)
    q = PaperQueue(cfg, resolver=resolver, clock=clock)
    assert q.calendar is fake_cal and q.library is fake_lib
    assert seen == [cfg, cfg.home]


# --- YouTube videos --------------------------------------------------------------------

WATCH = f"https://www.youtube.com/watch?v={VIDEO_ID}"


def test_add_video_dedupes_url_variants(queue: PaperQueue, ytdlp: FakeYtDlp, kicks: list) -> None:
    r = queue.add(f"https://youtu.be/{VIDEO_ID}", when="none")
    p = r.paper
    assert r.created and r.warnings == []
    assert (p.kind, p.video_id, p.title, p.authors, p.venue, p.year, p.url) == (
        "video", VIDEO_ID, "How Transformers Work", ["Some Channel"], "YouTube", 2024, WATCH)
    assert (p.duration_seconds, p.media_mode, p.media_status, p.needs_metadata) == (2520, "link", None, False)
    for variant in (f"{WATCH}&t=30s", f"{WATCH}&list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG",
                    f"https://m.youtube.com/watch?v={VIDEO_ID}", f"https://www.youtube.com/shorts/{VIDEO_ID}"):
        again = queue.add(variant)
        assert not again.created and again.paper.id == p.id
    assert len(ytdlp.calls) == 1  # duplicates are found without a lookup
    assert kicks == []  # link mode downloads nothing
    other = queue.add("https://youtu.be/abcdefghijk")
    assert other.created and other.paper.id != p.id
    with pytest.raises(MetadataError, match="playlists"):
        queue.add("https://www.youtube.com/playlist?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG")


def test_video_session_follows_its_length(
    queue: PaperQueue, ytdlp: FakeYtDlp, calendar: FakeCalendar, cfg: Config
) -> None:
    ytdlp.info[VIDEO_ID] = ytdlp_info(duration=2950)  # 49 min 10 s -> 50 min
    v = queue.add(WATCH, when="next").paper
    assert (v.scheduled_start, v.duration_minutes) == (MON, 50)
    event = calendar.events[v.calendar_uid or ""]
    assert event.title == "Watch: How Transformers Work"
    assert event.url == WATCH
    assert "YouTube · 2024 · 49 min" in event.notes and f"YouTube: {WATCH}" in event.notes

    # the length is kept in duration_seconds, so it survives unschedule (which clears duration_minutes)
    assert queue.unschedule(v.id or 0).paper.duration_minutes is None
    assert queue.schedule(v.id or 0, "tomorrow").paper.duration_minutes == 50
    queue.unschedule(v.id or 0)
    assert queue.auto_schedule().papers[0].duration_minutes == 50

    ytdlp.info["abcdefghijk"] = ytdlp_info("abcdefghijk", duration=3 * 3600)
    long = queue.add("https://youtu.be/abcdefghijk", when="next").paper
    assert long.duration_minutes == cfg.schedule.max_session_minutes == 90
    assert queue.schedule(long.id or 0, "next", duration_minutes=120).paper.duration_minutes == 120


def test_free_slots_fit_the_video(queue: PaperQueue, ytdlp: FakeYtDlp, papers: Router, cfg: Config) -> None:
    cfg.schedule.times = ["08:30", "10:00"]
    cfg.schedule.max_session_minutes = 120
    ytdlp.info[VIDEO_ID] = ytdlp_info(duration=3 * 3600)
    long = queue.add(WATCH).paper
    paper = queue.add("2502.09775").paper
    queue.schedule(paper.id or 0, "2026-11-16T10:00")
    assert queue.free_slots(1)[0].isoformat() == MON  # 45 minutes fit before 10:00
    assert queue.free_slots(1, paper_id=long.id)[0].isoformat() == TUE  # 120 minutes do not
    with pytest.raises(ValueError, match="no free reading slot left"):
        queue.schedule(long.id or 0, "today")  # "today" also needs room for the whole session
    assert queue.schedule(long.id or 0, "tomorrow").paper.scheduled_start == TUE


def test_set_media(queue: PaperQueue, ytdlp: FakeYtDlp, papers: Router, kicks: list, monkeypatch) -> None:
    v = queue.add(WATCH).paper
    assert v.id is not None
    r = queue.set_media(v.id, "Audio")
    assert (r.paper.media_mode, r.paper.media_status) == ("audio", "pending") and r.warnings == []
    assert len(kicks) == 1
    assert calendar_title(queue, v.id).startswith("Listen: ")
    queue.set_media(v.id, "audio")  # already pending: no change, worker nudged again
    assert len(kicks) == 2

    from paperqueue import metadata

    monkeypatch.setattr(metadata, "find_tool", lambda name: "/x/deno" if name == "deno" else None)
    r = queue.set_media(v.id, "video")
    assert (r.paper.media_status, r.paper.media_error) == ("failed", "no_ffmpeg")
    assert "brew install ffmpeg" in r.warnings[-1] and len(kicks) == 2
    assert queue.set_media(v.id, "link").paper.media_status is None

    paper = queue.add("2502.09775").paper
    with pytest.raises(ValueError, match="not a video"):
        queue.set_media(paper.id or 0, "audio")
    with pytest.raises(ValueError, match="invalid media mode"):
        queue.set_media(v.id, "mp3")
    with pytest.raises(ValueError, match="YouTube links only"):
        queue.add("2401.00001", media="audio")
    queue.mark_read(v.id)
    with pytest.raises(ValueError, match="already watched"):
        queue.set_media(v.id, "audio")


def calendar_title(queue: PaperQueue, paper_id: int) -> str:
    p = queue.get(paper_id)
    return event_spec(p, queue.cfg).title if p.scheduled_start else service.event_title(p)


def test_add_with_media_and_default_mode(queue: PaperQueue, ytdlp: FakeYtDlp, kicks: list, cfg: Config) -> None:
    r = queue.add(WATCH, media="audio")
    assert (r.paper.media_mode, r.paper.media_status) == ("audio", "pending") and len(kicks) == 1
    again = queue.add(f"https://youtu.be/{VIDEO_ID}", media="video")  # explicit mode on a duplicate
    assert not again.created and again.paper.media_mode == "video" and len(kicks) == 2
    cfg.media.default_mode = "audio"
    other = queue.add("https://youtu.be/abcdefghijk")
    assert other.paper.media_mode == "audio"


def test_watched_and_unwatched_video(queue: PaperQueue, ytdlp: FakeYtDlp, kicks: list, cfg: Config) -> None:
    v = queue.add(WATCH, media="audio").paper
    cfg.media.dir.mkdir()
    f = cfg.media.dir / "x.m4a"
    f.write_bytes(b"a")
    queue.store.update(v.id or 0, media_status="done", media_path=str(f))
    read = queue.mark_read(v.id or 0).paper
    assert not f.exists() and (read.media_mode, read.media_status, read.media_path) == ("audio", None, None)
    kicks.clear()
    back = queue.mark_read(v.id or 0, read=False).paper
    assert back.media_status == "pending" and len(kicks) == 1
    queue.remove(v.id or 0)
    revived = queue.add(WATCH).paper
    assert (revived.status, revived.media_mode, revived.media_status) == ("to_read", "link", None)


def test_oembed_fallback_is_completed_later(
    queue: PaperQueue, ytdlp: FakeYtDlp, router: Router, cfg: Config
) -> None:
    info = ytdlp.info.pop(VIDEO_ID)
    router.json("www.youtube.com", "/oembed", {"title": "How Transformers Work", "author_name": "Some Channel"})
    v = queue.add(WATCH, when="next").paper
    assert (v.needs_metadata, v.duration_seconds, v.duration_minutes) == (True, None, cfg.schedule.duration_minutes)
    ytdlp.info[VIDEO_ID] = info
    queue.maintain()
    fixed = queue.get(v.id or 0)
    assert (fixed.needs_metadata, fixed.duration_seconds) == (False, 2520)
    assert fixed.abstract and fixed.year == 2024


def test_videos_are_not_pushed_to_the_library(lib_queue: PaperQueue, ytdlp: FakeYtDlp, library: FakeLibrary) -> None:
    lib_queue.add(WATCH)
    assert lib_queue.sync_library().pushed == 0
    assert library.pushed == []


def test_status_media(queue: PaperQueue, ytdlp: FakeYtDlp, monkeypatch) -> None:
    v = queue.add(WATCH, media="audio").paper
    other = queue.add("https://youtu.be/abcdefghijk", media="audio").paper
    queue.store.update(other.id or 0, media_status="failed", media_error="bot_check")
    m = queue.status()["media"]
    assert (m["default_mode"], m["deno"], m["ffmpeg"], m["pending"]) == ("link", True, True, 1)
    assert m["failed"] == [{"id": other.id, "title": other.title, "error": "bot_check",
                            "fix": service.mediacache.FIXES["bot_check"]}]
    assert v.id != other.id
