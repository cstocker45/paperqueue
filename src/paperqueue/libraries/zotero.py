"""Zotero provider.

Reads the "To read" collection through Zotero 7's local API (http://localhost:23119, Zotero desktop must be
running with Settings > Advanced > "Allow other applications on this computer to communicate with Zotero"
enabled) or through the Zotero web API. Writes always go through the web API, which needs a user id and an
API key with write access (https://www.zotero.org/settings/keys).
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import httpx

from .. import __version__
from ..metadata import PaperMeta
from ..types import LibraryError, LibraryItem

LOCAL_BASE = "http://localhost:23119/api/users/0"
WEB_ROOT = "https://api.zotero.org"
PAGE = 100
SKIP_TYPES = {"note", "attachment", "annotation"}
VENUE_FIELDS = ("publicationTitle", "proceedingsTitle", "repository", "websiteTitle")

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.I)
EXTRA_DOI_RE = re.compile(r"^\s*DOI:\s*(10\.\d{4,9}/\S+)", re.I | re.M)
ARXIV_ID = r"(\d{4}\.\d{4,5}|[a-z][a-z\-]+(?:\.[A-Z]{2})?/\d{7})"
ARXIV_ARCHIVE_RE = re.compile(rf"^arxiv:\s*{ARXIV_ID}", re.I)
ARXIV_URL_RE = re.compile(rf"arxiv\.org/(?:abs|pdf|html)/{ARXIV_ID}", re.I)
ARXIV_EXTRA_RE = re.compile(rf"arxiv:\s*{ARXIV_ID}", re.I)
YEAR_RE = re.compile(r"\b(1[5-9]\d\d|2\d\d\d)\b")

NOT_RUNNING = (
    "Zotero is not reachable at localhost:23119. Start Zotero 7 and enable Settings > Advanced > "
    '"Allow other applications on this computer to communicate with Zotero" (the local API).'
)
LOCAL_DISABLED = (
    "Zotero is running but its local API is disabled. Enable Settings > Advanced > "
    '"Allow other applications on this computer to communicate with Zotero".'
)


def _creator_name(c: dict) -> str:
    if c.get("name"):
        return str(c["name"]).strip()
    return " ".join(p for p in (c.get("firstName", "").strip(), c.get("lastName", "").strip()) if p)


def _arxiv_id(data: dict) -> str | None:
    if m := ARXIV_ARCHIVE_RE.match(data.get("archiveID") or ""):
        return m.group(1)
    if m := ARXIV_URL_RE.search(data.get("url") or ""):
        return m.group(1)
    if m := ARXIV_EXTRA_RE.search(data.get("extra") or ""):
        return m.group(1)
    doi = data.get("DOI") or ""
    if m := re.match(r"10\.48550/arxiv\.(.+)$", doi, re.I):
        return m.group(1)
    return None


def item_to_meta(data: dict) -> PaperMeta:
    """Map a Zotero item's `data` object to PaperMeta."""
    creators = data.get("creators") or []
    authors = [c for c in creators if c.get("creatorType", "author") == "author"] or creators
    year = None
    if m := YEAR_RE.search(str(data.get("date") or "")):
        year = int(m.group(1))
    venue = next((data[f].strip() for f in VENUE_FIELDS if (data.get(f) or "").strip()), None)
    doi = (data.get("DOI") or "").strip() or None
    if not doi and (m := EXTRA_DOI_RE.search(data.get("extra") or "")):
        doi = m.group(1).rstrip(".,;")
    url = (data.get("url") or "").strip() or None
    if not url and doi:
        url = f"https://doi.org/{doi}"
    return PaperMeta(
        title=(data.get("title") or "").strip() or "(untitled)",
        authors=[n for n in (_creator_name(c) for c in authors) if n],
        year=year,
        venue=venue,
        abstract=(data.get("abstractNote") or "").strip() or None,
        url=url,
        doi=doi,
        arxiv_id=_arxiv_id(data),
        matched_by="zotero",
    )


def split_name(name: str) -> dict:
    name = " ".join(name.split())
    if "," in name:
        last, first = (p.strip() for p in name.split(",", 1))
        return {"creatorType": "author", "lastName": last, "firstName": first}
    parts = name.rsplit(" ", 1)
    if len(parts) == 1:
        return {"creatorType": "author", "name": name}
    return {"creatorType": "author", "lastName": parts[1], "firstName": parts[0]}


def meta_to_item(meta: PaperMeta, tags: list[str], collection_key: str) -> dict:
    """Build a Zotero web-API item for PaperMeta."""
    doi = meta.doi or ""
    is_arxiv = bool(meta.arxiv_id) or doi.lower().startswith("10.48550/")
    is_rxiv = doi.startswith("10.1101/")
    item: dict = {
        "title": meta.title,
        "creators": [split_name(a) for a in meta.authors if a.strip()],
        "date": str(meta.year) if meta.year else "",
        "url": meta.url or "",
        "abstractNote": meta.abstract or "",
        "DOI": doi,
        "tags": [{"tag": t} for t in tags],
        "collections": [collection_key],
    }
    if is_arxiv or is_rxiv:
        item["itemType"] = "preprint"
        if is_arxiv:
            item["repository"] = "arXiv"
            if meta.arxiv_id:
                item["archiveID"] = f"arXiv:{meta.arxiv_id}"
        else:
            venue = (meta.venue or "").strip()
            item["repository"] = venue if venue.lower() in ("biorxiv", "medrxiv") else "bioRxiv"
    else:
        item["itemType"] = "journalArticle"
        item["publicationTitle"] = meta.venue or ""
    return item


class ZoteroProvider:
    name = "zotero"

    def __init__(self, cfg, client: httpx.Client | None = None):
        self.cfg = cfg.zotero
        self._client = client or httpx.Client(timeout=15.0, headers={"User-Agent": f"paperqueue/{__version__}"})
        self._collection_key: dict[str, str] = {}

    # -- plumbing -------------------------------------------------------------------------------

    @property
    def _web_base(self) -> str:
        return f"{WEB_ROOT}/users/{self.cfg.user_id}"

    @property
    def _read_base(self) -> str:
        return LOCAL_BASE if self.cfg.source == "local" else self._web_base

    def _headers(self, web: bool) -> dict:
        h = {"Zotero-API-Version": "3"}
        if web and self.cfg.api_key:
            h["Zotero-API-Key"] = self.cfg.api_key
        return h

    def _get(self, base: str, path: str, params: dict | None = None) -> httpx.Response:
        web = base != LOCAL_BASE
        try:
            r = self._client.get(base + path, params=params, headers=self._headers(web))
        except httpx.ConnectError:
            raise LibraryError(NOT_RUNNING if not web else "Could not reach api.zotero.org.") from None
        except httpx.HTTPError as e:
            raise LibraryError(f"Zotero request failed: {e}") from None
        self._raise_for(r, web)
        return r

    def _raise_for(self, r: httpx.Response, web: bool) -> None:
        if r.status_code < 400:
            return
        body = r.text[:200].strip()
        if not web and r.status_code in (403, 404) and ("local api" in body.lower() or "not enabled" in body.lower()):
            raise LibraryError(LOCAL_DISABLED)
        if web and r.status_code in (401, 403):
            raise LibraryError(
                "Zotero web API rejected the key (HTTP %d). Check zotero.user_id / zotero.api_key and that the "
                "key has library access (write access for pushing)." % r.status_code
            )
        raise LibraryError(f"Zotero API error HTTP {r.status_code}: {body}")

    def _paged(self, base: str, path: str, params: dict | None = None) -> list[dict]:
        out: list[dict] = []
        start = 0
        while True:
            r = self._get(base, path, {**(params or {}), "start": start, "limit": PAGE, "format": "json"})
            try:
                page = r.json()
            except ValueError:
                raise LibraryError("Zotero returned invalid JSON") from None
            if not isinstance(page, list):
                raise LibraryError("Zotero returned an unexpected response")
            out.extend(page)
            try:
                total = int(r.headers.get("Total-Results", len(out)))
            except ValueError:
                total = len(out)
            start += len(page)
            if not page or start >= total:
                return out

    def _find_collection(self, base: str) -> str | None:
        if base in self._collection_key:
            return self._collection_key[base]
        want = self.cfg.collection.strip().casefold()
        for c in self._paged(base, "/collections"):
            data = c.get("data") or {}
            if str(data.get("name", "")).strip().casefold() == want:
                self._collection_key[base] = c.get("key") or data.get("key")
                return self._collection_key[base]
        return None

    # -- LibraryProvider ------------------------------------------------------------------------

    @property
    def can_write(self) -> bool:
        return bool(self.cfg.user_id and self.cfg.api_key)

    def check(self) -> tuple[bool, str]:
        if self.cfg.source == "web" and not self.cfg.user_id:
            return False, "zotero.source is 'web' but zotero.user_id is empty."
        try:
            key = self._find_collection(self._read_base)
        except LibraryError as e:
            return False, str(e)
        where = "Zotero (local API)" if self.cfg.source == "local" else "Zotero web library"
        if key is None:
            if self.can_write:
                return True, (
                    f"Connected to {where}; collection '{self.cfg.collection}' does not exist yet and will be "
                    "created when the first paper is pushed."
                )
            return False, (
                f"Connected to {where}, but there is no collection named '{self.cfg.collection}'. Create it in "
                "Zotero (File > New Collection) or change zotero.collection."
            )
        mode = "read/write" if self.can_write else "read-only (set zotero.user_id and api_key to push)"
        return True, f"Connected to {where}, collection '{self.cfg.collection}', {mode}."

    def pull(self) -> list[LibraryItem]:
        base = self._read_base
        key = self._find_collection(base)
        if key is None:
            if self.can_write:
                return []
            raise LibraryError(f"Zotero collection '{self.cfg.collection}' not found.")
        items: list[LibraryItem] = []
        for it in self._paged(base, f"/collections/{key}/items/top"):
            data = it.get("data") or {}
            if data.get("itemType") in SKIP_TYPES:
                continue
            added = None
            if data.get("dateAdded"):
                try:
                    added = datetime.fromisoformat(data["dateAdded"].replace("Z", "+00:00"))
                except ValueError:
                    added = None
            items.append(
                LibraryItem(
                    meta=item_to_meta(data),
                    key=it.get("key") or data.get("key"),
                    tags=[t["tag"] for t in data.get("tags") or [] if t.get("tag")],
                    added_at=added,
                )
            )
        return items

    def _post_web(self, path: str, payload: list[dict]) -> dict:
        try:
            r = self._client.post(self._web_base + path, json=payload, headers=self._headers(True))
        except httpx.HTTPError as e:
            raise LibraryError(f"Could not reach api.zotero.org: {e}") from None
        self._raise_for(r, True)
        try:
            return r.json()
        except ValueError:
            raise LibraryError("Zotero returned invalid JSON") from None

    @staticmethod
    def _created_key(resp: dict, what: str) -> str:
        succ = resp.get("successful") or {}
        if "0" in succ:
            entry = succ["0"]
            return entry.get("key") or (entry.get("data") or {}).get("key")
        if "0" in (resp.get("success") or {}):
            return resp["success"]["0"]
        failed = (resp.get("failed") or {}).get("0") or {}
        raise LibraryError(f"Zotero could not create the {what}: {failed.get('message') or 'unknown error'}")

    def push(self, meta: PaperMeta, tags: list[str]) -> str | None:
        if not self.can_write:
            return None
        base = self._web_base
        key = self._find_collection(base)
        if key is None:
            key = self._created_key(self._post_web("/collections", [{"name": self.cfg.collection}]), "collection")
            self._collection_key[base] = key
        return self._created_key(self._post_web("/items", [meta_to_item(meta, tags, key)]), "item")

    def item_link(self, key: str) -> str | None:
        return f"zotero://select/library/items/{key}"

    def pdf_file(self, key: str) -> Path | None:
        try:
            r = self._client.get(f"{LOCAL_BASE}/items/{key}/children", headers=self._headers(False))
            if r.status_code != 200:
                return None
            for child in r.json():
                data = child.get("data") or {}
                if data.get("itemType") != "attachment" or data.get("contentType") != "application/pdf":
                    continue
                href = ((child.get("links") or {}).get("enclosure") or {}).get("href") or ""
                if not href.startswith("file://"):
                    return None
                path = Path(url2pathname(urlparse(href).path))
                return path if path.is_file() else None
        except (httpx.HTTPError, ValueError, AttributeError, TypeError):
            return None
        return None
