"""Mendeley provider (web API, OAuth2 authorization-code flow).

Mendeley has no local API, so everything goes through https://api.mendeley.com. You need your own app
registration (client id + secret) from https://dev.mendeley.com/myapps.html with the redirect URL
``http://127.0.0.1:<mendeley.redirect_port>/callback``, then run ``paperqueue mendeley login``.

NOTE: Elsevier may no longer accept new Mendeley API app registrations. If dev.mendeley.com refuses to
create an app, this provider cannot be used; use Zotero instead (BibTeX import is planned).
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
from collections.abc import Callable
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .. import __version__
from ..metadata import PaperMeta
from ..types import LibraryError, LibraryItem

API = "https://api.mendeley.com"
DOC_TYPE = "application/vnd.mendeley-document.1+json"
FOLDER_TYPE = "application/vnd.mendeley-folder.1+json"
NOT_LOGGED_IN = "Not logged in to Mendeley. Run `paperqueue mendeley login`."
REFRESH_MARGIN_S = 60


def _new_client() -> httpx.Client:
    return httpx.Client(timeout=20.0, headers={"User-Agent": f"paperqueue/{__version__}"})


def _require_app(cfg) -> None:
    if not (cfg.mendeley.client_id and cfg.mendeley.client_secret):
        raise LibraryError(
            "Mendeley client_id/client_secret are not set. Register an app at https://dev.mendeley.com/myapps.html "
            f"(redirect URL {cfg.mendeley.redirect_uri}) and put them in the [mendeley] config section or in "
            "MENDELEY_CLIENT_ID / MENDELEY_CLIENT_SECRET. Note that Mendeley may no longer accept new app registrations."
        )


def save_token(path: Path, token: dict, now: float | None = None) -> dict:
    """Persist {access_token, refresh_token, expires_at} with mode 0600 (atomic replace)."""
    now = time.time() if now is None else now
    data = {
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token"),
        "expires_at": float(token["expires_at"]) if "expires_at" in token else now + float(token.get("expires_in", 3600)),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, json.dumps(data).encode())
    finally:
        os.close(fd)
    os.replace(tmp, path)
    return data


def _token_request(cfg, client: httpx.Client, form: dict) -> dict:
    try:
        r = client.post(
            f"{API}/oauth/token", data=form, auth=(cfg.mendeley.client_id, cfg.mendeley.client_secret)
        )
    except httpx.HTTPError as e:
        raise LibraryError(f"Could not reach Mendeley: {e}") from None
    if r.status_code != 200:
        raise LibraryError(f"Mendeley token request failed (HTTP {r.status_code}): {r.text[:200]}")
    try:
        tok = r.json()
        tok["access_token"]
    except (ValueError, KeyError, TypeError):
        raise LibraryError("Mendeley returned an invalid token response") from None
    return tok


def authorize_url(cfg, state: str) -> str:
    q = {
        "client_id": cfg.mendeley.client_id,
        "redirect_uri": cfg.mendeley.redirect_uri,
        "response_type": "code",
        "scope": "all",
        "state": state,
    }
    return f"{API}/oauth/authorize?{urlencode(q)}"


def login(
    cfg,
    open_url: Callable[[str], object] | None = None,
    *,
    client: httpx.Client | None = None,
    timeout: float = 300.0,
) -> Path:
    """Run the OAuth2 authorization-code flow and save the token to cfg.mendeley_token_path.

    A one-shot HTTP server on 127.0.0.1:<redirect_port> receives /callback. `open_url(url)` is called with
    the authorize URL once the server is listening (the CLI passes webbrowser.open); if None, the URL is
    printed to stderr.
    """
    _require_app(cfg)
    state = secrets.token_urlsafe(24)
    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            qs = parse_qs(parsed.query)
            if qs.get("state", [""])[0] != state:
                result["error"] = "state mismatch (possible CSRF); try `paperqueue mendeley login` again"
            elif "error" in qs:
                result["error"] = qs["error"][0]
            elif "code" in qs:
                result["code"] = qs["code"][0]
            else:
                result["error"] = "no authorization code in callback"
            ok = "code" in result
            body = (
                "paperqueue: Mendeley login complete. You can close this tab."
                if ok
                else f"paperqueue: Mendeley login failed: {result.get('error')}"
            ).encode()
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep stdout/stderr clean
            pass

    try:
        server = HTTPServer(("127.0.0.1", cfg.mendeley.redirect_port), Handler)
    except OSError as e:
        raise LibraryError(f"Cannot listen on 127.0.0.1:{cfg.mendeley.redirect_port} for the login callback: {e}")
    try:
        url = authorize_url(cfg, state)
        if open_url is None:
            print(f"Open this URL to log in to Mendeley:\n{url}", file=sys.stderr)
        else:
            open_url(url)
        deadline = time.monotonic() + timeout
        while not result:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LibraryError("Timed out waiting for the Mendeley login callback.")
            server.timeout = min(remaining, 1.0)
            server.handle_request()
    finally:
        server.server_close()
    if "error" in result:
        raise LibraryError(f"Mendeley login failed: {result['error']}")

    own = client is None
    client = client or _new_client()
    try:
        tok = _token_request(
            cfg,
            client,
            {"grant_type": "authorization_code", "code": result["code"], "redirect_uri": cfg.mendeley.redirect_uri},
        )
    finally:
        if own:
            client.close()
    save_token(cfg.mendeley_token_path, tok)
    return cfg.mendeley_token_path


def doc_to_meta(doc: dict) -> PaperMeta:
    ids = doc.get("identifiers") or {}
    authors = []
    for a in doc.get("authors") or []:
        name = " ".join(p for p in ((a.get("first_name") or "").strip(), (a.get("last_name") or "").strip()) if p)
        if name:
            authors.append(name)
    websites = doc.get("websites") or []
    doi = ids.get("doi") or None
    url = websites[0] if websites else (f"https://doi.org/{doi}" if doi else None)
    if not url and ids.get("pmid"):
        url = f"https://pubmed.ncbi.nlm.nih.gov/{ids['pmid']}/"
    if not url and ids.get("arxiv"):
        url = f"https://arxiv.org/abs/{ids['arxiv']}"
    year = doc.get("year")
    try:
        year = int(year) if year else None
    except (TypeError, ValueError):
        year = None
    return PaperMeta(
        title=(doc.get("title") or "").strip() or "(untitled)",
        authors=authors,
        year=year,
        venue=(doc.get("source") or "").strip() or None,
        abstract=(doc.get("abstract") or "").strip() or None,
        url=url,
        doi=doi,
        arxiv_id=ids.get("arxiv") or None,
        matched_by="mendeley",
    )


def _split(name: str) -> dict:
    name = " ".join(name.split())
    if "," in name:
        last, first = (p.strip() for p in name.split(",", 1))
    else:
        first, _, last = name.rpartition(" ")
    return {"first_name": first, "last_name": last} if first else {"last_name": last}


def meta_to_doc(meta: PaperMeta, tags: list[str]) -> dict:
    doc: dict = {"type": "journal", "title": meta.title}
    if meta.authors:
        doc["authors"] = [_split(a) for a in meta.authors if a.strip()]
    if meta.year:
        doc["year"] = meta.year
    if meta.venue:
        doc["source"] = meta.venue
    if meta.abstract:
        doc["abstract"] = meta.abstract
    ids = {k: v for k, v in (("doi", meta.doi), ("arxiv", meta.arxiv_id)) if v}
    if ids:
        doc["identifiers"] = ids
    if meta.url:
        doc["websites"] = [meta.url]
    if tags:
        doc["tags"] = list(tags)
    return doc


class MendeleyProvider:
    name = "mendeley"

    def __init__(
        self,
        cfg,
        client: httpx.Client | None = None,
        token_path: Path | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.cfg = cfg
        self.token_path = Path(token_path) if token_path else cfg.mendeley_token_path
        self._client = client or _new_client()
        self._clock = clock
        self._token: dict | None = None
        self._folder_id: str | None = None

    # -- auth -----------------------------------------------------------------------------------

    def _load_token(self) -> dict:
        if self._token is None:
            try:
                self._token = json.loads(self.token_path.read_text())
                self._token["access_token"]
            except FileNotFoundError:
                raise LibraryError(NOT_LOGGED_IN) from None
            except (ValueError, KeyError, TypeError, OSError):
                raise LibraryError("The saved Mendeley token is unreadable. " + NOT_LOGGED_IN) from None
        return self._token

    def _refresh(self) -> dict:
        _require_app(self.cfg)
        tok = self._load_token()
        if not tok.get("refresh_token"):
            raise LibraryError("Mendeley session expired. " + NOT_LOGGED_IN)
        try:
            new = _token_request(
                self.cfg,
                self._client,
                {
                    "grant_type": "refresh_token",
                    "refresh_token": tok["refresh_token"],
                    "redirect_uri": self.cfg.mendeley.redirect_uri,
                },
            )
        except LibraryError as e:
            raise LibraryError(f"Mendeley session expired and refresh failed ({e}). {NOT_LOGGED_IN}") from None
        new.setdefault("refresh_token", tok["refresh_token"])
        self._token = save_token(self.token_path, new, now=self._clock())
        return self._token

    def _access_token(self) -> str:
        tok = self._load_token()
        if float(tok.get("expires_at") or 0) <= self._clock() + REFRESH_MARGIN_S:
            tok = self._refresh()
        return tok["access_token"]

    def _request(self, method: str, url: str, **kw) -> httpx.Response:
        if not url.startswith("http"):
            url = API + url
        headers = dict(kw.pop("headers", {}) or {})
        for attempt in (0, 1):
            token = self._access_token() if attempt == 0 else self._refresh()["access_token"]
            try:
                r = self._client.request(method, url, headers={**headers, "Authorization": f"Bearer {token}"}, **kw)
            except httpx.HTTPError as e:
                raise LibraryError(f"Could not reach Mendeley: {e}") from None
            if r.status_code != 401:
                break
        if r.status_code >= 400:
            raise LibraryError(f"Mendeley API error HTTP {r.status_code}: {r.text[:200]}")
        return r

    def _paged(self, path: str, params: dict, headers: dict | None = None) -> list[dict]:
        out: list[dict] = []
        url: str | None = path
        first = True
        while url:
            r = self._request("GET", url, params=params if first else None, headers=headers)
            first = False
            try:
                page = r.json()
            except ValueError:
                raise LibraryError("Mendeley returned invalid JSON") from None
            out.extend(page if isinstance(page, list) else [])
            url = (r.links.get("next") or {}).get("url")
        return out

    # -- folders --------------------------------------------------------------------------------

    def _find_folder(self) -> str | None:
        if self._folder_id:
            return self._folder_id
        want = self.cfg.mendeley.folder.strip().casefold()
        for f in self._paged("/folders", {"limit": 500}, {"Accept": FOLDER_TYPE}):
            if str(f.get("name", "")).strip().casefold() == want:
                self._folder_id = f["id"]
                return self._folder_id
        return None

    def _ensure_folder(self) -> str:
        fid = self._find_folder()
        if fid:
            return fid
        r = self._request(
            "POST",
            "/folders",
            content=json.dumps({"name": self.cfg.mendeley.folder}),
            headers={"Content-Type": FOLDER_TYPE, "Accept": FOLDER_TYPE},
        )
        self._folder_id = r.json()["id"]
        return self._folder_id

    # -- LibraryProvider ------------------------------------------------------------------------

    @property
    def can_write(self) -> bool:
        return bool(self.cfg.mendeley.client_id and self.token_path.exists())

    def check(self) -> tuple[bool, str]:
        try:
            _require_app(self.cfg)
            fid = self._find_folder()
        except LibraryError as e:
            return False, str(e)
        if fid is None:
            return True, (
                f"Logged in to Mendeley; folder '{self.cfg.mendeley.folder}' does not exist yet and will be "
                "created when the first paper is pushed."
            )
        return True, f"Logged in to Mendeley, folder '{self.cfg.mendeley.folder}'."

    def pull(self) -> list[LibraryItem]:
        fid = self._find_folder()
        if fid is None:
            return []
        docs = self._paged(
            "/documents", {"folder_id": fid, "view": "bib", "limit": 500}, {"Accept": DOC_TYPE}
        )
        items = []
        for d in docs:
            added = None
            if d.get("created"):
                try:
                    added = datetime.fromisoformat(str(d["created"]).replace("Z", "+00:00"))
                except ValueError:
                    pass
            items.append(LibraryItem(meta=doc_to_meta(d), key=d["id"], tags=list(d.get("tags") or []), added_at=added))
        return items

    def push(self, meta: PaperMeta, tags: list[str]) -> str | None:
        fid = self._ensure_folder()
        r = self._request(
            "POST",
            "/documents",
            content=json.dumps(meta_to_doc(meta, tags)),
            headers={"Content-Type": DOC_TYPE, "Accept": DOC_TYPE},
        )
        try:
            doc_id = r.json()["id"]
        except (ValueError, KeyError, TypeError):
            raise LibraryError("Mendeley did not return the new document id") from None
        self._request(
            "POST",
            f"/folders/{fid}/documents",
            content=json.dumps({"id": doc_id}),
            headers={"Content-Type": DOC_TYPE},
        )
        return doc_id

    def item_link(self, key: str) -> str | None:
        return None

    def pdf_file(self, key: str) -> Path | None:
        return None
