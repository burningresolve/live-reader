#!/usr/bin/env python3
"""livereader: read files in your browser, with automatic reload.

Serves a directory with a clean dark file browser and readable previews for
Markdown, JSON, source code, plain text and images. The page reloads by itself
when files change.

Python standard library only. Linux only (auto-reload uses inotify).

Usage:
    livereader                 serve the current directory
    livereader /path/to/dir    serve a specific directory
"""

import argparse
import codecs
import ctypes
import errno
import html
import json
import mimetypes
import os
import random
import select
import stat
import struct
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

__version__ = "1.2.0"

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

HOST = "127.0.0.1"                  # localhost only: files are never exposed to the network
PORT_RANGE = (40000, 50000)         # rarely used by other software
PORT_ATTEMPTS = 50

# Size limits protect RAM and CPU on small machines. The browser must hold the
# whole file as text, so these are limits on what is rendered, not on what is served.
PREVIEW_LIMIT = 10 * 1024 * 1024    # larger text files ask for confirmation ("Render anyway")
FORMAT_LIMIT = 8 * 1024 * 1024      # larger minified JS / JSON is shown as-is, not reformatted
HIGHLIGHT_LIMIT = 3 * 1024 * 1024   # larger text is not highlighted unless you click "Highlight anyway"
INJECT_LIMIT = 20 * 1024 * 1024     # larger HTML files are served without the reload script

# Folder sizes in the directory listing (computed on demand, never in the background).
FOLDER_SIZE_LIMIT = 250_000         # stop counting after this many entries in one folder
FOLDER_SIZE_SECONDS = 8             # ...or after this long, and show "≥ size"
SIZE_CACHE_SECONDS = 30

SNIFF_BYTES = 8192
CHUNK_SIZE = 64 * 1024
DEBOUNCE_SECONDS = 0.15
HEARTBEAT_SECONDS = 15
EVENTS_PATH = "/__events"
SIZE_PATH = "/__size"
IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache"}

# Browser libraries, pinned and loaded on demand (only when a file needs them).
LIBS = {
    "hljsCss": "https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11.9.0/styles/github-dark.min.css",
    "hljs": "https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11.9.0/highlight.min.js",
    "marked": "https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js",
    "purify": "https://cdn.jsdelivr.net/npm/dompurify@3.1.2/dist/purify.min.js",
    "beautify": "https://cdn.jsdelivr.net/npm/js-beautify@1.15.1/js/lib/beautify.js",
}

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp", ".avif"}
JSON_EXTENSIONS = {".json", ".map", ".geojson", ".webmanifest"}  # .map = source maps
TEXT_KINDS = {"markdown", "json", "code"}

LANGUAGES = {
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".es6": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".py": "python", ".rb": "ruby", ".php": "php", ".pl": "perl", ".lua": "lua",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".swift": "swift",
    ".css": "css", ".scss": "scss", ".less": "less",
    ".xml": "xml", ".yml": "yaml", ".yaml": "yaml",
    ".toml": "ini", ".ini": "ini", ".conf": "ini", ".cfg": "ini",
    ".sql": "sql", ".diff": "diff", ".patch": "diff",
}
FILENAME_LANGUAGES = {"makefile": "makefile", "dockerfile": "dockerfile"}

# --------------------------------------------------------------------------
# Page assets
# --------------------------------------------------------------------------

CSS = r"""
:root{color-scheme:dark}
[hidden]{display:none!important}
*{box-sizing:border-box}
body{margin:0;background:#0d1117;color:#e6edf3;font:14px/1.5 -apple-system,"Segoe UI",Ubuntu,Roboto,sans-serif;
 -webkit-text-size-adjust:100%}
a{color:#58a6ff;text-decoration:none}
a:hover{text-decoration:underline}
button{font:inherit;color:inherit;cursor:pointer}

body>header{display:flex;flex-wrap:wrap;gap:8px 16px;justify-content:space-between;align-items:center;
 padding:10px 16px;background:#161b22;border-bottom:1px solid #30363d}
.crumbs{font-weight:600;overflow-wrap:anywhere}
.sep{color:#6e7681;margin:0 6px}
.meta{display:flex;gap:12px;align-items:center;color:#8b949e}
.seg{display:inline-flex;border:1px solid #30363d;border-radius:6px;overflow:hidden}
.seg a{padding:3px 14px;color:#8b949e}
.seg a:hover{color:#e6edf3;text-decoration:none}
.seg a.on{background:#30363d;color:#e6edf3}

main{max-width:1100px;margin:0 auto;padding:16px 16px 96px;font-size:calc(16px*var(--zoom,1));
 container-type:inline-size}

/* Directory listing: a grid, so header and rows always share the same columns. */
.listing{border:1px solid #30363d;border-radius:8px;overflow:hidden;font-size:.94em}
.lhead,.row{display:grid;grid-template-columns:minmax(0,1fr) 11ch 17ch;column-gap:1.5em;
 align-items:center;padding:.5em .9em}
.lhead{background:#161b22;border-bottom:1px solid #30363d;color:#8b949e}
.lhead button{display:inline-flex;align-items:center;gap:0;padding:.25em 0;background:none;border:0;
 color:inherit;font:inherit;font-size:.8em;text-transform:uppercase;letter-spacing:.03em}
.lhead button:hover,.lhead [aria-sort="ascending"] button,.lhead [aria-sort="descending"] button{color:#e6edf3}
.arrow{display:inline-block;width:0;overflow:hidden;font-size:.8em}
.lhead [aria-sort="ascending"] .arrow,.lhead [aria-sort="descending"] .arrow{width:1em;margin-left:.4em}
[aria-sort="ascending"] .arrow::after{content:"\25B2"}
[aria-sort="descending"] .arrow::after{content:"\25BC"}
.row{border-bottom:1px solid #21262d}
.row:last-child{border-bottom:0}
.row:hover{background:#161b22}
.row .name{min-width:0;overflow-wrap:anywhere}
.row[data-dir="1"] .name{font-weight:600}
.row[data-dir="1"] .name::after{content:"/"}
.row.up .name::after{content:none}
.row .size,.row .date{color:#8b949e;white-space:nowrap;font-variant-numeric:tabular-nums;text-align:left}
.empty{color:#8b949e;text-align:center;padding:2em}
@container (max-width:36em){
 .lhead,.row{grid-template-columns:minmax(0,1fr) 17ch;grid-template-areas:"name name" "size date";
  column-gap:1em;row-gap:.15em}
 .lhead .c-name{grid-area:name}
 .lhead .c-size{grid-area:size}
 .lhead .c-date{grid-area:date}
 .row.up{grid-template-areas:"name name"}
 .row .name{grid-area:name}
 .row .size{grid-area:size;font-size:.85em}
 .row .date{grid-area:date;font-size:.85em}
}

.notice{padding:1.5em;background:#161b22;border:1px solid #30363d;border-radius:6px;color:#8b949e}
.notice h1{margin:0 0 .4em;color:#e6edf3;font-size:1.3em}
.note{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center;color:#d29922;margin-bottom:.6em;font-size:.875em}
.linkbtn{padding:2px 12px;background:none;border:1px solid #30363d;border-radius:6px;color:#58a6ff}
.linkbtn:hover{background:#21262d}
.image{text-align:center;padding:1em;background:#161b22;border:1px solid #30363d;border-radius:6px}
.image img{max-width:100%;height:auto}

pre{margin:0;padding:1em;overflow:auto;background:#161b22;border:1px solid #30363d;border-radius:6px;
 font:.85em/1.5 "Ubuntu Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;tab-size:4}
pre code,pre code.hljs{display:inline;padding:0;overflow:visible;background:transparent}
html.lr-wrap pre{white-space:pre-wrap;overflow-wrap:anywhere}
.lr-chunked{display:block}
.lr-chunk{display:block;content-visibility:auto;contain-intrinsic-size:auto 4000px}
html:not(.lr-wrap) .lr-chunked{width:var(--lr-code-width,auto)}

.markdown{max-width:52em;margin:0 auto;line-height:1.65;overflow-wrap:break-word}
.markdown :is(h1,h2,h3,h4,h5,h6){margin:1.5em 0 .75em;line-height:1.25;scroll-margin-top:16px}
.markdown :is(h1,h2){padding-bottom:.3em;border-bottom:1px solid #30363d}
.markdown>:first-child{margin-top:0}
.markdown p,.markdown ul,.markdown ol{margin:0 0 1em}
.markdown code{padding:.2em .4em;background:rgba(110,118,129,.4);border-radius:6px;font-size:.85em;
 font-family:"Ubuntu Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.markdown pre{margin:0 0 1em}
.markdown pre code{padding:0;background:none;font-size:1em}
.markdown blockquote{margin:0 0 1em;padding:0 1em;color:#8b949e;border-left:.25em solid #30363d}
.markdown table{display:block;overflow:auto;border-collapse:collapse;margin:0 0 1em}
.markdown th,.markdown td{border:1px solid #30363d;padding:.4em .8em}
.markdown th{font-weight:600}
.markdown img{max-width:100%}
.markdown hr{border:0;border-top:1px solid #30363d;margin:1.5em 0}

.dock{position:fixed;right:16px;bottom:16px;display:flex;gap:8px;align-items:center;z-index:5}
.dock button{border:0;background:none}
.dock .fab{display:flex;align-items:center;justify-content:center;width:50px;height:50px;border-radius:50%;
 background:#1f6feb;color:#fff;box-shadow:0 4px 14px rgba(0,0,0,.45)}
.dock .fab:hover,.dock .fab[aria-expanded="true"]{background:#388bfd}

.settings-panel{position:fixed;right:16px;bottom:80px;z-index:6;display:flex;flex-direction:column;
 width:min(300px,calc(100vw - 32px));max-height:min(70vh,560px);background:#161b22;
 border:1px solid #30363d;border-radius:10px;box-shadow:0 8px 28px rgba(0,0,0,.55)}
.panel-head{display:flex;justify-content:space-between;align-items:center;padding:6px 8px 6px 14px;
 border-bottom:1px solid #30363d}
.panel-head button{width:34px;height:34px;font-size:22px;line-height:1;color:#8b949e;background:none;
 border:0;border-radius:6px}
.panel-head button:hover{background:#21262d;color:#e6edf3}
.panel-body{overflow:auto}
.control{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:10px 14px;
 border-bottom:1px solid #21262d}
.control:last-child{border-bottom:0}
.control-label{color:#8b949e;font-size:.875em}
.zoom{display:flex;background:#0d1117;border:1px solid #30363d;border-radius:8px;overflow:hidden}
.zoom button{height:36px;min-width:36px;padding:0 8px;font-size:16px}
.zoom button:hover{background:#21262d}
#lr-zoom-reset{min-width:54px;font-size:12px;color:#8b949e}
.switch{min-width:64px;padding:5px 12px;border:1px solid #30363d;border-radius:6px;background:#0d1117;
 color:#c9d1d9;font-size:.875em}
.switch:hover{background:#21262d}
.switch[aria-pressed="true"],.switch[aria-expanded="true"]{background:#1f6feb33;border-color:#1f6feb;color:#58a6ff}

.toc{position:fixed;right:16px;bottom:80px;z-index:5;display:flex;flex-direction:column;
 width:min(360px,calc(100vw - 32px));max-height:min(60vh,520px);background:#161b22;
 border:1px solid #30363d;border-radius:10px;box-shadow:0 8px 28px rgba(0,0,0,.55)}
.toc-head{display:flex;justify-content:space-between;align-items:center;padding:6px 8px 6px 14px;
 border-bottom:1px solid #30363d}
.toc-head button{width:34px;height:34px;font-size:22px;line-height:1;color:#8b949e;background:none;
 border:0;border-radius:6px}
.toc-head button:hover{background:#21262d;color:#e6edf3}
.toc-list{overflow:auto;padding:6px}
.toc-list a{display:block;padding:7px 10px;border-radius:6px;color:#c9d1d9;line-height:1.35;overflow-wrap:anywhere}
.toc-list a:hover{background:#21262d;text-decoration:none}
.toc-list a.on{background:#1f6feb33;color:#58a6ff}

@media(max-width:600px){
 main{padding:12px 12px 96px}
 body>header{padding:8px 12px}
 .dock{right:12px;bottom:12px}
 .settings-panel{right:12px;bottom:76px;width:calc(100vw - 24px)}
 .toc{right:12px;bottom:76px;width:calc(100vw - 24px)}
}
"""

# Runs in <head> so the saved zoom and word-wrap state apply before the first paint.
PAGE_INIT_JS = (
    'try{var r=document.documentElement,z=parseFloat(sessionStorage.getItem("zoom"));'
    'if(z>0)r.style.setProperty("--zoom",z);'
    'if(sessionStorage.getItem("wrap")!=="0")r.classList.add("lr-wrap")}'
    'catch(e){r.classList.add("lr-wrap")}'
)

# Added to the user's own HTML pages (kept minimal, touches no storage).
INJECTED_RELOAD_SCRIPT = (
    '<script>(function(){var v=null,es=new EventSource("%s");'
    "es.onmessage=function(e){if(v!==null&&e.data!==v)location.reload();v=e.data;};})();</script>"
    % EVENTS_PATH
)

# Shared by every page the server renders: live reload, scroll memory, zoom, word wrap.
# Zoom, wrap and scroll position live in sessionStorage, which is private to each tab.
PAGE_JS = r"""
(function () {
  var root = document.documentElement;
  var scrollKey = "lr-scroll:" + location.pathname + location.search;
  var STEPS = [0.7, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2];

  function read(key) { try { return sessionStorage.getItem(key); } catch (e) { return null; } }
  function write(key, value) { try { sessionStorage.setItem(key, value); } catch (e) {} }
  function forget(key) { try { sessionStorage.removeItem(key); } catch (e) {} }

  // Live reload. The scroll position is kept across the reload.
  var version = null;
  var events = new EventSource("/__events");
  events.onmessage = function (e) {
    if (version !== null && e.data !== version) {
      write(scrollKey, window.scrollY);
      location.reload();
    }
    version = e.data;
  };

  window.restoreScroll = function () {
    var saved = read(scrollKey);
    if (saved !== null) { forget(scrollKey); window.scrollTo(0, +saved); return; }
    if (location.hash.length > 1) {
      try {
        var target = document.getElementById(decodeURIComponent(location.hash.slice(1)));
        if (target) target.scrollIntoView();
      } catch (e) {}
    }
  };
  if (!document.getElementById("lr-content")) window.restoreScroll();

  // Layout changes (zoom, wrap) keep the reading position by scaling the scroll offset.
  function keepPosition(change) {
    var height = root.scrollHeight, top = window.scrollY;
    change();
    window.scrollTo(0, top * root.scrollHeight / height);
  }

  // Zoom (per tab).
  var label = document.getElementById("lr-zoom-reset");
  function currentZoom() { var z = parseFloat(read("zoom")); return z > 0 ? z : 1; }
  function showZoom(z) { if (label) label.textContent = Math.round(z * 100) + "%"; }
  function applyZoom(z) {
    keepPosition(function () {
      write("zoom", z);
      root.style.setProperty("--zoom", z);
      showZoom(z);
    });
  }
  function stepZoom(direction) {
    var z = currentZoom(), index = 0;
    STEPS.forEach(function (step, i) { if (Math.abs(step - z) < Math.abs(STEPS[index] - z)) index = i; });
    applyZoom(STEPS[Math.max(0, Math.min(STEPS.length - 1, index + direction))]);
  }
  if (label) {
    showZoom(currentZoom());
    document.getElementById("lr-zoom-in").addEventListener("click", function () { stepZoom(1); });
    document.getElementById("lr-zoom-out").addEventListener("click", function () { stepZoom(-1); });
    label.addEventListener("click", function () { applyZoom(1); });
  }

  // Word wrap (per tab). The CSS reflows by itself when the window is resized.
  var wrapButton = document.getElementById("lr-wrap");
  function showWrap(on) {
    if (!wrapButton) return;
    wrapButton.setAttribute("aria-pressed", on ? "true" : "false");
    wrapButton.textContent = on ? "On" : "Off";
  }
  function toggleWrap() {
    keepPosition(function () {
      var on = !root.classList.contains("lr-wrap");
      root.classList.toggle("lr-wrap", on);
      write("wrap", on ? "1" : "0");
      showWrap(on);
    });
  }
  if (wrapButton) {
    showWrap(root.classList.contains("lr-wrap"));
    wrapButton.addEventListener("click", toggleWrap);
  }

  // Settings panel. The controls live inside it so the reader stays uncluttered.
  var settingsButton = document.getElementById("lr-settings");
  var settingsPanel = document.getElementById("lr-settings-panel");
  function closeSettings() {
    if (!settingsPanel || settingsPanel.hidden) return;
    settingsPanel.hidden = true;
    settingsButton.setAttribute("aria-expanded", "false");
  }
  window.closeSettings = closeSettings;
  function toggleSettings(open) {
    if (!settingsButton || !settingsPanel) return;
    if (open === undefined) open = settingsPanel.hidden;
    if (open && window.toggleToc) window.toggleToc(false);
    settingsPanel.hidden = !open;
    settingsButton.setAttribute("aria-expanded", open ? "true" : "false");
  }
  if (settingsButton && settingsPanel) {
    settingsButton.addEventListener("click", function () { toggleSettings(); });
    document.getElementById("lr-settings-close").addEventListener("click", closeSettings);
    document.addEventListener("click", function (e) {
      if (settingsPanel.hidden) return;
      if (settingsPanel.contains(e.target) || settingsButton.contains(e.target)) return;
      closeSettings();
    });
  }

  // Keyboard: + - 0 zoom, w wrap, t contents box, p/r preview and raw.
  var previewLink = document.getElementById("lr-view-preview");
  var rawLink = document.getElementById("lr-view-raw");
  function goToView(link) {
    if (link && !link.classList.contains("on")) location.href = link.href;
  }
  document.addEventListener("keydown", function (e) {
    if (/^(INPUT|TEXTAREA)$/.test(e.target.tagName || "")) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (label && (e.key === "+" || e.key === "=")) stepZoom(1);
    else if (label && (e.key === "-" || e.key === "_")) stepZoom(-1);
    else if (label && e.key === "0") applyZoom(1);
    else if (wrapButton && e.key === "w") toggleWrap();
    else if (window.toggleToc && e.key === "t") window.toggleToc();
    else if (previewLink && e.key === "p") goToView(previewLink);
    else if (rawLink && e.key === "r") goToView(rawLink);
  });
})();
"""

# Directory listing: sorting and folder sizes.
LISTING_JS = r"""
(function () {
  var list = document.getElementById("lr-listing");
  var body = document.getElementById("lr-rows");
  if (!list || !body) return;

  var rows = Array.prototype.slice.call(body.querySelectorAll(".row[data-name]"));
  var DEFAULT_DIRECTION = { name: "asc", size: "desc", mtime: "desc" };
  var collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });
  var state = { key: "name", direction: "asc" };

  try {
    var saved = (sessionStorage.getItem("lr-sort") || "").split(":");
    if (DEFAULT_DIRECTION[saved[0]] && (saved[1] === "asc" || saved[1] === "desc")) {
      state = { key: saved[0], direction: saved[1] };
    }
  } catch (e) {}

  function sortRows() {
    var sign = state.direction === "asc" ? 1 : -1;
    rows.sort(function (a, b) {
      if (a.dataset.dir !== b.dataset.dir) return a.dataset.dir === "1" ? -1 : 1;  // folders stay first
      var result = state.key === "name"
        ? collator.compare(a.dataset.name, b.dataset.name)
        : (+a.dataset[state.key]) - (+b.dataset[state.key]);
      return result ? result * sign : collator.compare(a.dataset.name, b.dataset.name);
    });
    var fragment = document.createDocumentFragment();
    rows.forEach(function (row) { fragment.appendChild(row); });
    body.appendChild(fragment);
  }

  function markHeaders() {
    list.querySelectorAll("[data-key]").forEach(function (button) {
      var active = button.dataset.key === state.key;
      button.parentNode.setAttribute("aria-sort",
        active ? (state.direction === "asc" ? "ascending" : "descending") : "none");
    });
  }

  list.querySelector(".lhead").addEventListener("click", function (e) {
    var button = e.target.closest("[data-key]");
    if (!button) return;
    var key = button.dataset.key;
    state = key === state.key
      ? { key: key, direction: state.direction === "asc" ? "desc" : "asc" }
      : { key: key, direction: DEFAULT_DIRECTION[key] };
    try { sessionStorage.setItem("lr-sort", state.key + ":" + state.direction); } catch (err) {}
    sortRows();
    markHeaders();
  });

  markHeaders();
  if (state.key !== "name" || state.direction !== "asc") sortRows();

  // Folder sizes arrive one by one (two at a time) so a huge folder never blocks the page.
  var queue = rows.filter(function (row) { return row.dataset.dir === "1"; });
  var running = 0, resortTimer = null;

  function resortSoon() {
    if (state.key !== "size" || resortTimer) return;
    resortTimer = setTimeout(function () { resortTimer = null; sortRows(); }, 150);
  }

  function loadSize(row) {
    var cell = row.querySelector(".size");
    return fetch("/__size?path=" + encodeURIComponent(location.pathname + row.dataset.href))
      .then(function (response) { if (!response.ok) throw new Error(); return response.json(); })
      .then(function (info) {
        row.dataset.size = info.size;
        cell.textContent = info.text;
        if (info.partial) cell.title = "Partial total: counting stopped early because the folder is very large.";
      })
      .catch(function () { cell.textContent = "\u2014"; });
  }

  function pump() {
    while (running < 2 && queue.length) {
      running++;
      loadSize(queue.shift()).then(function () { running--; resortSoon(); pump(); });
    }
  }
  pump();
})();
"""

# Runs inside a Web Worker (built from a Blob), so it must stay self-contained.
# It formats minified JavaScript and highlights code off the main thread.
PREVIEW_JS = r"""
function workerMain() {
  self.window = self;  // some library builds look for `window`
  self.onmessage = function (event) {
    var job = event.data;
    try {
      var text = job.text;
      if (job.format) {
        importScripts(job.libs.beautify);
        text = self.js_beautify(text, { indent_size: 2 });
        self.postMessage({ type: "formatted", text: text });
      }
      if (job.highlight) {
        var tooBig = text.length > job.highlightLimit;
        var longLines = /[^\n]{20000}/.test(text);
        if (!job.force && (tooBig || longLines)) {
          self.postMessage({ type: "skipped", reason: tooBig ? "large" : "long" });
        } else {
          importScripts(job.libs.hljs);
          var result = self.hljs.highlight(text, { language: job.lang, ignoreIllegals: true });
          self.postMessage({ type: "highlighted", html: result.value });
        }
      }
      self.postMessage({ type: "done" });
    } catch (error) {
      self.postMessage({ type: "error", message: String((error && error.message) || error) });
    }
  };
}

// Runs in the page: fetches the raw file, renders it, builds the contents box.
(function () {
  var el = document.getElementById("lr-content");
  var note = document.getElementById("lr-note");
  var noteText = document.getElementById("lr-note-text");
  var actions = document.getElementById("lr-note-actions");
  var tocButton = document.getElementById("lr-toc-button");
  var toc = document.getElementById("lr-toc");
  var tocList = document.getElementById("lr-toc-list");
  var headings = [];
  var active = null;
  var loading = {};
  var notes = [];
  var status = "";
  var shown = false;

  function refreshNote() {
    noteText.textContent = notes.concat(status ? [status] : []).join(" ");
    note.hidden = !noteText.textContent && !actions.firstChild;
  }
  function addNote(message) { notes.push(message); refreshNote(); }
  function setStatus(message) { status = message; refreshNote(); }
  function finish() { window.restoreScroll(); }

  // Long code is split into blocks so the browser can skip layout for the parts
  // far from the viewport. Without this, toggling wrap re-lays out every line.
  var CHUNK_LINES = 200;
  function countLines(text) {
    var count = 0, at = -1;
    while ((at = text.indexOf("\n", at + 1)) !== -1) count++;
    return count;
  }

  function lineCut(text, wanted) {
    var at = -1, found = 0;
    while (found < wanted) {
      at = text.indexOf("\n", at + 1);
      if (at === -1) return -1;
      found++;
    }
    return at + 1;
  }

  function chunkContent() {
    var text = el.textContent;
    if (text.length < 200000 && countLines(text) < 1500) return;
    // Very long lines make each line expensive, so use smaller blocks there.
    var average = text.length / (countLines(text) + 1);
    CHUNK_LINES = average > 2000 ? 5 : average > 500 ? 20 : 200;
    // Without wrapping, the code block needs an explicit width. Chunks use paint
    // containment, so the browser otherwise forgets how far off-screen lines reach.
    var widest = 0;
    text.split("\n").forEach(function (line) {
      var width = line.length + 3 * (line.split("\t").length - 1);
      if (width > widest) widest = width;
    });
    el.style.setProperty("--lr-code-width", widest + "ch");
    var nodes = Array.prototype.slice.call(el.childNodes);
    var fragment = document.createDocumentFragment();
    var chunk = null, used = 0;

    function start() {
      chunk = document.createElement("div");
      chunk.className = "lr-chunk";
      fragment.appendChild(chunk);
      used = 0;
    }
    function add(node) {
      if (!chunk) start();
      chunk.appendChild(node);
      used += countLines(node.textContent || "");
      if (used >= CHUNK_LINES && (node.textContent || "").slice(-1) === "\n") chunk = null;
    }
    function addText(text) {
      while (text) {
        if (!chunk) start();
        var cut;
        if (used >= CHUNK_LINES) {
          var next = text.indexOf("\n");
          cut = next === -1 ? text.length : next + 1;
        } else {
          cut = lineCut(text, CHUNK_LINES - used);
          if (cut === -1) cut = text.length;
        }
        var part = text.slice(0, cut);
        chunk.appendChild(document.createTextNode(part));
        used += countLines(part);
        text = text.slice(cut);
        if (used >= CHUNK_LINES && part.slice(-1) === "\n") chunk = null;
      }
    }

    nodes.forEach(function (node) {
      if (node.nodeType === 3) addText(node.nodeValue);
      else add(node);
    });
    el.textContent = "";
    el.appendChild(fragment);
    el.classList.add("lr-chunked");
  }

  function showText(text) {
    el.classList.remove("lr-chunked");
    el.textContent = text;
    chunkContent();
    if (!shown) { shown = true; finish(); }
  }

  // Raw Markdown is still plain text, but heading lines are wrapped in spans so
  // the contents box can jump to them. Headings inside code fences are ignored.
  function renderRawMarkdown(text) {
    var fragment = document.createDocumentFragment();
    var buffer = "", fence = null;
    text.split("\n").forEach(function (line, i, lines) {
      var marker = /^\s*(`{3,}|~{3,})/.exec(line);
      var heading = fence === null && /^ {0,3}(#{1,4})\s+(.+?)\s*#*\s*$/.exec(line);
      if (heading) {
        if (buffer) { fragment.appendChild(document.createTextNode(buffer)); buffer = ""; }
        var span = document.createElement("span");
        span.dataset.tocLevel = heading[1].length;
        span.dataset.tocTitle = heading[2].trim();
        span.textContent = line;
        fragment.appendChild(span);
      } else {
        buffer += line;
      }
      if (marker) {
        var character = marker[1][0];
        fence = fence === character ? null : (fence === null ? character : fence);
      }
      if (i < lines.length - 1) buffer += "\n";
    });
    if (buffer) fragment.appendChild(document.createTextNode(buffer));
    el.textContent = "";
    el.appendChild(fragment);
    chunkContent();
    buildToc();
    if (!shown) { shown = true; finish(); }
  }

  function load(url) {
    if (!loading[url]) {
      loading[url] = new Promise(function (resolve, reject) {
        var script = document.createElement("script");
        script.src = url;
        script.onload = resolve;
        script.onerror = function () { reject(new Error("could not load a preview library (offline?)")); };
        document.head.appendChild(script);
      });
    }
    return loading[url];
  }

  function loadHighlightCss() {
    if (document.getElementById("lr-hljs-css")) return;
    var link = document.createElement("link");
    link.id = "lr-hljs-css";
    link.rel = "stylesheet";
    link.href = CFG.libs.hljsCss;
    document.head.appendChild(link);
  }

  function loadHighlighter() {
    loadHighlightCss();
    return load(CFG.libs.hljs);
  }

  // ---- background worker ----

  function runWorker(job, onMessage) {
    var url, worker, finished = false;
    function stop() {
      if (finished) return;
      finished = true;
      if (worker) worker.terminate();
      if (url) URL.revokeObjectURL(url);
    }
    try {
      url = URL.createObjectURL(new Blob(["(" + workerMain.toString() + ")()"], { type: "text/javascript" }));
      worker = new Worker(url);
    } catch (e) {
      onMessage({ type: "error", message: "Web Workers are unavailable" });
      stop();
      return;
    }
    worker.onmessage = function (event) {
      onMessage(event.data);
      if (event.data.type === "done" || event.data.type === "error") stop();
    };
    worker.onerror = function () {
      onMessage({ type: "error", message: "the background worker failed" });
      stop();
    };
    worker.postMessage(job);
  }

  function runJob(job, text) {
    var current = text;
    runWorker(job, function (m) {
      if (m.type === "formatted") {
        current = m.text;
        showText(current);
        addNote("Minified source detected: formatted for reading.");
        setStatus(job.highlight ? "Highlighting\u2026" : "");
      } else if (m.type === "highlighted") {
        el.innerHTML = m.html;
        el.className = "hljs language-" + job.lang;
        chunkContent();
      } else if (m.type === "skipped") {
        addNote(m.reason === "large"
          ? "Syntax highlighting skipped (large file)."
          : "Syntax highlighting skipped (very long lines).");
        offerHighlight(current);
      } else if (m.type === "error") {
        addNote("Could not " + (shown ? "highlight" : "format") + " this file (" + m.message + ").");
        setStatus("");
        if (!shown) showText(text);
      } else if (m.type === "done") {
        setStatus("");
      }
    });
  }

  function offerHighlight(text) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "linkbtn";
    button.textContent = "Highlight anyway";
    button.addEventListener("click", function () {
      actions.textContent = "";
      notes = notes.filter(function (n) { return n.indexOf("Syntax highlighting skipped") !== 0; });
      setStatus("Highlighting\u2026");
      runJob({ text: text, lang: CFG.lang, highlight: true, force: true,
               highlightLimit: CFG.highlightLimit, libs: CFG.libs }, text);
    });
    actions.appendChild(button);
    refreshNote();
  }

  // ---- contents box (Markdown only) ----

  function toggleToc(open) {
    if (!tocButton || tocButton.hidden) return;
    if (open === undefined) open = toc.hidden;
    if (open && window.closeSettings) window.closeSettings();
    toc.hidden = !open;
    tocButton.setAttribute("aria-expanded", open ? "true" : "false");
    tocButton.textContent = open ? "Close" : "Open";
    if (open) markCurrent();
  }

  function markCurrent(target) {
    var current = target || headings[0];
    if (!target) {
      for (var i = 0; i < headings.length; i++) {
        if (headings[i].el.getBoundingClientRect().top > 24) break;
        current = headings[i];
      }
    }
    if (active) active.classList.remove("on");
    active = current.link;
    active.classList.add("on");
    active.scrollIntoView({ block: "nearest" });
  }

  function headingLevel(element) {
    return element.dataset.tocLevel ? +element.dataset.tocLevel : +element.tagName[1];
  }

  function buildToc() {
    if (!tocButton) return;
    var found = el.querySelectorAll("h1,h2,h3,h4,[data-toc-level]");
    if (found.length < 2) return;
    var base = 6, counts = {}, fragment = document.createDocumentFragment();
    headings = [];
    tocList.textContent = "";
    found.forEach(function (h) { base = Math.min(base, headingLevel(h)); });
    found.forEach(function (h, i) {
      var title = (h.dataset.tocTitle || h.textContent).trim();
      var slug = title.toLowerCase()
        .replace(/[^\p{L}\p{N}\s-]/gu, "").replace(/\s/g, "-") || "section";
      var n = counts[slug] === undefined ? 0 : counts[slug] + 1;
      counts[slug] = n;
      h.id = n ? slug + "-" + n : slug;
      var link = document.createElement("a");
      link.href = "#" + h.id;
      link.textContent = title;
      link.dataset.i = i;
      link.style.paddingLeft = (10 + (headingLevel(h) - base) * 14) + "px";
      fragment.appendChild(link);
      headings.push({ el: h, id: h.id, link: link });
    });
    tocList.appendChild(fragment);
    var control = document.getElementById("lr-contents-control");
    if (control) control.hidden = false;
    tocButton.hidden = false;
  }

  if (tocButton) {
    window.toggleToc = toggleToc;
    tocButton.addEventListener("click", function () { toggleToc(); });
    document.getElementById("lr-toc-close").addEventListener("click", function () { toggleToc(false); });
    tocList.addEventListener("click", function (e) {
      var link = e.target.closest("a");
      if (!link) return;
      e.preventDefault();
      var heading = headings[+link.dataset.i];
      heading.el.scrollIntoView({ block: "start", inline: "nearest" });
      history.replaceState(null, "", "#" + heading.id);
      markCurrent(heading);
    });
  }

  // ---- renderers ----

  async function renderMarkdown(text) {
    await Promise.all([load(CFG.libs.marked), load(CFG.libs.purify)]);
    el.innerHTML = DOMPurify.sanitize(marked.parse(text));
    buildToc();
    finish();
    var blocks = Array.prototype.filter.call(el.querySelectorAll("pre code"), function (block) {
      return /language-\S+/.test(block.className);
    });
    if (!blocks.length) return;
    try {
      await loadHighlighter();
      blocks.forEach(function (block) {
        var name = /language-(\S+)/.exec(block.className)[1];
        if (hljs.getLanguage(name)) hljs.highlightElement(block);
      });
    } catch (e) { /* highlighting is optional */ }
  }

  function renderCode(text) {
    var lang = CFG.lang, format = false;
    if (CFG.kind === "json") {
      if (text.length > CFG.formatLimit) {
        addNote("File too large to pretty-print, showing it as-is.");
      } else {
        try { text = JSON.stringify(JSON.parse(text), null, 2); }
        catch (e) { addNote("Not valid JSON, showing the file as-is."); }
      }
    } else if (lang === "javascript" && (CFG.minHint || /[^\n]{500}/.test(text))) {
      if (text.length > CFG.formatLimit) addNote("Minified file is too large to format here, showing it as-is.");
      else format = true;
    }
    if (!format) showText(text);
    if (!lang) return;
    loadHighlightCss();
    setStatus(format ? "Formatting\u2026" : "Highlighting\u2026");
    runJob({ text: text, lang: lang, format: format, highlight: true,
             highlightLimit: CFG.highlightLimit, libs: CFG.libs }, text);
  }

  (async function () {
    var text = "";
    try {
      var response = await fetch(CFG.rawUrl);
      if (!response.ok) throw new Error(response.status + " " + response.statusText);
      text = await response.text();
      if (CFG.mode === "raw") {
        if (CFG.kind === "markdown") renderRawMarkdown(text);
        else showText(text);
      }
      else if (CFG.kind === "markdown") await renderMarkdown(text);
      else renderCode(text);
    } catch (e) {
      addNote("Preview failed (" + e.message + "). Showing the plain text.");
      el.textContent = text;
      el.style.whiteSpace = "pre-wrap";
      finish();
    }
  })();
})();
"""


def page(title, body, dock="", script=""):
    """Wrap body HTML in the shared dark page layout.

    The dock and page scripts come after the body so they can find every element they use.
    """
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="dark">'
        f"<title>{html.escape(title)}</title>"
        f"<script>{PAGE_INIT_JS}</script><style>{CSS}</style></head>"
        f"<body>{body}{dock}<script>{script}</script><script>{PAGE_JS}</script></body></html>"
    )


def dock_html(with_wrap=False, with_contents=False, view=""):
    """Floating settings button and the controls it opens."""
    controls = [
        '<div class="control"><span class="control-label">Zoom</span>'
        '<div class="zoom">'
        '<button id="lr-zoom-out" aria-label="Zoom out" title="Zoom out (-)">&minus;</button>'
        '<button id="lr-zoom-reset" aria-label="Reset zoom" title="Reset zoom (0)">100%</button>'
        '<button id="lr-zoom-in" aria-label="Zoom in" title="Zoom in (+)">+</button></div></div>'
    ]
    if with_wrap:
        controls.append(
            '<div class="control"><span class="control-label">Word wrap</span>'
            '<button id="lr-wrap" class="switch" type="button" aria-label="Toggle word wrap" '
            'title="Toggle word wrap (w)" aria-pressed="true">On</button></div>'
        )
    contents = ""
    if with_contents:
        controls.append(
            '<div class="control" id="lr-contents-control" hidden>'
            '<span class="control-label">Contents</span>'
            '<button id="lr-toc-button" class="switch" type="button" '
            'title="t / open or close the contents box" aria-expanded="false" '
            'aria-controls="lr-toc" hidden>Open</button></div>'
        )
        contents = (
            '<nav id="lr-toc" class="toc" aria-label="Contents" hidden>'
            '<div class="toc-head"><strong>Contents</strong>'
            '<button id="lr-toc-close" aria-label="Close contents">&times;</button></div>'
            '<div id="lr-toc-list" class="toc-list"></div></nav>'
        )
    if view:
        controls.append(view)
    gear = (
        '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<circle cx="12" cy="12" r="3"/>'
        '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06'
        'a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09'
        'a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06'
        'a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09'
        'a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06'
        'a1.65 1.65 0 0 0 1.82.33h.01a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09'
        'a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06'
        'a1.65 1.65 0 0 0-.33 1.82v.01a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09'
        'a1.65 1.65 0 0 0-1.51 1z"/></svg>'
    )
    return (
        '<div class="dock"><button id="lr-settings" class="fab" aria-label="Settings" '
        f'title="Settings" aria-expanded="false" aria-controls="lr-settings-panel">{gear}</button></div>'
        '<div id="lr-settings-panel" class="settings-panel" role="dialog" aria-label="Settings" hidden>'
        '<div class="panel-head"><strong>Settings</strong>'
        '<button id="lr-settings-close" aria-label="Close settings">&times;</button></div>'
        f'<div class="panel-body">{"".join(controls)}</div></div>{contents}'
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def is_inside(path, root):
    """True if path is root or lives below it (both must be real paths)."""
    return os.path.commonpath([root, path]) == root


def human_size(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def breadcrumbs(root_name, url_path):
    """Clickable path: parents are links, the current item is plain text."""
    parts = [part for part in unquote(url_path).split("/") if part]
    crumbs = [f'<a href="/">{html.escape(root_name)}</a>' if parts else html.escape(root_name)]
    href = "/"
    for part in parts[:-1]:
        href += quote(part) + "/"
        crumbs.append(f'<a href="{html.escape(href)}">{html.escape(part)}</a>')
    if parts:
        crumbs.append(html.escape(parts[-1]))
    return '<span class="sep">/</span>'.join(crumbs)


def view_control(url_path, raw_mode, force):
    """Preview / Raw switch shown inside the settings panel."""
    current = ' class="on" aria-current="page"'
    preview_url = url_path + ("?force=1" if force else "")
    raw_url = url_path + "?view=raw" + ("&force=1" if force else "")
    return (
        '<div class="control"><span class="control-label">View</span><div class="seg">'
        f'<a id="lr-view-preview" href="{html.escape(preview_url)}" title="Preview (p)"'
        f'{"" if raw_mode else current}>Preview</a>'
        f'<a id="lr-view-raw" href="{html.escape(raw_url)}" title="Raw (r)"'
        f'{current if raw_mode else ""}>Raw</a></div></div>'
    )


def is_text_file(path):
    """Cheap check on the first bytes: no NUL bytes and valid UTF-8."""
    try:
        with open(path, "rb") as file:
            sample = file.read(SNIFF_BYTES)
        if b"\0" in sample:
            return False
        codecs.getincrementaldecoder("utf-8")().decode(sample)  # tolerates a cut-off tail
        return True
    except (OSError, UnicodeDecodeError):
        return False


def classify(path, mime):
    """Return (kind, language) where kind is markdown/json/code/image/native/binary."""
    name = os.path.basename(path).lower()
    extension = os.path.splitext(name)[1]
    if extension in (".md", ".markdown"):
        return "markdown", "markdown"
    if extension in JSON_EXTENSIONS:
        return "json", "json"
    if extension in IMAGE_EXTENSIONS:
        return "image", ""
    if extension in LANGUAGES:
        return "code", LANGUAGES[extension]
    if name in FILENAME_LANGUAGES:
        return "code", FILENAME_LANGUAGES[name]
    if mime.startswith(("video/", "audio/")) or mime == "application/pdf":
        return "native", ""  # the browser's own viewer is better
    if mime.startswith("text/"):
        return "code", ""  # plain text, no highlighting
    return "binary", ""


def with_charset(mime):
    if mime.startswith("text/") or mime in ("application/json", "application/javascript"):
        return mime + "; charset=utf-8"
    return mime


def parse_range(header, size):
    """Parse a single 'bytes=a-b' range. Returns (start, end) or None to send the whole file."""
    if not header:
        return None
    try:
        unit, _, spec = header.partition("=")
        if unit.strip() != "bytes" or "," in spec:
            return None
        first, _, last = spec.strip().partition("-")
        if first == "":
            start, end = max(size - int(last), 0), size - 1
        else:
            start, end = int(first), min(int(last) if last else size - 1, size - 1)
        if start > end:
            return None
        return start, end
    except ValueError:
        return None


def inject_reload(data):
    """Insert the reload script before </body> (or append it)."""
    snippet = INJECTED_RELOAD_SCRIPT.encode()
    index = data.lower().rfind(b"</body>")
    if index == -1:
        return data + snippet
    return data[:index] + snippet + data[index:]


def describe_entry(entry, root):
    """Return (is_dir, size, mtime) or None if the entry must be hidden."""
    try:
        if entry.is_symlink() and not is_inside(os.path.realpath(entry.path), root):
            return None  # symlink pointing outside the served root
        info = entry.stat()
        return stat.S_ISDIR(info.st_mode), info.st_size, info.st_mtime
    except OSError:
        return None  # broken link or unreadable entry


def scan_folder_size(path):
    """Total size of the regular files below path (symlinks are not followed).

    Returns (bytes, complete). complete is False when a limit stopped the count.
    """
    total = 0
    seen = 0
    deadline = time.monotonic() + FOLDER_SIZE_SECONDS
    pending = [path]
    while pending:
        try:
            with os.scandir(pending.pop()) as iterator:
                for entry in iterator:
                    seen += 1
                    if seen > FOLDER_SIZE_LIMIT or (seen % 1024 == 0 and time.monotonic() > deadline):
                        return total, False
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue  # unreadable folder: skip it
    return total, True


def render_listing(root, root_name, url_path, entries):
    rows = []
    for entry in entries:
        details = describe_entry(entry, root)
        if details:
            rows.append((entry.name, *details))
    rows.sort(key=lambda row: (not row[1], row[0].lower()))

    items = []
    if url_path != "/":
        items.append('<div class="row up" role="row"><a class="name" href="../">..</a></div>')
    for name, is_dir, size, mtime in rows:
        href = html.escape(quote(name) + ("/" if is_dir else ""))
        modified = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        items.append(
            '<div class="row" role="row" data-name="%s" data-dir="%d" data-size="%d" data-mtime="%d" '
            'data-href="%s"><a class="name" href="%s">%s</a><span class="size">%s</span>'
            '<span class="date">%s</span></div>'
            % (html.escape(name), is_dir, -1 if is_dir else size, mtime, href, href,
               html.escape(name), "\u2026" if is_dir else human_size(size), modified)
        )
    if not rows:
        items.append('<div class="empty">This folder is empty</div>')

    header = (
        '<div class="lhead" role="row">'
        '<div class="c-name" role="columnheader" aria-sort="ascending">'
        '<button type="button" data-key="name">Name<span class="arrow"></span></button></div>'
        '<div class="c-size" role="columnheader" aria-sort="none">'
        '<button type="button" data-key="size">Size<span class="arrow"></span></button></div>'
        '<div class="c-date" role="columnheader" aria-sort="none">'
        '<button type="button" data-key="mtime">Modified<span class="arrow"></span></button></div></div>'
    )
    body = (
        f'<header><nav class="crumbs">{breadcrumbs(root_name, url_path)}</nav>'
        f'<span class="meta">{len(rows)} items</span></header>'
        f'<main><div class="listing" id="lr-listing" role="table" aria-label="Directory listing">'
        f'{header}<div id="lr-rows">{"".join(items)}</div></div></main>'
    )
    return page(f"Index of {unquote(url_path)}", body, dock_html(), LISTING_JS)


# --------------------------------------------------------------------------
# Change detection (inotify)
# --------------------------------------------------------------------------

IN_MODIFY, IN_MOVED_FROM, IN_MOVED_TO = 0x2, 0x40, 0x80
IN_CREATE, IN_DELETE = 0x100, 0x200
IN_IGNORED, IN_ISDIR = 0x8000, 0x40000000
WATCH_MASK = IN_MODIFY | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE


class ChangeNotifier:
    """Holds a version number that increases on every file change."""

    def __init__(self):
        self.version = int(time.time() * 1000)
        self._condition = threading.Condition()

    def bump(self):
        with self._condition:
            self.version += 1
            self._condition.notify_all()

    def wait(self, seen_version, timeout):
        """Block until the version differs from seen_version or the timeout passes."""
        with self._condition:
            self._condition.wait_for(lambda: self.version != seen_version, timeout)
            return self.version


class DirectoryWatcher(threading.Thread):
    """Recursively watches a directory tree. Sleeps in the kernel until something changes."""

    def __init__(self, root, notifier):
        super().__init__(daemon=True)
        libc = ctypes.CDLL(None, use_errno=True)
        libc.inotify_init1.argtypes = [ctypes.c_int]
        libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._add_watch = libc.inotify_add_watch
        self._fd = libc.inotify_init1(os.O_CLOEXEC)
        if self._fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self._notifier = notifier
        self._directories = {}  # watch descriptor -> directory path
        self.failed = 0         # directories that could not be watched
        self._watch_tree(root)

    def _watch_tree(self, top):
        for dirpath, dirnames, _ in os.walk(top):
            dirnames[:] = [name for name in dirnames if name not in IGNORED_DIRS]
            descriptor = self._add_watch(self._fd, os.fsencode(dirpath), WATCH_MASK)
            if descriptor >= 0:
                self._directories[descriptor] = dirpath
            else:
                self.failed += 1

    def _read_events(self):
        data = os.read(self._fd, 65536)
        offset = 0
        while offset < len(data):
            descriptor, mask, _cookie, name_length = struct.unpack_from("iIII", data, offset)
            name = os.fsdecode(data[offset + 16:offset + 16 + name_length].rstrip(b"\0"))
            offset += 16 + name_length
            if mask & IN_IGNORED:
                self._directories.pop(descriptor, None)
            elif (mask & IN_ISDIR and mask & (IN_CREATE | IN_MOVED_TO)
                  and name not in IGNORED_DIRS and descriptor in self._directories):
                self._watch_tree(os.path.join(self._directories[descriptor], name))

    def run(self):
        while True:
            self._read_events()
            # Debounce: wait until events stop arriving, then notify once.
            while select.select([self._fd], [], [], DEBOUNCE_SECONDS)[0]:
                self._read_events()
            self._notifier.bump()


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "livereader"
    sys_version = ""

    def log_message(self, format, *args):
        pass  # keep the terminal quiet

    # -- routing ----------------------------------------------------------

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == EVENTS_PATH:
            return self.serve_events()
        if url.path == SIZE_PATH:
            return self.serve_size(parse_qs(url.query).get("path", [""])[0])

        full_path = self.resolve(url.path)
        if full_path is None:
            return self.send_error_page(403, "Forbidden", "That path is outside the served directory.")
        try:
            info = os.stat(full_path)
        except (FileNotFoundError, NotADirectoryError):
            return self.send_error_page(404, "Not found", "Nothing exists at this path.")
        except PermissionError:
            return self.send_error_page(403, "Forbidden", "Permission denied.")
        except OSError as error:
            return self.send_error_page(500, "Server error", error.strerror or "Unexpected error.")

        if stat.S_ISDIR(info.st_mode):
            self.serve_directory(full_path, url)
        else:
            self.serve_file(full_path, url)

    def resolve(self, url_path):
        """Map a URL path to a real path inside the root, or None if it escapes the root."""
        relative = unquote(url_path).lstrip("/")
        if "\0" in relative:
            return None
        full_path = os.path.realpath(os.path.join(self.server.root, relative))
        return full_path if is_inside(full_path, self.server.root) else None

    # -- responses --------------------------------------------------------

    def send_text(self, status, content, content_type="text/html; charset=utf-8"):
        data = content.encode("utf-8") if isinstance(content, str) else content
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_error_page(self, status, title, message):
        body = (
            f'<header><nav class="crumbs">{html.escape(self.server.root_name)}</nav></header>'
            f'<main><div class="notice"><h1>{status} {html.escape(title)}</h1>'
            f'<p>{html.escape(message)}</p><a href="/">Back to the root</a></div></main>'
        )
        self.send_text(status, page(f"{status} {title}", body))

    def serve_events(self):
        """Server-Sent Events: sends the current version on connect, on change and as heartbeat."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.close_connection = True
        notifier = self.server.notifier
        version = notifier.version
        try:
            while True:
                self.wfile.write(f"data: {version}\n\n".encode())
                self.wfile.flush()
                version = notifier.wait(version, HEARTBEAT_SECONDS)
        except ConnectionError:
            pass  # browser tab closed

    def serve_size(self, url_path):
        """JSON with the total size of one folder, for the directory listing."""
        target = self.resolve(url_path)
        if target is None or not os.path.isdir(target):
            return self.send_text(404, "{}", "application/json")
        size, complete = self.server.folder_size(target)
        payload = {"size": size, "text": ("" if complete else "\u2265 ") + human_size(size),
                   "partial": not complete}
        self.send_text(200, json.dumps(payload), "application/json")

    def serve_directory(self, directory, url):
        if not url.path.endswith("/"):  # relative links need the trailing slash
            self.send_response(301)
            self.send_header("Location", url.path + "/" + (f"?{url.query}" if url.query else ""))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        index = os.path.join(directory, "index.html")
        if os.path.isfile(index):
            return self.serve_html(index)
        try:
            with os.scandir(directory) as iterator:
                entries = list(iterator)
        except PermissionError:
            return self.send_error_page(403, "Forbidden", "Permission denied.")
        except OSError as error:
            return self.send_error_page(500, "Server error", error.strerror or "Unexpected error.")
        listing = render_listing(self.server.root, self.server.root_name, url.path, entries)
        self.send_text(200, listing)

    def serve_file(self, path, url):
        mime = mimetypes.guess_type(path)[0]
        if mime is None:
            mime = "text/plain" if is_text_file(path) else "application/octet-stream"
        if mime == "text/html":
            return self.serve_html(path)

        # Only page navigations get the reader view. Sub-resource requests
        # (<script src>, fetch, <img>, ...) and tools like curl receive the raw bytes,
        # as does any URL with ?raw=1.
        query = parse_qs(url.query, keep_blank_values=True)
        is_navigation = self.headers.get("Sec-Fetch-Dest") == "document"
        if is_navigation and "raw" not in query:
            kind, language = classify(path, mime)
            if kind != "native":
                mode = "raw" if query.get("view") == ["raw"] else "preview"
                return self.serve_preview(path, url, kind, language, mode, "force" in query)
        self.serve_raw(path, mime)

    def serve_html(self, path):
        """HTML files are served as-is with the reload script injected."""
        try:
            if os.path.getsize(path) > INJECT_LIMIT:
                return self.serve_raw(path, "text/html")
            with open(path, "rb") as file:
                data = file.read()
        except PermissionError:
            return self.send_error_page(403, "Forbidden", "Permission denied.")
        except OSError as error:
            return self.send_error_page(500, "Server error", error.strerror or "Unexpected error.")
        self.send_text(200, inject_reload(data), "text/html; charset=utf-8")

    def serve_raw(self, path, mime):
        """Stream a file in chunks, with single-range support for media seeking."""
        if mime == "text/markdown":
            mime = "text/plain"  # browsers download text/markdown instead of showing it
        try:
            file = open(path, "rb")
        except PermissionError:
            return self.send_error_page(403, "Forbidden", "Permission denied.")
        except OSError as error:
            return self.send_error_page(404, "Not found", error.strerror or "Cannot open file.")
        with file:
            size = os.fstat(file.fileno()).st_size
            byte_range = parse_range(self.headers.get("Range"), size)
            start, end = byte_range or (0, size - 1)
            self.send_response(206 if byte_range else 200)
            self.send_header("Content-Type", with_charset(mime))
            self.send_header("Content-Length", str(max(end - start + 1, 0)))
            self.send_header("Accept-Ranges", "bytes")
            if byte_range:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            file.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = file.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def serve_preview(self, path, url, kind, language, mode, force):
        """Reader page. The browser fetches the raw file and renders it itself."""
        try:
            size = os.path.getsize(path)
        except OSError as error:
            return self.send_error_page(500, "Server error", error.strerror or "Unexpected error.")
        name = os.path.basename(path)
        raw_url = url.path + "?raw=1"
        raw_mode = mode == "raw"
        view = ""
        dock = ""
        script = ""

        if kind == "image":
            content = f'<div class="image"><img src="{html.escape(raw_url)}" alt="{html.escape(name)}"></div>'
        elif kind == "binary":
            content = '<div class="notice">No preview is available for this file type.</div>'
        elif size > PREVIEW_LIMIT and not force:
            again = url.path + ("?view=raw&force=1" if raw_mode else "?force=1")
            content = (f'<div class="notice">This file is {human_size(size)}, above the '
                       f'{human_size(PREVIEW_LIMIT)} render limit. '
                       f'<a href="{html.escape(again)}">Render anyway</a> (it may be slow) or '
                       f'<a href="{html.escape(raw_url)}">open the raw file</a>.</div>')
        else:
            if kind != "code" or language:  # plain text looks the same in both views
                view = view_control(url.path, raw_mode, force)
            config = json.dumps({
                "kind": kind, "lang": language, "mode": mode, "rawUrl": raw_url,
                "minHint": ".min." in name.lower(),
                "formatLimit": FORMAT_LIMIT, "highlightLimit": HIGHLIGHT_LIMIT, "libs": LIBS,
            }).replace("<", "\\u003c")
            if kind == "markdown" and not raw_mode:
                target = '<article id="lr-content" class="markdown"></article>'
            else:
                target = '<pre><code id="lr-content"></code></pre>'
            content = ('<div id="lr-note" class="note" hidden><span id="lr-note-text"></span>'
                       f'<span id="lr-note-actions"></span></div>{target}')
            dock = dock_html(with_wrap=not (kind == "markdown" and not raw_mode),
                             with_contents=kind == "markdown", view=view)
            script = f"const CFG={config};{PREVIEW_JS}"

        body = (
            f'<header><nav class="crumbs">{breadcrumbs(self.server.root_name, url.path)}</nav>'
            f'<div class="meta"><span>{human_size(size)}</span></div></header>'
            f"<main>{content}</main>"
        )
        self.send_text(200, page(name, body, dock, script))


class LiveServer(ThreadingHTTPServer):
    daemon_threads = True  # open event streams must not block shutdown

    def __init__(self, address, root):
        super().__init__(address, Handler)  # binding here is the real availability check
        self.root = root
        self.root_name = os.path.basename(root) or "/"
        self.notifier = ChangeNotifier()
        self._size_cache = {}  # folder -> (change version, time, size, complete)

    def folder_size(self, path):
        """Cached folder size. An entry is reused until files change or it gets old."""
        now = time.monotonic()
        version = self.notifier.version
        cached = self._size_cache.get(path)
        if cached and cached[0] == version and now - cached[1] < SIZE_CACHE_SECONDS:
            return cached[2], cached[3]
        size, complete = scan_folder_size(path)
        if len(self._size_cache) > 1000:
            self._size_cache.clear()
        self._size_cache[path] = (version, now, size, complete)
        return size, complete

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], ConnectionError):  # ignore closed tabs
            super().handle_error(request, client_address)


def create_server(root):
    """Bind to a random free port; fall back to an OS-assigned port."""
    candidates = random.sample(range(*PORT_RANGE), PORT_ATTEMPTS) + [0]
    for port in candidates:
        try:
            return LiveServer((HOST, port), root)
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise
    raise OSError("could not find a free port")


def main():
    parser = argparse.ArgumentParser(
        prog="livereader",
        description="Read files in your browser: file browser, clean previews and automatic reload.")
    parser.add_argument("directory", nargs="?", default=".",
                        help="directory to serve (default: current directory)")
    parser.add_argument("-V", "--version", action="version", version=f"livereader {__version__}")
    args = parser.parse_args()

    root = os.path.realpath(os.path.expanduser(args.directory))
    if not os.path.isdir(root):
        sys.exit(f"livereader: not a directory: {args.directory}")
    if not os.access(root, os.R_OK | os.X_OK):
        sys.exit(f"livereader: permission denied: {args.directory}")

    try:
        server = create_server(root)
    except OSError as error:
        sys.exit(f"livereader: cannot start server: {error}")

    try:
        watcher = DirectoryWatcher(root, server.notifier)
        watcher.start()
        if watcher.failed:
            print(f"warning: {watcher.failed} directories could not be watched "
                  "(inotify watch limit?)", file=sys.stderr)
    except (OSError, AttributeError) as error:
        print(f"warning: automatic reload disabled ({error})", file=sys.stderr)

    host, port = server.server_address[:2]
    print(f"Serving  {root}")
    print(f"Local    http://{host}:{port}/")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
