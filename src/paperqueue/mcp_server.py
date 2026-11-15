"""MCP server (stdio): lets agents add, schedule and query papers in the reading queue.

Calls the service layer directly (no HTTP server needed). Nothing is ever written to
stdout except the MCP protocol; logs go to stderr.
"""

from __future__ import annotations

import logging
import threading
import sys
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as FastMCP
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # pragma: no cover - mcp 1.x
    from mcp.server.fastmcp import FastMCP  # type: ignore[no-redef]
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[no-redef]

from .mediacache import fix_for, format_length

log = logging.getLogger("paperqueue.mcp")

INSTRUCTIONS = """\
paperqueue is the user's personal reading queue for research papers and YouTube videos.
Items added here are scheduled into sessions on their Apple Calendar (with an alert that
reaches their Mac, iPhone and Watch), papers are cached as PDFs for offline reading where
possible and mirrored to their reference manager (Zotero or Mendeley) when one is configured.

When to use it
- The user expresses intent to read a paper ("add this", "save this for later", "I want to read X",
  "queue it", "remind me to read ..."): call add_paper straight away. Do not ask for confirmation.
- Pass the most precise identifier you have as `ref`: an arXiv ID, a DOI, or a URL (arXiv, bioRxiv,
  PubMed, publisher page). Use a bare title only as a last resort; if you know the title and a URL,
  pass the URL as `ref` and the title as `title`.
- Scheduling: leave `when="next"` (the next free reading slot) unless the user names a time.
  Accepted values: "next", "today", "tomorrow", a weekday ("friday"), "YYYY-MM-DD",
  an ISO datetime ("2026-03-02T09:00", local time), or "none" to queue without scheduling.
- Tags: add one to three short lowercase tags from the topic of the conversation
  (e.g. ["protein-design", "diffusion"]). Set priority "high" only if the user signals urgency.
- When you recommend papers yourself (e.g. in a literature answer), offer once, in a single line,
  to add them to the reading queue. Do not repeat the offer, and do not add them unasked.

YouTube videos
- YouTube links (youtube.com/watch, youtu.be, /shorts/, /live/, /embed/) go in the same queue:
  "watch this later", "add this talk" -> add_paper(ref=<link>). The session lasts as long as the
  video (at most 90 minutes) and the calendar event reads "Watch: ...". Playlists are refused;
  add the videos one at a time.
- Keep the default (a link only). Pass media="audio" or media="video" only when the user asks
  for offline use, audio or a download ("download it", "for the plane", "as a podcast").
  Downloading is the user's choice and responsibility under YouTube's Terms of Service.
- To change it later, call set_media(paper_id, "link" | "audio" | "video"); "link" deletes the
  local file. Downloads run in the background: report media.status ("pending", "downloading",
  "done", "failed"), and for "failed" pass on media.fix (a one-line fix) to the user.

Reporting
- After adding or scheduling, tell the user what was queued and when, using the `scheduled`
  field (already in the user's local time), e.g. "Queued 'Attention Is All You Need' for
  Tue 18 Nov, 08:30." Mention any warnings briefly (e.g. calendar not reachable).
- add_paper deduplicates. If it returns status "already_queued", say it is already in the queue
  (and when it is scheduled); do not try to add it again.

Rules
- Never mark papers read or videos watched (mark_read), remove them (remove_paper) or change
  their media mode (set_media) unless the user asks.
- Never reschedule existing sessions unless asked; auto_schedule is for explicit requests like
  "schedule my queue" or "move my missed papers".
- For "what should I read today/this week?", call `upcoming` (it also lists missed sessions).
- Use list_reading_list to search the queue ("do I have the AlphaFold paper queued?"), and
  free_slots before proposing specific times.
"""

_PRIORITIES = {1: "high", 2: "normal", 3: "low"}


# --- formatting --------------------------------------------------------------


def _queue_tz(queue: Any) -> tzinfo:
    cfg = getattr(queue, "cfg", None) or getattr(queue, "config", None)
    if cfg is not None:
        try:
            from .scheduler import tz

            return tz(cfg)
        except Exception:  # noqa: BLE001 - fall back to the system zone
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def human_time(dt: datetime, tz: tzinfo) -> str:
    """'Tue 18 Nov 2025, 08:30' in the given zone."""
    local = dt.astimezone(tz)
    return f"{local:%a} {local.day} {local:%b %Y, %H:%M}"


def short_authors(authors: list[str] | None, n: int = 3) -> str:
    authors = list(authors or [])
    if not authors:
        return ""
    if len(authors) <= n:
        return ", ".join(authors)
    return f"{', '.join(authors[:n])} et al."


def _prio_name(p: Any) -> str:
    return _PRIORITIES.get(p, str(p)) if isinstance(p, int) else str(p)


def paper_summary(paper: Any, tz: tzinfo, now: datetime | None = None, *, full: bool = False) -> dict[str, Any]:
    """Compact, JSON-friendly view of a Paper."""
    now = now or datetime.now(timezone.utc)
    start = _parse_dt(getattr(paper, "scheduled_start", None))
    duration = getattr(paper, "duration_minutes", None)
    status = getattr(paper, "status", "to_read")
    video = getattr(paper, "kind", "paper") == "video"
    d: dict[str, Any] = {"id": paper.id, "title": paper.title}
    if video:
        d["kind"] = "video"
        d["channel"] = ", ".join(getattr(paper, "authors", []) or [])
        seconds = getattr(paper, "duration_seconds", None)
        if seconds:
            d["length"] = format_length(seconds)
        media_status = getattr(paper, "media_status", None)
        media: dict[str, Any] = {"mode": getattr(paper, "media_mode", "link")}
        if media_status:
            media["status"] = media_status
        if error := getattr(paper, "media_error", None):
            media |= {"error": error, "fix": fix_for(error)}
        if media_status == "done" and getattr(paper, "media_path", None):
            media["file"] = str(paper.media_path)
        d["media"] = media
    else:
        d["authors"] = short_authors(getattr(paper, "authors", []))
    venue, year = getattr(paper, "venue", None), getattr(paper, "year", None)
    vy = " ".join(str(x) for x in (venue, year) if x)
    if vy:
        d["venue_year"] = vy
    d["status"] = status
    d["priority"] = _prio_name(getattr(paper, "priority", 2))
    if getattr(paper, "tags", None):
        d["tags"] = list(paper.tags)
    if start is not None:
        local = start.astimezone(tz)
        d["scheduled"] = human_time(start, tz)
        d["scheduled_local"] = local.isoformat()
        if duration:
            d["duration_minutes"] = duration
        end = start + timedelta(minutes=duration or 0)
        if status == "to_read" and end < now:
            d["missed"] = True
    elif status == "to_read":
        d["scheduled"] = None
    links = {}
    for key, attr in (("url", "url"), ("pdf", "pdf_url")):
        if getattr(paper, attr, None):
            links[key] = getattr(paper, attr)
    if getattr(paper, "doi", None):
        links["doi"] = f"https://doi.org/{paper.doi}"
    if getattr(paper, "pdf_path", None):
        links["pdf_file"] = str(paper.pdf_path)
    if links:
        d["links"] = links
    if getattr(paper, "needs_metadata", False):
        d["needs_metadata"] = True
    if full:
        for attr in ("abstract", "notes", "doi", "arxiv_id", "library", "library_key", "source", "added_at", "read_at"):
            value = getattr(paper, attr, None)
            if value:
                d[attr] = value
        if getattr(paper, "authors", None):
            d["authors_full"] = list(paper.authors)
    return d


# --- server ------------------------------------------------------------------


def _default_queue() -> Any:
    from .config import load_config
    from .service import PaperQueue

    return PaperQueue(load_config())


def build_server(queue: Any = None) -> FastMCP:
    """Build the MCP server. `queue` is a service.PaperQueue (created lazily if omitted)."""
    state: dict[str, Any] = {"queue": queue}

    def q() -> Any:
        if state["queue"] is None:
            state["queue"] = _default_queue()
        return state["queue"]

    def tz() -> tzinfo:
        return _queue_tz(q())

    def summ(paper: Any, **kw: Any) -> dict[str, Any]:
        return paper_summary(paper, tz(), **kw)

    def call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except KeyError as exc:
            arg = exc.args[0] if exc.args else ""
            raise ToolError(f"no paper with id {arg}" if isinstance(arg, int) else f"not found: {arg}") from exc
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - surface a readable message to the agent
            log.exception("tool failed")
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc

    pdf_lock = threading.Lock()

    def _cache_pdfs_soon() -> None:
        """Fetch PDFs into iCloud Drive in the background, so agent-added papers reach the
        iPhone even when `paperqueue serve` isn't running."""

        def work() -> None:
            if not pdf_lock.acquire(blocking=False):
                return
            try:
                q().cache_pdfs()
            except Exception:  # noqa: BLE001 - best effort, never disturb the MCP session
                log.exception("background PDF cache failed")
            finally:
                pdf_lock.release()

        threading.Thread(target=work, name="paperqueue-pdf", daemon=True).start()

    mcp = FastMCP("paperqueue", instructions=INSTRUCTIONS)

    @mcp.tool()
    def add_paper(
        ref: str,
        priority: str = "normal",
        tags: list[str] | None = None,
        notes: str = "",
        when: str = "next",
        title: str | None = None,
        media: str | None = None,
    ) -> dict:
        """Add a paper or YouTube video to the reading queue and (by default) schedule it in the
        next free slot.

        ref: arXiv ID, DOI, URL (preferred), YouTube link or title. priority: "high" | "normal" | "low".
        tags: short topic tags. when: "next" (default) | "today" | "tomorrow" | weekday | "YYYY-MM-DD" |
        ISO datetime in local time | "none". title: optional title (used if metadata lookup fails).
        media (YouTube only): omit for a link; "audio" or "video" only when the user asks for a
        download / offline copy. Deduplicates: returns status "already_queued" for items already queued."""
        res = call(
            q().add,
            ref,
            title=title,
            priority=priority,
            tags=tuple(tags or ()),
            notes=notes or "",
            when=when,
            source="agent",
            media=media,
        )
        paper = res.paper
        _cache_pdfs_soon()
        if res.created:
            status = "added"
        elif getattr(paper, "status", "to_read") == "read":
            status = "already_read"
        else:
            status = "already_queued"
        return {"status": status, "paper": summ(paper), "warnings": list(res.warnings)}

    @mcp.tool()
    def list_reading_list(
        view: str = "queue", query: str | None = None, tag: str | None = None, limit: int = 50
    ) -> dict:
        """List papers. view: "queue" (unread, unscheduled first by priority) | "upcoming" | "missed" |
        "read" | "all". query: free-text search over title/authors/abstract/tags. tag: filter by tag."""
        papers = call(q().list, view=view, query=query, tag=tag)
        limit = max(1, int(limit or 50))
        return {
            "view": view,
            "count": len(papers),
            "papers": [summ(p) for p in papers[:limit]],
            "truncated": len(papers) > limit,
        }

    @mcp.tool()
    def get_paper(paper_id: int) -> dict:
        """Full details of one paper (abstract, notes, identifiers, links, schedule)."""
        return summ(call(q().get, paper_id), full=True)

    @mcp.tool()
    def schedule_paper(paper_id: int, when: str) -> dict:
        """Schedule (or reschedule) a paper. when: "next" | "today" | "tomorrow" | weekday |
        "YYYY-MM-DD" | ISO datetime in local time | "none" to unschedule."""
        if (when or "").strip().lower() in ("", "none", "unschedule", "off"):
            res = call(q().unschedule, paper_id)
        else:
            res = call(q().schedule, paper_id, when)
        return {"paper": summ(res.paper), "warnings": list(res.warnings)}

    @mcp.tool()
    def auto_schedule(include_missed: bool = True, limit: int | None = None) -> dict:
        """Place unscheduled (and, if include_missed, missed) papers into free reading slots,
        highest priority first. Only call when the user asks to schedule their queue."""
        res = call(q().auto_schedule, include_missed=include_missed, limit=limit)
        return {
            "scheduled": [summ(p) for p in res.papers],
            "unplaced": [summ(p) for p in res.unplaced],
            "warnings": list(res.warnings),
        }

    @mcp.tool()
    def upcoming(days: int = 7) -> dict:
        """Reading sessions in the next `days` days (local time), plus missed sessions.
        Use for "what should I read today / this week?"."""
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(days=max(0, int(days)))
        papers = call(q().list, view="upcoming")
        sessions = []
        for p in papers:
            start = _parse_dt(getattr(p, "scheduled_start", None))
            if start is None or start > horizon:
                continue
            sessions.append(summ(p))
        missed = [summ(p) for p in call(q().list, view="missed")]
        return {
            "now": human_time(now, tz()),
            "days": days,
            "sessions": sessions,
            "missed": missed,
        }

    @mcp.tool()
    def mark_read(paper_id: int, notes: str | None = None) -> dict:
        """Mark a paper as read, or a video as watched (only when the user says so). Deletes a
        downloaded video file. Optional notes are appended to the item's notes."""
        warnings: list[str] = []
        if notes:
            current = call(q().get, paper_id)
            existing = (getattr(current, "notes", "") or "").rstrip()
            call(q().update, paper_id, notes=f"{existing}\n\n{notes}" if existing else notes)
        res = call(q().mark_read, paper_id, True)
        warnings += list(res.warnings)
        return {"paper": summ(res.paper), "warnings": warnings}

    @mcp.tool()
    def update_paper(
        paper_id: int,
        priority: str | None = None,
        tags: list[str] | None = None,
        notes: str | None = None,
        title: str | None = None,
    ) -> dict:
        """Change a paper's priority ("high"|"normal"|"low"), tags (replaces the list), notes
        (replaces) or title."""
        paper = call(q().update, paper_id, title=title, priority=priority, tags=tags, notes=notes)
        return {"paper": summ(paper), "warnings": []}

    @mcp.tool()
    def set_media(paper_id: int, mode: str) -> dict:
        """Choose how a YouTube item is kept: "link" (no download; deletes a local file),
        "audio" or "video" (downloaded in the background into iCloud Drive for offline use).
        Only on the user's request. Calling it again with the same mode retries a failed download."""
        res = call(q().set_media, paper_id, mode)
        return {"paper": summ(res.paper), "warnings": list(res.warnings)}

    @mcp.tool()
    def remove_paper(paper_id: int) -> dict:
        """Remove a paper from the queue (only when the user asks). Also removes its calendar event."""
        res = call(q().remove, paper_id)
        return {"removed": paper_id, "title": res.paper.title, "warnings": list(res.warnings)}

    @mcp.tool()
    def free_slots(n: int = 5) -> dict:
        """The next `n` free reading slots, in the user's local time."""
        slots = call(q().free_slots, max(1, int(n)))
        zone = tz()
        return {
            "slots": [
                {"local": human_time(s, zone), "iso": s.astimezone(zone).isoformat()} for s in slots
            ]
        }

    @mcp.tool()
    def sync_library() -> dict:
        """Pull the "To read" collection from the configured reference manager (Zotero/Mendeley)
        and push papers added here. No-op when no library is configured."""
        res = call(q().sync_library)
        return {
            "pulled": res.pulled,
            "created": res.created,
            "pushed": res.pushed,
            "warnings": list(res.warnings),
        }

    return mcp


def run() -> None:
    """Run the MCP server over stdio."""
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    build_server().run()


if __name__ == "__main__":  # pragma: no cover
    run()
