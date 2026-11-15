import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from paperqueue import calendar as calmod
from paperqueue.calendar.apple import SCRIPT, AppleCalendar, RunResult
from paperqueue.config import Config
from paperqueue.types import CalendarBackend, CalendarError, EventSpec, event_marker


class FakeRunner:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append((argv, timeout))
        r = self.responses.pop(0)
        if isinstance(r, BaseException):
            raise r
        if isinstance(r, RunResult):
            return r
        return RunResult(0, json.dumps(r) + "\n", "")

    def request(self, i=-1):
        return json.loads(self.calls[i][0][-1])


def spec(pid, uid=None, url="https://arxiv.org/abs/2401.00001"):
    start = datetime(2026, 11, 16, 7, 30, tzinfo=timezone.utc)
    return EventSpec(
        paper_id=pid,
        uid=uid,
        title=f"Read: paper {pid}",
        start_utc=start,
        end_utc=start + timedelta(minutes=45),
        url=url,
        notes=f"Some notes\n{event_marker(pid)}",
        alert_minutes=10,
    )


def test_script_exists_and_protocol():
    assert SCRIPT.is_file()
    cal = AppleCalendar("Reading List", runner=FakeRunner({"ok": True}))
    assert isinstance(cal, CalendarBackend)


def test_check_ok_and_argv():
    r = FakeRunner({"ok": True, "calendar": "Reading List"})
    cal = AppleCalendar("Reading List", runner=r)
    ok, msg = cal.check()
    assert ok and "Reading List" in msg
    argv, timeout = r.calls[0]
    assert argv[:4] == ["/usr/bin/osascript", "-l", "JavaScript", str(SCRIPT)]
    assert timeout == 120
    assert r.request() == {"calendar": "Reading List", "op": "check"}


def test_check_calendar_not_found_lists_names():
    r = FakeRunner({"ok": False, "error": "calendar_not_found", "calendars": ["Home", "Work"]})
    ok, msg = AppleCalendar("Reading List", runner=r).check()
    assert not ok
    assert "iCloud" in msg and "'Reading List'" in msg and "Home, Work" in msg


def test_permission_denied_nonzero_exit():
    r = FakeRunner(RunResult(1, "", "execution error: Error: Not authorized to send Apple events to Calendar. (-1743)"))
    ok, msg = AppleCalendar("X", runner=r).check()
    assert not ok and "Privacy & Security" in msg and "Automation" in msg


def test_permission_denied_in_json():
    r = FakeRunner({"ok": False, "error": "Not authorized to send Apple events to Calendar.", "error_number": -1743})
    with pytest.raises(CalendarError, match="Automation"):
        AppleCalendar("X", runner=r).list_uids()


def test_timeout():
    r = FakeRunner(subprocess.TimeoutExpired(cmd="osascript", timeout=120))
    with pytest.raises(CalendarError, match="120 s"):
        AppleCalendar("X", runner=r).list_uids()


def test_garbage_output():
    r = FakeRunner(RunResult(0, "not json", ""))
    with pytest.raises(CalendarError, match="Unexpected output"):
        AppleCalendar("X", runner=r).list_uids()


def test_other_osascript_failure():
    r = FakeRunner(RunResult(1, "", "syntax error"))
    with pytest.raises(CalendarError, match="syntax error"):
        AppleCalendar("X", runner=r).list_uids()


def test_list_uids_window_and_parse():
    r = FakeRunner({"ok": True, "events": [{"uid": "A", "paper_id": 3}, {"uid": "B", "paper_id": "7"}, {"bad": 1}]})
    out = AppleCalendar("X", runner=r).list_uids()
    assert out == {"A": 3, "B": 7}
    req = r.request()
    assert req["op"] == "list" and req["marker"] == "paperqueue:#"
    span_days = (req["to_ms"] - req["from_ms"]) / 86_400_000
    assert span_days == pytest.approx(425)


def test_apply_payload_and_results():
    r = FakeRunner(
        {
            "ok": True,
            "results": [{"paper_id": 1, "ok": True, "uid": "U1"}, {"paper_id": 2, "ok": False, "error": "boom"}],
            "deleted": ["OLD"],
        }
    )
    cal = AppleCalendar("X", runner=r)
    out = cal.apply([spec(1, uid="U1"), spec(2, url=None)], ["OLD"])
    assert out == {1: "U1"}
    assert len(r.calls) == 1  # one osascript call per batch
    req = r.request()
    assert req["op"] == "apply" and req["deletes"] == ["OLD"]
    u1, u2 = req["upserts"]
    assert u1["uid"] == "U1" and u2["uid"] is None
    assert u1["start_ms"] == int(datetime(2026, 11, 16, 7, 30, tzinfo=timezone.utc).timestamp() * 1000)
    assert u1["end_ms"] - u1["start_ms"] == 45 * 60 * 1000
    assert u1["alert_minutes"] == 10 and "paperqueue:#1" in u1["notes"]
    assert u2["url"] is None


def test_apply_empty_is_noop():
    r = FakeRunner()
    assert AppleCalendar("X", runner=r).apply([], []) == {}
    assert r.calls == []


def test_apply_total_failure_raises():
    r = FakeRunner({"ok": True, "results": [{"paper_id": 1, "ok": False, "error": "nope"}]})
    with pytest.raises(CalendarError, match="nope"):
        AppleCalendar("X", runner=r).apply([spec(1)], [])


def test_apply_deletes_only():
    r = FakeRunner({"ok": True, "results": [], "deleted": ["A"]})
    assert AppleCalendar("X", runner=r).apply([], ["A"]) == {}


def test_apply_rejects_naive_datetime():
    s = spec(1)
    s.start_utc = s.start_utc.replace(tzinfo=None)
    with pytest.raises(CalendarError):
        AppleCalendar("X", runner=FakeRunner()).apply([s], [])


def test_get_backend(tmp_path, monkeypatch):
    cfg = Config(home=tmp_path)
    cfg.calendar.backend = "off"
    assert calmod.get_backend(cfg) is None
    cfg.calendar.backend = "apple"
    monkeypatch.setattr(calmod.sys, "platform", "linux")
    assert calmod.get_backend(cfg) is None
    monkeypatch.setattr(calmod.sys, "platform", "darwin")
    b = calmod.get_backend(cfg)
    assert isinstance(b, AppleCalendar) and b.name == cfg.calendar.apple_calendar


@pytest.mark.skipif(not Path("/usr/bin/osacompile").exists(), reason="macOS only")
def test_jxa_compiles():
    # Syntax check only; never executes against Calendar.app.
    res = subprocess.run(
        ["/usr/bin/osacompile", "-l", "JavaScript", "-o", "/dev/null", str(SCRIPT)], capture_output=True, text=True
    )
    assert res.returncode == 0, res.stderr
