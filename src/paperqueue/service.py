"""PaperQueue: the business logic shared by the web API, the CLI and the MCP server."""

from __future__ import annotations

import builtins
import fcntl
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import pdfcache, scheduler
from .config import Config
from .db import Paper, Store, to_iso
from .metadata import MetadataError, PaperMeta, Ref, Resolver, http_url, parse_ref
from .types import (
    AddResult,
    BatchResult,
    CalendarBackend,
    EventSpec,
    LibraryProvider,
    OpResult,
    SyncResult,
    event_marker,
)

log = logging.getLogger(__name__)

PRIORITIES = {"high": 1, "normal": 2, "low": 3}
VIEWS = ("queue", "upcoming", "missed", "read", "all")
LIBRARY_SOURCES = ("zotero", "mendeley")
TITLE_MAX = 90
ABSTRACT_EXCERPT = 500
CHECK_TTL = timedelta(minutes=5)
RETRY_AFTER = timedelta(hours=1)
SWEEP_EVERY = timedelta(hours=24)
LOCK_TIMEOUT_S = 30.0


def parse_priority(value: int | str) -> int:
    """1-3 or "high"/"normal"/"low" -> 1-3."""
    if isinstance(value, bool):
        raise ValueError(f"invalid priority {value!r}")
    if isinstance(value, str):
        s = value.strip().lower()
        if s in PRIORITIES:
            return PRIORITIES[s]
        if not s.isdigit():
            raise ValueError(f"invalid priority {value!r}; use high, normal or low")
        value = int(s)
    if value not in (1, 2, 3):
        raise ValueError(f"invalid priority {value!r}; use 1-3 or high/normal/low")
    return value


def clean_tags(tags: Iterable[str]) -> list[str]:
    """Strip, drop empties and case-insensitive duplicates, keep order."""
    if isinstance(tags, str):
        tags = [tags]
    out: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        t = " ".join(str(tag).split())[:100]
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def _merge_tags(old: list[str], new: list[str]) -> list[str]:
    return clean_tags([*old, *new])


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut[limit // 2 :]:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.-") + "…"


# --- calendar events -------------------------------------------------------------


def event_title(paper: Paper) -> str:
    return _shorten(f"Read: {paper.title}", TITLE_MAX)


def event_notes(paper: Paper) -> str:
    assert paper.id is not None
    lines: list[str] = []
    if paper.authors:
        lines.append(", ".join(paper.authors[:6]) + (" et al." if len(paper.authors) > 6 else ""))
    venue_year = " · ".join(x for x in (paper.venue, str(paper.year) if paper.year else None) if x)
    if venue_year:
        lines.append(venue_year)
    links = []
    if paper.url:
        links.append(f"Paper: {paper.url}")
    if paper.pdf_url and paper.pdf_url != paper.url:
        links.append(f"PDF: {paper.pdf_url}")
    if paper.doi and f"doi.org/{paper.doi}" not in (paper.url or ""):
        links.append(f"DOI: https://doi.org/{paper.doi}")
    if paper.pdf_path:
        links.append(f"Offline PDF (Files app): {Path(paper.pdf_path).name}")
    if links:
        lines += ["", *links]
    if paper.abstract:
        lines += ["", _shorten(paper.abstract, ABSTRACT_EXCERPT)]
    lines += ["", event_marker(paper.id)]
    return "\n".join(lines).strip()


def event_spec(paper: Paper, cfg: Config) -> EventSpec:
    start, end = paper.start, paper.end
    if paper.id is None or start is None or end is None:
        raise ValueError("only scheduled, stored papers have calendar events")
    return EventSpec(
        paper_id=paper.id,
        uid=paper.calendar_uid,
        title=event_title(paper),
        start_utc=start.astimezone(UTC),
        end_utc=end.astimezone(UTC),
        url=paper.pdf_url or paper.url,
        notes=event_notes(paper),
        alert_minutes=cfg.calendar.alert_minutes,
    )


def spec_hash(spec: EventSpec) -> str:
    """Hash of an event's content (not its uid), to skip unchanged events on reconcile."""
    content = [
        spec.title,
        spec.start_utc.isoformat(),
        spec.end_utc.isoformat(),
        spec.url,
        spec.notes,
        spec.alert_minutes,
    ]
    return hashlib.sha256(json.dumps(content, ensure_ascii=False).encode()).hexdigest()[:16]


def _wants_event(paper: Paper) -> bool:
    return paper.status == "to_read" and paper.end is not None


# --- metadata helpers ------------------------------------------------------------


def _paper_from_meta(meta: PaperMeta, **fields: Any) -> Paper:
    return Paper(
        id=None,
        title=meta.title.strip()[:500],
        authors=list(meta.authors),
        year=meta.year,
        venue=meta.venue,
        abstract=meta.abstract,
        url=http_url(meta.url),
        pdf_url=http_url(meta.pdf_url),
        doi=meta.doi,
        arxiv_id=meta.arxiv_id,
        **fields,
    )


def _meta_of(paper: Paper) -> PaperMeta:
    return PaperMeta(
        title=paper.title,
        authors=paper.authors,
        year=paper.year,
        venue=paper.venue,
        abstract=paper.abstract,
        url=paper.url,
        pdf_url=paper.pdf_url,
        doi=paper.doi,
        arxiv_id=paper.arxiv_id,
    )


def _minimal_meta(ref: Ref, title: str | None, pdf_url: str | None) -> PaperMeta | None:
    """A placeholder record when lookups fail but a URL (or identifier) or a title is known.
    `pdf_url` is the URL the resolver found to be a PDF, if any."""
    doi = arxiv_id = url = None
    if ref.kind == "doi":
        doi, url = ref.value, f"https://doi.org/{ref.value}"
    elif ref.kind == "arxiv":
        arxiv_id = ref.value
        url, pdf_url = f"https://arxiv.org/abs/{ref.value}", f"https://arxiv.org/pdf/{ref.value}"
    elif ref.kind == "pubmed":
        url = f"https://pubmed.ncbi.nlm.nih.gov/{ref.value}/"
    elif ref.kind == "pmc":
        url = f"https://pmc.ncbi.nlm.nih.gov/articles/{ref.value}/"
    elif ref.kind == "url":
        url = http_url(ref.value)
        if url and not pdf_url and urlparse(url).path.lower().endswith(".pdf"):
            pdf_url = url
    if not (url or title):
        return None
    return PaperMeta(
        title=title or url or "",
        url=url,
        pdf_url=pdf_url,
        doi=doi,
        arxiv_id=arxiv_id,
        matched_by="minimal",
    )


class PaperQueue:
    """The reading queue. Thread-safe; several processes may share one home directory."""

    def __init__(
        self,
        cfg: Config,
        store: Store | None = None,
        resolver: Resolver | None = None,
        calendar: CalendarBackend | None = None,
        library: LibraryProvider | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.cfg = cfg
        self.store = store or Store(cfg.db_path)
        self.resolver = resolver or Resolver()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.tz = scheduler.tz(cfg)
        # Set by the web server when it listens on the LAN; reported by status().
        self.lan_urls: builtins.list[str] = []
        self._calendar = calendar
        self._calendar_loaded = calendar is not None
        self._calendar_error: str | None = None
        self._library = library
        self._library_loaded = library is not None
        self._library_error: str | None = None
        self._init_lock = threading.Lock()
        self._checks: dict[str, tuple[bool, str, datetime]] = {}
        self._attempts: dict[tuple[str, int], datetime] = {}

    # backends -------------------------------------------------------------

    @property
    def calendar(self) -> CalendarBackend | None:
        with self._init_lock:
            if not self._calendar_loaded:
                self._calendar_loaded = True
                if self.cfg.calendar.backend == "off":
                    self._calendar_error = "Calendar sync is off"
                else:
                    try:
                        from .calendar import get_backend

                        self._calendar = get_backend(self.cfg)
                    except Exception as exc:  # includes ImportError; never fatal
                        self._calendar_error = f"calendar backend unavailable: {exc}"
                    if self._calendar is None and self._calendar_error is None:
                        self._calendar_error = "Calendar sync is off"
            return self._calendar

    @property
    def library(self) -> LibraryProvider | None:
        with self._init_lock:
            if not self._library_loaded:
                self._library_loaded = True
                if self.cfg.library.provider == "off":
                    self._library_error = "No reference manager configured"
                else:
                    try:
                        from .libraries import get_provider

                        self._library = get_provider(self.cfg, self.cfg.home)
                    except Exception as exc:  # includes ImportError; never fatal
                        self._library_error = f"{self.cfg.library.provider} unavailable: {exc}"
                    if self._library is None and self._library_error is None:
                        self._library_error = "No reference manager configured"
            return self._library

    def _push_enabled(self, provider: LibraryProvider) -> bool:
        section = getattr(self.cfg, provider.name, None)
        return bool(provider.can_write and getattr(section, "push_new_papers", False))

    # helpers --------------------------------------------------------------

    def _now(self) -> datetime:
        return self.clock().astimezone(UTC)

    def _active(self, paper_id: int) -> Paper:
        paper = self.store.get(paper_id)
        if paper is None or paper.status == "removed":
            raise KeyError(paper_id)
        return paper

    def _busy(self, exclude: Iterable[int] = ()) -> scheduler.Busy:
        skip = set(exclude)
        busy = []
        for p in self.store.list(status="to_read", scheduled=True, order="scheduled"):
            start, end = p.start, p.end
            if p.id not in skip and start is not None and end is not None:
                busy.append((start, end))
        return busy

    def _duration(self, value: int | None) -> int:
        if value is None:
            return self.cfg.schedule.duration_minutes
        if isinstance(value, bool) or not isinstance(value, int) or not 5 <= value <= 600:
            raise ValueError("duration_minutes must be an integer between 5 and 600")
        return value

    def _due(self, kind: str, paper_id: int, now: datetime) -> bool:
        """Rate-limit background retries per paper."""
        last = self._attempts.get((kind, paper_id))
        if last is not None and now - last < RETRY_AFTER:
            return False
        self._attempts[(kind, paper_id)] = now
        return True

    def _meta_changes(self, paper: Paper, meta: PaperMeta) -> dict[str, Any]:
        """Fields to fill in on `paper` from freshly resolved metadata."""
        changes: dict[str, Any] = {}
        if paper.needs_metadata and meta.matched_by not in ("hints", "minimal"):
            changes["needs_metadata"] = False
            if meta.title and meta.title != paper.title:
                changes["title"] = meta.title.strip()[:500]
        for name in ("authors", "year", "venue", "abstract"):
            value = getattr(meta, name)
            if value and not getattr(paper, name):
                changes[name] = value
        for name in ("url", "pdf_url"):
            value = http_url(getattr(meta, name))
            if value and not getattr(paper, name):
                changes[name] = value
        for name in ("doi", "arxiv_id"):
            value = getattr(meta, name)
            if value and not getattr(paper, name):
                other = self.store.find_duplicate(**{name: value})
                if other is None or other.id == paper.id:
                    changes[name] = value
        return changes

    def _find_ref(self, ref: Ref) -> Paper | None:
        """Duplicate check that needs no network."""
        if ref.kind == "arxiv":
            return self.store.find_duplicate(arxiv_id=ref.value)
        if ref.kind == "doi":
            return self.store.find_duplicate(doi=ref.value)
        if ref.kind == "url":
            return self.store.find_duplicate(url=ref.value)
        if ref.kind == "text":
            return self.store.find_duplicate(title=ref.value)
        return None

    def _finish(self, ids: Iterable[int]) -> builtins.list[str]:
        """After a mutation: reconcile the affected events and bump the revision."""
        warnings = self._reconcile(set(ids), force=False)
        self.store.bump_rev()
        return warnings

    # queries --------------------------------------------------------------

    def get(self, id: int) -> Paper:
        return self._active(id)

    def list(
        self, view: str = "queue", query: str | None = None, tag: str | None = None
    ) -> builtins.list[Paper]:
        now = self._now()
        if view == "queue":
            return self.store.list(status="to_read", scheduled=False, query=query, tag=tag)
        if view in ("upcoming", "missed"):
            papers = self.store.list(
                status="to_read", scheduled=True, query=query, tag=tag, order="scheduled"
            )
            return [p for p in papers if p.is_missed(now) == (view == "missed")]
        if view == "read":
            return self.store.list(status="read", query=query, tag=tag, order="read")
        if view == "all":
            return self.store.list(status="all", query=query, tag=tag, order="added")
        raise ValueError(f"unknown view {view!r}; use one of {', '.join(VIEWS)}")

    def free_slots(self, n: int = 5) -> builtins.list[datetime]:
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise ValueError("n must be a non-negative integer")
        duration = self.cfg.schedule.duration_minutes
        busy = self._busy()
        after = self._now()
        out: builtins.list[datetime] = []
        for _ in range(min(n, 100)):
            slot = scheduler.next_free(self.cfg, busy, after, duration)
            if slot is None:
                break
            out.append(slot)
            busy.append((slot, slot + timedelta(minutes=duration)))
        return out

    # mutations ------------------------------------------------------------

    def add(
        self,
        ref: str,
        *,
        title: str | None = None,
        hints: Mapping[str, Any] | None = None,
        priority: int | str = 2,
        tags: Iterable[str] = (),
        notes: str = "",
        when: str | None = None,
        source: str = "manual",
    ) -> AddResult:
        prio = parse_priority(priority)
        tag_list = clean_tags(tags)
        notes = (notes or "").strip()
        hint_map: dict[str, Any] = dict(hints or {})
        title = (title or "").strip() or None
        if title:
            hint_map["title"] = title
        ref = (ref or "").strip() or str(hint_map.get("url") or "").strip() or (title or "")
        if not ref:
            raise ValueError("nothing to add: give an arXiv ID, DOI, URL or title")
        parsed = parse_ref(ref)
        warnings: builtins.list[str] = []

        existing = self._find_ref(parsed)
        meta: PaperMeta | None = None
        if existing is None:
            try:
                meta = self.resolver.resolve(ref, hint_map or None)
            except MetadataError as exc:
                known_title = title or (str(hint_map["title"]) if hint_map.get("title") else None)
                if parsed.kind == "text" and not known_title:
                    raise
                meta = _minimal_meta(parsed, known_title, http_url(exc.pdf_url))
                if meta is None:
                    raise
                warnings.append(f"metadata lookup failed, saved a minimal record: {exc}")

        with self.store.transaction():
            dup = existing or self.store.find_duplicate(
                doi=meta.doi if meta else None,
                arxiv_id=meta.arxiv_id if meta else None,
                url=meta.url if meta else None,
                title=meta.title if meta else None,
            )
            if dup is None:
                assert meta is not None
                paper = self.store.insert(
                    _paper_from_meta(
                        meta,
                        tags=tag_list,
                        priority=prio,
                        notes=notes,
                        source=source,
                        needs_metadata=meta.matched_by in ("hints", "minimal"),
                    )
                )
                created = True
            else:
                paper, created = self._merge_duplicate(dup, meta, prio, tag_list, notes, warnings)
        paper_id = paper.id
        assert paper_id is not None

        if when is not None:
            paper = self._schedule_on_add(paper, when, warnings)
        if dup is None and source not in LIBRARY_SOURCES:
            warnings += self._push(paper)[1]
        warnings += self._finish([paper_id])
        return AddResult(paper=self._active(paper_id), created=created, warnings=warnings)

    def _merge_duplicate(
        self,
        dup: Paper,
        meta: PaperMeta | None,
        prio: int,
        tags: builtins.list[str],
        notes: str,
        warnings: builtins.list[str],
    ) -> tuple[Paper, bool]:
        assert dup.id is not None
        changes: dict[str, Any] = self._meta_changes(dup, meta) if meta else {}
        created = False
        if dup.status == "removed":
            changes |= {"status": "to_read", "read_at": None, "scheduled_start": None,
                        "duration_minutes": None, "priority": prio}
            created = True
        elif prio != 2 and prio != dup.priority:
            changes["priority"] = prio
        if dup.status == "read":
            warnings.append(f"already in the list, marked read on {(dup.read_at or '')[:10]}")
        merged = _merge_tags(dup.tags, tags)
        if merged != dup.tags:
            changes["tags"] = merged
        if notes and notes not in dup.notes:
            changes["notes"] = f"{dup.notes}\n\n{notes}".strip()
        paper = self.store.update(dup.id, **changes) if changes else dup
        return paper, created

    def _schedule_on_add(self, paper: Paper, when: str, warnings: builtins.list[str]) -> Paper:
        assert paper.id is not None
        if paper.status != "to_read":
            warnings.append("not scheduled: the paper is already read")
            return paper
        is_next = when.strip().lower() in ("next", "")
        if paper.scheduled_start and is_next:
            return paper
        try:
            start = scheduler.parse_when(when, self.cfg, self._busy([paper.id]), self._now())
        except ValueError as exc:
            warnings.append(f"not scheduled: {exc}")
            return paper
        if start is None:
            return paper
        return self.store.update(
            paper.id,
            scheduled_start=to_iso(start),
            duration_minutes=paper.duration_minutes or self.cfg.schedule.duration_minutes,
        )

    def _push(self, paper: Paper) -> tuple[bool, builtins.list[str]]:
        """Create the paper in the reference manager, if configured to. Returns (pushed, warnings)."""
        provider = self.library
        if provider is None or paper.library_key or not self._push_enabled(provider):
            return False, []
        assert paper.id is not None
        try:
            key = provider.push(_meta_of(paper), paper.tags)
            if key:
                self.store.update(paper.id, library=provider.name, library_key=key)
        except Exception as exc:  # provider failures are warnings, never errors
            self._note_failure("library", f"{provider.name}: {exc}")
            return False, [f"{provider.name}: could not add to library: {exc}"]
        return bool(key), []

    def update(
        self,
        id: int,
        *,
        title: str | None = None,
        priority: int | str | None = None,
        tags: Iterable[str] | None = None,
        notes: str | None = None,
        url: str | None = None,
        pdf_url: str | None = None,
    ) -> Paper:
        paper = self._active(id)
        changes: dict[str, Any] = {}
        if title is not None:
            if not title.strip():
                raise ValueError("title must not be empty")
            changes["title"] = " ".join(title.split())[:500]
        if priority is not None:
            changes["priority"] = parse_priority(priority)
        if tags is not None:
            changes["tags"] = clean_tags(tags)
        if notes is not None:
            changes["notes"] = notes.strip()
        for name, value in (("url", url), ("pdf_url", pdf_url)):
            if value is not None:
                clean = http_url(value) if value.strip() else None
                if value.strip() and clean is None:
                    raise ValueError(f"{name} must be an http(s) URL")
                changes[name] = clean
        if not changes:
            return paper
        paper = self.store.update(id, **changes)
        for warning in self._finish([id]):
            log.warning("%s", warning)
        return paper

    def schedule(
        self, id: int, when: str | datetime | None, duration_minutes: int | None = None
    ) -> OpResult:
        paper = self._active(id)
        if paper.status != "to_read":
            raise ValueError(f"paper #{id} is already read; mark it unread to schedule it")
        duration = self._duration(duration_minutes or paper.duration_minutes)
        now = self._now()
        busy = self._busy([id])
        if isinstance(when, datetime):
            start: datetime | None = (when if when.tzinfo else when.replace(tzinfo=self.tz)).astimezone(UTC)
        else:
            start = scheduler.parse_when(when, self.cfg, busy, now)
        if start is None:
            return self.unschedule(id)
        warnings: builtins.list[str] = []
        if not scheduler.is_free(start, duration, busy):
            warnings.append("overlaps another scheduled reading session")
        if start + timedelta(minutes=duration) < now:
            warnings.append("that time is in the past")
        self.store.update(id, scheduled_start=to_iso(start), duration_minutes=duration)
        warnings += self._finish([id])
        return OpResult(paper=self._active(id), warnings=warnings)

    def unschedule(self, id: int) -> OpResult:
        paper = self._active(id)
        if paper.scheduled_start is not None:
            self.store.update(id, scheduled_start=None, duration_minutes=None)
        warnings = self._finish([id])
        return OpResult(paper=self._active(id), warnings=warnings)

    def auto_schedule(self, include_missed: bool = True, limit: int | None = None) -> BatchResult:
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
            raise ValueError("limit must be a non-negative integer")
        now = self._now()
        candidates = self.list("queue") + (self.list("missed") if include_missed else [])
        candidates.sort(key=lambda p: (p.priority, p.added_at, p.id or 0))
        if limit is not None:
            candidates = candidates[:limit]
        ids = {p.id for p in candidates if p.id is not None}
        by_id = {p.id: p for p in candidates}
        placed: builtins.list[Paper] = []
        for paper_id, start in scheduler.plan(candidates, self.cfg, self._busy(ids), now):
            duration = by_id[paper_id].duration_minutes or self.cfg.schedule.duration_minutes
            placed.append(
                self.store.update(paper_id, scheduled_start=to_iso(start), duration_minutes=duration)
            )
        placed_ids = {p.id for p in placed if p.id is not None}
        warnings = self._finish(placed_ids) if placed else []
        placed = [self._active(p.id) for p in placed if p.id is not None]
        unplaced = [p for p in candidates if p.id not in placed_ids]
        if unplaced:
            warnings.append(
                f"{len(unplaced)} paper(s) did not fit in the next {self.cfg.schedule.horizon_days} days"
            )
        return BatchResult(papers=placed, unplaced=unplaced, warnings=warnings)

    def mark_read(self, id: int, read: bool = True) -> OpResult:
        paper = self._active(id)
        now = self._now()
        changes: dict[str, Any] = {}
        if read and paper.status != "read":
            changes = {"status": "read", "read_at": to_iso(now)}
            start = paper.start
            if start is not None and start > now:
                changes |= {"scheduled_start": None, "duration_minutes": None}
            if moved := pdfcache.retire(paper, self.cfg):
                changes["pdf_path"] = str(moved)
        elif not read and paper.status == "read":
            changes = {"status": "to_read", "read_at": None}
        if changes:
            self.store.update(id, **changes)
        warnings = self._finish([id])
        return OpResult(paper=self._active(id), warnings=warnings)

    def remove(self, id: int) -> OpResult:
        paper = self._active(id)
        changes: dict[str, Any] = {"status": "removed", "scheduled_start": None, "duration_minutes": None}
        if moved := pdfcache.retire(paper, self.cfg):
            changes["pdf_path"] = str(moved)
        removed = self.store.update(id, **changes)
        warnings = self._finish([id])
        return OpResult(paper=removed, warnings=warnings)

    # library --------------------------------------------------------------

    def sync_library(self) -> SyncResult:
        provider = self.library
        result = SyncResult()
        if provider is None:
            result.warnings.append(self._library_error or "No reference manager configured")
            return result
        try:
            items = provider.pull()
        except Exception as exc:  # provider failures are warnings, never errors
            self._note_failure("library", f"{provider.name}: {exc}")
            result.warnings.append(f"{provider.name}: {exc}")
            return result
        self._checks.pop("library", None)
        result.pulled = len(items)
        changed = False
        for item in items:
            with self.store.transaction():
                meta = item.meta
                dup = self.store.find_duplicate(
                    doi=meta.doi,
                    arxiv_id=meta.arxiv_id,
                    library=provider.name,
                    library_key=item.key,
                    url=meta.url,
                    title=meta.title,
                )
                if dup is None:
                    added = item.added_at
                    if added is not None and added.tzinfo is None:
                        added = added.replace(tzinfo=UTC)
                    self.store.insert(
                        _paper_from_meta(
                            meta,
                            tags=clean_tags(item.tags),
                            source=provider.name,
                            library=provider.name,
                            library_key=item.key,
                            added_at=to_iso(added) if added else "",
                        )
                    )
                    result.created += 1
                    changed = True
                elif dup.status != "removed":
                    assert dup.id is not None
                    changes = self._meta_changes(dup, meta)
                    if dup.library_key is None:
                        changes |= {"library": provider.name, "library_key": item.key}
                    merged = _merge_tags(dup.tags, item.tags)
                    if merged != dup.tags:
                        changes["tags"] = merged
                    if changes:
                        self.store.update(dup.id, **changes)
                        changed = True

        if self._push_enabled(provider):
            for paper in self.store.list(status="to_read"):
                if paper.library_key is None and paper.source not in LIBRARY_SOURCES:
                    pushed, push_warnings = self._push(paper)
                    result.warnings += push_warnings
                    if pushed:
                        result.pushed += 1
                        changed = True
        self.store.set_meta("library_last_sync", to_iso(self._now()))
        if changed:
            self.store.bump_rev()
        return result

    # calendar -------------------------------------------------------------

    def sync_calendar(self, force: bool = False) -> builtins.list[str]:
        """Full reconcile of calendar events with the queue. Returns warnings."""
        if self.calendar is None:
            return [self._calendar_error or "Calendar sync is off"]
        return self._reconcile(None, force=force)

    @contextmanager
    def _calendar_lock(self) -> Iterator[None]:
        fd = os.open(self.cfg.calendar_lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            deadline = time.monotonic() + LOCK_TIMEOUT_S
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise TimeoutError("calendar sync lock is busy") from None
                    time.sleep(0.1)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _reconcile(self, ids: set[int] | None, force: bool) -> builtins.list[str]:
        """Make calendar events match scheduled to_read papers (all papers when `ids` is None)."""
        backend = self.calendar
        if backend is None or ids == set():
            return []
        try:
            with self._calendar_lock():
                return self._reconcile_locked(backend, ids, force)
        except TimeoutError as exc:
            return [f"calendar: {exc}; will retry"]
        except Exception as exc:  # backend failures are warnings, never errors
            self._note_failure("calendar", str(exc))
            return [f"calendar: {exc}"]

    def _reconcile_locked(
        self, backend: CalendarBackend, ids: set[int] | None, force: bool
    ) -> builtins.list[str]:
        now = self._now()
        if ids is None:
            papers = self.store.list(status="all") + self.store.list(status="removed")
        else:
            papers = [p for p in (self.store.get(i) for i in sorted(ids)) if p is not None]

        swept_at = self.store.get_meta("calendar_swept_at")
        sweep = ids is None and (
            force or swept_at is None or now - datetime.fromisoformat(swept_at) >= SWEEP_EVERY
        )
        warnings: builtins.list[str] = []
        in_calendar: dict[str, int] | None = None
        if sweep:
            try:
                in_calendar = backend.list_uids()
            except Exception as exc:  # backend failures are warnings, never errors
                warnings.append(f"calendar: could not list events: {exc}")
                sweep = False
        uids_by_paper: dict[int, builtins.list[str]] = {}
        for uid, paper_id in (in_calendar or {}).items():
            uids_by_paper.setdefault(paper_id, []).append(uid)

        upserts: builtins.list[EventSpec] = []
        hashes: dict[int, str] = {}
        deletes: builtins.list[str] = []
        cleared: builtins.list[int] = []
        keep: set[str] = set()
        for paper in papers:
            assert paper.id is not None
            if _wants_event(paper):
                spec = event_spec(paper, self.cfg)
                if in_calendar is not None:
                    found = uids_by_paper.get(paper.id, [])
                    if spec.uid not in found:
                        # The event was deleted in the calendar (recreate it) or created
                        # without the uid being stored (adopt it).
                        spec.uid = found[0] if found else None
                digest = spec_hash(spec)
                if spec.uid:
                    keep.add(spec.uid)
                if not force and spec.uid and spec.uid == paper.calendar_uid and paper.calendar_hash == digest:
                    continue
                upserts.append(spec)
                hashes[paper.id] = digest
            elif paper.calendar_uid:
                deletes.append(paper.calendar_uid)
                cleared.append(paper.id)
        if in_calendar is not None:
            deletes += [uid for uid in in_calendar if uid not in keep and uid not in deletes]

        if upserts or deletes:
            written = backend.apply(upserts, deletes)
            self._checks.pop("calendar", None)
            for spec in upserts:
                new_uid = written.get(spec.paper_id)
                if new_uid:
                    self.store.update(spec.paper_id, calendar_uid=new_uid, calendar_hash=hashes[spec.paper_id])
                else:
                    warnings.append(f"calendar: could not write the event for #{spec.paper_id}")
            for paper_id in cleared:
                self.store.update(paper_id, calendar_uid=None, calendar_hash=None)
        if sweep:
            self.store.set_meta("calendar_swept_at", to_iso(now))
        return warnings

    # maintenance & status ---------------------------------------------------

    def maintain(self) -> None:
        """Background upkeep: calendar retry, library pull, metadata retry, PDF cache."""
        steps: builtins.list[tuple[str, Callable[[], SyncResult | builtins.list[str]]]] = [
            ("calendar", self.sync_calendar),
            ("library", self.sync_library),
            ("metadata", self._retry_metadata),
            ("pdf cache", self.cache_pdfs),
        ]
        for name, step in steps:
            if name == "calendar" and self.calendar is None:
                continue
            if name == "library" and self.library is None:
                continue
            try:
                outcome = step()
            except Exception:  # keep the maintenance loop alive whatever happens
                log.exception("maintenance step %s failed", name)
                continue
            for warning in outcome.warnings if isinstance(outcome, SyncResult) else outcome:
                log.warning("%s: %s", name, warning)

    def cache_pdfs(self) -> builtins.list[str]:
        """Download/copy PDFs into the iCloud Drive folder for unread papers that lack one."""
        if not self.cfg.pdf.enabled:
            return []
        now = self._now()
        provider = self.library
        cached: builtins.list[int] = []
        for paper in self.store.list(status="to_read"):
            assert paper.id is not None
            if paper.pdf_path or not (paper.pdf_url or (provider and paper.library_key)):
                continue
            if not self._due("pdf", paper.id, now):
                continue
            path = pdfcache.cache(paper, provider, self.cfg, self.resolver.client)
            if path is not None:
                self.store.update(paper.id, pdf_path=str(path))
                cached.append(paper.id)
        return self._finish(cached) if cached else []

    def _retry_metadata(self) -> builtins.list[str]:
        now = self._now()
        updated: builtins.list[int] = []
        for paper in self.store.list(status="all"):
            assert paper.id is not None
            if not paper.needs_metadata or not self._due("metadata", paper.id, now):
                continue
            ref = (
                f"arXiv:{paper.arxiv_id}" if paper.arxiv_id else paper.doi or paper.url or paper.title
            )
            try:
                meta = self.resolver.resolve(ref)
            except MetadataError:
                continue
            changes = self._meta_changes(paper, meta)
            if changes:
                try:
                    self.store.update(paper.id, **changes)
                except sqlite3.IntegrityError:
                    continue
                updated.append(paper.id)
        return self._finish(updated) if updated else []

    def _note_failure(self, kind: str, message: str) -> None:
        self._checks[kind] = (False, message, self._now())

    def _check(self, kind: str) -> tuple[bool, str]:
        now = self._now()
        cached = self._checks.get(kind)
        if cached and now - cached[2] < CHECK_TTL:
            return cached[0], cached[1]
        backend: CalendarBackend | LibraryProvider | None
        backend = self.calendar if kind == "calendar" else self.library
        if backend is None:
            error = self._calendar_error if kind == "calendar" else self._library_error
            return False, error or "off"
        try:
            ok, message = backend.check()
        except Exception as exc:  # backend failures are reported, never raised
            ok, message = False, str(exc)
        self._checks[kind] = (ok, message, now)
        return ok, message

    def status(self) -> dict[str, Any]:
        """Overview for clients. Never contains secrets."""
        lib_ok, lib_msg = self._check("library")
        cal_ok, cal_msg = self._check("calendar")
        provider = self.library
        pdf_dir = self.cfg.pdf.dir
        try:
            shown_dir = f"~/{pdf_dir.relative_to(Path.home())}"
        except ValueError:
            shown_dir = str(pdf_dir)
        s = self.cfg.schedule
        return {
            "rev": self.store.rev(),
            "counts": self.store.counts(self._now()),
            "library": {
                "provider": self.cfg.library.provider,
                "ok": lib_ok,
                "message": lib_msg,
                "can_write": bool(provider and provider.can_write),
                "last_sync": self.store.get_meta("library_last_sync"),
            },
            "calendar": {
                "backend": self.cfg.calendar.backend,
                "name": self.cfg.calendar.apple_calendar,
                "ok": cal_ok,
                "message": cal_msg,
            },
            "pdf": {"enabled": self.cfg.pdf.enabled, "dir": shown_dir},
            "schedule": {
                "days": list(s.days),
                "times": list(s.times),
                "duration_minutes": s.duration_minutes,
                "timezone": self.tz.key,
            },
            "lan": {"enabled": bool(self.lan_urls), "urls": list(self.lan_urls)},
        }
