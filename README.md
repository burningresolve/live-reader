# livereader

A tiny file reader for your browser. Point it at a folder and read Markdown, JSON, source code and more in a clean dark interface. Pages reload on their own when files change.

Python standard library only. No install step, no build step, no background services.

## Features

- **Made for reading.** Markdown is rendered with a GitHub Dark look. JSON is pretty-printed. Source code is syntax highlighted. Minified JavaScript is formatted automatically.
- **Works with real-world JavaScript.** Minified files, bundles, ES modules, JSX, obfuscated code and `.map` source maps are all handled. Formatting and highlighting run in a background worker, so the page stays responsive on large files.
- **Settings panel.** Zoom, word wrap, the Markdown contents box and the Preview/Raw switch are grouped behind one floating button, so the page stays clean.
- **Contents box for Markdown.** Open it from the settings panel or with `t`. Pick a heading to jump straight to that section.
- **Word wrap toggle.** Turn wrapping on or off from the settings panel or with `w`, like in VS Code. Wrapped text reflows as you resize the window.
- **Per-tab zoom.** Zoom in and out from the settings panel. The zoom level belongs to the tab, so other tabs and your browser's own zoom are not affected.
- **Preview / Raw switch.** Flip between the rendered view and the exact file contents from the settings panel, or press `p` / `r`.
- **Sortable file list.** Folders show their total size. Click Name, Size or Modified to sort in either direction.
- **Automatic reload.** Create, edit, rename or delete a file and the page refreshes. Your scroll position is kept.
- **Responsive.** Comfortable in narrow and half-screen windows as well as full screen.
- **Light on resources.** File watching uses the Linux kernel (inotify), so nothing polls. Rendering happens in the browser, and libraries are only downloaded when a file needs them. Long code is split into blocks, and the browser only lays out the blocks near the viewport, so word wrap and zoom stay fast even on large files.
- **Free port.** A random free port is chosen on every start, so it never conflicts with your other servers.
- **Safe by default.** Listens on `127.0.0.1` only and never serves anything outside the folder you chose.

## Requirements

- Linux (uses inotify). Developed for Ubuntu 26.04 LTS.
- Python 3.9 or newer (included with Ubuntu).
- A modern browser (Firefox or a Chromium-based browser).
- Internet access the first time you preview Markdown, JSON, code or minified JavaScript.

## Installation

Clone or download this repository, then run these commands from the project folder:

```bash
cd live-reader && ln -s "$PWD/live_reader.py" ~/.local/bin/livereader
```

## Usage

```bash
livereader                  # serve the current directory
livereader ~/notes          # serve a specific directory
livereader --help
```

The server prints its address, for example:

```
Serving  /home/you/notes
Local    http://127.0.0.1:43127/
Press Ctrl+C to stop.
```

Open the address in your browser and click a file to read it.

### Reading controls

| Control | Action |
| --- | --- |
| Settings button (bottom right) | Open the panel with the zoom, word wrap, contents and view controls |
| `−` / `100%` / `+` | Zoom out, reset, zoom in |
| Word wrap **On** / **Off** | Turn word wrap on or off |
| Contents **Open** / **Close** (Markdown only) | Open or close the list of sections |
| View **Preview** / **Raw** | Switch between the rendered view and the exact file contents |
| `+` `-` `0` | Zoom in, zoom out, reset zoom |
| `w` | Toggle word wrap |
| `t` | Open or close the contents box |
| `p` / `r` | Switch to Preview / Raw |

Zoom, word wrap and the file list sort order are remembered per tab. Word wrap is on by default.

### File list

Click **Name**, **Size** or **Modified** to sort. Click the same column again to reverse the order. Folders always stay above files.

Folder sizes are counted when you open the list, a couple of folders at a time, so a huge folder never blocks the page. Sizes are the total of the files inside, without following symlinks. A folder with an enormous number of files shows `≥` and a partial total after the counting limit is reached.

### Supported files

| Type | Behaviour |
| --- | --- |
| Markdown (`.md`) | Rendered, with a contents box and highlighted code blocks |
| JSON (`.json`, `.map`, `.geojson`, `.webmanifest`) | Pretty-printed and highlighted |
| JavaScript (`.js`, `.mjs`, `.cjs`, `.jsx`) | Highlighted, formatted first if minified |
| Other code (TypeScript, Python, shell, CSS, YAML, Go, Rust, SQL and more) | Highlighted |
| Plain text (`.txt`, `.log`, `.csv`, unknown text files) | Shown as text |
| Images (PNG, JPEG, GIF, WebP, SVG and more) | Shown on a dark background |
| PDF, audio, video | Opened in the browser's own viewer |
| HTML | Served as a normal web page with auto-reload added |
| Anything else | A notice instead of a broken page |

A folder that contains an `index.html` serves that page instead of the file list.

### Size limits

The browser has to hold a whole file as text, so large files are handled in steps. All limits are constants at the top of `livereader.py`.

| Limit | Default | What happens above it |
| --- | --- | --- |
| Render | 10 MiB | A notice with a **Render anyway** link (it may be slow) and a link to the raw file |
| Format (minified JS, JSON) | 8 MiB | The file is shown as-is, not reformatted |
| Highlight | 3 MiB, measured after formatting | The text is shown without colours and a **Highlight anyway** button appears |

Highlighting is also skipped for files that contain lines longer than 20,000 characters, with the same button to override it.

## How it works

- The server is a small `ThreadingHTTPServer` from the standard library.
- File changes are detected with inotify through `ctypes`. A single thread sleeps in the kernel until something changes, then tells open pages to reload through a Server-Sent Events stream.
- The server never parses or highlights files. It sends the raw file and the browser renders it. Formatting and highlighting of code run in a Web Worker so they never freeze the page.
- Long code is split into fixed-size blocks. The browser only lays out the blocks near the viewport, so changing word wrap or zoom does not reflow the whole file.
- Folder sizes are computed on request by a plain directory scan, cached briefly, and limited in time and entry count.
- The browser loads these libraries from [jsDelivr](https://www.jsdelivr.com/) at pinned versions, only when a file needs them, and caches them afterwards:
  - [marked](https://github.com/markedjs/marked) renders Markdown.
  - [DOMPurify](https://github.com/cure53/DOMPurify) sanitizes the rendered Markdown.
  - [highlight.js](https://highlightjs.org/) highlights code.
  - [js-beautify](https://github.com/beautifier/js-beautify) formats minified JavaScript.

  Python has no equivalents in its standard library, and doing this in the browser keeps the server's CPU and memory use close to zero. Without internet access you still get the file list, plain text, images and HTML pages, and everything else falls back to plain text.
- Zoom, word wrap, sort order and scroll position are stored in the browser's `sessionStorage`, which is private to each tab.

## Security notes

- The server listens on `127.0.0.1` only. Other devices cannot connect.
- Every path is resolved to a real path and must stay inside the folder you serve. Symlinks that point outside it are hidden and blocked.
- Rendered Markdown is sanitized before it is displayed.

## Limitations

- Linux only.
- Reader views are chosen from the `Sec-Fetch-Dest` header that modern browsers send. Tools such as `curl` always receive the raw file. `?raw=1` on any URL returns the raw file as well.
- Obfuscated JavaScript is formatted and highlighted, but it is not deobfuscated. Packed code such as `eval(function(p,a,c,k,e,d){...})` stays packed.
- Very large highlighted files use a lot of browser memory, because every coloured token becomes a page element. Turning word wrap off also makes big files lighter to display.
- The JSON preview is re-serialized by the browser, so integers above 2^53 lose precision. Use the Raw view for exact values.
- Each open tab holds one event connection, and browsers allow about six connections per host. More than six tabs on the same server can stall.
- Folders such as `.git` and `node_modules` are not watched, so changes inside them do not trigger a reload.
- Watching very large trees can hit the kernel's inotify watch limit. A warning is printed if that happens.
