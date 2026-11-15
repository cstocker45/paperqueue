"""Opt-in local copies of YouTube videos (audio or video) for offline use, made by yt-dlp.

One detached `paperqueue media-worker` process is the only downloader; the web server, the MCP
server and the CLI only start it (`kick`). The worker holds a non-blocking flock on
<home>/media.lock and passes that descriptor to its yt-dlp child, so a worker that died
mid-download still blocks a second one until yt-dlp exits. Files are downloaded into
<home>/media-tmp/<video_id>/ (outside iCloud Drive) and the finished file is moved into the
Media folder. Watched, removed or switched items have their file deleted, not archived.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from . import metadata
from .config import Config
from .db import Paper, from_iso
from .pdfcache import safe_name

if TYPE_CHECKING:
    from .service import PaperQueue

log = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT = 60 * 60.0
RETRY_AFTER = timedelta(hours=1)
# The only files ever served or kept, each with a fixed media type.
SUFFIXES = {".m4a": "audio/mp4", ".mp4": "video/mp4", ".webm": "video/webm"}

# media_error codes -> the one-line fix shown on the card, in MCP summaries and in doctor.
FIXES = {
    "rate_limited": "YouTube is rate-limiting downloads; paperqueue retries every hour.",
    "network": "The download was interrupted (network or timeout); paperqueue retries every hour.",
    "unknown": "The download failed; paperqueue retries every hour. If it keeps failing, run paperqueue doctor.",
    "bot_check": 'YouTube wants a sign-in: set [media] cookies_from_browser = "safari" (or your browser), '
    "then choose the mode again.",
    "unavailable": "The video is private, removed or restricted; keep it as a link.",
    "no_deno": "Downloads need Deno: brew install deno, then choose the mode again.",
    "no_ffmpeg": "Video downloads need ffmpeg: brew install ffmpeg, then choose the mode again (or try audio).",
    "too_large": "Larger than the size cap ([media] max_video_mb / max_audio_mb); try audio.",
    "outdated": "yt-dlp needs an update: uv tool upgrade paperqueue, then choose the mode again.",
    "no_folder": "The Media folder can't be created: turn on iCloud Drive or set [media] dir.",
}
RETRYABLE = frozenset({"rate_limited", "network", "unknown"})

# First match wins; yt-dlp's ERROR lines are searched before the rest of its output.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(p, re.I), code)
    for p, code in (
        (r"no module named .?yt_dlp", "outdated"),
        (r"larger than max-filesize|file is larger than", "too_large"),
        (r"sign in to confirm|not a bot|use --cookies", "bot_check"),
        (r"http error 429|too many requests|rate[- ]limit", "rate_limited"),
        (r"no supported javascript runtime", "no_deno"),
        (r"ffmpeg (?:is )?not (?:installed|found)|ffprobe .*not found", "no_ffmpeg"),
        (
            (
                r"private video|video unavailable|this video is (?:not available|unavailable)|"
                r"has been removed|members[- ]only|join this channel|premieres in|"
                r"this live event will begin|not available in your country"
            ),
            "unavailable",
        ),
        (
            (
                r"requested format is not available|unable to extract|signature extraction failed|"
                r"nsig extraction failed|please report this issue|update to the latest version"
            ),
            "outdated",
        ),
        (
            (
                r"timed out|connection (?:reset|refused|aborted)|temporary failure in name resolution|"
                r"nodename nor servname|network is unreachable|unable to download|http error 5\d\d|"
                r"incompleteread|remote end closed"
            ),
            "network",
        ),
    )
]


class MediaError(Exception):
    """A download failed; `code` is a FIXES key."""

    def __init__(self, code: str):
        super().__init__(FIXES[code])
        self.code = code


def fix_for(code: str | None) -> str | None:
    return FIXES.get(code or "")


def format_length(seconds: int) -> str:
    """42 -> "1 min", 2520 -> "42 min", 4320 -> "1 h 12 min"."""
    minutes = max(1, round(seconds / 60))
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"{minutes} min"
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def classify(output: str) -> tuple[str, bool]:
    """yt-dlp's output after a failure -> (media_error code, retry automatically?)."""
    errors = "\n".join(line for line in output.splitlines() if "ERROR" in line)
    for text in (errors, output):
        for pattern, code in _PATTERNS:
            if pattern.search(text):
                return code, code in RETRYABLE
    return "unknown", True


def tools() -> dict[str, bool]:
    """Which of the external tools downloads use are installed."""
    return {name: metadata.find_tool(name) is not None for name in ("deno", "ffmpeg")}


def preflight(mode: str) -> str | None:
    """The error code a download in `mode` is bound to fail with for lack of a tool, if any."""
    if mode == "link":
        return None
    have = tools()
    if not have["deno"]:
        return "no_deno"
    if mode == "video" and not have["ffmpeg"]:
        return "no_ffmpeg"
    return None


def build_args(mode: str, cfg: Config, have_ffmpeg: bool) -> list[str]:
    """yt-dlp options for a download in `mode` ("audio" or "video"), within the size caps."""
    m = cfg.media
    if mode == "audio":
        cap = m.max_audio_mb
        # Without ffmpeg, the DASH m4a is kept as is (best effort; plays in most players).
        fmt = ["-f", "ba[ext=m4a]/ba", "-x", "--audio-format", "m4a"] if have_ffmpeg else ["-f", "ba[ext=m4a]"]
    elif mode == "video":
        if not have_ffmpeg:
            raise MediaError("no_ffmpeg")
        cap = m.max_video_mb
        h = m.max_height
        fmt = [
            "-f", f"bv*[height<={h}]+ba/b[height<={h}]",
            "-S", "vcodec:h264,acodec:aac",
            "--merge-output-format", "mp4",
        ]
    else:
        raise ValueError(f"no download for media mode {mode!r}")
    args = ["--no-playlist", "--no-progress", "--no-mtime", "--max-filesize", f"{cap}M", *fmt]
    if m.cookies_from_browser.strip():
        args += ["--cookies-from-browser", m.cookies_from_browser.strip()]
    return args


def filename(paper: Paper, suffix: str) -> str:
    """`<short title> [<video id>].<ext>`: readable in the Files app, unique per video."""
    short = ""
    for word in safe_name(paper.title).split():
        if len(short) + len(word) + 1 > 60:
            break
        short = f"{short} {word}".strip()
    short = short.lstrip(". ") or "video"
    return f"{short} [{paper.video_id}]{suffix}"


def _media_dir(cfg: Config) -> Path:
    """The Media folder, created if its grandparent exists (so a missing iCloud Drive is not faked)."""
    directory = cfg.media.dir
    try:
        directory.parent.mkdir(exist_ok=True)
        directory.mkdir(exist_ok=True)
    except OSError as exc:
        log.info("media folder unavailable (%s): %s", directory, exc)
        raise MediaError("no_folder") from exc
    return directory


def download(paper: Paper, cfg: Config, pass_fds: tuple[int, ...] = ()) -> Path:
    """Download `paper` in its media mode and move the file into the Media folder.
    Raises MediaError. `pass_fds` are inherited by yt-dlp (the worker's lock)."""
    mode = paper.media_mode
    if not paper.video_id or mode not in ("audio", "video"):
        raise ValueError(f"#{paper.id} has nothing to download")
    if code := preflight(mode):
        raise MediaError(code)
    ffmpeg = metadata.find_tool("ffmpeg")
    args = build_args(mode, cfg, ffmpeg is not None)
    if ffmpeg:
        args += ["--ffmpeg-location", ffmpeg]
    tmp = cfg.media_tmp_dir / paper.video_id
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        try:
            proc = metadata.run_ytdlp(
                [*args, *metadata.js_runtime_args(), "-P", str(tmp), "-o", "%(id)s.%(ext)s", "--",
                 metadata.watch_url(paper.video_id)],
                timeout=DOWNLOAD_TIMEOUT,
                pass_fds=pass_fds,
            )
        except subprocess.TimeoutExpired:
            raise MediaError("network") from None
        output = f"{proc.stdout}\n{proc.stderr}"
        files = [
            f for f in tmp.iterdir()
            if f.is_file() and f.stem == paper.video_id and f.suffix.lower() in SUFFIXES
        ]
        if proc.returncode != 0 or not files:
            log.info("yt-dlp failed for #%s: %s", paper.id, output.strip()[-2000:])
            raise MediaError(classify(output)[0])
        source = files[0]
        cap_mb = cfg.media.max_audio_mb if mode == "audio" else cfg.media.max_video_mb
        if source.stat().st_size > cap_mb * 2**20:
            raise MediaError("too_large")
        target = _media_dir(cfg) / filename(paper, source.suffix.lower())
        shutil.move(source, target)
        return target
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def discard(paper: Paper) -> None:
    """Delete the downloaded file (and an iCloud placeholder of it), if any."""
    if not paper.media_path:
        return
    path = Path(paper.media_path)
    for candidate in (path, path.with_name(f".{path.name}.icloud")):
        try:
            candidate.unlink(missing_ok=True)
        except OSError as exc:
            log.info("could not delete %s: %s", candidate, exc)


def is_due(paper: Paper) -> bool:
    """Should the worker try `paper` now? Failed items retry hourly, and only for transient
    errors. (Compares with the wall clock, which stamps `updated_at`.)"""
    if paper.media_status != "failed":
        return True
    updated = from_iso(paper.updated_at)
    return paper.media_error in RETRYABLE and (updated is None or datetime.now(UTC) - updated >= RETRY_AFTER)


def worker(queue: PaperQueue) -> int:
    """Download every due item, one at a time. Returns the number of files saved, or 0 at once
    when another worker holds the lock."""
    cfg = queue.cfg
    fd = os.open(cfg.media_lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        saved = 0
        tried: set[tuple[int, str]] = set()  # (id, mode): a mode switched mid-download is still tried
        while True:
            due = [
                p for p in queue.store.list_media_pending()
                if (p.id, p.media_mode) not in tried and is_due(p)
            ]
            if not due:
                return saved
            paper = due[0]
            assert paper.id is not None
            mode = paper.media_mode
            tried.add((paper.id, mode))
            queue.media_result(paper.id, mode, "downloading")
            try:
                path = download(paper, cfg, pass_fds=(fd,))
            except MediaError as exc:
                log.warning("download of #%s failed: %s", paper.id, exc.code)
                queue.media_result(paper.id, mode, "failed", error=exc.code)
                continue
            except OSError as exc:
                log.warning("download of #%s failed: %s", paper.id, exc)
                queue.media_result(paper.id, mode, "failed", error="unknown")
                continue
            if queue.media_result(paper.id, mode, "done", path=path):
                saved += 1
    finally:
        os.close(fd)  # releases the lock unless a yt-dlp child still holds it


def _locked(path: Path) -> bool:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def kick(cfg: Config) -> bool:
    """Start a detached media worker unless one is running. Returns whether one was started."""
    if _locked(cfg.media_lock_path):
        return False
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PAPERQUEUE_HOME": str(cfg.home)}
    with open(cfg.logs_dir / "media.log", "ab") as log_file:
        subprocess.Popen(
            [sys.executable, "-m", "paperqueue", "media-worker"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            cwd=cfg.home,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    return True
