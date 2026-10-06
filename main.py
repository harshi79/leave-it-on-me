#!/usr/bin/env python3
"""leave-it-on-me — short link in, main link out.

Paste a short link, get the real one back. The tool walks the whole redirect
chain, then reads whatever the interstitial page tells it *without running a
browser*: meta refresh, location.href assignments, url= parameters, data-*
attributes, JSON payloads and obvious "continue" links. Covers the plain
redirect services (bit.ly, t.co, tinyurl, is.gd, cutt.ly, rb.gy …) plus a big
class of JS interstitial pages, entirely offline.

Single file, standard library only. No JavaScript is ever executed, and no
site-specific bypass APIs are reverse-engineered: if a gate genuinely needs a
browser (and, for ad-gates, a captcha or an ad view) the tool says so instead
of pretending.

Use it
------
    python main.py https://bit.ly/3xyzAb            # resolve on the command line
    python main.py https://short.link/x --json      # machine-readable
    python main.py --serve --demo                   # web UI on :8000
"""

from __future__ import annotations

import argparse
import base64
import html
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from string import Template
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urljoin, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
MAX_BODY = 512 * 1024
DEFAULT_TIMEOUT = 12.0
DEFAULT_MAX_HOPS = 12
DEFAULT_BUDGET = 45.0
MAX_URL_LEN = 4096
REDIRECT_STATUSES = (301, 302, 303, 307, 308)

# Services whose only job is to redirect you somewhere else.
SHORTENER_HOSTS = {
    "bit.ly", "bitly.com", "bitly.is", "j.mp", "t.co", "tinyurl.com", "tiny.cc",
    "is.gd", "v.gd", "goo.gl", "ow.ly", "buff.ly", "rebrand.ly", "cutt.ly",
    "rb.gy", "shorturl.at", "shrtco.de", "s.id", "lnkd.in", "t.ly", "trib.al",
    "dlvr.it", "ift.tt", "snip.ly", "bl.ink", "urlz.fr", "clck.ru", "vk.cc",
    "youtu.be", "youtube.com/redirect", "amzn.to", "amzn.eu", "a.co", "redd.it",
    "spoti.fi", "apple.co", "geni.us", "ffm.to", "mcaf.ee", "soo.gd",
    "shorte.st", "sh.st", "adfoc.us", "bc.vc", "ouo.io", "ouo.press",
    "clk.sh", "clik.pw", "post.cx", "x.gd", "1url.com", "3.ly", "4.gp",
    "7.ly", "aa.cx", "alturl.com", "budurl.com", "chilp.it", "cl.lk",
    "cli.re", "clkim.co", "cort.as", "cur.lv", "dai.ly", "dum.ly", "duolingo",
    "fa.by", "flic.kr", "flip.it", "fxn.ws", "g.ho.st", "gizmo.do",
    "hmm.xyz", "ht.ly", "hyperurl.co", "i-can.us", "ic.yt", "idek.io",
    "intent.io", "j.mp", "kck.st", "kickstarter.com", "kutt.it", "l.ead.me",
    "l.gg", "link.ac", "ln.is", "lnk.co", "lnk.to", "lnkfy.com", "m1p.fr",
    "migre.me", "mub.me", "mzl.la", "nbc.co", "nblo.gs", "nic.yt", "nxy.in",
    "oc.cm", "on.fb.me", "paper.li", "po.st", "post.ly", "qr.ae", "qr.cx",
    "r.ly", "rdbl.co", "reut.rs", "rlu.ru", "rtvote.com", "s.coop",
    "s.mtrbio.com", "s.tl", "ser.dj", "shar.es", "shorl.com", "shr.im",
    "shz.am", "sk.gy", "sq.re", "studivz.com", "su.pr", "t.ly", "t.me",
    "tgr.ph", "tidd.ly", "tl.gd", "tny.im", "toggl.ee", "tr.im", "tubreel.com",
    "twurl.nl", "u.bb", "u.to", "ub0.cc", "ur1.ca", "url.ie", "url4.eu",
    "urlborg.com", "urlgeni.us", "urls.im", "vwe.co", "w.wiki", "wp.me",
    "x.co", "xrl.us", "y.ahoo.it", "yep.it", "zi.ma", "zpr.io",
}
SHORTENER_HOSTS = {h for h in SHORTENER_HOSTS if "/" not in h}

# Monetised gates. HTTP redirects are still followed and the page is still
# parsed the same way as any other — but these deliberately hand the target to
# JavaScript (usually after an ad/captcha), so a plain client often can't see it.
GATEWAY_HOSTS = {
    "linkvertise.com", "linkvertise.net", "link-to.net", "up-to-down.net",
    "direct-link.net", "file-upload.net", "adf.ly", "adfly.com", "sh.st",
    "shorte.st", "ouo.io", "ouo.press", "exe.io", "exee.io", "gplinks.in",
    "gplinks.co", "tnlink.in", "tnshort.net", "shrinkme.io", "shrinke.me",
    "shrinkearn.com", "clk.sh", "mboost.me", "boost.ink", "boostlink.net",
    "sub2unlock.com", "sub2unlock.net", "sub2unlock.io", "bc.vc", "adfoc.us",
    "soo.gd", "tei.ai", "cuty.io", "za.gl", "zagl.xyz", "urlsopen.com",
    "droplink.co", "crypto-ads.xyz", "adrinolinks.in", "mdiskshortner.link",
    "loot-link.com", "lootlinks.co", "lootdest.org", "zshort.link",
    "social-unlock.com", "yoshort.xyz", "promo-visits.site", "indianshortner.in",
}

# Assets, analytics and social chrome — never a destination, so keep them out
# of the "outbound link in the page" heuristic.
NOISE_HOSTS = {
    "google.com", "google.co", "googleapis.com", "gstatic.com", "googletagmanager.com",
    "google-analytics.com", "googlesyndication.com", "doubleclick.net", "recaptcha.net",
    "hcaptcha.com", "cloudflare.com", "cdnjs.cloudflare.com", "jsdelivr.net", "unpkg.com",
    "bootstrapcdn.com", "jquery.com", "fonts.com", "typekit.net", "fontawesome.com",
    "facebook.com", "fbcdn.net", "twitter.com", "x.com", "twimg.com", "instagram.com",
    "linkedin.com", "tiktok.com", "pinterest.com", "disqus.com", "addthis.com",
    "sentry.io", "hotjar.com", "newrelic.com", "segment.com", "amplitude.com",
    "w3.org", "schema.org", "creativecommons.org", "gmpg.org", "wordpress.org",
    "wp.com", "gravatar.com", "wixstatic.com", "shields.io", "paypal.com",
    "stripe.com", "squarespace.com", "cdn.jsdelivr.net", "cdnjs.com", "akamaihd.net",
    "cloudfront.net", "fastly.net", "bunnycdn.com", "netlify.app", "vercel.app",
}

MULTI_LEVEL_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "co.in", "net.in", "org.in", "ac.in",
    "com.au", "net.au", "org.au", "co.jp", "com.br", "com.mx", "com.ar",
    "co.za", "com.tr", "co.kr", "com.cn", "com.hk", "com.sg", "co.id",
}

RISKY_TLDS = {
    "zip", "mov", "tk", "ml", "ga", "cf", "gq", "top", "work", "click", "rest",
    "cam", "sbs", "cfd", "icu", "buzz", "quest", "monster", "lol", "cyou",
    "wang", "best", "loan", "moml", "su", "ru", "ws", "pw",
}

DOWNLOAD_EXTENSIONS = (
    ".exe", ".msi", ".apk", ".apks", ".dmg", ".pkg", ".deb", ".rpm", ".bat",
    ".cmd", ".scr", ".vbs", ".jar", ".zip", ".rar", ".7z", ".iso", ".img",
    ".torrent", ".crx", ".xpi",
)

TRACKING_PARAMS = {
    "fbclid", "gclid", "gbraid", "wbraid", "dclid", "msclkid", "twclid",
    "ttclid", "yclid", "igshid", "mkt_tok", "vero_id", "_ga", "_gl", "ref_src",
    "spm", "scm", "share_id", "cmpid", "campaign_id", "oly_enc_id",
    "oly_anon_id", "s_kwcid", "wickedid", "mc_eid", "si", "feature",
    "trk", "trkCampaign", "sc_cid", "at_medium", "at_campaign",
}
TRACKING_PREFIXES = ("utm_", "pk_", "mtm_", "hsa_", "mkt_")

# Query parameters that commonly carry "the real URL" on redirect pages.
URL_PARAM_KEYS = {
    "url", "u", "r", "to", "t", "target", "link", "redirect", "redirect_uri",
    "redirect_url", "redirect_to", "goto", "go", "out", "out_url", "dest",
    "destination", "continue", "next", "q", "link_url", "target_url", "real",
    "real_url", "actual", "actual_url", "href", "adurl", "site", "open",
}
CONTINUE_WORDS = re.compile(
    r"\b(continue|proceed|go to|skip|get link|open link|open|download|visit|"
    r"click here|enter|i'?m ready|ready|verify|access|see more|view|start|"
    r"watch|play|claim|unlock)\b", re.I)
CONTINUE_PATHS = re.compile(r"/(go|out|redirect|link|visit|continue|away|api/go)\b", re.I)

# --------------------------------------------------------------------------- #
# Patterns used to read a page statically
# --------------------------------------------------------------------------- #

META_REFRESH_RE = re.compile(
    r"""<meta[^>]+http-equiv\s*=\s*["']?refresh["']?[^>]*content\s*=\s*["']([^"']+)["']""",
    re.I)
META_REFRESH_RE_ALT = re.compile(
    r"""<meta[^>]+content\s*=\s*["'][^"']*url\s*=\s*([^"']+)["'][^>]*"""
    r"""http-equiv\s*=\s*["']?refresh["']?""", re.I)
REFRESH_HEADER_RE = re.compile(r"""^\s*[\d.]*\s*;?\s*url\s*=\s*["']?([^"';]+)""", re.I)
JS_LOCATION_RE = re.compile(
    r"""(?:window|document|top|self|parent)?\s*\.?\s*location\s*\.?\s*"""
    r"""(?:href|replace|assign)?\s*(?:=|\()\s*["']([^"'\s]{4,})["']""", re.I)
JS_LOCATION_ALT_RE = re.compile(
    r"""(?:location|document\.URL|window\.location)\s*=\s*["']([^"'\s]{4,})["']""", re.I)
DATA_URL_RE = re.compile(
    r"""data-(?:url|href|target|destination|dest|link|redirect|redirect-url|"""
    r"""continue-url|out)\s*=\s*["']([^"']{6,})["']""", re.I)
JSON_URL_RE = re.compile(
    r"""["'](?:url|target|destination|dest|link|href|redirect|redirect_uri|"""
    r"""redirect_url|redirect_to|goto|to|out|out_url|continue_url|landing|"""
    r"""final_url|link_url|target_url|adurl|real_url)["']\s*:\s*["']([^"']{8,})["']""",
    re.I)
ANY_URL_RE = re.compile(r"""https?://[^\s"'<>()\\{}\[\]|^`]+""", re.I)
ANCHOR_RE = re.compile(r"""<a\s[^>]*?href\s*=\s*["']([^"'#]+)["'][^>]*>(.*?)</a>""",
                       re.I | re.S)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
CHARSET_RE = re.compile(r"""charset\s*=\s*["']?([\w-]+)""", re.I)
BASE64_RE = re.compile(r"^[A-Za-z0-9+/\-_]{16,}={0,2}$")
JS_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


class ResolveError(ValueError):
    """The input can't be resolved at all (bad URL, blocked host, …)."""


# --------------------------------------------------------------------------- #
# URL handling
# --------------------------------------------------------------------------- #

def normalize_url(raw: str) -> str:
    """Add a scheme if it's missing and refuse anything that isn't http(s)."""
    target = (raw or "").strip().strip("<>\"'")
    if not target:
        raise ResolveError("Paste a link to resolve.")
    if len(target) > MAX_URL_LEN:
        raise ResolveError(f"That link is {len(target)} characters — too long.")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in target):
        raise ResolveError("The link contains control characters.")
    if any(ch.isspace() for ch in target):
        raise ResolveError("The link contains a space — encode it as %20.")

    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):
        target = "https://" + target
    parsed = urlparse(target)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ResolveError("Only http:// and https:// links can be resolved.")
    if not parsed.netloc or not parsed.hostname:
        raise ResolveError("That doesn't look like a link — try example.com/page")
    return target


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def registrable_domain(host: str) -> str:
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    suffix = ".".join(parts[-2:])
    if suffix in MULTI_LEVEL_SUFFIXES and len(parts) >= 3:
        return ".".join(parts[-3:])
    return suffix


def host_matches(host: str, domains: set[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def is_shortener(url: str) -> bool:
    return host_matches(host_of(url), SHORTENER_HOSTS)


def is_gateway(url: str) -> bool:
    return host_matches(host_of(url), GATEWAY_HOSTS)


def same_site(a: str, b: str) -> bool:
    return registrable_domain(host_of(a)) == registrable_domain(host_of(b))


def strip_tracking(url: str) -> str:
    """Drop utm_*/fbclid-style noise so the answer is the clean destination."""
    parsed = urlparse(url)
    if not parsed.query:
        return url
    kept = []
    for key, values in parse_qs(parsed.query, keep_blank_values=True).items():
        low = key.lower()
        if low in {p.lower() for p in TRACKING_PARAMS} or low.startswith(TRACKING_PREFIXES):
            continue
        kept.extend((key, value) for value in values)
    query = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in kept)
    return urlunparse(parsed._replace(query=query))


RELATIVE_PATH_RE = re.compile(r"^(?:\.{0,2}/)[\w\-.~/]*(\?.*)?$", re.I)

# "example.com/path" with no scheme, e.g. a value in a config blob. The final
# label must look like a real TLD, so "index.html" or "photo.jpeg" don't match.
FILE_EXTENSIONS = {
    "html", "htm", "php", "asp", "aspx", "jsp", "js", "json", "css", "xml",
    "txt", "png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "woff", "woff2",
    "ttf", "eot", "map", "pdf", "zip", "rar", "gz", "tar", "exe", "msi", "apk",
    "dmg", "iso", "mp3", "mp4", "webm", "csv", "yml", "yaml", "md",
}
SCHEMELESS_HOST_RE = re.compile(r"^([\w-]+\.)+([A-Za-z]{2,24})([/?#].*)?$")

# Percent-escapes worth decoding to reveal a URL. Everything else (spaces,
# non-ASCII) stays encoded, because that's what a browser would send anyway.
STRUCTURAL_ESCAPES = {"3a": ":", "2f": "/", "3f": "?", "26": "&", "3d": "=",
                      "23": "#", "25": "%", "3b": ";", "40": "@", "2b": "+"}


def decode_structural_escapes(text: str) -> str:
    return re.sub(r"%([0-9a-fA-F]{2})",
                  lambda match: STRUCTURAL_ESCAPES.get(match.group(1).lower(),
                                                       match.group(0)), text)


def looks_like_filename(text: str) -> bool:
    head = re.split(r"[/?#]", text, 1)[0]
    return "." in head and head.rsplit(".", 1)[-1].lower() in FILE_EXTENSIONS


def decode_candidate(value: str, current: str) -> str | None:
    """Turn whatever was found in a page into a usable http(s) URL."""
    text = (value or "").strip().strip("'\"")
    if not text:
        return None
    text = text.replace("\\/", "/").replace("\\:", ":")
    text = JS_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), text)
    for _ in range(2):        # ?url=https%3A%2F%2F… and the occasional double encode
        text = html.unescape(decode_structural_escapes(text))
    text = text.strip().strip("'\"")

    if text.startswith("//"):
        text = urlparse(current).scheme + ":" + text

    if not re.match(r"^https?://", text, re.I):
        decoded_b64 = decode_base64_url(text)
        if decoded_b64:
            text = decoded_b64
        elif RELATIVE_PATH_RE.match(text) and len(text) > 1:
            text = urljoin(current, text)      # meta refresh often uses /path
        elif SCHEMELESS_HOST_RE.match(text) and not looks_like_filename(text):
            text = "https://" + text           # "example.com/path" in a config blob
        else:
            return None

    try:
        candidate = normalize_url(text)
    except ResolveError:
        return None

    if candidate.rstrip("/") == current.rstrip("/"):
        return None
    # A link to the current site's bare homepage is navigation, not a target.
    parsed, here = urlparse(candidate), urlparse(current)
    if (parsed.hostname == here.hostname and parsed.path in ("", "/")
            and not parsed.query):
        return None
    return candidate


def decode_base64_url(text: str) -> str | None:
    """Some shorteners base64 their target: ?r=aHR0cHM6Ly9leGFtcGxlLmNvbQ."""
    if not BASE64_RE.match(text):
        return None
    padded = text.replace("-", "+").replace("_", "/")
    padded += "=" * (-len(padded) % 4)
    try:
        raw = base64.b64decode(padded, validate=True)
        decoded = raw.decode("utf-8", "replace").strip()
    except (ValueError, TypeError):
        return None
    return decoded if re.match(r"^https?://", decoded, re.I) else None


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

class _NoAutoRedirect(HTTPRedirectHandler):
    """We want to see every hop, so ask urllib to hand redirects back to us."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


@dataclass
class Fetch:
    url: str
    status: int
    headers: dict
    body: bytes = b""
    error: str | None = None

    @property
    def location(self) -> str | None:
        for key, value in self.headers.items():
            if key.lower() == "location":
                return value
        return None

    @property
    def refresh(self) -> str | None:
        for key, value in self.headers.items():
            if key.lower() == "refresh":
                return value
        return None

    @property
    def charset(self) -> str:
        ctype = self.headers.get("Content-Type") or self.headers.get("content-type") or ""
        match = CHARSET_RE.search(ctype)
        return match.group(1) if match else "utf-8"

    def text(self, limit: int = MAX_BODY) -> str:
        return self.body[:limit].decode(self.charset, "replace")

    @property
    def content_type(self) -> str:
        value = self.headers.get("Content-Type") or self.headers.get("content-type") or ""
        return value.split(";")[0].strip().lower()


def describe_status(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "error"


def describe_error(err: BaseException) -> str:
    reason = getattr(err, "reason", None)
    if isinstance(reason, BaseException):
        err = reason
    if isinstance(err, socket.timeout) or "timed out" in str(err).lower():
        return "timed out"
    if isinstance(err, ssl.SSLError) or "certificate" in str(err).lower():
        text = re.sub(r"\s*\(_ssl\.c:\d+\)", "", str(err))
        if "CERTIFICATE_VERIFY_FAILED" in text:
            return "TLS certificate couldn't be verified (--insecure skips this)"
        return f"TLS problem: {text}"
    if isinstance(err, URLError) or isinstance(err, OSError):
        text = str(err) or err.__class__.__name__
        return text.replace("[Errno ", "errno ").strip()
    return str(err)


def fetch(url: str, timeout: float, insecure: bool = False) -> Fetch:
    context = ssl.create_default_context()
    if insecure:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    opener = build_opener(_NoAutoRedirect(), HTTPSHandler(context=context))
    request = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": ACCEPT,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
    }, method="GET")

    try:
        with opener.open(request, timeout=timeout) as response:
            return Fetch(url, response.status, dict(response.headers),
                         response.read(MAX_BODY))
    except HTTPError as err:                      # includes the 3xx we refused
        try:
            body = err.read(MAX_BODY)
        except Exception:                         # pragma: no cover - broken socket
            body = b""
        headers = dict(err.headers or {})
        return Fetch(url, int(err.code), headers, body)
    except (URLError, OSError, ValueError, ssl.SSLError) as err:
        return Fetch(url, 0, {}, b"", error=describe_error(err))


# --------------------------------------------------------------------------- #
# Reading a page
# --------------------------------------------------------------------------- #

@dataclass
class Found:
    url: str
    source: str      # human label, e.g. "meta refresh"
    score: int = 10  # 10 = explicit, lower = guesswork


def title_of(text: str) -> str | None:
    match = TITLE_RE.search(text)
    if not match:
        return None
    title = re.sub(r"\s+", " ", html.unescape(match.group(1))).strip()
    return title[:160] or None


def from_meta_refresh(text: str, current: str) -> Found | None:
    for pattern in (META_REFRESH_RE, META_REFRESH_RE_ALT):
        for match in pattern.finditer(text):
            value = match.group(1).strip()
            inner = re.search(r"url\s*=\s*(\S+)", value, re.I)  # "3; url=/next" → "/next"
            if inner:
                value = inner.group(1)
            value = value.split()[0] if value.split() else value
            candidate = decode_candidate(value.rstrip(";"), current)
            if candidate:
                return Found(candidate, "meta refresh")
    return None


def from_url_parameters(current: str) -> Found | None:
    """The short link itself carries the target: /go?url=https%3A%2F%2F…"""
    parsed = urlparse(current)
    params = parse_qs(parsed.query, keep_blank_values=True)
    params.update({k: v for k, v in parse_qs(parsed.fragment).items() if k not in params})
    for key, values in params.items():
        if key.lower() not in URL_PARAM_KEYS:
            continue
        for value in values:
            candidate = decode_candidate(value, current)
            if candidate:
                return Found(candidate, f"?{key}= parameter")
    return None


def from_javascript(text: str, current: str) -> Found | None:
    for pattern in (JS_LOCATION_RE, JS_LOCATION_ALT_RE):
        for match in pattern.finditer(text):
            candidate = decode_candidate(match.group(1), current)
            if candidate:
                return Found(candidate, "location.href in the page")
    return None


def from_data_attributes(text: str, current: str) -> Found | None:
    for match in DATA_URL_RE.finditer(text):
        candidate = decode_candidate(match.group(1), current)
        if candidate:
            return Found(candidate, "data-* attribute")
    return None


def from_json_blobs(text: str, current: str) -> Found | None:
    for match in JSON_URL_RE.finditer(text):
        candidate = decode_candidate(match.group(1), current)
        if candidate:
            return Found(candidate, '"url" field in embedded JSON')
    return None


ASSET_EXTENSIONS = (".css", ".js", ".mjs", ".png", ".jpg", ".jpeg", ".gif", ".svg",
                    ".webp", ".ico", ".woff", ".woff2", ".ttf", ".eot", ".map",
                    ".mp4", ".webm", ".mp3", ".avif")

CHROME_REGIONS = ("nav", "header", "footer", "aside")


def inside_chrome(text: str, position: int) -> bool:
    """Cheap check: is this offset inside a nav/header/footer block?"""
    depth = {region: 0 for region in CHROME_REGIONS}
    for match in re.finditer(r"</?(nav|header|footer|aside)\b", text[:position], re.I):
        closing = match.group(0).startswith("</")
        region = match.group(1).lower()
        depth[region] = max(0, depth[region] - 1) if closing else depth[region] + 1
    return any(depth.values())


def external_candidates(text: str, current: str, limit: int = 6) -> list[Found]:
    """Links a human would click. Scored, and only followed when obvious."""
    scores: dict[str, tuple[int, str]] = {}
    here = current.split("?")[0].split("#")[0].rstrip("/")

    for match in ANCHOR_RE.finditer(text):
        href, label = match.group(1), match.group(2)
        if "@" in href and not href.lower().startswith(("http://", "https://")):
            continue                          # mailto:, javascript:void(…)
        candidate = decode_candidate(href, current)
        if not candidate:
            continue
        host = host_of(candidate)
        path = (urlparse(candidate).path or "").lower()
        if (not host or host_matches(host, NOISE_HOSTS)
                or path.endswith(ASSET_EXTENSIONS)
                or candidate.split("?")[0].rstrip("/") == here):
            continue
        onto_another_site = not same_site(candidate, current)
        text_only = re.sub(r"<[^>]+>", " ", label)
        words = bool(CONTINUE_WORDS.search(text_only))
        if not onto_another_site and (not words or inside_chrome(text, match.start())):
            continue                          # same-site nav/"about us" links: out
        score = 4 if onto_another_site else 3
        if words:
            score += 5
        if CONTINUE_PATHS.search(path):
            score += 3
        previous = scores.get(candidate)
        if previous is None or previous[0] < score:
            scores[candidate] = (score, "link in the page")

    for match in ANY_URL_RE.finditer(text):
        candidate = decode_candidate(match.group(0), current)
        if not candidate:
            continue
        host = host_of(candidate)
        path = (urlparse(candidate).path or "").lower()
        if (not host or host_matches(host, NOISE_HOSTS) or same_site(candidate, current)
                or path.endswith(ASSET_EXTENSIONS)):
            continue
        scores.setdefault(candidate, (1, "URL in the page source"))

    ordered = sorted(scores.items(), key=lambda item: (-item[1][0], len(item[0])))
    return [Found(url, source, score) for url, (score, source) in ordered[:limit]]


def extract(fetched: Fetch, current: str) -> tuple[Found | None, list[Found]]:
    """Return (the destination to follow, other candidates worth showing)."""
    text = fetched.text()

    if fetched.refresh:
        match = REFRESH_HEADER_RE.search(fetched.refresh)
        if match:
            candidate = decode_candidate(match.group(1), current)
            if candidate:
                return Found(candidate, "Refresh header"), []

    if fetched.content_type and "html" not in fetched.content_type \
            and "text" not in fetched.content_type and "json" not in fetched.content_type:
        return None, []          # a binary payload has no next hop

    found = (from_meta_refresh(text, current)
             or from_javascript(text, current)
             or from_url_parameters(current)
             or from_data_attributes(text, current)
             or from_json_blobs(text, current))
    if found:
        return found, []

    candidates = external_candidates(text, current)
    strong = [c for c in candidates if c.score >= 8]
    if strong:
        return strong[0], [c for c in candidates if c.url != strong[0].url]
    return None, candidates


# --------------------------------------------------------------------------- #
# The resolver
# --------------------------------------------------------------------------- #

@dataclass
class Hop:
    url: str
    status: int
    note: str
    elapsed_ms: int = 0
    location: str | None = None
    error: str | None = None
    title: str | None = None


@dataclass
class Result:
    input_url: str
    final_url: str | None = None
    verdict: str = "error"          # resolved | already_main | gateway | loop | error
    via: list[str] = field(default_factory=list)
    hops: list[Hop] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    clean_url: str | None = None
    title: str | None = None
    note: str = ""
    shortener: bool = False
    elapsed_ms: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def analyze_destination(url: str, start: str, hops: int) -> list[str]:
    flags: list[str] = []
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()

    if parsed.scheme == "http":
        flags.append("served over HTTP, not HTTPS")
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host or ""):
        flags.append("host is a bare IP address")
    if host.startswith("xn--") or ".xn--" in host:
        flags.append("punycode host — may be a look-alike domain")
    if "@" in (parsed.netloc or ""):
        flags.append("'@' in the URL — the real host is hidden after it")
    if parsed.port and parsed.port not in (80, 443):
        flags.append(f"non-standard port {parsed.port}")
    tld = host.rsplit(".", 1)[-1] if "." in host else ""
    if tld in RISKY_TLDS:
        flags.append(f".{tld} is a TLD with a lot of abuse")
    if path.endswith(DOWNLOAD_EXTENSIONS):
        flags.append(f"links to a downloadable file ({path.rsplit('.', 1)[-1]})")
    if hops >= 6:
        flags.append(f"long chain — {hops} hops before the destination")
    if is_gateway(url):
        flags.append("destination is itself a link gate")
    return flags


def route_locally(url: str, self_routes: dict[str, str]) -> str:
    """If a link points back at this very server, fetch it over loopback.

    Hosts this service serves are often only reachable from outside (a proxy, a
    container name, a preview URL), and this machine may not be able to resolve
    or reach its own public address. The URL we *show* is untouched; only the
    round trip changes.
    """
    host = host_of(url)
    if host not in self_routes:
        return url
    parsed = urlparse(url)
    return urlunparse(parsed._replace(scheme="http", netloc=self_routes[host]))


def resolve(raw_url: str, *, max_hops: int = DEFAULT_MAX_HOPS,
            timeout: float = DEFAULT_TIMEOUT, budget: float = DEFAULT_BUDGET,
            allow_private: bool = False, allow_hosts: set[str] | None = None,
            self_routes: dict[str, str] | None = None,
            insecure: bool = False, guess: bool = True) -> Result:
    started = time.monotonic()
    result = Result(input_url=raw_url)

    try:
        start = normalize_url(raw_url)
    except ResolveError as exc:
        result.note = str(exc)
        result.final_url = None
        return result

    result.input_url = start
    result.shortener = is_shortener(start)
    allowed = {h.lower() for h in (allow_hosts or set())}
    deadline = started + budget
    seen = {start}
    current = start
    hops: list[Hop] = []
    via: list[str] = []
    candidates: list[Found] = []
    verdict = "error"
    note = ""
    title: str | None = None

    for index in range(max_hops):
        if time.monotonic() > deadline:
            note = f"Stopped after {budget:.0f}s — still had hops left to follow."
            break

        host = host_of(current)
        if not allow_private and host not in allowed:
            blocked = private_host_reason(host)
            if blocked:
                hops.append(Hop(current, 0, f"blocked: {blocked}"))
                note = (f"{blocked} — refusing to fetch it from a server. "
                        f"Pass --allow-private if this is your own machine.")
                verdict = "error"
                break

        tick = time.monotonic()
        fetched = fetch(route_locally(current, self_routes or {}), timeout, insecure)
        elapsed = int((time.monotonic() - tick) * 1000)

        if fetched.error:
            hops.append(Hop(current, 0, "no answer", elapsed, error=fetched.error))
            if index == 0:
                note = f"Couldn't reach {host}: {fetched.error}."
                verdict = "error"
            else:
                note = f"Stopped at hop {index + 1}: {fetched.error}."
                verdict = "resolved"
            break

        if fetched.status in REDIRECT_STATUSES and fetched.location:
            nxt = urljoin(current, fetched.location)
            hops.append(Hop(current, fetched.status, "→ redirect", elapsed,
                            location=nxt))
            via.append(f"{fetched.status} redirect")
            try:
                nxt = normalize_url(nxt)
            except ResolveError as exc:
                note = f"Redirect pointed at something unusable ({exc})."
                break
            if nxt in seen:
                hops.append(Hop(nxt, 0, "loop — already visited"))
                verdict, note = "loop", "The chain loops back on itself; no final link."
                current = nxt
                break
            seen.add(nxt)
            current = nxt
            continue

        if fetched.status >= 400:
            hops.append(Hop(current, fetched.status, "error page", elapsed,
                            title=title_of(fetched.text())))
            note = (f"The link answers {fetched.status} "
                    f"({describe_status(fetched.status)}) — nothing to follow.")
            verdict = "error"
            break

        text = fetched.text()
        title = title_of(text)
        found, extras = extract(fetched, current)
        candidates.extend(extras)

        if found and (guess or found.score >= 10):
            hops.append(Hop(current, fetched.status,
                            f"→ {found.source}", elapsed, location=found.url))
            via.append(found.source)
            if found.url in seen:
                hops.append(Hop(found.url, 0, "loop — already visited"))
                verdict, note = "loop", "The chain loops back on itself; no final link."
                break
            seen.add(found.url)
            current = found.url
            continue

        hops.append(Hop(current, fetched.status, "final page", elapsed, title=title))
        if found:
            candidates.insert(0, found)
        verdict = "already_main" if len(hops) == 1 else "resolved"
        break
    else:
        note = (f"Hit the {max_hops}-hop ceiling without settling on a destination — "
                f"raise --max-hops if the chain is genuinely that long.")

    if verdict == "error" and not note:
        note = "Couldn't resolve this link."

    final = current if verdict in ("resolved", "already_main") else None

    if final and is_gateway(final):
        verdict = "gateway"
        if not via:
            note = (f"This link is a monetised gate ({host_of(final)}). The target is "
                    f"handed to JavaScript, usually after an ad or a captcha — there is "
                    f"nothing static to read, and this tool won't fake a browser to "
                    f"harvest it.")
        else:
            note = (f"The chain ends on a monetised gate ({host_of(final)}). The real "
                    f"destination is handed out by JavaScript after an ad or captcha, so "
                    f"it can't be read from here — open it in a browser if you trust it.")
    elif verdict == "already_main" and not note:
        note = "This link doesn't redirect anywhere — it's already the main link."
    elif verdict == "resolved" and not note:
        note = f"Resolved after {len(via)} step{'s' if len(via) != 1 else ''}."
        if hops and hops[-1].error:
            note = (f"Destination found via {via[-1]}, but it couldn't be fetched from "
                    f"here ({hops[-1].error}).")

    result.hops = hops
    result.via = via
    result.final_url = final
    result.clean_url = strip_tracking(final) if final else None
    result.title = title
    result.verdict = verdict
    if not note:
        if verdict == "already_main":
            note = "This link doesn't redirect anywhere — it's already the main link."
        elif verdict == "resolved":
            note = f"Resolved after {len(via)} step{'s' if len(via) != 1 else ''}."
    result.note = note
    result.flags = analyze_destination(final, start, len(hops)) if final else []
    result.candidates = [
        {"url": c.url, "source": c.source, "score": c.score}
        for c in dedupe_candidates(candidates, final)
    ][:6]
    result.elapsed_ms = int((time.monotonic() - started) * 1000)
    return result


def dedupe_candidates(candidates: list[Found], final: str | None) -> list[Found]:
    seen: set[str] = set()
    out: list[Found] = []
    for candidate in candidates:
        key = candidate.url.rstrip("/")
        if key in seen or (final and key == final.rstrip("/")):
            continue
        seen.add(key)
        out.append(candidate)
    return out


def private_host_reason(host: str) -> str | None:
    if not host:
        return "there's no host in that link"
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return f"{host} is a local address"
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return None                     # unknown host: let the fetch fail normally
    for info in infos:
        address = info[4][0].split("%")[0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if not ip.is_global:
            if address == host:
                return f"{host} is a private address"
            return f"{host} resolves to the private address {address}"
    return None


# --------------------------------------------------------------------------- #
# Terminal output
# --------------------------------------------------------------------------- #

class Paint:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, text: str) -> str:
        return self("1", text)

    def dim(self, text: str) -> str:
        return self("2", text)

    def green(self, text: str) -> str:
        return self("32", text)

    def yellow(self, text: str) -> str:
        return self("33", text)

    def red(self, text: str) -> str:
        return self("31", text)

    def cyan(self, text: str) -> str:
        return self("36", text)


def render_result(result: Result, paint: Paint) -> str:
    lines: list[str] = []
    lines.append(f"{paint.bold('link')}  {result.input_url}")

    if result.hops:
        width = max(len(str(len(result.hops))), 1)
        for index, hop in enumerate(result.hops, start=1):
            host = host_of(hop.url) or hop.url
            status = str(hop.status) if hop.status else "---"
            tail = hop.error or hop.note
            if hop.location:
                tail = f"{hop.note}  {paint.cyan(hop.location)}"
            lines.append(
                f"  {str(index).rjust(width)}. {status:>3}  {host:<22.22} "
                f"{tail}{paint.dim(f'  ({hop.elapsed_ms} ms)') if hop.elapsed_ms else ''}")

    lines.append("")
    if result.final_url:
        lines.append(f"{paint.bold('main link')}  {paint.green(result.final_url)}")
        if result.clean_url and result.clean_url != result.final_url:
            lines.append(f"{paint.bold('clean url')}  {result.clean_url}")
        if result.title:
            lines.append(f"{paint.bold('page')}       {result.title}")
        lines.append(f"{paint.bold('found via')}  "
                     + (" → ".join(result.via) if result.via else "no redirect"))
    else:
        lines.append(f"{paint.bold('main link')}  {paint.yellow('not found')}")
    lines.append(f"{paint.bold('verdict')}    {result.verdict.replace('_', ' ')}"
                 f"{paint.dim(f'  ({result.elapsed_ms} ms)')}")
    lines.append(f"{paint.bold('note')}       {result.note}")

    if result.flags:
        lines.append("")
        for flag in result.flags:
            lines.append(f"{paint.yellow('  ! ' + flag)}")
    if result.candidates:
        lines.append("")
        lines.append(paint.dim("possible destinations (guesses, not followed):"))
        for candidate in result.candidates:
            lines.append(f"  ? {candidate['url']}  {paint.dim('— ' + candidate['source'])}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Web UI
# --------------------------------------------------------------------------- #

PAGE_CSS = """
:root {
  color-scheme: dark;
  --bg: #0b0d12; --panel: #141a26; --panel-2: #1b2231; --border: #242d40;
  --text: #e8edf7; --muted: #8b98ad; --accent: #6ea8fe; --ok: #7ee0b8;
  --warn: #ffc857; --danger: #ff6b81; --radius: 12px;
}
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh; color: var(--text);
  background: radial-gradient(1100px 520px at 50% -12%, #16203a 0%, var(--bg) 62%) fixed;
  font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
main { max-width: 880px; margin: 0 auto; padding: 40px 20px 72px; }
header h1 { margin: 0; font-size: 28px; letter-spacing: -0.4px; }
header h1 span { color: var(--accent); }
header p { margin: 6px 0 22px; color: var(--muted); }
code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.card {
  background: linear-gradient(180deg, var(--panel), var(--panel-2));
  border: 1px solid var(--border); border-radius: var(--radius);
  padding: 18px; margin-bottom: 16px;
}
form.go { display: flex; gap: 10px; flex-wrap: wrap; }
input[type=text] {
  flex: 1 1 340px; padding: 12px; color: var(--text); background: #0e1420;
  border: 1px solid var(--border); border-radius: 9px; font: inherit;
}
input[type=text]:focus { outline: 2px solid rgba(110,168,254,.45); outline-offset: 1px; }
button {
  cursor: pointer; border: 0; border-radius: 9px; padding: 12px 20px;
  background: var(--accent); color: #071022; font: inherit; font-weight: 600;
}
button:hover { filter: brightness(1.08); }
button:disabled { opacity: .55; cursor: progress; }
button.mini {
  background: transparent; color: var(--muted); border: 1px solid var(--border);
  padding: 6px 12px; font-weight: 500; font-size: 13px;
}
button.mini:hover { color: var(--accent); border-color: var(--accent); }
.chips { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
.chips span { color: var(--muted); font-size: 13px; align-self: center; }
.verdict { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.badge {
  font-size: 12px; text-transform: uppercase; letter-spacing: .08em; font-weight: 700;
  padding: 4px 10px; border-radius: 999px; border: 1px solid;
}
.badge.ok { color: var(--ok); border-color: rgba(126,224,184,.45); background: rgba(126,224,184,.08); }
.badge.warn { color: var(--warn); border-color: rgba(255,200,87,.45); background: rgba(255,200,87,.08); }
.badge.bad { color: var(--danger); border-color: rgba(255,107,129,.5); background: rgba(255,107,129,.09); }
.meta { color: var(--muted); font-size: 13px; }
.out {
  display: flex; gap: 10px; align-items: center; margin: 12px 0 6px; flex-wrap: wrap;
}
.out input {
  flex: 1 1 320px; padding: 11px 12px; background: #0e1420; color: var(--ok);
  border: 1px solid var(--border); border-radius: 9px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.chain { list-style: none; margin: 8px 0 0; padding: 0; }
.chain li {
  display: grid; grid-template-columns: 30px 56px 1fr auto; gap: 10px;
  align-items: baseline; padding: 9px 0; border-top: 1px solid var(--border);
}
.chain .n { color: var(--muted); }
.chain .st { font-family: ui-monospace, monospace; font-size: 13px; color: var(--accent); }
.chain .n2 { min-width: 0; word-break: break-all; }
.chain .src { color: var(--muted); font-size: 12px; }
.chain .ms { color: var(--muted); font-size: 12px; white-space: nowrap; }
.flags { list-style: none; margin: 10px 0 0; padding: 0; }
.flags li { padding: 6px 0; color: var(--warn); }
.guess { color: var(--muted); font-size: 13px; margin-top: 10px; }
.guess li { padding: 4px 0; word-break: break-all; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.err { color: var(--danger); }
.spin { color: var(--muted); margin-top: 12px; }
footer { color: var(--muted); font-size: 13px; margin-top: 24px; text-align: center; }
@media (max-width: 620px) { .chain li { grid-template-columns: 24px 1fr; } .chain .ms, .chain .src { display: none; } }
"""

PAGE = Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>leave-it-on-me</title>
<style>$css</style>
</head>
<body>
<main>
<header>
  <h1>leave-it-<span>on-me</span></h1>
  <p>Short link in, main link out. Paste a bit.ly / t.co / tinyurl / whatever link
     and see where it really goes — redirect chain, page tricks and all.</p>
</header>

<section class="card">
  <form class="go" id="form">
    <input type="text" id="url" name="url" autocomplete="off" spellcheck="false"
           autofocus placeholder="https://bit.ly/3xyzAb or any short link">
    <button type="submit" id="go">resolve</button>
  </form>
  <div class="chips" id="chips"></div>
</section>

<div id="error"></div>
<section class="card" id="result" hidden></section>

<footer>single-file resolver · no JavaScript executed · <a href="/api/resolve?url=https%3A%2F%2Fexample.com">api</a></footer>
$script
</main>
</body>
</html>
""")

UI_SCRIPT = """<script>
const form = document.getElementById("form");
const input = document.getElementById("url");
const button = document.getElementById("go");
const resultBox = document.getElementById("result");
const errorBox = document.getElementById("error");
const chips = document.getElementById("chips");

const escapeHtml = (text) => String(text == null ? "" : text)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;");

const VERDICTS = {
  resolved: ["ok", "resolved"],
  already_main: ["ok", "already the main link"],
  gateway: ["warn", "monetised gate"],
  loop: ["bad", "redirect loop"],
  error: ["bad", "couldn't resolve"],
};

$demo_chips

function render(data) {
  const [tone, label] = VERDICTS[data.verdict] || ["bad", data.verdict];
  let html = '<div class="verdict"><span class="badge ' + tone + '">' + escapeHtml(label) +
    '</span><span class="meta">' + escapeHtml(data.note) + ' · ' + data.elapsed_ms + ' ms</span></div>';

  if (data.final_url) {
    html += '<div class="out"><input id="final" readonly value="' + escapeHtml(data.final_url) + '">' +
      '<button type="button" class="mini" data-copy="#final">copy</button>' +
      '<a class="mini" href="' + escapeHtml(data.final_url) + '" target="_blank" rel="noopener noreferrer">open</a></div>';
    if (data.clean_url && data.clean_url !== data.final_url) {
      html += '<div class="meta">clean: <span class="mono">' + escapeHtml(data.clean_url) + '</span></div>';
    }
  }
  if (data.title) {
    html += '<div class="meta">page title: ' + escapeHtml(data.title) + '</div>';
  }

  if (data.hops && data.hops.length) {
    html += '<ul class="chain">';
    data.hops.forEach((hop, index) => {
      html += '<li><span class="n">' + (index + 1) + '.</span>' +
        '<span class="st">' + (hop.status || "---") + '</span>' +
        '<span class="n2">' + escapeHtml(hop.url) +
        (hop.location ? '<br><span class="src">' + escapeHtml(hop.note) + ' → ' +
          '<span class="mono">' + escapeHtml(hop.location) + '</span></span>' : '') +
        (hop.error ? '<br><span class="src err">' + escapeHtml(hop.error) + '</span>' : '') +
        (!hop.location && !hop.error ? '<br><span class="src">' + escapeHtml(hop.note) +
          (hop.title ? ' · ' + escapeHtml(hop.title) : '') + '</span>' : '') +
        '</span><span class="ms">' + (hop.elapsed_ms ? hop.elapsed_ms + ' ms' : '') + '</span></li>';
    });
    html += '</ul>';
  }

  if (data.flags && data.flags.length) {
    html += '<ul class="flags">';
    data.flags.forEach((flag) => { html += '<li>! ' + escapeHtml(flag) + '</li>'; });
    html += '</ul>';
  }

  if (data.candidates && data.candidates.length) {
    html += '<div class="guess">possible destinations (guesses, not followed)<ul>';
    data.candidates.forEach((candidate) => {
      html += '<li>' + escapeHtml(candidate.url) + ' <span class="src">— ' +
        escapeHtml(candidate.source) + '</span></li>';
    });
    html += '</ul></div>';
  }

  resultBox.innerHTML = html;
  resultBox.hidden = false;
}

async function run(url) {
  errorBox.innerHTML = "";
  button.disabled = true;
  resultBox.hidden = true;
  const spin = document.createElement("p");
  spin.className = "spin";
  spin.textContent = "walking the chain…";
  errorBox.appendChild(spin);
  try {
    const response = await fetch("/api/resolve", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({url: url}),
    });
    const data = await response.json();
    spin.remove();
    if (!response.ok) {
      errorBox.innerHTML = '<section class="card err">' + escapeHtml(data.error || "failed") + '</section>';
      return;
    }
    render(data);
  } catch (err) {
    spin.remove();
    errorBox.innerHTML = '<section class="card err">' + escapeHtml(String(err)) + '</section>';
  } finally {
    button.disabled = false;
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const url = input.value.trim();
  if (url) run(url);
});

document.addEventListener("click", (event) => {
  const copy = event.target.closest("[data-copy]");
  if (copy) {
    const field = document.querySelector(copy.getAttribute("data-copy"));
    if (field) {
      field.select();
      if (navigator.clipboard) navigator.clipboard.writeText(field.value);
      else document.execCommand("copy");
      const old = copy.textContent;
      copy.textContent = "copied!";
      setTimeout(() => { copy.textContent = old; }, 1200);
    }
    return;
  }
  const chip = event.target.closest("[data-fill]");
  if (chip) {
    input.value = chip.getAttribute("data-fill");
    run(input.value);
  }
});

const params = new URLSearchParams(location.search);
if (params.get("url")) {
  input.value = params.get("url");
  run(input.value);
}
</script>"""

DEMO_CHIPS_SCRIPT = """const DEMOS = [
  ["302 redirect chain", "/demo/chain"],
  ["meta refresh", "/demo/meta"],
  ["location.href", "/demo/js"],
  ["?url= parameter", "/demo/param?url=" + encodeURIComponent(location.origin + "/demo/dest/param-target")],
  ["'continue' link", "/demo/continue"],
  ["monetised gate", "/demo/gate"],
  ["redirect loop", "/demo/loop-a"],
  ["to a .exe", "/demo/download"],
];
if (DEMOS.length) {
  chips.innerHTML = '<span>try:</span>' + DEMOS.map(([label, path]) =>
    '<button type="button" class="mini" data-fill="' + location.origin + path + '">' +
    label + '</button>').join("");
}"""


def render_page(script: str = "") -> bytes:
    return PAGE.substitute(css=PAGE_CSS, script=UI_SCRIPT.replace("$demo_chips", script)) \
        .encode("utf-8")


# --------------------------------------------------------------------------- #
# Demo fixtures (offline sandbox so the UI has something real to chew on)
# --------------------------------------------------------------------------- #

DEMO_REDIRECTS = {
    "/demo/chain": "/demo/chain/2",
    "/demo/chain/2": "/demo/dest/article",
    "/demo/loop-a": "/demo/loop-b",
    "/demo/loop-b": "/demo/loop-a",
    "/demo/download": "/demo/dest/setup.exe",
    "/demo/param": None,
}

DEMO_DESTINATIONS = {
    "/demo/dest/article": ("The article you actually wanted", "text/html"),
    "/demo/dest/meta-target": ("Meta refresh landed here", "text/html"),
    "/demo/dest/js-target": ("JavaScript hop landed here", "text/html"),
    "/demo/dest/param-target": ("?url= parameter landed here", "text/html"),
    "/demo/dest/download-ready": ("The download is this way", "text/html"),
    "/demo/dest/setup.exe": ("binary file", "application/octet-stream"),
}


def demo_page(title: str, body: str) -> bytes:
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{html.escape(title)}</title></head>"
            f"<body style='font-family:system-ui;background:#0b0d12;color:#e8edf7;"
            f"padding:40px'><h1>{html.escape(title)}</h1><p>{body}</p></body></html>"
            ).encode("utf-8")


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #

class ResolveHandler(BaseHTTPRequestHandler):
    server_version = "leave-it-on-me/2.0"
    protocol_version = "HTTP/1.1"

    # -- helpers ----------------------------------------------------------- #

    @property
    def limiter(self):
        return self.server.limiter            # type: ignore[attr-defined]

    def log_message(self, fmt, *args):
        if not getattr(self.server, "quiet", False):
            print(f"{self.log_date_time_string()} {self.address_string()} {fmt % args}",
                  flush=True)

    def own_hosts(self) -> set[str]:
        """Hosts we're allowed to fetch even though they're 'private'. Only our
        own Host header, and (in demo mode) loopback for the fixture links."""
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        hosts = {host} if host else set()
        if getattr(self.server, "demo", False):
            hosts |= {"localhost", "127.0.0.1"}
        return hosts

    def self_routes(self) -> dict[str, str]:
        route = f"127.0.0.1:{self.server.server_address[1]}"  # type: ignore[attr-defined]
        return {host: route for host in self.own_hosts()}

    def client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For")
        return forwarded.split(",")[0].strip() if forwarded else self.client_address[0]

    def body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        return self.rfile.read(max(0, min(length, 65536))) if length else b""

    def handle_one_request(self) -> None:
        """Never leave a client staring at an empty reply if we have a bug."""
        self.responded = False
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:                       # noqa: BLE001 - last resort
            self.close_connection = True
            if not self.responded:
                try:
                    self.json_response(HTTPStatus.INTERNAL_SERVER_ERROR,
                                       {"error": f"internal error: {exc}"})
                except Exception:                      # pragma: no cover
                    pass

    def respond(self, status: int, body: bytes = b"", *,
                content_type: str = "text/plain; charset=utf-8",
                headers: dict | None = None, head_only: bool = False) -> None:
        self.responded = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if status not in (204, 304):
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
                              "Access-Control-Allow-Origin": "*"}, head_only=head_only)

    # -- routes ------------------------------------------------------------ #

    def do_GET(self, head_only: bool = False) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            script = DEMO_CHIPS_SCRIPT if getattr(self.server, "demo", False) else ""
            return self.respond(HTTPStatus.OK, render_page(script),
                                content_type="text/html; charset=utf-8",
                                headers={"Cache-Control": "no-store"}, head_only=head_only)

        if path == "/healthz":
            return self.json_response(HTTPStatus.OK, {
                "ok": True,
                "demo": bool(getattr(self.server, "demo", False)),
                "uptime_seconds": round(time.time() - self.server.started_at, 1),
            }, head_only)

        if path == "/favicon.ico":
            return self.respond(HTTPStatus.NO_CONTENT, head_only=head_only)

        if path == "/api/resolve":
            target = (parse_qs(parsed.query).get("url") or [""])[0]
            if not target:
                return self.json_response(HTTPStatus.BAD_REQUEST,
                                          {"error": "pass ?url=<short link>"}, head_only)
            return self.resolve_and_reply(target, head_only)

        if path.startswith("/api/"):
            return self.json_response(HTTPStatus.NOT_FOUND,
                                      {"error": "unknown endpoint"}, head_only)

        if getattr(self.server, "demo", False) and path.startswith("/demo"):
            return self.serve_demo(parsed, head_only)

        return self.respond(HTTPStatus.NOT_FOUND, b"not found\n", head_only=head_only)

    def do_HEAD(self) -> None:
        self.do_GET(head_only=True)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/resolve":
            return self.json_response(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})
        raw = self.body().decode("utf-8", "replace")
        target = ""
        if raw.strip().startswith("{"):
            try:
                payload = json.loads(raw)
                target = str(payload.get("url") or "")
            except json.JSONDecodeError:
                return self.json_response(HTTPStatus.BAD_REQUEST,
                                          {"error": "invalid JSON body"})
        else:
            target = (parse_qs(raw).get("url") or [""])[0]
        if not target:
            return self.json_response(HTTPStatus.BAD_REQUEST, {"error": "no url given"})
        return self.resolve_and_reply(target)

    def do_OPTIONS(self) -> None:
        self.respond(HTTPStatus.NO_CONTENT, headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "86400",
        })

    def resolve_and_reply(self, target: str, head_only: bool = False) -> None:
        if not self.limiter.allow(self.client_ip()):
            return self.json_response(HTTPStatus.TOO_MANY_REQUESTS,
                                      {"error": "too many resolutions from this address — "
                                                "give it a minute"}, head_only)
        result = resolve(
            target,
            max_hops=getattr(self.server, "max_hops", DEFAULT_MAX_HOPS),
            timeout=getattr(self.server, "timeout", DEFAULT_TIMEOUT),
            allow_hosts=self.own_hosts(),
            self_routes=self.self_routes(),
            allow_private=getattr(self.server, "allow_private", False),
            insecure=getattr(self.server, "insecure", False),
        )
        status = HTTPStatus.OK
        if result.verdict == "error" and not result.hops:
            status = HTTPStatus.BAD_REQUEST
        return self.json_response(status, result.as_dict(), head_only)

    # -- demo fixtures ------------------------------------------------------ #

    def serve_demo(self, parsed, head_only: bool = False) -> None:
        path, query = parsed.path, parse_qs(parsed.query)

        if path == "/demo/param":
            target = (query.get("url") or ["/demo/dest/param-target"])[0]
            body = demo_page("Just a moment…", (
                f"We are preparing <span class='mono'>{html.escape(target)}</span> for you."
                "<p>No redirect happens here — the destination is sitting in the "
                "<span class='mono'>?url=</span> parameter of this very URL.</p>"))
            return self.respond(HTTPStatus.OK, body,
                                content_type="text/html; charset=utf-8", head_only=head_only)

        if path in DEMO_REDIRECTS and DEMO_REDIRECTS[path]:
            return self.redirect(HTTPStatus.FOUND, DEMO_REDIRECTS[path], head_only)

        if path == "/demo/meta":
            body = demo_page("Please wait…", (
                "<meta http-equiv='refresh' content='3; url=/demo/dest/meta-target'>"
                "Redirecting you in 3 seconds. Ad slot here. Ad slot there."))
            return self.respond(HTTPStatus.OK, body,
                                content_type="text/html; charset=utf-8", head_only=head_only)

        if path == "/demo/js":
            body = demo_page("Preparing your link…", (
                "<p id='count'>5</p><script>var t=5;setInterval(function(){"
                "t--;document.getElementById('count').textContent=t;"
                "if(t<=0){window.location.href='/demo/dest/js-target';}},1000);</script>"))
            return self.respond(HTTPStatus.OK, body,
                                content_type="text/html; charset=utf-8", head_only=head_only)

        if path == "/demo/continue":
            body = demo_page("Your download is ready", (
                "<a href='/demo/dest/download-ready'>Continue to the download</a>"
                "<p><a href='https://example.com/sponsor'>Our sponsor</a></p>"))
            return self.respond(HTTPStatus.OK, body,
                                content_type="text/html; charset=utf-8", head_only=head_only)

        if path == "/demo/gate":
            # A real gate host, on purpose: this sandbox has no outbound network, so
            # the chain ends with "known gate, target not readable" — which is exactly
            # what the tool reports for a gate that hands its target to JavaScript.
            return self.redirect(HTTPStatus.FOUND, "https://linkvertise.com/00000/demo",
                                 head_only)

        if path == "/demo/gate-page":
            body = demo_page("Verifying your browser", (
                "<p>This gate hands the destination to JavaScript after an ad and a "
                "captcha. There is no plain URL in this page at all — which is exactly "
                "what leave-it-on-me reports instead of pretending to bypass it.</p>"
                "<script>var payload=null;function unlock(token){return payload;}</script>"))
            return self.respond(HTTPStatus.OK, body,
                                content_type="text/html; charset=utf-8", head_only=head_only)

        if path in DEMO_DESTINATIONS:
            title, content_type = DEMO_DESTINATIONS[path]
            if content_type == "application/octet-stream":
                return self.respond(HTTPStatus.OK, b"PK\x03\x04 demo payload",
                                    content_type=content_type,
                                    headers={"Content-Disposition": "attachment"},
                                    head_only=head_only)
            body = demo_page(title, "This is where the short link really pointed. "
                                    "Nothing else happens here.")
            return self.respond(HTTPStatus.OK, body, content_type=content_type,
                                head_only=head_only)

        return self.respond(HTTPStatus.NOT_FOUND, b"no such demo route\n", head_only)

    def redirect(self, status: int, location: str, head_only: bool = False) -> None:
        self.respond(status, b"", headers={"Location": location,
                                           "Cache-Control": "no-store"},
                     head_only=head_only)


class RateLimiter:
    """Sliding window, in memory: keeps a public instance from being an open proxy."""

    def __init__(self, limit: int, window: float = 600.0) -> None:
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
            if len(self._hits) > 5000:
                self._hits = {k: v for k, v in self._hits.items()
                              if v and now - v[-1] < self.window}
            return True


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="leave-it-on-me — turn a short link back into the real one",
        epilog="examples:\n"
               "  python main.py https://bit.ly/3xyzAb\n"
               "  python main.py bit.ly/3xyzAb --json\n"
               "  python main.py --serve --demo\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", nargs="?", help="the short link to resolve")
    parser.add_argument("--serve", action="store_true", help="run the web UI instead")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"),
                        help="interface for --serve (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")),
                        help="port for --serve (default: 8000)")
    parser.add_argument("--demo", action="store_true",
                        help="with --serve: add offline demo links to play with")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument("--max-hops", type=int, default=DEFAULT_MAX_HOPS,
                        help=f"give up after N hops (default: {DEFAULT_MAX_HOPS})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"seconds per request (default: {DEFAULT_TIMEOUT:g})")
    parser.add_argument("--budget", type=float, default=DEFAULT_BUDGET,
                        help=f"seconds for the whole chain (default: {DEFAULT_BUDGET:g})")
    parser.add_argument("--rate-limit", type=int, default=60,
                        help="with --serve: resolutions per IP per 10 min (0 = off)")
    parser.add_argument("--allow-private", action="store_true",
                        help="allow localhost/LAN targets (off by default)")
    parser.add_argument("--insecure", action="store_true",
                        help="skip TLS certificate verification")
    parser.add_argument("--no-guess", action="store_true",
                        help="don't follow guessed links; only explicit destinations")
    parser.add_argument("--no-color", action="store_true", help="plain output")
    parser.add_argument("--quiet", action="store_true",
                        help="with --serve: don't log requests")
    return parser.parse_args(argv)


def serve(args: argparse.Namespace) -> int:
    server = ThreadingHTTPServer((args.host, args.port), ResolveHandler)
    server.daemon_threads = True
    server.started_at = time.time()          # type: ignore[attr-defined]
    server.limiter = RateLimiter(args.rate_limit)  # type: ignore[attr-defined]
    server.demo = args.demo                  # type: ignore[attr-defined]
    server.quiet = args.quiet                # type: ignore[attr-defined]
    server.max_hops = args.max_hops          # type: ignore[attr-defined]
    server.timeout = args.timeout            # type: ignore[attr-defined]
    server.allow_private = args.allow_private  # type: ignore[attr-defined]
    server.insecure = args.insecure          # type: ignore[attr-defined]

    host, port = server.server_address[0], server.server_address[1]
    shown = "localhost" if host in ("0.0.0.0", "::") else host
    print(f"leave-it-on-me listening on http://{shown}:{port}")
    if args.demo:
        print("  demo links : /demo/chain, /demo/meta, /demo/js, /demo/param, "
              "/demo/continue, /demo/gate, /demo/loop-a, /demo/download")
    if not args.allow_private:
        print("  note       : localhost/LAN targets are refused (--allow-private to allow)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye 👋")
    finally:
        server.server_close()
    return 0


def resolve_once(args: argparse.Namespace) -> int:
    result = resolve(
        args.url,
        max_hops=args.max_hops,
        timeout=args.timeout,
        budget=args.budget,
        allow_private=args.allow_private,
        insecure=args.insecure,
        guess=not args.no_guess,
    )
    if args.json:
        print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    else:
        paint = Paint(enabled=sys.stdout.isatty() and not args.no_color)
        print(render_result(result, paint))
    if result.verdict in ("resolved", "already_main"):
        return 0
    return 1 if result.verdict == "error" else 2


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.serve:
        return serve(args)
    if not args.url:
        print("usage: python main.py <short link>      resolve one link\n"
              "       python main.py --serve            web UI\n"
              "       python main.py --help             all options", file=sys.stderr)
        return 2
    return resolve_once(args)


if __name__ == "__main__":
    raise SystemExit(main())
