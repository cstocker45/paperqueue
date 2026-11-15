from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from paperqueue import pdfcache
from paperqueue.config import Config
from paperqueue.db import Paper

from .conftest import FakeLibrary, Router

PDF = b"%PDF-1.7\n" + b"0" * 2000


def paper(**kw: object) -> Paper:
    base: dict[str, object] = {
        "id": 7,
        "title": "Attention: is all/you need? A <really> long title that keeps going and going",
        "authors": ["Ashish Vaswani", "Noam Shazeer"],
        "year": 2017,
        "pdf_url": "https://example.org/paper.pdf",
    }
    base.update(kw)
    return Paper(**base)  # type: ignore[arg-type]


@pytest.fixture
def pdf_cfg(cfg: Config, tmp_path: Path) -> Config:
    (tmp_path / "pdfs").mkdir()
    return cfg


def test_filename() -> None:
    assert pdfcache.filename(paper()) == "Vaswani 2017 - Attention is all you need A really long title that keeps.pdf"
    assert pdfcache.filename(paper(authors=["Curie, Marie"], year=None, title="..hidden")) == "Curie - ..hidden.pdf"
    assert pdfcache.filename(paper(authors=[], year=None, title="../../etc/passwd")) == "etc passwd.pdf"


def test_download(router: Router, pdf_cfg: Config) -> None:
    router.text("example.org", "/paper.pdf", PDF, "application/pdf")
    path = pdfcache.cache(paper(), None, pdf_cfg, router.client())
    assert path is not None and path.parent == pdf_cfg.pdf.dir
    assert path.read_bytes() == PDF
    assert [p.name for p in pdf_cfg.pdf.dir.iterdir()] == [path.name]  # no temp files left
    # a second paper with the same name gets a unique file
    other = pdfcache.cache(paper(id=8), None, pdf_cfg, router.client())
    assert other is not None and other.name.endswith(" (2).pdf")
    # already cached
    assert pdfcache.cache(paper(pdf_path=str(path)), None, pdf_cfg, router.client()) == path


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>paywall</html>", headers={"content-type": "text/html"}),
        httpx.Response(200, content=b"PK\x03\x04 zip", headers={"content-type": "application/octet-stream"}),
        httpx.Response(200, content=b"", headers={"content-type": "application/pdf"}),
        httpx.Response(200, content=PDF, headers={"content-type": "application/pdf", "content-length": str(10**9)}),
        httpx.Response(403, content=b"no"),
    ],
)
def test_download_rejected(router: Router, pdf_cfg: Config, response: httpx.Response) -> None:
    router.add("example.org", "/paper.pdf", response)
    assert pdfcache.cache(paper(), None, pdf_cfg, router.client()) is None
    assert list(pdf_cfg.pdf.dir.iterdir()) == []


def test_size_cap_while_streaming(router: Router, pdf_cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdfcache, "MAX_BYTES", 100)
    router.add("example.org", "/paper.pdf", lambda _r: httpx.Response(200, content=iter([PDF[:50], PDF[50:]])))
    assert pdfcache.cache(paper(), None, pdf_cfg, router.client()) is None
    assert list(pdf_cfg.pdf.dir.iterdir()) == []


def test_ssrf_guarded(router: Router, pdf_cfg: Config) -> None:
    assert pdfcache.cache(paper(pdf_url="http://127.0.0.1/x.pdf"), None, pdf_cfg, router.client()) is None
    assert router.requests == []


def test_copies_library_pdf(router: Router, pdf_cfg: Config, library: FakeLibrary, tmp_path: Path) -> None:
    source = tmp_path / "zotero.pdf"
    source.write_bytes(PDF)
    library.pdfs["K1"] = source
    path = pdfcache.cache(paper(library="zotero", library_key="K1", pdf_url=None), library, pdf_cfg, router.client())
    assert path is not None and path.read_bytes() == PDF
    assert source.exists()
    assert router.requests == []


def test_disabled_or_missing_dir(router: Router, cfg: Config, tmp_path: Path) -> None:
    cfg.pdf.dir = tmp_path / "no-icloud" / "PaperQueue"
    assert pdfcache.cache(paper(), None, cfg, router.client()) is None
    assert not cfg.pdf.dir.parent.exists()
    cfg.pdf.enabled = False
    assert pdfcache.cache(paper(), None, cfg, router.client()) is None
    assert router.requests == []


def test_retire(pdf_cfg: Config) -> None:
    f = pdf_cfg.pdf.dir / "Vaswani 2017 - Attention.pdf"
    f.write_bytes(PDF)
    moved = pdfcache.retire(paper(pdf_path=str(f)), pdf_cfg)
    assert moved == pdf_cfg.pdf.dir / "Read" / f.name
    assert moved.read_bytes() == PDF and not f.exists()
    assert pdfcache.retire(paper(pdf_path=str(moved)), pdf_cfg) is None
    assert pdfcache.retire(paper(), pdf_cfg) is None
