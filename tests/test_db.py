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
    assert con.execute("PRAGMA user_version").fetchone()[0] == 1
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
