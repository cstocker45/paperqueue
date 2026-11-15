"""Best-effort PDF copies in iCloud Drive, for offline reading in the iPhone Files app."""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import unicodedata
from pathlib import Path
from typing import BinaryIO

import httpx

from .config import Config
from .db import Paper
from .metadata import MetadataError, open_public
from .types import LibraryProvider

log = logging.getLogger(__name__)

MAX_BYTES = 60 * 1024 * 1024
READ_DIR = "Read"
_UNSAFE = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]+')


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    return " ".join(_UNSAFE.sub(" ", text).split())


def _surname(author: str) -> str:
    author = author.strip()
    if "," in author:
        return author.split(",", 1)[0].strip()
    parts = author.split()
    return parts[-1] if parts else ""


def filename(paper: Paper) -> str:
    """`<FirstAuthorSurname> <year> - <short title>.pdf`, safe on APFS/iCloud and in URLs."""
    words = _clean(paper.title).split()
    short = ""
    for word in words:
        if len(short) + len(word) + 1 > 60:
            break
        short = f"{short} {word}".strip()
    short = short or (words[0][:60] if words else f"paper {paper.id}")
    lead = " ".join(
        x for x in (_clean(_surname(paper.authors[0])) if paper.authors else "", str(paper.year or "")) if x
    )
    name = f"{lead} - {short}" if lead else short
    return name.lstrip(". ")[:150] + ".pdf"


def _unique(directory: Path, name: str) -> Path:
    path = directory / name
    stem = path.stem
    n = 2
    while path.exists():
        path = directory / f"{stem} ({n}).pdf"
        n += 1
    return path


def _pdf_dir(cfg: Config) -> Path | None:
    """The cache dir, created if its parent exists (so a missing iCloud Drive is not faked)."""
    directory = cfg.pdf.dir
    try:
        directory.mkdir(exist_ok=True)
    except OSError as exc:
        log.info("PDF cache unavailable (%s): %s", directory, exc)
        return None
    return directory


def _download(url: str, client: httpx.Client, out: BinaryIO) -> None:
    with open_public(client, url) as resp:
        ctype = resp.headers.get("content-type", "").lower()
        if "html" in ctype:
            raise MetadataError(f"{url} returned a web page, not a PDF")
        length = resp.headers.get("content-length", "")
        if length.isdigit() and int(length) > MAX_BYTES:
            raise MetadataError(f"{url} is larger than {MAX_BYTES // 2**20} MB")
        head = b""
        size = 0
        for chunk in resp.iter_bytes():
            if len(head) < 4:
                head += chunk[:4]
                if len(head) >= 4 and not head.startswith(b"%PDF"):
                    raise MetadataError(f"{url} is not a PDF")
            size += len(chunk)
            if size > MAX_BYTES:
                raise MetadataError(f"{url} is larger than {MAX_BYTES // 2**20} MB")
            out.write(chunk)
        if not head.startswith(b"%PDF"):
            raise MetadataError(f"{url} is not a PDF")


def _copy(source: Path, out: BinaryIO) -> None:
    with source.open("rb") as f:
        if f.read(4) != b"%PDF":
            raise MetadataError(f"{source.name} is not a PDF")
        if source.stat().st_size > MAX_BYTES:
            raise MetadataError(f"{source.name} is larger than {MAX_BYTES // 2**20} MB")
        f.seek(0)
        shutil.copyfileobj(f, out)


def cache(
    paper: Paper, provider: LibraryProvider | None, cfg: Config, client: httpx.Client
) -> Path | None:
    """Store a PDF for `paper` in the cache dir; return its path, or None if none was obtained.
    Prefers a PDF the library already holds, then the open-access `pdf_url`."""
    if not cfg.pdf.enabled:
        return None
    if paper.pdf_path and Path(paper.pdf_path).is_file():
        return Path(paper.pdf_path)
    directory = _pdf_dir(cfg)
    if directory is None:
        return None

    local: Path | None = None
    if provider is not None and paper.library == provider.name and paper.library_key:
        try:
            local = provider.pdf_file(paper.library_key)
        except Exception as exc:  # provider code is best-effort by contract
            log.info("library PDF lookup failed for #%s: %s", paper.id, exc)
    if local is None and not paper.pdf_url:
        return None

    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".paperqueue-", suffix=".part")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out:
            if local is not None:
                _copy(local, out)
            elif paper.pdf_url:
                _download(paper.pdf_url, client, out)
        target = _unique(directory, filename(paper))
        os.replace(tmp, target)
        return target
    except (OSError, httpx.HTTPError, MetadataError) as exc:
        log.info("could not cache PDF for #%s: %s", paper.id, exc)
        return None
    finally:
        tmp.unlink(missing_ok=True)


def retire(paper: Paper, cfg: Config) -> Path | None:
    """Move a cached PDF into `Read/` (after mark-read or remove). Returns the new path."""
    if not paper.pdf_path:
        return None
    source = Path(paper.pdf_path)
    read_dir = cfg.pdf.dir / READ_DIR
    if not source.is_file() or source.parent == read_dir:
        return None
    try:
        read_dir.mkdir(parents=True, exist_ok=True)
        target = _unique(read_dir, source.name)
        shutil.move(source, target)
    except OSError as exc:
        log.info("could not move PDF for #%s: %s", paper.id, exc)
        return None
    return target
