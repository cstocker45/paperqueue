"""Apple Calendar backend: drives Calendar.app through JXA (`osascript -l JavaScript`).

Events created in an iCloud calendar sync to the iPhone (and Watch), and their display alarm is the
reading reminder. Each batch of changes is a single osascript call; requests go in as JSON on argv and
the script prints one JSON object (see calendar.jxa.js).
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..types import CalendarError, EventSpec

SCRIPT = Path(__file__).with_name("calendar.jxa.js")
OSASCRIPT = "/usr/bin/osascript"
TIMEOUT_S = 120
MARKER_PREFIX = "paperqueue:#"
LIST_PAST_DAYS = 60
LIST_FUTURE_DAYS = 365

PERMISSION_HELP = (
    "macOS blocked paperqueue from controlling Calendar. Open System Settings > Privacy & Security > "
    "Automation, find the app running paperqueue (Terminal, iTerm, Python, ...), and enable 'Calendar' for it. "
    "If it is not listed, run `paperqueue doctor` from a terminal to trigger the permission prompt."
)


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str


# runner(argv, timeout_seconds) -> RunResult; raises subprocess.TimeoutExpired on timeout.
Runner = Callable[[list[str], float], RunResult]


def _subprocess_runner(argv: list[str], timeout: float) -> RunResult:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return RunResult(proc.returncode, proc.stdout, proc.stderr)


def _is_permission_error(text: str, number: object = None) -> bool:
    return str(number) == "-1743" or "-1743" in text or "Not authorized to send Apple events" in text


def _ms(dt) -> int:
    if dt.tzinfo is None:
        raise CalendarError("EventSpec times must be timezone-aware")
    return int(dt.timestamp() * 1000)


class AppleCalendar:
    """CalendarBackend for a named calendar in Calendar.app."""

    def __init__(self, name: str, runner: Runner | None = None, *, timeout: float = TIMEOUT_S):
        self.name = name
        self._runner = runner or _subprocess_runner
        self._timeout = timeout

    # -- plumbing -------------------------------------------------------------------------------

    def _not_found_message(self, calendars: list[str] | None) -> str:
        msg = (
            f"Calendar '{self.name}' was not found in Calendar.app. Create it: Calendar > File > New Calendar > "
            f"iCloud (so it syncs to your iPhone), name it '{self.name}', or set calendar.apple_calendar in the "
            "paperqueue config to an existing calendar."
        )
        if calendars:
            msg += " Existing calendars: " + ", ".join(sorted(set(calendars))) + "."
        return msg

    def _call(self, request: dict) -> dict:
        request = {"calendar": self.name, **request}
        argv = [OSASCRIPT, "-l", "JavaScript", str(SCRIPT), json.dumps(request, ensure_ascii=False)]
        try:
            res = self._runner(argv, self._timeout)
        except subprocess.TimeoutExpired:
            raise CalendarError(
                f"Calendar.app did not respond within {int(self._timeout)} s. If a permission dialog is open, "
                "answer it; otherwise open Calendar.app once and retry. " + PERMISSION_HELP
            ) from None
        except FileNotFoundError:
            raise CalendarError(f"{OSASCRIPT} not found: the Apple Calendar backend needs macOS.") from None
        except OSError as e:
            raise CalendarError(f"Could not run osascript: {e}") from None

        out = (res.stdout or "").strip()
        err = (res.stderr or "").strip()
        if res.returncode != 0:
            if _is_permission_error(err + out):
                raise CalendarError(PERMISSION_HELP)
            raise CalendarError(f"osascript failed (exit {res.returncode}): {err or out or 'no output'}")
        try:
            data = json.loads(out.splitlines()[-1] if out else "")
        except (json.JSONDecodeError, IndexError):
            raise CalendarError(f"Unexpected output from Calendar script: {out[:200]!r} {err[:200]}") from None
        if not isinstance(data, dict):
            raise CalendarError(f"Unexpected output from Calendar script: {out[:200]!r}")
        if not data.get("ok"):
            error = str(data.get("error") or "unknown error")
            if error == "calendar_not_found":
                raise CalendarError(self._not_found_message(data.get("calendars")))
            if _is_permission_error(error, data.get("error_number")):
                raise CalendarError(PERMISSION_HELP)
            raise CalendarError(f"Calendar.app error: {error}")
        return data

    # -- CalendarBackend ------------------------------------------------------------------------

    def check(self) -> tuple[bool, str]:
        try:
            self._call({"op": "check"})
        except CalendarError as e:
            return False, str(e)
        return True, f"Apple Calendar '{self.name}' is reachable."

    def list_uids(self) -> dict[str, int]:
        now_ms = int(time.time() * 1000)
        day = 86_400_000
        data = self._call(
            {
                "op": "list",
                "marker": MARKER_PREFIX,
                "from_ms": now_ms - LIST_PAST_DAYS * day,
                "to_ms": now_ms + LIST_FUTURE_DAYS * day,
            }
        )
        out: dict[str, int] = {}
        for ev in data.get("events") or []:
            try:
                out[str(ev["uid"])] = int(ev["paper_id"])
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def apply(self, upserts: list[EventSpec], deletes: list[str]) -> dict[int, str]:
        if not upserts and not deletes:
            return {}
        payload = {
            "op": "apply",
            "upserts": [
                {
                    "paper_id": s.paper_id,
                    "uid": s.uid,
                    "title": s.title,
                    "start_ms": _ms(s.start_utc),
                    "end_ms": _ms(s.end_utc),
                    "url": s.url or None,
                    "notes": s.notes,
                    "alert_minutes": s.alert_minutes,
                }
                for s in upserts
            ],
            "deletes": list(deletes),
        }
        data = self._call(payload)
        results = data.get("results") or []
        done: dict[int, str] = {}
        errors: list[str] = []
        for r in results:
            if r.get("ok") and r.get("uid"):
                done[int(r["paper_id"])] = str(r["uid"])
            else:
                err = str(r.get("error") or "unknown error")
                if _is_permission_error(err, r.get("error_number")):
                    raise CalendarError(PERMISSION_HELP)
                errors.append(f"paper {r.get('paper_id')}: {err}")
        if upserts and not done:
            raise CalendarError("Calendar.app rejected every event: " + "; ".join(errors or ["no results"]))
        return done
