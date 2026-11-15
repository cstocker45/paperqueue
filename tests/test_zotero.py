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
    assert ok and "read-only" in msg


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

    p = make(tmp_path, handler)
    assert not p.can_write
    assert p.push(PaperMeta(title="T"), []) is None


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
