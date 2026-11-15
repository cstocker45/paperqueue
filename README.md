# paperqueue

A local-first reading queue for research papers. Save a paper from Chrome, the terminal or an AI
agent; paperqueue fetches its metadata, books a reading session in your Apple Calendar (so the
reminder reaches your Mac, iPhone and Apple Watch), saves the PDF to iCloud Drive for offline
reading, and keeps your Zotero or Mendeley "To read" collection in step.

Everything runs on your Mac. There is no account and no cloud service.

## Features

- **Add papers by arXiv ID, DOI, URL or title.** Metadata comes from arXiv, Crossref, Europe PMC,
  PubMed or the publisher page. Duplicates are detected.
- **Scheduled reading sessions.** You choose your reading slots (for example weekdays at 08:30
  for 45 min). New papers go into the next free slot, or into the time you name ("tomorrow",
  "friday", "2026-03-02 09:00"). Missed sessions are kept visible and are never moved silently.
- **Apple Calendar, with reminders everywhere.** Sessions are events in an iCloud calendar, and
  each carries an alert, so the reminder reaches every device signed in to iCloud.
- **Offline PDFs on iPhone.** Open-access PDFs are saved to `iCloud Drive/PaperQueue`, so you
  can read them in the Files app; papers you have read move to `PaperQueue/Read`.
- **A web app** at `http://localhost:8765`, with Upcoming, Queue and Read views, search, and
  scheduling. Chrome can install it as an app, and your iPhone can open it on your home network.
- **A Chrome bookmarklet** that saves the paper you are looking at.
- **Zotero or Mendeley sync** (optional). The app pulls your "To read" collection and can push
  papers you add back into it.
- **Agents.** An MCP server and a Claude Code skill let Claude (or any MCP client) queue papers
  you mention in a chat: "save this for later" just works.

## Install

paperqueue needs macOS for the Calendar integration; the rest works anywhere with Python 3.11+.
Install it with [uv](https://docs.astral.sh/uv/):

```sh
uv tool install git+https://github.com/cstocker45/paperqueue
```

Or install from a clone, which is editable, so a `git pull` updates it:

```sh
git clone https://github.com/cstocker45/paperqueue
cd paperqueue
uv tool install -e .
```

## Quick start

```sh
paperqueue serve            # then open http://localhost:8765
paperqueue add 1706.03762   # or a DOI, a URL, or a title
paperqueue upcoming
```

Settings live in `~/.paperqueue/config.toml`, which is created on first run. Run
`paperqueue doctor` at any point to check the setup; it prints a fix for each failed check.

## Apple Calendar

1. In Calendar.app choose **File > New Calendar > iCloud** and name the calendar **Reading List**.
   It must be an *iCloud* calendar, not "On My Mac", or it will not sync to your iPhone.
2. Run `paperqueue doctor`. macOS will ask whether your terminal may control Calendar; click
   **OK**. If you missed the prompt, go to **System Settings > Privacy & Security > Automation**
   and allow Calendar for your terminal (and later for the background service).
3. Optionally, set the alert lead time with `alert_minutes` under `[calendar]`. The default is 10
   minutes.

Each session is an event titled with the paper, with its link and abstract in the notes.
The event alert is the reminder: iCloud delivers it to your Mac, iPhone and Watch. paperqueue only
touches events it created itself.

## On your iPhone

- **Calendar.** Reading sessions appear in the Reading List calendar with a tappable link to the
  paper.
- **Offline PDFs.** In the Files app, open **iCloud Drive > PaperQueue**. Download the folder (or
  long-press it and choose "Keep Downloaded") so the PDFs are there on the train.
- **The web app.** Run `paperqueue serve --lan`, scan the QR code it prints with the iPhone
  camera, then choose **Share > Add to Home Screen**. The QR link carries your access token.

  This uses plain HTTP and is meant for your **home network only**. To reach it while you are
  away, use [Tailscale](https://tailscale.com) and add your Mac's Tailscale name to
  `extra_hosts` under `[server]`, for example `extra_hosts = ["my-mac.tailnet-name.ts.net"]`.

## Zotero (optional)

1. Install Zotero 7 and enable **Settings > Advanced > Allow other applications on this computer
   to communicate with Zotero** (the local API). Zotero must be running when paperqueue syncs.
2. Create a collection named **To read**, or set `collection` under `[zotero]`.
3. Set `provider = "zotero"` under `[library]`.
4. To write back as well, so that papers you add here reach Zotero on your iPhone, create a key
   at <https://www.zotero.org/settings/keys> with write access. Then set `user_id` and `api_key`
   under `[zotero]`, or the `ZOTERO_USER_ID` and `ZOTERO_API_KEY` environment variables.

Sync runs in the background while `serve` is running, or on demand with `paperqueue sync`.
Papers are never deleted from Zotero.

## Mendeley (optional)

Mendeley has no local API, so paperqueue uses its web API, which requires you to register your
own app.

> **Caveat.** Elsevier sometimes restricts new app registrations at dev.mendeley.com, and
> Mendeley's mobile apps have been retired. If you cannot register an app, use Zotero or no
> library at all; paperqueue works fully on its own.

1. Register an app at <https://dev.mendeley.com/myapps.html> with the redirect URL
   `http://127.0.0.1:8766/callback`.
2. Put the client ID and secret under `[mendeley]`, or in `MENDELEY_CLIENT_ID` and
   `MENDELEY_CLIENT_SECRET`, and set `provider = "mendeley"` under `[library]`.
3. Run `paperqueue mendeley login`. Your browser opens, and once you approve, the token is stored
   in `~/.paperqueue/mendeley_token.json` (readable only by you).

The folder used is **To read**; set `folder` under `[mendeley]` to change it.

## Chrome

- **Bookmarklet.** Open the web app and drag the **Save to paperqueue** link to your bookmarks
  bar. On any paper page (arXiv, bioRxiv, a journal), click it to see a confirmation page, then
  click Save. It reads the page's citation metadata, so it also works on paywalled pages.
- **Install as an app.** In Chrome, open `http://localhost:8765` and choose **Install app** from
  the address bar or the ⋮ menu. paperqueue then opens in its own window and Dock icon.

## Agents (Claude Code, Claude Desktop, Cursor)

Register the MCP server with Claude Code for all your projects, and install the skill:

```sh
claude mcp add -s user paperqueue -- paperqueue mcp
paperqueue install-skill        # writes ~/.claude/skills/paper-queue/SKILL.md
```

After this, saying "add this paper", "save it for later" or "I want to read the AlphaFold 3
paper on Friday" queues and schedules it. Claude reports the time slot, offers once to queue
papers it recommends itself, and never marks papers read or removes them unless you ask. Ask
"what should I read today?" to see your sessions.

For other MCP clients, add this to the client's MCP config (for example
`claude_desktop_config.json` or `~/.cursor/mcp.json`). Use the full path from `which paperqueue`
if the client does not see your shell's `PATH`:

```json
{
  "mcpServers": {
    "paperqueue": {
      "command": "paperqueue",
      "args": ["mcp"]
    }
  }
}
```

Tools: `add_paper`, `list_reading_list`, `get_paper`, `schedule_paper`, `auto_schedule`,
`upcoming`, `free_slots`, `mark_read`, `update_paper`, `remove_paper` and `sync_library`.
The MCP server works directly on the local database, so `serve` does not need to be running.

## Run at login

```sh
paperqueue install-service             # LaunchAgent: starts `serve` at login, restarts on failure
paperqueue install-service --dry-run   # show the plist without installing it
paperqueue install-service --uninstall
```

Logs go to `~/.paperqueue/logs/`. The first calendar sync from the service may trigger the
Automation prompt again, this time for Python.

## Commands

| Command | |
|---|---|
| `serve [--lan] [--port N] [--no-maintenance]` | Web app plus background sync |
| `add REF [--when W] [--priority P] [--tag T] [--title T] [--notes N]` | Add a paper |
| `list [queue\|upcoming\|missed\|read\|all] [-q TEXT] [-t TAG] [--json]` | List papers |
| `upcoming [--days N] [--json]` | Sessions coming up, plus missed ones |
| `schedule ID [WHEN]` | Schedule or reschedule; `WHEN=none` unschedules |
| `auto-schedule [--no-missed] [--limit N]` | Fill free slots from the queue |
| `read ID [--unread]`, `remove ID` | Mark read; remove |
| `sync [--library\|--calendar] [--force]` | Sync the library and the calendar now |
| `doctor` | Check the setup |
| `config` | Show the config path and contents, with secrets masked |
| `mcp`, `install-skill`, `install-service` | Agents and background service |
| `mendeley login` | Authorise Mendeley |

`WHEN` can be `next` (the default), `today`, `tomorrow`, a weekday, `YYYY-MM-DD`, an ISO datetime
in local time, or `none`.

## Configuration

`~/.paperqueue/config.toml` (set `PAPERQUEUE_HOME` to use another directory):

| Section | Key | Default | |
|---|---|---|---|
| `[server]` | `host`, `port` | `127.0.0.1`, `8765` | `--lan` binds `0.0.0.0` |
| | `token` | random | Required from other devices; keep it private |
| | `extra_hosts` | `[]` | Extra host names to accept, e.g. a Tailscale name |
| `[library]` | `provider` | `"off"` | `"zotero"`, `"mendeley"` or `"off"` |
| `[zotero]` | `source` | `"local"` | `"local"` (Zotero 7 desktop) or `"web"` |
| | `collection` | `"To read"` | |
| | `user_id`, `api_key` | empty | Web API key, needed to write back |
| | `push_new_papers` | `true` | Add papers you queue here to Zotero |
| `[mendeley]` | `client_id`, `client_secret` | empty | From dev.mendeley.com |
| | `folder`, `push_new_papers`, `redirect_port` | `"To read"`, `true`, `8766` | |
| `[calendar]` | `backend` | `"apple"` | `"apple"` or `"off"` |
| | `apple_calendar` | `"Reading List"` | Name of the iCloud calendar |
| | `alert_minutes` | `10` | Reminder lead time |
| `[schedule]` | `days`, `times` | Mon–Fri, `["08:30"]` | Your reading slots |
| | `duration_minutes`, `horizon_days` | `45`, `14` | |
| | `timezone` | system | IANA name, e.g. `"Europe/Berlin"` |
| `[pdf]` | `enabled`, `dir` | `true`, `iCloud Drive/PaperQueue` | Offline PDF folder |

Environment overrides: `PAPERQUEUE_HOME`, `PAPERQUEUE_TOKEN`, `PAPERQUEUE_LIBRARY`,
`PAPERQUEUE_CALENDAR` (`apple` or `off`), `ZOTERO_USER_ID`, `ZOTERO_API_KEY`, `MENDELEY_CLIENT_ID` and
`MENDELEY_CLIENT_SECRET`.

## Privacy

Everything stays on your Mac: the queue is a SQLite file in `~/.paperqueue/`, and the config file
is readable only by you. paperqueue makes outbound requests only to the public metadata services
(arXiv, Crossref, Europe PMC, PubMed and the paper's own web page), to open-access PDF links, and
to the reference manager you choose. There is no telemetry. The web server listens on
`localhost` unless you pass `--lan`, and other devices need the token.

## Development

```sh
git clone https://github.com/cstocker45/paperqueue && cd paperqueue
uv sync
uv run pytest
uv run paperqueue serve
```

The code lives in `src/paperqueue/`. `service.py` holds the business logic, which the web API
(`server.py`), the CLI (`cli.py`) and the MCP server (`mcp_server.py`) all share. The tests mock
all HTTP and never touch your real calendar or library.

## License

MIT
