// paperqueue <-> Calendar.app bridge (JavaScript for Automation).
//
// Usage: /usr/bin/osascript -l JavaScript calendar.jxa.js '<json request>'
// Request: {"op": "check"|"list"|"apply", "calendar": "<name>", ...}
//   list:  {"marker": "paperqueue:#", "from_ms": <epoch ms>, "to_ms": <epoch ms>}
//   apply: {"upserts": [{paper_id, uid|null, title, start_ms, end_ms, url|null, notes, alert_minutes|null}],
//           "deletes": ["<uid>", ...]}
// Response (stdout, one JSON object):
//   check: {ok: true, calendar} | {ok: false, error: "calendar_not_found", calendars: [...]}
//   list:  {ok: true, events: [{uid, paper_id}]}
//   apply: {ok: true, results: [{paper_id, ok, uid|error}], deleted: [...], delete_errors: [...]}
//   any failure: {ok: false, error, error_number?}

function errInfo(e) {
  return { message: String((e && e.message) || e), number: e && e.errorNumber !== undefined ? e.errorNumber : null };
}

function findCalendar(app, name) {
  var matches = app.calendars.whose({ name: name });
  if (matches.length === 0) {
    return null;
  }
  return matches[0];
}

function calendarNames(app) {
  try {
    return app.calendars.name();
  } catch (e) {
    return [];
  }
}

function firstEventByUid(cal, uid) {
  if (!uid) {
    return null;
  }
  var found = cal.events.whose({ uid: uid });
  if (found.length === 0) {
    return null;
  }
  return found[0];
}

function setAlarm(app, ev, minutes) {
  var existing = ev.displayAlarms();
  for (var i = existing.length - 1; i >= 0; i--) {
    app.delete(existing[i]);
  }
  try {
    var sounds = ev.soundAlarms();
    for (var j = sounds.length - 1; j >= 0; j--) {
      app.delete(sounds[j]);
    }
  } catch (e) {
    // sound alarms are optional
  }
  if (minutes !== null && minutes !== undefined) {
    var alarm = app.DisplayAlarm({ triggerInterval: -Math.abs(minutes) });
    ev.displayAlarms.push(alarm);
  }
}

function upsert(app, cal, spec) {
  var start = new Date(spec.start_ms);
  var end = new Date(spec.end_ms);
  var ev = firstEventByUid(cal, spec.uid);
  if (ev === null) {
    var props = { summary: spec.title, startDate: start, endDate: end, description: spec.notes };
    if (spec.url) {
      props.url = spec.url;
    }
    ev = app.Event(props);
    cal.events.push(ev);
  } else {
    ev.summary = spec.title;
    // Keep start < end at every step: if moving later past the old end, move the end first.
    var oldEnd = ev.endDate();
    if (start.getTime() >= oldEnd.getTime()) {
      ev.endDate = end;
      ev.startDate = start;
    } else {
      ev.startDate = start;
      ev.endDate = end;
    }
    ev.description = spec.notes;
    if (spec.url) {
      ev.url = spec.url;
    }
  }
  setAlarm(app, ev, spec.alert_minutes);
  return ev.uid();
}

function doList(app, cal, req) {
  var from = new Date(req.from_ms);
  var to = new Date(req.to_ms);
  var marker = req.marker || "paperqueue:#";
  var evs = cal.events.whose({
    _and: [{ startDate: { _greaterThan: from } }, { startDate: { _lessThan: to } }, { description: { _contains: marker } }],
  });
  var uids = evs.uid();
  var notes = evs.description();
  var out = [];
  var re = new RegExp(marker.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "(\\d+)");
  for (var i = 0; i < uids.length; i++) {
    var m = re.exec(notes[i] || "");
    if (m) {
      out.push({ uid: uids[i], paper_id: parseInt(m[1], 10) });
    }
  }
  return { ok: true, events: out };
}

function doApply(app, cal, req) {
  var results = [];
  var upserts = req.upserts || [];
  for (var i = 0; i < upserts.length; i++) {
    var spec = upserts[i];
    try {
      results.push({ paper_id: spec.paper_id, ok: true, uid: upsert(app, cal, spec) });
    } catch (e) {
      var info = errInfo(e);
      results.push({ paper_id: spec.paper_id, ok: false, error: info.message, error_number: info.number });
    }
  }
  var deleted = [];
  var deleteErrors = [];
  var deletes = req.deletes || [];
  for (var k = 0; k < deletes.length; k++) {
    try {
      var ev = firstEventByUid(cal, deletes[k]);
      if (ev !== null) {
        app.delete(ev);
      }
      deleted.push(deletes[k]);
    } catch (e) {
      deleteErrors.push({ uid: deletes[k], error: errInfo(e).message });
    }
  }
  return { ok: true, results: results, deleted: deleted, delete_errors: deleteErrors };
}

function run(argv) {
  var req;
  try {
    req = JSON.parse(argv[0]);
  } catch (e) {
    return JSON.stringify({ ok: false, error: "bad_request: " + errInfo(e).message });
  }
  try {
    var app = Application("Calendar");
    var cal = findCalendar(app, req.calendar);
    if (cal === null) {
      return JSON.stringify({ ok: false, error: "calendar_not_found", calendars: calendarNames(app) });
    }
    if (req.op === "check") {
      return JSON.stringify({ ok: true, calendar: req.calendar });
    }
    if (req.op === "list") {
      return JSON.stringify(doList(app, cal, req));
    }
    if (req.op === "apply") {
      return JSON.stringify(doApply(app, cal, req));
    }
    return JSON.stringify({ ok: false, error: "unknown_op: " + req.op });
  } catch (e) {
    var info = errInfo(e);
    return JSON.stringify({ ok: false, error: info.message, error_number: info.number });
  }
}
