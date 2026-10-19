from __future__ import annotations

import contextlib
import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from asmrlib_archiver.config import AppConfig
from asmrlib_archiver.db import ArchiveDb
from asmrlib_archiver.player_resolver import _rank
from asmrlib_archiver.source_health import (
    PlaybackOutcome,
    PlaybackSession,
    health_key,
    rank_sources,
)
from asmrlib_archiver.viewer import ArchiveViewer

_BI = "https://bysetayico.com/e/abc"
_UP = "https://upn.one/embed/9x8y"
_AB = "https://abyssplayer.com/e/xyz"


class _HealthCase(unittest.TestCase):
    """共享一个临时归档库，走 ArchiveDb 的公开 API（与 viewer 用法一致）。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "archive.sqlite3"
        self.db = ArchiveDb(self.path)
        self.db.init()

    def tearDown(self) -> None:
        with contextlib.suppress(Exception):
            self.db.close()
        self.temp_dir.cleanup()

    def rank(self, candidates, *, preferred_url=None):
        records = self.db.get_source_health([url for _, url in candidates])
        return rank_sources(
            candidates,
            health=records,
            preferred_url=preferred_url,
            static_rank=_rank,
        )

    def play(self, url: str, latency_ms: int = 800) -> None:
        self.db.record_playback_outcome(
            url, PlaybackOutcome.PLAYING, latency_ms, ""
        )

    def fail(self, url: str, error: str = "连接超时") -> None:
        self.db.record_playback_outcome(url, PlaybackOutcome.FAILED, -1, error)

    def record(self, url: str):
        return self.db.get_source_health([url])[health_key(url)]


class SourceHealthOrderingTests(_HealthCase):
    """健康排序：混合历史、无历史中性、失败冷却。"""

    def test_mixed_history_orders_healthy_then_unknown_then_cooling(self) -> None:
        # Candidates are handed in the *static* server order (UP, AB, BI) so a
        # passing assertion can only come from the measured health.
        candidates = [("UP", _UP), ("AB", _AB), ("BI", _BI)]
        self.play(_BI, 600)
        self.play(_BI, 700)
        self.fail(_UP, "HTTP 403")
        self.fail(_UP, "HTTP 403")

        ranked = self.rank(candidates)

        self.assertEqual([r.url for r in ranked], [_BI, _AB, _UP])
        self.assertGreater(ranked[0].score, ranked[1].score)
        self.assertFalse(ranked[0].in_cooldown)
        self.assertFalse(ranked[1].in_cooldown)
        self.assertTrue(ranked[2].in_cooldown)

    def test_source_with_no_history_is_neutral_not_buried(self) -> None:
        # `_BI` has failed once; `_UP` has never been tried. Static order would
        # open `_BI` first — the whole point is that an untried source is worth
        # trying, so it must come first instead.
        self.fail(_BI, "连接超时")

        ranked = self.rank([("BI", _BI), ("UP", _UP)])

        self.assertEqual(ranked[0].url, _UP)
        self.assertEqual(ranked[0].score, 0.0)
        self.assertFalse(ranked[0].in_cooldown)
        self.assertEqual(ranked[0].reason, "尚无记录")
        self.assertEqual(ranked[1].url, _BI)

    def test_proven_source_still_outranks_an_unknown_one(self) -> None:
        # Neutral must not mean "promoted above known-good".
        self.play(_BI, 900)

        ranked = self.rank([("UP", _UP), ("BI", _BI)])

        self.assertEqual(ranked[0].url, _BI)
        self.assertEqual(ranked[1].url, _UP)

    def test_no_history_at_all_keeps_the_static_server_order(self) -> None:
        ranked = self.rank([("AB", _AB), ("UP", _UP), ("BI", _BI)])
        self.assertEqual([r.label for r in ranked], ["BI", "UP", "AB"])

    def test_single_failure_demotes_but_does_not_cool_down(self) -> None:
        self.fail(_BI, "连接超时")
        record = self.record(_BI)

        self.assertEqual(record.consecutive_failures, 1)
        self.assertFalse(record.in_cooldown())

    def test_consecutive_failures_enter_cooldown_and_sort_last(self) -> None:
        self.fail(_BI, "HTTP 410")
        self.fail(_BI, "HTTP 410")
        record = self.record(_BI)

        self.assertEqual(record.consecutive_failures, 2)
        self.assertTrue(record.in_cooldown())

        ranked = self.rank([("BI", _BI), ("UP", _UP)])
        self.assertEqual(ranked[-1].url, _BI)
        self.assertTrue(ranked[-1].in_cooldown)
        self.assertIn("冷却", ranked[-1].reason)

    def test_cooldown_lapses_and_the_source_becomes_tryable_again(self) -> None:
        self.fail(_BI, "HTTP 410")
        self.fail(_BI, "HTTP 410")
        record = self.record(_BI)
        until = datetime.fromisoformat(record.cooldown_until())
        self.assertTrue(record.in_cooldown(until - timedelta(seconds=1)))

        later = until + timedelta(seconds=1)
        ranked = rank_sources(
            [("BI", _BI)], health={health_key(_BI): record}, now=later
        )

        self.assertFalse(ranked[0].in_cooldown)
        # Still carrying its failure penalty, so it does not jump ahead of a
        # healthy source — it is merely attemptable again.
        self.assertLess(ranked[0].score, 0.0)

    def test_a_success_clears_the_failure_streak(self) -> None:
        self.fail(_BI, "连接超时")
        self.fail(_BI, "连接超时")
        self.assertTrue(self.record(_BI).in_cooldown())

        self.play(_BI, 500)
        record = self.record(_BI)

        self.assertEqual(record.consecutive_failures, 0)
        self.assertFalse(record.in_cooldown())
        self.assertEqual(record.success_count, 1)
        self.assertEqual(record.latency_ms, 500)

    def test_health_is_keyed_on_the_url_not_the_display_label(self) -> None:
        # The site renames its server buttons; the embed URL stays put. A label
        # key would lose this history on the next rename.
        self.fail(_BI, "HTTP 410")
        self.fail(_BI, "HTTP 410")

        renamed = self.rank([("线路1", _BI), ("UP", _UP)])

        self.assertEqual(renamed[-1].url, _BI)
        self.assertTrue(renamed[-1].in_cooldown)


class ManualPreferenceTests(_HealthCase):
    """手动偏好压过评分，并且跨会话记住。"""

    def setUp(self) -> None:
        super().setUp()
        self.item = "https://asmrlib.com/posts/11111111111111111111111111111111"
        for _ in range(5):
            self.play(_BI, 500)
        for _ in range(3):
            self.fail(_UP, "HTTP 500")

    def test_manual_choice_beats_the_computed_score(self) -> None:
        candidates = [("BI", _BI), ("UP", _UP)]
        self.assertEqual(self.rank(candidates)[0].url, _BI)

        self.db.set_source_preference(self.item, _UP)
        ranked = self.rank(
            candidates, preferred_url=self.db.get_source_preference(self.item)
        )

        self.assertEqual(ranked[0].url, _UP)
        self.assertTrue(ranked[0].manual)
        self.assertEqual(ranked[0].reason, "手动首选")
        self.assertEqual(ranked[1].url, _BI)
        self.assertFalse(ranked[1].manual)

    def test_manual_choice_survives_reopening_the_archive(self) -> None:
        self.db.set_source_preference(self.item, _UP)
        self.db.close()

        # A fresh handle on the same file: nothing is cached in the process.
        self.db = ArchiveDb(self.path)

        self.assertEqual(self.db.get_source_preference(self.item), _UP)
        ranked = self.rank(
            [("BI", _BI), ("UP", _UP)],
            preferred_url=self.db.get_source_preference(self.item),
        )
        self.assertEqual(ranked[0].url, _UP)

    def test_manual_choice_outranks_even_a_cooling_down_source(self) -> None:
        # The user asked for this source explicitly; the score must not veto it.
        self.db.set_source_preference(self.item, _UP)
        self.assertTrue(self.record(_UP).in_cooldown())

        ranked = self.rank(
            [("BI", _BI), ("UP", _UP)],
            preferred_url=self.db.get_source_preference(self.item),
        )

        self.assertEqual(ranked[0].url, _UP)
        self.assertTrue(ranked[0].manual)
        self.assertTrue(ranked[0].in_cooldown)

    def test_a_second_choice_replaces_the_first(self) -> None:
        self.db.set_source_preference(self.item, _UP)
        self.db.set_source_preference(self.item, _AB)

        self.assertEqual(self.db.get_source_preference(self.item), _AB)
        ranked = self.rank(
            [("BI", _BI), ("UP", _UP), ("AB", _AB)],
            preferred_url=self.db.get_source_preference(self.item),
        )
        self.assertEqual(ranked[0].url, _AB)

    def test_clearing_the_preference_restores_the_computed_order(self) -> None:
        self.db.set_source_preference(self.item, _UP)
        self.db.clear_source_preference(self.item)

        self.assertEqual(self.db.get_source_preference(self.item), "")
        ranked = self.rank(
            [("BI", _BI), ("UP", _UP)],
            preferred_url=self.db.get_source_preference(self.item),
        )
        self.assertEqual(ranked[0].url, _BI)

    def test_preference_is_scoped_to_one_post(self) -> None:
        other = "https://asmrlib.com/posts/22222222222222222222222222222222"
        self.db.set_source_preference(self.item, _UP)

        self.assertEqual(self.db.get_source_preference(self.item), _UP)
        self.assertEqual(self.db.get_source_preference(other), "")


class PlaybackDegradationTests(_HealthCase):
    """自动降级：失败换下一个候选，全失败给出可读原因。"""

    def _session(self, candidates=None):
        candidates = candidates or [("BI", _BI), ("UP", _UP), ("AB", _AB)]
        return PlaybackSession(
            self.rank(candidates), recorder=self.db.record_playback_outcome
        )

    def test_failure_moves_to_the_next_candidate_and_records_it(self) -> None:
        session = self._session()
        self.assertEqual(session.current.url, _BI)

        nxt = session.report(PlaybackOutcome.FAILED, error="连接超时")

        self.assertEqual(nxt.url, _UP)
        self.assertEqual(session.current.url, _UP)
        self.assertFalse(session.exhausted)
        failed = self.record(_BI)
        self.assertEqual(failed.failure_count, 1)
        self.assertEqual(failed.last_error, "连接超时")
        self.assertEqual(failed.success_count, 0)
        self.assertEqual(failed.latency_ms, -1)

    def test_a_playing_report_ends_the_walk_and_is_recorded_as_success(self) -> None:
        session = self._session()
        session.report(PlaybackOutcome.FAILED, error="连接超时")

        current = session.report(PlaybackOutcome.PLAYING, latency_ms=740)

        self.assertEqual(current.url, _UP)
        self.assertEqual(session.started.url, _UP)
        self.assertEqual(session.failure_reason(), "")
        played = self.record(_UP)
        self.assertEqual(played.success_count, 1)
        self.assertEqual(played.latency_ms, 740)
        self.assertEqual(played.failure_count, 0)

    def test_all_candidates_failing_surfaces_a_readable_reason(self) -> None:
        session = self._session()
        session.report(PlaybackOutcome.FAILED, error="连接超时")
        session.report(PlaybackOutcome.FAILED, error="HTTP 403")
        session.report(PlaybackOutcome.FAILED, error="无法加载")

        self.assertTrue(session.exhausted)
        self.assertIsNone(session.current)
        self.assertIsNone(session.report(PlaybackOutcome.FAILED, error="再试"))
        reason = session.failure_reason()

        # Readable means: how many failed, which ones, and why each one failed.
        self.assertTrue(reason.strip())
        self.assertIn("3", reason)
        self.assertIn("bysetayico.com", reason)
        self.assertIn("upn.one", reason)
        self.assertIn("abyssplayer.com", reason)
        self.assertIn("连接超时", reason)
        self.assertIn("HTTP 403", reason)

    def test_failed_candidates_are_demoted_below_the_untried_one(self) -> None:
        candidates = [("BI", _BI), ("UP", _UP), ("AB", _AB)]
        session = self._session(candidates)
        session.report(PlaybackOutcome.FAILED, error="连接超时")
        session.report(PlaybackOutcome.FAILED, error="HTTP 403")

        # The next ranking must not start at a source that just failed.
        reranked = self.rank(candidates)

        self.assertEqual(reranked[0].url, _AB)
        self.assertEqual(reranked[0].score, 0.0)
        self.assertEqual(reranked[0].reason, "尚无记录")
        self.assertLess(reranked[1].score, 0.0)
        self.assertLess(reranked[2].score, 0.0)

    def test_repeated_failures_across_attempts_end_in_cooldown(self) -> None:
        # Two playback attempts on the same source: the streak crosses the
        # cooldown threshold, so the source is now reported as cooling down.
        for _ in range(2):
            session = PlaybackSession(
                self.rank([("BI", _BI)]), recorder=self.db.record_playback_outcome
            )
            self.assertEqual(session.current.url, _BI)
            session.report(PlaybackOutcome.FAILED, error="连接超时")

        record = self.record(_BI)
        self.assertEqual(record.consecutive_failures, 2)
        self.assertTrue(record.in_cooldown())
        demoted = self.rank([("BI", _BI), ("UP", _UP)])[-1]
        self.assertEqual(demoted.url, _BI)
        self.assertIn("冷却", demoted.reason)

    def test_only_the_attempted_source_is_recorded(self) -> None:
        session = self._session()
        session.report(PlaybackOutcome.FAILED, error="连接超时")

        self.assertEqual(self.record(_BI).failure_count, 1)
        # Candidates that were never tried keep no row at all.
        self.assertEqual(self.db.get_source_health([_UP, _AB]), {})

    def test_a_recent_failure_moves_the_next_attempt_off_that_source(self) -> None:
        first = PlaybackSession(
            self.rank([("BI", _BI), ("UP", _UP)]),
            recorder=self.db.record_playback_outcome,
        )
        self.assertEqual(first.current.url, _BI)
        first.report(PlaybackOutcome.FAILED, error="连接超时")

        # The next attempt must not start at the source that just failed.
        second = PlaybackSession(
            self.rank([("BI", _BI), ("UP", _UP)]),
            recorder=self.db.record_playback_outcome,
        )
        self.assertEqual(second.current.url, _UP)
        self.assertTrue(self.rank([("BI", _BI), ("UP", _UP)])[-1].in_cooldown)

    def test_preference_promotes_the_manual_choice_to_the_front(self) -> None:
        session = self._session().with_preference(_AB)

        self.assertEqual(session.current.url, _AB)
        self.assertTrue(session.current.manual)


class AttemptHonestyTests(_HealthCase):
    """没有真正开始播放的尝试，绝不记成功。"""

    def test_selecting_a_source_records_nothing(self) -> None:
        # Building the session (i.e. rendering the source rows and picking the
        # first one) is not an attempt. Nothing may be written yet.
        session = PlaybackSession(
            self.rank([("BI", _BI), ("UP", _UP)]),
            recorder=self.db.record_playback_outcome,
        )

        self.assertIsNotNone(session.current)
        self.assertEqual(self.db.get_source_health([_BI, _UP]), {})
        self.assertEqual(session.started, None)

    def test_a_failed_attempt_never_credits_a_success_or_a_latency(self) -> None:
        session = PlaybackSession(
            self.rank([("BI", _BI)]), recorder=self.db.record_playback_outcome
        )

        session.report(PlaybackOutcome.FAILED, error="播放器返回 HTML 页面")

        record = self.record(_BI)
        self.assertEqual(record.success_count, 0)
        self.assertEqual(record.last_success_at, "")
        self.assertEqual(record.latency_ms, -1)
        self.assertEqual(record.failure_count, 1)

    def test_an_attempt_that_never_started_is_not_counted_as_playing(self) -> None:
        session = PlaybackSession(
            self.rank([("BI", _BI), ("UP", _UP)]),
            recorder=self.db.record_playback_outcome,
        )
        session.report(PlaybackOutcome.FAILED, error="连接超时")

        # Still walking: no source has begun playing, so no success exists.
        self.assertIsNone(session.started)
        # `_UP` was never tried, so it has neither a success nor any record.
        self.assertEqual(self.db.get_source_health([_UP]), {})


class PlayPageHealthTests(unittest.TestCase):
    """播放页源行显示健康结论与首选原因，并支持手动覆盖（走真实 HTTP）。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        data = root / "data"
        data.mkdir()
        self.config = AppConfig(
            config_path=root / "config.yaml",
            output_dir=data,
            database_path=data / "archive.sqlite3",
            # A configured install lists the player hosts it is willing to
            # embed; the source rows only exist because of that.
            allowed_domains=[
                "asmrlib.com",
                "bysetayico.com",
                "upn.one",
                "abyssplayer.com",
            ],
        )
        self.viewer = ArchiveViewer(self.config)
        self.item = "https://asmrlib.com/posts/11111111111111111111111111111111"
        self.candidates = [("BI", _BI), ("UP", _UP), ("AB", _AB)]
        self.server = self.viewer.create_http_server("127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.viewer.close()
        self.temp_dir.cleanup()

    def post_json(self, path: str, payload: dict) -> tuple[int, dict]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        conn.request(
            "POST",
            path,
            body=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        body = response.read()
        status = response.status
        conn.close()
        return status, json.loads(body.decode("utf-8"))

    def source_panel(self) -> str:
        """The rendered source rows, in the order the page will try them."""

        with patch(
            "asmrlib_archiver.player_resolver.resolve_player_candidates",
            return_value=list(self.candidates),
        ):
            html, status = self.viewer._render_play({"url": [self.item]})
        self.assertEqual(status, 200)
        return html.split("class='glass-panel play-sources'", 1)[1].split(
            "</div>", 1
        )[0]

    def test_source_rows_show_the_health_verdict_and_the_preferred_marker(self) -> None:
        self.viewer.db.record_playback_outcome(_UP, PlaybackOutcome.PLAYING, 640, "")
        self.viewer.db.record_playback_outcome(_BI, PlaybackOutcome.FAILED, -1, "连接超时")
        self.viewer.db.set_source_preference(self.item, _AB)

        panel = self.source_panel()
        chips = panel.split("<button")

        # The manual choice is first, marked on, and says why.
        chosen = next(chip for chip in chips if _AB in chip)
        self.assertIn("is-on", chosen)
        self.assertIn("手动首选", chosen)

        # A source that has actually played shows its measured history.
        played = next(chip for chip in chips if _UP in chip)
        self.assertIn("成功 1 次", played)
        self.assertIn("640ms", played)

        # A source that just failed is flagged as cooling in the row itself.
        failed = next(chip for chip in chips if _BI in chip)
        self.assertIn("is-cooling", failed)

        self.assertLess(panel.index(_AB), panel.index(_UP))
        self.assertLess(panel.index(_UP), panel.index(_BI))

    def test_manual_override_beats_the_score_and_is_remembered(self) -> None:
        # A real success puts _UP ahead on score alone.
        self.viewer.db.record_playback_outcome(_UP, PlaybackOutcome.PLAYING, 500, "")
        panel = self.source_panel()
        self.assertLess(panel.index(_UP), panel.index(_AB))

        status, payload = self.post_json(
            "/api/play-preference", {"item_url": self.item, "url": _AB}
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

        # Stored, not merely echoed: the next render starts at the choice.
        self.assertEqual(self.viewer.db.get_source_preference(self.item), _AB)
        panel = self.source_panel()
        self.assertLess(panel.index(_AB), panel.index(_UP))
        self.assertIn("手动首选", panel)

    def test_reporting_a_playing_source_records_a_success(self) -> None:
        status, payload = self.post_json(
            "/api/play-outcome",
            {
                "item_url": self.item,
                "url": _UP,
                "outcome": "playing",
                "latency_ms": 820,
            },
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        record = self.viewer.db.get_source_health([_UP])[health_key(_UP)]
        self.assertEqual(record.success_count, 1)
        self.assertEqual(record.latency_ms, 820)

    def test_reporting_a_failure_cools_the_source_and_explains_the_dead_end(
        self,
    ) -> None:
        failures = [
            {"label": "BI", "url": _BI, "error": "连接超时"},
            {"label": "UP", "url": _UP, "error": "HTTP 403"},
        ]
        status, payload = self.post_json(
            "/api/play-outcome",
            {
                "item_url": self.item,
                "url": _UP,
                "outcome": "failed",
                "error": "HTTP 403",
                "failures": failures,
            },
        )

        self.assertEqual(status, 200)
        record = self.viewer.db.get_source_health([_UP])[health_key(_UP)]
        self.assertEqual(record.failure_count, 1)
        self.assertEqual(record.last_error, "HTTP 403")

        reason = payload["reason"]
        for token in ("bysetayico.com", "upn.one", "连接超时", "HTTP 403"):
            self.assertIn(token, reason)

    def test_an_attempt_with_no_outcome_is_rejected_and_records_nothing(self) -> None:
        # The page posts nothing when a source is merely selected; a claim
        # that is not a real outcome must not become a success.
        status, payload = self.post_json(
            "/api/play-outcome",
            {"item_url": self.item, "url": _UP, "outcome": "selected"},
        )

        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(self.viewer.db.get_source_health([_UP]), {})


if __name__ == "__main__":
    unittest.main()
