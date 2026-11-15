from __future__ import annotations

import httpx
import pytest

from paperqueue import metadata
from paperqueue.metadata import MetadataError, Ref, Resolver, meta_from_hints, parse_ref, read_capped

from .conftest import ARXIV_NOT_FOUND, Router, arxiv_atom, crossref_work, europepmc_result


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("2502.09775", Ref("arxiv", "2502.09775")),
        ("arXiv:2502.09775v2", Ref("arxiv", "2502.09775")),
        ("hep-th/9901001", Ref("arxiv", "hep-th/9901001")),
        ("https://arxiv.org/abs/2502.09775v1", Ref("arxiv", "2502.09775")),
        ("https://arxiv.org/pdf/2502.09775v1", Ref("arxiv", "2502.09775")),
        ("https://arxiv.org/pdf/2502.09775.pdf", Ref("arxiv", "2502.09775")),
        ("10.48550/arXiv.2502.09775", Ref("arxiv", "2502.09775")),
        ("https://doi.org/10.48550/arXiv.2502.09775", Ref("arxiv", "2502.09775")),
        (
            "https://www.biorxiv.org/content/10.1101/2024.01.01.573000v2.full",
            Ref("doi", "10.1101/2024.01.01.573000", 2),
        ),
        (
            "https://www.biorxiv.org/content/10.1101/2024.01.01.573000v2.full.pdf",
            Ref("doi", "10.1101/2024.01.01.573000", 2),
        ),
        ("https://www.medrxiv.org/content/10.1101/2024.05.06.24306912v1", Ref("doi", "10.1101/2024.05.06.24306912", 1)),
        ("10.1101/2024.01.01.573000", Ref("doi", "10.1101/2024.01.01.573000")),
        ("10.1101/123456", Ref("doi", "10.1101/123456")),
        ("https://www.nature.com/articles/s41467-024-55707-8", Ref("doi", "10.1038/s41467-024-55707-8")),
        ("https://www.nature.com/articles/s41467-024-55707-8.pdf", Ref("doi", "10.1038/s41467-024-55707-8")),
        ("https://doi.org/10.1038/s41467-024-55707-8", Ref("doi", "10.1038/s41467-024-55707-8")),
        ("doi:10.1016/j.cell.2020.01.001.", Ref("doi", "10.1016/j.cell.2020.01.001")),
        ("https://pubmed.ncbi.nlm.nih.gov/12345678/", Ref("pubmed", "12345678")),
        ("PMID: 12345678", Ref("pubmed", "12345678")),
        ("https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/", Ref("pmc", "PMC1234567")),
        ("https://www.ncbi.nlm.nih.gov/pmc/articles/PMC1234567/", Ref("pmc", "PMC1234567")),
        ("pmc1234567", Ref("pmc", "PMC1234567")),
        ("https://example.org/files/paper.pdf", Ref("url", "https://example.org/files/paper.pdf")),
        ("  <https://example.org/a>  ", Ref("url", "https://example.org/a")),
        ("Attention is all you need", Ref("text", "Attention is all you need")),
    ],
)
def test_parse_ref(ref: str, expected: Ref) -> None:
    assert parse_ref(ref) == expected


def test_arxiv(router: Router, resolver: Resolver) -> None:
    router.text("export.arxiv.org", "/api/query", arxiv_atom(doi="10.1/journal"), "application/atom+xml")
    meta = resolver.resolve("10.48550/arXiv.2502.09775")
    assert meta.title == "A Study of Things: With Line Breaks"
    assert meta.authors == ["Ada Lovelace", "Alan Turing"]
    assert meta.year == 2025
    assert meta.venue == "arXiv"
    assert meta.abstract == "We study things. Results are good."
    assert meta.arxiv_id == "2502.09775"
    assert meta.doi == "10.1/journal"
    assert meta.pdf_url == "https://arxiv.org/pdf/2502.09775"
    assert router.requests[0].url.params["id_list"] == "2502.09775"


def test_arxiv_unknown_and_malformed(router: Router, resolver: Resolver) -> None:
    router.text("export.arxiv.org", "/api/query", ARXIV_NOT_FOUND, "application/atom+xml")
    with pytest.raises(MetadataError, match="no paper"):
        resolver.resolve("2502.00000")
    router.text("export.arxiv.org", "/api/query", "<feed><unclosed>", "application/atom+xml")
    with pytest.raises(MetadataError, match="ParseError"):
        resolver.resolve("2502.00000")


def test_crossref(router: Router, resolver: Resolver) -> None:
    router.json("api.crossref.org", "/works/10.1038/s41467-024-55707-8", crossref_work())
    meta = resolver.resolve("https://www.nature.com/articles/s41467-024-55707-8")
    assert meta.title == "Deep learning of cell states"
    assert meta.authors == ["Marie Curie", "The Consortium"]
    assert (meta.year, meta.venue) == (2025, "Nature Communications")
    assert meta.abstract == "Cells have states."
    assert meta.url == "https://doi.org/10.1038/s41467-024-55707-8"
    assert meta.pdf_url == "https://www.nature.com/articles/x.pdf"


def test_crossref_abstract_falls_back_to_europepmc(router: Router, resolver: Resolver) -> None:
    router.json("api.crossref.org", "/works/", crossref_work(abstract=None))
    router.json("www.ebi.ac.uk", "/europepmc/", europepmc_result())
    meta = resolver.resolve("10.1038/s41467-024-55707-8")
    assert meta.abstract == "An abstract from Europe PMC."
    assert meta.title == "Deep learning of cell states"


@pytest.mark.parametrize(
    ("ref", "url", "pdf"),
    [
        (
            "https://www.biorxiv.org/content/10.1101/2024.01.01.573000v2.full",
            "https://www.biorxiv.org/content/10.1101/2024.01.01.573000v2",
            "https://www.biorxiv.org/content/10.1101/2024.01.01.573000v2.full.pdf",
        ),
        ("10.1101/2024.01.01.573000", "https://www.biorxiv.org/content/10.1101/2024.01.01.573000", None),
    ],
)
def test_biorxiv_links(router: Router, resolver: Resolver, ref: str, url: str, pdf: str | None) -> None:
    router.json(
        "api.crossref.org",
        "/works/10.1101/2024.01.01.573000",
        crossref_work(
            "10.1101/2024.01.01.573000", type="posted-content", **{"container-title": []},
            institution=[{"name": "bioRxiv"}], link=[],
        ),
    )
    meta = resolver.resolve(ref)
    assert (meta.venue, meta.url, meta.pdf_url) == ("bioRxiv", url, pdf)


def test_doi_chain_crossref_404_then_europepmc(router: Router, resolver: Resolver) -> None:
    router.json("api.crossref.org", "/works/", {"status": "error"}, status=404)
    router.json("www.ebi.ac.uk", "/europepmc/", europepmc_result(doi="10.1000/epmc.1"))
    meta = resolver.resolve("10.1000/epmc.1")
    assert meta.title == "A PubMed paper"
    assert meta.authors == ["Rosalind Franklin"]
    assert meta.matched_by == "Europe PMC"


def test_doi_chain_ends_at_web_page_then_hints(router: Router, resolver: Resolver) -> None:
    router.json("www.ebi.ac.uk", "/europepmc/", {"resultList": {"result": []}})
    router.text("doi.org", "/", "blocked", status=403)
    with pytest.raises(MetadataError) as exc:
        resolver.resolve("10.9999/missing")
    assert "Crossref has no record" in str(exc.value)
    assert [r.url.host for r in router.requests] == ["api.crossref.org", "www.ebi.ac.uk", "doi.org"]
    meta = resolver.resolve("10.9999/missing", {"citation_title": "From the page", "citation_author": ["A B"]})
    assert (meta.title, meta.authors, meta.matched_by) == ("From the page", ["A B"], "hints")


@pytest.mark.parametrize("ref", ["https://pubmed.ncbi.nlm.nih.gov/12345678/", "PMC1234567"])
def test_europepmc_ids(router: Router, resolver: Resolver, ref: str) -> None:
    router.json("www.ebi.ac.uk", "/europepmc/", europepmc_result())
    meta = resolver.resolve(ref)
    assert meta.doi == "10.1000/epmc.1"
    assert meta.pdf_url == "https://europepmc.org/articles/PMC1234567?pdf=render"
    query = router.requests[0].url.params["query"]
    assert query in ("EXT_ID:12345678 AND SRC:MED", "PMCID:PMC1234567")


PAGE = """<html><head><title>Ignored</title>
<meta name="citation_title" content="A Web Paper &amp; More">
<meta name="citation_author" content="Grace Hopper">
<meta name="citation_author" content="Katherine Johnson">
<meta name="citation_publication_date" content="2021/03/04">
<meta name="citation_journal_title" content="Web Journal">
<meta name="citation_pdf_url" content="/files/paper.pdf">
<meta name="description" content="Abstract: A page abstract.">
</head><body></body></html>"""


def test_webpage_citation_meta(router: Router, resolver: Resolver) -> None:
    router.text("example.org", "/paper", PAGE)
    meta = resolver.resolve("https://example.org/paper")
    assert meta.title == "A Web Paper & More"
    assert meta.authors == ["Grace Hopper", "Katherine Johnson"]
    assert (meta.year, meta.venue) == (2021, "Web Journal")
    assert meta.abstract == "A page abstract."
    assert meta.pdf_url == "https://example.org/files/paper.pdf"


def test_webpage_rejects_non_http_pdf_url(router: Router, resolver: Resolver) -> None:
    router.text("example.org", "/paper", PAGE.replace("/files/paper.pdf", "javascript:alert(1)"))
    assert resolver.resolve("https://example.org/paper").pdf_url is None


def test_webpage_with_doi_uses_crossref(router: Router, resolver: Resolver) -> None:
    page = PAGE.replace("</head>", '<meta name="citation_doi" content="doi:10.1038/s41467-024-55707-8"></head>')
    router.text("example.org", "/paper", page)
    router.json("api.crossref.org", "/works/", crossref_work(link=[]))
    meta = resolver.resolve("https://example.org/paper")
    assert meta.title == "Deep learning of cell states"
    assert meta.pdf_url == "https://example.org/files/paper.pdf"


def test_pdf_url_gives_no_garbage_title(router: Router, resolver: Resolver) -> None:
    router.text("example.org", "/files/x.pdf", b"%PDF-1.7 garbage", "application/pdf")
    with pytest.raises(MetadataError, match="is a PDF"):
        resolver.resolve("https://example.org/files/x.pdf")
    meta = resolver.resolve("https://example.org/files/x.pdf", {"title": "Hinted title"})
    assert (meta.title, meta.url) == ("Hinted title", "https://example.org/files/x.pdf")


def test_hint_identifiers_tried_before_page(router: Router, resolver: Resolver) -> None:
    router.text("export.arxiv.org", "/api/query", arxiv_atom(), "application/atom+xml")
    meta = resolver.resolve("https://blocked.example/paper", {"citation_arxiv_id": "2502.09775"})
    assert meta.arxiv_id == "2502.09775"
    assert router.hosts() == ["export.arxiv.org"]


@pytest.mark.parametrize("url", ["http://127.0.0.1:8765/api/status", "http://localhost/x", "http://intranet.test/"])
def test_ssrf_guard(router: Router, resolver: Resolver, url: str) -> None:
    with pytest.raises(MetadataError, match="non-public"):
        resolver.resolve(url)
    assert router.requests == []


def test_ssrf_guard_after_redirect(router: Router, resolver: Resolver) -> None:
    router.add("example.org", "/r", httpx.Response(302, headers={"location": "http://169.254.169.254/latest"}))
    with pytest.raises(MetadataError, match="non-public"):
        resolver.resolve("https://example.org/r")
    assert router.hosts() == ["example.org"]


def test_redirect_to_arxiv_is_followed(router: Router, resolver: Resolver) -> None:
    router.add("short.example", "/x", httpx.Response(301, headers={"location": "https://arxiv.org/abs/2502.09775"}))
    router.text("arxiv.org", "/abs/", "<html></html>")
    router.text("export.arxiv.org", "/api/query", arxiv_atom(), "application/atom+xml")
    assert resolver.resolve("https://short.example/x").arxiv_id == "2502.09775"


def test_title_search_candidates(router: Router, resolver: Resolver) -> None:
    items = [
        {"title": ["Something quite different"], "DOI": "10.1/a", "issued": {"date-parts": [[2020]]}},
        {"title": ["Another unrelated work"], "DOI": "10.1/b", "issued": {"date-parts": [[2019]]}},
    ]
    router.json("api.crossref.org", "/works", {"message": {"items": items}})
    with pytest.raises(MetadataError) as exc:
        resolver.resolve("Attention is all you need")
    assert exc.value.candidates == [
        {"title": "Something quite different", "doi": "10.1/a", "year": 2020},
        {"title": "Another unrelated work", "doi": "10.1/b", "year": 2019},
    ]
    assert isinstance(exc.value, ValueError)


def test_title_search_match(router: Router, resolver: Resolver) -> None:
    items = [{"title": ["Attention Is All You Need"], "DOI": "10.1/att"}]
    router.json("api.crossref.org", "/works", {"message": {"items": items}})
    meta = resolver.resolve("attention is all you need")
    assert (meta.doi, meta.matched_by) == ("10.1/att", "title search")


def test_bad_json_is_metadata_error(router: Router, resolver: Resolver) -> None:
    router.text("api.crossref.org", "/works/", "not json", "application/json")
    router.json("www.ebi.ac.uk", "/europepmc/", {"unexpected": True})
    with pytest.raises(MetadataError):
        resolver.resolve("10.1234/x")


def test_meta_from_hints_sanitises() -> None:
    meta = meta_from_hints(
        {
            "title": "T" * 900,
            "citation_author": "Ann One; Bob Two",
            "citation_date": "2020-05-01",
            "citation_doi": "https://doi.org/10.1000/xyz",
            "citation_pdf_url": "data:application/pdf;base64,AAAA",
            "url": "javascript:alert(1)",
        },
        "https://example.org/landing",
    )
    assert meta is not None
    assert len(meta.title) == 500
    assert meta.authors == ["Ann One", "Bob Two"]
    assert (meta.year, meta.doi, meta.pdf_url) == (2020, "10.1000/xyz", None)
    assert meta.url == "https://example.org/landing"
    assert meta_from_hints({"author": "No title"}) is None


def test_read_capped() -> None:
    resp = httpx.Response(200, content=b"x" * 100)
    body, truncated = read_capped(resp, 10)
    assert (len(body), truncated) == (10, True)


def test_page_cap_still_parses_head(router: Router, resolver: Resolver, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metadata, "PAGE_CAP", len(PAGE) + 10)
    router.text("example.org", "/big", PAGE + "x" * 10_000)
    assert resolver.resolve("https://example.org/big").title == "A Web Paper & More"


# --- YouTube ------------------------------------------------------------------------

VID = "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "ref",
    [
        f"https://www.youtube.com/watch?v={VID}",
        f"https://youtube.com/watch?v={VID}&t=30s",
        f"https://www.youtube.com/watch?v={VID}&list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG&index=3",
        f"https://m.youtube.com/watch?feature=share&v={VID}",
        f"https://youtu.be/{VID}",
        f"https://youtu.be/{VID}?si=abc&t=42",
        f"youtu.be/{VID}",
        f"https://www.youtube.com/shorts/{VID}",
        f"https://www.youtube.com/live/{VID}?feature=shared",
        f"https://www.youtube.com/embed/{VID}",
        f"  <https://www.youtube.com/watch?v={VID}#t=1m>  ",
    ],
)
def test_parse_youtube(ref: str) -> None:
    assert parse_ref(ref) == Ref("youtube", VID)


@pytest.mark.parametrize(
    ("ref", "message"),
    [
        ("https://www.youtube.com/playlist?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG", "playlists"),
        ("https://www.youtube.com/watch?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG", "playlists"),
        ("https://www.youtube.com/@SomeChannel", "not a YouTube video"),
        ("https://youtu.be/short", "not a YouTube video"),
    ],
)
def test_parse_youtube_rejects(ref: str, message: str) -> None:
    with pytest.raises(MetadataError, match=message):
        parse_ref(ref)


def test_not_youtube_lookalike() -> None:
    assert parse_ref("https://notyoutube.com/watch?v=dQw4w9WgXcQ").kind == "url"


def test_youtube_via_ytdlp(router: Router, resolver: Resolver, ytdlp) -> None:
    meta = resolver.resolve(f"https://youtu.be/{VID}?t=1")
    assert (meta.title, meta.authors, meta.year, meta.venue) == ("How Transformers Work", ["Some Channel"], 2024, "YouTube")
    assert meta.url == f"https://www.youtube.com/watch?v={VID}"
    assert (meta.video_id, meta.duration_seconds, meta.matched_by) == (VID, 2520, "identifier")
    assert meta.abstract and len(meta.abstract) <= 1501 and meta.abstract.endswith("…")
    (args,) = ytdlp.calls
    assert args[:3] == ["-J", "--no-playlist", "--ignore-no-formats-error"]
    assert args[-2:] == ["--", meta.url]
    assert router.requests == []


def test_youtube_falls_back_to_oembed(router: Router, resolver: Resolver, ytdlp) -> None:
    ytdlp.info.clear()  # yt-dlp fails (e.g. no Deno, bot check)
    router.json("www.youtube.com", "/oembed", {"title": "How Transformers Work", "author_name": "Some Channel"})
    meta = resolver.resolve(f"https://www.youtube.com/watch?v={VID}")
    assert (meta.title, meta.authors, meta.video_id, meta.matched_by) == (
        "How Transformers Work", ["Some Channel"], VID, "oembed")
    assert meta.duration_seconds is None
    assert router.requests[0].url.params["url"] == f"https://www.youtube.com/watch?v={VID}"


def test_youtube_unavailable(router: Router, resolver: Resolver, ytdlp) -> None:
    ytdlp.info.clear()
    router.json("www.youtube.com", "/oembed", {}, status=404)
    with pytest.raises(MetadataError, match="unavailable"):
        resolver.resolve(f"https://youtu.be/{VID}")
    meta = resolver.resolve(f"https://youtu.be/{VID}", {"title": "From the page"})
    assert (meta.title, meta.video_id, meta.venue, meta.matched_by) == ("From the page", VID, "YouTube", "hints")


def test_new_biorxiv_doi_prefix_strips_version() -> None:
    from paperqueue.metadata import parse_ref

    for ref in ("https://doi.org/10.64898/2026.10.07.756550v1",
                "https://www.biorxiv.org/content/10.64898/2026.10.07.756550v1.full"):
        r = parse_ref(ref)
        assert (r.kind, r.value) == ("doi", "10.64898/2026.10.07.756550")
