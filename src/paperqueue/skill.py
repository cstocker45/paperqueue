"""Claude Code skill for paperqueue. `paperqueue install-skill` writes SKILL_MD to
~/.claude/skills/paper-queue/SKILL.md."""

from __future__ import annotations

from pathlib import Path

SKILL_MD = """\
---
name: paper-queue
description: >-
  Add research papers and YouTube videos to the user's reading queue and schedule sessions in
  their calendar, via the paperqueue MCP tools. Use when the user wants to read, watch, listen to,
  save, queue, bookmark or be reminded about a paper or video ("add this", "save for later",
  "I want to read X", "watch this later", "queue it", "put it on my reading list", "remind me to
  read"), shares an arXiv ID, DOI, paper URL or YouTube link to keep, asks what to read or watch
  today or this week, asks to schedule or reschedule items, or after you recommend papers (offer
  once to queue them).
---

# Paper queue

The user keeps a reading queue in **paperqueue**. Adding a paper schedules a reading session in
their Apple Calendar (alerts reach Mac, iPhone and Watch), caches the PDF for offline reading where
possible, and mirrors it to Zotero or Mendeley if configured. Use the `paperqueue` MCP tools.

## When to act

- **The user expresses intent to read a paper** ("add this", "save for later", "I want to read X",
  "queue it"): call `add_paper` immediately. Do not ask for confirmation.
- **You recommended papers yourself**: offer once, in one line, to queue them
  ("Want me to add these to your reading queue?"). Don't repeat the offer; don't add unasked.
- **"What should I read today / this week?"**: call `upcoming`.

## How to add

- `ref`: prefer an identifier: arXiv ID (`2401.01234`), DOI (`10.1038/...`) or URL (arXiv,
  bioRxiv, PubMed, publisher page). A bare title is a last resort. If you have a URL and a title,
  pass the URL as `ref` and the title as `title`.
- `when`: keep the default `"next"` (next free reading slot) unless the user names a time:
  `"today"`, `"tomorrow"`, `"friday"`, `"2026-03-02"`, `"2026-03-02T09:00"` (local time),
  or `"none"` to queue without scheduling.
- `tags`: one to three short lowercase tags from the conversation topic.
- `priority`: `"normal"` unless the user signals urgency (`"high"`) or "someday" (`"low"`).

## YouTube videos

- A YouTube link (`youtube.com/watch?v=`, `youtu.be/`, `/shorts/`, `/live/`) goes through
  `add_paper(ref=<link>)` like a paper. The session lasts as long as the video (capped at 90
  minutes) and the event reads "Watch: ...". Playlists are refused; add videos one at a time.
- Keep the default, a link only. Pass `media="audio"` or `media="video"` only when the user asks
  for a download, offline viewing or audio ("for the flight", "as a podcast", "download it").
  Downloading is the user's choice and responsibility under YouTube's Terms of Service.
- Change it later with `set_media(paper_id, mode)`; `"link"` deletes the local file. Downloads run
  in the background: report `media.status`, and when it is `"failed"`, pass on `media.fix`.
- Watched videos are marked with `mark_read`, which also deletes the downloaded file.

## Reporting

- Say what was queued and when, in the user's local time, using the returned `scheduled` field:
  "Queued *Attention Is All You Need* for Tue 18 Nov, 08:30."
- If `add_paper` returns `"already_queued"`, say so (with its scheduled time). Never add it again;
  the tool dedupes.
- Mention warnings briefly (e.g. calendar unavailable: the paper is still queued).

## Rules

- Never call `mark_read`, `remove_paper` or `set_media` unless the user asks.
- Don't reschedule existing sessions or run `auto_schedule` unless asked.

## Tools

| Tool | Use |
|---|---|
| `add_paper(ref, priority, tags, notes, when, title, media)` | Add and schedule (dedupes) |
| `list_reading_list(view, query, tag, limit)` | view: queue, upcoming, missed, read, all; search |
| `get_paper(paper_id)` | Full details incl. abstract and notes |
| `schedule_paper(paper_id, when)` | Schedule or reschedule; `when="none"` unschedules |
| `auto_schedule(include_missed, limit)` | Fill free slots from the queue (on request) |
| `upcoming(days)` | Sessions in the next N days plus missed ones |
| `free_slots(n)` | Next free reading slots |
| `mark_read(paper_id, notes)` | Only when the user says they read or watched it |
| `update_paper(paper_id, priority, tags, notes, title)` | Edit fields |
| `set_media(paper_id, mode)` | YouTube: `link`, `audio` or `video` (on request) |
| `remove_paper(paper_id)` | Only when asked |
| `sync_library()` | Pull/push the Zotero or Mendeley "To read" collection |

## Examples

- User: "Save 2310.06825 for later." -> `add_paper(ref="2310.06825", tags=["llm"])` ->
  "Queued *Mistral 7B* for Mon 17 Nov, 08:30."
- User: "I want to read the AlphaFold 3 paper on Friday." ->
  `add_paper(ref="10.1038/s41586-024-07487-w", when="friday", tags=["protein-structure"])`
- User: "Add this: https://www.biorxiv.org/content/10.1101/2024.01.01.573000v1" -> `add_paper(ref=<url>)`
- You list five papers on diffusion models -> end with one line: "Want me to queue any of these?"
- User: "What's on my reading list today?" -> `upcoming(days=1)`
- User: "Move it to tomorrow." -> `schedule_paper(paper_id=<id>, when="tomorrow")`
- User: "Watch this later: https://youtu.be/dQw4w9WgXcQ" -> `add_paper(ref=<url>)` ->
  "Queued *...* (42 min) for Tue 18 Nov, 08:30."
- User: "Download that talk as audio for my flight." -> `set_media(paper_id=<id>, mode="audio")`
"""


def default_skill_path() -> Path:
    return Path.home() / ".claude" / "skills" / "paper-queue" / "SKILL.md"


def install_skill(dest: Path | str | None = None) -> Path:
    """Write SKILL_MD. `dest` may be a SKILL.md path or a directory (SKILL.md is written inside it)."""
    path = Path(dest).expanduser() if dest else default_skill_path()
    if path.suffix.lower() != ".md":
        path = path / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SKILL_MD, encoding="utf-8")
    return path
