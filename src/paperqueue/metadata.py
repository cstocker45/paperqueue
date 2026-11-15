"""Resolve a paper reference (arXiv ID, DOI, PubMed/PMC ID, URL or title) to metadata."""

from __future__ import annotations

import difflib
import html
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse, urlsplit

import httpx

from . import __version__

USER_AGENT = f"paperqueue/{__version__} (local reading-queue tool)"
TIMEOUT = 10.0
PAGE_CAP = 3 * 1024 * 1024
MAX_REDIRECTS = 5
TITLE_MATCH = 0.85

ARXIV_NEW = r"\d{4}\.\d{4,5}"
ARXIV_OLD = r"[a-z][a-z\-]+(?:\.[A-Z]{2})?/\d{7}"
ARXIV_ID_RE = re.compile(rf"^(?:arxiv:)?\s*({ARXIV_NEW}|{ARXIV_OLD})(v\d+)?$", re.I)
ARXIV_URL_RE = re.compile(rf"arxiv\.org/(?:abs|pdf|html)/({ARXIV_NEW}|{ARXIV_OLD})(v\d+)?", re.I)
ARXIV_DOI_RE = re.compile(rf"10\.48550/arxiv\.({ARXIV_NEW}|{ARXIV_OLD})(v\d+)?", re.I)
# bioRxiv/medRxiv DOIs: the original 10.1101 prefix and the newer 10.64898 one.
RXIV_DOI_RE = re.compile(r"(10\.(?:1101|64898)/(?:\d{4}\.\d{2}\.\d{2}\.\d+|\d{6}))(?:v(\d+))?", re.I)
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>&?#]+)", re.I)
NATURE_URL_RE = re.compile(r"nature\.com/articles/([a-z0-9\-]+)", re.I)
PMC_RE = re.compile(r"(?:^|/|pmcid:?\s*)(PMC\d{4,9})\b", re.I)
PUBMED_RE = re.compile(
    r"(?:pubmed\.ncbi\.nlm\.nih\.gov/|europepmc\.org/(?:abstract|article)/MED/|^pmid:?\s*)(\d{5,9})",
    re.I,
)
YOUTUBE_RE = re.compile(r"^(?:https?://)?(?:(?:www|m)\.)?(youtube\.com|youtu\.be)(?=[/?#]|$)", re.I)
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
YTDLP = [sys.executable, "-m", "yt_dlp"]
YTDLP_PROBE_TIMEOUT = 20.0
DESCRIPTION_MAX = 1500
# Homebrew's bin dirs, which launchd and some GUI-launched MCP clients leave off PATH.
TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"
EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

# Parsing failures of upstream payloads, turned into MetadataError per source.
_SOURCE_ERRORS = (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, ET.ParseError)


class MetadataError(ValueError):
    """Metadata could not be resolved. `candidates` lists near title matches, if any;
    `pdf_url` is set when the reference turned out to be a PDF file."""

    def __init__(
        self,
        message: str,
        candidates: list[dict[str, Any]] | None = None,
        pdf_url: str | None = None,
    ):
        super().__init__(message)
        self.candidates: list[dict[str, Any]] = candidates or []
        self.pdf_url = pdf_url


@dataclass
class Ref:
    kind: str  # arxiv | doi | pubmed | pmc | youtube | url | text
    value: str
    version: int | None = None  # bioRxiv/medRxiv version, when the reference names one


@dataclass
class PaperMeta:
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    abstract: str | None = None
    url: str | None = None
    pdf_url: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    video_id: str | None = None
    duration_seconds: int | None = None
    matched_by: str = "identifier"  # also: title search, web page, Europe PMC, hints, oembed, minimal


def _clean_doi(doi: str) -> str:
    doi = unquote(doi).strip().rstrip(".,;)]}")
    return re.sub(
        r"(\.full|\.pdf|/full|/abstract|/pdf|\.abstract|\.full-text|\.full\.pdf)+$", "", doi, flags=re.I
    )


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _youtube_ref(s: str) -> Ref:
    """A youtube.com / youtu.be URL -> Ref("youtube", id). Raises MetadataError for playlists
    and other pages that name no single video."""
    parts = urlsplit(s if re.match(r"^https?://", s, re.I) else f"https://{s}")
    host = (parts.hostname or "").lower()
    path = [x for x in parts.path.split("/") if x]
    query = parse_qs(parts.query)
    candidate = ""
    if host == "youtu.be":
        candidate = path[0] if path else ""
    elif path == ["watch"]:
        candidate = (query.get("v") or [""])[0]
    elif len(path) >= 2 and path[0] in ("shorts", "live", "embed", "v"):
        candidate = path[1]
    if VIDEO_ID_RE.match(candidate):
        return Ref("youtube", candidate)
    if "list" in query or path[:1] == ["playlist"]:
        raise MetadataError("YouTube playlists are not supported; add the videos one at a time")
    raise MetadataError(f"not a YouTube video link: {s}")


def parse_ref(ref: str) -> Ref:
    s = ref.strip().strip("<>").strip()
    if YOUTUBE_RE.match(s):
        return _youtube_ref(s)
    if m := ARXIV_ID_RE.match(s):
        return Ref("arxiv", m.group(1))
    if m := ARXIV_URL_RE.search(s):
        return Ref("arxiv", m.group(1).removesuffix(".pdf"))
    if m := ARXIV_DOI_RE.search(s):
        return Ref("arxiv", m.group(1))
    if m := RXIV_DOI_RE.search(s):
        return Ref("doi", m.group(1), int(m.group(2)) if m.group(2) else None)
    if m := PMC_RE.search(s):
        return Ref("pmc", m.group(1).upper())
    if m := PUBMED_RE.search(s):
        return Ref("pubmed", m.group(1))
    if m := DOI_RE.search(s):
        return Ref("doi", _clean_doi(m.group(1)))
    if m := NATURE_URL_RE.search(s):
        return Ref("doi", f"10.1038/{m.group(1)}")
    if re.match(r"^https?://", s, re.I):
        return Ref("url", s)
    return Ref("text", s)


def http_url(url: str | None, base: str | None = None) -> str | None:
    """Absolute http(s) URL, or None for anything else (javascript:, data:, file:, ...)."""
    if not url:
        return None
    url = urljoin(base, url.strip()) if base else url.strip()
    p = urlparse(url)
    return url if p.scheme in ("http", "https") and p.hostname else None


def _strip_tags(text: str | None) -> str | None:
    if not text:
        return None
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    text = re.sub(r"^(abstract|summary)[:.\s]+", "", text, flags=re.I)
    return text or None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def _year(text: str | None) -> int | None:
    m = re.search(r"(?:19|20)\d{2}", text or "")
    return int(m.group()) if m else None


# --- yt-dlp ------------------------------------------------------------------------


def find_tool(name: str) -> str | None:
    """Path of an executable on PATH or in Homebrew's bin dirs."""
    found = shutil.which(name)
    if found:
        return found
    for directory in TOOL_DIRS:
        path = os.path.join(directory, name)
        if os.access(path, os.X_OK):
            return path
    return None


def run_ytdlp(
    args: list[str], timeout: float, pass_fds: tuple[int, ...] = ()
) -> subprocess.CompletedProcess[str]:
    """Run yt-dlp as a child process in its own process group, with no stdin and captured
    output (so it can never write to an MCP stdio channel). On timeout the whole group
    (yt-dlp and any ffmpeg it started) is killed and TimeoutExpired is raised."""
    with subprocess.Popen(
        [*YTDLP, *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
        pass_fds=pass_fds,
    ) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def js_runtime_args() -> list[str]:
    """yt-dlp needs a JavaScript runtime (Deno) for YouTube; point it at Deno explicitly."""
    deno = find_tool("deno")
    return ["--js-runtimes", f"deno:{deno}"] if deno else []


def _trim(text: str | None, limit: int) -> str | None:
    text = (text or "").strip()
    if len(text) <= limit:
        return text or None
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.-") + "…"


# --- SSRF guard -----------------------------------------------------------------


def _host_addresses(host: str) -> list[str]:
    try:
        return [str(ipaddress.ip_address(host.strip("[]")))]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as exc:
        raise MetadataError(f"cannot resolve host {host!r}") from exc
    return [str(info[4][0]) for info in infos]


def check_public_url(url: str) -> None:
    """Refuse URLs whose host is (or resolves to) a loopback, private or link-local address."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise MetadataError(f"not an http(s) URL: {url!r}")
    for addr in _host_addresses(p.hostname):
        ip = ipaddress.ip_address(addr.split("%")[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise MetadataError(f"refusing to fetch non-public address {p.hostname}")


@contextmanager
def open_public(client: httpx.Client, url: str) -> Iterator[httpx.Response]:
    """Stream a GET of a public URL, following redirects manually so every hop is checked."""
    for _ in range(MAX_REDIRECTS + 1):
        check_public_url(url)
        with client.stream("GET", url, follow_redirects=False) as resp:
            if resp.is_redirect and "location" in resp.headers:
                url = urljoin(str(resp.url), resp.headers["location"])
                continue
            resp.raise_for_status()
            yield resp
            return
    raise MetadataError(f"too many redirects for {url}")


def read_capped(resp: httpx.Response, cap: int) -> tuple[bytes, bool]:
    """Read at most `cap` bytes of a streamed body. Returns (body, truncated)."""
    chunks: list[bytes] = []
    size = 0
    for chunk in resp.iter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size > cap:
            return b"".join(chunks)[:cap], True
    return b"".join(chunks), False


# --- hints (DOM metadata scraped by the bookmarklet) -----------------------------

_HINT_KEYS: dict[str, tuple[str, ...]] = {
    "title": ("title", "citation_title", "dc.title", "og:title"),
    "authors": ("authors", "author", "citation_author", "citation_authors", "dc.creator"),
    "date": ("year", "citation_publication_date", "citation_date", "citation_online_date", "dc.date"),
    "venue": ("venue", "citation_journal_title", "citation_conference_title", "og:site_name"),
    "abstract": ("abstract", "citation_abstract", "dc.description", "og:description", "description"),
    "doi": ("doi", "citation_doi", "prism.doi", "dc.identifier"),
    "arxiv_id": ("arxiv_id", "citation_arxiv_id"),
    "pdf_url": ("pdf_url", "citation_pdf_url"),
    "url": ("url", "location", "href", "citation_abstract_html_url", "og:url"),
}


def _hint_values(hints: Mapping[str, Any], name: str) -> list[str]:
    for key in _HINT_KEYS[name]:
        value = hints.get(key)
        values = value if isinstance(value, list) else [value]
        out = [str(v).strip() for v in values if isinstance(v, (str, int)) and str(v).strip()]
        if out:
            return out
    return []


def hint_identifiers(hints: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """(doi, arxiv_id) named by page hints, if any."""
    dois = _hint_values(hints, "doi")
    doi_m = DOI_RE.search(dois[0][:300]) if dois else None
    arxivs = _hint_values(hints, "arxiv_id")
    arxiv_m = ARXIV_ID_RE.match(arxivs[0][:50]) if arxivs else None
    return (_clean_doi(doi_m.group(1)) if doi_m else None, arxiv_m.group(1) if arxiv_m else None)


def meta_from_hints(hints: Mapping[str, Any], base_url: str | None = None) -> PaperMeta | None:
    """PaperMeta from untrusted page hints (citation_*/og/dc keys or PaperMeta field names)."""

    def one(name: str, limit: int) -> str | None:
        values = _hint_values(hints, name)
        return values[0][:limit] if values else None

    title = one("title", 500)
    url = http_url(one("url", 2000), base_url) or http_url(base_url)
    authors = _hint_values(hints, "authors")
    if len(authors) == 1 and ";" in authors[0]:
        authors = [a.strip() for a in authors[0].split(";") if a.strip()]
    doi, arxiv_id = hint_identifiers(hints)
    meta = PaperMeta(
        title=html.unescape(title) if title else "",
        authors=[a[:200] for a in authors[:100]],
        year=_year(one("date", 50)),
        venue=one("venue", 300),
        abstract=_strip_tags(one("abstract", 10_000)),
        url=url,
        pdf_url=http_url(one("pdf_url", 2000), url),
        doi=doi,
        arxiv_id=arxiv_id,
        matched_by="hints",
    )
    return meta if meta.title else None


# --- HTML meta tags --------------------------------------------------------------


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, list[str]] = {}
        self._in_title = False
        self.title = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "meta":
            name = (a.get("name") or a.get("property") or "").lower()
            content = a.get("content")
            if name and content:
                self.meta.setdefault(name, []).append(content.strip())
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data


class Resolver:
    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(
            timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        )

    def resolve(self, ref: str, hints: Mapping[str, Any] | None = None) -> PaperMeta:
        """Try the sources for `ref` in order; fall back to `hints`; else raise MetadataError."""
        r = parse_ref(ref)
        hinted = meta_from_hints(hints, ref if r.kind == "url" else None) if hints else None
        attempts: list[Callable[[], PaperMeta]] = []
        if r.kind == "youtube":
            attempts.append(lambda: self._youtube(r.value))
        elif r.kind == "arxiv":
            attempts.append(lambda: self.arxiv(r.value))
        elif r.kind == "doi":
            attempts += self._doi_attempts(r.value, r.version)
        elif r.kind in ("pubmed", "pmc"):
            query = f"EXT_ID:{r.value} AND SRC:MED" if r.kind == "pubmed" else f"PMCID:{r.value}"
            attempts.append(lambda: self.europepmc(query))
        elif r.kind == "url":
            hint_doi, hint_arxiv = hint_identifiers(hints) if hints else (None, None)
            if hint_arxiv:
                attempts.append(lambda: self.arxiv(hint_arxiv))
            if hint_doi:
                attempts += self._doi_attempts(hint_doi, None)[:2]
            attempts.append(lambda: self.webpage(r.value))
        else:
            attempts.append(lambda: self.search_title(r.value))

        errors: list[str] = []
        candidates: list[dict[str, Any]] = []
        pdf_url: str | None = None
        for attempt in attempts:
            try:
                return attempt()
            except MetadataError as exc:
                errors.append(str(exc))
                candidates = candidates or exc.candidates
                pdf_url = pdf_url or exc.pdf_url
            except _SOURCE_ERRORS as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
        if hinted:
            hinted.pdf_url = hinted.pdf_url or pdf_url
            if r.kind == "youtube":
                hinted.url, hinted.venue, hinted.video_id = watch_url(r.value), "YouTube", r.value
            return hinted
        raise MetadataError(
            f"could not resolve {ref!r}: " + "; ".join(errors or ["no source"]), candidates, pdf_url
        )

    def _doi_attempts(self, doi: str, version: int | None) -> list[Callable[[], PaperMeta]]:
        return [
            lambda: self.crossref(doi, version),
            lambda: self.europepmc(f'DOI:"{doi}"'),
            lambda: self.webpage(f"https://doi.org/{quote(doi, safe='/')}"),
        ]

    # sources --------------------------------------------------------------

    def _youtube(self, video_id: str) -> PaperMeta:
        """Video metadata from yt-dlp; if that fails, title and channel from YouTube oEmbed
        (matched_by "oembed", so the record is retried later for the length and description)."""
        url = watch_url(video_id)
        try:
            proc = run_ytdlp(
                ["-J", "--no-playlist", "--ignore-no-formats-error", *js_runtime_args(), "--", url],
                timeout=YTDLP_PROBE_TIMEOUT,
            )
            if proc.returncode == 0:
                info = json.loads(proc.stdout)
                title = str(info.get("title") or "").strip()
                if title:
                    duration = info.get("duration")
                    channel = info.get("channel") or info.get("uploader")
                    return PaperMeta(
                        title=title,
                        authors=[str(channel)] if channel else [],
                        year=_year(str(info.get("upload_date") or "")[:4]),
                        venue="YouTube",
                        abstract=_trim(info.get("description"), DESCRIPTION_MAX),
                        url=url,
                        video_id=video_id,
                        duration_seconds=int(duration) if isinstance(duration, (int, float)) else None,
                    )
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        resp = self.client.get(
            "https://www.youtube.com/oembed", params={"url": url, "format": "json"}
        )
        if resp.status_code in (401, 403, 404):
            raise MetadataError(f"YouTube video {video_id} is unavailable or private")
        resp.raise_for_status()
        data = resp.json()
        title = str(data.get("title") or "").strip()
        if not title:
            raise MetadataError(f"YouTube has no title for video {video_id}")
        channel = str(data.get("author_name") or "").strip()
        return PaperMeta(
            title=title,
            authors=[channel] if channel else [],
            venue="YouTube",
            url=url,
            video_id=video_id,
            matched_by="oembed",
        )

    def arxiv(self, arxiv_id: str) -> PaperMeta:
        resp = self.client.get(
            "https://export.arxiv.org/api/query", params={"id_list": arxiv_id, "max_results": 1}
        )
        resp.raise_for_status()
        entry = ET.fromstring(resp.text).find(f"{ATOM}entry")
        if entry is None or "api/errors" in (entry.findtext(f"{ATOM}id") or ""):
            raise MetadataError(f"arXiv has no paper {arxiv_id}")
        title = re.sub(r"\s+", " ", entry.findtext(f"{ATOM}title") or "").strip()
        if not title:
            raise MetadataError(f"arXiv has no paper {arxiv_id}")
        doi = (entry.findtext(f"{ARXIV_NS}doi") or "").strip()
        return PaperMeta(
            title=title,
            authors=[(a.findtext(f"{ATOM}name") or "").strip() for a in entry.findall(f"{ATOM}author")],
            year=_year(entry.findtext(f"{ATOM}published")),
            venue=re.sub(r"\s+", " ", entry.findtext(f"{ARXIV_NS}journal_ref") or "arXiv").strip(),
            abstract=_strip_tags(entry.findtext(f"{ATOM}summary")),
            url=f"https://arxiv.org/abs/{arxiv_id}",
            pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
            doi=doi or None,
            arxiv_id=arxiv_id,
        )

    def crossref(self, doi: str, version: int | None = None) -> PaperMeta:
        resp = self.client.get(f"https://api.crossref.org/works/{quote(doi, safe='/')}")
        if resp.status_code == 404:
            raise MetadataError(f"Crossref has no record for DOI {doi}")
        resp.raise_for_status()
        message = resp.json()["message"]
        meta = self._from_crossref(message)
        if RXIV_DOI_RE.fullmatch(meta.doi or ""):
            names = " ".join(
                [i.get("name", "") for i in message.get("institution", [])] + [message.get("publisher", "")]
            )
            server = "medrxiv" if "medrxiv" in names.lower() else "biorxiv"
            landing = f"https://www.{server}.org/content/{meta.doi}"
            meta.url = f"{landing}v{version}" if version else landing
            meta.pdf_url = f"{landing}v{version}.full.pdf" if version else None
        if not meta.abstract and meta.doi:
            meta.abstract = self._europepmc_abstract(meta.doi)
        return meta

    def _from_crossref(self, m: dict[str, Any], matched_by: str = "identifier") -> PaperMeta:
        authors = []
        for a in m.get("author", []):
            name = " ".join(x for x in (a.get("given"), a.get("family")) if x) or a.get("name", "")
            if name:
                authors.append(name)
        year = None
        for key in ("published-print", "published-online", "posted", "issued", "created"):
            parts = (m.get(key) or {}).get("date-parts") or [[None]]
            if parts[0] and parts[0][0]:
                year = int(parts[0][0])
                break
        venue = (m.get("container-title") or [None])[0]
        if not venue and m.get("institution"):
            venue = m["institution"][0].get("name")
        if not venue and m.get("type") == "posted-content":
            venue = m.get("publisher")
        doi = m.get("DOI")
        pdf = next(
            (link["URL"] for link in m.get("link", []) if link.get("content-type") == "application/pdf"),
            None,
        )
        title = re.sub(r"\s+", " ", (m.get("title") or [""])[0]).strip()
        if not title:
            raise MetadataError(f"Crossref record {doi} has no title")
        return PaperMeta(
            title=_strip_tags(title) or title,
            authors=authors,
            year=year,
            venue=venue,
            abstract=_strip_tags(m.get("abstract")),
            url=f"https://doi.org/{doi}" if doi else m.get("URL"),
            pdf_url=http_url(pdf),
            doi=doi,
            matched_by=matched_by,
        )

    def europepmc(self, query: str) -> PaperMeta:
        resp = self.client.get(
            EUROPEPMC, params={"query": query, "resultType": "core", "format": "json", "pageSize": 1}
        )
        resp.raise_for_status()
        results = resp.json().get("resultList", {}).get("result", [])
        if not results:
            raise MetadataError(f"Europe PMC has no record for {query}")
        return self._from_europepmc(results[0])

    def _from_europepmc(self, rec: dict[str, Any]) -> PaperMeta:
        title = _strip_tags(rec.get("title"))
        if not title:
            raise MetadataError("Europe PMC record has no title")
        authors = [
            " ".join(x for x in (a.get("firstName"), a.get("lastName")) if x) or a.get("fullName", "")
            for a in rec.get("authorList", {}).get("author", [])
        ]
        if not any(authors) and rec.get("authorString"):
            authors = [a.strip() for a in rec["authorString"].rstrip(".").split(",")]
        doi, pmid = rec.get("doi"), rec.get("pmid")
        if doi:
            url = f"https://doi.org/{doi}"
        elif pmid:
            url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        else:
            url = f"https://europepmc.org/article/{rec.get('source', 'MED')}/{rec.get('id', '')}"
        pdf = next(
            (
                u.get("url")
                for u in rec.get("fullTextUrlList", {}).get("fullTextUrl", [])
                if u.get("documentStyle") == "pdf" and u.get("availabilityCode") in ("OA", "F")
            ),
            None,
        )
        journal = rec.get("journalInfo", {}).get("journal", {})
        return PaperMeta(
            title=title.rstrip("."),
            authors=[a for a in authors if a],
            year=_year(str(rec.get("pubYear") or "")),
            venue=journal.get("title") or rec.get("bookOrReportDetails", {}).get("publisher"),
            abstract=_strip_tags(rec.get("abstractText")),
            url=url,
            pdf_url=http_url(pdf),
            doi=doi,
            matched_by="Europe PMC",
        )

    def _europepmc_abstract(self, doi: str) -> str | None:
        """Best-effort abstract for DOIs whose Crossref record has none."""
        try:
            return self.europepmc(f'DOI:"{doi}"').abstract
        except (MetadataError, *_SOURCE_ERRORS):
            return None

    def webpage(self, url: str) -> PaperMeta:
        with open_public(self.client, url) as resp:
            final = str(resp.url)
            if final != url and (r := parse_ref(final)).kind == "arxiv":
                return self.arxiv(r.value)
            ctype = resp.headers.get("content-type", "").lower()
            body, _ = read_capped(resp, PAGE_CAP)
            if "pdf" in ctype or body.startswith(b"%PDF"):
                raise MetadataError(f"{final} is a PDF, which carries no usable metadata", pdf_url=final)
            if ctype and not any(t in ctype for t in ("html", "xml", "text/plain")):
                raise MetadataError(f"{final} is not a web page ({ctype})")
            text = body.decode(resp.charset_encoding or "utf-8", errors="replace")
        p = _MetaParser()
        p.feed(text)
        meta = p.meta

        def first(*names: str) -> str | None:
            for n in names:
                if meta.get(n):
                    return meta[n][0]
            return None

        pdf_url = http_url(first("citation_pdf_url"), final)
        doi_m = DOI_RE.search(first("citation_doi", "prism.doi", "dc.identifier") or "")
        doi = _clean_doi(doi_m.group(1)) if doi_m else None
        if doi:
            rx = RXIV_DOI_RE.search(pdf_url or final)
            try:
                out = self.crossref(doi, int(rx.group(2)) if rx and rx.group(2) else None)
                out.pdf_url = out.pdf_url or pdf_url
                return out
            except (MetadataError, *_SOURCE_ERRORS):
                pass
        title = first("citation_title", "dc.title", "og:title") or p.title.strip()
        if not title:
            raise MetadataError(f"no paper metadata found at {final}")
        return PaperMeta(
            title=html.unescape(re.sub(r"\s+", " ", title)).strip(),
            authors=meta.get("citation_author", []) or meta.get("dc.creator", []),
            year=_year(
                first("citation_publication_date", "citation_date", "citation_online_date", "dc.date")
            ),
            venue=first("citation_journal_title", "citation_conference_title", "og:site_name")
            or urlparse(final).netloc,
            abstract=_strip_tags(
                first("citation_abstract", "dc.description", "description", "og:description")
            ),
            url=final,
            pdf_url=pdf_url,
            doi=doi,
            matched_by="web page",
        )

    def search_title(self, text: str) -> PaperMeta:
        resp = self.client.get(
            "https://api.crossref.org/works", params={"query.bibliographic": text, "rows": 5}
        )
        resp.raise_for_status()
        items = resp.json().get("message", {}).get("items", [])
        best, best_score = None, 0.0
        candidates = []
        for item in items:
            cand = re.sub(r"\s+", " ", (item.get("title") or [""])[0]).strip()
            if not cand:
                continue
            score = difflib.SequenceMatcher(None, _norm(cand), _norm(text)).ratio()
            if score > best_score:
                best, best_score = item, score
            parts = (item.get("issued") or {}).get("date-parts") or [[None]]
            candidates.append({"title": cand, "doi": item.get("DOI"), "year": (parts[0] or [None])[0]})
        if best is None or best_score < TITLE_MATCH:
            raise MetadataError(
                f"no confident match for title {text!r}; pass a DOI, arXiv ID or URL instead",
                candidates,
            )
        return self._from_crossref(best, matched_by="title search")
