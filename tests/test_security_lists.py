from __future__ import annotations

import unittest

from asmrlib_archiver import guards, online_shield, security_lists
from asmrlib_archiver.viewer import player_guard


class SecurityListSingleSourceTests(unittest.TestCase):
    """The three URL-policy modules must share one table, not copies.

    guards.py, online_shield.py and viewer/player_guard.py each used to carry
    their own domain tables, and they had drifted (different ad-host counts,
    entries present in one file but missing from another). These assertions
    fail if anyone reintroduces a local copy.
    """

    def test_modules_reference_the_same_blocked_host_table(self) -> None:
        self.assertIs(guards.DEFAULT_AD_DOMAINS, security_lists.BLOCKED_HOSTS)
        self.assertIs(online_shield.DEFAULT_BLOCKED_HOSTS, security_lists.BLOCKED_HOSTS)
        self.assertIs(player_guard.DEFAULT_BLOCKED_HOSTS, security_lists.BLOCKED_HOSTS)

    def test_modules_reference_the_same_player_domain_table(self) -> None:
        self.assertIs(online_shield.DEFAULT_PLAYER_DOMAINS, security_lists.PLAYER_DOMAINS)
        self.assertIs(player_guard.DEFAULT_PLAYER_DOMAINS, security_lists.PLAYER_DOMAINS)

    def test_remaining_shared_tables_come_from_one_source(self) -> None:
        self.assertIs(online_shield.DEFAULT_ASSET_DOMAINS, security_lists.ASSET_DOMAINS)
        self.assertIs(
            online_shield.DEFAULT_BLOCK_URL_KEYWORDS,
            security_lists.BLOCK_URL_KEYWORDS,
        )
        self.assertIs(guards.AD_DOMAIN_ALIASES, security_lists.AD_DOMAIN_ALIASES)

    def test_consolidation_widened_the_weakest_blocklist(self) -> None:
        """player_guard previously missed hosts the other two blocked.

        Link shorteners are the important ones: they can redirect the system
        browser anywhere, so the hand-off validator must reject them too.
        """
        policy = player_guard.ExternalUrlPolicy()
        for host in ("bit.ly", "goo.gl", "t.co", "tinyurl.com", "amazon-adsystem.com"):
            with self.subTest(host=host), self.assertRaises(player_guard.PlayerUrlError):
                policy.validate(f"https://{host}/whatever")

    def test_python_and_injected_js_agree_on_ad_urls(self) -> None:
        """The bootstrap JS embeds the same policy the Python side enforces.

        The script builds its own matcher in JS, so at minimum the tables it
        receives must be the shared ones -- otherwise the in-page filter and
        the native request filter would disagree.
        """
        policy = online_shield.ShieldPolicy()
        script = online_shield.build_page_bootstrap(policy)
        for host in security_lists.BLOCKED_HOSTS:
            with self.subTest(host=host):
                self.assertIn(host, script)
                self.assertTrue(policy.is_ad_url(f"https://{host}/ad.js"))


if __name__ == "__main__":
    unittest.main()
