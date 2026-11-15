"""MCP server tools, exercised through build_server() on a real PaperQueue in a temp home."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from paperqueue import mcp_server
from paperqueue.mcp_server import build_server, human_time, paper_summary, short_authors

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

TITLES = {"1706.03762": "Attention Is All You Need", "2310.06825": "Mistral 7B"}


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "export.arxiv.org":
        aid = request.url.params.get("id_list", "")
        if aid in TITLES:
            return httpx.Response(
                200, text=ARXIV_FEED.format(id=aid, title=TITLES[aid]),
                headers={"content-type": "application/atom+xml"},
            )
    return httpx.Response(404, text="not found")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "pqhome"
    h.mkdir()
    (h / "config.toml").write_text(
        '[server]\ntoken = "test-token"\n'
        '[library]\nprovider = "off"\n'
        '[calendar]\nbackend = "off"\n'
        '[pdf]\nenabled = false\n'
        '[schedule]\ntimezone = "Europe/Berlin"\n'
    )
    monkeypatch.setenv("PAPERQUEUE_HOME", str(h))
    monkeypatch.setenv("PAPERQUEUE_LIBRARY", "off")
    monkeypatch.setenv("PAPERQUEUE_CALENDAR", "off")

    def no_network(self, request):
        raise httpx.ConnectError(f"network disabled in tests: {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    return h


def make_queue(home):
    from paperqueue.config import load_config
    from paperqueue.metadata import Resolver
    from paperqueue.service import PaperQueue

    client = httpx.Client(transport=httpx.MockTransport(_handler))
    return PaperQueue(load_config(home), resolver=Resolver(client=client))


@pytest.fixture
def server(home):
    return build_server(make_queue(home))


def call(server, name, **args):
    result = asyncio.run(server.call_tool(name, args))
    if isinstance(result, tuple):  # mcp 1.x: (content, structured)
        result = result[0]
    content = getattr(result, "content", result)
    if getattr(result, "is_error", False) or getattr(result, "isError", False):
        raise RuntimeError(content[0].text)
    return json.loads(content[0].text)


def test_tools_and_instructions(server):
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == {
        "add_paper", "list_reading_list", "get_paper", "schedule_paper", "auto_schedule", "upcoming",
        "mark_read", "update_paper", "remove_paper", "free_slots", "sync_library",
    }
    text = server.instructions
    for phrase in ("without", "already_queued", "local time", "Never mark papers read", "upcoming"):
        assert phrase.lower() in text.lower()


def test_add_dedupe_and_schedule(server):
    out = call(server, "add_paper", ref="1706.03762", tags=["transformers"])
    assert out["status"] == "added"
    p = out["paper"]
    assert p["title"] == "Attention Is All You Need"
    assert p["authors"] == "Ashish Vaswani, Noam Shazeer, Niki Parmar et al."
    assert p["tags"] == ["transformers"]
    assert p["status"] == "to_read"
    assert p["scheduled"]  # next free slot, human-readable local time
    assert datetime.fromisoformat(p["scheduled_local"]).utcoffset() is not None
    assert p["links"]["url"].startswith("https://arxiv.org/abs/1706.03762")
    assert isinstance(out["warnings"], list)

    again = call(server, "add_paper", ref="arXiv:1706.03762")
    assert again["status"] == "already_queued"
    assert again["paper"]["id"] == p["id"]
    assert call(server, "list_reading_list", view="all")["count"] == 1


def test_add_unscheduled_and_lifecycle(server):
    p = call(server, "add_paper", ref="2310.06825", when="none", priority="high")["paper"]
    assert p["scheduled"] is None
    assert p["priority"] == "high"
    pid = p["id"]

    listed = call(server, "list_reading_list", view="queue")
    assert [x["id"] for x in listed["papers"]] == [pid]
    assert call(server, "list_reading_list", view="all", query="mistral")["count"] == 1

    full = call(server, "get_paper", paper_id=pid)
    assert "network architecture" in full["abstract"]

    sched = call(server, "schedule_paper", paper_id=pid, when="tomorrow")
    assert sched["paper"]["scheduled"]
    unsched = call(server, "schedule_paper", paper_id=pid, when="none")
    assert unsched["paper"]["scheduled"] is None

    upd = call(server, "update_paper", paper_id=pid, tags=["llm"], priority="low")
    assert upd["paper"]["tags"] == ["llm"] and upd["paper"]["priority"] == "low"

    auto = call(server, "auto_schedule")
    assert [x["id"] for x in auto["scheduled"]] == [pid]

    up = call(server, "upcoming", days=14)
    assert [x["id"] for x in up["sessions"]] == [pid]
    assert up["missed"] == []

    slots = call(server, "free_slots", n=3)["slots"]
    assert len(slots) == 3 and all("iso" in s and "local" in s for s in slots)

    done = call(server, "mark_read", paper_id=pid, notes="Good baseline.")
    assert done["paper"]["status"] == "read"
    assert "Good baseline." in call(server, "get_paper", paper_id=pid)["notes"]

    removed = call(server, "remove_paper", paper_id=pid)
    assert removed["removed"] == pid
    assert call(server, "list_reading_list", view="all")["count"] == 0


def test_errors_are_readable(server):
    with pytest.raises(Exception, match="999"):
        call(server, "get_paper", paper_id=999)


def test_sync_library_off(server):
    out = call(server, "sync_library")
    assert out["pulled"] == 0 and out["pushed"] == 0


def test_formatting_helpers():
    assert short_authors([]) == ""
    assert short_authors(["A", "B"]) == "A, B"
    assert short_authors(["A", "B", "C", "D"]) == "A, B, C et al."
    dt = datetime(2026, 11, 17, 7, 30, tzinfo=timezone.utc)
    assert human_time(dt, ZoneInfo("Europe/Berlin")) == "Tue 17 Nov 2026, 08:30"

    class P:
        id, title, authors, venue, year, status, priority, tags = 1, "T", ["X"], "NeurIPS", 2023, "to_read", 1, []
        scheduled_start, duration_minutes, url, pdf_url, doi = dt.isoformat(), 45, "https://x", None, "10.1/x"

    s = paper_summary(P(), ZoneInfo("Europe/Berlin"), now=datetime(2026, 11, 18, tzinfo=timezone.utc))
    assert s["venue_year"] == "NeurIPS 2023"
    assert s["priority"] == "high"
    assert s["missed"] is True
    assert s["links"]["doi"] == "https://doi.org/10.1/x"


def test_server_does_not_print(home, capsys):
    build_server(make_queue(home))
    assert capsys.readouterr().out == ""
    assert mcp_server.INSTRUCTIONS
