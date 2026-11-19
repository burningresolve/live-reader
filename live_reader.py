#!/usr/bin/env python3
"""live-server: a small static file server with a directory listing,
dark file previews and automatic browser reload.
 
Python standard library only. Linux only (auto-reload uses inotify).
 
Usage:
    live-server                 serve the current directory
    live-server /path/to/dir    serve a specific directory
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
 
# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
 
HOST = "127.0.0.1"                  # localhost only: files are never exposed to the network
PORT_RANGE = (40000, 50000)         # rarely used by other software
PORT_ATTEMPTS = 50
PREVIEW_LIMIT = 1024 * 1024         # larger text files are not previewed (raw link only)
HIGHLIGHT_LIMIT = 200 * 1024        # larger text files are shown without highlighting
INJECT_LIMIT = 20 * 1024 * 1024     # larger HTML files are served without reload script
SNIFF_BYTES = 8192
CHUNK_SIZE = 64 * 1024
DEBOUNCE_SECONDS = 0.15
HEARTBEAT_SECONDS = 15
EVENTS_PATH = "/__events"
IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache"}
 
# Browser libraries for previews (pinned versions, cached by the browser).
HLJS_CSS = "https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11.9.0/styles/github-dark.min.css"
HLJS_JS = "https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11.9.0/highlight.min.js"
MARKED_JS = "https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"
PURIFY_JS = "https://cdn.jsdelivr.net/npm/dompurify@3.1.2/dist/purify.min.js"
BEAUTIFY_JS = "https://cdn.jsdelivr.net/npm/js-beautify@1.15.1/js/lib/beautify.js"
 
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp", ".avif"}
 
LANGUAGES = {
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
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
# Page templates
# --------------------------------------------------------------------------
 
CSS = """
*{box-sizing:border-box}
body{margin:0;background:#0d1117;color:#e6edf3;font:14px/1.5 -apple-system,"Segoe UI",Ubuntu,Roboto,sans-serif}
a{color:#58a6ff;text-decoration:none}a:hover{text-decoration:underline}
header{display:flex;flex-wrap:wrap;gap:8px 16px;justify-content:space-between;align-items:center;
 padding:12px 16px;background:#161b22;border-bottom:1px solid #30363d;position:sticky;top:0;z-index:1}
.crumbs{font-weight:600;word-break:break-all}
.sep{color:#6e7681;margin:0 6px}
.meta{color:#8b949e;display:flex;gap:12px;align-items:center}
main{max-width:1100px;margin:0 auto;padding:16px}
table{width:100%;border-collapse:collapse}
th{text-align:left;color:#8b949e;font-weight:500;font-size:12px;text-transform:uppercase}
th,td{padding:8px 12px;border-bottom:1px solid #21262d}
tbody tr:hover{background:#161b22}
td.name{width:100%;word-break:break-all}
.dir a{font-weight:600}
.dir a:not([href="../"])::after{content:"/"}
.size,.date{color:#8b949e;white-space:nowrap}
.size{text-align:right}
.empty{color:#8b949e;text-align:center;padding:32px}
.notice{padding:24px;background:#161b22;border:1px solid #30363d;border-radius:6px;color:#8b949e}
.notice h1{margin:0 0 8px;color:#e6edf3;font-size:20px}
.note{color:#d29922;margin-bottom:8px}
.note:empty{display:none}
.image{text-align:center;padding:16px;background:#161b22;border:1px solid #30363d;border-radius:6px}
.image img{max-width:100%;height:auto}
pre{margin:0;padding:16px;overflow:auto;background:#161b22;border:1px solid #30363d;border-radius:6px;
 font:13px/1.45 "Ubuntu Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;tab-size:4}
pre code,pre code.hljs{padding:0;background:transparent}
.markdown{line-height:1.6;word-wrap:break-word}
.markdown h1,.markdown h2,.markdown h3,.markdown h4{margin:24px 0 16px;line-height:1.25}
.markdown h1,.markdown h2{padding-bottom:.3em;border-bottom:1px solid #30363d}
.markdown code{padding:.2em .4em;background:rgba(110,118,129,.4);border-radius:6px;font-size:85%;
 font-family:"Ubuntu Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.markdown pre{margin:16px 0}
.markdown pre code{padding:0;background:none;font:inherit}
.markdown blockquote{margin:0;padding:0 1em;color:#8b949e;border-left:.25em solid #30363d}
.markdown table{width:auto;display:block;overflow:auto}
.markdown th,.markdown td{border:1px solid #30363d;padding:6px 13px}
.markdown th{text-transform:none;font-size:inherit;color:inherit;font-weight:600}
.markdown img{max-width:100%}
.markdown hr{border:0;border-top:1px solid #30363d}
@media(max-width:600px){.date{display:none}main{padding:8px}}
"""
 
# Reconnects after a server restart; reloads only when the version changed.
RELOAD_SCRIPT = (
    '<script>(function(){var v=null,es=new EventSource("%s");'
    "es.onmessage=function(e){if(v!==null&&e.data!==v)location.reload();v=e.data;};})();</script>"
    % EVENTS_PATH
)
 
# Runs in the browser: fetches the raw file and renders it.
PREVIEW_JS = """
(async function () {
  var el = document.getElementById("content");
  var note = document.getElementById("note");
  var text = "";
  try {
    var res = await fetch(CFG.rawUrl);
    if (!res.ok) throw new Error(res.status + " " + res.statusText);
    text = await res.text();
    if (CFG.kind === "markdown") {
      el.innerHTML = DOMPurify.sanitize(marked.parse(text));
      el.querySelectorAll("pre code").forEach(function (block) {
        var m = /language-(\\S+)/.exec(block.className);
        if (m && hljs.getLanguage(m[1])) hljs.highlightElement(block);
      });
      return;
    }
    if (CFG.kind === "json") {
      try { text = JSON.stringify(JSON.parse(text), null, 2); }
      catch (e) { note.textContent = "Invalid JSON, showing the file as-is: " + e.message; }
    } else if (CFG.lang === "javascript" && /[^\\n]{500}/.test(text)) {
      text = js_beautify(text, { indent_size: 2 });
      note.textContent = "Minified source detected: beautified for display.";
    }
    el.textContent = text;
    if (CFG.lang && window.hljs) {
      if (text.length > CFG.highlightLimit) {
        note.textContent += " Syntax highlighting skipped (large file).";
      } else if (hljs.getLanguage(CFG.lang)) {
        el.className = "language-" + CFG.lang;
        hljs.highlightElement(el);
      }
    }
  } catch (e) {
    note.textContent = "Preview failed (" + e.message + "). Showing plain text.";
    el.textContent = text;
    el.style.whiteSpace = "pre-wrap";
  }
})();
"""
 
 
def page(title, body, head=""):
    """Wrap body HTML in the shared dark page layout (includes live reload)."""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title>{head}<style>{CSS}</style></head>"
        f"<body>{body}{RELOAD_SCRIPT}</body></html>"
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
    crumbs = []
    href = "/"
    if parts:
        crumbs.append(f'<a href="/">{html.escape(root_name)}</a>')
    else:
        crumbs.append(html.escape(root_name))
    for part in parts[:-1]:
        href += quote(part) + "/"
        crumbs.append(f'<a href="{html.escape(href)}">{html.escape(part)}</a>')
    if parts:
        crumbs.append(html.escape(parts[-1]))
    return '<span class="sep">/</span>'.join(crumbs)
 
 
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
    if extension == ".json":
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
    snippet = RELOAD_SCRIPT.encode()
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
 
 
def render_listing(root, root_name, url_path, entries):
    rows = []
    for entry in entries:
        details = describe_entry(entry, root)
        if details:
            rows.append((entry.name, *details))
    rows.sort(key=lambda row: (not row[1], row[0].lower()))
 
    html_rows = []
    if url_path != "/":
        html_rows.append('<tr><td class="name dir"><a href="../">..</a></td>'
                         '<td class="size"></td><td class="date"></td></tr>')
    for name, is_dir, size, mtime in rows:
        href = quote(name) + ("/" if is_dir else "")
        modified = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        html_rows.append(
            f'<tr><td class="name{" dir" if is_dir else ""}"><a href="{href}">{html.escape(name)}</a></td>'
            f'<td class="size">{"" if is_dir else human_size(size)}</td>'
            f'<td class="date">{modified}</td></tr>'
        )
    if not rows:
        html_rows.append('<tr><td colspan="3" class="empty">This folder is empty</td></tr>')
 
    body = (
        f'<header><nav class="crumbs">{breadcrumbs(root_name, url_path)}</nav>'
        f'<span class="meta">{len(rows)} items</span></header>'
        '<main><table><thead><tr><th>Name</th><th class="size">Size</th><th class="date">Modified</th></tr></thead>'
        f'<tbody>{"".join(html_rows)}</tbody></table></main>'
    )
    return page(f"Index of {unquote(url_path)}", body)
 
 
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
    server_version = "live-server"
    sys_version = ""
 
    def log_message(self, format, *args):
        pass  # keep the terminal quiet
 
    # -- routing ----------------------------------------------------------
 
    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == EVENTS_PATH:
            return self.serve_events()
 
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
 
        # Only direct page navigations get the pretty preview. Sub-resource requests
        # (<script src>, fetch, <img>, ...) and tools like curl receive the raw file.
        is_navigation = self.headers.get("Sec-Fetch-Dest") == "document"
        wants_raw = "raw" in parse_qs(url.query, keep_blank_values=True)
        if is_navigation and not wants_raw:
            kind, language = classify(path, mime)
            if kind != "native":
                return self.serve_preview(path, url, kind, language)
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
 
    def serve_preview(self, path, url, kind, language):
        """Dark preview page. The browser fetches and renders the file itself."""
        try:
            size = os.path.getsize(path)
        except OSError as error:
            return self.send_error_page(500, "Server error", error.strerror or "Unexpected error.")
        name = os.path.basename(path)
        raw_url = html.escape(url.path + "?raw=1")
        head = ""
 
        if kind == "image":
            content = f'<div class="image"><img src="{raw_url}" alt="{html.escape(name)}"></div>'
        elif kind == "binary":
            content = '<div class="notice">No preview is available for this file type.</div>'
        elif size > PREVIEW_LIMIT:
            content = (f'<div class="notice">This file is {human_size(size)}, which is too large '
                       "to preview. Use the Raw or Download links above.</div>")
        else:
            scripts = ""
            if language:
                head = f'<link rel="stylesheet" href="{HLJS_CSS}">'
                scripts += f'<script src="{HLJS_JS}"></script>'
            if kind == "markdown":
                scripts += f'<script src="{MARKED_JS}"></script><script src="{PURIFY_JS}"></script>'
            if language == "javascript":
                scripts += f'<script src="{BEAUTIFY_JS}"></script>'
            config = json.dumps({"kind": kind, "lang": language, "rawUrl": url.path + "?raw=1",
                                 "highlightLimit": HIGHLIGHT_LIMIT}).replace("<", "\\u003c")
            if kind == "markdown":
                target = '<article id="content" class="markdown"></article>'
            else:
                target = '<pre><code id="content"></code></pre>'
            content = (f'<div id="note" class="note"></div>{target}{scripts}'
                       f"<script>const CFG={config};{PREVIEW_JS}</script>")
 
        body = (
            f'<header><nav class="crumbs">{breadcrumbs(self.server.root_name, url.path)}</nav>'
            f'<span class="meta">{human_size(size)}<a href="{raw_url}">Raw</a>'
            f'<a href="{raw_url}" download>Download</a></span></header>'
            f"<main>{content}</main>"
        )
        self.send_text(200, page(name, body, head))
 
 
class LiveServer(ThreadingHTTPServer):
    daemon_threads = True  # open event streams must not block shutdown
 
    def __init__(self, address, root):
        super().__init__(address, Handler)  # binding here is the real availability check
        self.root = root
        self.root_name = os.path.basename(root) or "/"
        self.notifier = ChangeNotifier()
 
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
        prog="live-server",
        description="Serve a directory with a file browser, previews and automatic reload.")
    parser.add_argument("directory", nargs="?", default=".",
                        help="directory to serve (default: current directory)")
    args = parser.parse_args()
 
    root = os.path.realpath(os.path.expanduser(args.directory))
    if not os.path.isdir(root):
        sys.exit(f"live-server: not a directory: {args.directory}")
    if not os.access(root, os.R_OK | os.X_OK):
        sys.exit(f"live-server: permission denied: {args.directory}")
 
    try:
        server = create_server(root)
    except OSError as error:
        sys.exit(f"live-server: cannot start server: {error}")
 
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
