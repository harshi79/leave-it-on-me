# leave-it-on-me

A short-link **resolver** that fits in a single `main.py`. Paste a short link,
get the main link back.

```
https://bit.ly/3xyzAb   →   https://example.com/the/real/page?with=params
```

No dependencies, no framework, no browser. It's the Python standard library
walking the redirect chain and reading what the page tells it — including pages
that only ever hand the destination to JavaScript.

## Run it

```bash
python main.py https://bit.ly/3xyzAb          # resolve one link
python main.py bit.ly/3xyzAb --json           # machine-readable
python main.py bit.ly/3xyzAb --no-color       # plain text
python main.py --serve                        # web UI on :8000
python main.py --serve --demo                 # ...with offline demo links
```

The web UI is a paste-and-go page: it shows the final URL, the whole hop-by-hop
chain, why each hop was taken, a "clean" URL with tracking parameters stripped,
and warnings about where it's about to send you.

## How it finds the destination

Redirects it follows itself:

| Signal | Example |
| --- | --- |
| HTTP `Location` | `301/302/303/307/308` — followed hop by hop, up to `--max-hops` |
| `Refresh` header | `Refresh: 0; url=/next` |
| `<meta http-equiv="refresh">` | `content="3; url=https://…"`, either attribute order |

Then, if the page is a gate that never redirects, it reads the destination out
of the page statically (no JavaScript is executed):

| Signal | Example it catches |
| --- | --- |
| `location.href` / `replace` / `assign` | `window.location.href="https://…"`, `top.location='…'` |
| URL parameters | `/go?url=`, `?r=`, `?target=`, `?redirect_uri=`, … |
| `data-*` attributes | `<div data-url="https://…">` |
| embedded JSON | `{"target": "https://…"}` in a config blob |
| base64 payloads | `?r=aHR0cHM6Ly9leGFtcGxlLmNvbQ` |
| "continue" links | `<a href="https://…">Continue to the download</a>` |

Anything weaker than that — a bare link in the page with neutral text — is
*listed as a guess* rather than silently followed, so you can see the
candidates without the tool guessing wrong. `--no-guess` disables even the
confident "continue" clicks, leaving only explicit, machine-readable hops.

Some things it deliberately will not do: a link **gate** (linkvertise,
shrinkme, gplinks and friends) hands its destination to JavaScript after an ad
or a captcha, and sometimes only after a server round-trip tied to your session.
There is nothing static to read, so the tool says exactly that instead of
pretending to bypass it. Faking a browser to defeat an ad gate isn't something
this does.

## Output

```
$ python main.py https://bit.ly/3xyzAb
link  https://bit.ly/3xyzAb
  1. 301  bit.ly                 → redirect  https://example.com/l/9f2 (48 ms)
  2. 200  example.com            final page  (112 ms)

main link  https://example.com/l/9f2?utm_source=newsletter
clean url  https://example.com/l/9f2
page       Example — the real page
found via  301 redirect
verdict    resolved  (171 ms)
note       Resolved after 1 step.

  ! .zip is a TLD with a lot of abuse
```

Verdicts: `resolved`, `already_main` (the link doesn't redirect anywhere),
`gateway` (a monetised gate), `loop` (the chain eats itself) and `error`.
Exit codes: `0` resolved, `1` error, `2` loop or gateway.

Every destination is also checked for things worth knowing before you click:
plain `http`, bare IP hosts, punycode look-alikes, `@`-obfuscated URLs,
non-standard ports, abuse-heavy TLDs, long chains, and links that end in an
executable or archive (`.exe`, `.apk`, `.zip`, …).

## HTTP API

| Method | Path | What it does |
| --- | --- | --- |
| `POST` | `/api/resolve` | `{"url": "https://bit.ly/x"}` → the full result as JSON |
| `GET` | `/api/resolve?url=…` | same thing, handy for `curl` |
| `GET` | `/healthz` | liveness |

```bash
curl -s 'localhost:8000/api/resolve?url=https://bit.ly/3xyzAb' | jq .final_url
```

```json
{
  "input_url": "https://bit.ly/3xyzAb",
  "final_url": "https://example.com/the/real/page",
  "clean_url": "https://example.com/the/real/page",
  "verdict": "resolved",
  "via": ["301 redirect", "meta refresh"],
  "hops": [{"url": "…", "status": 301, "note": "→ redirect", "elapsed_ms": 48}],
  "candidates": [{"url": "…", "source": "link in the page", "score": 4}],
  "flags": [], "title": "…", "note": "Resolved after 2 steps.", "elapsed_ms": 171
}
```

## Options worth knowing

| Flag | Why |
| --- | --- |
| `--max-hops N` | give up after N hops (default 12) |
| `--timeout S` / `--budget S` | per request / whole chain (default 12s / 45s) |
| `--insecure` | skip TLS verification, for a site with a broken certificate |
| `--allow-private` | allow localhost/LAN targets; off by default |
| `--no-guess` | only follow explicit, machine-readable destinations |
| `--rate-limit N` | with `--serve`: resolutions per IP per 10 min (default 60) |

## Notes

- **SSRF guard.** `http://127.0.0.1:…`, `10.x`, `192.168.x`, `169.254.x` and
  friends are refused by default, so a hosted instance can't be used to poke at
  the network behind it. A link that points at *this* server is fetched over
  loopback while still being displayed as the public URL, so a preview or proxy
  hostname resolves fine.
- **No JavaScript is executed** and no page scripts are trusted: destinations
  are read as text and validated (`http`/`https` only, no control characters),
  and `javascript:`, `data:` and `file:` values are dropped.
- **Tracking parameters** (`utm_*`, `fbclid`, `gclid`, `si`, `ttclid`, …) are
  stripped only in the `clean_url` field — `final_url` stays byte-for-byte what
  the chain produced.
- **HTML is generated from untrusted strings** (page titles, URLs, hostnames)
  and is escaped before it reaches the DOM; the UI builds nodes via
  `textContent`-style escaping, not raw `innerHTML`.
- Not included on purpose: paywall/article extraction, captcha solving, and
  anything that needs a real browser engine. If you need a browser, drive a
  headless one — this tool stays a plain HTTP client.

## Tests

45 offline tests, no network needed:

```bash
python -m unittest -v test_main
```
