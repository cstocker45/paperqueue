"""Shared fixtures: isolated homes, mocked HTTP, fake calendar and library backends."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from paperqueue import mediacache, metadata
from paperqueue.config import Config, load_config
from paperqueue.metadata import PaperMeta, Resolver
from paperqueue.service import PaperQueue
from paperqueue.types import CalendarError, EventSpec, LibraryError, LibraryItem, LibraryUnavailable, event_marker

# Monday 16 Nov 2026, 07:00 in Berlin.
NOW = datetime(2026, 11, 16, 6, 0, tzinfo=UTC)

_ENV = (
    "ZOTERO_USER_ID", "ZOTERO_API_KEY", "MENDELEY_CLIENT_ID", "MENDELEY_CLIENT_SECRET",
    "PAPERQUEUE_LIBRARY", "PAPERQUEUE_TOKEN", "PAPERQUEUE_CALENDAR",
)
_FAKE_DNS = {"localhost": "127.0.0.1", "intranet.test": "10.0.0.5"}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for var in _ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PAPERQUEUE_HOME", str(tmp_path / "home"))

    def fake_addresses(host: str) -> list[str]:
        try:
            return [str(metadata.ipaddress.ip_address(host.strip("[]")))]
        except ValueError:
            return [_FAKE_DNS.get(host, "93.184.216.34")]

    monkeypatch.setattr(metadata, "_host_addresses", fake_addresses)


@pytest.fixture(autouse=True)
def kicks(monkeypatch: pytest.MonkeyPatch) -> list[Config]:
    """Never start a real media worker; record the attempts instead."""
    calls: list[Config] = []

    def fake_kick(cfg: Config) -> bool:
        calls.append(cfg)
        return True

    monkeypatch.setattr(mediacache, "kick", fake_kick)
    return calls


@pytest.fixture(autouse=True)
def _no_ytdlp(monkeypatch: pytest.MonkeyPatch) -> None:
    """yt-dlp is never run for real, and Deno/ffmpeg look absent whatever this machine has.
    Tests that need them use the `ytdlp` fixture."""

    def refuse(args: list[str], timeout: float, pass_fds: tuple[int, ...] = ()) -> Any:
        raise AssertionError(f"unexpected yt-dlp call: {args}")

    monkeypatch.setattr(metadata, "run_ytdlp", refuse)
    monkeypatch.setattr(metadata, "find_tool", lambda name: None)


VIDEO_ID = "dQw4w9WgXcQ"


def ytdlp_info(video_id: str = VIDEO_ID, **extra: Any) -> dict[str, Any]:
    info = {
        "id": video_id,
        "title": "How Transformers Work",
        "channel": "Some Channel",
        "uploader": "someone",
        "upload_date": "20240115",
        "duration": 2520,
        "description": "A talk about attention. " * 100,
    }
    info.update(extra)
    return info


@dataclass
class FakeYtDlp:
    """Stands in for `metadata.run_ytdlp`: answers -J probes from `info`, and for downloads
    writes `<id>.<ext>` into the -P directory (or runs `on_download`)."""

    info: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)
    fds: list[tuple[int, ...]] = field(default_factory=list)
    ext: str = "m4a"
    size: int = 1000
    returncode: int = 0
    output: str = ""
    on_download: Callable[[list[str], Path], None] | None = None

    def __call__(self, args: list[str], timeout: float, pass_fds: tuple[int, ...] = ()) -> Any:
        self.calls.append(args)
        self.fds.append(pass_fds)
        video_id = args[-1].rsplit("=", 1)[-1]
        if "-J" in args:
            if video_id in self.info:
                return subprocess.CompletedProcess(args, 0, json.dumps(self.info[video_id]), "")
            return subprocess.CompletedProcess(args, 1, "", f"ERROR: [youtube] {video_id}: Video unavailable")
        tmp = Path(args[args.index("-P") + 1])
        if self.on_download:
            self.on_download(args, tmp)
        elif self.returncode == 0:
            (tmp / f"{video_id}.{self.ext}").write_bytes(b"\0" * self.size)
        return subprocess.CompletedProcess(args, self.returncode, "", self.output)

    @property
    def downloads(self) -> list[list[str]]:
        return [c for c in self.calls if "-J" not in c]


@pytest.fixture
def ytdlp(monkeypatch: pytest.MonkeyPatch) -> FakeYtDlp:
    """A fake yt-dlp with Deno and ffmpeg "installed"."""
    fake = FakeYtDlp(info={VIDEO_ID: ytdlp_info()})
    monkeypatch.setattr(metadata, "run_ytdlp", fake)
    monkeypatch.setattr(metadata, "find_tool", lambda name: f"/opt/homebrew/bin/{name}")
    return fake


Handler = Callable[[httpx.Request], httpx.Response]


class Router:
    """httpx.MockTransport handler: routes by host + path prefix, records every request,
    answers 404 for anything unrouted. No request ever leaves the process."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, Handler]] = []
        self.requests: list[httpx.Request] = []

    def add(self, host: str, path: str, response: httpx.Response | Handler) -> None:
        handler = response if callable(response) else (lambda _req, r=response: r)
        self.routes.insert(0, (host, path, handler))

    def json(self, host: str, path: str, data: Any, status: int = 200) -> None:
        self.add(host, path, lambda _r: httpx.Response(status, json=data))

    def text(self, host: str, path: str, body: str | bytes, ctype: str = "text/html", status: int = 200) -> None:
        self.add(host, path, lambda _r: httpx.Response(status, content=body, headers={"content-type": ctype}))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for host, path, handler in self.routes:
            if request.url.host == host and request.url.path.startswith(path):
                return handler(request)
        return httpx.Response(404, text="not found")

    def hosts(self) -> list[str]:
        return [r.url.host for r in self.requests]

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self), follow_redirects=True)


@pytest.fixture
def router() -> Router:
    return Router()


@pytest.fixture
def resolver(router: Router) -> Resolver:
    return Resolver(router.client())


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    c = load_config(tmp_path / "home")
    c.calendar.backend = "off"
    c.library.provider = "off"
    c.pdf.dir = tmp_path / "pdfs"
    c.media.dir = tmp_path / "Media"
    c.schedule.timezone = "Europe/Berlin"
    return c


@dataclass
class Clock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@dataclass
class FakeCalendar:
    name: str = "fake"
    events: dict[str, EventSpec] = field(default_factory=dict)
    calls: list[tuple[str, Any]] = field(default_factory=list)
    fail: bool = False
    _next: int = 0

    def check(self) -> tuple[bool, str]:
        self.calls.append(("check", None))
        return (not self.fail, "fake calendar")

    def apply(self, upserts: list[EventSpec], deletes: list[str]) -> dict[int, str]:
        self.calls.append(("apply", ([u.paper_id for u in upserts], list(deletes))))
        if self.fail:
            raise CalendarError("Calendar.app is not responding")
        for uid in deletes:
            self.events.pop(uid, None)
        out = {}
        for spec in upserts:
            uid = spec.uid if spec.uid in self.events else self.new_uid()
            self.events[uid] = spec
            out[spec.paper_id] = uid
        return out

    def list_uids(self) -> dict[str, int]:
        self.calls.append(("list_uids", None))
        out = {}
        for uid, spec in self.events.items():
            for line in spec.notes.splitlines():
                if line.startswith("paperqueue:#"):
                    out[uid] = int(line.removeprefix("paperqueue:#"))
        return out

    def new_uid(self) -> str:
        self._next += 1
        return f"UID-{self._next}"

    def add_event(self, paper_id: int | None, title: str = "Other event") -> str:
        """An event created outside paperqueue's normal flow (marked) or by the user (unmarked)."""
        uid = self.new_uid()
        notes = event_marker(paper_id) if paper_id is not None else "Dentist"
        self.events[uid] = EventSpec(
            paper_id=paper_id or 0, uid=uid, title=title, start_utc=NOW, end_utc=NOW,
            url=None, notes=notes, alert_minutes=None,
        )
        return uid


@pytest.fixture
def calendar() -> FakeCalendar:
    return FakeCalendar()


@dataclass
class FakeLibrary:
    name: str = "zotero"
    writable: bool = True
    items: list[LibraryItem] = field(default_factory=list)
    pushed: list[tuple[PaperMeta, list[str]]] = field(default_factory=list)
    pdfs: dict[str, Path] = field(default_factory=dict)
    fail_push: bool = False
    closed: bool = False  # like Zotero desktop not running

    @property
    def can_write(self) -> bool:
        return self.writable

    def check(self) -> tuple[bool, str]:
        return True, "fake library"

    def pull(self) -> list[LibraryItem]:
        if self.closed:
            raise LibraryError("library is not running")
        return list(self.items)

    def push(self, meta: PaperMeta, tags: list[str]) -> str | None:
        if self.closed:
            raise LibraryUnavailable("library is not running")
        if self.fail_push:
            raise RuntimeError("HTTP 403 from library")
        self.pushed.append((meta, tags))
        return f"KEY{len(self.pushed)}"

    def item_link(self, key: str) -> str | None:
        return f"zotero://select/library/items/{key}"

    def pdf_file(self, key: str) -> Path | None:
        return self.pdfs.get(key)


@pytest.fixture
def library() -> FakeLibrary:
    return FakeLibrary()


@pytest.fixture
def queue(cfg: Config, resolver: Resolver, calendar: FakeCalendar, clock: Clock) -> PaperQueue:
    return PaperQueue(cfg, resolver=resolver, calendar=calendar, clock=clock)


# --- upstream API fixtures -------------------------------------------------------


def arxiv_atom(arxiv_id: str = "2502.09775", title: str = "A Study of Things", doi: str | None = None) -> str:
    doi_el = f"<arxiv:doi>{doi}</arxiv:doi>" if doi else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/{arxiv_id}v1</id>
    <published>2025-02-13T18:00:00Z</published>
    <title>{title}:
      With Line Breaks</title>
    <summary>  We study things.
      Results are good.</summary>
    <author><name>Ada Lovelace</name></author>
    <author><name>Alan Turing</name></author>
    {doi_el}
  </entry>
</feed>"""


ARXIV_NOT_FOUND = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><id>http://arxiv.org/api/errors#incorrect_id_format</id><title>Error</title></entry>
</feed>"""


def crossref_work(doi: str = "10.1038/s41467-024-55707-8", **extra: Any) -> dict[str, Any]:
    message = {
        "DOI": doi,
        "title": ["Deep <i>learning</i> of cell states"],
        "author": [{"given": "Marie", "family": "Curie"}, {"name": "The Consortium"}],
        "published-online": {"date-parts": [[2025, 1, 2]]},
        "container-title": ["Nature Communications"],
        "abstract": "<jats:p>Abstract: Cells have states.</jats:p>",
        "link": [{"URL": "https://www.nature.com/articles/x.pdf", "content-type": "application/pdf"}],
        "type": "journal-article",
    }
    message.update(extra)
    return {"status": "ok", "message": message}


def europepmc_result(**extra: Any) -> dict[str, Any]:
    rec = {
        "id": "12345678",
        "source": "MED",
        "pmid": "12345678",
        "pmcid": "PMC1234567",
        "doi": "10.1000/epmc.1",
        "title": "A PubMed paper.",
        "authorList": {"author": [{"firstName": "Rosalind", "lastName": "Franklin", "fullName": "Franklin R"}]},
        "pubYear": "2023",
        "journalInfo": {"journal": {"title": "Journal of Tests"}},
        "abstractText": "An <b>abstract</b> from Europe PMC.",
        "fullTextUrlList": {"fullTextUrl": [
            {"url": "https://europepmc.org/articles/PMC1234567?pdf=render", "documentStyle": "pdf",
             "availabilityCode": "OA"},
        ]},
    }
    rec.update(extra)
    return {"resultList": {"result": [rec]}}


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data))
