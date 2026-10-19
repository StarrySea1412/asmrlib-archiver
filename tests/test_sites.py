from __future__ import annotations

import re
import unittest
from urllib.parse import urlparse, urlunparse

from asmrlib_archiver import errors, guards
from asmrlib_archiver.errors import BlockedRedirect, BlockedUrl, GuardError
from asmrlib_archiver.guards import UrlGuard
from asmrlib_archiver.sites import AsmrlibSite, SiteRegistry, default_registry

POST_ID = "77badb9b69d7bfbcc2fc3369e2d3b5ae"


class SyntheticSite:
    """一个假想的第二个视频源：/v/<数字ID> 帖子、/c/<slug> 标签、?p=N 分页。"""

    name = "synthetic"
    domains = ("vids.example",)

    _POST_PATH_RE = re.compile(r"^/v/([0-9]+)/?$")
    _TAG_PATH_RE = re.compile(r"^/c/([^/]+)/?$")
    _PAGE_QUERY_RE = re.compile(r"^p=([1-9][0-9]*)$")

    def canonical_post_url(self, url: str) -> str | None:
        parsed = urlparse(url)
        if parsed.params or parsed.query:
            raise BlockedUrl(f"invalid_post_url_query: {url}")
        match = self._POST_PATH_RE.fullmatch(parsed.path)
        if not match:
            return None
        return urlunparse(
            parsed._replace(path=f"/v/{match.group(1)}", params="", query="", fragment="")
        )

    def canonical_tag_url(self, url: str) -> str | None:
        parsed = urlparse(url)
        if parsed.params or parsed.query:
            raise BlockedUrl(f"invalid_tag_url_query: {url}")
        match = self._TAG_PATH_RE.fullmatch(parsed.path)
        if not match:
            return None
        return urlunparse(
            parsed._replace(path=f"/c/{match.group(1)}", params="", query="", fragment="")
        )

    def canonical_tag_page_url(self, url: str, tag_url: str) -> str | None:
        parsed = urlparse(url)
        if not parsed.query:
            return tag_url
        match = self._PAGE_QUERY_RE.fullmatch(parsed.query)
        if not match:
            return None
        page_number = int(match.group(1))
        if page_number == 1:
            return tag_url
        return f"{tag_url}?p={page_number}"

    def build_post_url(self, post_id: str) -> str:
        return f"https://vids.example/v/{post_id}"

    def build_tag_url(self, slug: str) -> str:
        return f"https://vids.example/c/{slug}"


def registry_with_synthetic() -> SiteRegistry:
    registry = SiteRegistry()
    registry.register(SyntheticSite())
    return registry


class SiteRegistryTests(unittest.TestCase):
    def test_for_url_routes_by_registered_domain_only(self) -> None:
        registry = registry_with_synthetic()
        adapter = registry.for_url("https://vids.example/v/12345")
        self.assertIsInstance(adapter, SyntheticSite)
        self.assertIsNone(registry.for_url("https://asmrlib.com/posts/" + POST_ID))
        self.assertIsNone(registry.for_url("https://other.example/v/1"))
        self.assertEqual(len(registry.all()), 1)

    def test_default_registry_holds_only_the_builtin_adapter(self) -> None:
        registry = default_registry()
        self.assertEqual([adapter.name for adapter in registry.all()], ["asmrlib"])
        self.assertIsInstance(registry.for_url("https://asmrlib.com/tags/yoonying"), AsmrlibSite)


class AsmrlibAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.site = AsmrlibSite()

    def test_post_syntax_matches_legacy_behaviour(self) -> None:
        self.assertEqual(
            self.site.canonical_post_url(f"https://asmrlib.com/posts/{POST_ID.upper()}"),
            f"https://asmrlib.com/posts/{POST_ID}",
        )
        self.assertIsNone(self.site.canonical_post_url("https://asmrlib.com/posts/1234"))
        with self.assertRaises(BlockedUrl):
            self.site.canonical_post_url(f"https://asmrlib.com/posts/{POST_ID}?utm_source=ad")

    def test_tag_and_build_syntax_matches_legacy_behaviour(self) -> None:
        self.assertEqual(
            self.site.canonical_tag_url("https://asmrlib.com/tags/%E4%B8%AD%E6%96%87/"),
            "https://asmrlib.com/tags/%E4%B8%AD%E6%96%87",
        )
        self.assertEqual(
            self.site.build_tag_url("中文"),
            "https://asmrlib.com/tags/%E4%B8%AD%E6%96%87",
        )
        self.assertEqual(
            self.site.build_post_url(POST_ID.upper()),
            f"https://asmrlib.com/posts/{POST_ID}",
        )
        self.assertIsNone(self.site.canonical_tag_url("https://asmrlib.com/tags/a/b"))

    def test_tag_page_folds_first_page_and_caps_page_digits(self) -> None:
        tag = "https://asmrlib.com/tags/yoonying"
        self.assertEqual(self.site.canonical_tag_page_url(tag, tag), tag)
        self.assertEqual(self.site.canonical_tag_page_url(f"{tag}?page=1", tag), tag)
        self.assertEqual(self.site.canonical_tag_page_url(f"{tag}?page=2", tag), f"{tag}?page=2")
        self.assertIsNone(self.site.canonical_tag_page_url(f"{tag}?page=0", tag))
        self.assertIsNone(self.site.canonical_tag_page_url(f"{tag}?page={'9' * 19}", tag))


class RegisteredAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.site = SyntheticSite()
        registry = SiteRegistry()
        registry.register(self.site)
        self.guard = UrlGuard(["vids.example"], registry=registry)

    def test_registered_site_urls_are_canonicalized(self) -> None:
        self.assertTrue(self.guard.is_post_url("https://vids.example/v/12345"))
        self.assertEqual(
            self.guard.canonical_post_url("https://vids.example/v/12345/#player"),
            "https://vids.example/v/12345",
        )
        self.assertEqual(
            self.guard.canonical_post_url(self.site.build_post_url("777")),
            self.site.build_post_url("777"),
        )

        self.assertTrue(self.guard.is_tag_url("https://vids.example/c/nature"))
        self.assertEqual(
            self.guard.canonical_tag_url("https://vids.example/c/nature/"),
            self.site.build_tag_url("nature"),
        )
        self.assertEqual(
            self.guard.canonical_tag_url(self.site.build_tag_url("nature")),
            self.site.build_tag_url("nature"),
        )

    def test_registered_site_tag_pagination_uses_its_own_query_syntax(self) -> None:
        tag = "https://vids.example/c/nature"
        self.assertEqual(self.guard.canonical_tag_page_url("?p=1", tag), tag)
        self.assertEqual(self.guard.canonical_tag_page_url("?p=3", tag), f"{tag}?p=3")
        self.assertTrue(self.guard.is_tag_page_url(f"{tag}?p=3", tag))
        # asmrlib 的 ?page=N 不是这个站的语法
        self.assertFalse(self.guard.is_tag_page_url(f"{tag}?page=3", tag))
        with self.assertRaises(BlockedUrl) as ctx:
            self.guard.canonical_tag_page_url(f"{tag}?p=0", tag)
        self.assertIn("invalid_tag_page_query", str(ctx.exception))

    def test_unregistered_urls_are_rejected_under_the_default_registry(self) -> None:
        guard = UrlGuard(["vids.example"])
        self.assertFalse(guard.is_post_url("https://vids.example/v/12345"))
        self.assertFalse(guard.is_tag_url("https://vids.example/c/nature"))
        with self.assertRaises(BlockedUrl) as ctx:
            guard.canonical_post_url("https://vids.example/v/12345")
        self.assertIn("invalid_post_url_path", str(ctx.exception))
        with self.assertRaises(BlockedUrl) as ctx:
            guard.canonical_tag_url("https://vids.example/c/nature")
        self.assertIn("invalid_tag_url_path", str(ctx.exception))

    def test_builtin_site_is_rejected_by_a_registry_without_it(self) -> None:
        guard = UrlGuard(["asmrlib.com"], registry=registry_with_synthetic())
        self.assertFalse(guard.is_post_url(f"https://asmrlib.com/posts/{POST_ID}"))
        with self.assertRaises(BlockedUrl) as ctx:
            guard.canonical_post_url(f"https://asmrlib.com/posts/{POST_ID}")
        self.assertIn("invalid_post_url_path", str(ctx.exception))


class ExceptionSurfaceTests(unittest.TestCase):
    """guards 再导出的异常必须与 errors.py 里的是同一个对象。"""

    def test_guards_reexports_the_single_exception_classes(self) -> None:
        self.assertIs(guards.BlockedUrl, errors.BlockedUrl)
        self.assertIs(guards.BlockedRedirect, errors.BlockedRedirect)
        self.assertIs(guards.GuardError, errors.GuardError)
        self.assertIs(BlockedUrl, errors.BlockedUrl)
        self.assertIs(BlockedRedirect, errors.BlockedRedirect)
        self.assertIs(GuardError, errors.GuardError)
        self.assertTrue(issubclass(errors.BlockedUrl, errors.GuardError))
        self.assertTrue(issubclass(errors.BlockedRedirect, errors.GuardError))


class AdapterRaisedMessagePassesThroughTests(unittest.TestCase):
    """适配器抛 BlockedUrl 时，站点专属消息要原样透传到 UrlGuard 调用方。"""

    def setUp(self) -> None:
        registry = SiteRegistry()
        registry.register(SyntheticSite())
        self.guard = UrlGuard(["vids.example"], registry=registry)

    def test_site_specific_post_message_is_not_rewritten(self) -> None:
        url = "https://vids.example/v/12345?utm_source=ad"
        with self.assertRaises(BlockedUrl) as ctx:
            self.guard.canonical_post_url(url)
        self.assertEqual(str(ctx.exception), f"invalid_post_url_query: {url}")

    def test_site_specific_tag_message_is_not_rewritten(self) -> None:
        url = "https://vids.example/c/nature?utm_source=ad"
        with self.assertRaises(BlockedUrl) as ctx:
            self.guard.canonical_tag_url(url)
        self.assertEqual(str(ctx.exception), f"invalid_tag_url_query: {url}")

    def test_message_is_raised_as_the_shared_exception_type(self) -> None:
        with self.assertRaises(errors.BlockedUrl):
            self.guard.canonical_tag_url("https://vids.example/c/nature?utm_source=ad")


class AdapterCannotBypassDomainWhitelistTests(unittest.TestCase):
    def test_registered_adapter_does_not_grant_domain_access(self) -> None:
        guard = UrlGuard(["asmrlib.com"], registry=registry_with_synthetic())
        self.assertNotIn("vids.example", guard.allowed_domains)

        with self.assertRaises(BlockedUrl) as ctx:
            guard.assert_page_allowed("https://vids.example/v/12345")
        self.assertIn("blocked_cross_domain_page", str(ctx.exception))

        self.assertFalse(guard.page_decision("https://vids.example/v/12345").allowed)
        self.assertFalse(guard.is_post_url("https://vids.example/v/12345"))
        self.assertFalse(guard.is_tag_url("https://vids.example/c/nature"))
        with self.assertRaises(BlockedUrl) as ctx:
            guard.canonical_tag_page_url("?p=2", "https://vids.example/c/nature")
        self.assertIn("blocked_cross_domain_page", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
