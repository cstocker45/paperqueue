import base64
import json
import os
import socket
import stat
import threading
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from paperqueue.config import Config
from paperqueue.libraries import mendeley as mod
from paperqueue.libraries.mendeley import MendeleyProvider, doc_to_meta, login, meta_to_doc, save_token
from paperqueue.metadata import PaperMeta
from paperqueue.types import LibraryError, LibraryProvider

NOW = 1_800_000_000.0


def cfg_for(tmp_path):
    cfg = Config(home=tmp_path)
    cfg.mendeley.client_id = "cid"
    cfg.mendeley.client_secret = "csecret"
    return cfg


def write_token(cfg, access="AT", refresh="RT", expires_at=NOW + 3600):
    save_token(cfg.mendeley_token_path, {"access_token": access, "refresh_token": refresh, "expires_at": expires_at})


def basic(request):
    return base64.b64decode(request.headers["Authorization"].split()[1]).decode()


class Api:
    """Minimal fake of api.mendeley.com."""

    def __init__(self, folders=None, docs=None):
        self.folders = folders if folders is not None else [{"id": "F1", "name": "To Read"}]
        self.docs = docs or []
        self.requests = []
        self.reject_token = set()
        self.token_seq = 0

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if path == "/oauth/token":
            assert basic(request) == "cid:csecret"
            form = parse_qs(request.content.decode())
            self.token_seq += 1
            assert form["grant_type"][0] in ("refresh_token", "authorization_code")
            return httpx.Response(200, json={"access_token": f"NEW{self.token_seq}", "expires_in": 3600})
        auth = request.headers.get("Authorization", "")
        if auth.removeprefix("Bearer ") in self.reject_token:
            return httpx.Response(401, json={"message": "expired"})
        if path == "/folders" and request.method == "GET":
            # two pages via Link header
            if request.url.params.get("page") == "2":
                return httpx.Response(200, json=self.folders)
            return httpx.Response(
                200,
                json=[{"id": "X", "name": "Other"}],
                headers={"Link": '<https://api.mendeley.com/folders?page=2>; rel="next"'},
            )
        if path == "/folders" and request.method == "POST":
            assert request.headers["Content-Type"] == "application/vnd.mendeley-folder.1+json"
            self.folders.append({"id": "NEWF", "name": json.loads(request.content)["name"]})
            return httpx.Response(201, json=self.folders[-1])
        if path == "/documents" and request.method == "GET":
            assert request.headers["Accept"] == "application/vnd.mendeley-document.1+json"
            assert request.url.params["view"] == "bib" and request.url.params["limit"] == "500"
            return httpx.Response(200, json=self.docs)
        if path == "/documents" and request.method == "POST":
            assert request.headers["Content-Type"] == "application/vnd.mendeley-document.1+json"
            return httpx.Response(201, json={"id": "DOC1", **json.loads(request.content)})
        if path.startswith("/folders/") and path.endswith("/documents"):
            return httpx.Response(201)
        return httpx.Response(404)


def provider(cfg, api, clock=lambda: NOW):
    return MendeleyProvider(cfg, client=httpx.Client(transport=httpx.MockTransport(api)), clock=clock)


DOC = {
    "id": "D1",
    "title": "Single-cell atlas",
    "type": "journal",
    "authors": [{"first_name": "Jane", "last_name": "Doe"}, {"last_name": "Consortium"}],
    "year": 2023,
    "source": "Cell",
    "identifiers": {"doi": "10.1016/j.cell.2023.01.001", "pmid": "3600000"},
    "abstract": "We map ...",
    "websites": ["https://www.cell.com/x"],
    "tags": ["atlas"],
    "created": "2026-10-01T12:00:00.000Z",
}


def test_doc_mapping():
    m = doc_to_meta(DOC)
    assert m.title == "Single-cell atlas" and m.authors == ["Jane Doe", "Consortium"]
    assert m.year == 2023 and m.venue == "Cell" and m.doi == "10.1016/j.cell.2023.01.001"
    assert m.url == "https://www.cell.com/x" and m.abstract == "We map ..."
    a = doc_to_meta({"title": "t", "identifiers": {"arxiv": "2401.00001"}})
    assert a.arxiv_id == "2401.00001" and a.url == "https://arxiv.org/abs/2401.00001"
    p = doc_to_meta({"title": "t", "identifiers": {"pmid": "123"}})
    assert p.url == "https://pubmed.ncbi.nlm.nih.gov/123/"


def test_meta_to_doc():
    d = meta_to_doc(PaperMeta(title="T", authors=["Ada Lovelace", "Doe, J."], year=2020, doi="10.1/x", url="u"), ["t"])
    assert d["type"] == "journal"
    assert d["authors"] == [{"first_name": "Ada", "last_name": "Lovelace"}, {"first_name": "J.", "last_name": "Doe"}]
    assert d["identifiers"] == {"doi": "10.1/x"} and d["websites"] == ["u"] and d["tags"] == ["t"]


def test_not_logged_in(tmp_path):
    cfg = cfg_for(tmp_path)
    p = provider(cfg, Api())
    assert isinstance(p, LibraryProvider)
    ok, msg = p.check()
    assert not ok and "mendeley login" in msg
    with pytest.raises(LibraryError, match="Not logged in"):
        p.pull()
    assert not p.can_write


def test_missing_app_credentials(tmp_path):
    cfg = Config(home=tmp_path)
    ok, msg = provider(cfg, Api()).check()
    assert not ok and "client_id" in msg and "no longer accept" in msg


def test_pull_with_folder_pagination(tmp_path):
    cfg = cfg_for(tmp_path)
    write_token(cfg)
    api = Api(docs=[DOC])
    p = provider(cfg, api)
    items = p.pull()
    assert len(items) == 1 and items[0].key == "D1" and items[0].tags == ["atlas"]
    assert items[0].added_at.year == 2026
    doc_req = [r for r in api.requests if r.url.path == "/documents"][0]
    assert doc_req.url.params["folder_id"] == "F1"
    assert all(r.headers["Authorization"] == "Bearer AT" for r in api.requests)
    assert p.can_write and p.item_link("D1") is None and p.pdf_file("D1") is None


def test_refresh_on_expiry(tmp_path):
    cfg = cfg_for(tmp_path)
    write_token(cfg, expires_at=NOW - 10)
    api = Api()
    p = provider(cfg, api)
    assert p.check()[0]
    assert api.requests[0].url.path == "/oauth/token"
    form = parse_qs(api.requests[0].content.decode())
    assert form["refresh_token"] == ["RT"]
    saved = json.loads(cfg.mendeley_token_path.read_text())
    assert saved["access_token"] == "NEW1" and saved["refresh_token"] == "RT"
    assert saved["expires_at"] == NOW + 3600
    assert stat.S_IMODE(os.stat(cfg.mendeley_token_path).st_mode) == 0o600


def test_refresh_on_401(tmp_path):
    cfg = cfg_for(tmp_path)
    write_token(cfg)
    api = Api()
    api.reject_token.add("AT")
    p = provider(cfg, api)
    assert p._find_folder() == "F1"
    assert [r.url.path for r in api.requests][:3] == ["/folders", "/oauth/token", "/folders"]
    assert api.requests[2].headers["Authorization"] == "Bearer NEW1"


def test_refresh_failure_is_clear(tmp_path):
    cfg = cfg_for(tmp_path)
    write_token(cfg, expires_at=NOW - 10)

    def handler(request):
        return httpx.Response(400, json={"error": "invalid_grant"})

    ok, msg = provider(cfg, handler).check()
    assert not ok and "refresh failed" in msg and "mendeley login" in msg


def test_push_creates_folder(tmp_path):
    cfg = cfg_for(tmp_path)
    write_token(cfg)
    api = Api(folders=[])
    p = provider(cfg, api)
    ok, msg = p.check()
    assert ok and "will be created" in msg
    assert p.push(PaperMeta(title="T", authors=["Ada Lovelace"], doi="10.1/x"), ["t"]) == "DOC1"
    paths = [(r.method, r.url.path) for r in api.requests]
    assert ("POST", "/folders") in paths
    add = [r for r in api.requests if r.url.path == "/folders/NEWF/documents"][0]
    assert json.loads(add.content) == {"id": "DOC1"}
    created = [r for r in api.requests if r.method == "POST" and r.url.path == "/documents"][0]
    assert json.loads(created.content)["type"] == "journal"


def test_authorize_url(tmp_path):
    cfg = cfg_for(tmp_path)
    u = urlparse(mod.authorize_url(cfg, "S"))
    assert u.netloc == "api.mendeley.com" and u.path == "/oauth/authorize"
    q = parse_qs(u.query)
    assert q == {
        "client_id": ["cid"],
        "redirect_uri": ["http://127.0.0.1:8766/callback"],
        "response_type": ["code"],
        "scope": ["all"],
        "state": ["S"],
    }


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fake_browser(state_override=None, code="CODE"):
    """Simulates the browser following the redirect back to the local callback server."""
    responses = []

    def open_url(url):
        q = parse_qs(urlparse(url).query)
        state = state_override or q["state"][0]
        cb = q["redirect_uri"][0].replace("localhost", "127.0.0.1")

        def go():
            with httpx.Client(transport=httpx.HTTPTransport(), trust_env=False) as c:
                responses.append(c.get(cb, params={"code": code, "state": state}))

        threading.Thread(target=go, daemon=True).start()

    return open_url, responses


def test_login_flow(tmp_path):
    cfg = cfg_for(tmp_path)
    cfg.mendeley.redirect_port = free_port()
    api = Api()
    open_url, responses = fake_browser()
    path = login(cfg, open_url, client=httpx.Client(transport=httpx.MockTransport(api)), timeout=10)
    assert path == cfg.mendeley_token_path
    saved = json.loads(path.read_text())
    assert saved["access_token"] == "NEW1" and "expires_at" in saved
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    form = parse_qs(api.requests[0].content.decode())
    assert form["grant_type"] == ["authorization_code"] and form["code"] == ["CODE"]
    assert form["redirect_uri"] == [cfg.mendeley.redirect_uri]
    assert responses and responses[0].status_code == 200


def test_login_rejects_bad_state(tmp_path):
    cfg = cfg_for(tmp_path)
    cfg.mendeley.redirect_port = free_port()
    api = Api()
    open_url, _ = fake_browser(state_override="forged")
    with pytest.raises(LibraryError, match="state mismatch"):
        login(cfg, open_url, client=httpx.Client(transport=httpx.MockTransport(api)), timeout=10)
    assert api.requests == [] and not cfg.mendeley_token_path.exists()


def test_login_requires_app(tmp_path):
    with pytest.raises(LibraryError, match="client_id"):
        login(Config(home=tmp_path), lambda u: None)
