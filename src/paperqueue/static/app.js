/* paperqueue web app. Vanilla JS, no build step. DOM is built with createElement/textContent only. */
"use strict";

(function () {
  // ---------------------------------------------------------------- utilities

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === "class") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k === "dataset") Object.assign(node.dataset, v);
        else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
        else node.setAttribute(k, v === true ? "" : String(v));
      }
    }
    for (const child of [].concat(children || [])) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function playIcon(size) {
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    for (const [k, v] of Object.entries({
      "aria-hidden": "true",
      width: size,
      height: size,
      viewBox: "0 0 24 24",
      fill: "none",
      stroke: "currentColor",
      "stroke-width": "1.5",
    }))
      svg.setAttribute(k, v);
    const poly = document.createElementNS(NS, "polygon");
    poly.setAttribute("points", "8,5 19,12 8,19");
    svg.append(poly);
    return svg;
  }

  function safeUrl(url, schemes) {
    if (!url || typeof url !== "string") return null;
    try {
      const u = new URL(url, location.origin);
      return (schemes || ["http:", "https:"]).includes(u.protocol) ? u.href : null;
    } catch (_) {
      return null;
    }
  }

  function store(key, value) {
    try {
      if (value === undefined) return localStorage.getItem(key);
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    } catch (_) {}
    return null;
  }

  class ApiError extends Error {
    constructor(message, status) {
      super(message);
      this.status = status;
    }
  }

  async function api(path, opts) {
    const o = opts || {};
    const init = { method: o.method || "GET", headers: { Accept: "application/json" }, credentials: "same-origin" };
    if (init.method !== "GET") init.headers["X-PaperQueue"] = "1";
    if (o.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(o.body);
    }
    const res = await fetch(path, init);
    let data = null;
    try {
      data = await res.json();
    } catch (_) {
      data = null;
    }
    if (!res.ok) {
      let msg = data && data.detail;
      if (Array.isArray(msg)) msg = msg.map((d) => d.msg || String(d)).join("; ");
      throw new ApiError(msg || `Request failed (${res.status})`, res.status);
    }
    return data;
  }

  const reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const phoneQuery = window.matchMedia("(max-width: 700px)");

  // ---------------------------------------------------------------- spinner

  const SPIN = ["|", "/", "-", "\\"];
  let spinStep = 1;
  function spinner() {
    return el("span", { class: "ink spin", "aria-hidden": "true", text: `[ ${SPIN[spinStep]} ]` });
  }
  if (!reduceMotion) {
    setInterval(() => {
      const nodes = $$(".spin");
      if (!nodes.length) return;
      spinStep = (spinStep + 1) % SPIN.length;
      for (const n of nodes) n.textContent = `[ ${SPIN[spinStep]} ]`;
    }, 140);
  }

  // ---------------------------------------------------------------- toasts

  const toasts = $("#toasts");
  const TICKS = { info: "[OK]", warn: "[ ! ]", error: "[ERR]" };

  function toast(message, opts) {
    const o = opts || {};
    const kind = o.kind || "info";
    const node = el("div", { class: `toast toast-${kind}` }, [
      el("span", { class: "toast-tick", text: TICKS[kind] || TICKS.info, "aria-hidden": "true" }),
      el("span", { class: "visually-hidden", text: kind === "error" ? "Error: " : kind === "warn" ? "Note: " : "" }),
      el("span", { class: "toast-text", text: message }),
    ]);
    let timer = null;
    const close = () => {
      if (!node.isConnected) return;
      clearTimeout(timer);
      node.remove();
    };
    if (o.action) {
      node.append(
        el("button", {
          type: "button",
          text: `[${o.action.label.toUpperCase()}]`,
          onclick: () => {
            close();
            o.action.run();
          },
        })
      );
    }
    node.append(el("button", { type: "button", text: "[×]", "aria-label": "Dismiss", onclick: close }));
    toasts.append(node);
    while (toasts.children.length > 4) toasts.firstElementChild.remove();
    timer = setTimeout(close, o.timeout || (kind === "info" ? 4500 : 9000));
    return { close, node };
  }

  function showWarnings(data) {
    for (const w of (data && data.warnings) || []) toast(w, { kind: "warn" });
  }

  function showError(err) {
    toast(err && err.message ? err.message : String(err), { kind: "error" });
  }

  // ---------------------------------------------------------------- state

  const VIEWS = ["upcoming", "queue", "media", "read"];
  const state = {
    view: "upcoming",
    q: "",
    tag: "",
    rev: null,
    status: null,
    tz: undefined,
    items: [], // the filtered list behind the current view
    all: [], // unfiltered, for counts and meters
    order: [], // ids in display order
    selId: null,
    removeAsk: null, // id awaiting the inline remove check
    loading: false,
    reloadQueued: false,
    hiding: new Set(),
    dialogPaper: null,
    slotPick: null,
    players: new Map(), // id:mode -> audio/video element, kept so playback survives re-renders
    syncing: { library: false, calendar: false },
    nextSlot: undefined,
    addMode: "link",
    qrTarget: "url",
    booted: false,
  };

  const byId = (id) => state.items.find((p) => p.id === id) || null;

  // ---------------------------------------------------------------- time

  function startIso(p) {
    return p.scheduled_start_local || p.scheduled_start || null;
  }

  function fmt(opts) {
    try {
      return new Intl.DateTimeFormat("en-GB", Object.assign({ timeZone: state.tz }, opts));
    } catch (_) {
      return new Intl.DateTimeFormat("en-GB", opts);
    }
  }

  function zonedParts(iso) {
    const d = iso instanceof Date ? iso : new Date(iso);
    if (isNaN(d)) return null;
    let f;
    try {
      f = new Intl.DateTimeFormat("en-CA", {
        timeZone: state.tz,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
      });
    } catch (_) {
      f = new Intl.DateTimeFormat("en-CA", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
    }
    const parts = {};
    for (const part of f.formatToParts(d)) parts[part.type] = part.value;
    const hour = parts.hour === "24" ? "00" : parts.hour;
    return {
      day: `${parts.year}-${parts.month}-${parts.day}`,
      time: `${hour}:${parts.minute}`,
      local: `${parts.year}-${parts.month}-${parts.day}T${hour}:${parts.minute}`,
    };
  }

  function dayKeyOffset(offsetDays, fromKey) {
    const key = fromKey || zonedParts(new Date()).day;
    const [y, m, d] = key.split("-").map(Number);
    return new Date(Date.UTC(y, m - 1, d + offsetDays)).toISOString().slice(0, 10);
  }

  const dayFmt = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });
  const dayFmtYear = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });

  function keyDate(dayKey) {
    const [y, m, d] = dayKey.split("-").map(Number);
    return new Date(Date.UTC(y, m - 1, d));
  }

  // "SUN 15 NOV" (with the year when it is not this year)
  function plainDay(dayKey) {
    const thisYear = dayKeyOffset(0).slice(0, 4);
    return (dayKey.slice(0, 4) === thisYear ? dayFmt : dayFmtYear).format(keyDate(dayKey)).replace(/,/g, "").toUpperCase();
  }

  function relDay(dayKey) {
    if (dayKey === dayKeyOffset(0)) return "TODAY";
    if (dayKey === dayKeyOffset(1)) return "TOMORROW";
    if (dayKey === dayKeyOffset(-1)) return "YESTERDAY";
    return null;
  }

  function whenText(iso) {
    const p = zonedParts(iso);
    if (!p) return "";
    return `${relDay(p.day) || plainDay(p.day)} ${p.time}`;
  }

  function sessionText(p) {
    const iso = startIso(p);
    if (!iso) return null;
    const s = zonedParts(iso);
    let text = `${relDay(s.day) || plainDay(s.day)} ${s.time}`;
    const endIso = p.scheduled_end_local || p.scheduled_end;
    if (endIso) text += `–${zonedParts(endIso).time}`;
    return text;
  }

  function clockText() {
    const now = new Date();
    const date = fmt({ weekday: "short", day: "2-digit", month: "short", year: "numeric" }).format(now).replace(/,/g, "");
    const time = fmt({ hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(now);
    let zone = "";
    try {
      const part = fmt({ timeZoneName: "short" }).formatToParts(now).find((x) => x.type === "timeZoneName");
      zone = part ? ` ${part.value}` : "";
    } catch (_) {}
    return `${date} · ${time}${zone}`.toUpperCase();
  }

  function tickClock() {
    $("#clock").textContent = clockText();
  }

  // ---------------------------------------------------------------- formatting

  const PARTICLES = new Set(["van", "von", "der", "den", "de", "del", "della", "di", "da", "du", "la", "le", "ter", "ten", "dos", "das", "y", "bin", "al"]);

  function splitName(name) {
    let n = String(name || "").trim().replace(/\s+/g, " ");
    if (!n) return null;
    if (n.includes(",")) {
      const [last, first] = n.split(",", 2).map((s) => s.trim());
      n = first ? `${first} ${last}` : last;
    }
    const words = n.split(" ");
    if (words.length === 1) return { given: [], surname: words[0] };
    let i = words.length - 1;
    while (i > 1 && PARTICLES.has(words[i - 1].toLowerCase())) i -= 1;
    return { given: words.slice(0, i), surname: words.slice(i).join(" ") };
  }

  function abbreviateAuthor(name) {
    const s = splitName(name);
    if (!s) return "";
    const given = s.given.map((w) =>
      w
        .split("-")
        .filter(Boolean)
        .map((x) => x.charAt(0).toUpperCase() + ".")
        .join("-")
    );
    return [...given, s.surname].join(" ");
  }

  function authorLine(authors) {
    const list = (authors || []).filter(Boolean);
    if (!list.length) return "";
    if (list.length <= 3) return list.map(abbreviateAuthor).join(", ");
    return `${list.slice(0, 2).map(abbreviateAuthor).join(", ")} … +${list.length - 2}`;
  }

  function shortAuthors(authors) {
    const list = (authors || []).filter(Boolean);
    if (!list.length) return "";
    const s = splitName(list[0]);
    return list.length > 1 ? `${s.surname} et al` : s.surname;
  }

  function lengthText(seconds) {
    const mins = Math.max(1, Math.round(Number(seconds) / 60));
    const h = Math.floor(mins / 60);
    const m = mins % 60;
    if (!h) return `${mins} MIN`;
    return m ? `${h} H ${m} MIN` : `${h} H`;
  }

  function clockPos(seconds) {
    const s = Math.floor(seconds);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = String(s % 60).padStart(2, "0");
    return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
  }

  function fmtBytes(n) {
    if (!n) return "0 MB";
    if (n < 1e6) return `${Math.max(1, Math.round(n / 1e3))} KB`;
    if (n < 1e9) return `${n < 1e7 ? (n / 1e6).toFixed(1) : Math.round(n / 1e6)} MB`;
    return `${(n / 1e9).toFixed(1)} GB`;
  }

  function bar(n, d, cells) {
    const c = cells || 10;
    const filled = d > 0 ? Math.max(0, Math.min(c, Math.round((n / d) * c))) : 0;
    return `[${"█".repeat(filled)}${"░".repeat(c - filled)}]`;
  }

  const DAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];

  function describeDays(days) {
    const idx = (days || [])
      .map((d) => DAY_ORDER.indexOf(String(d).slice(0, 3).toLowerCase()))
      .filter((i) => i >= 0)
      .sort((a, b) => a - b);
    if (!idx.length) return "NO DAYS";
    if (idx.length === 7) return "EVERY DAY";
    const runs = [];
    let start = idx[0];
    let prev = idx[0];
    for (const i of idx.slice(1).concat([null])) {
      if (i !== null && i === prev + 1) {
        prev = i;
        continue;
      }
      const a = DAY_ORDER[start].toUpperCase();
      const b = DAY_ORDER[prev].toUpperCase();
      runs.push(start === prev ? a : prev - start === 1 ? `${a}, ${b}` : `${a}–${b}`);
      if (i !== null) start = prev = i;
    }
    return runs.join(", ");
  }

  function describeSlots(sched) {
    if (!sched) return "";
    const parts = [`SLOTS ${describeDays(sched.days)} AT ${(sched.times || []).join(", ") || "NO TIMES"}`];
    if (sched.duration_minutes) parts.push(`${sched.duration_minutes} MIN`);
    if (sched.timezone) parts.push(String(sched.timezone).replace(/_/g, " ").toUpperCase());
    return parts.join(" · ");
  }

  function inICloud(dir) {
    return /com~apple~CloudDocs/.test(String(dir || ""));
  }

  // ---------------------------------------------------------------- items

  const YOUTUBE_RE = /^(?:https?:\/\/)?(?:(?:www|m|music)\.)?(?:youtube\.com|youtu\.be)(?:[/?#]|$)/i;

  const isVideo = (p) => p.kind === "video";
  const isRead = (p) => p.status === "read";
  const isMissed = (p) => !isRead(p) && Boolean(p.missed);

  function thumbUrl(p) {
    return /^[A-Za-z0-9_-]{11}$/.test(p.video_id || "") ? `https://i.ytimg.com/vi/${p.video_id}/mqdefault.jpg` : null;
  }

  function videoMinutes(p) {
    const sched = (state.status && state.status.schedule) || {};
    if (!isVideo(p) || !p.duration_seconds) return null;
    const mins = Math.max(5, Math.ceil(p.duration_seconds / 300) * 5);
    return Math.min(mins, sched.max_session_minutes || 90);
  }

  function openHref(p) {
    if (isVideo(p)) return safeUrl(p.url);
    if (p.has_pdf) return `/api/papers/${encodeURIComponent(p.id)}/pdf`;
    return safeUrl(p.url) || safeUrl(p.pdf_url);
  }

  function pdfHref(p) {
    if (isVideo(p)) return null;
    return p.has_pdf ? `/api/papers/${encodeURIComponent(p.id)}/pdf` : safeUrl(p.pdf_url);
  }

  function pdfState(p) {
    const st = state.status || {};
    const pdf = st.pdf || {};
    if (p.has_pdf) return { tick: "PDF [OK]", ok: true, sub: inICloud(pdf.dir) ? "ICLOUD" : "CACHED" };
    if (safeUrl(p.pdf_url)) return { tick: "PDF [ - ]", ok: false, sub: pdf.enabled === false ? "ONLINE" : "NOT CACHED" };
    return { tick: "PDF [ - ]", ok: false, sub: "NO PDF LINK" };
  }

  function alertMinutes() {
    const cal = (state.status && state.status.calendar) || {};
    if (!cal.backend || cal.backend === "off" || cal.backend === "none") return null;
    return typeof cal.alert_minutes === "number" ? cal.alert_minutes : null;
  }

  // Playback position per item, so a long video resumes where it stopped (even on another visit).
  function loadPos(id) {
    return Number(store(`pq.pos.${id}`)) || 0;
  }

  function savePos(id, seconds) {
    store(`pq.pos.${id}`, seconds > 0 ? String(Math.floor(seconds)) : null);
  }

  function player(p) {
    const tag = p.media_mode === "video" ? "video" : "audio";
    const key = `${p.id}:${p.media_mode}`;
    const kept = state.players.get(key);
    if (kept) return kept;
    const node = el(tag, {
      class: `media-player media-${tag}`,
      controls: true,
      preload: "metadata",
      playsinline: tag === "video",
      poster: tag === "video" ? thumbUrl(p) : null,
      src: `/api/papers/${encodeURIComponent(p.id)}/media`,
      dataset: { key },
      "aria-label": `${tag === "audio" ? "Listen to" : "Watch"} ${p.title}`,
    });
    node.addEventListener(
      "loadedmetadata",
      () => {
        const at = loadPos(p.id);
        if (at > 0 && (!node.duration || at < node.duration - 5)) node.currentTime = at;
      },
      { once: true }
    );
    let saved = 0;
    node.addEventListener("timeupdate", () => {
      if (Math.abs(node.currentTime - saved) >= 5) {
        saved = node.currentTime;
        savePos(p.id, saved);
      }
    });
    node.addEventListener("pause", () => savePos(p.id, node.currentTime));
    node.addEventListener("ended", () => savePos(p.id, 0));
    state.players.set(key, node);
    return node;
  }

  // The media states of a YouTube item, as in the design's state table.
  function mediaState(p) {
    if (p.media_mode === "link") return { key: "LINK", parts: [["▶ OPENS ON YOUTUBE", ""], [" · NOTHING STORED", "dim"]] };
    if (p.media_status === "downloading") return { key: "DOWNLOADING", spin: true, parts: [["DOWNLOADING", ""]] };
    if (p.media_status === "done" && p.has_media) {
      const at = loadPos(p.id);
      const parts = [["[▶]", "ink"], [at ? ` PLAY · RESUME ${clockPos(at)}` : " PLAY", ""]];
      if (p.media_bytes) parts.push([` · ${fmtBytes(p.media_bytes)}`, "dim"]);
      return { key: "SAVED", parts };
    }
    if (p.media_status === "done") return { key: "SAVED", parts: [["[ ! ]", "warn"], [" SAVED, BUT NOT ON THIS MAC", ""]] };
    if (p.media_status === "failed") {
      return { key: "FAILED", failed: true, parts: [["[ ! ] ", "warn"], [(p.media_error || "download failed").replace(/_/g, " ").toUpperCase(), "warn"]], fix: p.media_fix };
    }
    return { key: "QUEUED", parts: [["[ - ]", "ink"], [" WAITING FOR WORKER", ""]] };
  }

  function mediaStateNode(p) {
    const m = mediaState(p);
    const line = el("div", { class: "mstate" });
    const main = el("span");
    if (m.spin) main.append(spinner(), " ");
    for (const [text, cls] of m.parts) main.append(el("span", { class: cls || null, text }));
    line.append(main);
    if (m.fix) line.append(el("span", { class: "mstate-sub", text: m.fix }));
    return line;
  }

  // ---------------------------------------------------------------- data

  async function fetchAll(filtered) {
    const params = new URLSearchParams({ view: "all" });
    if (filtered && state.q) params.set("q", state.q);
    if (filtered && state.tag) params.set("tag", state.tag);
    const data = await api(`/api/papers?${params}`);
    return (data.papers || []).filter((p) => !state.hiding.has(p.id));
  }

  async function load() {
    if (state.loading) {
      state.reloadQueued = true;
      return;
    }
    state.loading = true;
    const list = $("#list");
    list.setAttribute("aria-busy", "true");
    try {
      const filtered = Boolean(state.q || state.tag);
      const [items, all] = await Promise.all([fetchAll(true), filtered ? fetchAll(false) : null]);
      state.items = items;
      state.all = all || items;
      render();
      renderCounts();
      renderMeters();
    } catch (err) {
      if (err.status !== 401) showError(err);
    } finally {
      list.setAttribute("aria-busy", "false");
      state.loading = false;
      if (state.reloadQueued) {
        state.reloadQueued = false;
        load();
      }
    }
  }

  function byStart(a, b) {
    return new Date(startIso(a)) - new Date(startIso(b));
  }

  function groupByDay(papers, isoOf, prefix) {
    const groups = new Map();
    for (const p of papers) {
      const parts = zonedParts(isoOf(p));
      const key = parts ? parts.day : "unknown";
      if (!groups.has(key)) groups.set(key, { key: `${prefix}${key}`, day: key, papers: [] });
      groups.get(key).papers.push(p);
    }
    return Array.from(groups.values());
  }

  function groupsFor(view, items) {
    const open = items.filter((p) => !isRead(p));
    if (view === "upcoming") {
      const missed = open.filter(isMissed).sort(byStart);
      const upcoming = open.filter((p) => p.scheduled && !isMissed(p)).sort(byStart);
      const groups = [];
      if (missed.length) groups.push({ key: "missed", label: `MISSED · ${missed.length}`, missed: true, papers: missed });
      return groups.concat(groupByDay(upcoming, startIso, "d"));
    }
    if (view === "queue") {
      const queued = open
        .filter((p) => !p.scheduled)
        .sort((a, b) => a.priority - b.priority || String(a.added_at).localeCompare(String(b.added_at)));
      const names = { 1: "HIGH PRIORITY", 2: "NORMAL", 3: "LOW PRIORITY" };
      const out = [];
      for (const pr of [1, 2, 3]) {
        const ps = queued.filter((p) => Number(p.priority) === pr);
        if (ps.length) out.push({ key: `p${pr}`, label: names[pr], papers: ps });
      }
      const other = queued.filter((p) => ![1, 2, 3].includes(Number(p.priority)));
      if (other.length) out.push({ key: "other", label: "OTHER", papers: other });
      return out;
    }
    if (view === "media") {
      const vids = open.filter(isVideo);
      const sched = vids.filter((p) => p.scheduled).sort(byStart);
      const rest = vids.filter((p) => !p.scheduled);
      const out = [];
      if (sched.length) out.push({ key: "ms", label: "SCHEDULED", papers: sched });
      if (rest.length) out.push({ key: "mq", label: "NOT SCHEDULED", papers: rest });
      return out;
    }
    const read = items.filter(isRead).sort((a, b) => String(b.read_at || "").localeCompare(String(a.read_at || "")));
    return groupByDay(read, (p) => p.read_at_local || p.read_at, "r");
  }

  // ---------------------------------------------------------------- list rendering

  function dayHead(g) {
    if (g.label) {
      return el("h2", { class: `day${g.missed ? " day-missed" : ""}`, text: g.label });
    }
    if (g.day === "unknown") return el("h2", { class: "day", text: "SCHEDULED" });
    const rel = relDay(g.day);
    const date = plainDay(g.day);
    if (rel === "TODAY") return el("h2", { class: "day" }, ["TODAY", el("span", { class: "wide", text: ` · ${date}` })]);
    if (rel) return el("h2", { class: "day" }, [el("span", { class: "wide", text: `${rel} · ` }), date]);
    return el("h2", { class: "day", text: date });
  }

  function render() {
    const list = $("#list");
    const groups = groupsFor(state.view, state.items);
    state.order = groups.flatMap((g) => g.papers.map((p) => p.id));
    if (!state.order.includes(state.selId)) state.selId = state.order.length ? state.order[0] : null;
    if (state.removeAsk !== null && !state.order.includes(state.removeAsk)) state.removeAsk = null;
    renderViewbar();
    const frag = document.createDocumentFragment();
    if (!groups.length) frag.append(emptyState());
    for (const g of groups) {
      const section = el("section", { class: "group", "aria-label": g.label || plainDayLabel(g) });
      section.append(dayHead(g));
      for (const p of g.papers) section.append(row(p, g));
      if (g.missed) {
        section.append(
          el("div", { class: "missed-actions" }, [
            el("button", { class: "key", type: "button", onclick: (e) => autoSchedule(true, e.currentTarget) }, [
              el("b", { text: "[M]" }),
              "RESCHEDULE MISSED → NEXT FREE SLOTS",
            ]),
          ])
        );
      }
      frag.append(section);
    }
    list.replaceChildren(frag);
    renderDetail();
  }

  function plainDayLabel(g) {
    return g.day && g.day !== "unknown" ? relDay(g.day) || plainDay(g.day) : "Items";
  }

  function emptyState() {
    const filtered = state.q || state.tag;
    const copy = {
      upcoming: ["NOTHING SCHEDULED", "Add a paper above, or open the queue and auto-schedule it into your reading slots."],
      queue: ["QUEUE EMPTY", "Paste a paper or YouTube link above, use the bookmarklet, or ask an agent to queue something."],
      media: ["NO VIDEOS", "Paste a YouTube link above to keep a talk as a link, or as audio or video for offline use."],
      read: ["NOTHING READ YET", "Items you mark as read or watched collect here."],
    }[state.view];
    const [title, text] = filtered ? ["NO MATCHES", "Try a different search, or clear the tag filter."] : copy;
    return el("div", { class: "empty" }, [
      el("pre", { "aria-hidden": "true", text: "┏━━      ━━┓\n┃   ┏━━┓   ┃\n┃   ┃  ┃   ┃\n┃   ┗╲╱┛   ┃\n┃          ┃\n┗━━      ━━┛" }),
      el("p", { class: "empty-title", text: title }),
      el("p", { class: "empty-text", text }),
    ]);
  }

  function whenCol(p, g) {
    const col = el("div", { class: "when" });
    if (isRead(p)) {
      const parts = p.read_at ? zonedParts(p.read_at) : null;
      col.append(el("div", { class: "when-time", text: parts ? parts.time : "--:--" }));
      col.append(el("div", { class: "when-sub", text: isVideo(p) ? "WATCHED" : "READ" }));
      return col;
    }
    const iso = startIso(p);
    if (!iso) {
      col.append(el("div", { class: "when-time dim", text: "--:--" }));
      const mins = p.duration_minutes || videoMinutes(p);
      col.append(el("div", { class: "when-sub", text: mins ? `${mins} MIN` : "QUEUED" }));
      return col;
    }
    const parts = zonedParts(iso);
    if (isMissed(p)) col.classList.add("when-missed");
    col.append(el("div", { class: "when-time", text: parts.time }));
    const sameDay = g && g.day === parts.day;
    const sub = sameDay ? (p.duration_minutes ? `${p.duration_minutes} MIN` : "") : plainDay(parts.day).replace(/^\w+ /, "");
    col.append(el("div", { class: "when-sub", text: sub }));
    return col;
  }

  function byline(p) {
    const line = el("div", { class: "byline" });
    if (isVideo(p)) {
      const len = p.duration_seconds ? `▶ ${lengthText(p.duration_seconds)}` : "▶ VIDEO";
      const channel = (p.authors || [])[0];
      line.append(el("span", { class: "wide", text: [len, channel, "YOUTUBE"].filter(Boolean).join(" · ") }));
      line.append(el("span", { class: "narrow", text: [len, channel, p.media_mode === "link" ? null : p.media_mode].filter(Boolean).join(" · ") }));
      return line;
    }
    const authors = authorLine(p.authors);
    const wide = [authors, p.venue, p.year].filter(Boolean).join(" · ");
    if (wide) line.append(el("span", { class: "wide", text: wide, title: (p.authors || []).join(", ") || null }));
    const short = [shortAuthors(p.authors), [p.venue, p.year].filter(Boolean).join(" ")].filter(Boolean).join(" · ");
    const pdf = pdfState(p);
    const narrow = el("span", { class: "narrow" }, [short]);
    if (pdf.ok) narrow.append(short ? " · " : "", el("span", { class: "ink", text: pdf.tick }));
    line.append(narrow);
    return line.childNodes.length ? line : null;
  }

  function row(p, g) {
    const on = p.id === state.selId;
    const article = el("article", {
      class: `row${on ? " is-on" : ""}${isRead(p) ? " is-read" : ""}`,
      dataset: { id: p.id },
      "aria-labelledby": `t-${p.id}`,
      "aria-current": on ? "true" : null,
    });
    article.addEventListener("click", (e) => {
      if (e.target.closest("a, button, summary, audio, video, input, select")) return;
      select(p.id, { scroll: false });
    });
    article.addEventListener("focusin", () => {
      if (state.selId !== p.id) select(p.id, { scroll: false });
    });

    const main = el("div", { class: "row-main" });
    const href = openHref(p);
    const title = p.title || "Untitled";
    main.append(
      el("h3", { class: "row-title-wrap", id: `t-${p.id}` }, [
        href
          ? el("a", { class: "row-title", href, target: "_blank", rel: "noopener noreferrer", text: title })
          : el("span", { class: "row-title", text: title }),
      ])
    );
    const by = byline(p);
    if (by) main.append(by);

    const chips = [];
    for (const t of p.tags || []) {
      chips.push(el("button", { class: "chip", type: "button", text: t, "aria-label": `Filter by tag ${t}`, onclick: () => setTag(t) }));
    }
    if (Number(p.priority) === 1) chips.push(el("span", { class: "chip chip-high", text: "HIGH" }));
    if (Number(p.priority) === 3) chips.push(el("span", { class: "chip", text: "LOW" }));
    if (chips.length) main.append(el("div", { class: "chips" }, chips));

    if (isVideo(p) && !isRead(p) && (p.media_status === "downloading" || p.media_status === "failed" || p.media_status === "pending")) {
      main.append(mediaStateNode(p));
    }
    if (p.needs_metadata) main.append(el("p", { class: "flag", text: "[ ! ] DETAILS INCOMPLETE · LOOKUP WILL RETRY" }));
    main.append(el("div", { class: "row-inline" }));

    const meta = el("div", { class: "row-meta" });
    if (isVideo(p)) {
      meta.append(el("span", { class: "chip chip-mode", text: p.media_mode }));
      if (isRead(p)) meta.append(el("span", { text: "WATCHED" }));
      else if (p.media_mode === "link") {
        const a = alertMinutes();
        meta.append(el("span", { text: p.scheduled && a !== null ? `ALERT −${a} MIN` : "YOUTUBE" }));
      } else {
        const m = mediaState(p);
        meta.append(el("span", { class: m.failed ? "warn" : null, text: m.key === "SAVED" && p.media_bytes ? fmtBytes(p.media_bytes) : m.key }));
      }
    } else {
      const pdf = pdfState(p);
      meta.append(el("span", { class: pdf.ok ? "ink" : null, text: pdf.tick }), el("span", { text: pdf.sub }));
    }

    article.append(whenCol(p, g), el("div", { class: "cursor", "aria-hidden": "true", text: on ? ">" : "·" }), main, meta);
    return article;
  }

  function renderViewbar() {
    const bar = $("#viewbar");
    const children = [];
    if (state.view === "queue") {
      children.push(el("span", { class: "viewbar-note", text: describeSlots(state.status && state.status.schedule) }));
      const has = state.items.some((p) => !isRead(p) && !p.scheduled);
      children.push(
        el("button", {
          class: "key",
          type: "button",
          text: "AUTO-SCHEDULE",
          disabled: !has,
          title: "Place queued items into free reading slots, by priority",
          onclick: (e) => autoSchedule(false, e.currentTarget),
        })
      );
    }
    bar.replaceChildren(...children);
    bar.hidden = !children.length;
  }

  // ---------------------------------------------------------------- selection + detail

  function select(id, opts) {
    if (id === null || id === undefined) return;
    const prev = state.selId;
    state.selId = id;
    if (prev !== id) state.removeAsk = null;
    for (const r of $$(".row")) {
      const on = Number(r.dataset.id) === id;
      r.classList.toggle("is-on", on);
      if (on) r.setAttribute("aria-current", "true");
      else r.removeAttribute("aria-current");
      const c = $(".cursor", r);
      if (c) c.textContent = on ? ">" : "·";
    }
    renderDetail();
    if (opts && opts.scroll) {
      const r = $(`.row[data-id="${id}"]`);
      if (r) r.scrollIntoView({ block: "nearest" });
    }
  }

  function move(delta) {
    if (!state.order.length) return;
    let i = state.order.indexOf(state.selId);
    i = i < 0 ? 0 : Math.max(0, Math.min(state.order.length - 1, i + delta));
    select(state.order[i], { scroll: true });
  }

  function keyButton(key, label, onclick, extra) {
    const o = extra || {};
    return el(
      "button",
      {
        class: `key${o.cls ? ` ${o.cls}` : ""}`,
        type: "button",
        onclick,
        "aria-pressed": o.pressed === undefined ? null : String(o.pressed),
        "aria-keyshortcuts": key ? key.toLowerCase() : null,
        title: o.title || null,
      },
      [key ? el("b", { text: `[${key}]` }) : null, label]
    );
  }

  function keyLink(label, href, title) {
    return el("a", { class: "key", href, target: "_blank", rel: "noopener noreferrer", title: title || null }, [label]);
  }

  function detailFor(p) {
    const frag = el("div", { class: "detail" });
    const parts = [];
    const thumb = thumbUrl(p);
    const playable = p.has_media && !isRead(p);

    if (isVideo(p)) {
      if (playable && p.media_mode === "video") {
        parts.push(player(p));
      } else {
        const box = el("div", { class: "thumb" });
        if (thumb) box.append(el("img", { src: thumb, alt: "", width: "320", height: "180", loading: "lazy" }));
        else box.append(playIcon(56));
        box.append(el("span", { class: "thumb-cap", text: `YOUTUBE · ${p.video_id || ""}` }));
        parts.push(box);
      }
    }

    const href = openHref(p);
    parts.push(
      el("div", { class: "detail-title" }, [
        href ? el("a", { href, target: "_blank", rel: "noopener noreferrer", text: p.title || "Untitled" }) : p.title || "Untitled",
      ])
    );

    const dl = el("dl", { class: "dl dl-wide" });
    const add = (k, v, cls) => dl.append(el("dt", { text: k }), el("dd", { class: cls || null }, [v]));
    if (isRead(p)) {
      add(isVideo(p) ? "WATCHED" : "READ", p.read_at ? whenText(p.read_at) : "YES");
    } else {
      const session = sessionText(p);
      add("SESSION", session ? `${isMissed(p) ? "MISSED · " : ""}${session}` : "NOT SCHEDULED", isMissed(p) ? "warn" : null);
      const cal = (state.status && state.status.calendar) || {};
      const a = alertMinutes();
      if (a === null) add("CALENDAR", "OFF");
      else add("CALENDAR", p.scheduled ? `${cal.name || "CALENDAR"}${a ? ` · ALERT −${a}` : ""}` : "NO EVENT YET");
    }
    if (isVideo(p)) {
      add("MODE", { link: "LINK ONLY", audio: "AUDIO", video: "VIDEO" }[p.media_mode] || p.media_mode);
    } else {
      const pdf = pdfState(p);
      add("PDF", pdf.ok ? `CACHED · ${pdf.sub}` : pdf.sub);
    }
    add("PRIORITY", p.priority_name || { 1: "high", 2: "normal", 3: "low" }[p.priority] || "normal");
    if (p.duration_seconds) add("LENGTH", lengthText(p.duration_seconds));
    parts.push(dl);

    if (isVideo(p) && !isRead(p)) {
      parts.push(mediaStateNode(p));
      if (playable && p.media_mode === "audio") parts.push(player(p));
      parts.push(
        el(
          "div",
          { class: "seg", role: "group", "aria-label": "Keep the video as" },
          ["link", "audio", "video"].map((mode) =>
            el("button", {
              type: "button",
              text: mode.toUpperCase(),
              "aria-pressed": String(p.media_mode === mode),
              title: mode === "link" ? "Keep only the YouTube link" : `Download the ${mode} for offline use`,
              onclick: (e) => setMedia(p, mode, e.currentTarget),
            })
          )
        )
      );
    }

    if (state.removeAsk === p.id) {
      parts.push(
        el("div", { class: "confirm", role: "group", "aria-label": "Remove this item?" }, [
          el("span", { text: "REMOVE THIS ITEM?" }),
          keyButton("Y", "YES, REMOVE", () => removePaper(p), { cls: "key-danger" }),
          keyButton("N", "KEEP", cancelRemove),
        ])
      );
    } else {
      const acts = el("div", { class: "actions" });
      if (isVideo(p) && playable) {
        acts.append(keyButton("O", "PLAY", () => openItem(p), { cls: "key-open" }));
      } else if (href) {
        const b = keyButton("O", isVideo(p) && phoneQuery.matches ? "WATCH" : "OPEN", () => openItem(p), { cls: "key-open" });
        acts.append(b);
      }
      if (isVideo(p) && !isRead(p)) {
        acts.append(keyButton("A", "AUDIO", () => toggleAudio(p), { pressed: p.media_mode === "audio", title: "Download the audio, or go back to the link only" }));
      }
      if (!isRead(p)) acts.append(keyButton("R", p.scheduled ? "RESCHEDULE" : "SCHEDULE", () => openSchedule(p)));
      if (isRead(p)) acts.append(keyButton(isVideo(p) ? "W" : "D", "BACK TO QUEUE", (e) => markRead(p, false, e.currentTarget)));
      else if (isVideo(p)) acts.append(keyButton("W", "WATCHED", (e) => markRead(p, true, e.currentTarget)));
      else acts.append(keyButton("D", "DONE", (e) => markRead(p, true, e.currentTarget)));
      if (qrTargets(p).length) acts.append(keyButton("Q", "PHONE", () => openQr(p), { title: "QR code of this item's public link" }));
      const pdf = pdfHref(p);
      if (pdf && pdf !== href) acts.append(keyLink("PDF", pdf, p.has_pdf ? "Open the cached PDF" : "Open the PDF online"));
      const lib = safeUrl(p.library_link, ["zotero:", "http:", "https:"]);
      if (lib) acts.append(el("a", { class: "key", href: lib, rel: "noopener noreferrer", text: p.library === "mendeley" ? "MENDELEY" : "ZOTERO" }));
      acts.append(keyButton("X", "REMOVE", () => askRemove(p), { cls: "key-danger" }));
      parts.push(acts);
    }

    if (p.needs_metadata) parts.push(el("p", { class: "flag dl-wide", text: "[ ! ] DETAILS INCOMPLETE · LOOKUP WILL RETRY" }));
    if (p.abstract) {
      parts.push(el("details", { class: "abstract" }, [el("summary", { text: "ABSTRACT" }), el("p", { text: p.abstract })]));
    }
    frag.append(...parts);
    return frag;
  }

  function renderDetail() {
    const p = byId(state.selId);
    const box = $("#detail");
    for (const inline of $$(".row-inline")) inline.replaceChildren();
    const i = state.order.indexOf(state.selId);
    $("#detail-pos").textContent = p && i >= 0 ? `ITEM ${i + 1} / ${state.order.length}` : "";
    if (!p) {
      box.replaceChildren(el("p", { class: "detail-empty", text: "Nothing selected. Add something above, or press 1–4 to switch views." }));
      return;
    }
    const node = detailFor(p);
    // Something else still playing keeps playing: carry its player along under the selection.
    for (const [key, n] of state.players) {
      if (n.paused || key.startsWith(`${p.id}:`)) continue;
      const other = state.items.find((x) => `${x.id}:${x.media_mode}` === key);
      node.append(
        el("div", { class: "now-playing" }, [
          el("span", { class: "tool-label", text: `NOW PLAYING · ${other ? other.title : "MEDIA"}` }),
          n,
        ])
      );
    }
    if (phoneQuery.matches) {
      box.replaceChildren();
      const target = $(`.row[data-id="${p.id}"] .row-inline`);
      if (target) target.append(node);
    } else {
      box.replaceChildren(node);
    }
    // Drop players for items that are gone or switched mode, so they stop.
    for (const [key, n] of state.players) {
      const [id, mode] = key.split(":");
      const item = state.all.find((x) => String(x.id) === id) || state.items.find((x) => String(x.id) === id);
      if (!item || item.media_mode !== mode || !item.has_media || isRead(item)) {
        n.pause();
        state.players.delete(key);
      }
    }
  }

  function setPlaceholder() {
    addRef.placeholder = phoneQuery.matches ? "paste a link_" : "paste a paper or youtube link_";
  }
  phoneQuery.addEventListener("change", () => {
    setPlaceholder();
    renderDetail();
  });

  // ---------------------------------------------------------------- actions

  async function withBusy(target, fn) {
    const r = target && target.closest ? target.closest(".row") : null;
    if (target) target.disabled = true;
    if (r) r.classList.add("is-busy");
    try {
      return await fn();
    } catch (err) {
      showError(err);
      return null;
    } finally {
      if (target) target.disabled = false;
      if (r) r.classList.remove("is-busy");
    }
  }

  async function afterMutation(data) {
    showWarnings(data);
    await Promise.all([load(), refreshStatus()]);
  }

  function openItem(p) {
    const playable = p.has_media && !isRead(p) && isVideo(p);
    if (playable) {
      const n = player(p);
      if (n.isConnected) {
        n.focus();
        n.play().catch(() => {});
        return;
      }
    }
    const href = openHref(p);
    if (href) window.open(href, "_blank", "noopener,noreferrer");
  }

  async function markRead(p, read, button) {
    const data = await withBusy(button, () => api(`/api/papers/${p.id}/read`, { method: "POST", body: { read } }));
    if (!data) return;
    const verb = isVideo(p) ? "watched" : "read";
    toast(read ? `Marked ${verb}: ${p.title}` : `Back in the queue: ${p.title}`, {
      action: { label: "Undo", run: () => markRead(p, !read, null) },
    });
    await afterMutation(data);
  }

  function askRemove(p) {
    state.removeAsk = p.id;
    renderDetail();
    const yes = $(".confirm .key-danger");
    if (yes) yes.focus();
  }

  function cancelRemove() {
    state.removeAsk = null;
    renderDetail();
  }

  const pendingRemovals = new Map();

  function removePaper(p) {
    state.removeAsk = null;
    const i = state.order.indexOf(p.id);
    state.hiding.add(p.id);
    state.items = state.items.filter((x) => x.id !== p.id);
    state.selId = state.order[i + 1] || state.order[i - 1] || null;
    render();
    let undone = false;
    const commit = async () => {
      if (undone) return;
      pendingRemovals.delete(p.id);
      try {
        const data = await api(`/api/papers/${p.id}`, { method: "DELETE" });
        showWarnings(data);
      } catch (err) {
        showError(err);
      } finally {
        state.hiding.delete(p.id);
        await Promise.all([load(), refreshStatus()]);
      }
    };
    const timer = setTimeout(commit, 5000);
    pendingRemovals.set(p.id, () => {
      clearTimeout(timer);
      commit();
    });
    toast(`Removed: ${p.title}`, {
      timeout: 5000,
      action: {
        label: "Undo",
        run: () => {
          undone = true;
          clearTimeout(timer);
          pendingRemovals.delete(p.id);
          state.hiding.delete(p.id);
          state.selId = p.id;
          load();
        },
      },
    });
  }
  window.addEventListener("pagehide", () => {
    for (const flush of Array.from(pendingRemovals.values())) flush();
  });

  async function setMedia(p, mode, button) {
    if (p.media_mode === mode) return;
    const data = await withBusy(button, () => api(`/api/papers/${p.id}/media`, { method: "POST", body: { mode } }));
    if (!data) return;
    const failed = data.paper && data.paper.media_status === "failed";
    if (mode !== "link" && !failed) toast(`Downloading the ${mode} in the background.`);
    if (mode === "link") toast("Keeping the link only; any downloaded copy was deleted.");
    await afterMutation(data);
  }

  function toggleAudio(p) {
    if (!isVideo(p) || isRead(p)) return;
    setMedia(p, p.media_mode === "audio" ? "link" : "audio", null);
  }

  async function autoSchedule(includeMissed, button) {
    const data = await withBusy(button, () => api("/api/auto-schedule", { method: "POST", body: { include_missed: includeMissed } }));
    if (!data) return;
    const n = (data.papers || []).length;
    const u = (data.unplaced || []).length;
    let msg = n ? `Scheduled ${n} item${n === 1 ? "" : "s"}` : "Nothing to schedule";
    if (u) msg += `; ${u} didn’t fit in the scheduling horizon`;
    toast(msg + ".");
    await afterMutation(data);
  }

  // ---------------------------------------------------------------- add bar

  const addForm = $("#add-form");
  const addRef = $("#add-ref");

  function setAddMode(mode) {
    state.addMode = mode;
    for (const b of $$("#add-media button")) b.setAttribute("aria-pressed", String(b.dataset.mode === mode));
  }

  // The LINK / AUDIO / VIDEO control appears only while the add bar holds a YouTube link.
  function updateAddBar() {
    const value = addRef.value.trim();
    addForm.classList.toggle("has-text", Boolean(value));
    const field = $("#add-media-field");
    const show = YOUTUBE_RE.test(value);
    if (show && field.hidden) setAddMode((state.status && state.status.media && state.status.media.default_mode) || "link");
    field.hidden = !show;
  }

  async function addPaper(ev) {
    ev.preventDefault();
    const ref = addRef.value.trim();
    if (!ref) {
      addRef.focus();
      return;
    }
    const button = $("#add-submit");
    const buttons = $$('button[type="submit"]', addForm);
    for (const b of buttons) b.disabled = true;
    button.textContent = "ADDING…";
    const body = { ref, priority: Number($("#add-priority").value), when: $("#add-when").value, source: "web" };
    if (!$("#add-media-field").hidden) body.media = state.addMode;
    try {
      const data = await api("/api/papers", { method: "POST", body });
      addRef.value = "";
      updateAddBar();
      const p = data.paper;
      const iso = startIso(p);
      if (!data.created) toast(`Already in your list: ${p.title}`);
      else toast(iso ? `Added #${p.id} · scheduled ${whenText(iso)}${alertMinutes() !== null ? " · event in the calendar" : ""}.` : `Added #${p.id} to the queue: ${p.title}`);
      showWarnings(data);
      state.selId = p.id;
      if (data.created) setView(iso ? "upcoming" : p.kind === "video" ? "media" : "queue", { silent: true });
      await Promise.all([load(), refreshStatus()]);
    } catch (err) {
      showError(err);
    } finally {
      for (const b of buttons) b.disabled = false;
      button.textContent = "[ ENTER ] ADD";
    }
  }

  addForm.addEventListener("submit", addPaper);
  addRef.addEventListener("input", updateAddBar);
  for (const b of $$("#add-media button")) b.addEventListener("click", () => setAddMode(b.dataset.mode));

  // ---------------------------------------------------------------- schedule dialog

  const sDialog = $("#schedule-dialog");

  async function openSchedule(p) {
    if (!p || isRead(p)) return;
    state.dialogPaper = p;
    state.slotPick = null;
    $("#schedule-title").textContent = p.title || "Untitled";
    $("#schedule-kicker").textContent = p.scheduled ? "RESCHEDULE" : "SCHEDULE";
    const iso = startIso(p);
    $("#schedule-current").textContent = iso ? `Currently ${whenText(iso)}${isMissed(p) ? " (missed)" : ""}.` : "Not scheduled yet.";
    $("#schedule-clear").hidden = !iso;
    const at = $("#schedule-at");
    at.value = iso ? zonedParts(iso).local : "";
    at.min = zonedParts(new Date()).local;
    const defaultDur = state.status && state.status.schedule && state.status.schedule.duration_minutes;
    $("#schedule-duration").value = p.duration_minutes || videoMinutes(p) || defaultDur || 45;
    const box = $("#schedule-slots");
    box.replaceChildren(el("span", { class: "hint", text: "LOADING…" }));
    sDialog.showModal();
    try {
      const data = await api(`/api/slots?n=6&paper_id=${encodeURIComponent(p.id)}`);
      const slots = data.slots || [];
      box.replaceChildren(
        ...(slots.length
          ? slots.map((s) => {
              const parts = zonedParts(s.local || s.utc);
              const label = `${plainDay(parts.day).replace(/ \w+$/, "")} · ${parts.time}`;
              return el("button", {
                class: "slot",
                type: "button",
                text: label,
                "aria-pressed": "false",
                "aria-label": `Pick ${whenText(s.local || s.utc)}`,
                onclick: (e) => {
                  state.slotPick = { utc: s.utc || s.local, local: parts.local };
                  at.value = parts.local;
                  for (const b of $$(".slot", box)) b.setAttribute("aria-pressed", String(b === e.currentTarget));
                },
                ondblclick: () => doSchedule(s.utc || s.local),
              });
            })
          : [el("span", { class: "hint", text: "No free slots in the scheduling horizon." })])
      );
    } catch (_) {
      box.replaceChildren(el("span", { class: "hint", text: "Couldn’t load free slots." }));
    }
  }

  async function doSchedule(when) {
    const p = state.dialogPaper;
    if (!p) return;
    const durRaw = Number($("#schedule-duration").value);
    const body = { when };
    const defaultDur = (state.status && state.status.schedule && state.status.schedule.duration_minutes) || 0;
    if (durRaw > 0 && (durRaw !== defaultDur || p.duration_minutes)) body.duration_minutes = Math.round(durRaw);
    try {
      const data = await api(`/api/papers/${p.id}/schedule`, { method: "POST", body });
      sDialog.close();
      const iso = startIso(data.paper);
      toast(iso ? `Scheduled ${whenText(iso)}.` : "Scheduled.");
      await afterMutation(data);
    } catch (err) {
      showError(err);
    }
  }

  async function doUnschedule() {
    const p = state.dialogPaper;
    if (!p || !p.scheduled) return;
    try {
      const data = await api(`/api/papers/${p.id}/schedule`, { method: "DELETE" });
      sDialog.close();
      toast("Moved back to the queue.");
      await afterMutation(data);
    } catch (err) {
      showError(err);
    }
  }

  function saveSchedule() {
    const v = $("#schedule-at").value;
    if (!v) {
      $("#schedule-at").focus();
      toast("Pick a date and time, or choose a free slot.", { kind: "warn" });
      return;
    }
    doSchedule(state.slotPick && state.slotPick.local === v ? state.slotPick.utc : v);
  }

  $("#schedule-next").addEventListener("click", () => doSchedule("next"));
  $("#schedule-clear").addEventListener("click", doUnschedule);
  $("#schedule-save").addEventListener("click", saveSchedule);
  sDialog.addEventListener("close", () => {
    state.dialogPaper = null;
  });

  for (const d of $$("dialog")) {
    d.addEventListener("click", (e) => {
      if (e.target === d) d.close();
    });
  }

  // ---------------------------------------------------------------- per-item QR ("[Q] PHONE")

  const qDialog = $("#qr-dialog");
  let qrPaper = null;

  function qrTargets(p) {
    const out = [];
    if (safeUrl(p.url)) out.push("url");
    if (!isVideo(p) && safeUrl(p.pdf_url)) out.push("pdf");
    return out;
  }

  function showQr(target) {
    const p = qrPaper;
    if (!p) return;
    state.qrTarget = target;
    const link = target === "pdf" ? p.pdf_url : p.url;
    const img = $("#qr-img");
    img.src = `/api/papers/${encodeURIComponent(p.id)}/qr.svg?target=${target}`;
    img.alt = `QR code for ${link}`;
    $("#qr-link").textContent = link;
    for (const b of $$("#qr-target button")) b.setAttribute("aria-pressed", String(b.dataset.target === target));
  }

  function openQr(p) {
    const targets = qrTargets(p);
    if (!targets.length) {
      toast("This item has no public link to send.", { kind: "warn" });
      return;
    }
    qrPaper = p;
    $("#qr-title").textContent = p.title || "Untitled";
    const seg = $("#qr-target");
    seg.hidden = targets.length < 2;
    $('#qr-target [data-target="url"]').textContent = isVideo(p) ? "YOUTUBE" : "PAGE";
    showQr(targets[0]);
    qDialog.showModal();
  }

  for (const b of $$("#qr-target button")) b.addEventListener("click", () => showQr(b.dataset.target));
  $("#qr-print").addEventListener("click", () => window.print());
  qDialog.addEventListener("close", () => {
    qrPaper = null;
  });

  // ---------------------------------------------------------------- iPhone dialog

  const iDialog = $("#iphone-dialog");
  $("#iphone-open").addEventListener("click", () => {
    const st = state.status || {};
    const lan = st.lan || {};
    const on = Boolean(lan.enabled);
    const cal = st.calendar || {};
    $("#iphone-cal").textContent = alertMinutes() !== null && cal.name ? `“${cal.name}”` : "iCloud";
    $("#iphone-lan").hidden = !on;
    $("#iphone-off").hidden = on;
    if (on) {
      $("#iphone-qr").src = `/api/qr.svg?t=${Date.now()}`;
      $("#iphone-urls").textContent = (lan.urls || []).length ? `Address: ${lan.urls[0]} (the QR code adds the token).` : "";
    } else {
      $("#iphone-qr").removeAttribute("src");
    }
    iDialog.showModal();
  });

  // ---------------------------------------------------------------- status, meters, footer

  function libName(lib) {
    return lib.provider ? lib.provider.toUpperCase() : "LIBRARY";
  }

  function tickSpan(ok) {
    return el("span", { class: ok ? "ink" : "warn", text: ok ? "[OK]" : "[ ! ]" });
  }

  function renderCounts() {
    const all = state.all;
    const open = all.filter((p) => !isRead(p));
    const counts = {
      upcoming: open.filter((p) => p.scheduled).length,
      queue: open.filter((p) => !p.scheduled).length,
      media: open.filter(isVideo).length,
      read: all.filter(isRead).length,
    };
    for (const node of $$("[data-count]")) node.textContent = counts[node.dataset.count] ? String(counts[node.dataset.count]) : "";
    const names = { upcoming: "UPCOMING", queue: "QUEUED", media: "VIDEOS", read: "READ" };
    $("#phone-count").textContent = `${counts[state.view]} ${names[state.view]}`;
  }

  function weekKeys() {
    const today = dayKeyOffset(0);
    const dow = (keyDate(today).getUTCDay() + 6) % 7; // Monday = 0
    const monday = dayKeyOffset(-dow, today);
    return [monday, dayKeyOffset(6, monday)];
  }

  function meterRow(label, barText, value, opts) {
    const o = opts || {};
    return el("div", { class: "meter", title: o.title || null }, [
      el("span", { class: "meter-label", text: label }),
      el("span", { class: `meter-bar${o.low ? " is-low" : ""}`, text: barText, "aria-hidden": o.hideBar ? "true" : null }),
      el("span", { class: "meter-value", text: value }),
    ]);
  }

  // Every number here comes from the API; a meter without a real value is left out.
  function renderMeters() {
    const st = state.status;
    if (!st) return;
    const all = state.all;
    const rows = [];
    const foot = [];

    const [mon, sun] = weekKeys();
    const inWeek = (iso) => {
      const p = iso ? zonedParts(iso) : null;
      return Boolean(p && p.day >= mon && p.day <= sun);
    };
    const doneWeek = all.filter((p) => isRead(p) && inWeek(p.read_at)).length;
    const openWeek = all.filter((p) => !isRead(p) && p.scheduled && inWeek(startIso(p))).length;
    if (doneWeek + openWeek > 0) {
      rows.push(
        meterRow("THIS WEEK", bar(doneWeek, doneWeek + openWeek), `${doneWeek} / ${doneWeek + openWeek}`, {
          title: `${doneWeek} read or watched this week, of ${doneWeek + openWeek} (read this week plus sessions still open this week)`,
        })
      );
    }

    const pdf = st.pdf || {};
    const papers = all.filter((p) => !isRead(p) && !isVideo(p));
    const cached = papers.filter((p) => p.has_pdf).length;
    if (pdf.enabled !== false && papers.length) {
      rows.push(meterRow("PDFS CACHED", bar(cached, papers.length), `${cached} / ${papers.length}`, { title: `Cached PDFs for unread papers, in ${pdf.dir || "the PDF folder"}` }));
      foot.push(`PDF ${cached}/${papers.length}`);
    }

    const media = st.media || {};
    const icloud = inICloud(media.dir);
    if (typeof media.bytes === "number") {
      const files = media.files || 0;
      rows.push(
        meterRow(icloud ? "ICLOUD MEDIA" : "MEDIA ON DISK", `${files} FILE${files === 1 ? "" : "S"}`, fmtBytes(media.bytes), {
          low: true,
          title: `Downloaded audio and video in ${media.dir || "the media folder"}`,
        })
      );
    }
    $("#meters").replaceChildren(...rows);

    // footer + phone footer
    const lib = st.library || {};
    const cal = st.calendar || {};
    const footer = el("span");
    const dot = (ok, off) => el("span", { class: off ? "dim" : ok ? "ink" : "warn", text: off ? "○" : "●", "aria-hidden": "true" });
    const libOff = !lib.provider || lib.provider === "off";
    const calOff = alertMinutes() === null;
    footer.append("LIB ", dot(lib.ok, libOff), ` ${libOff ? "OFF" : libName(lib)}`, " · CAL ", dot(cal.ok, calOff), ` ${calOff ? "OFF" : cal.name || "ON"}`);
    for (const f of foot) footer.append(` · ${f}`);
    if (media.pending) footer.append(` · MEDIA ${media.pending} ↓`);
    $("#foot-status").replaceChildren(footer);

    const left = el("span");
    left.append("CAL ", calOff ? el("span", { class: "dim", text: "OFF" }) : tickSpan(cal.ok));
    for (const f of foot) left.append(` · ${f}`);
    $("#phone-foot-left").replaceChildren(left);
    $("#phone-foot-right").textContent = typeof media.bytes === "number" ? `${icloud ? "ICLOUD" : "MEDIA"} · ${fmtBytes(media.bytes)}` : "";
  }

  function renderStatus(st) {
    const v = st.server && st.server.version;
    $("#version").textContent = v ? `v${String(v).split(".").slice(0, 2).join(".")}` : "";

    const lib = st.library || {};
    const libNode = $("#library-status");
    const libBtn = $("#library-sync");
    if (!lib.provider || lib.provider === "off") {
      libNode.replaceChildren(el("span", { class: "dim", text: "OFF" }));
      libNode.title = "Set library.provider to zotero or mendeley";
      libBtn.hidden = true;
    } else {
      libNode.replaceChildren(`${libName(lib)} `, tickSpan(lib.ok));
      libNode.title = [lib.message, lib.last_sync ? `synced ${whenText(lib.last_sync)}` : ""].filter(Boolean).join(" · ");
      libBtn.hidden = false;
    }

    const cal = st.calendar || {};
    const calNode = $("#calendar-status");
    const calBtn = $("#calendar-sync");
    if (alertMinutes() === null) {
      calNode.replaceChildren(el("span", { class: "dim", text: "OFF" }));
      calNode.title = cal.message || "";
      calBtn.hidden = true;
    } else {
      calNode.replaceChildren(`${cal.name || "CALENDAR"} `, tickSpan(cal.ok));
      calNode.title = cal.message || "";
      calBtn.hidden = false;
    }

    const media = st.media || {};
    const notes = [];
    if (media.deno === false) notes.push(["[ ! ] NEED DENO", "warn"]);
    else if (media.ffmpeg === false) notes.push(["VIDEO NEEDS FFMPEG", "warn"]);
    if (media.pending) notes.push([`${media.pending} QUEUED`, ""]);
    if ((media.failed || []).length) notes.push([`${media.failed.length} FAILED`, "warn"]);
    const mediaNode = $("#media-status");
    mediaNode.replaceChildren(...notes.flatMap(([t, c], i) => [i ? " · " : "", el("span", { class: c || null, text: t })]));
    mediaNode.title = media.deno === false ? "brew install deno" : media.ffmpeg === false ? "brew install ffmpeg" : "";
    $("#media-line").hidden = !notes.length;

    const slot = state.nextSlot;
    $("#next-slot").textContent = slot === undefined ? "…" : slot ? `${plainDay(zonedParts(slot).day)} ${zonedParts(slot).time}` : "NONE IN HORIZON";
    renderMeters();
  }

  async function refreshSlot() {
    try {
      const data = await api("/api/slots?n=1");
      const s = (data.slots || [])[0];
      state.nextSlot = s ? s.local || s.utc : null;
    } catch (_) {
      state.nextSlot = undefined;
    }
    if (state.status) renderStatus(state.status);
  }

  async function refreshStatus() {
    try {
      const st = await api("/api/status");
      state.status = st;
      if (st.schedule && st.schedule.timezone && st.schedule.timezone !== state.tz) {
        try {
          new Intl.DateTimeFormat("en", { timeZone: st.schedule.timezone });
          state.tz = st.schedule.timezone;
        } catch (_) {
          state.tz = undefined;
        }
        tickClock();
      }
      const changed = state.rev !== null && st.rev !== state.rev;
      if (changed || state.rev === null) refreshSlot();
      state.rev = st.rev;
      renderStatus(st);
      if (state.view === "queue") renderViewbar();
      return changed;
    } catch (err) {
      if (err.status === 401) {
        $("#list").replaceChildren(
          el("div", { class: "empty" }, [
            el("p", { class: "empty-title", text: "[ ! ] NOT SIGNED IN" }),
            el("p", { class: "empty-text", text: "Open the link with ?token= that `paperqueue serve --lan` printed." }),
          ])
        );
      }
      return false;
    }
  }

  async function poll() {
    if (document.hidden) return;
    if (await refreshStatus()) load();
  }

  function setSyncing(kind, on) {
    state.syncing[kind] = on;
    const lib = state.status && state.status.library;
    const label = state.syncing.library ? `SYNCING ${lib && lib.provider ? lib.provider.toUpperCase() : "LIBRARY"}` : state.syncing.calendar ? "SYNCING CALENDAR" : "";
    $("#syncing").hidden = !label;
    $("#syncing-text").textContent = label;
  }

  async function syncAction(button, kind, path, okText) {
    if (state.syncing[kind]) return;
    setSyncing(kind, true);
    try {
      const data = await withBusy(button, () => api(path, { method: "POST", body: {} }));
      if (!data) return;
      if (typeof data.pulled === "number") toast(`${okText}: ${data.pulled} in library, ${data.created || 0} new${data.pushed ? `, ${data.pushed} pushed` : ""}.`);
      else toast(`${okText}.`);
      await afterMutation(data);
    } finally {
      setSyncing(kind, false);
    }
  }

  $("#library-sync").addEventListener("click", (e) => syncAction(e.currentTarget, "library", "/api/library/sync", "Library synced"));
  $("#calendar-sync").addEventListener("click", (e) => syncAction(e.currentTarget, "calendar", "/api/calendar/sync", "Calendar re-synced"));

  // ---------------------------------------------------------------- theme + scanlines

  function renderThemeControls() {
    const t = window.pqTheme;
    if (!t) return;
    for (const b of $$("#theme-seg button")) b.setAttribute("aria-pressed", String(b.dataset.themeChoice === t.get()));
    const on = t.scanlines();
    const sb = $("#scan-toggle");
    sb.setAttribute("aria-pressed", String(on));
    sb.textContent = on ? "[ ON ]" : "[ OFF ]";
  }

  for (const b of $$("#theme-seg button")) {
    b.addEventListener("click", () => {
      if (window.pqTheme) window.pqTheme.set(b.dataset.themeChoice);
      renderThemeControls();
    });
  }
  $("#scan-toggle").addEventListener("click", () => {
    if (window.pqTheme) window.pqTheme.setScanlines(!window.pqTheme.scanlines());
    renderThemeControls();
  });

  // ---------------------------------------------------------------- bookmarklet

  // Runs on the publisher's page, never on ours. Kept dependency-free and ES5-ish.
  function bookmarklet(origin) {
    var d = document;
    function meta(n) {
      var e = d.querySelector('meta[name="' + n + '"],meta[property="' + n + '"]');
      return e && e.content ? e.content.trim() : "";
    }
    function metas(n) {
      var out = [];
      var nodes = d.querySelectorAll('meta[name="' + n + '"]');
      for (var i = 0; i < nodes.length; i++) if (nodes[i].content) out.push(nodes[i].content.trim());
      return out;
    }
    var doi = meta("citation_doi") || meta("dc.identifier") || meta("DC.identifier") || meta("prism.doi");
    doi = doi.replace(/^(https?:\/\/(dx\.)?doi\.org\/|doi:)/i, "");
    var h = {
      url: location.href,
      title: meta("citation_title") || meta("dc.title") || meta("og:title") || d.title,
      authors: metas("citation_author").length ? metas("citation_author") : metas("dc.creator"),
      doi: /^10\./.test(doi) ? doi : "",
      pdf_url: meta("citation_pdf_url"),
      journal: meta("citation_journal_title") || meta("citation_conference_title") || meta("citation_publisher") || meta("og:site_name"),
      date: meta("citation_publication_date") || meta("citation_date") || meta("citation_online_date") || meta("dc.date"),
      abstract: (meta("citation_abstract") || meta("dc.description") || meta("og:description") || meta("description")).slice(0, 1500),
    };
    for (var k in h) if (!h[k] || h[k].length === 0) delete h[k];
    var b = btoa(unescape(encodeURIComponent(JSON.stringify(h)))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    window.open(origin + "/add?ref=" + encodeURIComponent(location.href) + "&hints=" + b, "_blank");
  }

  function setupBookmarklet() {
    const link = $("#bookmarklet");
    const src = `(${bookmarklet.toString()})(${JSON.stringify(location.origin)})`;
    link.setAttribute("href", "javascript:" + encodeURIComponent(src.replace(/\s*\n\s*/g, " ")));
    link.addEventListener("click", (e) => {
      e.preventDefault();
      toast("Drag SAVE TO PAPERQUEUE to your bookmarks bar, then click it on a paper’s page.");
    });
  }

  // ---------------------------------------------------------------- views, search, tags

  function setView(view, opts) {
    if (!VIEWS.includes(view)) return;
    if (view !== state.view) state.removeAsk = null;
    state.view = view;
    for (const t of $$(".tab")) t.setAttribute("aria-pressed", String(t.dataset.view === view));
    store("pq.view", view);
    if (!(opts && opts.silent)) {
      render();
      renderCounts();
    }
  }

  function setTag(tag) {
    state.tag = tag || "";
    const chip = $("#tag-clear");
    chip.hidden = !state.tag;
    chip.textContent = state.tag ? `TAG: ${state.tag} [×]` : "";
    chip.setAttribute("aria-label", state.tag ? `Clear the tag filter ${state.tag}` : "Clear the tag filter");
    load();
  }

  for (const t of $$(".tab")) t.addEventListener("click", () => setView(t.dataset.view));
  $("#tag-clear").addEventListener("click", () => setTag(""));

  let searchTimer = null;
  $("#search").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.q = e.target.value.trim();
      load();
    }, 220);
  });

  // ---------------------------------------------------------------- keyboard

  function dialogKeys(e, dialog) {
    const t = e.target;
    const typing = t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName);
    const k = e.key.toLowerCase();
    if (dialog === sDialog) {
      if (e.key === "Enter" && !(t && /^(BUTTON|A|SELECT)$/.test(t.tagName))) {
        e.preventDefault();
        saveSchedule();
      } else if (!typing && k === "n") {
        e.preventDefault();
        doSchedule("next");
      } else if (!typing && k === "u" && !$("#schedule-clear").hidden) {
        e.preventDefault();
        doUnschedule();
      }
    } else if (dialog === qDialog && !typing && k === "p") {
      e.preventDefault();
      window.print();
    }
  }

  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const t = e.target;
    const open = $("dialog[open]");
    if (open) {
      dialogKeys(e, open);
      return;
    }
    const typing = t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
    if (typing) {
      if (e.key === "Escape") {
        if (t.id === "search" && t.value) {
          t.value = "";
          state.q = "";
          load();
        } else t.blur();
      }
      return;
    }
    const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    const p = byId(state.selId);

    if (state.removeAsk !== null) {
      if (k === "y" || k === "x") {
        e.preventDefault();
        const target = byId(state.removeAsk);
        if (target) removePaper(target);
        return;
      }
      if (k === "n" || k === "Escape") {
        e.preventDefault();
        cancelRemove();
        return;
      }
    }

    const onControl = t && t.closest && t.closest("a, button, summary, audio, video");
    switch (k) {
      case "j":
      case "ArrowDown":
        e.preventDefault();
        move(1);
        break;
      case "k":
      case "ArrowUp":
        e.preventDefault();
        move(-1);
        break;
      case "Enter":
        if (onControl) return;
        if (p) {
          e.preventDefault();
          openItem(p);
        }
        break;
      case "o":
        if (p) openItem(p);
        break;
      case "a":
        if (p && isVideo(p)) toggleAudio(p);
        break;
      case "r":
        if (p && !isRead(p)) openSchedule(p);
        break;
      case "w":
      case "d":
        if (p) markRead(p, !isRead(p), null);
        break;
      case "x":
        if (p) askRemove(p);
        break;
      case "q":
        if (p) openQr(p);
        break;
      case "m":
        if (state.items.some(isMissed)) autoSchedule(true, null);
        break;
      case "/":
        e.preventDefault();
        $("#search").focus();
        break;
      case "n":
        e.preventDefault();
        addRef.focus();
        break;
      case "1":
      case "2":
      case "3":
      case "4":
        setView(VIEWS[Number(k) - 1]);
        break;
      default:
        return;
    }
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) poll();
  });

  // ---------------------------------------------------------------- start

  function boot() {
    $("#list").replaceChildren(
      el("pre", { class: "boot" }, ["PAPERQUEUE ··········· ", el("span", { class: "ink", text: "LOADING" }), "\n> ", el("span", { class: "ink", text: "█" })])
    );
  }

  (async function start() {
    setupBookmarklet();
    setPlaceholder();
    renderThemeControls();
    const saved = store("pq.view");
    if (saved && VIEWS.includes(saved)) setView(saved, { silent: true });
    boot();
    tickClock();
    setInterval(tickClock, 15000);
    await refreshStatus();
    await load();
    setInterval(poll, 5000);
  })();
})();
