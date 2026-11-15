"""Configuration: ~/.paperqueue/config.toml, with environment overrides."""

from __future__ import annotations

import os
import re
import secrets
import sys
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ICLOUD_PDF_DIR = Path("~/Library/Mobile Documents/com~apple~CloudDocs/PaperQueue")

DEFAULT_CONFIG = """\
# paperqueue configuration. Edit and restart `paperqueue serve`.

[server]
host = "127.0.0.1"
port = 8765
# Required when the app is opened from another device (e.g. your iPhone with --lan).
token = "{token}"
# Extra Host names the web server accepts (e.g. a Tailscale name).
extra_hosts = []

[library]
# Which reference manager your "To read" list lives in: "zotero", "mendeley" or "off".
provider = "off"

[zotero]
# "local" reads Zotero 7's local API (Zotero desktop must be running, with
#   Settings > Advanced > "Allow other applications on this computer to communicate with Zotero" on).
# "web" reads the Zotero web API (needs user_id + api_key).
source = "local"
collection = "To read"
# With source = "local", papers you add here (or via an agent) are written into this collection
# through Zotero desktop's local connector: no account or key, but Zotero must be open (papers
# added while it is closed are added when it is next open). Zotero attaches the PDF.
# Optional: a web API key writes through api.zotero.org instead (it takes precedence when set).
# Create one at https://www.zotero.org/settings/keys, or set ZOTERO_USER_ID / ZOTERO_API_KEY.
user_id = ""
api_key = ""
push_new_papers = true

[mendeley]
# Mendeley has no local API, so paperqueue uses the Mendeley web API (OAuth).
# Register an app at https://dev.mendeley.com/myapps.html with redirect URL
# http://127.0.0.1:8766/callback, paste the ID/secret here (or set MENDELEY_CLIENT_ID /
# MENDELEY_CLIENT_SECRET), then run `paperqueue mendeley login`.
client_id = ""
client_secret = ""
folder = "To read"
push_new_papers = true
redirect_port = 8766

[calendar]
# "apple" writes reading sessions into an Apple Calendar calendar; "off" disables it.
backend = "{backend}"
# Make it an iCloud calendar so the sessions (and their alerts) reach your iPhone.
apple_calendar = "Reading List"
# Minutes before a session that the calendar alert fires.
alert_minutes = 10

[pdf]
# Keep a copy of open-access PDFs in iCloud Drive for offline reading in the Files app.
enabled = true
dir = "{pdf_dir}"

[media]
# YouTube videos: "link" (just the link), "audio" or "video" (a local copy, made by yt-dlp,
# in `dir` for the iPhone Files app). Downloads need Deno; video also needs ffmpeg:
#   brew install deno ffmpeg
# Downloading is opt-in and your responsibility under YouTube's Terms of Service.
default_mode = "link"
dir = "{media_dir}"
max_height = 480
max_video_mb = 1500
max_audio_mb = 300
# If YouTube asks to confirm you are not a bot, read its cookies from a browser you are
# signed in with: "safari", "chrome", "firefox", ... Empty = no cookies.
cookies_from_browser = ""

[schedule]
# Reading slots used by "next free slot" and auto-scheduling.
days = ["mon", "tue", "wed", "thu", "fri"]
times = ["08:30"]
duration_minutes = 45
horizon_days = 14
# Longest session booked for a video (sessions otherwise last as long as the video).
max_session_minutes = 90
# IANA timezone, e.g. "Europe/Berlin". Empty = system timezone.
timezone = ""
"""

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
PROVIDERS = ("off", "zotero", "mendeley")
ZOTERO_SOURCES = ("local", "web")
CALENDAR_BACKENDS = ("off", "apple")
MEDIA_MODES = ("link", "audio", "video")
COOKIE_BROWSERS = ("brave", "chrome", "chromium", "edge", "firefox", "opera", "safari", "vivaldi", "whale")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ConfigError(Exception):
    pass


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    token: str = ""
    extra_hosts: list[str] = field(default_factory=list)


@dataclass
class LibraryConfig:
    provider: str = "off"


@dataclass
class ZoteroConfig:
    source: str = "local"
    collection: str = "To read"
    user_id: str = ""
    api_key: str = ""
    push_new_papers: bool = True

    @property
    def can_write(self) -> bool:
        return bool(self.user_id and self.api_key)


@dataclass
class MendeleyConfig:
    client_id: str = ""
    client_secret: str = ""
    folder: str = "To read"
    push_new_papers: bool = True
    redirect_port: int = 8766

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.redirect_port}/callback"


@dataclass
class CalendarConfig:
    backend: str = "apple" if sys.platform == "darwin" else "off"
    apple_calendar: str = "Reading List"
    alert_minutes: int = 10


@dataclass
class PdfConfig:
    enabled: bool = True
    dir: Path = field(default_factory=lambda: ICLOUD_PDF_DIR.expanduser())


@dataclass
class MediaConfig:
    default_mode: str = "link"
    dir: Path = field(default_factory=lambda: (ICLOUD_PDF_DIR / "Media").expanduser())
    max_height: int = 480
    max_video_mb: int = 1500
    max_audio_mb: int = 300
    cookies_from_browser: str = ""


@dataclass
class ScheduleConfig:
    days: list[str] = field(default_factory=lambda: ["mon", "tue", "wed", "thu", "fri"])
    times: list[str] = field(default_factory=lambda: ["08:30"])
    duration_minutes: int = 45
    horizon_days: int = 14
    max_session_minutes: int = 90
    timezone: str = ""


@dataclass
class Config:
    home: Path
    server: ServerConfig = field(default_factory=ServerConfig)
    library: LibraryConfig = field(default_factory=LibraryConfig)
    zotero: ZoteroConfig = field(default_factory=ZoteroConfig)
    mendeley: MendeleyConfig = field(default_factory=MendeleyConfig)
    calendar: CalendarConfig = field(default_factory=CalendarConfig)
    pdf: PdfConfig = field(default_factory=PdfConfig)
    media: MediaConfig = field(default_factory=MediaConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)

    @property
    def db_path(self) -> Path:
        return self.home / "paperqueue.db"

    @property
    def mendeley_token_path(self) -> Path:
        return self.home / "mendeley_token.json"

    @property
    def config_path(self) -> Path:
        return self.home / "config.toml"

    @property
    def calendar_lock_path(self) -> Path:
        return self.home / "calendar.lock"

    @property
    def media_lock_path(self) -> Path:
        return self.home / "media.lock"

    @property
    def media_tmp_dir(self) -> Path:
        """Downloads land here first (outside iCloud Drive), then move into `media.dir`."""
        return self.home / "media-tmp"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"


def default_home() -> Path:
    return Path(os.environ.get("PAPERQUEUE_HOME", Path.home() / ".paperqueue")).expanduser()


def _coerce(section: str, key: str, default: Any, value: Any) -> Any:
    where = f"[{section}] {key}"
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ConfigError(f"{where} must be true or false")
    elif isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where} must be an integer")
    elif isinstance(default, Path):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{where} must be a path")
        return Path(value).expanduser()
    elif isinstance(default, str):
        if not isinstance(value, str):
            raise ConfigError(f"{where} must be a string")
    elif isinstance(default, list):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{where} must be a list of strings")
    return value


def _apply(name: str, section: object, values: Any) -> None:
    if not isinstance(values, dict):
        raise ConfigError(f"[{name}] must be a table")
    for f in fields(section):  # type: ignore[arg-type]
        if f.name in values:
            default = getattr(section, f.name)
            setattr(section, f.name, _coerce(name, f.name, default, values[f.name]))


def validate(cfg: Config) -> None:
    """Raise ConfigError for values that would break the app at runtime."""
    if not 1 <= cfg.server.port <= 65535:
        raise ConfigError("[server] port must be between 1 and 65535")
    if cfg.library.provider not in PROVIDERS:
        raise ConfigError(f"[library] provider must be one of {', '.join(PROVIDERS)}")
    if cfg.zotero.source not in ZOTERO_SOURCES:
        raise ConfigError(f"[zotero] source must be one of {', '.join(ZOTERO_SOURCES)}")
    if not 1 <= cfg.mendeley.redirect_port <= 65535:
        raise ConfigError("[mendeley] redirect_port must be between 1 and 65535")
    if cfg.calendar.backend not in CALENDAR_BACKENDS:
        raise ConfigError(f"[calendar] backend must be one of {', '.join(CALENDAR_BACKENDS)}")
    if not cfg.calendar.apple_calendar.strip():
        raise ConfigError("[calendar] apple_calendar must not be empty")
    if not 0 <= cfg.calendar.alert_minutes <= 24 * 60:
        raise ConfigError("[calendar] alert_minutes must be between 0 and 1440")
    m = cfg.media
    if m.default_mode not in MEDIA_MODES:
        raise ConfigError(f"[media] default_mode must be one of {', '.join(MEDIA_MODES)}")
    if not 144 <= m.max_height <= 4320:
        raise ConfigError("[media] max_height must be between 144 and 4320")
    if not 1 <= m.max_video_mb <= 100_000 or not 1 <= m.max_audio_mb <= 100_000:
        raise ConfigError("[media] max_video_mb and max_audio_mb must be between 1 and 100000")
    browser = re.split(r"[+:]", m.cookies_from_browser.strip(), maxsplit=1)[0].lower()
    if m.cookies_from_browser.strip() and browser not in COOKIE_BROWSERS:
        raise ConfigError(f"[media] cookies_from_browser must start with one of {', '.join(COOKIE_BROWSERS)}")
    s = cfg.schedule
    s.days = [d.strip().lower()[:3] for d in s.days]
    if not s.days or any(d not in WEEKDAYS for d in s.days):
        raise ConfigError(f"[schedule] days must be a non-empty list of {', '.join(WEEKDAYS)}")
    if not s.times or any(not _TIME_RE.match(t) for t in s.times):
        raise ConfigError('[schedule] times must be a non-empty list of "HH:MM" strings')
    if not 5 <= s.duration_minutes <= 600:
        raise ConfigError("[schedule] duration_minutes must be between 5 and 600")
    if not 1 <= s.horizon_days <= 365:
        raise ConfigError("[schedule] horizon_days must be between 1 and 365")
    if not 5 <= s.max_session_minutes <= 600:
        raise ConfigError("[schedule] max_session_minutes must be between 5 and 600")
    if s.timezone:
        try:
            ZoneInfo(s.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(f"[schedule] unknown timezone {s.timezone!r}") from exc


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def _set_in_section(text: str, section: str, key: str, value: str) -> str:
    """Return `text` with `key = value` (value already TOML-encoded) set inside `[section]`, keeping
    everything else, comments included, as it was."""
    line = f"{key} = {value}"
    header = re.search(rf"(?m)^\[{re.escape(section)}\][ \t]*$", text)
    if header is None:
        return text.rstrip("\n") + f"\n\n[{section}]\n{line}\n"
    end = re.search(r"(?m)^\[", text[header.end() :])
    body_end = header.end() + end.start() if end else len(text)
    body = text[header.end() : body_end]
    body, n = re.subn(rf"(?m)^{re.escape(key)}[ \t]*=.*$", line, body, count=1)
    if not n:
        body = f"\n{line}" + body
    return text[: header.end()] + body + text[body_end:]


def _persist_token(path: Path, text: str, token: str) -> None:
    """Write a generated token into the [server] section of an existing config file."""
    _write_private(path, _set_in_section(text, "server", "token", f'"{token}"'))


def file_library_provider(path: Path) -> str:
    """`[library] provider` as written in the config file, ignoring PAPERQUEUE_LIBRARY."""
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    library = raw.get("library")
    value = library.get("provider", "off") if isinstance(library, dict) else "off"
    return str(value).strip().lower()


def set_library_provider(path: Path, provider: str) -> None:
    """Set `[library] provider` in the config file in place."""
    if provider not in PROVIDERS:
        raise ConfigError(f"[library] provider must be one of {', '.join(PROVIDERS)}")
    text = path.read_text(encoding="utf-8")
    _write_private(path, _set_in_section(text, "library", "provider", f'"{provider}"'))


def load_config(home: Path | None = None) -> Config:
    home = (home or default_home()).expanduser()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = home / "config.toml"
    if not path.exists():
        _write_private(
            path,
            DEFAULT_CONFIG.format(
                token=secrets.token_urlsafe(18),
                backend="apple" if sys.platform == "darwin" else "off",
                pdf_dir=str(ICLOUD_PDF_DIR),
                media_dir=str(ICLOUD_PDF_DIR / "Media"),
            ),
        )
    text = path.read_text(encoding="utf-8")
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    cfg = Config(home=home)
    for name in ("server", "library", "zotero", "mendeley", "calendar", "pdf", "media", "schedule"):
        _apply(name, getattr(cfg, name), raw.get(name, {}))

    if not cfg.server.token:
        cfg.server.token = secrets.token_urlsafe(18)
        _persist_token(path, text, cfg.server.token)

    env = os.environ
    cfg.zotero.user_id = env.get("ZOTERO_USER_ID", cfg.zotero.user_id)
    cfg.zotero.api_key = env.get("ZOTERO_API_KEY", cfg.zotero.api_key)
    cfg.mendeley.client_id = env.get("MENDELEY_CLIENT_ID", cfg.mendeley.client_id)
    cfg.mendeley.client_secret = env.get("MENDELEY_CLIENT_SECRET", cfg.mendeley.client_secret)
    cfg.library.provider = env.get("PAPERQUEUE_LIBRARY", cfg.library.provider)
    cfg.calendar.backend = env.get("PAPERQUEUE_CALENDAR", cfg.calendar.backend)
    cfg.server.token = env.get("PAPERQUEUE_TOKEN") or cfg.server.token
    validate(cfg)
    return cfg
