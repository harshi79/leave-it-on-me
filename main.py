#!/usr/bin/env python3
"""leave-it-on-me — a short-link service that lives in a single file.

No dependencies, no build step, no framework: just the Python standard library
and SQLite. Short links look like  https://<host>/a7Kq2p  and redirect to the
long URL you gave them.

Run it
------
    python main.py                      # http://0.0.0.0:8000
    PORT=9000 python main.py            # port from the environment
    python main.py --port 8080 --db my.db --rate-limit 100

HTTP API
--------
    GET    /                    dashboard (HTML form + link list)
    POST   /                    create a link from an HTML form  (url, slug?, expires_in?)
    GET    /healthz             health probe
    GET    /api/links           list links as JSON
    POST   /api/links           create a link as JSON            -> 201
    GET    /api/links/<code>    a single link as JSON
    DELETE /api/links/<code>    delete a link
    GET    /<code>              302 redirect to the target URL

Example
-------
    curl -X POST localhost:8000/api/links \\
         -H 'content-type: application/json' \\
         -d '{"url": "example.com/some/long/path", "slug": "docs"}'
    # {"code": "docs", "short_url": "http://localhost:8000/docs", ...}
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from string import Template
from urllib.parse import parse_qs, quote, urlparse

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Ambiguous characters (0/O, 1/l/I) are left out so links survive being read
# aloud or copied from a screenshot.
CODE_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
CODE_LENGTH = 6
CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# Paths the router owns; they can never be used as custom slugs.
RESERVED_CODES = {
    "api", "about", "admin", "assets", "dashboard", "delete", "favicon.ico",
    "healthz", "help", "index.html", "links", "login", "logout", "new", "null",
    "privacy", "robots.txt", "shorten", "static", "terms",
}

MAX_TARGET_LEN = 2048
MAX_BODY_BYTES = 64 * 1024
MAX_EXPIRY_SECONDS = 10 * 365 * 24 * 3600
DEFAULT_RATE_LIMIT = 60          # creations allowed per window, per client IP
RATE_LIMIT_WINDOW = 600.0        # seconds
LINKS_SHOWN = 100                # rows on the dashboard

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    code          TEXT PRIMARY KEY,
    target        TEXT NOT NULL,
    created_at    REAL NOT NULL,
    expires_at    REAL,
    clicks        INTEGER NOT NULL DEFAULT 0,
    last_click_at REAL,
    creator_ip    TEXT
);
CREATE INDEX IF NOT EXISTS links_created_at ON links (created_at DESC);
"""


class CreationError(ValueError):
    """The caller sent something we refuse to shorten."""


class SlugTaken(CreationError):
    """The requested custom slug already exists."""


class RateLimited(CreationError):
    """The caller is creating links too quickly."""


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

class Store:
    """A very small SQLite layer: one connection per operation, so the
    threaded HTTP server never shares a connection across threads."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._write_lock = threading.Lock()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 15000")
        return conn

    def init(self) -> None:
        with closing(self.connect()) as conn, conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)

    # -- reads ------------------------------------------------------------- #

    def get(self, code: str) -> sqlite3.Row | None:
        with closing(self.connect()) as conn:
            return conn.execute(
                "SELECT * FROM links WHERE code = ?", (code,)
            ).fetchone()

    def exists(self, code: str) -> bool:
        """Case-insensitive, so 'Docs' can't shadow an existing 'docs'."""
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM links WHERE code = ? COLLATE NOCASE", (code,)
            ).fetchone()
        return row is not None

    def list_recent(self, limit: int = LINKS_SHOWN) -> list[sqlite3.Row]:
        with closing(self.connect()) as conn:
            return conn.execute(
                "SELECT * FROM links ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()

    def count(self) -> int:
        with closing(self.connect()) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM links").fetchone()[0])

    # -- writes ------------------------------------------------------------ #

    def create(self, code: str, target: str, expires_at: float | None, ip: str) -> None:
        with self._write_lock, closing(self.connect()) as conn, conn:
            conn.execute(
                "INSERT INTO links (code, target, created_at, expires_at, creator_ip)"
                " VALUES (?, ?, ?, ?, ?)",
                (code, target, time.time(), expires_at, ip),
            )

    def register_click(self, code: str) -> None:
        with self._write_lock, closing(self.connect()) as conn, conn:
            conn.execute(
                "UPDATE links SET clicks = clicks + 1, last_click_at = ?"
                " WHERE code = ?",
                (time.time(), code),
            )

    def delete(self, code: str) -> bool:
        with self._write_lock, closing(self.connect()) as conn, conn:
            cur = conn.execute("DELETE FROM links WHERE code = ?", (code,))
        return cur.rowcount > 0


class RateLimiter:
    """In-memory sliding window. Good enough for one process; if you ever run
    several workers behind a load balancer, move this counter into the DB."""

    def __init__(self, limit: int, window: float = RATE_LIMIT_WINDOW) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        if self.limit <= 0:
            return True
        now = time.time()
        with self._lock:
            hits = [t for t in self._hits.get(key, ()) if now - t < self.window]
            if len(hits) >= self.limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            if len(self._hits) > 10_000:  # bound memory on long uptimes
                self._hits = {k: v for k, v in self._hits.items()
                              if v and now - v[-1] < self.window}
            return True


# --------------------------------------------------------------------------- #
# Validation & little helpers
# --------------------------------------------------------------------------- #

def normalize_target(raw: str) -> str:
    """Best-effort normalisation: add https:// if the scheme is missing, then
    insist on a real http(s) URL so we never redirect into javascript:, data:
    or a header-injection payload."""
    target = (raw or "").strip()
    if not target:
        raise CreationError("Paste a URL to shorten.")
    if len(target) > MAX_TARGET_LEN:
        raise CreationError(f"URL is too long (max {MAX_TARGET_LEN} characters).")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in target):
        raise CreationError("URL contains control characters.")
    if any(ch.isspace() for ch in target):
        raise CreationError("URL contains a space — encode it as %20.")

    try:
        if not urlparse(target).scheme:
            target = "https://" + target
        parsed = urlparse(target)
    except ValueError as exc:  # e.g. malformed IPv6 brackets
        raise CreationError(f"That URL can't be parsed: {exc}") from exc

    if parsed.scheme.lower() not in ("http", "https"):
        raise CreationError("Only http:// and https:// links can be shortened.")
    if not parsed.netloc or not parsed.hostname:
        raise CreationError("That doesn't look like a URL — try example.com/page")
    return target


def validate_slug(raw: str) -> str | None:
    slug = (raw or "").strip()
    if not slug:
        return None
    if not CODE_RE.match(slug):
        raise CreationError("Slug must be 1-64 characters: letters, digits, '-' or '_'.")
    if slug.lower() in RESERVED_CODES:
        raise CreationError(f"'{slug}' is reserved for the app itself — pick another slug.")
    return slug


def parse_expiry(raw: object) -> float | None:
    """`expires_in` is a number of seconds from now (0 / empty = never)."""
    if raw in (None, "", 0, "0"):
        return None
    try:
        seconds = int(float(str(raw)))
    except (TypeError, ValueError) as exc:
        raise CreationError("Expiration must be a number of seconds.") from exc
    if seconds <= 0:
        return None
    return time.time() + min(seconds, MAX_EXPIRY_SECONDS)


def generate_code(store: Store) -> str:
    for length in (CODE_LENGTH, CODE_LENGTH + 2, CODE_LENGTH + 4):
        for _ in range(12):
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))
            if not store.exists(code):
                return code
    raise CreationError("Could not allocate a free code — try again.")


def humanize_age(seconds: float) -> str:
    return "just now" if seconds < 5 else f"{describe_span(seconds)} ago"


def describe_in(seconds: float) -> str:
    """'in 3d', 'in 45m' … for expiry badges."""
    return "any moment" if seconds < 60 else f"in {describe_span(seconds)}"


def describe_span(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    if days < 30:
        return f"{days}d"
    months = days // 30
    return f"{months}y" if months >= 12 else f"{months}mo"


def quote_target(target: str) -> str:
    """Percent-encode anything non-ASCII so the Location header stays legal."""
    return quote(target, safe=":/?#[]@!$&'()*+,;=%~")


def link_payload(row: sqlite3.Row, base: str) -> dict:
    expires_at = row["expires_at"]
    return {
        "code": row["code"],
        "short_url": f"{base}/{row['code']}",
        "target": row["target"],
        "clicks": row["clicks"],
        "created_at": iso(row["created_at"]),
        "last_click_at": iso(row["last_click_at"]),
        "expires_at": iso(expires_at),
        "expired": bool(expires_at and expires_at < time.time()),
    }


def iso(ts: float | None) -> str | None:
    return None if ts is None else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #

PAGE_CSS = """
:root {
  color-scheme: dark;
  --bg: #0b0d12; --panel: #141a26; --panel-2: #1b2231; --border: #242d40;
  --text: #e8edf7; --muted: #8b98ad; --accent: #6ea8fe; --ok: #7ee0b8;
  --danger: #ff6b81; --radius: 12px;
}
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh; color: var(--text);
  background: radial-gradient(1100px 520px at 50% -12%, #16203a 0%, var(--bg) 62%) fixed;
  font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
main { max-width: 1000px; margin: 0 auto; padding: 40px 20px 72px; }
header h1 { margin: 0; font-size: 28px; letter-spacing: -0.4px; }
header h1 span { color: var(--accent); }
header p { margin: 6px 0 24px; color: var(--muted); }
code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.card {
  background: linear-gradient(180deg, var(--panel), var(--panel-2));
  border: 1px solid var(--border); border-radius: var(--radius);
  padding: 18px; margin-bottom: 18px;
}
form.create { display: grid; gap: 12px; }
label { display: grid; gap: 6px; font-size: 13px; color: var(--muted); }
input, select, button, textarea { font: inherit; }
input[type=text], input[type=url], select {
  width: 100%; padding: 11px 12px; color: var(--text);
  background: #0e1420; border: 1px solid var(--border); border-radius: 9px;
}
input:focus, select:focus { outline: 2px solid rgba(110,168,254,.45); outline-offset: 1px; }
.row { display: grid; grid-template-columns: 1fr 200px; gap: 12px; }
button {
  cursor: pointer; border: 0; border-radius: 9px; padding: 11px 18px;
  background: var(--accent); color: #071022; font-weight: 600;
}
button:hover { filter: brightness(1.08); }
button.ghost {
  background: transparent; color: var(--muted); border: 1px solid var(--border);
  padding: 6px 11px; font-weight: 500;
}
button.ghost:hover { color: var(--danger); border-color: var(--danger); }
.banner { border-radius: var(--radius); padding: 12px 14px; margin-bottom: 18px; border: 1px solid; }
.banner.ok { border-color: rgba(126,224,184,.4); background: rgba(126,224,184,.08); }
.banner.err { border-color: rgba(255,107,129,.45); background: rgba(255,107,129,.09); }
.banner .label { font-size: 12px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); }
.shortlink { display: flex; gap: 10px; align-items: center; margin: 6px 0; }
.shortlink input {
  flex: 1; padding: 9px 11px; background: #0e1420; color: var(--ok);
  border: 1px solid var(--border); border-radius: 9px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.banner .target { color: var(--muted); font-size: 13px; word-break: break-all; }
table { width: 100%; border-collapse: collapse; }
th {
  text-align: left; font-size: 12px; letter-spacing: .06em; text-transform: uppercase;
  color: var(--muted); padding: 0 10px 10px; font-weight: 600;
}
td { padding: 11px 10px; border-top: 1px solid var(--border); vertical-align: top; }
td.num { text-align: right; white-space: nowrap; }
td.act { text-align: right; }
tbody tr:hover { background: rgba(110,168,254,.045); }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.target-cell { color: var(--muted); font-size: 13px; word-break: break-all; max-width: 380px; }
.pill {
  display: inline-block; font-size: 11px; padding: 2px 7px; border-radius: 999px;
  border: 1px solid var(--border); color: var(--muted); margin-left: 6px;
}
.pill.expired { color: var(--danger); border-color: rgba(255,107,129,.45); }
.empty { color: var(--muted); text-align: center; padding: 28px 10px; }
footer { color: var(--muted); font-size: 13px; margin-top: 26px; text-align: center; }
@media (max-width: 680px) { .row { grid-template-columns: 1fr; } .target-cell { max-width: 200px; } }
"""

PAGE = Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title</title>
<style>$css</style>
</head>
<body>
<main>
$content
<footer>leave-it-on-me · single-file short links · <a href="/api/links">api</a></footer>
</main>
$script
</body>
</html>
""")

COPY_SCRIPT = """<script>
document.addEventListener("click", function (event) {
  var button = event.target.closest("[data-copy]");
  if (!button) return;
  var input = document.querySelector(button.getAttribute("data-copy"));
  if (!input) return;
  var finish = function () {
    var old = button.textContent;
    button.textContent = "copied!";
    setTimeout(function () { button.textContent = old; }, 1200);
  };
  if (navigator.clipboard) {
    navigator.clipboard.writeText(input.value).then(finish, function () { input.select(); });
  } else {
    input.select();
    document.execCommand("copy");
    finish();
  }
});
</script>"""


def render_page(title: str, content: str, script: str = "") -> bytes:
    return PAGE.substitute(title=html.escape(title), css=PAGE_CSS,
                           content=content, script=script).encode("utf-8")


def render_link_row(row: sqlite3.Row, base: str) -> str:
    expired = bool(row["expires_at"] and row["expires_at"] < time.time())
    short = f"{base}/{row['code']}"
    pill = ""
    if expired:
        pill = '<span class="pill expired">expired</span>'
    elif row["expires_at"]:
        pill = f'<span class="pill">expires {html.escape(describe_in(row["expires_at"] - time.time()))}</span>'
    return f"""<tr>
  <td><a class="mono" href="/{html.escape(row['code'])}">/{html.escape(row['code'])}</a>{pill}</td>
  <td class="target-cell"><a href="{html.escape(row['target'])}" rel="noreferrer noopener">{html.escape(row['target'])}</a></td>
  <td class="num">{row['clicks']}</td>
  <td class="num">{html.escape(humanize_age(time.time() - row['created_at']))}</td>
  <td class="act">
    <form method="post" action="/delete" onsubmit="return confirm('Delete /{html.escape(row['code'])}?')">
      <input type="hidden" name="code" value="{html.escape(row['code'])}">
      <button class="ghost" type="submit">delete</button>
    </form>
  </td>
</tr>"""


def render_dashboard(store: Store, base: str, *, notice_code: str | None = None,
                     error: str | None = None, prefill_url: str = "",
                     prefill_slug: str = "") -> bytes:
    rows = store.list_recent(LINKS_SHOWN)
    total = store.count()
    total_clicks = sum(int(r["clicks"]) for r in rows)

    banner = ""
    if error:
        banner += (f'<div class="banner err"><div class="label">nope</div>'
                   f'{html.escape(error)}</div>')
    if notice_code:
        row = store.get(notice_code)
        if row is not None:
            short = f"{base}/{row['code']}"
            banner += (
                f'<div class="banner ok">'
                f'<div class="label">your short link</div>'
                f'<div class="shortlink">'
                f'<input id="new-link" readonly value="{html.escape(short)}">'
                f'<button type="button" data-copy="#new-link">copy</button>'
                f'<a class="mono" href="/{html.escape(row["code"])}">test →</a>'
                f'</div>'
                f'<div class="target">→ {html.escape(row["target"])}</div>'
                f'</div>')

    if rows:
        table = f"""<table>
  <thead><tr><th>short</th><th>target</th><th class="num">clicks</th>
  <th class="num">created</th><th></th></tr></thead>
  <tbody>{''.join(render_link_row(r, base) for r in rows)}</tbody>
</table>"""
    else:
        table = '<p class="empty">No links yet — shorten your first URL above.</p>'

    content = f"""<header>
  <h1>leave-it-<span>on-me</span></h1>
  <p>{total} link{'' if total == 1 else 's'} stored · {total_clicks} click{'' if total_clicks == 1 else 's'} in the last {len(rows)} links</p>
</header>
{banner}
<section class="card">
  <form class="create" method="post" action="/shorten">
    <label>long url
      <input type="text" name="url" required autofocus placeholder="example.com/a/very/long/path?with=params"
             value="{html.escape(prefill_url)}">
    </label>
    <div class="row">
      <label>custom slug (optional)
        <input type="text" name="slug" placeholder="launch" value="{html.escape(prefill_slug)}">
      </label>
      <label>expires
        <select name="expires_in">
          <option value="">never</option>
          <option value="3600">in 1 hour</option>
          <option value="86400">in 1 day</option>
          <option value="604800">in 7 days</option>
          <option value="2592000">in 30 days</option>
        </select>
      </label>
    </div>
    <div><button type="submit">shorten it</button></div>
  </form>
</section>
<section class="card">{table}</section>"""
    return render_page("leave-it-on-me", content, COPY_SCRIPT)


def render_simple_redirect(target: str, code: str) -> bytes:
    content = f"""<header><h1>leave-it-<span>on-me</span></h1></header>
<section class="card">
  <p>Taking you to your link…</p>
  <p class="target-cell"><a href="{html.escape(target)}">{html.escape(target)}</a></p>
  <p class="target-cell mono">/{html.escape(code)}</p>
</section>"""
    return render_page("redirecting…", content)


def render_missing(code: str) -> bytes:
    content = f"""<header><h1>leave-it-<span>on-me</span></h1></header>
<section class="card">
  <p><strong>/{html.escape(code)}</strong> isn't a link I know about — it may have been deleted or mistyped.</p>
  <p><a href="/">← make a new short link</a></p>
</section>"""
    return render_page("not found", content)


def render_expired(code: str, target: str) -> bytes:
    content = f"""<header><h1>leave-it-<span>on-me</span></h1></header>
<section class="card">
  <p><strong>/{html.escape(code)}</strong> expired, so it no longer redirects.</p>
  <p class="target-cell">It used to point at <span class="mono">{html.escape(target)}</span></p>
  <p><a href="/">← make a new short link</a></p>
</section>"""
    return render_page("link expired", content)


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #

class ShortenerHandler(BaseHTTPRequestHandler):
    server_version = "leave-it-on-me/1.0"
    protocol_version = "HTTP/1.1"

    # -- generic helpers --------------------------------------------------- #

    @property
    def store(self) -> Store:
        return self.server.store  # type: ignore[attr-defined]

    @property
    def limiter(self) -> RateLimiter:
        return self.server.limiter  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        if not getattr(self.server, "quiet", False):
            print(f"{self.log_date_time_string()} {self.address_string()} {fmt % args}",
                  flush=True)

    def base_url(self) -> str:
        """Absolute base for the links we hand back, proxy-aware."""
        host = (self.headers.get("X-Forwarded-Host")
                or self.headers.get("Host")
                or f"localhost:{self.server.server_port}")  # type: ignore[attr-defined]
        host = host.split(",")[0].strip()
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip()
        if not proto:
            hostname = host.rsplit(":", 1)[0].strip("[]")
            loopback = hostname in ("localhost", "127.0.0.1", "::1")
            proto = "http" if loopback else "https"
        return f"{proto}://{host}"

    def client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.client_address[0]

    def read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        length = max(0, min(length, MAX_BODY_BYTES))
        return self.rfile.read(length) if length else b""

    def parse_form(self) -> dict[str, str]:
        raw = self.read_body().decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def parse_json(self) -> dict:
        raw = self.read_body()
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise CreationError(f"Invalid JSON body: {exc.msg}") from exc
        if not isinstance(payload, dict):
            raise CreationError("Expected a JSON object.")
        return payload

    def respond(self, status: int, body: bytes = b"", *, content_type: str =
                "text/plain; charset=utf-8", headers: dict[str, str] | None = None,
                head_only: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if status not in (HTTPStatus.NO_CONTENT, 304):
            self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body and not head_only:
            self.wfile.write(body)

    def json_response(self, status: int, payload, head_only: bool = False) -> None:
        body = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        self.respond(status, body, content_type="application/json; charset=utf-8",
                     headers={"Cache-Control": "no-store",
                              "Access-Control-Allow-Origin": "*"},
                     head_only=head_only)

    def html_response(self, status: int, body: bytes, head_only: bool = False) -> None:
        self.respond(status, body, content_type="text/html; charset=utf-8",
                     headers={"Cache-Control": "no-store"}, head_only=head_only)

    # -- creation (shared by the form and the API) ------------------------- #

    def create_link(self, data: dict) -> sqlite3.Row:
        target = normalize_target(str(data.get("url") or data.get("target") or ""))
        slug = validate_slug(str(data.get("slug") or data.get("code") or ""))
        expires_at = parse_expiry(data.get("expires_in") or data.get("expires_at"))

        if slug and self.store.exists(slug):
            raise SlugTaken(f"/{slug} is already taken — pick another slug.")
        if not self.limiter.allow(self.client_ip()):
            raise RateLimited("Too many links from this address — slow down a minute.")

        code = slug or generate_code(self.store)
        self.store.create(code, target, expires_at, self.client_ip())
        row = self.store.get(code)
        if row is None:  # pragma: no cover - only on a race with a delete
            raise CreationError("The link vanished right after it was created.")
        return row

    def status_for(self, error: CreationError) -> int:
        if isinstance(error, RateLimited):
            return HTTPStatus.TOO_MANY_REQUESTS
        if isinstance(error, SlugTaken):
            return HTTPStatus.CONFLICT
        return HTTPStatus.BAD_REQUEST

    # -- routes ------------------------------------------------------------ #

    def do_GET(self, head_only: bool = False) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self.html_response(HTTPStatus.OK,
                                      render_dashboard(self.store, self.base_url()),
                                      head_only)
        if path == "/healthz":
            return self.json_response(HTTPStatus.OK, {
                "ok": True,
                "links": self.store.count(),
                "uptime_seconds": round(time.time() - self.server.started_at, 1),  # type: ignore[attr-defined]
            }, head_only)
        if path == "/robots.txt":
            return self.respond(HTTPStatus.OK, b"User-agent: *\nDisallow:\n",
                                head_only=head_only)
        if path == "/favicon.ico":
            return self.respond(HTTPStatus.NO_CONTENT, head_only=head_only)
        if path == "/api/links":
            rows = self.store.list_recent(LINKS_SHOWN)
            base = self.base_url()
            return self.json_response(
                HTTPStatus.OK,
                {"count": len(rows), "links": [link_payload(r, base) for r in rows]},
                head_only)
        if path.startswith("/api/links/"):
            code = path[len("/api/links/"):]
            row = self.store.get(code)
            if row is None:
                return self.json_response(HTTPStatus.NOT_FOUND,
                                          {"error": f"/{code} not found"}, head_only)
            return self.json_response(HTTPStatus.OK,
                                      link_payload(row, self.base_url()), head_only)
        if path.startswith("/api/"):
            return self.json_response(HTTPStatus.NOT_FOUND,
                                      {"error": "unknown endpoint"}, head_only)

        code = path.lstrip("/")
        if not CODE_RE.match(code):
            return self.html_response(HTTPStatus.NOT_FOUND, render_missing(code),
                                      head_only)
        return self.redirect_to(code, head_only)

    def do_HEAD(self) -> None:
        self.do_GET(head_only=True)

    def redirect_to(self, code: str, head_only: bool = False) -> None:
        row = self.store.get(code)
        if row is None:
            return self.html_response(HTTPStatus.NOT_FOUND, render_missing(code),
                                      head_only)
        if row["expires_at"] and row["expires_at"] < time.time():
            return self.html_response(HTTPStatus.GONE,
                                      render_expired(code, row["target"]), head_only)
        if not head_only:
            self.store.register_click(code)
        self.respond(
            HTTPStatus.FOUND,
            render_simple_redirect(row["target"], code),
            content_type="text/html; charset=utf-8",
            headers={"Location": quote_target(row["target"]),
                     "Cache-Control": "no-store"},
            head_only=head_only,
        )

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        wants_json = path.startswith("/api/") or "application/json" in \
            (self.headers.get("Content-Type") or "")

        if path in ("/api/links", "/shorten", "/"):
            if wants_json:
                try:
                    data = self.parse_json()
                except CreationError as exc:
                    return self.json_response(self.status_for(exc), {"error": str(exc)})
            else:
                data = self.parse_form()

            try:
                row = self.create_link(data)
            except CreationError as exc:
                if wants_json:
                    return self.json_response(self.status_for(exc), {"error": str(exc)})
                return self.html_response(
                    self.status_for(exc),
                    render_dashboard(self.store, self.base_url(), error=str(exc),
                                     prefill_url=str(data.get("url") or ""),
                                     prefill_slug=str(data.get("slug") or "")),
                )

            base = self.base_url()
            if wants_json:
                body = json.dumps(link_payload(row, base), indent=2, ensure_ascii=False) \
                    .encode("utf-8")
                return self.respond(
                    HTTPStatus.CREATED, body,
                    content_type="application/json; charset=utf-8",
                    headers={"Location": f"{base}/{row['code']}",
                             "Access-Control-Allow-Origin": "*",
                             "Cache-Control": "no-store"})

            return self.respond(
                HTTPStatus.SEE_OTHER, b"",
                headers={"Location": f"/?created={quote(row['code'])}"})

        if path == "/delete":
            data = self.parse_form()
            self.store.delete(str(data.get("code") or ""))
            return self.respond(HTTPStatus.SEE_OTHER, b"", headers={"Location": "/"})

        if path.startswith("/api/"):
            return self.json_response(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})
        return self.html_response(HTTPStatus.NOT_FOUND, render_missing(path.lstrip("/")))

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/links/"):
            code = path[len("/api/links/"):]
            if self.store.delete(code):
                return self.json_response(HTTPStatus.OK, {"deleted": code})
            return self.json_response(HTTPStatus.NOT_FOUND, {"error": f"/{code} not found"})
        return self.json_response(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})

    def do_OPTIONS(self) -> None:
        self.respond(HTTPStatus.NO_CONTENT, headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "86400",
        })


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(
        prog="main.py", description="leave-it-on-me — single-file URL shortener")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"),
                        help="interface to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")),
                        help="port to listen on (default: 8000)")
    parser.add_argument("--db", default=os.environ.get("DB_PATH", os.path.join(here, "links.db")),
                        help="SQLite file for the links (default: links.db next to main.py)")
    parser.add_argument("--rate-limit", type=int, default=DEFAULT_RATE_LIMIT,
                        help=f"link creations per client per {int(RATE_LIMIT_WINDOW // 60)} min"
                             " (0 disables the limit)")
    parser.add_argument("--quiet", action="store_true", help="don't log requests")
    return parser.parse_args(argv)


def build_server(args: argparse.Namespace) -> ThreadingHTTPServer:
    store = Store(args.db)
    store.init()

    server = ThreadingHTTPServer((args.host, args.port), ShortenerHandler)
    server.daemon_threads = True
    server.store = store                      # type: ignore[attr-defined]
    server.limiter = RateLimiter(args.rate_limit)  # type: ignore[attr-defined]
    server.started_at = time.time()           # type: ignore[attr-defined]
    server.quiet = args.quiet                 # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    server = build_server(args)
    host, port = server.server_address[0], server.server_address[1]
    shown = "localhost" if host in ("0.0.0.0", "::") else host
    print(f"leave-it-on-me listening on http://{shown}:{port}")
    print(f"  database : {os.path.abspath(args.db)}")
    print(f"  rate cap : {args.rate_limit or 'off'} creations / "
          f"{int(RATE_LIMIT_WINDOW // 60)} min per IP")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye 👋")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
