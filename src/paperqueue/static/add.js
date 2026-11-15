/* Bookmarklet confirm page. Loading it never changes anything; only Save POSTs. */
"use strict";

(function () {
  const $ = (sel) => document.querySelector(sel);

  function decodeHints(raw) {
    if (!raw) return {};
    try {
      let b64 = raw.replace(/-/g, "+").replace(/_/g, "/");
      while (b64.length % 4) b64 += "=";
      const bin = atob(b64);
      const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
      const obj = JSON.parse(new TextDecoder().decode(bytes));
      return obj && typeof obj === "object" && !Array.isArray(obj) ? obj : {};
    } catch (_) {
      return {};
    }
  }

  function clean(value, max) {
    if (typeof value !== "string") return "";
    return value.replace(/\s+/g, " ").trim().slice(0, max || 2000);
  }

  function safeUrl(url) {
    try {
      const u = new URL(url);
      return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
    } catch (_) {
      return null;
    }
  }

  function toast(message, kind) {
    const node = document.createElement("div");
    node.className = `toast toast-${kind || "info"}`;
    const tick = document.createElement("span");
    tick.className = "toast-tick";
    tick.setAttribute("aria-hidden", "true");
    tick.textContent = { warn: "[ ! ]", error: "[ERR]" }[kind] || "[OK]";
    node.append(tick);
    const text = document.createElement("span");
    text.className = "toast-text";
    text.textContent = message;
    node.append(text);
    $("#toasts").append(node);
    setTimeout(() => node.remove(), 8000);
  }

  const params = new URLSearchParams(location.search);
  const raw = decodeHints(params.get("hints"));
  const ref = clean(params.get("ref") || raw.url || raw.doi || "", 2000);

  // Keep only the fields we understand, as plain strings.
  const hints = {};
  for (const key of ["url", "title", "doi", "pdf_url", "journal", "date", "abstract"]) {
    const v = clean(raw[key], key === "abstract" ? 4000 : 1000);
    if (v) hints[key] = v;
  }
  if (Array.isArray(raw.authors)) {
    const authors = raw.authors.map((a) => clean(a, 200)).filter(Boolean).slice(0, 200);
    if (authors.length) hints.authors = authors;
  }
  if (!hints.url && safeUrl(ref)) hints.url = ref;

  // ---- render the hints

  $("#hint-title").textContent = hints.title || ref || "Nothing to save";
  if (!hints.title && ref) $("#hint-kicker").textContent = "REFERENCE";
  document.title = hints.title ? `Save: ${hints.title}` : "Save to paperqueue";

  const meta = $("#hint-meta");
  function row(label, value, href) {
    if (!value) return;
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    if (href) {
      const a = document.createElement("a");
      a.href = href;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = value;
      dd.append(a);
    } else {
      dd.textContent = value;
    }
    meta.append(dt, dd);
  }
  if (hints.authors) {
    const a = hints.authors;
    row("AUTHORS", a.length > 6 ? `${a.slice(0, 5).join(", ")} and ${a.length - 5} more` : a.join(", "));
  }
  row("JOURNAL", [hints.journal, hints.date].filter(Boolean).join(" · "));
  if (hints.doi) row("DOI", hints.doi, `https://doi.org/${encodeURI(hints.doi)}`);
  if (hints.url) {
    let host = hints.url;
    try {
      host = new URL(hints.url).host + new URL(hints.url).pathname.replace(/\/$/, "");
    } catch (_) {}
    row("PAGE", host, safeUrl(hints.url));
  }
  row("PDF", hints.pdf_url ? "AVAILABLE" : "", safeUrl(hints.pdf_url));
  if (hints.abstract) {
    const p = $("#hint-abstract");
    p.textContent = hints.abstract.length > 600 ? hints.abstract.slice(0, 600).trimEnd() + " …" : hints.abstract;
    p.hidden = false;
  }

  const form = $("#save-form");
  const saveButton = $("#save-button");
  if (!ref && !hints.title) {
    saveButton.disabled = true;
    toast("This link has no paper reference. Use the bookmarklet on a paper’s page.", "warn");
  }

  $("#save-when").addEventListener("change", (e) => {
    const pick = e.target.value === "pick";
    $("#save-at-field").hidden = !pick;
    if (pick) $("#save-at").focus();
  });

  // ---- time formatting for the result

  let tz;
  function whenText(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return "";
    const opts = { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" };
    try {
      return new Intl.DateTimeFormat("en-GB", Object.assign({ timeZone: tz }, opts)).format(d).replace(",", "").replace(" at", ",");
    } catch (_) {
      return new Intl.DateTimeFormat("en-GB", opts).format(d);
    }
  }

  fetch("/api/status", { credentials: "same-origin" })
    .then((r) => (r.ok ? r.json() : null))
    .then((st) => {
      if (st && st.schedule && st.schedule.timezone) tz = st.schedule.timezone;
    })
    .catch(() => {});

  // ---- save

  async function save(ev) {
    if (ev) ev.preventDefault();
    if (saveButton.disabled) return;
    let when = $("#save-when").value;
    if (when === "pick") {
      when = $("#save-at").value;
      if (!when) {
        $("#save-at").focus();
        toast("Pick a date and time, or choose another schedule option.", "warn");
        return;
      }
    }
    const tags = $("#save-tags")
      .value.split(",")
      .map((t) => t.trim())
      .filter(Boolean);
    saveButton.disabled = true;
    saveButton.textContent = "SAVING…";
    try {
      const res = await fetch("/api/papers", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", Accept: "application/json", "X-PaperQueue": "1" },
        body: JSON.stringify({
          ref: ref || hints.title,
          title: hints.title || null,
          hints,
          priority: Number($("#save-priority").value),
          tags,
          when,
          source: "web",
        }),
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) throw new Error((data && typeof data.detail === "string" && data.detail) || `Save failed (${res.status})`);
      showResult(data);
    } catch (err) {
      toast(err.message || String(err), "error");
      saveButton.disabled = false;
      saveButton.textContent = "[ENTER] SAVE";
    }
  }

  function showResult(data) {
    const p = data.paper || {};
    const start = p.scheduled_start_local || p.scheduled_local || p.scheduled_start;
    form.hidden = true;
    $("#result").hidden = false;
    $("#result-title").textContent = data.created === false ? "[OK] ALREADY IN YOUR QUEUE" : "[OK] SAVED";
    $("#result-when").textContent = start
      ? `Reading session: ${whenText(start)}${p.duration_minutes ? `, ${p.duration_minutes} min` : ""}.`
      : "In your queue, not scheduled yet.";
    $("#result-warnings").textContent = (data.warnings || []).join(" ");
    $("#result-title").focus();
  }

  form.addEventListener("submit", save);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !form.hidden) save(e);
    else if (e.key === "Escape" && !form.hidden) closeTab();
  });

  function closeTab() {
    window.close();
    // Browsers refuse to close tabs they didn't open; fall back to the queue.
    setTimeout(() => {
      if (!window.closed) location.href = "/";
    }, 150);
  }
  $("#close-button").addEventListener("click", closeTab);
  $("#cancel-button").addEventListener("click", closeTab);
})();
