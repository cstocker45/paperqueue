import json
from pathlib import Path

import httpx
import pytest

from paperqueue.config import Config
from paperqueue.libraries import get_provider
from paperqueue.libraries.zotero import (
    LOCAL_BASE,
    ZoteroProvider,
    item_to_meta,
    meta_to_item,
    split_name,
)
from paperqueue.metadata import PaperMeta
from paperqueue.types import LibraryError, LibraryProvider

WEB = "https://api.zotero.org/users/123"


def make(tmp_path, handler, **zot):
    cfg = Config(home=tmp_path)
    for k, v in zot.items():
        setattr(cfg.zotero, k, v)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return ZoteroProvider(cfg, client=client)


def coll(key, name):
    return {"key": key, "data": {"key": key, "name": name}}


def paged(request, items):
    start = int(request.url.params.get("start", 0))
    limit = int(request.url.params.get("limit", 25))
    assert limit == 100
    return httpx.Response(200, json=items[start : start + limit], headers={"Total-Results": str(len(items))})


ARXIV_ITEM = {
    "key": "AAAA1111",
    "data": {
        "key": "AAAA1111",
        "itemType": "preprint",
        "title": "Attention Is All You Need",
        "creators": [
            {"creatorType": "author", "firstName": "Ashish", "lastName": "Vaswani"},
            {"creatorType": "editor", "firstName": "Ed", "lastName": "Itor"},
            {"creatorType": "author", "name": "Google Brain"},
        ],
        "date": "2017-06-12",
        "repository": "arXiv",
        "archiveID": "arXiv:1706.03762",
        "url": "https://arxiv.org/abs/1706.03762",
        "abstractNote": "The dominant ...",
        "tags": [{"tag": "transformers"}, {"tag": "nlp", "type": 1}],
        "dateAdded": "2026-11-01T10:00:00Z",
    },
}
JOURNAL_ITEM = {
    "key": "BBBB2222",
    "data": {
        "key": "BBBB2222",
        "itemType": "journalArticle",
        "title": "A cell atlas",
        "creators": [{"creatorType": "author", "firstName": "Jane", "lastName": "Doe"}],
        "date": "March 2021",
        "publicationTitle": "Nature",
        "extra": "DOI: 10.1038/s41586-021-00001-x\nPMID: 123",
        "tags": [],
    },
}
NOTE = {"key": "N1", "data": {"key": "N1", "itemType": "note", "note": "hi"}}
ATTACH = {"key": "P1", "data": {"key": "P1", "itemType": "attachment"}}


def test_item_mapping():
    m = item_to_meta(ARXIV_ITEM["data"])
    assert m.title == "Attention Is All You Need"
    assert m.authors == ["Ashish Vaswani", "Google Brain"]
    assert m.year == 2017 and m.venue == "arXiv" and m.arxiv_id == "1706.03762"
    assert m.url == "https://arxiv.org/abs/1706.03762" and m.abstract == "The dominant ..."
    j = item_to_meta(JOURNAL_ITEM["data"])
    assert j.doi == "10.1038/s41586-021-00001-x" and j.year == 2021 and j.venue == "Nature"
    assert j.url == "https://doi.org/10.1038/s41586-021-00001-x"
    assert item_to_meta({"title": "x", "extra": "arXiv: 2401.12345"}).arxiv_id == "2401.12345"
    assert item_to_meta({"title": "x", "url": "https://arxiv.org/pdf/2401.12345v2"}).arxiv_id == "2401.12345"
    assert item_to_meta({"title": "x", "proceedingsTitle": "NeurIPS"}).venue == "NeurIPS"
    assert item_to_meta({"title": "x", "websiteTitle": "Blog"}).venue == "Blog"


def test_pull_local_with_pagination(tmp_path):
    collections = [coll(f"K{i}", f"Other {i}") for i in range(150)] + [coll("TR", "to READ")]
    items = [ARXIV_ITEM, NOTE, ATTACH, JOURNAL_ITEM]
    seen = []

    def handler(request):
        seen.append(request)
        assert str(request.url).startswith(LOCAL_BASE)
        assert "Zotero-API-Key" not in request.headers
        if request.url.path.endswith("/collections"):
            return paged(request, collections)
        assert request.url.path == "/api/users/0/collections/TR/items/top"
        return paged(request, items)

    p = make(tmp_path, handler)
    assert isinstance(p, LibraryProvider)
    out = p.pull()
    assert [i.key for i in out] == ["AAAA1111", "BBBB2222"]
    assert out[0].tags == ["transformers", "nlp"]
    assert out[0].added_at.year == 2026
    assert sum(r.url.path.endswith("/collections") for r in seen) == 2  # two pages


def test_pull_web_headers(tmp_path):
    def handler(request):
        assert str(request.url).startswith(WEB)
        assert request.headers["Zotero-API-Key"] == "sekret"
        assert request.headers["Zotero-API-Version"] == "3"
        if request.url.path.endswith("/collections"):
            return paged(request, [coll("TR", "To read")])
        return paged(request, [JOURNAL_ITEM])

    p = make(tmp_path, handler, source="web", user_id="123", api_key="sekret")
    assert [i.key for i in p.pull()] == ["BBBB2222"]


def test_check_not_running(tmp_path):
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    ok, msg = make(tmp_path, handler).check()
    assert not ok and "not reachable" in msg and "Allow other applications" in msg


def test_check_local_api_disabled(tmp_path):
    def handler(request):
        return httpx.Response(403, text="Local API is not enabled")

    ok, msg = make(tmp_path, handler).check()
    assert not ok and "local API is disabled" in msg


def test_check_collection_missing(tmp_path):
    def handler(request):
        return paged(request, [coll("K", "Other")])

    ok, msg = make(tmp_path, handler).check()
    assert not ok and "no collection named 'To read'" in msg
    with pytest.raises(LibraryError, match="not found"):
        make(tmp_path, handler).pull()
    ok, msg = make(tmp_path, handler, user_id="1", api_key="k").check()
    assert ok and "will be created" in msg


def test_check_ok(tmp_path):
    ok, msg = make(tmp_path, lambda r: paged(r, [coll("TR", "To read")])).check()
    assert ok and "read/write (local)" in msg and "local connector" in msg
    ok, msg = make(tmp_path, lambda r: paged(r, [coll("TR", "To read")]), source="web", user_id="1").check()
    assert ok and "read-only" in msg
    ok, msg = make(tmp_path, lambda r: paged(r, [coll("TR", "To read")]), user_id="1", api_key="k").check()
    assert ok and "read/write (web API)" in msg


def test_split_name():
    assert split_name("Ashish Vaswani") == {"creatorType": "author", "lastName": "Vaswani", "firstName": "Ashish"}
    assert split_name("Doe, Jane A.") == {"creatorType": "author", "lastName": "Doe", "firstName": "Jane A."}
    assert split_name("Plato") == {"creatorType": "author", "name": "Plato"}
    assert split_name("Ludwig van  Beethoven")["firstName"] == "Ludwig van"


def test_meta_to_item_types():
    a = meta_to_item(PaperMeta(title="T", authors=["A B"], year=2024, arxiv_id="2401.1"), ["x"], "C")
    assert a["itemType"] == "preprint" and a["repository"] == "arXiv" and a["archiveID"] == "arXiv:2401.1"
    assert a["tags"] == [{"tag": "x"}] and a["collections"] == ["C"] and a["date"] == "2024"
    b = meta_to_item(PaperMeta(title="T", doi="10.1101/2024.01.01.123456", venue="bioRxiv"), [], "C")
    assert b["itemType"] == "preprint" and b["repository"] == "bioRxiv" and "archiveID" not in b
    j = meta_to_item(PaperMeta(title="T", doi="10.1038/x", venue="Nature"), [], "C")
    assert j["itemType"] == "journalArticle" and j["publicationTitle"] == "Nature" and j["DOI"] == "10.1038/x"


def test_push_creates_collection_then_item(tmp_path):
    posts = []

    def handler(request):
        assert str(request.url).startswith(WEB)
        assert request.headers["Zotero-API-Key"] == "k"
        if request.method == "GET":
            return paged(request, [coll("K", "Other")])
        body = json.loads(request.content)
        posts.append((request.url.path, body))
        if request.url.path.endswith("/collections"):
            return httpx.Response(200, json={"successful": {"0": {"key": "NEWC"}}, "failed": {}})
        return httpx.Response(200, json={"successful": {"0": {"key": "ITEM1", "data": {}}}, "failed": {}})

    # source=local: reads are local, but push must still go to the web API only.
    p = make(tmp_path, handler, user_id="123", api_key="k")
    meta = PaperMeta(title="Attention", authors=["Ashish Vaswani"], arxiv_id="1706.03762", year=2017)
    assert p.push(meta, ["nlp"]) == "ITEM1"
    assert posts[0] == ("/users/123/collections", [{"name": "To read"}])
    item = posts[1][1][0]
    assert item["collections"] == ["NEWC"] and item["itemType"] == "preprint"
    assert item["creators"] == [{"creatorType": "author", "lastName": "Vaswani", "firstName": "Ashish"}]
    # second push reuses cached collection
    assert p.push(meta, []) == "ITEM1"
    assert sum(path.endswith("/collections") for path, _ in posts) == 1


def test_push_failed(tmp_path):
    def handler(request):
        if request.method == "GET":
            return paged(request, [coll("TR", "To read")])
        return httpx.Response(200, json={"successful": {}, "failed": {"0": {"code": 400, "message": "bad field"}}})

    p = make(tmp_path, handler, user_id="123", api_key="k")
    with pytest.raises(LibraryError, match="bad field"):
        p.push(PaperMeta(title="T"), [])


def test_push_not_writable(tmp_path):
    def handler(request):
        raise AssertionError("no request expected")

    p = make(tmp_path, handler, source="web", user_id="123")
    assert not p.can_write
    assert p.push(PaperMeta(title="T"), []) is None
    assert make(tmp_path, handler).can_write  # local mode writes through the connector


def test_item_link(tmp_path):
    assert make(tmp_path, lambda r: None).item_link("ABC") == "zotero://select/library/items/ABC"


def test_pdf_file(tmp_path):
    pdf = tmp_path / "My Paper.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    children = [
        {"key": "N", "data": {"itemType": "note"}},
        {
            "key": "P",
            "data": {"itemType": "attachment", "contentType": "application/pdf"},
            "links": {"enclosure": {"href": pdf.as_uri(), "type": "application/pdf"}},
        },
    ]

    def handler(request):
        assert request.url.path == "/api/users/0/items/AAAA/children"
        return httpx.Response(200, json=children)

    assert make(tmp_path, handler).pdf_file("AAAA") == pdf


def test_pdf_file_best_effort(tmp_path):
    remote = [
        {
            "data": {"itemType": "attachment", "contentType": "application/pdf"},
            "links": {"enclosure": {"href": "https://api.zotero.org/x/file/view"}},
        }
    ]
    assert make(tmp_path, lambda r: httpx.Response(200, json=remote)).pdf_file("K") is None
    assert make(tmp_path, lambda r: httpx.Response(404)).pdf_file("K") is None

    def down(request):
        raise httpx.ConnectError("refused", request=request)

    assert make(tmp_path, down).pdf_file("K") is None


def test_get_provider(tmp_path):
    cfg = Config(home=tmp_path)
    cfg.library.provider = "off"
    assert get_provider(cfg, tmp_path) is None
    cfg.library.provider = "zotero"
    assert isinstance(get_provider(cfg, tmp_path), ZoteroProvider)
    cfg.zotero.source = "off"
    assert get_provider(cfg, tmp_path) is None
    cfg.library.provider = "mendeley"
    from paperqueue.libraries.mendeley import MendeleyProvider

    p = get_provider(cfg, tmp_path)
    assert isinstance(p, MendeleyProvider) and p.token_path == Path(tmp_path) / "mendeley_token.json"
    cfg.library.provider = "endnote"
    with pytest.raises(LibraryError):
        get_provider(cfg, tmp_path)


# --- local connector (shapes from Zotero's server_connector.js / saveSession.js) -------------------

TARGETS = {
    "libraryID": 1, "libraryName": "My Library", "libraryEditable": True, "filesEditable": True,
    "editable": True, "id": None, "name": "My Library",
    "targets": [
        {"id": "L1", "name": "My Library", "filesEditable": True, "level": 0},
        {"id": "C3", "name": "Other", "filesEditable": True, "level": 1},
        {"id": "C7", "name": "to READ", "filesEditable": True, "level": 1},
        {"id": "L2", "name": "Group", "filesEditable": True, "level": 0},
        {"id": "C9", "name": "To read", "filesEditable": True, "level": 1},
    ],
    "tags": {"L1": []},
}
PDF_BYTES = b"%PDF-1.7\n" + b"0" * 100


class FakeZotero:
    """Zotero desktop on localhost:23119: the connector endpoints plus the read-only local API."""

    def __init__(self, items=None, targets=TARGETS, running=True):
        self.items = list(items or [])  # local API item objects
        self.in_collection: set[str] = {i["key"] for i in self.items}
        self.targets = targets
        self.running = running
        self.requests: list[httpx.Request] = []
        self.sessions: dict[str, list[dict]] = {}
        self.attachments: list[tuple[dict, bytes]] = []
        self.web_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        if url.host == "arxiv.org":
            return httpx.Response(200, content=PDF_BYTES, headers={"content-type": "application/pdf"})
        if url.host == "api.zotero.org":
            self.web_calls += 1
            if request.method == "GET":
                return paged(request, [coll("WC", "To read")])
            return httpx.Response(200, json={"successful": {"0": {"key": "WEBKEY", "data": {}}}, "failed": {}})
        assert url.host == "localhost" and url.port == 23119, url
        if not self.running:
            raise httpx.ConnectError("refused", request=request)
        if url.path.startswith("/connector/"):
            assert request.headers["X-Zotero-Connector-API-Version"] == "3"
            assert "origin" not in request.headers
            assert not request.headers.get("user-agent", "").startswith("Mozilla/")
            return self.connector(url.path.removeprefix("/connector/"), request)
        return self.local_api(request)

    def connector(self, name: str, request: httpx.Request) -> httpx.Response:
        if name == "ping":
            assert request.method == "GET"
            return httpx.Response(200, text="<!DOCTYPE html><html><body>Zotero is running</body></html>")
        if name == "saveAttachment":
            assert request.headers["Content-Type"] == "application/pdf"
            meta = json.loads(request.headers["X-Metadata"])
            assert meta["sessionID"] in self.sessions and request.url.params["sessionID"] == meta["sessionID"]
            self.attachments.append((meta, request.content))
            return httpx.Response(201)
        assert request.method == "POST" and request.headers["Content-Type"] == "application/json"
        body = json.loads(request.content)
        if name == "getSelectedCollection":
            return httpx.Response(200, json=self.targets)
        if name == "saveItems":
            if body["sessionID"] in self.sessions:
                return httpx.Response(409, json={"error": "SESSION_EXISTS"})
            self.sessions[body["sessionID"]] = []
            for n, item in enumerate(body["items"]):
                assert "collections" not in item and item["id"]
                key = f"NEW{len(self.items) + n}"
                data = {k: v for k, v in item.items() if k != "id"} | {"key": key, "tags": []}
                self.items.append({"key": key, "data": data})
                self.sessions[body["sessionID"]].append(data)
            return httpx.Response(201, headers={"Content-Type": "application/json"})
        if name == "updateSession":
            if body["sessionID"] not in self.sessions:
                return httpx.Response(400, json={"error": "SESSION_NOT_FOUND"})
            assert body["target"].startswith("C") and isinstance(body["tags"], list)
            for data in self.sessions[body["sessionID"]]:
                data["tags"] = [{"tag": t} for t in body["tags"]]
                self.in_collection.add(data["key"])
            self.moved_to = body["target"]
            return httpx.Response(200, json={})
        if name == "saveAttachmentFromResolver":
            return httpx.Response(500, text="Failed to save an attachment")
        raise AssertionError(f"unexpected connector call {name}")

    def local_api(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and str(request.url).startswith(LOCAL_BASE)
        path = request.url.path.removeprefix("/api/users/0")
        if path == "/collections":
            return paged(request, [coll("TR", "To read")])
        if path == "/collections/TR/items/top":
            return httpx.Response(200, json=[i for i in self.items if i["key"] in self.in_collection])
        if path == "/items/top" and "q" not in request.url.params:
            assert request.url.params["sort"] == "dateAdded" and request.url.params["direction"] == "desc"
            return httpx.Response(200, json=self.items[::-1][: int(request.url.params["limit"])])
        if path == "/items/top":
            q = request.url.params["q"].lower()
            assert request.url.params["qmode"] == "everything"
            hits = [i for i in self.items if q in json.dumps(i["data"]).lower()]
            return httpx.Response(200, json=hits)
        raise AssertionError(f"unexpected local API call {path}")

    def calls(self, name: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == f"/connector/{name}"]


def local(tmp_path, zot, **kw):
    p = make(tmp_path, zot, **kw)
    p._sleep = lambda s: None
    return p


ARXIV_META = PaperMeta(
    title="Attention Is All You Need", authors=["Ashish Vaswani", "Noam Shazeer"], year=2017,
    abstract="The dominant ...", url="https://arxiv.org/abs/1706.03762",
    pdf_url="https://arxiv.org/pdf/1706.03762", arxiv_id="1706.03762", doi="10.48550/arXiv.1706.03762",
)


def test_local_push_creates_item_and_moves_it(tmp_path):
    zot = FakeZotero()
    p = local(tmp_path, zot)
    key = p.push(ARXIV_META, ["nlp"])
    assert key == "NEW0"
    [save] = zot.calls("saveItems")
    body = json.loads(save.content)
    [item] = body["items"]
    assert item["itemType"] == "preprint" and item["repository"] == "arXiv"
    assert item["archiveID"] == "arXiv:1706.03762" and item["DOI"] == "10.48550/arXiv.1706.03762"
    assert item["title"] == "Attention Is All You Need" and item["date"] == "2017"
    assert item["url"] == "https://arxiv.org/abs/1706.03762" and item["abstractNote"] == "The dominant ..."
    assert item["creators"][0] == {"creatorType": "author", "lastName": "Vaswani", "firstName": "Ashish"}
    assert "tags" not in item and "collections" not in item
    [update] = zot.calls("updateSession")
    upd = json.loads(update.content)
    # The case-insensitive match in My Library, not the group library's "To read".
    assert upd == {"sessionID": body["sessionID"], "target": "C7", "tags": ["nlp", "paperqueue"]}
    [(meta, pdf)] = zot.attachments
    assert pdf == PDF_BYTES
    assert meta == {"sessionID": body["sessionID"], "parentItemID": item["id"], "title": "Full Text PDF",
                    "url": "https://arxiv.org/pdf/1706.03762"}
    assert zot.calls("saveAttachmentFromResolver") == []
    assert {r.url.host for r in zot.requests} == {"localhost", "arxiv.org"}
    # A second push of the same paper adopts the item instead of saving it again.
    assert p.push(ARXIV_META, []) == "NEW0" and len(zot.calls("saveItems")) == 1


def test_local_push_journal_without_pdf_asks_zotero(tmp_path):
    zot = FakeZotero()
    meta = PaperMeta(title="A cell atlas of the human body", authors=["Jane Doe"], doi="10.1038/s41586-x",
                     venue="Nature")
    assert local(tmp_path, zot).push(meta, []) == "NEW0"
    item = json.loads(zot.calls("saveItems")[0].content)["items"][0]
    assert item["itemType"] == "journalArticle" and item["publicationTitle"] == "Nature"
    [res] = zot.calls("saveAttachmentFromResolver")
    assert json.loads(res.content) == {"sessionID": json.loads(zot.calls("saveItems")[0].content)["sessionID"],
                                       "itemID": item["id"]}


def test_local_push_dedupes(tmp_path):
    existing = {"key": "OLD1", "data": {"key": "OLD1", "itemType": "preprint", "title": "Attention is all you need",
                                        "archiveID": "arXiv:1706.03762v5", "tags": []}}
    zot = FakeZotero(items=[existing])
    zot.in_collection = set()  # elsewhere in the library: still the same paper
    assert local(tmp_path, zot).push(ARXIV_META, []) == "OLD1"
    assert zot.calls("saveItems") == [] and zot.calls("updateSession") == []
    by_title = {"key": "OLD2", "data": {"key": "OLD2", "itemType": "journalArticle",
                                        "title": "A Cell Atlas of the Human Body!", "tags": []}}
    zot = FakeZotero(items=[by_title])
    assert local(tmp_path, zot).push(PaperMeta(title="A cell atlas of the human body"), []) == "OLD2"
    # Same title, different DOI: a different work.
    by_title["data"]["DOI"] = "10.1/other"
    zot = FakeZotero(items=[by_title])
    assert local(tmp_path, zot).push(PaperMeta(title="A cell atlas of the human body", doi="10.1/mine"), []) != "OLD2"


def test_local_push_missing_collection(tmp_path):
    targets = {**TARGETS, "targets": [TARGETS["targets"][0], TARGETS["targets"][1], *TARGETS["targets"][3:]]}
    zot = FakeZotero(targets=targets)
    with pytest.raises(LibraryError, match="no collection named 'To read'.*Create it"):
        local(tmp_path, zot).push(ARXIV_META, [])
    assert zot.calls("saveItems") == []


def test_local_push_zotero_closed(tmp_path):
    from paperqueue.types import LibraryUnavailable

    zot = FakeZotero(running=False)
    with pytest.raises(LibraryUnavailable, match="not open"):
        local(tmp_path, zot).push(ARXIV_META, [])


def test_local_push_errors(tmp_path):
    zot = FakeZotero()
    zot.connector = lambda name, req: (
        httpx.Response(200, text="ok") if name == "ping"
        else httpx.Response(200, json=TARGETS) if name == "getSelectedCollection"
        else httpx.Response(500, json={"libraryEditable": False})
    )
    with pytest.raises(LibraryError, match="read-only"):
        local(tmp_path, zot).push(ARXIV_META, [])


def test_local_push_key_not_found(tmp_path):
    zot = FakeZotero()
    zot.local_api = lambda request: httpx.Response(200, json=[]) if "items" in request.url.path else paged(
        request, [coll("TR", "To read")])
    p = local(tmp_path, zot)
    import paperqueue.libraries.zotero as z

    z_wait = z.KEY_WAIT_S
    z.KEY_WAIT_S = 0.0
    try:
        with pytest.raises(LibraryError, match="linked on the next sync"):
            p.push(ARXIV_META, [])
    finally:
        z.KEY_WAIT_S = z_wait
    assert len(zot.calls("saveItems")) == 1


def test_web_push_takes_precedence(tmp_path):
    zot = FakeZotero()
    p = local(tmp_path, zot, user_id="123", api_key="k")
    assert p.push(ARXIV_META, []) == "WEBKEY"
    assert zot.web_calls == 2 and not any(r.url.path.startswith("/connector/") for r in zot.requests)


def test_pull_hides_push_tag(tmp_path):
    zot = FakeZotero()
    p = local(tmp_path, zot)
    key = p.push(ARXIV_META, ["nlp"])
    [item] = p.pull()
    assert item.key == key and item.tags == ["nlp"]


def test_local_push_short_title_without_ids_is_not_duplicated(tmp_path):
    zot = FakeZotero(items=[{"key": "X1", "data": {"key": "X1", "itemType": "journalArticle",
                                                   "title": "Deep learning", "DOI": "10.1/other"}}])
    p = local(tmp_path, zot)
    meta = PaperMeta(title="Deep learning", authors=["Y LeCun"])
    key = p.push(meta, [])
    assert key == "NEW1"  # the new item, not the same-titled one with a DOI
    assert p.push(meta, []) == "NEW1" and len(zot.calls("saveItems")) == 1
