"""The server-preference order must live in exactly one place.

``player_resolver._rank`` (Python, over a fetched + parsed post page) and the
``rank`` closure inside online_shield's injected bootstrap JS (over the live
DOM) used to carry separate copies of the site's server order.  Adding a
player meant editing both, and editing only one silently made the native
resolver and the in-page auto-picker choose different servers.

Both now read ``security_lists.SOURCE_RANKING``.  These tests fail if anyone
re-inlines the order into either consumer.
"""

from __future__ import annotations

import inspect
import json
import re
import unittest

from asmrlib_archiver import player_resolver as pr
from asmrlib_archiver import security_lists
from asmrlib_archiver.online_shield import ShieldPolicy, build_page_bootstrap

_POLICY_RE = re.compile(r"const policy = (\{.*?\});", re.DOTALL)


def _payload_from_script(script: str) -> dict:
    """Pull the policy object back out of the generated bootstrap JS."""

    match = _POLICY_RE.search(script)
    if match is None:
        raise AssertionError("generated bootstrap carries no policy object")
    return json.loads(match.group(1))


def _js_rank_mirror(payload: dict, server: str | None, url: str, text: str) -> int:
    """Python transcription of the injected JS ``rankOf`` closure.

    It deliberately reads the *same payload the JS receives* rather than the
    Python table, so a test that passes here cannot be passing because the
    mirror quietly used a different (still-correct) source.
    """

    server = (server or "").lower()
    url = (url or "").lower()
    text = (text or "").strip().upper()
    for tier in payload.get("source_ranking") or []:
        if (
            server in (tier.get("servers") or [])
            or text == tier.get("label")
            or any(marker in url for marker in (tier.get("markers") or []))
        ):
            return tier["rank"]
    return payload["source_ranking_default"]


# (button label / textContent, data-url, data-server, expected rank)
_SHARED_CASES: tuple[tuple[str, str, str | None, int], ...] = (
    ("BI", "https://bysetayico.com/e/1", "byse", 0),
    ("UP", "https://upn.one/e/1", "upnshare", 1),
    ("AB", "https://abyssplayer.com/e/1", "abyss", 2),
    ("?", "https://other.example/e/1", None, 9),
    # Normalization: the JS uppercases trimmed text, Python uppercases the
    # stripped label -- both must land on the same tier.
    ("bi", "https://other.example/e/1", None, 0),
    ("  up ", "https://other.example/e/1", None, 1),
    # URL-only matches (no usable label, no data-server).
    ("", "https://v.upn.one/#iupmtw", None, 1),
    ("?", "https://cdn.bysetayico.com/e/1", None, 0),
    # The earliest tier wins when signals disagree.
    ("AB", "https://bysetayico.com/e/1", "abyss", 0),
    # Unknown server and unknown URL stay on the default rank.
    ("?", "https://example.test/e/1", "zz-unknown", 9),
)


class SourceRankingTableTests(unittest.TestCase):
    def test_table_shape_is_usable_by_both_consumers(self) -> None:
        ranks = [tier.rank for tier in security_lists.SOURCE_RANKING]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(len(set(ranks)), len(ranks))
        for tier in security_lists.SOURCE_RANKING:
            self.assertTrue(tier.label, "every tier needs a short label")
            self.assertTrue(tier.url_markers, "every tier needs a URL marker")
            self.assertTrue(tier.server_ids, "every tier needs a data-server id")

    def test_markers_are_lowercase_and_labels_uppercase(self) -> None:
        """Both consumers normalize before comparing, so the table must too."""

        for tier in security_lists.SOURCE_RANKING:
            self.assertEqual(tier.label, tier.label.upper())
            for marker in tier.url_markers:
                self.assertEqual(marker, marker.lower())
            for server in tier.server_ids:
                self.assertEqual(server, server.lower())

    def test_rank_signature_is_unchanged(self) -> None:
        """``_rank`` is passed to ``rank_sources`` as ``static_rank``."""

        self.assertEqual(list(inspect.signature(pr._rank).parameters), ["label", "url"])


class PythonRankReadsTheTableTests(unittest.TestCase):
    def test_rank_reads_the_table(self) -> None:
        fake = (
            security_lists.SourceRanking(
                rank=0, label="ZZ", url_markers=("zz-marker.invalid",), server_ids=("zzsrv",)
            ),
        )
        original = pr.SOURCE_RANKING
        pr.SOURCE_RANKING = fake
        try:
            self.assertEqual(pr._rank("ZZ", "https://example.test/e/1"), 0)
            self.assertEqual(pr._rank("zz", "https://example.test/e/1"), 0)
            self.assertEqual(pr._rank("?", "https://zz-marker.invalid/e/1"), 0)
            # The old inline literals are gone: with the table swapped out,
            # bysetayico/upn.one/abyss no longer match anything.
            self.assertEqual(pr._rank("BI", "https://bysetayico.com/e/1"), 9)
            self.assertEqual(pr._rank("UP", "https://upn.one/e/1"), 9)
            self.assertEqual(pr._rank("AB", "https://abyssplayer.com/e/1"), 9)
        finally:
            pr.SOURCE_RANKING = original

    def test_rank_defaults_to_the_shared_default(self) -> None:
        self.assertEqual(
            pr._rank("?", "https://unknown.example/e/1"),
            security_lists.SOURCE_RANKING_DEFAULT,
        )


class InjectedJsReadsTheTableTests(unittest.TestCase):
    def test_injected_js_carries_the_table_verbatim(self) -> None:
        payload = _payload_from_script(build_page_bootstrap(ShieldPolicy()))
        self.assertEqual(
            payload["source_ranking"],
            [tier.as_payload() for tier in security_lists.SOURCE_RANKING],
        )
        self.assertEqual(
            payload["source_ranking_default"], security_lists.SOURCE_RANKING_DEFAULT
        )

    def test_generated_js_tracks_the_table_instead_of_a_copy(self) -> None:
        fake = (
            security_lists.SourceRanking(
                rank=0, label="ZZ", url_markers=("zz-marker.invalid",), server_ids=("zzsrv",)
            ),
        )
        original = security_lists.SOURCE_RANKING
        security_lists.SOURCE_RANKING = fake
        try:
            script = build_page_bootstrap(ShieldPolicy())
        finally:
            security_lists.SOURCE_RANKING = original
        self.assertIn("zz-marker.invalid", script)
        self.assertIn("zzsrv", script)
        self.assertIn('"label":"ZZ"', script)

    def test_no_inlined_copy_of_the_order_survives_in_the_js(self) -> None:
        """The exact expressions the old duplicated closure used are gone."""

        script = build_page_bootstrap(ShieldPolicy())
        for inlined in (
            "indexOf('bysetayico')",
            "indexOf('upn.one')",
            "indexOf('abyss')",
            "=== 'byse'",
            "=== 'upnshare'",
            "=== 'abyss'",
        ):
            with self.subTest(inlined=inlined):
                self.assertNotIn(inlined, script)


class PythonAndJsAgreeTests(unittest.TestCase):
    def test_shared_cases_rank_identically(self) -> None:
        payload = _payload_from_script(build_page_bootstrap(ShieldPolicy()))
        for label, url, server, expected in _SHARED_CASES:
            with self.subTest(label=label, url=url, server=server):
                self.assertEqual(pr._rank(label, url), expected)
                self.assertEqual(_js_rank_mirror(payload, server, url, label), expected)

    def test_data_server_is_a_js_only_signal(self) -> None:
        """Deliberate asymmetry, kept from the pre-refactor behaviour.

        The JS sees the button's ``data-server`` attribute; a fetched and
        parsed page does not, so ``player_resolver`` must keep falling back
        to the label/URL and the shared default.
        """

        payload = _payload_from_script(build_page_bootstrap(ShieldPolicy()))
        self.assertEqual(
            _js_rank_mirror(payload, "upnshare", "https://cdn.example/e/1", "播放"), 1
        )
        self.assertEqual(pr._rank("播放", "https://cdn.example/e/1"), 9)

    def test_mirror_falls_back_like_the_js(self) -> None:
        payload = _payload_from_script(build_page_bootstrap(ShieldPolicy()))
        self.assertEqual(
            _js_rank_mirror(payload, None, "https://unknown.example/e/1", "?"),
            security_lists.SOURCE_RANKING_DEFAULT,
        )


if __name__ == "__main__":
    unittest.main()
