"""SQLite storage for the reading queue."""

from __future__ import annotations

import builtins
import json
import re
import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE papers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        authors TEXT NOT NULL DEFAULT '[]',
        year INTEGER,
        venue TEXT,
        abstract TEXT,
        url TEXT,
        pdf_url TEXT,
        doi TEXT,
        arxiv_id TEXT,
        library TEXT,
        library_key TEXT,
        tags TEXT NOT NULL DEFAULT '[]',
        priority INTEGER NOT NULL DEFAULT 2,
        status TEXT NOT NULL DEFAULT 'to_read'
            CHECK (status IN ('to_read', 'read', 'removed')),
        notes TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'manual',
        added_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        scheduled_start TEXT,
        duration_minutes INTEGER,
        calendar_uid TEXT,
        calendar_hash TEXT,
        read_at TEXT,
        pdf_path TEXT,
        needs_metadata INTEGER NOT NULL DEFAULT 0,
        title_key TEXT,
        url_key TEXT
    );
    CREATE INDEX ix_papers_status ON papers(status, scheduled_start);
    CREATE UNIQUE INDEX ux_papers_doi ON papers(lower(doi)) WHERE doi IS NOT NULL;
    CREATE UNIQUE INDEX ux_papers_arxiv ON papers(arxiv_id) WHERE arxiv_id IS NOT NULL;
    CREATE UNIQUE INDEX ux_papers_library ON papers(library, library_key)
        WHERE library IS NOT NULL AND library_key IS NOT NULL;
    CREATE INDEX ix_papers_url_key ON papers(url_key) WHERE url_key IS NOT NULL;
    CREATE INDEX ix_papers_title_key ON papers(title_key) WHERE title_key IS NOT NULL;
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """,
    # 2: YouTube videos share the table (kind = 'video'), deduplicated by video_id
    """
    ALTER TABLE papers ADD COLUMN kind TEXT NOT NULL DEFAULT 'paper';
    ALTER TABLE papers ADD COLUMN video_id TEXT;
    ALTER TABLE papers ADD COLUMN media_mode TEXT NOT NULL DEFAULT 'link';
    ALTER TABLE papers ADD COLUMN media_path TEXT;
    ALTER TABLE papers ADD COLUMN media_status TEXT;
    ALTER TABLE papers ADD COLUMN media_error TEXT;
    ALTER TABLE papers ADD COLUMN duration_seconds INTEGER;
    CREATE UNIQUE INDEX ux_papers_video ON papers(video_id) WHERE video_id IS NOT NULL;
    """,
]

PRIORITY_NAMES = {1: "high", 2: "normal", 3: "low"}
STATUSES = ("to_read", "read", "removed")
ORDERS = {
    "priority": "priority ASC, added_at ASC, id ASC",
    "scheduled": "scheduled_start ASC, priority ASC, id ASC",
    "added": "added_at DESC, id DESC",
    "read": "read_at DESC, id DESC",
}
_TRACKING_PARAMS = re.compile(r"^(utm_.*|fbclid|gclid|mc_cid|mc_eid|ref|referrer|source)$", re.I)


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    """Aware datetime -> UTC ISO8601 with +00:00 (the only stored timestamp form)."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def title_key(title: str | None) -> str | None:
    """Accent-, case- and punctuation-insensitive title, for duplicate detection."""
    if not title:
        return None
    text = unicodedata.normalize("NFKD", title)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())
    return text if len(text) >= 16 else None


def url_key(url: str | None) -> str | None:
    """Scheme-, www-, fragment-, trailing-slash- and tracking-parameter-insensitive URL."""
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower().removeprefix("www.")
    if not host:
        return None
    query = urlencode(
        sorted((k, v) for k, v in parse_qsl(parts.query) if not _TRACKING_PARAMS.match(k))
    )
    key = host + (parts.path.rstrip("/") or "")
    return f"{key}?{query}" if query else key


@dataclass
class Paper:
    id: int | None
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    abstract: str | None = None
    url: str | None = None
    pdf_url: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    library: str | None = None
    library_key: str | None = None
    tags: list[str] = field(default_factory=list)
    priority: int = 2
    status: str = "to_read"
    notes: str = ""
    source: str = "manual"
    added_at: str = ""
    updated_at: str = ""
    scheduled_start: str | None = None
    duration_minutes: int | None = None
    calendar_uid: str | None = None
    calendar_hash: str | None = None
    read_at: str | None = None
    pdf_path: str | None = None
    needs_metadata: bool = False
    kind: str = "paper"  # paper | video
    video_id: str | None = None
    media_mode: str = "link"  # link | audio | video
    media_path: str | None = None
    media_status: str | None = None  # None | pending | downloading | done | failed
    media_error: str | None = None  # a mediacache.FIXES code
    duration_seconds: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Paper:
        d = {k: row[k] for k in row.keys() if k in _FIELDS}  # noqa: SIM118 (Row has no __contains__ for keys)
        d["authors"] = json.loads(d["authors"] or "[]")
        d["tags"] = json.loads(d["tags"] or "[]")
        d["needs_metadata"] = bool(d["needs_metadata"])
        return cls(**d)

    @property
    def start(self) -> datetime | None:
        return from_iso(self.scheduled_start)

    @property
    def end(self) -> datetime | None:
        start = self.start
        if start is None or not self.duration_minutes:
            return None
        return start + timedelta(minutes=self.duration_minutes)

    def is_missed(self, now: datetime) -> bool:
        end = self.end
        return self.status == "to_read" and end is not None and end < now

    def to_dict(self, tz: tzinfo | None = None, now: datetime | None = None) -> dict[str, Any]:
        """JSON-ready dict. Timestamps are UTC ISO; `*_local` variants are in `tz`."""
        tz = tz or UTC
        now = now or utcnow()
        d = asdict(self)
        for internal in ("calendar_uid", "calendar_hash", "pdf_path", "media_path"):
            d.pop(internal)
        end = self.end
        d["scheduled_end"] = to_iso(end) if end else None
        for name in ("scheduled_start", "scheduled_end", "added_at", "updated_at", "read_at"):
            value = from_iso(d[name])
            d[f"{name}_local"] = value.astimezone(tz).isoformat(timespec="seconds") if value else None
        d["priority_name"] = PRIORITY_NAMES.get(self.priority, "normal")
        d["scheduled"] = self.scheduled_start is not None
        d["missed"] = self.is_missed(now)
        d["has_pdf"] = bool(self.pdf_path and Path(self.pdf_path).is_file())
        d["has_media"] = bool(self.media_path and Path(self.media_path).is_file())
        return d


_FIELDS = set(Paper.__dataclass_fields__)
_COLUMNS = [f for f in Paper.__dataclass_fields__ if f != "id"]
_WRITABLE = set(_COLUMNS) - {"added_at", "updated_at"}


def _encode(name: str, value: Any) -> Any:
    if name in ("authors", "tags"):
        return json.dumps(list(value or []), ensure_ascii=False)
    if name == "needs_metadata":
        return int(bool(value))
    return value


def _derived(changes: dict[str, Any]) -> dict[str, Any]:
    out = {}
    if "title" in changes:
        out["title_key"] = title_key(changes["title"])
    if "url" in changes:
        out["url_key"] = url_key(changes["url"])
    return out


class Store:
    """Thread-safe SQLite store. Each operation uses its own connection unless it runs
    inside `transaction()`, in which case it joins that transaction (same thread)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._migrate()

    def _open(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout = 10000")
        return con

    def _migrate(self) -> None:
        con = self._open()
        try:
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("BEGIN IMMEDIATE")
            version = con.execute("PRAGMA user_version").fetchone()[0]
            for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
                for statement in filter(str.strip, script.split(";")):
                    con.execute(statement)
                con.execute(f"PRAGMA user_version = {number}")
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE ... COMMIT. Store methods called inside join it; nesting is a no-op."""
        with self._lock:
            current = getattr(self._local, "con", None)
            if current is not None:
                yield current
                return
            con = self._open()
            con.execute("BEGIN IMMEDIATE")
            self._local.con = con
            try:
                yield con
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
            finally:
                self._local.con = None
                con.close()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            current = getattr(self._local, "con", None)
            if current is not None:
                yield current
                return
            con = self._open()
            try:
                yield con
            finally:
                con.close()

    # papers -------------------------------------------------------------

    def insert(self, paper: Paper) -> Paper:
        now = to_iso(utcnow())
        paper.added_at = paper.added_at or now
        paper.updated_at = now
        values = {c: _encode(c, getattr(paper, c)) for c in _COLUMNS}
        values |= _derived({"title": paper.title, "url": paper.url})
        with self._connect() as con:
            cur = con.execute(
                f"INSERT INTO papers ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                list(values.values()),
            )
            paper.id = cur.lastrowid
        return paper

    def update(self, paper_id: int, **changes: Any) -> Paper:
        bad = set(changes) - _WRITABLE
        if bad:
            raise ValueError(f"unknown fields: {sorted(bad)}")
        with self._connect() as con:
            if changes:
                values = {k: _encode(k, v) for k, v in changes.items()}
                values |= _derived(changes)
                values["updated_at"] = to_iso(utcnow())
                sets = ", ".join(f"{k} = ?" for k in values)
                con.execute(
                    f"UPDATE papers SET {sets} WHERE id = ?", [*values.values(), paper_id]
                )
            row = con.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if row is None:
            raise KeyError(paper_id)
        return Paper.from_row(row)

    def get(self, paper_id: int) -> Paper | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
        return Paper.from_row(row) if row else None

    def find_duplicate(
        self,
        *,
        video_id: str | None = None,
        doi: str | None = None,
        arxiv_id: str | None = None,
        library: str | None = None,
        library_key: str | None = None,
        url: str | None = None,
        title: str | None = None,
    ) -> Paper | None:
        """First match by video ID, DOI, arXiv ID, (library, key), normalised URL, then title.
        Includes tombstoned (removed) papers so they are not re-created. Videos are never
        matched by title: many share one ("Lecture 1")."""
        checks: list[tuple[str, list[Any]]] = []
        if video_id:
            checks.append(("video_id = ?", [video_id]))
        if doi:
            checks.append(("lower(doi) = lower(?)", [doi]))
        if arxiv_id:
            checks.append(("arxiv_id = ?", [arxiv_id]))
        if library and library_key:
            checks.append(("library = ? AND library_key = ?", [library, library_key]))
        if key := url_key(url):
            checks.append(("url_key = ?", [key]))
        if not video_id and (key := title_key(title)):
            # Same title but a different DOI is a different work (e.g. erratum, reply).
            checks.append(
                ("title_key = ? AND kind = 'paper' AND (doi IS NULL OR ? IS NULL OR lower(doi) = lower(?))",
                 [key, doi, doi])
            )
        with self._connect() as con:
            for clause, params in checks:
                row = con.execute(
                    f"SELECT * FROM papers WHERE {clause} ORDER BY id LIMIT 1", params
                ).fetchone()
                if row:
                    return Paper.from_row(row)
        return None

    def list(
        self,
        *,
        status: str | None = "to_read",
        query: str | None = None,
        tag: str | None = None,
        scheduled: bool | None = None,
        order: str = "priority",
    ) -> list[Paper]:
        """`status` None or "all" means every paper except removed ones."""
        if order not in ORDERS:
            raise ValueError(f"unknown order {order!r}")
        clauses: list[str] = []
        params: list[Any] = []
        if status in (None, "all"):
            clauses.append("status != 'removed'")
        else:
            if status not in STATUSES:
                raise ValueError(f"unknown status {status!r}")
            clauses.append("status = ?")
            params.append(status)
        if scheduled is True:
            clauses.append("scheduled_start IS NOT NULL")
        elif scheduled is False:
            clauses.append("scheduled_start IS NULL")
        if query and query.strip():
            fields_ = ("title", "authors", "abstract", "tags", "venue", "notes", "doi", "arxiv_id")
            needle = query.strip().lower()
            clauses.append(
                "(" + " OR ".join(f"instr(lower(coalesce({f}, '')), ?) > 0" for f in fields_) + ")"
            )
            params += [needle] * len(fields_)
        if tag and tag.strip():
            clauses.append(
                "EXISTS (SELECT 1 FROM json_each(papers.tags) WHERE lower(json_each.value) = ?)"
            )
            params.append(tag.strip().lower())
        sql = f"SELECT * FROM papers WHERE {' AND '.join(clauses)} ORDER BY {ORDERS[order]}"
        with self._connect() as con:
            rows = con.execute(sql, params).fetchall()
        return [Paper.from_row(r) for r in rows]

    def list_media_pending(self) -> builtins.list[Paper]:
        """Unwatched videos whose audio/video copy is not downloaded yet (incl. failed ones),
        scheduled ones first."""
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM papers WHERE status = 'to_read' AND media_mode IN ('audio', 'video') "
                "AND (media_status IS NULL OR media_status != 'done') "
                "ORDER BY scheduled_start IS NULL, scheduled_start, priority, id"
            ).fetchall()
        return [Paper.from_row(r) for r in rows]

    def counts(self, now: datetime | None = None) -> dict[str, int]:
        now = now or utcnow()
        out = {"to_read": 0, "scheduled": 0, "unscheduled": 0, "upcoming": 0, "missed": 0, "read": 0}
        for p in self.list(status="all"):
            if p.status == "read":
                out["read"] += 1
                continue
            out["to_read"] += 1
            if p.scheduled_start is None:
                out["unscheduled"] += 1
            else:
                out["scheduled"] += 1
                out["missed" if p.is_missed(now) else "upcoming"] += 1
        return out

    # meta ---------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        with self._connect() as con:
            row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def bump_rev(self) -> int:
        """Increment the change counter that clients poll to detect updates."""
        with self._connect() as con:
            row = con.execute(
                "INSERT INTO meta (key, value) VALUES ('rev', '1') "
                "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1 "
                "RETURNING value"
            ).fetchone()
        return int(row[0])

    def rev(self) -> int:
        value = self.get_meta("rev")
        return int(value) if value else 0
