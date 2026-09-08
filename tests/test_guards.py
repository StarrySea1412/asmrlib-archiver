from __future__ import annotations

import unittest

from asmrlib_archiver.guards import BlockedRedirect, BlockedUrl, UrlGuard

POST_ID = "77badb9b69d7bfbcc2fc3369e2d3b5ae"


class UrlGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = UrlGuard(
            allowed_domains=["asmrlib.com"],
            allowed_media_domains=["media.asmrlib-cdn.example"],
            allow_external_media=True,
            ad_keywords=["affiliate", "tracking", "doubleclick", "customads.example"],
        )

    def test_normalize_canonicalizes_host_default_port_and_fragment(self) -> None:
        self.assertEqual(
            self.guard.normalize(f"HTTPS://ASMRLIB.COM.:443/posts/{POST_ID}#player"),
            f"https://asmrlib.com/posts/{POST_ID}",
        )
        self.assertEqual(
            self.guard.normalize("HTTP://ASMRLIB.COM:80/tags/yoonying"),
            "http://asmrlib.com/tags/yoonying",
        )
        self.assertEqual(
            self.guard.normalize("https://asmrlib.com:8443/tags/yoonying"),
            "https://asmrlib.com:8443/tags/yoonying",
        )

    def test_normalize_rejects_credentials_controls_and_non_http_urls(self) -> None:
        invalid = [
            "https://user:secret@asmrlib.com/tags/yoonying",
            "https://asmrlib.com/tag s/yoonying",
            "javascript:alert(1)",
            "data:text/plain,hello",
            "/relative/without/base",
        ]
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(BlockedUrl):
                self.guard.normalize(url)

    def test_ad_domains_match_exact_host_or_subdomain_boundary(self) -> None:
        blocked = [
            "https://downrightfootball.com/x.js",
            "https://cdn.downrightfootball.com/x.js",
            "https://wpadmngr.com/static/adManager.js",
            "https://js.wpadmngr.com/static/adManager.js",
            "https://histats.com/pixel",
            "https://s10.histats.com/js15_as.js",
            "https://sead.pages.dev/xingchen",
            "https://track.sead.pages.dev/pixel",
            "https://bit.ly/fulise",
            "https://ad.doubleclick.net/banner",
            "https://customads.example/banner",
            "https://cdn.customads.example/banner",
        ]
        for url in blocked:
            with self.subTest(url=url):
                self.assertTrue(self.guard.is_ad_url(url))

    def test_ad_domains_do_not_match_paths_queries_or_host_substrings(self) -> None:
        allowed = [
            "https://asmrlib.com/tags/affiliate",
            "https://asmrlib.com/posts/tracking",
            "https://asmrlib.com/search?q=wpadmngr.com",
            "https://notwpadmngr.com/banner",
            "https://wpadmngr.com.example.org/banner",
            "https://downrightfootball.com.example.org/x.js",
            "https://pages.dev/sead.pages.dev/xingchen",
            "https://example.com/path/bit.ly/fulise",
        ]
        for url in allowed:
            with self.subTest(url=url):
                self.assertFalse(self.guard.is_ad_url(url))

        decision = self.guard.page_decision("https://asmrlib.com/tags/affiliate-tracking")
        self.assertTrue(decision.allowed)

    def test_page_and_media_decisions_still_apply_ad_and_domain_guards(self) -> None:
        self.assertTrue(self.guard.page_decision("https://asmrlib.com/tags/yoonying").allowed)
        self.assertEqual(
            self.guard.page_decision("https://bit.ly/fulise").reason,
            "blocked_ad_url",
        )
        self.assertEqual(
            self.guard.page_decision("https://example.com/page").reason,
            "blocked_cross_domain_page",
        )
        self.assertTrue(
            self.guard.media_decision("https://media.asmrlib-cdn.example/video.mp4").allowed
        )
        self.assertEqual(
            self.guard.media_decision("https://cdn.wpadmngr.com/video.mp4").reason,
            "blocked_ad_url",
        )

    def test_canonical_tag_url_accepts_one_safe_segment_only(self) -> None:
        self.assertEqual(
            self.guard.canonical_tag_url("HTTPS://ASMRLIB.COM:443/tags/%79oonying/#top"),
            "https://asmrlib.com/tags/yoonying",
        )
        self.assertEqual(
            self.guard.canonical_tag_url("https://asmrlib.com/tags/%E4%B8%AD%E6%96%87/"),
            "https://asmrlib.com/tags/%E4%B8%AD%E6%96%87",
        )
        self.assertTrue(self.guard.is_tag_url("https://asmrlib.com/tags/ASMR"))

        invalid = [
            "https://asmrlib.com/tags/",
            "https://asmrlib.com/tags/yoonying/extra",
            "https://asmrlib.com/tags/yoonying?page=2",
            "https://asmrlib.com/tags/yoonying?utm_source=ad",
            "https://asmrlib.com/tags/%2Fadmin",
            "https://asmrlib.com/tags/%ZZ",
            "https://example.com/tags/yoonying",
        ]
        for url in invalid:
            with self.subTest(url=url):
                self.assertFalse(self.guard.is_tag_url(url))

    def test_canonical_post_url_requires_exact_32_hex_id(self) -> None:
        upper_id = POST_ID.upper()
        self.assertEqual(
            self.guard.canonical_post_url(f"/posts/{upper_id}/#player", "https://asmrlib.com/"),
            f"https://asmrlib.com/posts/{POST_ID}",
        )
        self.assertTrue(self.guard.is_post_url(f"https://asmrlib.com/posts/{POST_ID}"))

        invalid = [
            f"https://asmrlib.com/posts/{POST_ID}?utm_source=ad",
            f"https://asmrlib.com/posts/{POST_ID}/download",
            "https://asmrlib.com/posts/1234",
            "https://asmrlib.com/posts/zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz",
            f"https://example.com/posts/{POST_ID}",
        ]
        for url in invalid:
            with self.subTest(url=url):
                self.assertFalse(self.guard.is_post_url(url))

    def test_tag_pagination_canonicalizes_only_a_positive_page_query(self) -> None:
        tag_url = "https://asmrlib.com/tags/yoonying"
        self.assertEqual(self.guard.canonical_tag_page_url(tag_url, tag_url), tag_url)
        self.assertEqual(self.guard.canonical_tag_page_url("?page=1", tag_url), tag_url)
        self.assertEqual(
            self.guard.canonical_tag_page_url("?page=2#cards", tag_url),
            f"{tag_url}?page=2",
        )
        self.assertTrue(self.guard.is_tag_page_url(f"{tag_url}?page=42", tag_url))

    def test_tag_pagination_rejects_other_queries_origins_and_tag_slugs(self) -> None:
        tag_url = "https://asmrlib.com/tags/yoonying"
        invalid = [
            f"{tag_url}?page=0",
            f"{tag_url}?page=-1",
            f"{tag_url}?page=+1",
            f"{tag_url}?page=01",
            f"{tag_url}?page=%32",
            f"{tag_url}?page=2&sort=new",
            f"{tag_url}?page=2&page=3",
            f"{tag_url}?utm_source=ad&page=2",
            "https://asmrlib.com/tags/ASMR?page=2",
            "https://sub.asmrlib.com/tags/yoonying?page=2",
            "http://asmrlib.com/tags/yoonying?page=2",
            "https://example.com/tags/yoonying?page=2",
        ]
        for url in invalid:
            with self.subTest(url=url):
                self.assertFalse(self.guard.is_tag_page_url(url, tag_url))

    def test_redirect_api_remains_compatible(self) -> None:
        self.assertEqual(
            self.guard.assert_redirect_allowed(
                "https://asmrlib.com/start", f"/posts/{POST_ID}"
            ),
            f"https://asmrlib.com/posts/{POST_ID}",
        )
        with self.assertRaises(BlockedRedirect):
            self.guard.assert_redirect_allowed(
                "https://asmrlib.com/start", "https://example.com/end"
            )
        with self.assertRaises(BlockedRedirect):
            self.guard.assert_redirect_allowed(
                "https://asmrlib.com/start", "/end", policy="none"
            )
        for target in [
            "http://asmrlib.com/end",
            "https://asmrlib.com:8443/end",
        ]:
            with self.subTest(target=target), self.assertRaises(BlockedRedirect):
                self.guard.assert_redirect_allowed(
                    "https://asmrlib.com/start",
                    target,
                )


if __name__ == "__main__":
    unittest.main()
