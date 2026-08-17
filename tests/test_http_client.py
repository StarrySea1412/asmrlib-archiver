from __future__ import annotations

import unittest

import httpx

from asmrlib_archiver.config import CrawlerConfig
from asmrlib_archiver.guards import UrlGuard
from asmrlib_archiver.http_client import SafeHttpClient


class SafeHttpClientTests(unittest.TestCase):
    def _client(self, handler, *, max_bytes: int = 1024) -> SafeHttpClient:
        crawler = CrawlerConfig(
            retries=0,
            delay_seconds=0,
            max_response_bytes=max_bytes,
        )
        client = SafeHttpClient(UrlGuard(["asmrlib.com"]), crawler)
        client.client.close()
        client.client = httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )
        return client

    def test_reads_one_html_response_within_limit(self) -> None:
        client = self._client(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b"<main>ok</main>",
                request=request,
            )
        )
        try:
            result = client.fetch_page("https://asmrlib.com/tags/yoonying")
        finally:
            client.close()
        self.assertEqual(result.content, b"<main>ok</main>")

    def test_rejects_response_body_over_hard_limit(self) -> None:
        client = self._client(
            lambda request: httpx.Response(200, content=b"123456", request=request),
            max_bytes=5,
        )
        try:
            with self.assertRaisesRegex(RuntimeError, "page_response_too_large"):
                client.fetch_page("https://asmrlib.com/tags/yoonying")
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
