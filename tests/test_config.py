from __future__ import annotations

import stat
import tomllib
from pathlib import Path

import pytest

from paperqueue.config import ICLOUD_PDF_DIR, ConfigError, default_home, load_config


def test_defaults_and_permissions(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cfg = load_config(home)
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE(cfg.config_path.stat().st_mode) == 0o600
    assert cfg.library.provider == "off"
    assert cfg.calendar.alert_minutes == 10
    assert cfg.calendar.backend in ("apple", "off")
    assert cfg.server.extra_hosts == []
    assert cfg.pdf.enabled is True
    assert cfg.pdf.dir == ICLOUD_PDF_DIR.expanduser()
    m = cfg.media
    assert (m.default_mode, m.max_height, m.max_video_mb, m.max_audio_mb, m.cookies_from_browser) == (
        "link", 480, 1500, 300, "")
    assert m.dir == (ICLOUD_PDF_DIR / "Media").expanduser()
    assert cfg.schedule.max_session_minutes == 90
    assert (cfg.media_lock_path, cfg.media_tmp_dir) == (home / "media.lock", home / "media-tmp")
    raw = tomllib.loads(cfg.config_path.read_text())
    assert raw["media"]["default_mode"] == "link" and raw["schedule"]["max_session_minutes"] == 90
    assert len(cfg.server.token) >= 20
    assert load_config(home).server.token == cfg.server.token
    tomllib.loads(cfg.config_path.read_text())


def test_default_home_from_env(tmp_path: Path) -> None:
    assert default_home() == tmp_path / "home"


@pytest.mark.parametrize(
    "text",
    ['[server]\nport = 8765\ntoken = ""\n', "[server]\nport = 8765\n", "[library]\nprovider = 'off'\n"],
)
def test_generated_token_is_persisted(tmp_path: Path, text: str) -> None:
    (tmp_path / "config.toml").write_text(text)
    token = load_config(tmp_path).server.token
    assert token
    raw = tomllib.loads((tmp_path / "config.toml").read_text())
    assert raw["server"]["token"] == token
    assert load_config(tmp_path).server.token == token


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[library]\nprovider = 'endnote'", "provider"),
        ("[server]\nport = 'abc'", "integer"),
        ("[server]\nport = 70000", "port"),
        ("[schedule]\ntimes = ['8:30']", "HH:MM"),
        ("[schedule]\ndays = ['funday']", "days"),
        ("[schedule]\ntimezone = 'Mars/Olympus'", "timezone"),
        ("[calendar]\nbackend = 'outlook'", "backend"),
        ("[pdf]\nenabled = 'yes'", "true or false"),
        ("[server]\nextra_hosts = 'pq.example'", "list"),
        ("[media]\ndefault_mode = 'mp3'", "default_mode"),
        ("[media]\nmax_height = 10", "max_height"),
        ("[media]\nmax_video_mb = 0", "max_video_mb"),
        ("[media]\ncookies_from_browser = '--exec=rm'", "cookies_from_browser"),
        ("[schedule]\nmax_session_minutes = 1000", "max_session_minutes"),
        ("this is = = not toml", "config.toml"),
    ],
)
def test_validation(tmp_path: Path, text: str, message: str) -> None:
    (tmp_path / "config.toml").write_text('[server]\ntoken = "t"\n' if "[server]" not in text else "")
    with (tmp_path / "config.toml").open("a") as f:
        f.write("\n" + text + "\n")
    with pytest.raises(ConfigError, match=message):
        load_config(tmp_path)


def test_file_values_and_env_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "config.toml").write_text(
        '[server]\ntoken = "file-token"\nextra_hosts = ["pq.tailnet.ts.net"]\n'
        '[pdf]\ndir = "~/Papers"\n[schedule]\ndays = ["Monday", "sat"]\ntimezone = "America/New_York"\n'
    )
    monkeypatch.setenv("ZOTERO_API_KEY", "secret")
    monkeypatch.setenv("PAPERQUEUE_LIBRARY", "zotero")
    monkeypatch.setenv("PAPERQUEUE_CALENDAR", "off")
    cfg = load_config(tmp_path)
    assert cfg.server.token == "file-token"
    assert cfg.server.extra_hosts == ["pq.tailnet.ts.net"]
    assert cfg.pdf.dir == Path.home() / "Papers"
    assert cfg.schedule.days == ["mon", "sat"]
    assert (cfg.zotero.api_key, cfg.library.provider, cfg.calendar.backend) == ("secret", "zotero", "off")
    monkeypatch.setenv("PAPERQUEUE_LIBRARY", "nope")
    with pytest.raises(ConfigError):
        load_config(tmp_path)
