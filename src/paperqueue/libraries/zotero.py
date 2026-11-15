"""Zotero provider.

Reads the "To read" collection through Zotero 7's local API (http://localhost:23119, Zotero desktop must be
running with Settings > Advanced > "Allow other applications on this computer to communicate with Zotero"
enabled) or through the Zotero web API.

Writes go through the Zotero web API when zotero.user_id and zotero.api_key are set. Otherwise, in local mode,
they go through the same localhost "connector" endpoints the Zotero browser extension uses, so no account or
key is needed. The protocol, from Zotero's chrome/content/zotero/xpcom/server/server_connector.js and
saveSession.js:

- every request carries X-Zotero-Connector-API-Version: 3 (a lower version makes Zotero show a dialog), no
  Origin header and a non-browser User-Agent (browser-looking requests without the version header are dropped);
- POST /connector/getSelectedCollection {} -> {"targets": [{"id": "L1" | "C12", "name", "level"}, ...], ...};
- POST /connector/saveItems {"sessionID", "items": [{"id", "itemType", ...}]} -> 201 with an empty body (no
  keys). Items land in whatever collection is selected in Zotero. Item `attachments` are ignored
  (ATTACHMENT_MODE_IGNORE); 409 = SESSION_EXISTS, 500 {"libraryEditable": false} = read-only library;
- POST /connector/updateSession {"sessionID", "target": "C12", "tags": [...]} moves the session's items to the
  target and sets their (manual) tags. 400 = SESSION_NOT_FOUND / COLLECTION_NOT_FOUND. Zotero also selects
  the target collection and the new item in its window;
- POST /connector/saveAttachment?sessionID=... with the PDF bytes (Content-Type: application/pdf) and an
  X-Metadata JSON header {"sessionID", "parentItemID": <the item's "id">, "title", "url"} attaches the PDF;
- POST /connector/saveAttachmentFromResolver {"sessionID", "itemID"} asks Zotero to find an open-access PDF.

paperqueue itself sends nothing anywhere but localhost:23119, apart from fetching the paper's own open-access
PDF URL. When there is no PDF URL, Zotero runs its usual "Find Available PDF" (doi.org, the item URL and
Zotero's open-access lookup, which sees the DOI), as it does for papers saved from the browser.
"""

from __future__ import annotations

import io
import json
import logging
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import httpx

from .. import __version__
from ..metadata import PaperMeta
from ..db import title_key, url_key
from ..types import LibraryError, LibraryItem, LibraryUnavailable

log = logging.getLogger(__name__)

LOCAL_BASE = "http://localhost:23119/api/users/0"
CONNECTOR = "http://localhost:23119/connector"
CONNECTOR_API_VERSION = "3"
PUSH_TAG = "paperqueue"
KEY_WAIT_S = 5.0
KEY_POLL_S = 0.5
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
NOT_OPEN = "Zotero is not open (nothing answers on localhost:23119)"
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


def _unversioned(arxiv_id: str) -> str:
    return re.sub(r"v\d+$", "", arxiv_id.strip()).lower()


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
    is_rxiv = doi.startswith(("10.1101/", "10.64898/"))
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
        self._sleep = time.sleep

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
    def _web_write(self) -> bool:
        return bool(self.cfg.user_id and self.cfg.api_key)

    @property
    def can_write(self) -> bool:
        """Web API with a key, or (no key) the local connector of Zotero desktop."""
        return self._web_write or self.cfg.source == "local"

    def check(self) -> tuple[bool, str]:
        if self.cfg.source == "web" and not self.cfg.user_id:
            return False, "zotero.source is 'web' but zotero.user_id is empty."
        try:
            key = self._find_collection(self._read_base)
        except LibraryError as e:
            return False, str(e)
        where = "Zotero (local API)" if self.cfg.source == "local" else "Zotero web library"
        if key is None:
            if self._web_write:
                return True, (
                    f"Connected to {where}; collection '{self.cfg.collection}' does not exist yet and will be "
                    "created when the first paper is pushed."
                )
            return False, (
                f"Connected to {where}, but there is no collection named '{self.cfg.collection}'. Create it in "
                "Zotero (File > New Collection) or change zotero.collection."
            )
        if self._web_write:
            mode = "read/write (web API)"
        elif self.can_write:
            mode = "read/write (local), via the local connector"
        else:
            mode = "read-only (set zotero.user_id and api_key to push)"
        return True, f"Connected to {where}, collection '{self.cfg.collection}', {mode}."

    def pull(self) -> list[LibraryItem]:
        base = self._read_base
        key = self._find_collection(base)
        if key is None:
            if self._web_write:
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
                    tags=[t["tag"] for t in data.get("tags") or [] if t.get("tag") and t["tag"] != PUSH_TAG],
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
        """Create the item in the collection and return its key. The web API wins when a key is set;
        otherwise the local connector is used. Raises LibraryUnavailable when Zotero is closed."""
        if not self.can_write:
            return None
        if not self._web_write:
            return self._push_local(meta, tags)
        base = self._web_base
        key = self._find_collection(base)
        if key is None:
            key = self._created_key(self._post_web("/collections", [{"name": self.cfg.collection}]), "collection")
            self._collection_key[base] = key
        return self._created_key(self._post_web("/items", [meta_to_item(meta, tags, key)]), "item")

    # -- local connector ------------------------------------------------------------------------

    def _connector(self, path: str, payload: dict | None = None, **kw) -> httpx.Response:
        headers = {"X-Zotero-Connector-API-Version": CONNECTOR_API_VERSION, **kw.pop("headers", {})}
        try:
            if payload is None and "content" not in kw:
                return self._client.get(f"{CONNECTOR}/{path}", headers=headers, **kw)
            if payload is not None:
                kw["content"] = json.dumps(payload).encode()
                headers["Content-Type"] = "application/json"
            return self._client.post(f"{CONNECTOR}/{path}", headers=headers, **kw)
        except httpx.ConnectError:
            raise LibraryUnavailable(NOT_OPEN) from None
        except httpx.HTTPError as e:
            raise LibraryError(f"Zotero connector request failed: {e}") from None

    @staticmethod
    def _connector_error(r: httpx.Response, what: str) -> LibraryError:
        try:
            body = r.json()
        except ValueError:
            body = {}
        if isinstance(body, dict) and body.get("libraryEditable") is False:
            return LibraryError("Zotero's selected library is read-only; select My Library in Zotero and retry.")
        code = body.get("error") if isinstance(body, dict) else None
        return LibraryError(f"Zotero could not {what} (HTTP {r.status_code}{', ' + code if code else ''}).")

    def _local_target(self) -> str:
        """The connector target id ("C12") of the configured collection in the user library."""
        r = self._connector("getSelectedCollection", {})
        if r.status_code != 200:
            raise self._connector_error(r, "list its collections")
        try:
            targets = r.json().get("targets") or []
        except (ValueError, AttributeError):
            raise LibraryError("Zotero returned an unexpected collection list") from None
        want = self.cfg.collection.strip().casefold()
        libraries = 0
        for t in targets:
            if not isinstance(t, dict):
                continue
            if t.get("level", 0) == 0:
                libraries += 1  # the first library is the user library (/users/0 in the local API)
                if libraries > 1:
                    break
                continue
            if str(t.get("id", "")).startswith("C") and str(t.get("name", "")).strip().casefold() == want:
                return str(t["id"])
        raise LibraryError(
            f"There is no collection named '{self.cfg.collection}' in My Library in Zotero. Create it there "
            "(File > New Collection) or change zotero.collection."
        )

    @staticmethod
    def _same(meta: PaperMeta, data: dict) -> bool:
        if data.get("itemType") in SKIP_TYPES:
            return False
        other = item_to_meta(data)
        if meta.doi and other.doi:
            return meta.doi.lower() == other.doi.lower()
        if meta.arxiv_id and other.arxiv_id:
            return _unversioned(meta.arxiv_id) == _unversioned(other.arxiv_id)
        if (mine := url_key(meta.url)) and mine == url_key(data.get("url")):
            return True
        key = title_key(meta.title)
        if key is not None:
            return key == title_key(other.title)
        # A short title ("Deep learning") only matches exactly, and only without identifiers.
        ids = (meta.doi, meta.arxiv_id, other.doi, other.arxiv_id)
        return not any(ids) and meta.title.strip().casefold() == other.title.strip().casefold()

    def _newest_keys(self) -> list[str]:
        items = self._local_items("/items/top", {"sort": "dateAdded", "direction": "desc", "limit": 25})
        return [str(i.get("key")) for i in items if i.get("key")]

    def _new_key(self, meta: PaperMeta, before: set[str]) -> str | None:
        """Key of the item just saved: a recent item that was not there before, with our title."""
        want = meta.title.strip().casefold()
        items = self._local_items("/items/top", {"sort": "dateAdded", "direction": "desc", "limit": 25})
        for it in items:
            data = it.get("data") or {}
            if it.get("key") not in before and str(data.get("title", "")).strip().casefold() == want:
                return it.get("key")
        return self._find_local(meta)

    def _local_items(self, path: str, params: dict) -> list[dict]:
        r = self._get(LOCAL_BASE, path, {**params, "format": "json"})
        try:
            page = r.json()
        except ValueError:
            return []
        return page if isinstance(page, list) else []

    def _find_local(self, meta: PaperMeta) -> str | None:
        """Key of an item in the local library that is the same paper, if any."""
        queries = [q for q in (meta.doi, meta.arxiv_id, meta.title) if q]
        for q in queries:
            for it in self._local_items("/items/top", {"q": q, "qmode": "everything", "limit": 50}):
                if self._same(meta, it.get("data") or {}):
                    return it.get("key") or (it.get("data") or {}).get("key")
        coll = self._find_collection(LOCAL_BASE)
        if coll:
            params = {"sort": "dateAdded", "direction": "desc", "limit": 50}
            for it in self._local_items(f"/collections/{coll}/items/top", params):
                if self._same(meta, it.get("data") or {}):
                    return it.get("key") or (it.get("data") or {}).get("key")
        return None

    def _attach_pdf(self, session: str, item_id: str, meta: PaperMeta) -> None:
        """Best effort: upload the open-access PDF, else let Zotero look for one itself."""
        from ..pdfcache import _download

        if meta.pdf_url:
            buf = io.BytesIO()
            try:
                _download(meta.pdf_url, self._client, buf)
            except Exception as exc:  # noqa: BLE001 - the PDF is optional
                log.info("could not fetch the PDF for Zotero: %s", exc)
            else:
                info = {"sessionID": session, "parentItemID": item_id, "title": "Full Text PDF", "url": meta.pdf_url}
                try:
                    r = self._connector(
                        "saveAttachment",
                        params={"sessionID": session},
                        content=buf.getvalue(),
                        headers={"Content-Type": "application/pdf", "X-Metadata": json.dumps(info)},
                        timeout=60.0,
                    )
                    if r.status_code == 201:
                        return
                    log.info("Zotero did not take the PDF: HTTP %s %s", r.status_code, r.text[:100])
                except LibraryError as exc:
                    log.info("could not send the PDF to Zotero: %s", exc)
        try:
            # Zotero keeps downloading after we stop waiting, so a short timeout only skips its answer.
            self._connector("saveAttachmentFromResolver", {"sessionID": session, "itemID": item_id}, timeout=4.0)
        except LibraryError as exc:
            log.info("Zotero could not find a PDF: %s", exc)

    def _push_local(self, meta: PaperMeta, tags: list[str]) -> str:
        r = self._connector("ping")
        if r.status_code != 200:
            raise LibraryUnavailable(f"Zotero did not answer its connector ping (HTTP {r.status_code})")
        target = self._local_target()
        existing = self._find_local(meta)
        if existing:
            return existing

        before = set(self._newest_keys())
        session = uuid.uuid4().hex
        item_id = uuid.uuid4().hex[:8]
        item = meta_to_item(meta, [], "")
        del item["collections"], item["tags"]  # the connector does not take collection keys; tags go below
        item["id"] = item_id
        r = self._connector("saveItems", {"sessionID": session, "items": [item]})
        if r.status_code != 201:
            raise self._connector_error(r, "save the item")

        # From here on the item exists: never save it again, so a failure only leaves the key unlinked
        # (the next push or pull adopts it by DOI/arXiv id/title).
        all_tags = list(dict.fromkeys([*tags, PUSH_TAG]))
        moved = None
        for _ in range(2):
            try:
                moved = self._connector("updateSession", {"sessionID": session, "target": target, "tags": all_tags})
            except LibraryError:
                moved = None
            if moved is not None and moved.status_code == 200:
                break
        self._attach_pdf(session, item_id, meta)

        deadline = time.monotonic() + KEY_WAIT_S
        while True:
            key = self._new_key(meta, before)
            if key:
                break
            if time.monotonic() >= deadline:
                raise LibraryError("saved in Zotero, but its item key did not show up yet; it is linked on the next sync")
            self._sleep(KEY_POLL_S)
        if moved is None or moved.status_code != 200:
            raise LibraryError(
                f"saved in Zotero, but could not move it into '{self.cfg.collection}'"
                + (f" (HTTP {moved.status_code})" if moved is not None else "")
            )
        return key

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
