from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from paperqueue.db import Paper, Store, title_key, to_iso, url_key


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "pq.db")


def add(store: Store, title: str = "A sufficiently long title", **kw: object) -> Paper:
    return store.insert(Paper(id=None, title=title, **kw))  # type: ignore[arg-type]


def test_migrations(tmp_path: Path) -> None:
    Store(tmp_path / "pq.db")
    Store(tmp_path / "pq.db")  # reopening is a no-op
    con = sqlite3.connect(tmp_path / "pq.db")
    assert con.execute("PRAGMA user_version").fetchone()[0] == 2
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    cols = {r[1] for r in con.execute("PRAGMA table_info(papers)")}
    assert {"updated_at", "pdf_path", "calendar_hash", "title_key", "needs_metadata"} <= cols


def test_insert_roundtrip_and_ensure_ascii(store: Store) -> None:
    p = add(store, "Über α-Synuclein — eine Studie", authors=["Jürgen Müller"], tags=["神经"], needs_metadata=True)
    raw = sqlite3.connect(store.path).execute("SELECT authors, tags FROM papers").fetchone()
    assert raw == ('["Jürgen Müller"]', '["神经"]')
    got = store.get(p.id or 0)
    assert got is not None
    assert (got.authors, got.tags, got.needs_metadata) == (["Jürgen Müller"], ["神经"], True)
    assert got.added_at.endswith("+00:00") and got.updated_at.endswith("+00:00")


def test_update(store: Store) -> None:
    p = add(store)
    assert store.update(p.id or 0, priority=1, tags=["x"]).priority == 1
    with pytest.raises(ValueError):
        store.update(p.id or 0, title_key="nope")
    with pytest.raises(KeyError):
        store.update(999, priority=1)


def test_keys() -> None:
    assert title_key("Attention Is All You Need!") == "attention is all you need"
    assert title_key("Éclair à la crème, revisited") == title_key("eclair a la creme revisited")
    assert title_key("Short") is None
    assert url_key("https://www.Example.org/a/b/?utm_source=x&b=2&a=1#frag") == "example.org/a/b?a=1&b=2"
    assert url_key("http://example.org/a/b") == "example.org/a/b"
    assert url_key("not a url") is None


def test_find_duplicate(store: Store) -> None:
    a = add(store, doi="10.1000/ABC", arxiv_id="2502.09775", url="https://example.org/paper/")
    b = add(store, "Another rather long title", library="zotero", library_key="K1")
    assert store.find_duplicate(doi="10.1000/abc") == a
    assert store.find_duplicate(arxiv_id="2502.09775") == a
    assert store.find_duplicate(url="http://www.example.org/paper?utm_medium=email") == a
    assert store.find_duplicate(library="zotero", library_key="K1") == b
    assert store.find_duplicate(library="mendeley", library_key="K1") is None
    assert store.find_duplicate(library_key="K1") is None
    assert store.find_duplicate(title="another  RATHER long title.") == b
    # same title but a different DOI is a different work
    assert store.find_duplicate(title="A sufficiently long title", doi="10.1000/other") is None
    assert store.find_duplicate() is None
    # deterministic: identifier checks come before the title fallback
    assert store.find_duplicate(title="Another rather long title", doi="10.1000/abc") == a


def test_tombstones_are_found_but_not_listed(store: Store) -> None:
    p = add(store, doi="10.1000/gone")
    store.update(p.id or 0, status="removed")
    assert store.find_duplicate(doi="10.1000/gone") is not None
    assert store.list(status="all") == []
    assert len(store.list(status="removed")) == 1


def test_unique_indexes(store: Store) -> None:
    add(store, doi="10.1000/x", library="zotero", library_key="K")
    with pytest.raises(sqlite3.IntegrityError):
        add(store, "Other title here, long enough", doi="10.1000/X")
    with pytest.raises(sqlite3.IntegrityError):
        add(store, "Other title here, long enough", library="zotero", library_key="K")
    add(store, "Other title here, long enough", library="mendeley", library_key="K")


def test_transaction_rollback_and_nesting(store: Store) -> None:
    with pytest.raises(RuntimeError), store.transaction():
        add(store)
        with store.transaction():
            add(store, "Second paper in the transaction")
        raise RuntimeError
    assert store.list(status="all") == []
    with store.transaction():
        add(store)
    assert len(store.list(status="all")) == 1


def test_list_filters_and_order(store: Store) -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    a = add(store, "Low priority older paper", priority=3, tags=["Neuro"], added_at=to_iso(t0))
    b = add(store, "High priority newer paper", priority=1, added_at=to_iso(t0 + timedelta(days=1)),
            abstract="about synapses")
    c = add(store, "Scheduled paper for later", scheduled_start=to_iso(t0), duration_minutes=30)
    assert [p.id for p in store.list()] == [b.id, c.id, a.id]
    assert [p.id for p in store.list(order="added")] == [c.id, b.id, a.id]
    assert [p.id for p in store.list(scheduled=True)] == [c.id]
    assert [p.id for p in store.list(scheduled=False, order="scheduled")] == [b.id, a.id]
    assert [p.id for p in store.list(tag="neuro")] == [a.id]
    assert [p.id for p in store.list(query="SYNAPSE")] == [b.id]
    assert store.list(query="100%_") == []
    with pytest.raises(ValueError):
        store.list(order="random")
    with pytest.raises(ValueError):
        store.list(status="deleted")


def test_to_dict(store: Store) -> None:
    start = datetime(2026, 3, 30, 6, 30, tzinfo=UTC)
    p = add(store, scheduled_start=to_iso(start), duration_minutes=45, calendar_uid="U", calendar_hash="H",
            pdf_path="/nonexistent.pdf")
    d = p.to_dict(ZoneInfo("Europe/Berlin"), now=start + timedelta(hours=1))
    assert d["scheduled_start"] == "2026-03-30T06:30:00+00:00"
    assert d["scheduled_start_local"] == "2026-03-30T08:30:00+02:00"
    assert d["scheduled_end"] == "2026-03-30T07:15:00+00:00"
    assert d["scheduled"] is True and d["missed"] is True and d["has_pdf"] is False
    assert d["priority_name"] == "normal"
    assert not {"calendar_uid", "calendar_hash", "pdf_path"} & set(d)
    assert p.to_dict(now=start)["missed"] is False


def test_counts_and_rev(store: Store) -> None:
    now = datetime(2026, 1, 10, tzinfo=UTC)
    add(store)
    add(store, "Past scheduled paper here", scheduled_start=to_iso(now - timedelta(days=1)), duration_minutes=30)
    add(store, "Future scheduled paper here", scheduled_start=to_iso(now + timedelta(days=1)), duration_minutes=30)
    add(store, "A paper that was read", status="read")
    add(store, "A paper that was removed", status="removed")
    assert store.counts(now) == {"to_read": 3, "scheduled": 2, "unscheduled": 1, "upcoming": 1, "missed": 1, "read": 1}
    assert store.rev() == 0
    assert store.bump_rev() == 1
    assert store.bump_rev() == 2
    assert store.rev() == 2


def test_migration_from_v1_database(tmp_path: Path) -> None:
    from paperqueue.db import MIGRATIONS

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    for statement in filter(str.strip, MIGRATIONS[0].split(";")):
        con.execute(statement)
    con.execute(
        "INSERT INTO papers (title, added_at, updated_at, scheduled_start, duration_minutes) "
        "VALUES ('An old paper from version one', '2026-01-01T00:00:00+00:00', "
        "'2026-01-01T00:00:00+00:00', '2026-11-16T07:30:00+00:00', 45)"
    )
    con.execute("PRAGMA user_version = 1")
    con.commit()
    con.close()

    store = Store(path)
    (old,) = store.list(status="all")
    assert (old.kind, old.media_mode, old.video_id, old.media_status, old.duration_seconds) == (
        "paper", "link", None, None, None)
    assert old.duration_minutes == 45
    raw = sqlite3.connect(path)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 2
    indexes = {r[1] for r in raw.execute("PRAGMA index_list(papers)")}
    assert "ux_papers_video" in indexes
    video = add(store, "A video", kind="video", video_id="dQw4w9WgXcQ")
    with pytest.raises(sqlite3.IntegrityError):
        add(store, "Same video again", kind="video", video_id="dQw4w9WgXcQ")
    assert store.find_duplicate(video_id="dQw4w9WgXcQ") == video


def test_videos_never_match_by_title(store: Store) -> None:
    lecture = add(store, "Lecture 1: Introduction to the course", kind="video", video_id="aaaaaaaaaaa")
    assert store.find_duplicate(video_id="bbbbbbbbbbb", title=lecture.title) is None
    assert store.find_duplicate(title=lecture.title) is None  # a paper with the same title is new
    paper = add(store, "Lecture 1: Introduction to the course")
    assert store.find_duplicate(title=lecture.title) == paper


def test_list_media_pending_and_to_dict(store: Store, tmp_path: Path) -> None:
    add(store, "Link only video", kind="video", video_id="aaaaaaaaaaa")
    pending = add(store, "Audio video", kind="video", video_id="bbbbbbbbbbb", media_mode="audio",
                  media_status="pending")
    failed = add(store, "Failed video", kind="video", video_id="ccccccccccc", media_mode="video",
                 media_status="failed", media_error="no_ffmpeg", scheduled_start="2026-11-16T07:30:00+00:00")
    f = tmp_path / "x.m4a"
    f.write_bytes(b"a")
    add(store, "Done video", kind="video", video_id="ddddddddddd", media_mode="audio", media_status="done",
        media_path=str(f))
    add(store, "Watched video", kind="video", video_id="eeeeeeeeeee", media_mode="audio", status="read")
    assert [p.id for p in store.list_media_pending()] == [failed.id, pending.id]  # scheduled first
    done = store.find_duplicate(video_id="ddddddddddd")
    assert done is not None
    d = done.to_dict()
    assert d["has_media"] is True and "media_path" not in d and d["kind"] == "video"
    assert pending.to_dict()["has_media"] is False
