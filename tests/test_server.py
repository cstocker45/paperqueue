"""Web server: security middleware, JSON API on a real PaperQueue, pages, PDF serving."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from paperqueue.server import COOKIE_NAME, create_app

TOKEN = "test-token"
MUT = {"X-PaperQueue": "1"}
TEST_HOSTS = ["testserver"]  # Starlette's TestClient default Host
TEST_CLIENTS = ["testclient"]  # ...and client address


# --------------------------------------------------------------------------- fakes


class FakePaper:
    def __init__(
        self,
        id: int,
        title: str = "A paper",
        pdf_path: str | None = None,
        media_path: str | None = None,
        url: str | None = None,
        pdf_url: str | None = None,
    ):
        self.id = id
        self.title = title
        self.pdf_path = pdf_path
        self.media_path = media_path
        self.url = url
        self.pdf_url = pdf_url

    def to_dict(self, tz=None):
        return {"id": self.id, "title": self.title, "has_pdf": bool(self.pdf_path)}


class FakeQueue:
    """Just enough of PaperQueue for middleware and file-serving tests."""

    def __init__(self, pdf_dir: Path | None = None, media_dir: Path | None = None):
        self.cfg = SimpleNamespace(
            server=SimpleNamespace(token=TOKEN, port=8765, extra_hosts=["papers.example.ts.net"]),
            pdf=SimpleNamespace(enabled=True, dir=str(pdf_dir) if pdf_dir else ""),
            media=SimpleNamespace(dir=str(media_dir) if media_dir else ""),
            schedule=SimpleNamespace(timezone="UTC"),
        )
        self.papers: dict[int, FakePaper] = {}
        self.calls: list[str] = []

    def status(self):
        return {"rev": 1, "counts": {}, "schedule": {"timezone": "UTC"}}

    def get(self, id):
        if id not in self.papers:
            raise KeyError(id)
        return self.papers[id]

    def list(self, view="queue", query=None, tag=None):
        self.calls.append(f"list:{view}")
        return list(self.papers.values())

    def add(self, *a, **k):
        self.calls.append("add")
        raise ValueError("nope")


def client_for(queue, *, local=True, lan=False, **kw) -> TestClient:
    app = create_app(
        queue, lan=lan, extra_hosts=TEST_HOSTS, trusted_clients=TEST_CLIENTS if local else (), **kw
    )
    return TestClient(app, follow_redirects=False)


# --------------------------------------------------------------------------- security


def test_security_headers_on_every_response():
    c = client_for(FakeQueue())
    for path in ("/", "/api/status", "/static/style.css", "/nope"):
        r = c.get(path)
        csp = r.headers["content-security-policy"]
        assert "default-src 'self'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert "unsafe-inline" not in csp
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize("host", ["evil.example.com", "attacker.localhost.evil.com", ""])
def test_rejects_unknown_host(host):
    c = client_for(FakeQueue())
    r = c.get("/api/status", headers={"host": host})
    assert r.status_code == 400
    assert r.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize(
    "host", ["localhost:8765", "127.0.0.1:8765", "[::1]:8765", "192.168.1.20:8765", "papers.example.ts.net"]
)
def test_allows_known_hosts(host):
    r = client_for(FakeQueue()).get("/api/status", headers={"host": host})
    assert r.status_code == 200


def test_allows_mdns_hostname():
    from paperqueue.server import _local_hostname

    r = client_for(FakeQueue()).get("/api/status", headers={"host": f"{_local_hostname()}:8765"})
    assert r.status_code == 200


def test_testserver_host_not_allowed_by_default():
    app = create_app(FakeQueue(), trusted_clients=TEST_CLIENTS)
    assert TestClient(app).get("/api/status").status_code == 400


def test_remote_client_needs_token():
    c = client_for(FakeQueue(), local=False)
    assert c.get("/api/status").status_code == 401
    assert c.get("/").status_code == 401
    assert c.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/api/status?token=wrong").status_code == 401
    # icons are public so iOS/Chrome can fetch them while installing
    assert c.get("/static/apple-touch-icon.png").status_code == 200


def test_bearer_token_accepted():
    c = client_for(FakeQueue(), local=False)
    assert c.get("/api/status", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_token_query_sets_cookie_and_redirects_without_token():
    c = client_for(FakeQueue(), local=False)
    r = c.get(f"/?view=queue&token={TOKEN}")
    assert r.status_code == 303
    assert r.headers["location"] == "/?view=queue"
    cookie = r.headers["set-cookie"]
    assert f"{COOKIE_NAME}={TOKEN}" in cookie
    assert "httponly" in cookie.lower() and "samesite=lax" in cookie.lower()
    # the cookie now authenticates
    assert c.get("/").status_code == 200
    assert c.get("/api/status").status_code == 200


def test_loopback_client_needs_no_token():
    assert client_for(FakeQueue(), local=True).get("/api/status").status_code == 200


def test_mutations_require_header():
    q = FakeQueue()
    c = client_for(q)
    r = c.post("/api/papers", json={"ref": "x"})
    assert r.status_code == 403
    assert "add" not in q.calls
    assert c.delete("/api/papers/1").status_code == 403
    assert c.patch("/api/papers/1", json={}).status_code == 403
    # with the header the request reaches the service (which raises ValueError -> 400)
    r = c.post("/api/papers", json={"ref": "x"}, headers=MUT)
    assert r.status_code == 400 and r.json()["detail"] == "nope"


def test_remote_mutation_needs_token_and_header():
    c = client_for(FakeQueue(), local=False)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert c.post("/api/papers", json={"ref": "x"}, headers=MUT).status_code == 401
    assert c.post("/api/papers", json={"ref": "x"}, headers=auth).status_code == 403
    assert c.post("/api/papers", json={"ref": "x"}, headers={**auth, **MUT}).status_code == 400


def test_unknown_paper_is_404():
    r = client_for(FakeQueue()).get("/api/papers/999")
    assert r.status_code == 404 and "detail" in r.json()


# --------------------------------------------------------------------------- manifest, QR, pages


def test_manifest_without_lan_has_plain_start_url():
    r = client_for(FakeQueue()).get("/manifest.webmanifest")
    assert r.status_code == 200
    m = r.json()
    assert m["start_url"] == "/" and m["display"] == "standalone"
    assert {i["sizes"] for i in m["icons"]} >= {"192x192", "512x512"}


def test_manifest_with_lan_carries_token():
    r = client_for(FakeQueue(), lan=True).get("/manifest.webmanifest")
    assert r.json()["start_url"] == f"/?token={TOKEN}"


def test_qr_only_with_lan():
    assert client_for(FakeQueue()).get("/api/qr.svg").status_code == 404
    r = client_for(FakeQueue(), lan=True).get("/api/qr.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in r.content


# --------------------------------------------------------------------------- per-item QR


@pytest.fixture
def qr_payloads(monkeypatch):
    """Record what each generated QR code encodes."""
    import segno

    seen: list[str] = []
    real = segno.make

    def spy(content, *a, **k):
        seen.append(content)
        return real(content, *a, **k)

    monkeypatch.setattr(segno, "make", spy)
    return seen


def qr_queue():
    q = FakeQueue()
    q.papers = {
        1: FakePaper(1, url="https://arxiv.org/abs/2502.09775", pdf_url="https://arxiv.org/pdf/2502.09775"),
        2: FakePaper(2, url="https://www.youtube.com/watch?v=aircAruvnKk"),
        3: FakePaper(3, url="javascript:alert(1)", pdf_url="file:///etc/passwd"),
        4: FakePaper(4, url="http://localhost:8765/?token=x", pdf_url="http://127.0.0.1/a.pdf"),
        5: FakePaper(5, url="ftp://example.org/paper.pdf", pdf_url="  "),
        6: FakePaper(6, url=f"https://example.org/?k={TOKEN}"),
        7: FakePaper(7, url="https://user:pw@example.org/paper"),
        8: FakePaper(8),
    }
    return q


@pytest.mark.parametrize("lan", [False, True])
def test_item_qr_encodes_the_stored_public_link_only(qr_payloads, lan):
    c = client_for(qr_queue(), lan=lan)
    r = c.get("/api/papers/1/qr.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert r.headers["cache-control"] == "no-store"
    assert b"<svg" in r.content and TOKEN.encode() not in r.content
    r = c.get("/api/papers/1/qr.svg?target=pdf")
    assert r.status_code == 200
    r = c.get("/api/papers/2/qr.svg?target=url")
    assert r.status_code == 200
    assert qr_payloads == [
        "https://arxiv.org/abs/2502.09775",
        "https://arxiv.org/pdf/2502.09775",
        "https://www.youtube.com/watch?v=aircAruvnKk",
    ]
    for payload in qr_payloads:
        assert TOKEN not in payload and "localhost" not in payload and "token" not in payload


def test_item_qr_is_dark_on_light_with_a_quiet_zone():
    svg = client_for(qr_queue()).get("/api/papers/1/qr.svg").text
    assert 'stroke="#000"' in svg or "#000000" in svg or 'stroke="#000000"' in svg
    assert "#fff" in svg  # light background, not transparent


@pytest.mark.parametrize(
    ("pid", "target"),
    [(3, "url"), (3, "pdf"), (4, "url"), (4, "pdf"), (5, "url"), (5, "pdf"), (6, "url"), (7, "url"),
     (8, "url"), (8, "pdf"), (2, "pdf")],
)
def test_item_qr_refuses_non_public_or_missing_links(qr_payloads, pid, target):
    r = client_for(qr_queue()).get(f"/api/papers/{pid}/qr.svg?target={target}")
    assert r.status_code == 400, (pid, target)
    assert "detail" in r.json()
    assert qr_payloads == []


def test_item_qr_unknown_item_and_bad_target():
    c = client_for(qr_queue())
    assert c.get("/api/papers/999/qr.svg").status_code == 404
    assert c.get("/api/papers/1/qr.svg?target=lan").status_code == 400
    assert c.get("/api/papers/1/qr.svg?target=").status_code == 400


def test_item_qr_needs_token_for_remote_clients():
    c = client_for(qr_queue(), local=False, lan=True)
    assert c.get("/api/papers/1/qr.svg").status_code == 401
    r = c.get("/api/papers/1/qr.svg", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and TOKEN.encode() not in r.content


def test_manifest_colours_follow_theme():
    c = client_for(FakeQueue())
    light = c.get("/manifest.webmanifest").json()
    dark = c.get("/manifest.webmanifest?theme=dark").json()
    assert light["theme_color"] == light["background_color"] == "#f4f7f5"
    assert dark["theme_color"] == dark["background_color"] == "#0b0d0c"
    assert c.get("/manifest.webmanifest?theme=nope").json()["theme_color"] == "#f4f7f5"


def test_status_reports_lan_without_token():
    st = client_for(FakeQueue(), lan=True).get("/api/status").json()
    assert st["lan"]["enabled"] is True
    assert TOKEN not in json.dumps(st)


def test_pages_and_static_load():
    c = client_for(FakeQueue())
    r = c.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert '<script src="/static/app.js"' in r.text
    for path in ("/static/app.js", "/static/style.css", "/static/add.js", "/static/icon.svg", "/static/icon-512.png"):
        assert c.get(path).status_code == 200, path


def test_no_inline_script_or_style_in_pages():
    static = Path(__file__).resolve().parents[1] / "src" / "paperqueue" / "static"
    for name in ("index.html", "add.html"):
        html = (static / name).read_text()
        assert "<style" not in html and " style=" not in html, name
        assert "<script>" not in html and " onclick=" not in html, name
    for name in ("app.js", "add.js"):
        assert "innerHTML" not in (static / name).read_text(), name
        js = (static / name).read_text()
        for banned in ("alert(", "confirm(", "prompt("):
            assert banned not in js, (name, banned)


# --------------------------------------------------------------------------- PDF serving


def test_pdf_served_from_cache_dir(tmp_path):
    pdf_dir = tmp_path / "PaperQueue"
    (pdf_dir / "Read").mkdir(parents=True)
    good = pdf_dir / "Palma 2025 - Something.pdf"
    good.write_bytes(b"%PDF-1.4 test")
    read = pdf_dir / "Read" / "Old 2020 - Done.pdf"
    read.write_bytes(b"%PDF-1.4 old")
    q = FakeQueue(pdf_dir)
    q.papers = {1: FakePaper(1, pdf_path=str(good)), 2: FakePaper(2, pdf_path=str(read))}
    c = client_for(q)
    r = c.get("/api/papers/1/pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("inline")
    assert r.content == b"%PDF-1.4 test"
    assert c.get("/api/papers/2/pdf").status_code == 200


def test_pdf_path_traversal_rejected(tmp_path):
    pdf_dir = tmp_path / "PaperQueue"
    pdf_dir.mkdir()
    secret = tmp_path / "secret.pdf"
    secret.write_bytes(b"%PDF secret")
    notpdf = pdf_dir / "notes.txt"
    notpdf.write_text("hello")
    link = pdf_dir / "link.pdf"
    link.symlink_to(secret)
    q = FakeQueue(pdf_dir)
    q.papers = {
        1: FakePaper(1, pdf_path=str(pdf_dir / ".." / "secret.pdf")),
        2: FakePaper(2, pdf_path=str(secret)),
        3: FakePaper(3, pdf_path=str(link)),
        4: FakePaper(4, pdf_path=str(notpdf)),
        5: FakePaper(5, pdf_path="/etc/passwd"),
        6: FakePaper(6, pdf_path=None),
        7: FakePaper(7, pdf_path=str(tmp_path / "PaperQueueEvil" / "x.pdf")),
    }
    c = client_for(q)
    for pid in q.papers:
        r = c.get(f"/api/papers/{pid}/pdf")
        assert r.status_code == 404, pid
        assert b"secret" not in r.content


def test_pdf_not_served_when_dir_unset(tmp_path):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"%PDF")
    q = FakeQueue(None)
    q.papers = {1: FakePaper(1, pdf_path=str(f))}
    assert client_for(q).get("/api/papers/1/pdf").status_code == 404


# --------------------------------------------------------------------------- real PaperQueue

ARXIV_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/{id}v1</id>
    <published>2017-06-12T17:57:34Z</published>
    <title>{title}</title>
    <summary>We propose a new simple network architecture.</summary>
    <author><name>Ashish Vaswani</name></author>
    <author><name>Noam Shazeer</name></author>
    <author><name>Niki Parmar</name></author>
    <author><name>Jakob Uszkoreit</name></author>
  </entry>
</feed>"""

TITLES = {"1706.03762": "Attention Is All You Need", "2310.06825": "Mistral 7B", "2106.09685": "LoRA"}


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "export.arxiv.org":
        aid = request.url.params.get("id_list", "")
        if aid in TITLES:
            return httpx.Response(
                200,
                text=ARXIV_FEED.format(id=aid, title=TITLES[aid]),
                headers={"content-type": "application/atom+xml"},
            )
    return httpx.Response(404, text="not found")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "pqhome"
    h.mkdir()
    (h / "config.toml").write_text(
        f'[server]\ntoken = "{TOKEN}"\n'
        '[library]\nprovider = "off"\n'
        '[calendar]\nbackend = "off"\n'
        f'[pdf]\nenabled = false\ndir = "{tmp_path / "pdfs"}"\n'
        f'[media]\ndir = "{tmp_path / "Media"}"\n'
        '[schedule]\ntimezone = "Europe/Berlin"\n'
    )
    monkeypatch.setenv("PAPERQUEUE_HOME", str(h))
    monkeypatch.setenv("PAPERQUEUE_LIBRARY", "off")
    monkeypatch.setenv("PAPERQUEUE_CALENDAR", "off")

    def no_network(self, request):
        raise httpx.ConnectError(f"network disabled in tests: {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    return h


@pytest.fixture
def queue(home):
    from paperqueue.config import load_config
    from paperqueue.metadata import Resolver
    from paperqueue.service import PaperQueue

    client = httpx.Client(transport=httpx.MockTransport(_handler))
    return PaperQueue(load_config(home), resolver=Resolver(client=client))


@pytest.fixture
def c(queue):
    return client_for(queue)


def add(c, ref, **body):
    r = c.post("/api/papers", json={"ref": ref, **body}, headers=MUT)
    assert r.status_code in (200, 201), r.text
    return r.json()


def test_status(c):
    st = c.get("/api/status").json()
    for key in ("rev", "counts", "library", "calendar", "pdf", "schedule", "lan"):
        assert key in st, key
    assert st["lan"] == {"enabled": False, "urls": []}
    assert st["server"]["version"]
    assert st["media"]["bytes"] == 0 and st["media"]["files"] == 0
    assert st["calendar"]["alert_minutes"] == 10
    assert TOKEN not in json.dumps(st)


def test_status_media_bytes_counts_existing_files(c, queue, tmp_path):
    p = add(c, "1706.03762", when="none")["paper"]
    media = tmp_path / "Media"
    media.mkdir(exist_ok=True)
    f = media / "Talk [abcdefghijk].m4a"
    f.write_bytes(b"x" * 1234)
    queue.store.update(p["id"], media_path=str(f))
    st = c.get("/api/status").json()
    assert st["media"]["bytes"] == 1234 and st["media"]["files"] == 1
    assert c.get(f"/api/papers/{p['id']}").json()["media_bytes"] == 1234
    f.unlink()
    assert c.get("/api/status").json()["media"]["bytes"] == 0


def test_crud_flow(c):
    rev0 = c.get("/api/status").json()["rev"]
    data = add(c, "1706.03762", priority="high", tags=["nlp"], when="none")
    assert data["created"] is True and isinstance(data["warnings"], list)
    p = data["paper"]
    assert p["title"] == "Attention Is All You Need"
    assert p["priority"] == 1
    assert p["tags"] == ["nlp"]
    assert not p.get("scheduled_start")
    assert c.get("/api/status").json()["rev"] != rev0

    # duplicate add is not created twice
    again = c.post("/api/papers", json={"ref": "arXiv:1706.03762"}, headers=MUT)
    assert again.status_code == 200 and again.json()["created"] is False

    # get, list, search, tag filter
    assert c.get(f"/api/papers/{p['id']}").json()["title"] == p["title"]
    queue_ids = [x["id"] for x in c.get("/api/papers?view=queue").json()["papers"]]
    assert p["id"] in queue_ids
    assert c.get("/api/papers?view=queue&q=attention").json()["papers"]
    assert not c.get("/api/papers?view=queue&q=zzzz-nothing").json()["papers"]
    assert c.get("/api/papers?view=queue&tag=nlp").json()["papers"]
    assert not c.get("/api/papers?view=queue&tag=bio").json()["papers"]

    # update
    r = c.patch(f"/api/papers/{p['id']}", json={"priority": 3, "tags": ["nlp", "classic"], "notes": "hi"}, headers=MUT)
    assert r.status_code == 200
    up = r.json()["paper"]
    assert up["priority"] == 3 and up["tags"] == ["nlp", "classic"] and up["notes"] == "hi"

    # mark read, then back
    r = c.post(f"/api/papers/{p['id']}/read", json={"read": True}, headers=MUT)
    assert r.json()["paper"]["status"] == "read"
    assert p["id"] in [x["id"] for x in c.get("/api/papers?view=read").json()["papers"]]
    r = c.post(f"/api/papers/{p['id']}/read", json={"read": False}, headers=MUT)
    assert r.json()["paper"]["status"] == "to_read"

    # remove (tombstone): hidden everywhere
    r = c.delete(f"/api/papers/{p['id']}", headers=MUT)
    assert r.status_code == 200
    for view in ("queue", "upcoming", "read", "all"):
        assert p["id"] not in [x["id"] for x in c.get(f"/api/papers?view={view}").json()["papers"]]


def test_add_validation(c):
    r = c.post("/api/papers", json={"ref": ""}, headers=MUT)
    assert r.status_code == 400
    r = c.post("/api/papers", json={"ref": "1706.03762", "priority": "urgent"}, headers=MUT)
    assert r.status_code == 400


def test_add_with_hints_and_unresolvable_url(c):
    hints = {"title": "A bookmarklet paper", "authors": ["Ada Lovelace"], "journal": "J. Tests", "date": "2024-01-02"}
    data = add(c, "https://example.org/paper/123", title=hints["title"], hints=hints, when="none")
    assert data["created"] is True
    assert data["paper"]["title"] == "A bookmarklet paper"


def test_schedule_and_unschedule(c):
    p = add(c, "2310.06825", when="none")["paper"]
    when = (datetime.now(timezone.utc) + timedelta(days=3)).replace(microsecond=0).isoformat()
    r = c.post(f"/api/papers/{p['id']}/schedule", json={"when": when, "duration_minutes": 30}, headers=MUT)
    assert r.status_code == 200, r.text
    sp = r.json()["paper"]
    assert sp["scheduled_start"]
    assert datetime.fromisoformat(sp["scheduled_start"]) == datetime.fromisoformat(when)
    assert sp["duration_minutes"] == 30
    assert p["id"] in [x["id"] for x in c.get("/api/papers?view=upcoming").json()["papers"]]

    r = c.post(f"/api/papers/{p['id']}/schedule", json={"when": "next"}, headers=MUT)
    assert r.status_code == 200 and r.json()["paper"]["scheduled_start"]

    r = c.post(f"/api/papers/{p['id']}/schedule", json={"when": "not a time"}, headers=MUT)
    assert r.status_code == 400

    r = c.delete(f"/api/papers/{p['id']}/schedule", headers=MUT)
    assert r.status_code == 200 and not r.json()["paper"]["scheduled_start"]
    assert c.post("/api/papers/9999/schedule", json={"when": "next"}, headers=MUT).status_code == 404


def test_slots(c):
    slots = c.get("/api/slots?n=3").json()["slots"]
    assert len(slots) == 3
    first = datetime.fromisoformat(slots[0]["utc"])
    assert first.utcoffset() == timedelta(0)
    assert datetime.fromisoformat(slots[0]["local"]) == first
    assert first > datetime.now(timezone.utc)


def test_auto_schedule(c):
    ids = [add(c, ref, when="none")["paper"]["id"] for ref in ("1706.03762", "2310.06825", "2106.09685")]
    r = c.post("/api/auto-schedule", json={"include_missed": True}, headers=MUT)
    assert r.status_code == 200, r.text
    data = r.json()
    assert {"papers", "unplaced", "warnings"} <= set(data)
    placed = {p["id"] for p in data["papers"]}
    assert placed == set(ids)
    starts = [p["scheduled_start"] for p in data["papers"]]
    assert len(set(starts)) == len(starts)  # no double booking
    assert not c.get("/api/papers?view=queue").json()["papers"]


def test_add_get_page_never_mutates(c):
    rev = c.get("/api/status").json()["rev"]
    hints = base64.urlsafe_b64encode(json.dumps({"title": "Hi"}).encode()).decode().rstrip("=")
    r = c.get(f"/add?ref=https%3A%2F%2Farxiv.org%2Fabs%2F1706.03762&hints={hints}")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert '<script src="/static/add.js"' in r.text
    assert c.get("/api/status").json()["rev"] == rev
    assert c.get("/api/papers?view=all").json()["papers"] == []


def test_library_and_calendar_sync_endpoints(c):
    r = c.post("/api/library/sync", json={}, headers=MUT)
    assert r.status_code == 200 and "warnings" in r.json()
    r = c.post("/api/calendar/sync", json={}, headers=MUT)
    assert r.status_code == 200 and isinstance(r.json()["warnings"], list)


# --------------------------------------------------------------------------- media serving


def test_csp_allows_youtube_thumbnails_only():
    csp = client_for(FakeQueue()).get("/").headers["content-security-policy"]
    assert "img-src 'self' data: https://i.ytimg.com;" in csp
    assert "media-src" not in csp  # falls back to default-src 'self'
    assert csp.count("https://") == 1


def media_queue(tmp_path):
    media_dir = tmp_path / "Media"
    media_dir.mkdir(parents=True)
    audio = media_dir / "Talk [dQw4w9WgXcQ].m4a"
    audio.write_bytes(bytes(range(256)) * 4)
    video = media_dir / "Talk [abcdefghijk].mp4"
    video.write_bytes(b"\0" * 64)
    q = FakeQueue(media_dir=media_dir)
    q.papers = {1: FakePaper(1, media_path=str(audio)), 2: FakePaper(2, media_path=str(video))}
    return q, media_dir


def test_media_supports_range_requests(tmp_path):
    q, _ = media_queue(tmp_path)
    c = client_for(q)
    r = c.get("/api/papers/1/media")
    assert r.status_code == 200 and len(r.content) == 1024
    assert r.headers["content-type"] == "audio/mp4"
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-disposition"].startswith("inline")
    r = c.get("/api/papers/1/media", headers={"Range": "bytes=100-199"})
    assert r.status_code == 206
    assert r.headers["content-range"] == "bytes 100-199/1024"
    assert r.content == (bytes(range(256)) * 4)[100:200]
    assert c.get("/api/papers/2/media").headers["content-type"] == "video/mp4"


def test_media_confinement_and_suffixes(tmp_path):
    q, media_dir = media_queue(tmp_path)
    secret = tmp_path / "secret.m4a"
    secret.write_bytes(b"secret")
    link = media_dir / "link.m4a"
    link.symlink_to(secret)
    for name in ("notes.txt", "clip.mkv", "page.html"):
        (media_dir / name).write_text("secret")
    q.papers = {
        1: FakePaper(1, media_path=str(media_dir / ".." / "secret.m4a")),
        2: FakePaper(2, media_path=str(secret)),
        3: FakePaper(3, media_path=str(link)),
        4: FakePaper(4, media_path=str(media_dir / "notes.txt")),
        5: FakePaper(5, media_path=str(media_dir / "clip.mkv")),
        6: FakePaper(6, media_path=str(media_dir / "page.html")),
        7: FakePaper(7, media_path=None),
        8: FakePaper(8, media_path=str(tmp_path / "MediaEvil" / "x.m4a")),
        9: FakePaper(9, media_path=str(media_dir / "missing.m4a")),
    }
    c = client_for(q)
    for pid in q.papers:
        r = c.get(f"/api/papers/{pid}/media")
        assert r.status_code == 404, pid
        assert b"secret" not in r.content
    assert c.get("/api/papers/42/media").status_code == 404
    q, _ = media_queue(tmp_path / "unset")
    q.cfg.media.dir = ""
    assert client_for(q).get("/api/papers/1/media").status_code == 404


def test_media_needs_token_from_other_devices(tmp_path):
    q, _ = media_queue(tmp_path)
    remote = client_for(q, local=False)
    assert remote.get("/api/papers/1/media").status_code == 401
    r = remote.get("/api/papers/1/media", headers={"Authorization": f"Bearer {TOKEN}", "Range": "bytes=0-9"})
    assert r.status_code == 206 and len(r.content) == 10


def test_youtube_add_and_set_media(c, ytdlp, kicks, monkeypatch):
    VID = "dQw4w9WgXcQ"
    r = c.post("/api/papers", json={"ref": f"https://youtu.be/{VID}", "media": "audio", "when": "none"}, headers=MUT)
    assert r.status_code == 201, r.text
    p = r.json()["paper"]
    assert (p["kind"], p["video_id"], p["media_mode"], p["media_status"], p["has_media"]) == (
        "video", VID, "audio", "pending", False)
    assert p["duration_seconds"] == 2520 and "media_path" not in p and len(kicks) == 1
    pid = p["id"]
    assert c.post(f"/api/papers/{pid}/media", json={"mode": "link"}).status_code == 403  # no X-PaperQueue
    r = c.post(f"/api/papers/{pid}/media", json={"mode": "link"}, headers=MUT)
    assert r.status_code == 200 and r.json()["paper"]["media_mode"] == "link"
    assert c.post(f"/api/papers/{pid}/media", json={"mode": "dvd"}, headers=MUT).status_code == 400
    assert c.post("/api/papers/999/media", json={"mode": "audio"}, headers=MUT).status_code == 404

    from paperqueue import metadata

    monkeypatch.setattr(metadata, "find_tool", lambda name: None)
    r = c.post(f"/api/papers/{pid}/media", json={"mode": "audio"}, headers=MUT)
    body = r.json()
    assert body["paper"]["media_error"] == "no_deno"
    assert "brew install deno" in body["paper"]["media_fix"] and "brew install deno" in body["warnings"][0]
    slots = c.get(f"/api/slots?n=2&paper_id={pid}").json()["slots"]
    assert len(slots) == 2
    st = c.get("/api/status").json()["media"]
    assert st["deno"] is False and st["failed"][0]["error"] == "no_deno"
