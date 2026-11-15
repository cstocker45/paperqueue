/* Applies the saved theme (system / light / dark) and scanline choice before first paint.
   Loaded synchronously in <head> by every page; app.js calls window.pqTheme to change it. */
"use strict";

(function () {
  const root = document.documentElement;
  const COLORS = { light: "#f4f7f5", dark: "#0b0d0c" };
  const dark = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function read(key, fallback) {
    try {
      return localStorage.getItem(key) || fallback;
    } catch (_) {
      return fallback;
    }
  }

  function write(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch (_) {}
  }

  function resolved(choice) {
    if (choice === "light" || choice === "dark") return choice;
    return dark && dark.matches ? "dark" : "light";
  }

  function apply(choice) {
    const theme = resolved(choice);
    if (choice === "light" || choice === "dark") root.dataset.theme = choice;
    else delete root.dataset.theme;
    root.dataset.resolvedTheme = theme;
    for (const meta of document.querySelectorAll('meta[name="theme-color"]')) {
      if (choice === "light" || choice === "dark") {
        meta.setAttribute("content", COLORS[theme]);
        meta.removeAttribute("media");
      } else {
        meta.setAttribute("content", COLORS[meta.dataset.scheme] || COLORS.light);
        meta.setAttribute("media", `(prefers-color-scheme: ${meta.dataset.scheme || "light"})`);
      }
    }
    const manifest = document.querySelector('link[rel="manifest"]');
    if (manifest) manifest.setAttribute("href", theme === "dark" ? "/manifest.webmanifest?theme=dark" : "/manifest.webmanifest");
  }

  function setScan(on) {
    root.classList.toggle("scanlines", Boolean(on));
  }

  let choice = read("pq.theme", "system");
  if (!["system", "light", "dark"].includes(choice)) choice = "system";
  apply(choice);
  setScan(read("pq.scanlines", "off") === "on");
  if (dark && dark.addEventListener) dark.addEventListener("change", () => apply(choice));
  document.addEventListener("DOMContentLoaded", () => apply(choice));

  window.pqTheme = {
    get: () => choice,
    set(next) {
      choice = ["light", "dark"].includes(next) ? next : "system";
      write("pq.theme", choice);
      apply(choice);
    },
    scanlines: () => root.classList.contains("scanlines"),
    setScanlines(on) {
      write("pq.scanlines", on ? "on" : "off");
      setScan(on);
    },
  };
})();
