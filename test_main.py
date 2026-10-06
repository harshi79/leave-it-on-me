#!/usr/bin/env python3
"""Offline tests for the resolver: no network, everything is faked.

    python -m unittest -v test_main
"""

import base64
import unittest
from urllib.parse import quote

import main as m


def pool(html_text: str, current: str = "https://short.example/abc123") -> m.Fetch:
    return m.Fetch(current, 200, {"Content-Type": "text/html; charset=utf-8"},
                   html_text.encode())


class NormalizeTests(unittest.TestCase):
    def test_adds_scheme(self):
        self.assertEqual(m.normalize_url("example.com/x"), "https://example.com/x")
        self.assertEqual(m.normalize_url("  bit.ly/abc  "), "https://bit.ly/abc")

    def test_rejects_dangerous_and_broken(self):
        for bad in ("javascript:alert(1)", "data:text/html,<h1>x", "ftp://x.com/a",
                    "file:///etc/passwd", "", "   ", "http://exa mple.com",
                    "https://example.com/\x01"):
            with self.subTest(bad=bad):
                with self.assertRaises(m.ResolveError):
                    m.normalize_url(bad)

    def test_length_limit(self):
        with self.assertRaises(m.ResolveError):
            m.normalize_url("https://example.com/" + "a" * m.MAX_URL_LEN)


class HelperTests(unittest.TestCase):
    def test_registrable_domain(self):
        self.assertEqual(m.registrable_domain("a.b.example.co.uk"), "example.co.uk")
        self.assertEqual(m.registrable_domain("www.example.com"), "example.com")
        self.assertEqual(m.registrable_domain("example.com"), "example.com")

    def test_shortener_and_gateway_detection(self):
        self.assertTrue(m.is_shortener("https://bit.ly/xyz"))
        self.assertTrue(m.is_shortener("https://www.tinyurl.com/xyz"))
        self.assertFalse(m.is_shortener("https://example.com/xyz"))
        self.assertTrue(m.is_gateway("https://linkvertise.com/123/x"))
        self.assertTrue(m.is_gateway("https://sub.gplinks.in/abc"))

    def test_strip_tracking(self):
        self.assertEqual(m.strip_tracking("https://x.com/p?utm_source=a&id=7&fbclid=z"),
                         "https://x.com/p?id=7")
        self.assertEqual(m.strip_tracking("https://x.com/p"), "https://x.com/p")

    def test_describe_status(self):
        self.assertEqual(m.describe_status(404), "Not Found")
        self.assertEqual(m.describe_status(599), "error")

    def test_inside_chrome(self):
        page = "<nav><a href='/x'>Docs</a></nav><main><a href='/y'>Go</a></main>"
        self.assertTrue(m.inside_chrome(page, page.index("Docs")))
        self.assertFalse(m.inside_chrome(page, page.index("Go")))


class DecodeCandidateTests(unittest.TestCase):
    here = "https://short.example/abc123"

    def test_absolute_and_schemeless(self):
        self.assertEqual(m.decode_candidate("https://x.com/p?q=1", self.here),
                         "https://x.com/p?q=1")
        self.assertEqual(m.decode_candidate("x.com/p", self.here), "https://x.com/p")

    def test_percent_encoded(self):
        self.assertEqual(
            m.decode_candidate(quote("https://ex.com/a b", safe=""), self.here),
            "https://ex.com/a%20b")

    def test_base64_payload(self):
        payload = base64.b64encode(b"https://example.com/b64").decode()
        self.assertEqual(m.decode_candidate(payload, self.here), "https://example.com/b64")

    def test_relative_paths_join_against_current(self):
        self.assertEqual(m.decode_candidate("/next/page", self.here),
                         "https://short.example/next/page")
        self.assertEqual(m.decode_candidate("//other.com/x", self.here), "https://other.com/x")

    def test_rejects_junk_and_self(self):
        for junk in ("", "5", "12345", "/", "https://short.example/abc123",
                     "https://short.example/", "javascript:alert(1)",
                     "not a url", "//"):
            with self.subTest(junk=junk):
                self.assertIsNone(m.decode_candidate(junk, self.here))


class ReaderTests(unittest.TestCase):
    """Each way a page can reveal its destination."""

    def test_meta_refresh(self):
        html = ('<head><meta http-equiv="refresh" content="3; url=https://real.example/done">'
                "</head>")
        found = m.from_meta_refresh(html, "https://short.example/a")
        self.assertEqual(found.url, "https://real.example/done")
        self.assertEqual(found.source, "meta refresh")

    def test_meta_refresh_reversed_attribute_order_and_entities(self):
        html = ("<meta content='0;URL=https://real.example/p?x=1&amp;y=2' "
                "http-equiv='REFRESH'>")
        self.assertEqual(m.from_meta_refresh(html, "https://short.example/a").url,
                         "https://real.example/p?x=1&y=2")

    def test_refresh_header(self):
        fetched = m.Fetch("https://s.example/a", 200, {"Refresh": "0; url=/final"})
        found, _ = m.extract(fetched, "https://s.example/a")
        self.assertEqual(found.url, "https://s.example/final")
        self.assertEqual(found.source, "Refresh header")

    def test_location_href_variants(self):
        for snippet in ('window.location.href = "https://real.example/1"',
                        "location.replace('https://real.example/1')",
                        'top.location = "https://real.example/1";',
                        'document.location.assign("https://real.example/1")'):
            with self.subTest(snippet=snippet):
                found = m.from_javascript(snippet, "https://short.example/a")
                self.assertEqual(found.url, "https://real.example/1")

    def test_data_attributes(self):
        html = '<div data-url="https://real.example/x" data-other="1">'
        self.assertEqual(m.from_data_attributes(html, "https://s.example/a").url,
                         "https://real.example/x")

    def test_embedded_json(self):
        html = '<script>var cfg = {"target": "https:\\/\\/real.example\\/json", "t": 3};</script>'
        self.assertEqual(m.from_json_blobs(html, "https://s.example/a").url,
                         "https://real.example/json")

    def test_url_parameter(self):
        current = "https://s.example/go?url=" + quote("https://real.example/p", safe="")
        found = m.from_url_parameters(current)
        self.assertEqual(found.url, "https://real.example/p")
        self.assertEqual(found.source, "?url= parameter")

    def test_binary_response_has_no_next_hop(self):
        fetched = m.Fetch("https://s.example/f.exe", 200,
                          {"Content-Type": "application/octet-stream"}, b"PK\x03\x04")
        self.assertEqual(m.extract(fetched, "https://s.example/f.exe"), (None, []))

    def test_explicit_signals_win_over_links(self):
        html = ('<meta http-equiv="refresh" content="0;url=https://real.example/meta">'
                "<a href='https://ads.example/click'>Continue</a>")
        found, extras = m.extract(pool(html), "https://short.example/abc123")
        self.assertEqual(found.url, "https://real.example/meta")
        self.assertEqual(extras, [])


class LinkPolicyTests(unittest.TestCase):
    here = "https://short.example/abc123"

    def test_continue_link_is_followed(self):
        html = '<a href="https://real.example/dl">Continue to the download</a>'
        found, _ = m.extract(pool(html, self.here), self.here)
        self.assertEqual(found.url, "https://real.example/dl")
        self.assertGreaterEqual(found.score, 8)

    def test_neutral_link_is_only_a_candidate(self):
        html = '<a href="https://other.example/about">About us</a>'
        found, candidates = m.extract(pool(html, self.here), self.here)
        self.assertIsNone(found)
        self.assertEqual([c.url for c in candidates], ["https://other.example/about"])

    def test_asset_and_noise_links_are_ignored(self):
        html = ('<a href="https://cdn.example/x.js">Continue</a>'
                '<a href="https://fonts.gstatic.com/f.woff2">Continue</a>'
                '<a href="https://www.google.com/analytics">Continue</a>')
        found, candidates = m.extract(pool(html, self.here), self.here)
        self.assertIsNone(found)
        self.assertEqual(candidates, [])

    def test_same_site_navigation_is_ignored_but_same_site_continue_is_not(self):
        nav = '<nav><a href="/pricing">Continue</a></nav>'
        self.assertEqual(m.external_candidates(nav, self.here), [])
        body = '<div><a href="/next-step">Continue</a></div>'
        self.assertEqual(m.external_candidates(body, self.here)[0].url,
                         "https://short.example/next-step")

    def test_mailto_and_javascript_anchors_ignored(self):
        html = ('<a href="mailto:x@y.com">Continue</a>'
                '<a href="javascript:void(0)">Continue</a>')
        self.assertEqual(m.external_candidates(html, self.here), [])


class ResolveFlowTests(unittest.TestCase):
    """Drive resolve() with a fake network so the chain logic is testable."""

    def setUp(self):
        self.real_fetch = m.fetch
        self.pages = {}
        self.calls = []

        def fake_fetch(url, timeout, insecure=False):
            self.calls.append(url)
            if url not in self.pages:
                return m.Fetch(url, 0, {}, b"", error="no such page in the fake net")
            status, headers, body = self.pages[url]
            return m.Fetch(url, status, headers, body.encode() if isinstance(body, str) else body)

        m.fetch = fake_fetch
        self.addCleanup(lambda: setattr(m, "fetch", self.real_fetch))

    def page(self, url, body, status=200, headers=None):
        self.pages[url] = (status, headers or {"Content-Type": "text/html"}, body)

    def redirect(self, url, location, status=302):
        self.pages[url] = (status, {"Location": location}, "")

    def test_plain_chain(self):
        self.redirect("https://bit.ly/aaa", "https://mid.example/1", 301)
        self.redirect("https://mid.example/1", "/final")
        self.page("https://mid.example/final", "<title>The End</title>ok")
        result = m.resolve("https://bit.ly/aaa")
        self.assertEqual(result.verdict, "resolved")
        self.assertEqual(result.final_url, "https://mid.example/final")
        self.assertEqual(result.title, "The End")
        self.assertEqual(result.via, ["301 redirect", "302 redirect"])
        self.assertEqual(len(result.hops), 3)

    def test_tracking_stripped_in_clean_url(self):
        self.redirect("https://bit.ly/aaa", "https://real.example/p?utm_source=x&id=2")
        self.page("https://real.example/p?utm_source=x&id=2", "hello")
        result = m.resolve("https://bit.ly/aaa")
        self.assertEqual(result.final_url, "https://real.example/p?utm_source=x&id=2")
        self.assertEqual(result.clean_url, "https://real.example/p?id=2")

    def test_already_main_link(self):
        self.page("https://example.com/plain", "nothing here")
        result = m.resolve("https://example.com/plain")
        self.assertEqual(result.verdict, "already_main")
        self.assertEqual(result.via, [])

    def test_redirect_loop_is_reported(self):
        self.redirect("https://s.example/a", "https://s.example/b")
        self.redirect("https://s.example/b", "https://s.example/a")
        result = m.resolve("https://s.example/a")
        self.assertEqual(result.verdict, "loop")
        self.assertIsNone(result.final_url)
        self.assertEqual(len(self.calls), 2)

    def test_gateway_is_flagged_not_faked(self):
        self.redirect("https://bit.ly/aaa", "https://linkvertise.com/1/x")
        self.page("https://linkvertise.com/1/x", "<script>var x=1</script>verify")
        result = m.resolve("https://bit.ly/aaa")
        self.assertEqual(result.verdict, "gateway")
        self.assertIn("monetised gate", result.note)

    def test_error_page_stops_the_chain(self):
        self.page("https://s.example/gone", "nope", status=404)
        result = m.resolve("https://s.example/gone")
        self.assertEqual(result.verdict, "error")
        self.assertIn("404", result.note)

    def test_hop_ceiling(self):
        self.redirect("https://s.example/a", "https://s.example/b")
        self.redirect("https://s.example/b", "https://s.example/c")
        self.page("https://s.example/c", "end")
        result = m.resolve("https://s.example/a", max_hops=2)
        self.assertEqual(result.verdict, "error")
        self.assertIn("ceiling", result.note)

    def test_private_targets_refused_by_default(self):
        result = m.resolve("http://127.0.0.1:8080/admin")
        self.assertEqual(result.verdict, "error")
        self.assertIn("local address", result.note)
        self.assertEqual(self.calls, [])

    def test_private_targets_allowed_when_asked(self):
        self.page("http://127.0.0.1:8080/admin", "ok")
        result = m.resolve("http://127.0.0.1:8080/admin", allow_private=True)
        self.assertEqual(result.verdict, "already_main")

    def test_self_routes_fetch_over_loopback(self):
        public = "https://8000-abc.e2b.app/demo/chain"
        self.page("http://127.0.0.1:8000/demo/chain", "<title>local</title>ok")
        result = m.resolve(public, allow_hosts={"8000-abc.e2b.app"},
                           self_routes={"8000-abc.e2b.app": "127.0.0.1:8000"})
        self.assertEqual(self.calls, ["http://127.0.0.1:8000/demo/chain"])
        self.assertEqual(result.final_url, public)      # shown as the public URL

    def test_guess_can_be_disabled(self):
        page = '<a href="https://real.example/dl">Continue to the download</a>'
        self.page("https://s.example/a", page)
        guessed = m.resolve("https://s.example/a")
        self.assertEqual(guessed.final_url, "https://real.example/dl")
        cautious = m.resolve("https://s.example/a", guess=False)
        self.assertEqual(cautious.verdict, "already_main")
        self.assertEqual(cautious.candidates[0]["url"], "https://real.example/dl")

    def test_meta_refresh_is_followed_even_when_guessing_is_off(self):
        self.page("https://s.example/a",
                  '<meta http-equiv="refresh" content="0;url=https://real.example/x">')
        result = m.resolve("https://s.example/a", guess=False)
        self.assertEqual(result.final_url, "https://real.example/x")

    def test_flags_for_risky_destination(self):
        self.redirect("https://bit.ly/aaa", "http://192.0.2.9/install/setup.exe")
        self.page("http://192.0.2.9/install/setup.exe", "PK", headers={
            "Content-Type": "application/octet-stream"})
        result = m.resolve("https://bit.ly/aaa", allow_private=True)
        joined = " | ".join(result.flags)
        self.assertIn("HTTP, not HTTPS", joined)
        self.assertIn("bare IP address", joined)
        self.assertIn("downloadable file (exe)", joined)

    def test_unusable_input_returns_error_without_fetching(self):
        result = m.resolve("javascript:alert(1)")
        self.assertEqual(result.verdict, "error")
        self.assertEqual(self.calls, [])


class RateLimiterTests(unittest.TestCase):
    def test_window_and_disable(self):
        limiter = m.RateLimiter(limit=2, window=60)
        self.assertTrue(limiter.allow("a"))
        self.assertTrue(limiter.allow("a"))
        self.assertFalse(limiter.allow("a"))
        self.assertTrue(limiter.allow("b"))
        self.assertTrue(m.RateLimiter(0).allow("a"))


class RenderTests(unittest.TestCase):
    def test_result_renders_without_ansi_when_disabled(self):
        paint = m.Paint(enabled=False)
        result = m.Result(input_url="https://bit.ly/x", verdict="already_main",
                          final_url="https://example.com/x", note="nothing to do")
        text = m.render_result(result, paint)
        self.assertIn("https://example.com/x", text)
        self.assertNotIn("\033[", text)

    def test_ui_escapes_page_content(self):
        """The dashboard renders JSON in the DOM via JS escapeHtml()."""
        self.assertIn(".replace(/&/g, \"&amp;\")", m.UI_SCRIPT)

    def test_page_has_no_unfilled_placeholders(self):
        page = m.render_page(m.DEMO_CHIPS_SCRIPT).decode()
        self.assertNotIn("$demo_chips", page)
        self.assertNotIn("$css", page)
        self.assertIn("/api/resolve", page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
