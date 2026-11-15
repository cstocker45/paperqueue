"""Media cache: yt-dlp arguments, error classification, the worker lock, caps and cleanup.
yt-dlp is always faked (tests.conftest.FakeYtDlp); nothing is downloaded."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from paperqueue import mediacache, metadata
from paperqueue.config import Config
from paperqueue.db import Paper, to_iso
from paperqueue.mediacache import MediaError, build_args, classify, download, worker
from paperqueue.service import PaperQueue

from .conftest import VIDEO_ID, FakeCalendar, FakeYtDlp

REAL_KICK = mediacache.kick  # the autouse `kicks` fixture replaces it after import
REAL_RUN = metadata.run_ytdlp
URL = f"https://youtu.be/{VIDEO_ID}"


def video(**kw: object) -> Paper:
    fields = {"id": 1, "title": "How Transformers Work", "kind": "video", "video_id": VIDEO_ID,
              "media_mode": "audio", "media_status": "pending"} | kw
    return Paper(**fields)  # type: ignore[arg-type]


# --- arguments ------------------------------------------------------------------


def test_build_args_audio(cfg: Config) -> None:
    with_ffmpeg = build_args("audio", cfg, True)
    assert with_ffmpeg[with_ffmpeg.index("-f") + 1] == "ba[ext=m4a]/ba"
    assert ["--audio-format", "m4a"] == with_ffmpeg[with_ffmpeg.index("--audio-format"):][:2]
    assert with_ffmpeg[with_ffmpeg.index("--max-filesize") + 1] == "300M"
    assert "--no-playlist" in with_ffmpeg and "--cookies-from-browser" not in with_ffmpeg
    without = build_args("audio", cfg, False)
    assert without[without.index("-f") + 1] == "ba[ext=m4a]" and "-x" not in without


def test_build_args_video(cfg: Config) -> None:
    cfg.media.cookies_from_browser = "safari"
    args = build_args("video", cfg, True)
    assert args[args.index("-f") + 1] == "bv*[height<=480]+ba/b[height<=480]"
    assert args[args.index("--merge-output-format") + 1] == "mp4"
    assert args[args.index("--max-filesize") + 1] == "1500M"
    assert args[args.index("--cookies-from-browser") + 1] == "safari"
    with pytest.raises(MediaError) as exc:
        build_args("video", cfg, False)  # never the unreliable b[ext=mp4] fallback
    assert exc.value.code == "no_ffmpeg" and "brew install ffmpeg" in str(exc.value)
    with pytest.raises(ValueError):
        build_args("link", cfg, True)


@pytest.mark.parametrize(
    ("output", "code", "retry"),
    [
        ("ERROR: [youtube] x: Sign in to confirm you’re not a bot. Use --cookies-from-browser", "bot_check", False),
        ("ERROR: unable to download video data: HTTP Error 429: Too Many Requests", "rate_limited", True),
        ("ERROR: [youtube] x: Private video. Sign in if you've been granted access", "unavailable", False),
        ("ERROR: [youtube] x: Video unavailable. This video has been removed by the uploader", "unavailable", False),
        ("WARNING: No supported JavaScript runtime could be found\nERROR: Requested format is not available",
         "outdated", False),
        ("WARNING: [youtube] No supported JavaScript runtime could be found", "no_deno", False),
        ("ERROR: Postprocessing: ffmpeg not found. Please install or provide the path", "no_ffmpeg", False),
        ("File is larger than max-filesize (2000000 bytes > 1000 bytes). Aborting.", "too_large", False),
        ("ERROR: [youtube] x: Unable to extract nsig function code; please report this issue", "outdated", False),
        ("/usr/bin/python3: No module named yt_dlp", "outdated", False),
        ("ERROR: Unable to download webpage: <urlopen error [Errno 8] nodename nor servname provided>",
         "network", True),
        ("ERROR: Read timed out.", "network", True),
        ("Segmentation fault", "unknown", True),
    ],
)
def test_classify(output: str, code: str, retry: bool) -> None:
    assert classify(output) == (code, retry)
    assert mediacache.fix_for(code)


def test_error_lines_win_over_warnings() -> None:
    out = "WARNING: [youtube] Sign in to confirm you're not a bot\nERROR: HTTP Error 429: Too Many Requests"
    assert classify(out) == ("rate_limited", True)


def test_format_length() -> None:
    assert [mediacache.format_length(s) for s in (20, 2520, 3600, 4320)] == ["1 min", "42 min", "1 h", "1 h 12 min"]


# --- download ----------------------------------------------------------------------


def test_download_moves_file_and_cleans_up(cfg: Config, ytdlp: FakeYtDlp) -> None:
    path = download(video(), cfg, pass_fds=(7,))
    assert path == cfg.media.dir / f"How Transformers Work [{VIDEO_ID}].m4a"
    assert path.read_bytes() == b"\0" * 1000
    assert not (cfg.media_tmp_dir / VIDEO_ID).exists()
    (args,) = ytdlp.downloads
    assert ytdlp.fds[-1] == (7,)
    assert args[-2:] == ["--", f"https://www.youtube.com/watch?v={VIDEO_ID}"]
    assert args[args.index("-P") + 1] == str(cfg.media_tmp_dir / VIDEO_ID)
    assert args[args.index("--js-runtimes") + 1] == "deno:/opt/homebrew/bin/deno"
    assert args[args.index("--ffmpeg-location") + 1] == "/opt/homebrew/bin/ffmpeg"


def test_download_failure_is_classified_and_cleaned(cfg: Config, ytdlp: FakeYtDlp) -> None:
    ytdlp.returncode = 1
    ytdlp.output = "ERROR: [youtube] x: Sign in to confirm you're not a bot"
    with pytest.raises(MediaError) as exc:
        download(video(), cfg)
    assert exc.value.code == "bot_check"
    assert not (cfg.media_tmp_dir / VIDEO_ID).exists() and not cfg.media.dir.exists()


def test_download_over_cap(cfg: Config, ytdlp: FakeYtDlp) -> None:
    cfg.media.max_audio_mb = 1
    ytdlp.size = 2 * 2**20
    with pytest.raises(MediaError) as exc:
        download(video(), cfg)
    assert exc.value.code == "too_large" and "try audio" in str(exc.value)
    assert not (cfg.media_tmp_dir / VIDEO_ID).exists()
    # yt-dlp's own --max-filesize abort exits 0 without a file
    ytdlp.size, ytdlp.returncode = 10, 0
    ytdlp.on_download = lambda args, tmp: None
    ytdlp.output = "[download] File is larger than max-filesize (400000000 bytes > 314572800 bytes). Aborting."
    with pytest.raises(MediaError, match="size cap"):
        download(video(), cfg)


def test_download_timeout_is_a_network_error(cfg: Config, ytdlp: FakeYtDlp, monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(args: list[str], timeout: float, pass_fds: tuple[int, ...] = ()) -> subprocess.CompletedProcess[str]:
        assert timeout == 3600
        raise subprocess.TimeoutExpired(args, timeout)

    monkeypatch.setattr(metadata, "run_ytdlp", slow)
    with pytest.raises(MediaError) as exc:
        download(video(), cfg)
    assert exc.value.code == "network"


def test_download_needs_deno_and_ffmpeg(cfg: Config, ytdlp: FakeYtDlp, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metadata, "find_tool", lambda name: None)
    with pytest.raises(MediaError, match="brew install deno"):
        download(video(), cfg)
    monkeypatch.setattr(metadata, "find_tool", lambda name: "/x/deno" if name == "deno" else None)
    with pytest.raises(MediaError, match="brew install ffmpeg"):
        download(video(media_mode="video"), cfg)
    assert ytdlp.downloads == []


def test_download_without_icloud_drive(cfg: Config, ytdlp: FakeYtDlp, tmp_path: Path) -> None:
    cfg.media.dir = tmp_path / "no-icloud" / "PaperQueue" / "Media"
    with pytest.raises(MediaError) as exc:
        download(video(), cfg)
    assert exc.value.code == "no_folder"


def test_discard(tmp_path: Path) -> None:
    f = tmp_path / "x.m4a"
    f.write_bytes(b"a")
    placeholder = tmp_path / ".x.m4a.icloud"
    placeholder.write_bytes(b"")
    mediacache.discard(video(media_path=str(f)))
    assert not f.exists() and not placeholder.exists()
    mediacache.discard(video(media_path=None))  # no file: no-op


def test_run_ytdlp_kills_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metadata, "YTDLP", [sys.executable, "-c", "import time; time.sleep(30)"])
    with pytest.raises(subprocess.TimeoutExpired):
        REAL_RUN([], timeout=0.3)
    monkeypatch.setattr(metadata, "YTDLP", [sys.executable, "-c", "import sys; print('out'); sys.exit(3)"])
    proc = REAL_RUN([], timeout=10)
    assert (proc.returncode, proc.stdout.strip()) == (3, "out")


# --- worker ------------------------------------------------------------------------------


def add_video(queue: PaperQueue, mode: str = "audio") -> Paper:
    res = queue.add(URL, media=mode, when="next")
    assert res.paper.id is not None
    return res.paper


def test_worker_downloads_and_records(queue: PaperQueue, ytdlp: FakeYtDlp, calendar: FakeCalendar, kicks: list) -> None:
    p = add_video(queue)
    assert p.media_status == "pending" and len(kicks) == 1
    assert worker(queue) == 1
    done = queue.get(p.id or 0)
    assert done.media_status == "done" and done.media_error is None
    assert Path(done.media_path or "").is_file()
    assert done.to_dict()["has_media"] is True and "media_path" not in done.to_dict()
    event = calendar.events[done.calendar_uid or ""]
    assert event.title.startswith("Listen: ")
    assert f"Offline audio (Files app): How Transformers Work [{VIDEO_ID}].m4a" in event.notes
    assert worker(queue) == 0  # nothing left
    queue.mark_read(p.id or 0)
    assert not Path(done.media_path or "").exists()
    watched = queue.get(p.id or 0)
    assert watched.media_path is None and watched.media_status is None


def test_worker_lock_contention(queue: PaperQueue, ytdlp: FakeYtDlp, cfg: Config) -> None:
    add_video(queue)
    fd = os.open(cfg.media_lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert worker(queue) == 0
        assert ytdlp.downloads == []
    finally:
        os.close(fd)
    assert worker(queue) == 1
    assert ytdlp.fds[-1] and all(isinstance(x, int) for x in ytdlp.fds[-1])  # the lock goes to yt-dlp


def test_worker_retry_policy(queue: PaperQueue, ytdlp: FakeYtDlp) -> None:
    p = add_video(queue)
    ytdlp.returncode, ytdlp.output = 1, "ERROR: HTTP Error 429: Too Many Requests"
    assert worker(queue) == 0
    failed = queue.get(p.id or 0)
    assert (failed.media_status, failed.media_error) == ("failed", "rate_limited")
    assert worker(queue) == 0 and len(ytdlp.downloads) == 1  # not due yet
    ytdlp.returncode = 0
    with queue.store.transaction() as con:  # pretend the failure was an hour ago
        con.execute("UPDATE papers SET updated_at = ? WHERE id = ?",
                    (to_iso(datetime.now(UTC) - timedelta(hours=1, minutes=1)), p.id))
    assert worker(queue) == 1

    q2 = queue.add("https://www.youtube.com/watch?v=abcdefghijk", media="audio").paper
    ytdlp.returncode, ytdlp.output = 1, "ERROR: Private video"
    worker(queue)
    assert queue.get(q2.id or 0).media_error == "unavailable"
    old = to_iso(datetime.now(UTC) - timedelta(days=1))
    with queue.store.transaction() as con:
        con.execute("UPDATE papers SET updated_at = ? WHERE id = ?", (old, q2.id))
    calls = len(ytdlp.downloads)
    assert worker(queue) == 0 and len(ytdlp.downloads) == calls  # terminal: only set_media retries
    ytdlp.returncode = 0
    res = queue.set_media(q2.id or 0, "audio")
    assert res.paper.media_status == "pending"
    assert worker(queue) == 1


def test_mode_switch_during_download_drops_the_file(queue: PaperQueue, ytdlp: FakeYtDlp, cfg: Config) -> None:
    p = add_video(queue)
    assert p.id is not None

    def switch(args: list[str], tmp: Path) -> None:
        assert queue.get(p.id or 0).media_status == "downloading"
        (tmp / f"{VIDEO_ID}.m4a").write_bytes(b"a")
        queue.set_media(p.id or 0, "link")

    ytdlp.on_download = switch
    assert worker(queue) == 0
    now = queue.get(p.id)
    assert (now.media_mode, now.media_status, now.media_path) == ("link", None, None)
    assert list(cfg.media.dir.iterdir()) == []


def test_remove_deletes_media(queue: PaperQueue, ytdlp: FakeYtDlp) -> None:
    p = add_video(queue)
    worker(queue)
    path = Path(queue.get(p.id or 0).media_path or "")
    assert path.is_file()
    queue.remove(p.id or 0)
    assert not path.exists()


# --- kick ----------------------------------------------------------------------------------


def test_kick_spawns_detached_worker(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[tuple[list[str], dict]] = []

    class FakePopen:
        def __init__(self, cmd: list[str], **kw: object) -> None:
            started.append((cmd, kw))

    monkeypatch.setattr(mediacache.subprocess, "Popen", FakePopen)
    assert REAL_KICK(cfg) is True
    ((cmd, kw),) = started
    assert cmd == [sys.executable, "-m", "paperqueue", "media-worker"]
    assert kw["start_new_session"] is True and kw["stdin"] is subprocess.DEVNULL
    assert kw["stdout"] is subprocess.DEVNULL  # never the MCP stdio channel
    assert kw["env"]["PAPERQUEUE_HOME"] == str(cfg.home)  # type: ignore[index]

    fd = os.open(cfg.media_lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert REAL_KICK(cfg) is False
    finally:
        os.close(fd)
    assert len(started) == 1
