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
# Optional, for writing papers you add here (or via an agent) back into Zotero,
# so they sync to Zotero on your iPhone. Create a key at https://www.zotero.org/settings/keys
# You can also set ZOTERO_USER_ID / ZOTERO_API_KEY in the environment instead.
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

[schedule]
# Reading slots used by "next free slot" and auto-scheduling.
days = ["mon", "tue", "wed", "thu", "fri"]
times = ["08:30"]
duration_minutes = 45
horizon_days = 14
# IANA timezone, e.g. "Europe/Berlin". Empty = system timezone.
timezone = ""
"""

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
PROVIDERS = ("off", "zotero", "mendeley")
ZOTERO_SOURCES = ("local", "web")
CALENDAR_BACKENDS = ("off", "apple")
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
class ScheduleConfig:
    days: list[str] = field(default_factory=lambda: ["mon", "tue", "wed", "thu", "fri"])
    times: list[str] = field(default_factory=lambda: ["08:30"])
    duration_minutes: int = 45
    horizon_days: int = 14
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
    if s.timezone:
        try:
            ZoneInfo(s.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(f"[schedule] unknown timezone {s.timezone!r}") from exc


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def _persist_token(path: Path, text: str, token: str) -> None:
    """Write a generated token into the [server] section of an existing config file."""
    line = f'token = "{token}"'
    section = re.search(r"(?m)^\[server\][ \t]*$", text)
    if section is None:
        text = text.rstrip("\n") + f"\n\n[server]\n{line}\n"
    else:
        end = re.search(r"(?m)^\[", text[section.end() :])
        body_end = section.end() + end.start() if end else len(text)
        body = text[section.end() : body_end]
        body, n = re.subn(r"(?m)^token[ \t]*=.*$", line, body, count=1)
        if not n:
            body = f"\n{line}" + body
        text = text[: section.end()] + body + text[body_end:]
    _write_private(path, text)


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
            ),
        )
    text = path.read_text(encoding="utf-8")
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    cfg = Config(home=home)
    for name in ("server", "library", "zotero", "mendeley", "calendar", "pdf", "schedule"):
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
