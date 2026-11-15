/* paperqueue web app. Vanilla JS, no build step. DOM is built with createElement/textContent only. */
"use strict";

(function () {
  // ---------------------------------------------------------------- utilities

  const $ = (sel, root) => (root || document).querySelector(sel);

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

  function safeUrl(url, schemes) {
    if (!url || typeof url !== "string") return null;
    try {
      const u = new URL(url, location.origin);
      return (schemes || ["http:", "https:"]).includes(u.protocol) ? u.href : null;
    } catch (_) {
      return null;
    }
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

  // ---------------------------------------------------------------- toasts

  const toasts = $("#toasts");

  function toast(message, opts) {
    const o = opts || {};
    const kind = o.kind || "info";
    const node = el("div", { class: `toast toast-${kind}` }, [el("span", { class: "toast-text", text: message })]);
    let timer = null;
    const close = () => {
      if (!node.isConnected) return;
      clearTimeout(timer);
      node.classList.add("is-leaving");
      setTimeout(() => node.remove(), 220);
    };
    if (o.action) {
      node.append(
        el("button", {
          type: "button",
          text: o.action.label,
          onclick: () => {
            close();
            o.action.run();
          },
        })
      );
    }
    node.append(el("button", { type: "button", text: "Dismiss", "aria-label": "Dismiss", onclick: close }));
    toasts.append(node);
    while (toasts.children.length > 4) toasts.firstElementChild.remove();
    timer = setTimeout(close, o.timeout || (kind === "info" ? 4000 : 8000));
    return { close, node };
  }

  function showWarnings(data) {
    for (const w of (data && data.warnings) || []) toast(w, { kind: "warn" });
  }

  function showError(err) {
    toast(err && err.message ? err.message : String(err), { kind: "error" });
  }

  // ---------------------------------------------------------------- time

  const state = {
    view: "upcoming",
    q: "",
    tag: "",
    rev: null,
    status: null,
    tz: undefined,
    loading: false,
    reloadQueued: false,
    hiding: new Set(), // ids pending removal (undo window)
    dialogPaper: null,
    players: new Map(), // audio/video elements kept across re-renders, so playback continues
  };

  function tzName() {
    return state.tz;
  }

  function startIso(p) {
    return (
      p.scheduled_start_local ||
      p.scheduled_local ||
      (p.local && p.local.scheduled_start) ||
      p.scheduled_start_utc ||
      p.scheduled_start ||
      null
    );
  }

  function zonedParts(iso) {
    const d = iso instanceof Date ? iso : new Date(iso);
    if (isNaN(d)) return null;
    const opts = {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    };
    let fmt;
    try {
      fmt = new Intl.DateTimeFormat("en-CA", Object.assign({ timeZone: tzName() }, opts));
    } catch (_) {
      fmt = new Intl.DateTimeFormat("en-CA", opts);
    }
    const parts = {};
    for (const part of fmt.formatToParts(d)) parts[part.type] = part.value;
    const hour = parts.hour === "24" ? "00" : parts.hour;
    return {
      day: `${parts.year}-${parts.month}-${parts.day}`,
      time: `${hour}:${parts.minute}`,
      local: `${parts.year}-${parts.month}-${parts.day}T${hour}:${parts.minute}`,
    };
  }

  function dayKeyOffset(offsetDays) {
    const p = zonedParts(new Date());
    const [y, m, d] = p.day.split("-").map(Number);
    const t = new Date(Date.UTC(y, m - 1, d + offsetDays));
    return t.toISOString().slice(0, 10);
  }

  const dayFmt = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });
  const dayFmtYear = new Intl.DateTimeFormat("en-GB", {
    weekday: "short",
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });

  function plainDay(dayKey) {
    const [y, m, d] = dayKey.split("-").map(Number);
    const date = new Date(Date.UTC(y, m - 1, d));
    const thisYear = Number(dayKeyOffset(0).slice(0, 4));
    return (y === thisYear ? dayFmt : dayFmtYear).format(date).replace(",", "");
  }

  function dayLabel(dayKey) {
    if (dayKey === dayKeyOffset(0)) return "Today";
    if (dayKey === dayKeyOffset(1)) return "Tomorrow";
    if (dayKey === dayKeyOffset(-1)) return "Yesterday";
    return plainDay(dayKey);
  }

  function whenText(iso) {
    const p = zonedParts(iso);
    if (!p) return "";
    return `${dayLabel(p.day)}, ${p.time}`;
  }

  // ---------------------------------------------------------------- formatting

  const PARTICLES = new Set(["van", "von", "der", "den", "de", "del", "della", "di", "da", "du", "la", "le", "ter", "ten", "dos", "das", "y", "bin", "al"]);

  function initial(word) {
    return word
      .split("-")
      .filter(Boolean)
      .map((w) => w.charAt(0).toUpperCase() + ".")
      .join("-");
  }

  function abbreviateAuthor(name) {
    let n = String(name || "").trim().replace(/\s+/g, " ");
    if (!n) return "";
    if (n.includes(",")) {
      const [last, first] = n.split(",", 2).map((s) => s.trim());
      n = first ? `${first} ${last}` : last;
    }
    const words = n.split(" ");
    if (words.length === 1) return words[0];
    let i = words.length - 1;
    while (i > 1 && PARTICLES.has(words[i - 1].toLowerCase())) i -= 1;
    const surname = words.slice(i).join(" ");
    const given = words.slice(0, i).map((w) => (/^[A-Z]\.?$/.test(w) ? w.charAt(0) + "." : initial(w)));
    return `${given.join(" ")} ${surname}`;
  }

  function authorLine(authors) {
    const list = (authors || []).filter(Boolean);
    if (!list.length) return "";
    if (list.length <= 3) return list.map(abbreviateAuthor).join(", ");
    return `${list.slice(0, 2).map(abbreviateAuthor).join(", ")} … +${list.length - 2}`;
  }

  const DAY_NAMES = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };
  const DAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];

  function describeDays(days) {
    const idx = (days || [])
      .map((d) => DAY_ORDER.indexOf(String(d).slice(0, 3).toLowerCase()))
      .filter((i) => i >= 0)
      .sort((a, b) => a - b);
    if (!idx.length) return "no days";
    if (idx.length === 7) return "every day";
    const runs = [];
    let start = idx[0];
    let prev = idx[0];
    for (const i of idx.slice(1).concat([null])) {
      if (i !== null && i === prev + 1) {
        prev = i;
        continue;
      }
      const a = DAY_NAMES[DAY_ORDER[start]];
      const b = DAY_NAMES[DAY_ORDER[prev]];
      runs.push(start === prev ? a : prev - start === 1 ? `${a}, ${b}` : `${a}–${b}`);
      if (i !== null) start = prev = i;
    }
    return runs.join(", ");
  }

  function describeSlots(sched) {
    if (!sched) return "";
    const times = (sched.times || []).join(", ") || "no times";
    const parts = [`Reading slots: ${describeDays(sched.days)} at ${times}`];
    if (sched.duration_minutes) parts.push(`${sched.duration_minutes} min`);
    if (sched.timezone) parts.push(String(sched.timezone).replace(/_/g, " "));
    return parts.join(" · ");
  }

  function timeAgo(iso) {
    const t = new Date(iso);
    if (isNaN(t)) return "";
    const s = Math.round((Date.now() - t.getTime()) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return whenText(iso);
  }

  function lengthText(seconds) {
    const mins = Math.max(1, Math.round(Number(seconds) / 60));
    const h = Math.floor(mins / 60);
    const m = mins % 60;
    if (!h) return `${mins} min`;
    return m ? `${h} h ${m} min` : `${h} h`;
  }

  // ---------------------------------------------------------------- YouTube

  const YOUTUBE_RE = /^(?:https?:\/\/)?(?:(?:www|m)\.)?(?:youtube\.com|youtu\.be)(?:[/?#]|$)/i;
  const MEDIA_MODES = [
    ["link", "Link"],
    ["audio", "Audio"],
    ["video", "Video"],
  ];

  function isVideo(p) {
    return p.kind === "video";
  }

  function thumbUrl(p) {
    return /^[A-Za-z0-9_-]{11}$/.test(p.video_id || "") ? `https://i.ytimg.com/vi/${p.video_id}/mqdefault.jpg` : null;
  }

  function videoMinutes(p) {
    const sched = (state.status && state.status.schedule) || {};
    if (!isVideo(p) || !p.duration_seconds) return null;
    const mins = Math.max(5, Math.ceil(p.duration_seconds / 300) * 5);
    return Math.min(mins, sched.max_session_minutes || 90);
  }

  // Playback position per item, so a long video resumes where it stopped (even on another visit).
  function loadPos(id) {
    try {
      return Number(localStorage.getItem(`pq.pos.${id}`)) || 0;
    } catch (_) {
      return 0;
    }
  }

  function savePos(id, seconds) {
    try {
      if (seconds > 0) localStorage.setItem(`pq.pos.${id}`, String(Math.floor(seconds)));
      else localStorage.removeItem(`pq.pos.${id}`);
    } catch (_) {}
  }

  function player(p, tag, poster) {
    const src = `/api/papers/${encodeURIComponent(p.id)}/media`;
    const key = `${p.id}:${p.media_mode}`;
    const kept = state.players.get(key);
    if (kept) return kept;
    const node = el(tag, {
      class: `media-player media-${tag}`,
      controls: true,
      preload: "metadata",
      playsinline: tag === "video",
      poster: poster || null,
      src,
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
    return node;
  }

  function mediaStateText(p) {
    if (p.media_mode === "link") return "";
    if (p.media_status === "downloading") return "Downloading…";
    if (p.media_status === "done") return p.has_media ? "Saved to iCloud Drive" : "Saved, but the file is not on this Mac";
    if (p.media_status === "failed") return p.media_fix || "Download failed";
    return "Waiting to download";
  }

  function mediaBlock(p) {
    const wrap = el("div", { class: "media" });
    const thumb = thumbUrl(p);
    if (p.has_media && p.media_mode === "video") {
      wrap.append(player(p, "video", thumb));
    } else {
      const yt = safeUrl(p.url);
      if (thumb && yt) {
        wrap.append(
          el("a", { class: "media-thumb", href: yt, target: "_blank", rel: "noopener noreferrer", tabindex: "-1", "aria-hidden": "true" }, [
            el("img", { src: thumb, alt: "", loading: "lazy", width: "320", height: "180" }),
          ])
        );
      }
      if (p.has_media && p.media_mode === "audio") wrap.append(player(p, "audio"));
    }
    if (p.status !== "read") {
      const group = el(
        "div",
        { class: "segmented", role: "group", "aria-label": "Keep as" },
        MEDIA_MODES.map(([mode, label]) =>
          el("button", {
            class: "segment",
            type: "button",
            text: label,
            "aria-pressed": String(p.media_mode === mode),
            title: mode === "link" ? "Keep only the YouTube link" : `Download the ${mode} for offline use`,
            onclick: (e) => setMedia(p, mode, e.currentTarget),
          })
        )
      );
      const row = el("div", { class: "media-row" }, [group]);
      const text = mediaStateText(p);
      if (text) row.append(el("span", { class: `media-state${p.media_status === "failed" ? " is-failed" : ""}`, text }));
      wrap.append(row);
    }
    return wrap;
  }

  function prettyDir(dir) {
    if (!dir) return "";
    const icloud = String(dir).match(/com~apple~CloudDocs\/(.*)$/);
    if (icloud) return `iCloud Drive/${icloud[1]}`.replace(/\/$/, "");
    return String(dir).replace(/^\/Users\/[^/]+/, "~");
  }

  // ---------------------------------------------------------------- data

  async function fetchView(view) {
    const params = new URLSearchParams({ view });
    if (state.q) params.set("q", state.q);
    if (state.tag) params.set("tag", state.tag);
    const data = await api(`/api/papers?${params}`);
    return (data.papers || []).filter((p) => !state.hiding.has(p.id));
  }

  function isMissed(p) {
    return Boolean(p.missed || p.overdue);
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
      const view = state.view;
      let groups;
      if (view === "upcoming") {
        const [missed, upcoming] = await Promise.all([fetchView("missed"), fetchView("upcoming")]);
        const missedIds = new Set(missed.map((p) => p.id));
        groups = groupUpcoming(missed, upcoming.filter((p) => !missedIds.has(p.id) && !isMissed(p)));
      } else if (view === "queue") {
        groups = groupQueue(await fetchView("queue"));
      } else {
        const read = await fetchView("read");
        groups = read.length ? [{ key: "read", title: null, papers: read }] : [];
      }
      if (view === state.view) render(groups);
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

  function groupUpcoming(missed, upcoming) {
    const groups = [];
    if (missed.length) groups.push({ key: "missed", title: "Missed", papers: missed, missed: true });
    const byDay = new Map();
    const sorted = upcoming.slice().sort((a, b) => new Date(startIso(a)) - new Date(startIso(b)));
    for (const p of sorted) {
      const parts = zonedParts(startIso(p));
      const key = parts ? parts.day : "unknown";
      if (!byDay.has(key)) byDay.set(key, []);
      byDay.get(key).push(p);
    }
    for (const [key, papers] of byDay) {
      groups.push({ key, title: key === "unknown" ? "Scheduled" : dayLabel(key), sub: key !== "unknown" && /^To/.test(dayLabel(key)) ? plainDay(key) : null, papers });
    }
    return groups;
  }

  function groupQueue(papers) {
    const names = { 1: "High priority", 2: "Normal", 3: "Low priority" };
    const out = [];
    for (const pr of [1, 2, 3]) {
      const ps = papers.filter((p) => Number(p.priority) === pr);
      if (ps.length) out.push({ key: `p${pr}`, title: names[pr], papers: ps });
    }
    const other = papers.filter((p) => ![1, 2, 3].includes(Number(p.priority)));
    if (other.length) out.push({ key: "other", title: "Other", papers: other });
    return out;
  }

  // ---------------------------------------------------------------- rendering

  function render(groups) {
    const list = $("#list");
    state.players = new Map(Array.from(list.querySelectorAll(".media-player"), (n) => [n.dataset.key, n]));
    renderViewbar(groups);
    const frag = document.createDocumentFragment();
    if (!groups.length) {
      frag.append(emptyState());
    }
    for (const g of groups) {
      const section = el("section", { class: `group${g.missed ? " group-missed" : ""}`, "aria-label": g.title || "Papers" });
      if (g.title) {
        const head = el("div", { class: "group-head" }, [
          el("h2", { class: "group-title" }, [g.title, g.sub ? el("span", { class: "visually-hidden", text: `, ${g.sub}` }) : null]),
        ]);
        if (g.sub) head.append(el("span", { class: "hint", text: g.sub, "aria-hidden": "true" }));
        if (g.missed) {
          head.append(
            el("button", {
              class: "link-button",
              type: "button",
              text: "Reschedule missed",
              onclick: (e) => autoSchedule(true, e.currentTarget),
            })
          );
        }
        section.append(head);
      }
      for (const p of g.papers) section.append(card(p, g));
      frag.append(section);
    }
    list.replaceChildren(frag);
    state.players = new Map();
  }

  function renderViewbar(groups) {
    const bar = $("#viewbar");
    const children = [];
    if (state.view === "queue") {
      const sched = state.status && state.status.schedule;
      children.push(el("span", { class: "viewbar-note", text: describeSlots(sched) }));
      const hasPapers = groups.some((g) => g.papers.length);
      children.push(
        el("button", {
          class: "button",
          type: "button",
          text: "Auto-schedule",
          disabled: !hasPapers,
          title: "Place queued papers into free reading slots, by priority",
          onclick: (e) => autoSchedule(false, e.currentTarget),
        })
      );
    }
    bar.replaceChildren(...children);
  }

  function emptyState() {
    const filtered = state.q || state.tag;
    const copy = {
      upcoming: ["Nothing scheduled", "Add a paper above, or open the Queue and press Auto-schedule to fill your reading slots."],
      queue: ["The queue is empty", "Paste an arXiv ID, DOI, URL, title or YouTube link above, or use the bookmarklet on any paper page."],
      read: ["Nothing read yet", "Papers you mark as read collect here."],
    }[state.view];
    const [title, text] = filtered ? ["No matches", "Try a different search or clear the tag filter."] : copy;
    return el("div", { class: "empty" }, [el("p", { class: "empty-title", text: title }), el("p", { text })]);
  }

  function card(p, group) {
    const missed = isMissed(p) && p.status !== "read";
    const isRead = p.status === "read";
    const article = el("article", {
      class: `paper${isRead ? " paper-read" : ""}`,
      dataset: { id: p.id },
      "aria-labelledby": `paper-title-${p.id}`,
    });

    // left column: time
    const iso = startIso(p);
    const whenCol = el("div", { class: "paper-when" });
    if (isRead) {
      const parts = p.read_at ? zonedParts(p.read_at) : null;
      whenCol.classList.add("is-none");
      whenCol.append(parts ? plainDay(parts.day) : "Read");
    } else if (iso) {
      const parts = zonedParts(iso);
      if (missed) whenCol.classList.add("is-missed");
      if (group && group.key === parts.day) {
        whenCol.append(parts.time, el("span", { class: "when-sub", text: p.duration_minutes ? `${p.duration_minutes} min` : "" }));
      } else {
        whenCol.append(parts.time, el("span", { class: "when-sub", text: dayLabel(parts.day) }));
      }
    } else {
      whenCol.classList.add("is-none");
      whenCol.append("Unscheduled");
    }

    const main = el("div", { class: "paper-main" });
    const href = safeUrl(p.url) || safeUrl(p.pdf_url);
    const prName = p.priority_name || { 1: "high", 2: "normal", 3: "low" }[p.priority] || "normal";
    const marker = el("span", {
      class: `priority priority-${p.priority}`,
      title: `${prName[0].toUpperCase()}${prName.slice(1)} priority`,
    });
    const titleText = p.title || "Untitled";
    const titleNode = href
      ? el("a", { href, target: "_blank", rel: "noopener noreferrer", text: titleText })
      : document.createTextNode(titleText);
    main.append(
      el("h3", { class: "paper-title", id: `paper-title-${p.id}` }, [
        marker,
        el("span", { class: "visually-hidden", text: `${prName} priority: ` }),
        titleNode,
      ])
    );

    const by = [];
    if (isVideo(p)) {
      const parts = [p.duration_seconds ? `▶ ${lengthText(p.duration_seconds)}` : "▶ YouTube", (p.authors || [])[0]];
      by.push(el("span", { text: parts.filter(Boolean).join(" · ") }));
    } else {
      const authors = authorLine(p.authors);
      if (authors) by.push(el("span", { text: authors, title: (p.authors || []).join(", ") }));
      const venueYear = [p.venue, p.year].filter(Boolean).join(" · ");
      if (venueYear) {
        if (by.length) by.push(el("span", { class: "sep", text: "—", "aria-hidden": "true" }));
        by.push(el("span", { text: venueYear }));
      }
    }
    if (by.length) main.append(el("p", { class: "paper-byline" }, by));
    if (isVideo(p)) main.append(mediaBlock(p));

    if ((p.tags || []).length) {
      main.append(
        el(
          "div",
          { class: "paper-tags" },
          p.tags.map((t) =>
            el("button", {
              class: "tag",
              type: "button",
              text: t,
              "aria-label": `Filter by tag ${t}`,
              onclick: () => setTag(t),
            })
          )
        )
      );
    }

    if (p.needs_metadata) {
      main.append(el("p", { class: "paper-flag", text: "Details incomplete; paperqueue will retry the lookup." }));
    }
    if (missed && iso) {
      main.append(el("p", { class: "paper-flag", text: `Missed session: ${whenText(iso)}` }));
    }

    // actions
    const actions = el("div", { class: "paper-actions" });
    const pdfHref = p.has_pdf ? `/api/papers/${encodeURIComponent(p.id)}/pdf` : safeUrl(p.pdf_url);
    const ytHref = isVideo(p) ? safeUrl(p.url) : null;
    if (ytHref) {
      actions.append(el("a", { href: ytHref, target: "_blank", rel: "noopener noreferrer", text: "YouTube", title: "Open on YouTube" }));
    } else if (pdfHref) {
      actions.append(
        el("a", {
          href: pdfHref,
          target: "_blank",
          rel: "noopener noreferrer",
          text: "PDF",
          title: p.has_pdf ? "Open the cached PDF" : "Open the PDF online",
        })
      );
    }
    if (!isRead) {
      actions.append(
        el("button", {
          class: `link-button${iso ? "" : " action-primary"}`,
          type: "button",
          text: iso ? "Reschedule" : "Schedule",
          onclick: () => openSchedule(p),
        })
      );
    }
    actions.append(
      el("button", {
        class: "link-button",
        type: "button",
        text: isRead ? "Back to queue" : isVideo(p) ? "Mark watched" : "Mark read",
        onclick: (e) => markRead(p, !isRead, e.currentTarget),
      })
    );
    const lib = safeUrl(p.library_link, ["zotero:", "http:", "https:"]);
    if (lib) {
      actions.append(
        el("a", {
          href: lib,
          rel: "noopener noreferrer",
          text: p.library === "mendeley" ? "Open in Mendeley" : "Open in library",
          title: "Open this item in your reference manager",
        })
      );
    }
    actions.append(
      el("button", {
        class: "link-button action-danger",
        type: "button",
        text: "Remove",
        onclick: () => removePaper(p, article),
      })
    );
    main.append(actions);

    if (p.abstract) {
      main.append(
        el("details", { class: "abstract" }, [el("summary", { text: "Abstract" }), el("p", { text: p.abstract })])
      );
    }

    article.append(whenCol, main);
    return article;
  }

  // ---------------------------------------------------------------- actions

  async function withBusy(target, fn) {
    const article = target && target.closest ? target.closest(".paper") : null;
    if (target) target.disabled = true;
    if (article) article.classList.add("is-busy");
    try {
      return await fn();
    } catch (err) {
      showError(err);
      return null;
    } finally {
      if (target) target.disabled = false;
      if (article) article.classList.remove("is-busy");
    }
  }

  async function afterMutation(data) {
    showWarnings(data);
    await Promise.all([load(), refreshStatus()]);
  }

  async function markRead(p, read, button) {
    const data = await withBusy(button, () => api(`/api/papers/${p.id}/read`, { method: "POST", body: { read } }));
    if (!data) return;
    toast(read ? `Marked as ${isVideo(p) ? "watched" : "read"}: ${p.title}` : `Back in the queue: ${p.title}`, {
      action: { label: "Undo", run: () => markRead(p, !read, null) },
    });
    await afterMutation(data);
  }

  function removePaper(p, article) {
    state.hiding.add(p.id);
    article.hidden = true;
    let undone = false;
    const commit = async () => {
      if (undone) return;
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
          article.hidden = false;
          load();
        },
      },
    });
  }
  const pendingRemovals = new Map();
  window.addEventListener("pagehide", () => {
    for (const flush of pendingRemovals.values()) flush();
  });

  async function setMedia(p, mode, button) {
    const data = await withBusy(button, () => api(`/api/papers/${p.id}/media`, { method: "POST", body: { mode } }));
    if (!data) return;
    const failed = data.paper && data.paper.media_status === "failed";
    if (mode !== "link" && !failed) toast(`Downloading the ${mode} in the background.`);
    await afterMutation(data);
  }

  async function autoSchedule(includeMissed, button) {
    const data = await withBusy(button, () =>
      api("/api/auto-schedule", { method: "POST", body: { include_missed: includeMissed } })
    );
    if (!data) return;
    const n = (data.papers || []).length;
    const u = (data.unplaced || []).length;
    let msg = n ? `Scheduled ${n} paper${n === 1 ? "" : "s"}` : "Nothing to schedule";
    if (u) msg += `; ${u} didn’t fit in the next ${(state.status && state.status.schedule && state.status.schedule.horizon_days) || 14} days`;
    toast(msg + ".");
    await afterMutation(data);
  }

  async function addPaper(ev) {
    ev.preventDefault();
    const input = $("#add-ref");
    const ref = input.value.trim();
    if (!ref) {
      input.focus();
      return;
    }
    const button = $("#add-submit");
    button.disabled = true;
    button.textContent = "Adding…";
    const body = { ref, priority: Number($("#add-priority").value), when: $("#add-when").value, source: "web" };
    if (!$("#add-media-field").hidden) body.media = $("#add-media").value;
    try {
      const data = await api("/api/papers", { method: "POST", body });
      input.value = "";
      updateAddMedia();
      const p = data.paper;
      const iso = startIso(p);
      if (!data.created) toast(`Already in your list: ${p.title}`);
      else toast(iso ? `Added “${p.title}” for ${whenText(iso)}.` : `Added “${p.title}” to the queue.`);
      showWarnings(data);
      if (data.created) setView(iso ? "upcoming" : "queue", { silent: true });
      await Promise.all([load(), refreshStatus()]);
    } catch (err) {
      showError(err);
    } finally {
      button.disabled = false;
      button.textContent = "Add";
    }
  }

  // ---------------------------------------------------------------- schedule dialog

  const sDialog = $("#schedule-dialog");

  async function openSchedule(p) {
    state.dialogPaper = p;
    $("#schedule-title").textContent = p.title || "Untitled";
    $("#schedule-kicker").textContent = isVideo(p) ? (p.media_mode === "audio" ? "Schedule listening" : "Schedule watching") : "Schedule reading";
    const iso = startIso(p);
    $("#schedule-current").textContent = iso ? `Currently ${whenText(iso)}${isMissed(p) ? " (missed)" : ""}` : "Not scheduled yet";
    $("#schedule-clear").hidden = !iso;
    const at = $("#schedule-at");
    at.value = iso ? zonedParts(iso).local : "";
    at.min = zonedParts(new Date()).local;
    const dur = $("#schedule-duration");
    const defaultDur = state.status && state.status.schedule && state.status.schedule.duration_minutes;
    dur.value = p.duration_minutes || videoMinutes(p) || defaultDur || 45;
    const chips = $("#schedule-slots");
    chips.replaceChildren(el("span", { class: "hint", text: "Loading…" }));
    sDialog.showModal();
    try {
      const data = await api(`/api/slots?n=6&paper_id=${encodeURIComponent(p.id)}`);
      const slots = data.slots || [];
      chips.replaceChildren(
        ...(slots.length
          ? slots.map((s) => {
              const when = s.local || s.utc;
              return el("button", {
                class: "chip",
                type: "button",
                text: whenText(when),
                onclick: () => doSchedule(s.utc || s.local),
              });
            })
          : [el("span", { class: "hint", text: "No free slots in the scheduling horizon." })])
      );
    } catch (err) {
      chips.replaceChildren(el("span", { class: "hint", text: "Couldn’t load free slots." }));
    }
  }

  async function doSchedule(when) {
    const p = state.dialogPaper;
    if (!p) return;
    const durRaw = Number($("#schedule-duration").value);
    const body = { when };
    if (durRaw > 0 && durRaw !== ((state.status && state.status.schedule && state.status.schedule.duration_minutes) || 0)) {
      body.duration_minutes = Math.round(durRaw);
    } else if (p.duration_minutes && durRaw > 0) {
      body.duration_minutes = Math.round(durRaw);
    }
    try {
      const data = await api(`/api/papers/${p.id}/schedule`, { method: "POST", body });
      sDialog.close();
      const iso = startIso(data.paper);
      toast(iso ? `Scheduled for ${whenText(iso)}.` : "Scheduled.");
      await afterMutation(data);
    } catch (err) {
      showError(err);
    }
  }

  async function doUnschedule() {
    const p = state.dialogPaper;
    if (!p) return;
    try {
      const data = await api(`/api/papers/${p.id}/schedule`, { method: "DELETE" });
      sDialog.close();
      toast("Moved back to the queue.");
      await afterMutation(data);
    } catch (err) {
      showError(err);
    }
  }

  $("#schedule-next").addEventListener("click", () => doSchedule("next"));
  $("#schedule-clear").addEventListener("click", doUnschedule);
  $("#schedule-save").addEventListener("click", () => {
    const v = $("#schedule-at").value;
    if (!v) {
      $("#schedule-at").focus();
      toast("Pick a date and time, or choose a free slot.", { kind: "warn" });
      return;
    }
    doSchedule(v);
  });
  $("#schedule-at").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      $("#schedule-save").click();
    }
  });
  sDialog.addEventListener("close", () => {
    state.dialogPaper = null;
  });

  // close dialogs on backdrop click
  for (const d of document.querySelectorAll("dialog")) {
    d.addEventListener("click", (e) => {
      if (e.target === d) d.close();
    });
  }

  // ---------------------------------------------------------------- iPhone dialog

  const iDialog = $("#iphone-dialog");
  $("#iphone-open").addEventListener("click", () => {
    const lan = state.status && state.status.lan;
    const on = Boolean(lan && lan.enabled);
    $("#iphone-lan").hidden = !on;
    $("#iphone-off").hidden = on;
    if (on) {
      $("#iphone-qr").src = `/api/qr.svg?t=${Date.now()}`;
      $("#iphone-urls").textContent = (lan.urls || []).length ? `Or open ${lan.urls[0]} and use the link from the terminal.` : "";
    }
    iDialog.showModal();
  });

  // ---------------------------------------------------------------- status + footer

  function setStatusText(node, text, kind) {
    node.textContent = text;
    node.className = kind ? `status-${kind}` : "";
  }

  function renderStatus(st) {
    const counts = st.counts || {};
    const toRead = counts.to_read || 0;
    const parts = [`${toRead} to read`];
    if (counts.scheduled) parts.push(`${counts.scheduled} scheduled`);
    const missed = counts.missed || counts.overdue;
    if (missed) parts.push(`${missed} missed`);
    $("#summary").textContent = parts.join(" · ");

    const countFor = {
      upcoming: counts.scheduled,
      queue: counts.unscheduled,
      read: counts.read,
    };
    for (const node of document.querySelectorAll("[data-count]")) {
      const n = countFor[node.dataset.count];
      node.textContent = n ? String(n) : "";
    }

    const lib = st.library || {};
    const libBtn = $("#library-sync");
    if (!lib.provider || lib.provider === "off") {
      setStatusText($("#library-status"), "Off (set library.provider to zotero or mendeley)", "off");
      libBtn.hidden = true;
    } else {
      const name = lib.provider.charAt(0).toUpperCase() + lib.provider.slice(1);
      let text = `${name}: ${lib.message || (lib.ok ? "connected" : "not reachable")}`;
      if (lib.last_sync) text += ` · synced ${timeAgo(lib.last_sync)}`;
      setStatusText($("#library-status"), text, lib.ok ? "ok" : "bad");
      libBtn.hidden = false;
    }

    const cal = st.calendar || {};
    const calBtn = $("#calendar-sync");
    if (!cal.backend || cal.backend === "off" || cal.backend === "none") {
      setStatusText($("#calendar-status"), cal.message || "Off", "off");
      calBtn.hidden = true;
    } else {
      const label = cal.name ? `“${cal.name}”` : "Calendar";
      setStatusText($("#calendar-status"), `${label}: ${cal.message || (cal.ok ? "connected" : "not reachable")}`, cal.ok ? "ok" : "bad");
      calBtn.hidden = false;
    }

    const media = st.media || {};
    const modeName = { link: "links only", audio: "audio downloads", video: "video downloads" }[media.default_mode] || "links only";
    const notes = [`Default: ${modeName}`];
    if (media.deno === false) notes.push("downloads need Deno (brew install deno)");
    else if (media.ffmpeg === false) notes.push("video needs ffmpeg (brew install ffmpeg)");
    if (media.pending) notes.push(`${media.pending} to download`);
    if ((media.failed || []).length) notes.push(`${media.failed.length} failed`);
    const mediaNode = $("#media-status");
    setStatusText(mediaNode, notes.join(" · "), media.deno === false || (media.failed || []).length ? "bad" : media.deno ? "ok" : "off");
    mediaNode.title = media.dir || "";

    const pdf = st.pdf || {};
    const pdfNode = $("#pdf-status");
    if (pdf.enabled === false) setStatusText(pdfNode, "Cache off", "off");
    else {
      setStatusText(pdfNode, prettyDir(pdf.dir) || "Cache folder not set", pdf.dir ? "ok" : "off");
      pdfNode.title = pdf.dir || "";
    }
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
      }
      renderStatus(st);
      if (state.view === "queue") {
        const note = $(".viewbar-note");
        if (note) note.textContent = describeSlots(st.schedule);
      }
      const changed = state.rev !== null && st.rev !== state.rev;
      state.rev = st.rev;
      return changed;
    } catch (err) {
      if (err.status === 401) {
        $("#summary").textContent = "Not signed in: open the link with ?token= from `paperqueue serve --lan`.";
      }
      return false;
    }
  }

  async function poll() {
    if (document.hidden) return;
    if (await refreshStatus()) load();
  }

  async function syncAction(button, path, okText) {
    const prev = button.textContent;
    button.textContent = "Syncing…";
    try {
      const data = await withBusy(button, () => api(path, { method: "POST", body: {} }));
      if (!data) return;
      if (typeof data.pulled === "number") {
        toast(`${okText}: ${data.pulled} in library, ${data.created || 0} new${data.pushed ? `, ${data.pushed} pushed` : ""}.`);
      } else {
        toast(`${okText}.`);
      }
      await afterMutation(data);
    } finally {
      button.textContent = prev;
    }
  }

  $("#library-sync").addEventListener("click", (e) => syncAction(e.currentTarget, "/api/library/sync", "Library synced"));
  $("#calendar-sync").addEventListener("click", (e) =>
    syncAction(e.currentTarget, "/api/calendar/sync", "Calendar re-synced")
  );

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
    for (var k in h) if (!h[k] || (h[k].length === 0)) delete h[k];
    var b = btoa(unescape(encodeURIComponent(JSON.stringify(h)))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    window.open(origin + "/add?ref=" + encodeURIComponent(location.href) + "&hints=" + b, "_blank");
  }

  function setupBookmarklet() {
    const link = $("#bookmarklet");
    const src = `(${bookmarklet.toString()})(${JSON.stringify(location.origin)})`;
    link.setAttribute("href", "javascript:" + encodeURIComponent(src.replace(/\s*\n\s*/g, " ")));
    link.addEventListener("click", (e) => {
      e.preventDefault();
      toast("Drag “Save to paperqueue” to your bookmarks bar, then click it on a paper’s page.");
    });
  }

  // ---------------------------------------------------------------- views, search, tags

  function setView(view, opts) {
    if (!["upcoming", "queue", "read"].includes(view)) return;
    state.view = view;
    for (const t of document.querySelectorAll(".tab")) t.setAttribute("aria-pressed", String(t.dataset.view === view));
    try {
      localStorage.setItem("pq.view", view);
    } catch (_) {}
    if (!(opts && opts.silent)) load();
  }

  function setTag(tag) {
    state.tag = tag || "";
    const bar = $("#filterbar");
    bar.hidden = !state.tag;
    $("#tag-clear").textContent = state.tag;
    load();
  }

  // The "Keep" selector appears only while the add bar holds a YouTube link.
  function updateAddMedia() {
    const field = $("#add-media-field");
    const show = YOUTUBE_RE.test($("#add-ref").value.trim());
    if (show && field.hidden) {
      const mode = state.status && state.status.media && state.status.media.default_mode;
      $("#add-media").value = mode || "link";
    }
    field.hidden = !show;
  }

  for (const t of document.querySelectorAll(".tab")) t.addEventListener("click", () => setView(t.dataset.view));
  $("#tag-clear").addEventListener("click", () => setTag(""));
  $("#add-form").addEventListener("submit", addPaper);
  $("#add-ref").addEventListener("input", updateAddMedia);

  let searchTimer = null;
  $("#search").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.q = e.target.value.trim();
      load();
    }, 220);
  });

  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const t = e.target;
    const typing = t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
    if (document.querySelector("dialog[open]")) return;
    if (e.key === "Escape" && t && t.id === "search" && t.value) {
      t.value = "";
      state.q = "";
      load();
      return;
    }
    if (typing) return;
    if (e.key === "/") {
      e.preventDefault();
      $("#search").focus();
    } else if (e.key === "n" || e.key === "a") {
      e.preventDefault();
      $("#add-ref").focus();
    } else if (e.key === "1" || e.key === "2" || e.key === "3") {
      setView(["upcoming", "queue", "read"][Number(e.key) - 1]);
    }
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) poll();
  });

  // ---------------------------------------------------------------- start

  (async function start() {
    setupBookmarklet();
    try {
      const saved = localStorage.getItem("pq.view");
      if (saved) setView(saved, { silent: true });
    } catch (_) {}
    await refreshStatus();
    await load();
    setInterval(poll, 5000);
  })();
})();
