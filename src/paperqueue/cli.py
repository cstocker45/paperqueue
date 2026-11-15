"""Command-line interface: `paperqueue <command>`."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import socket
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import __version__

SERVICE_LABEL = "io.github.paperqueue"
MCP_ADD_HINT = "claude mcp add -s user paperqueue -- paperqueue mcp"
SECRET_KEYS = ("api_key", "client_secret", "token", "access_token", "refresh_token")
MAINTENANCE_INTERVAL = 300  # seconds
YTDLP_MAX_AGE_DAYS = 60


# --- helpers -----------------------------------------------------------------


def load_cfg():
    from .config import load_config

    return load_config()


def make_queue(cfg=None):
    """Build the service. Tests monkeypatch this to inject fakes."""
    from .service import PaperQueue

    return PaperQueue(cfg or load_cfg())


def _fmt():
    from .mcp_server import _queue_tz, human_time, paper_summary

    return _queue_tz, human_time, paper_summary


def _print_warnings(warnings) -> None:
    for w in warnings or []:
        print(f"  warning: {w}")


def _paper_line(s: dict) -> str:
    parts = [f"#{s['id']:<4}", s["title"]]
    if s.get("kind") == "video":
        meta = " · ".join(x for x in ("YouTube", s.get("length"), s.get("channel")) if x)
    else:
        meta = " ".join(x for x in (s.get("authors"), f"({s['venue_year']})" if s.get("venue_year") else "") if x)
    line = "  ".join(parts)
    if meta:
        line += f"\n       {meta}"
    extra = []
    if s.get("priority") and s["priority"] != "normal":
        extra.append(f"priority {s['priority']}")
    if s.get("tags"):
        extra.append(" ".join(f"#{t}" for t in s["tags"]))
    media = s.get("media") or {}
    if media.get("mode", "link") != "link":
        extra.append(f"{media['mode']} {media.get('status') or ''}".strip())
    if s.get("status") == "read":
        extra.append("watched" if s.get("kind") == "video" else "read")
    elif s.get("scheduled"):
        extra.append(("MISSED " if s.get("missed") else "") + s["scheduled"])
    elif s.get("status") == "to_read":
        extra.append("unscheduled")
    if extra:
        line += "\n       " + "  ·  ".join(extra)
    return line


def _print_paper_result(verb: str, paper, warnings, queue) -> None:
    _queue_tz, _, paper_summary = _fmt()
    s = paper_summary(paper, _queue_tz(queue))
    when = f" for {s['scheduled']}" if s.get("scheduled") else ""
    print(f"{verb} #{s['id']} {s['title']}{when}")
    _print_warnings(warnings)


def mask_secrets(text: str) -> str:
    """Mask the values of secret keys in TOML text."""
    pattern = re.compile(
        r'^(\s*(?:%s)\s*=\s*)(["\'])(.*?)\2' % "|".join(SECRET_KEYS), re.MULTILINE
    )

    def repl(m: re.Match) -> str:
        value = m.group(3)
        masked = "********" if value else ""
        return f"{m.group(1)}{m.group(2)}{masked}{m.group(2)}"

    return pattern.sub(repl, text)


def lan_hostname() -> str:
    name = socket.gethostname().lower().rstrip(".")
    return name if name.endswith(".local") else f"{name.split('.')[0]}.local"


def lan_ips() -> list[str]:
    ips: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET; no packets are sent for UDP connect
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith("127.")]


# --- commands ----------------------------------------------------------------


def cmd_serve(args) -> int:
    import uvicorn

    from .server import create_app

    cfg = load_cfg()
    queue = make_queue(cfg)
    port = args.port or cfg.server.port
    host = "0.0.0.0" if args.lan else (args.host or cfg.server.host)
    app = create_app(queue, lan=args.lan)

    print(f"paperqueue {__version__}")
    print(f"  Local:   http://localhost:{port}/")
    if args.lan:
        token = cfg.server.token
        url = f"http://{lan_hostname()}:{port}/?token={token}"
        print(f"  LAN:     {url}")
        for ip in lan_ips():
            print(f"           http://{ip}:{port}/?token={token}")
        print("\nScan with your iPhone camera (same Wi-Fi), then Share > Add to Home Screen:\n")
        try:
            import segno

            segno.make(url, error="m").terminal(compact=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  (could not draw QR code: {exc})")
        print("\nPlain HTTP on your home network only. Keep the token private.")
    print("Press Ctrl+C to stop.\n", flush=True)

    stop = threading.Event()
    if not args.no_maintenance:
        threading.Thread(target=_maintenance_loop, args=(queue, stop), name="paperqueue-maintenance", daemon=True).start()
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        stop.set()
    return 0


def _maintenance_loop(queue, stop: threading.Event, interval: float = MAINTENANCE_INTERVAL) -> None:
    import logging

    log = logging.getLogger("paperqueue.maintenance")
    while not stop.is_set():
        try:
            queue.maintain()
        except Exception:  # noqa: BLE001
            log.exception("maintenance failed")
        stop.wait(interval)


def cmd_add(args) -> int:
    queue = make_queue()
    res = queue.add(
        args.ref,
        title=args.title,
        priority=args.priority,
        tags=tuple(args.tag or ()),
        notes=args.notes or "",
        when=args.when,
        source="manual",
        media=args.media,
    )
    _print_paper_result("Added" if res.created else "Already queued:", res.paper, res.warnings, queue)
    return 0


def cmd_media(args) -> int:
    queue = make_queue()
    res = queue.set_media(args.id, args.mode)
    if args.mode == "link":
        verb = "Link only:"
    elif res.paper.media_status == "failed":
        verb = f"Could not download {args.mode}:"
    else:
        verb = f"Downloading {args.mode} in the background:"
    _print_paper_result(verb, res.paper, res.warnings, queue)
    return 0


def cmd_media_worker() -> int:
    """Hidden: the detached downloader started by `mediacache.kick`."""
    import logging

    from . import mediacache

    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    mediacache.worker(make_queue())
    return 0


def cmd_list(args) -> int:
    queue = make_queue()
    _queue_tz, _, paper_summary = _fmt()
    zone = _queue_tz(queue)
    papers = queue.list(view=args.view, query=args.query, tag=args.tag)
    summaries = [paper_summary(p, zone, full=args.json) for p in papers]
    if args.json:
        print(json.dumps(summaries, indent=2, ensure_ascii=False))
        return 0
    if not summaries:
        print(f"No papers in view '{args.view}'.")
        return 0
    for s in summaries:
        print(_paper_line(s))
    return 0


def cmd_upcoming(args) -> int:
    queue = make_queue()
    _queue_tz, _, paper_summary = _fmt()
    zone = _queue_tz(queue)
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=args.days)
    sessions = [
        p for p in queue.list(view="upcoming")
        if p.scheduled_start and datetime.fromisoformat(p.scheduled_start) <= horizon
    ]
    missed = queue.list(view="missed")
    if args.json:
        print(json.dumps(
            {
                "sessions": [paper_summary(p, zone, full=True) for p in sessions],
                "missed": [paper_summary(p, zone, full=True) for p in missed],
            },
            indent=2,
            ensure_ascii=False,
        ))
        return 0
    if missed:
        print("Missed:")
        for p in missed:
            print(_paper_line(paper_summary(p, zone)))
        print()
    if not sessions:
        print(f"Nothing scheduled in the next {args.days} days.")
        return 0
    day = None
    for p in sessions:
        local = datetime.fromisoformat(p.scheduled_start).astimezone(zone)
        label = f"{local:%A} {local.day} {local:%B}"
        if label != day:
            print(("\n" if day else "") + label)
            day = label
        s = paper_summary(p, zone)
        print(f"  {local:%H:%M}  #{s['id']} {s['title']}" + (f"  ({s['authors']})" if s.get("authors") else ""))
    return 0


def cmd_schedule(args) -> int:
    queue = make_queue()
    if args.when.strip().lower() in ("none", "unschedule", "off"):
        res = queue.unschedule(args.id)
        _print_paper_result("Unscheduled", res.paper, res.warnings, queue)
    else:
        res = queue.schedule(args.id, args.when, args.duration)
        _print_paper_result("Scheduled", res.paper, res.warnings, queue)
    return 0


def cmd_auto_schedule(args) -> int:
    queue = make_queue()
    _queue_tz, _, paper_summary = _fmt()
    zone = _queue_tz(queue)
    res = queue.auto_schedule(include_missed=not args.no_missed, limit=args.limit)
    if not res.papers:
        print("Nothing to schedule.")
    for p in res.papers:
        s = paper_summary(p, zone)
        print(f"Scheduled #{s['id']} {s['title']} for {s.get('scheduled')}")
    for p in res.unplaced:
        print(f"No free slot for #{p.id} {p.title}")
    _print_warnings(res.warnings)
    return 0


def cmd_read(args) -> int:
    queue = make_queue()
    res = queue.mark_read(args.id, not args.unread)
    _print_paper_result("Marked unread:" if args.unread else "Marked read:", res.paper, res.warnings, queue)
    return 0


def cmd_remove(args) -> int:
    queue = make_queue()
    res = queue.remove(args.id)
    print(f"Removed #{args.id} {res.paper.title}")
    _print_warnings(res.warnings)
    return 0


def cmd_sync(args) -> int:
    queue = make_queue()
    if not args.calendar_only:
        res = queue.sync_library()
        print(f"Library: pulled {res.pulled}, new {res.created}, pushed {res.pushed}")
        _print_warnings(res.warnings)
    if not args.library_only:
        warnings = queue.sync_calendar(force=args.force)
        print("Calendar: synced")
        _print_warnings(warnings)
    return 0


def _check(ok: bool, label: str, detail: str = "", fix: str = "", *, level: str = "FAIL") -> bool:
    """Print one doctor line. `level="warn"` reports a problem that does not fail the run."""
    print(f"[{'ok' if ok else level}] {label}" + (f": {detail}" if detail else ""))
    if not ok and fix:
        for line in fix.splitlines():
            print(f"       fix: {line}")
    return ok or level == "warn"


def _youtube_checks(cfg) -> list[bool]:
    """yt-dlp freshness, Deno and ffmpeg, and failed downloads. Missing tools only fail the
    run when the configured default media mode needs them."""
    from .db import Store
    from .mediacache import fix_for
    from .metadata import find_tool

    results: list[bool] = []
    try:
        from yt_dlp.version import __version__ as ytdlp_version  # type: ignore[import-untyped]

        year, month, day = (int(x) for x in ytdlp_version.split(".")[:3])
        released = datetime(year, month, day)
        age = (datetime.now() - released).days
        results.append(_check(
            age <= YTDLP_MAX_AGE_DAYS, "yt-dlp", f"{ytdlp_version} ({age} days old)",
            "YouTube changes often and old versions break: uv tool upgrade paperqueue", level="warn",
        ))
    except (ImportError, ValueError, TypeError) as exc:
        results.append(_check(False, "yt-dlp", f"not usable: {exc}",
                              "reinstall: uv tool install --reinstall paperqueue"))
    mode = cfg.media.default_mode
    for tool, needed, purpose in (
        ("deno", mode != "link", "needed for YouTube audio/video downloads"),
        ("ffmpeg", mode == "video", "needed for video downloads; audio works without it"),
    ):
        path = find_tool(tool)
        results.append(_check(path is not None, tool, path or purpose, f"brew install {tool}",
                              level="FAIL" if needed else "warn"))
    for paper in Store(cfg.db_path).list_media_pending():
        if paper.media_status == "failed":
            results.append(_check(False, f"Download #{paper.id}", f"{paper.title} ({paper.media_error})",
                                  fix_for(paper.media_error) or "", level="warn"))
    return results


def cmd_doctor(args) -> int:
    results: list[bool] = []
    v = sys.version_info
    results.append(_check(v >= (3, 11), "Python", f"{v.major}.{v.minor}.{v.micro} ({sys.executable})",
                          "install Python 3.11+ (e.g. `uv tool install --python 3.12 ...`)"))
    try:
        cfg = load_cfg()
    except Exception as exc:  # noqa: BLE001
        _check(False, "Config", str(exc), "fix or delete ~/.paperqueue/config.toml (a default is recreated)")
        return 1
    results.append(_check(True, "Config", str(cfg.config_path)))

    try:
        from .db import Store

        Store(cfg.db_path)
        results.append(_check(True, "Database", str(cfg.db_path)))
    except Exception as exc:  # noqa: BLE001
        results.append(_check(False, "Database", f"{cfg.db_path}: {exc}",
                              "check permissions of the paperqueue home directory"))

    provider_name = getattr(cfg.library, "provider", "off")
    try:
        from .libraries import get_provider

        provider = get_provider(cfg, cfg.home)
        if provider is None:
            results.append(_check(True, "Library", f"provider '{provider_name}' (no reference manager)"))
        else:
            ok, msg = provider.check()
            fix = {
                "zotero": "start Zotero 7 and enable Settings > Advanced > 'Allow other applications on this "
                          "computer to communicate with Zotero',\nand create a collection named "
                          f"'{getattr(getattr(cfg, 'zotero', None), 'collection', 'To read')}'",
                "mendeley": "set mendeley.client_id/client_secret in the config, then run `paperqueue mendeley login`",
            }.get(provider.name, "")
            results.append(_check(ok, f"Library ({provider.name})", msg, fix))
    except Exception as exc:  # noqa: BLE001
        results.append(_check(False, f"Library ({provider_name})", str(exc),
                              "check the [library] section of the config"))

    try:
        from .calendar import get_backend

        if getattr(cfg.calendar, "backend", "apple") == "off":
            backend = None
        else:
            print("Checking Apple Calendar (macOS may ask to allow Automation access to Calendar; click OK)...",
                  flush=True)
            backend = get_backend(cfg)
        if backend is None:
            results.append(_check(True, "Calendar", "disabled (set [calendar] backend = \"apple\" to enable)"))
        else:
            ok, msg = backend.check()
            name = getattr(cfg.calendar, "apple_calendar", "Reading List")
            results.append(_check(
                ok, f"Calendar ({backend.name})", msg,
                f"create an iCloud calendar named '{name}' in Calendar.app (File > New Calendar > iCloud),\n"
                "and allow access in System Settings > Privacy & Security > Automation > Calendar",
            ))
    except Exception as exc:  # noqa: BLE001
        results.append(_check(False, "Calendar", str(exc), "check the [calendar] section of the config"))

    pdf = getattr(cfg, "pdf", None)
    if pdf is not None and getattr(pdf, "enabled", False):
        d = Path(pdf.dir).expanduser()
        if not d.is_dir() and d.parent.is_dir():
            results.append(_check(True, "PDF folder", f"{d} (created on first download)"))
        else:
            results.append(_check(d.is_dir(), "PDF folder", str(d),
                                  f"turn on iCloud Drive, then: mkdir -p \"{d}\"\n"
                                  "(or set [pdf] dir / enabled = false in the config)"))
    else:
        _check(True, "PDF folder", "disabled")

    results += _youtube_checks(cfg)

    registered = _mcp_registered()
    if registered:
        _check(True, "MCP", "registered with Claude Code")
    else:
        print(f"[info] MCP: to let Claude Code add papers, run:\n         {MCP_ADD_HINT}\n"
              "         paperqueue install-skill")

    failed = results.count(False)
    print("\nAll checks passed." if not failed else f"\n{failed} check(s) failed.")
    return 1 if failed else 0


def _mcp_registered() -> bool:
    path = Path.home() / ".claude.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    if "paperqueue" in (data.get("mcpServers") or {}):
        return True
    return any("paperqueue" in (p.get("mcpServers") or {}) for p in (data.get("projects") or {}).values()
               if isinstance(p, dict))


def cmd_mendeley(args) -> int:
    import webbrowser

    from .libraries import mendeley

    cfg = load_cfg()
    path = mendeley.login(cfg, webbrowser.open)
    print(f"Logged in to Mendeley. Token saved to {path}")
    return 0


def cmd_mcp(args) -> int:
    from .mcp_server import run

    run()
    return 0


def cmd_install_skill(args) -> int:
    from .skill import install_skill

    path = install_skill(args.dest)
    print(f"Installed Claude Code skill: {path}")
    return 0


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"


def build_plist(home: Path) -> dict[str, Any]:
    logs = home / "logs"
    env = {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"}
    if os.environ.get("PAPERQUEUE_HOME"):
        env["PAPERQUEUE_HOME"] = str(home)
    return {
        "Label": SERVICE_LABEL,
        "ProgramArguments": [sys.executable, "-m", "paperqueue", "serve"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "WorkingDirectory": str(home),
        "StandardOutPath": str(logs / "serve.log"),
        "StandardErrorPath": str(logs / "serve.err.log"),
        "EnvironmentVariables": env,
        "ProcessType": "Background",
    }


def cmd_install_service(args) -> int:
    cfg = load_cfg()
    path = plist_path()
    domain = f"gui/{os.getuid()}"
    if args.uninstall:
        if args.dry_run:
            print(f"Would run: launchctl bootout {domain}/{SERVICE_LABEL}\nWould remove: {path}")
            return 0
        subprocess.run(["launchctl", "bootout", f"{domain}/{SERVICE_LABEL}"], check=False)
        path.unlink(missing_ok=True)
        print(f"Removed {path}")
        return 0
    data = plistlib.dumps(build_plist(cfg.home)).decode()
    if args.dry_run:
        print(f"# {path}")
        print(data)
        print(f"# then: launchctl bootstrap {domain} {path}")
        return 0
    if sys.platform != "darwin":
        print("install-service needs macOS (launchd).", file=sys.stderr)
        return 1
    (cfg.home / "logs").mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "bootout", f"{domain}/{SERVICE_LABEL}"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    path.write_text(data)
    proc = subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=False)
    if proc.returncode != 0:
        print(f"launchctl bootstrap failed ({proc.returncode}); the plist is at {path}", file=sys.stderr)
        return 1
    print(f"Installed {path}\npaperqueue now runs at login: http://localhost:{cfg.server.port}/")
    print(f"Logs: {cfg.home / 'logs'}")
    return 0


def cmd_config(args) -> int:
    cfg = load_cfg()
    print(f"# {cfg.config_path}\n")
    print(mask_secrets(cfg.config_path.read_text()))
    return 0


# --- parser ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="paperqueue", description="A local reading queue for research papers."
    )
    p.add_argument("--version", action="version", version=f"paperqueue {__version__}")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    s = sub.add_parser("serve", help="run the web app (http://localhost:8765)")
    s.add_argument("--lan", action="store_true", help="listen on the home network and print a QR code for your phone")
    s.add_argument("--port", type=int, help="port (default from config, 8765)")
    s.add_argument("--host", help="bind address (default from config)")
    s.add_argument("--no-maintenance", action="store_true", help="don't run background calendar/library sync")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("add", help="add a paper (arXiv ID, DOI, URL or title) or a YouTube link")
    s.add_argument("ref")
    s.add_argument("--title", help="title to use if metadata can't be fetched")
    s.add_argument("-p", "--priority", default="normal", choices=["high", "normal", "low"])
    s.add_argument("-t", "--tag", action="append", help="tag (repeatable)")
    s.add_argument("-n", "--notes", default="")
    s.add_argument("-w", "--when", default="next",
                   help="next (default), today, tomorrow, a weekday, YYYY-MM-DD, ISO datetime, or none")
    s.add_argument("-m", "--media", choices=["link", "audio", "video"],
                   help="YouTube only: keep a link (default) or download audio/video for offline use")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("media", help="YouTube: keep as link, or download audio/video")
    s.add_argument("id", type=int)
    s.add_argument("mode", choices=["link", "audio", "video"])
    s.set_defaults(func=cmd_media)

    s = sub.add_parser("list", help="list papers")
    s.add_argument("view", nargs="?", default="queue", choices=["queue", "upcoming", "missed", "read", "all"])
    s.add_argument("-q", "--query", help="search text")
    s.add_argument("-t", "--tag")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("upcoming", help="scheduled reading sessions")
    s.add_argument("-d", "--days", type=int, default=7)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_upcoming)

    s = sub.add_parser("schedule", help="schedule a paper (WHEN=none to unschedule)")
    s.add_argument("id", type=int)
    s.add_argument("when", nargs="?", default="next")
    s.add_argument("--duration", type=int, help="minutes")
    s.set_defaults(func=cmd_schedule)

    s = sub.add_parser("auto-schedule", help="put queued and missed papers into free slots")
    s.add_argument("--no-missed", action="store_true", help="leave missed sessions alone")
    s.add_argument("--limit", type=int)
    s.set_defaults(func=cmd_auto_schedule)

    s = sub.add_parser("read", help="mark a paper read (or a video watched)")
    s.add_argument("id", type=int)
    s.add_argument("--unread", action="store_true", help="mark as unread again")
    s.set_defaults(func=cmd_read)

    s = sub.add_parser("remove", help="remove a paper")
    s.add_argument("id", type=int)
    s.set_defaults(func=cmd_remove)

    s = sub.add_parser("sync", help="sync the reference manager and the calendar")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--library", dest="library_only", action="store_true", help="library only")
    g.add_argument("--calendar", dest="calendar_only", action="store_true", help="calendar only")
    s.add_argument("--force", action="store_true", help="rewrite all calendar events")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("doctor", help="check the setup and print fixes")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("mendeley", help="Mendeley account")
    msub = s.add_subparsers(dest="mendeley_command", metavar="<command>", required=True)
    m = msub.add_parser("login", help="authorise paperqueue with your Mendeley account")
    m.set_defaults(func=cmd_mendeley)

    s = sub.add_parser("mcp", help="run the MCP server on stdio (for agents)")
    s.set_defaults(func=cmd_mcp)

    s = sub.add_parser("install-skill", help="install the Claude Code skill")
    s.add_argument("--dest", help="path (default ~/.claude/skills/paper-queue/SKILL.md)")
    s.set_defaults(func=cmd_install_skill)

    s = sub.add_parser("install-service", help="run paperqueue at login (macOS launchd)")
    s.add_argument("--uninstall", action="store_true")
    s.add_argument("--dry-run", action="store_true", help="print the plist; change nothing")
    s.set_defaults(func=cmd_install_service)

    s = sub.add_parser("config", help="show the config file (secrets masked)")
    s.set_defaults(func=cmd_config)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["media-worker"]:  # hidden; kept out of argparse so it is not listed in --help
        return cmd_media_worker()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except KeyError as exc:
        arg = exc.args[0] if exc.args else ""
        print(f"error: no paper with id {arg}" if isinstance(arg, int) else f"error: not found: {arg}",
              file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
