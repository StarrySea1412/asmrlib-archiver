from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from asmrlib_archiver.cli import build_parser, main
from asmrlib_archiver.config import load_config
from asmrlib_archiver.crawler import Archiver


TAG_URL = "https://asmrlib.com/tags/yoonying"


class CliTests(unittest.TestCase):
    def test_open_player_command_is_removed(self) -> None:
        parser = build_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["open-player", "https://asmrlib.com/posts/example"])

    def test_limits_must_be_positive(self) -> None:
        parser = build_parser()
        for argv in [
            ["discover", "--limit", "0"],
            ["crawl", "--limit", "-1"],
            ["run", "--page-limit", "0"],
        ]:
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args(argv)

    def test_historical_blocked_tag_makes_discover_and_run_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "tag_seeds:",
                        f'  - "{TAG_URL}"',
                        f'output_dir: "{(root / "data").as_posix()}"',
                        f'database_path: "{(root / "data" / "archive.sqlite3").as_posix()}"',
                        "crawler:",
                        "  obey_robots: false",
                    ]
                ),
                encoding="utf-8",
            )
            archiver = Archiver(load_config(config_path))
            try:
                archiver.init()
                archiver.add_config_seeds()
                archiver.db.mark_tag_page_failed(
                    TAG_URL,
                    TAG_URL,
                    status="blocked",
                    error="robots_disallow",
                )
            finally:
                archiver.close()

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--config", str(config_path), "discover"]), 1)
                self.assertEqual(main(["--config", str(config_path), "run"]), 1)

    def test_run_prints_each_stage_before_work_and_keeps_final_summary(self) -> None:
        messages: list[str] = []

        class FakeArchiver:
            def __init__(self, _config, progress=None) -> None:
                self.progress = progress

            def init(self) -> None:
                self._assert_last_message("Starting run.")

            def sanitize_existing(self) -> tuple[int, int]:
                self._assert_last_message("Stage 1/4: sanitize")
                return 1, 0

            def add_config_seeds(self) -> int:
                return 2

            def discover(self, **_kwargs):
                self._assert_last_message("Stage 2/4: discover")
                return SimpleNamespace(
                    pages_ok=2,
                    pages_failed=0,
                    posts_added=2,
                    duplicate_pages=0,
                    incomplete=0,
                )

            def status(self) -> dict[str, int]:
                return {"tags.total": 1}

            def crawl(self, **_kwargs) -> tuple[int, int]:
                self._assert_last_message("Stage 3/4: crawl")
                return 2, 0

            def download(self, **_kwargs) -> tuple[int, int]:
                self._assert_last_message("Stage 4/4: download")
                return 0, 0

            def unresolved(self) -> dict[str, int]:
                return {"tag_pages": 0, "items": 0, "media": 0}

            def close(self) -> None:
                pass

            def _assert_last_message(self, expected: str) -> None:
                if not messages or messages[-1] != expected:
                    raise AssertionError(f"Expected progress before work: {expected}")

        config = SimpleNamespace(
            discovery=SimpleNamespace(max_posts_per_tag=20),
        )
        with (
            patch("asmrlib_archiver.cli.load_config", return_value=config),
            patch("asmrlib_archiver.cli.Archiver", FakeArchiver),
            patch("asmrlib_archiver.cli._console_print", side_effect=messages.append),
        ):
            result = main(["run"])

        self.assertEqual(result, 0)
        self.assertEqual(
            messages[:5],
            [
                "Starting run.",
                "Stage 1/4: sanitize",
                "Stage 2/4: discover",
                "Stage 3/4: crawl",
                "Stage 4/4: download",
            ],
        )
        self.assertTrue(messages[-1].startswith("Run complete. "))
        self.assertIn("tag_pages=2", messages[-1])
        self.assertIn("crawled=2", messages[-1])


if __name__ == "__main__":
    unittest.main()
