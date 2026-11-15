"""Local web app: FastAPI JSON API, static frontend, PWA manifest and the bookmarklet confirm page.

Security model (ARCHITECTURE.md v1, decisions 6, 8, 12, 20, 21):

* Host-header allowlist (any IP literal, ``localhost``, ``<hostname>.local``, ``server.extra_hosts``)
  defends against DNS rebinding.
* Non-loopback clients need the token: ``?token=`` (sets an httponly SameSite=Lax cookie, then a 303
  redirect to the same URL without it), the cookie, or ``Authorization: Bearer``.
* Every mutating request needs ``X-PaperQueue: 1``, which forces a CORS preflight and so blocks
  cross-site form posts to localhost.
* Strict CSP, ``Referrer-Policy: no-referrer``, ``nosniff`` and ``X-Frame-Options: DENY`` on every response.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import secrets
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .mediacache import SUFFIXES as MEDIA_TYPES
from .mediacache import fix_for

STATIC_DIR = Path(__file__).parent / "static"
COOKIE_NAME = "pq_token"
COOKIE_MAX_AGE = 400 * 24 * 3600  # browsers cap cookie lifetime at 400 days
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Not secret and needed by iOS/Chrome when installing the app, sometimes without cookies.
PUBLIC_PATHS = frozenset(
    {
        "/static/icon.svg",
        "/static/icon-192.png",
        "/static/icon-512.png",
        "/static/apple-touch-icon.png",
        "/apple-touch-icon.png",
        "/favicon.ico",
    }
)

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data: https://i.ytimg.com; style-src 'self'; script-src 'self'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


# request bodies -----------------------------------------------------------


class AddBody(BaseModel):
    ref: str = ""
    title: str | None = None
    hints: dict[str, Any] | None = None
    priority: int | str = 2
    tags: list[str] = []
    notes: str = ""
    when: str | None = None
    source: str = "web"
    media: str | None = None


class UpdateBody(BaseModel):
    title: str | None = None
    priority: int | str | None = None
    tags: list[str] | None = None
    notes: str | None = None
    url: str | None = None
    pdf_url: str | None = None


class ScheduleBody(BaseModel):
    when: str | None = "next"
    duration_minutes: int | None = None


class MediaBody(BaseModel):
    mode: str


class ReadBody(BaseModel):
    read: bool = True


class AutoScheduleBody(BaseModel):
    include_missed: bool = True
    limit: int | None = None


class CalendarSyncBody(BaseModel):
    force: bool = False


# helpers -------------------------------------------------------------------


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _strip_port(host_header: str) -> str:
    host = host_header.strip().lower()
    if host.startswith("["):  # [::1]:8765
        end = host.find("]")
        return host[1:end] if end != -1 else host
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host.rstrip(".")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _local_hostname() -> str:
    name = socket.gethostname().lower().rstrip(".")
    return name if name.endswith(".local") else f"{name.split('.')[0]}.local"


def _lan_ips() -> list[str]:
    ips: list[str] = []
    try:  # the address used for the default route; no packets are sent
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    return [ip for ip in ips if not ipaddress.ip_address(ip).is_loopback]


def _cfg_get(obj: Any, *path: str, default: Any = None) -> Any:
    for name in path:
        if obj is None:
            return default
        obj = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
    return default if obj is None else obj


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def _default_queue():
    from .config import load_config
    from .service import PaperQueue

    return PaperQueue(load_config())


# app factory -----------------------------------------------------------------


def create_app(
    queue: Any = None,
    *,
    lan: bool = False,
    extra_hosts: Iterable[str] = (),
    trusted_clients: Iterable[str] = (),
) -> FastAPI:
    """Build the web app.

    ``extra_hosts`` adds allowed Host header values and ``trusted_clients`` adds client addresses treated
    like loopback (no token needed). Both exist for tests (Starlette's TestClient sends Host ``testserver``
    from client ``testclient``); production uses the defaults plus ``server.extra_hosts`` from config.
    """
    q = queue if queue is not None else _default_queue()
    cfg = getattr(q, "cfg", None) or getattr(q, "config", None)
    token: str = _cfg_get(cfg, "server", "token", default="") or ""
    port: int = int(_cfg_get(cfg, "server", "port", default=8765))
    hostname = _local_hostname()
    allowed_hosts = {"localhost", hostname}
    allowed_hosts.update(h.lower() for h in _cfg_get(cfg, "server", "extra_hosts", default=[]) or [])
    allowed_hosts.update(h.lower() for h in extra_hosts)
    trusted = set(trusted_clients)

    app = FastAPI(title="paperqueue", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.queue = q
    app.state.lan = lan

    def tz() -> ZoneInfo | timezone:
        zone = getattr(q, "tz", None)
        if zone is not None:
            return zone
        try:
            from .scheduler import tz as sched_tz

            return sched_tz(cfg)
        except Exception:
            pass
        name = _cfg_get(cfg, "schedule", "timezone", default="")
        try:
            return ZoneInfo(name) if name else datetime.now().astimezone().tzinfo  # type: ignore[return-value]
        except Exception:
            return timezone.utc

    def library_link(paper: Any) -> str | None:
        name, key = getattr(paper, "library", None), getattr(paper, "library_key", None)
        if not (name and key):
            return None
        try:
            provider = q.library
            if provider is not None and provider.name == name:
                return provider.item_link(key)
        except Exception:
            pass
        return None

    def pdict(paper: Any) -> dict[str, Any]:
        d = _jsonable(paper.to_dict(tz()))
        if not d.get("library_link"):
            d["library_link"] = library_link(paper)
        if d.get("media_error"):
            d["media_fix"] = fix_for(d["media_error"])
        return d

    def lan_urls() -> list[str]:
        if not lan:
            return []
        hosts = [hostname, *_lan_ips()]
        return [f"http://{h}:{port}/" for h in hosts]

    if lan and hasattr(q, "lan_urls"):
        q.lan_urls = lan_urls()  # reported by PaperQueue.status()

    def with_token(url: str) -> str:
        return f"{url}?{urlencode({'token': token})}" if token else url

    # middleware --------------------------------------------------------------

    def _deny(request: Request, status: int, detail: str) -> Response:
        if request.url.path.startswith("/api/") or request.method != "GET":
            return JSONResponse({"detail": detail}, status_code=status)
        return Response(detail + "\n", status_code=status, media_type="text/plain; charset=utf-8")

    def _has_token(request: Request) -> bool:
        if not token:
            return False
        candidates = [request.cookies.get(COOKIE_NAME, "")]
        auth = request.headers.get("authorization", "")
        if auth[:7].lower() == "bearer ":
            candidates.append(auth[7:].strip())
        return any(c and secrets.compare_digest(c.encode(), token.encode()) for c in candidates)

    @app.middleware("http")
    async def security(request: Request, call_next):
        response = await _guard(request, call_next)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    async def _guard(request: Request, call_next) -> Response:
        host = _strip_port(request.headers.get("host", ""))
        if not host or not (_is_ip(host) or host in allowed_hosts):
            return _deny(request, 400, "Invalid Host header")

        client = request.client.host if request.client else None
        local = _is_loopback(client) or client in trusted

        authed = False
        supplied = request.query_params.get("token")
        if supplied is not None:
            valid = bool(token) and secrets.compare_digest(supplied.encode(), token.encode())
            if valid and request.method in ("GET", "HEAD"):
                rest = [(k, v) for k, v in request.query_params.multi_items() if k != "token"]
                target = request.url.path + (f"?{urlencode(rest)}" if rest else "")
                resp = RedirectResponse(target, status_code=303)
                resp.set_cookie(
                    COOKIE_NAME, token, max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax", path="/"
                )
                resp.headers["Cache-Control"] = "no-store"
                return resp
            if not valid and not local:
                return _deny(request, 401, "Invalid token")
            authed = valid

        if not (local or authed or request.url.path in PUBLIC_PATHS or _has_token(request)):
            return _deny(
                request, 401, "Token required: open the link printed by `paperqueue serve --lan`."
            )

        if request.method in MUTATING and request.headers.get("x-paperqueue") != "1":
            return _deny(request, 403, "Missing X-PaperQueue: 1 header")

        return await call_next(request)

    # error mapping -------------------------------------------------------------

    @app.exception_handler(KeyError)
    async def _not_found(request: Request, exc: KeyError):
        arg = exc.args[0] if exc.args else ""
        return JSONResponse({"detail": f"Not found: {arg}"}, status_code=404)

    @app.exception_handler(ValueError)
    async def _bad_request(request: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc) or "Bad request"}, status_code=400)

    try:
        from .metadata import MetadataError

        @app.exception_handler(MetadataError)
        async def _metadata(request: Request, exc: MetadataError):
            body: dict[str, Any] = {"detail": str(exc) or "Could not resolve metadata"}
            candidates = getattr(exc, "candidates", None)
            if candidates:
                body["candidates"] = _jsonable(candidates)
            return JSONResponse(body, status_code=400)
    except ImportError:  # pragma: no cover
        pass

    # API -----------------------------------------------------------------------

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        st = _jsonable(q.status())
        st["lan"] = {"enabled": lan, "urls": lan_urls()}
        st["server"] = {"port": port}
        return st

    @app.get("/api/papers")
    def list_papers(
        view: str = "queue", q_: str | None = Query(None, alias="q"), tag: str | None = None
    ) -> dict[str, Any]:
        papers = q.list(view=view, query=q_ or None, tag=tag or None)
        return {"papers": [pdict(p) for p in papers]}

    @app.post("/api/papers")
    def add_paper(body: AddBody) -> JSONResponse:
        ref = body.ref.strip()
        hints = body.hints or None
        if not ref and hints:
            ref = str(hints.get("url") or hints.get("doi") or hints.get("title") or "").strip()
        if not ref and not body.title:
            raise ValueError("Paste an arXiv ID, DOI, URL or title")
        source = body.source if body.source in ("web", "manual", "agent") else "web"
        res = q.add(
            ref or (body.title or ""),
            title=body.title or None,
            hints=hints,
            priority=body.priority,
            tags=tuple(t.strip() for t in body.tags if t.strip()),
            notes=body.notes,
            when=body.when or None,
            source=source,
            media=body.media or None,
        )
        return JSONResponse(
            {"paper": pdict(res.paper), "created": res.created, "warnings": list(res.warnings)},
            status_code=201 if res.created else 200,
        )

    @app.get("/api/papers/{paper_id}")
    def get_paper(paper_id: int) -> dict[str, Any]:
        return pdict(q.get(paper_id))

    @app.patch("/api/papers/{paper_id}")
    def update_paper(paper_id: int, body: UpdateBody) -> dict[str, Any]:
        changes = body.model_dump(exclude_unset=True)
        paper = q.update(paper_id, **changes)
        return {"paper": pdict(paper), "warnings": []}

    @app.delete("/api/papers/{paper_id}")
    def remove_paper(paper_id: int) -> dict[str, Any]:
        res = q.remove(paper_id)
        return {"paper": pdict(res.paper), "warnings": list(res.warnings)}

    @app.post("/api/papers/{paper_id}/schedule")
    def schedule_paper(paper_id: int, body: ScheduleBody = Body(default_factory=ScheduleBody)):
        res = q.schedule(paper_id, body.when, duration_minutes=body.duration_minutes)
        return {"paper": pdict(res.paper), "warnings": list(res.warnings)}

    @app.delete("/api/papers/{paper_id}/schedule")
    def unschedule_paper(paper_id: int) -> dict[str, Any]:
        res = q.unschedule(paper_id)
        return {"paper": pdict(res.paper), "warnings": list(res.warnings)}

    @app.post("/api/papers/{paper_id}/read")
    def mark_read(paper_id: int, body: ReadBody = Body(default_factory=ReadBody)):
        res = q.mark_read(paper_id, read=body.read)
        return {"paper": pdict(res.paper), "warnings": list(res.warnings)}

    @app.get("/api/papers/{paper_id}/pdf")
    def paper_pdf(paper_id: int) -> Response:
        paper = q.get(paper_id)
        raw = getattr(paper, "pdf_path", None)
        pdf_dir = _cfg_get(cfg, "pdf", "dir")
        if not raw or not pdf_dir:
            raise KeyError(f"no cached PDF for paper {paper_id}")
        try:
            root = Path(pdf_dir).expanduser().resolve()
            path = Path(raw).expanduser().resolve()
        except (OSError, RuntimeError):
            raise KeyError(f"no cached PDF for paper {paper_id}") from None
        if not path.is_relative_to(root) or path.suffix.lower() != ".pdf" or not path.is_file():
            raise KeyError(f"no cached PDF for paper {paper_id}")
        return FileResponse(
            path, media_type="application/pdf", filename=path.name, content_disposition_type="inline"
        )

    @app.post("/api/papers/{paper_id}/media")
    def set_media(paper_id: int, body: MediaBody) -> dict[str, Any]:
        res = q.set_media(paper_id, body.mode)
        return {"paper": pdict(res.paper), "warnings": list(res.warnings)}

    @app.get("/api/papers/{paper_id}/media")
    def paper_media(paper_id: int) -> Response:
        # Same confinement as the PDF route, plus a suffix allowlist with a fixed media type each.
        # FileResponse answers Range requests (206), which audio/video seeking needs.
        paper = q.get(paper_id)
        raw = getattr(paper, "media_path", None)
        media_dir = _cfg_get(cfg, "media", "dir")
        missing = f"no downloaded media for item {paper_id}"
        if not raw or not media_dir:
            raise KeyError(missing)
        try:
            root = Path(media_dir).expanduser().resolve()
            path = Path(raw).expanduser().resolve()
        except (OSError, RuntimeError):
            raise KeyError(missing) from None
        media_type = MEDIA_TYPES.get(path.suffix.lower())
        if not path.is_relative_to(root) or media_type is None or not path.is_file():
            raise KeyError(missing)
        return FileResponse(path, media_type=media_type, filename=path.name, content_disposition_type="inline")

    @app.post("/api/auto-schedule")
    def auto_schedule(body: AutoScheduleBody = Body(default_factory=AutoScheduleBody)):
        res = q.auto_schedule(include_missed=body.include_missed, limit=body.limit)
        return {
            "papers": [pdict(p) for p in res.papers],
            "unplaced": [pdict(p) for p in res.unplaced],
            "warnings": list(res.warnings),
        }

    @app.get("/api/slots")
    def slots(n: int = Query(5, ge=1, le=50), paper_id: int | None = None) -> dict[str, Any]:
        zone = tz()
        out = []
        for dt in q.free_slots(n=n, paper_id=paper_id):
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            out.append({"utc": dt.astimezone(timezone.utc).isoformat(), "local": dt.astimezone(zone).isoformat()})
        return {"slots": out}

    @app.post("/api/library/sync")
    def library_sync() -> dict[str, Any]:
        return _jsonable(q.sync_library())

    @app.post("/api/calendar/sync")
    def calendar_sync(body: CalendarSyncBody = Body(default_factory=CalendarSyncBody)):
        return {"warnings": list(q.sync_calendar(force=body.force) or [])}

    @app.get("/api/qr.svg")
    def qr_svg() -> Response:
        urls = lan_urls()
        if not lan or not urls:
            return JSONResponse({"detail": "LAN mode is off; run `paperqueue serve --lan`"}, status_code=404)
        import io

        import segno

        buf = io.BytesIO()
        segno.make(with_token(urls[0]), error="m").save(
            buf, kind="svg", scale=6, border=2, dark="#1c1c1e", light="#ffffff", xmldecl=False
        )
        return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})

    # pages -----------------------------------------------------------------------

    def _page(name: str) -> FileResponse:
        return FileResponse(STATIC_DIR / name, media_type="text/html", headers={"Cache-Control": "no-cache"})

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return _page("index.html")

    @app.get("/add", include_in_schema=False)
    def add_page() -> FileResponse:
        # Never mutates: add.js shows the hints and POSTs only when the user presses Save.
        return _page("add.html")

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> Response:
        start_url = with_token("/") if lan else "/"
        data = {
            "name": "paperqueue",
            "short_name": "Papers",
            "description": "Your reading queue for research papers",
            "start_url": start_url,
            "scope": "/",
            "display": "standalone",
            "background_color": "#f7f6f3",
            "theme_color": "#f7f6f3",
            "icons": [
                {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
                {"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
            ],
        }
        return Response(
            json.dumps(data),
            media_type="application/manifest+json",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/apple-touch-icon.png", include_in_schema=False)
    def apple_touch_icon() -> FileResponse:
        return FileResponse(STATIC_DIR / "apple-touch-icon.png", media_type="image/png")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(STATIC_DIR / "icon.svg", media_type="image/svg+xml")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
