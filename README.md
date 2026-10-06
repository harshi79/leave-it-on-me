# leave-it-on-me

A short-link service that fits in a single `main.py`. No dependencies, no
framework, no build step — just the Python standard library and SQLite.

```
https://your-host/repo   →   https://github.com/you/a/really/long/path
```

## Run it

```bash
python main.py                              # http://0.0.0.0:8000, db=./links.db
python main.py --port 9000 --db /data/links.db
PORT=9000 python main.py                    # env vars: HOST, PORT, DB_PATH
python main.py --rate-limit 0               # disable the create-rate cap
```

Then open <http://localhost:8000> for the dashboard: paste a URL, optionally
pick a slug and an expiry, and you get a copy-ready short link plus a table of
everything you've shortened with click counts.

Requires Python 3.9+. The database is created on first run and is the only state
the app has.

## HTTP API

| Method | Path | What it does |
| --- | --- | --- |
| `POST` | `/api/links` | create a link → `201` + the link as JSON |
| `GET` | `/api/links` | list recent links |
| `GET` | `/api/links/<code>` | one link (any casing: `/api/links/Repo` finds `repo`) |
| `DELETE` | `/api/links/<code>` | delete a link |
| `GET` | `/<code>` | `302` redirect to the target; `404` unknown, `410` expired |
| `GET` | `/healthz` | liveness + link count |

```bash
curl -X POST localhost:8000/api/links \
     -H 'content-type: application/json' \
     -d '{"url": "example.com/some/long/path", "slug": "docs", "expires_in": 86400}'
```

```json
{
  "code": "docs",
  "short_url": "http://localhost:8000/docs",
  "target": "https://example.com/some/long/path",
  "clicks": 0,
  "created_at": "2026-10-06T18:13:33Z",
  "expires_at": "2026-10-07T18:13:33Z",
  "expired": false
}
```

`expires_in` is a number of seconds from now (omit it, or pass `0`, for a link
that never expires). Both fields are optional; `slug` defaults to a random
6-character code from an alphabet with no look-alike characters
(`23456789abcdefghjkmnpqrstuvwxyz`).

Errors come back as `{"error": "..."}` with a useful status: `400` bad input,
`409` slug taken, `429` rate limited. Unknown slugs render a themed 404 page
instead of redirecting anywhere.

## Notes on behaviour

- **Input is normalised and checked.** `example.com/x` becomes
  `https://example.com/x`; only `http`/`https` targets are accepted, so
  `javascript:`, `data:` and header-injection payloads are rejected. Everything
  rendered into HTML is escaped, and non-ASCII targets are percent-encoded in
  the `Location` header.
- **Clicks** are counted on real `GET` requests; `HEAD` probes and browsers
  prefetching don't inflate the number.
- **Expired links** stay listed on the dashboard with an `expired` badge and
  return `410 Gone`.
- **Slugs** are unique case-insensitively and may not collide with app routes
  (`/api`, `/healthz`, …).
- **Rate limit** defaults to 60 new links per 10 minutes per IP; it's in memory,
  so it resets on restart and is per-process.
- **Reverse proxies**: the app honours `X-Forwarded-Host` and
  `X-Forwarded-Proto` when building the short URL it shows you, so behind
  nginx/Caddy the links you copy are the public ones. Only trust those headers
  if something in front of the app sets them.
- **Scaling**: SQLite in WAL mode with one connection per request and a write
  lock, which happily handles a small personal instance. For real traffic, put
  it behind a proxy and swap `Store` for Postgres — the HTTP layer doesn't care.
