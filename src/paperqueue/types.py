"""Shared types and interfaces. Frozen contract between modules — change only with care."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .db import Paper
    from .metadata import PaperMeta


@dataclass
class EventSpec:
    """One calendar event, derived from a scheduled paper. Times are timezone-aware UTC."""

    paper_id: int
    uid: str | None  # existing calendar uid, or None to create
    title: str
    start_utc: datetime
    end_utc: datetime
    url: str | None
    notes: str  # must contain the marker f"paperqueue:#{paper_id}"
    alert_minutes: int | None


def event_marker(paper_id: int) -> str:
    return f"paperqueue:#{paper_id}"


@dataclass
class LibraryItem:
    meta: "PaperMeta"
    key: str
    tags: list[str] = field(default_factory=list)
    added_at: datetime | None = None


@dataclass
class OpResult:
    paper: "Paper"
    warnings: list[str] = field(default_factory=list)


@dataclass
class AddResult:
    paper: "Paper"
    created: bool
    warnings: list[str] = field(default_factory=list)


@dataclass
class BatchResult:
    papers: list["Paper"]
    unplaced: list["Paper"] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class SyncResult:
    pulled: int = 0
    created: int = 0
    pushed: int = 0
    warnings: list[str] = field(default_factory=list)


@runtime_checkable
class CalendarBackend(Protocol):
    name: str

    def check(self) -> tuple[bool, str]:
        """Is the target calendar reachable? Returns (ok, human-readable message)."""

    def apply(self, upserts: list[EventSpec], deletes: list[str]) -> dict[int, str]:
        """Create/update events (in place when uid exists) and delete uids.
        Returns {paper_id: uid} for every successful upsert. Raises CalendarError on total failure."""

    def list_uids(self) -> dict[str, int]:
        """uids of events in the target calendar carrying a paperqueue marker -> paper_id."""


@runtime_checkable
class LibraryProvider(Protocol):
    name: str  # "zotero" | "mendeley"

    @property
    def can_write(self) -> bool: ...

    def check(self) -> tuple[bool, str]: ...

    def pull(self) -> list[LibraryItem]:
        """Items in the configured "To read" collection/folder."""

    def push(self, meta: "PaperMeta", tags: list[str]) -> str | None:
        """Create the item in the collection/folder; return its key (None if not writable)."""

    def item_link(self, key: str) -> str | None: ...

    def pdf_file(self, key: str) -> Path | None:
        """Local path of a PDF the user already holds for this item, if available."""


class CalendarError(Exception):
    pass


class LibraryError(Exception):
    pass


class LibraryUnavailable(LibraryError):
    """The library cannot be written right now (e.g. Zotero desktop is closed); retry later."""
