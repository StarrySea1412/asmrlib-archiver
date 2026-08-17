from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from asmrlib_archiver.config import AppConfig
from asmrlib_archiver.db import ArchiveDb
from asmrlib_archiver.export import CatalogExporter
from asmrlib_archiver.models import MediaCandidate
from asmrlib_archiver.storage import Storage


class ExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.data = root / "data"
        self.data.mkdir()
        self.config = AppConfig(
            config_path=root / "config.yaml",
            output_dir=self.data,
            database_path=self.data / "archive.sqlite3",
        )
        self.db = ArchiveDb(self.config.database_path)
        self.db.init()
        self.source = "https://asmrlib.com/posts/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        self.db.add_seed(self.source)
        self.db.update_item_result(
            self.source,
            title="Export sample",
            published_at="2026-07-01",
            status="archived",
        )
        self.db.add_tag_seed("https://asmrlib.com/tags/yoonying")
        self.db.complete_tag_page(
            "https://asmrlib.com/tags/yoonying",
            "https://asmrlib.com/tags/yoonying",
            post_urls=[self.source],
            content_hash="export-hash",
        )
        media_path = self.data / "videos" / "sample.mp3"
        media_path.parent.mkdir(parents=True, exist_ok=True)
        media_path.write_bytes(b"ID3fake")
        self.db.replace_media_candidates(
            self.source,
            [
                MediaCandidate(
                    self.source,
                    "https://bysetayico.com/e/abc",
                    label="BI",
                    kind="embed",
                    status="reference",
                    error="player_reference_not_downloaded",
                ),
                MediaCandidate(
                    self.source,
                    "https://asmrlib.com/media/sample.mp3",
                    label="Audio",
                    kind="download",
                    status="pending",
                ),
            ],
        )
        media_id = self.db.conn.execute(
            "SELECT id FROM media_candidates WHERE kind = 'download'"
        ).fetchone()["id"]
        self.db.update_media_result(
            media_id,
            status="downloaded",
            file_path=str(media_path),
        )
        self.exporter = CatalogExporter(self.db, Storage(self.data))

    def tearDown(self) -> None:
        self.db.close()
        self.temp_dir.cleanup()

    def test_export_json_catalog(self) -> None:
        result = self.exporter.export(fmt="json")
        self.assertTrue(result.path.is_file())
        self.assertEqual(result.posts, 1)
        self.assertEqual(result.references, 1)
        self.assertEqual(result.downloaded, 1)
        payload = json.loads(result.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["summary"]["posts"], 1)
        self.assertEqual(payload["posts"][0]["title"], "Export sample")
        self.assertEqual(payload["posts"][0]["servers"], ["BI"])
        statuses = {item["status"] for item in payload["posts"][0]["media"]}
        self.assertEqual(statuses, {"reference", "downloaded"})

    def test_export_csv_and_markdown(self) -> None:
        csv_path = self.data / "out.csv"
        md_path = self.data / "out.md"
        csv_result = self.exporter.export(fmt="csv", output=csv_path)
        md_result = self.exporter.export(fmt="markdown", output=md_path)
        self.assertEqual(csv_result.path, csv_path.resolve())
        self.assertEqual(md_result.path, md_path.resolve())
        csv_text = csv_path.read_text(encoding="utf-8")
        self.assertIn("bysetayico.com", csv_text)
        self.assertIn("Export sample", csv_text)
        md_text = md_path.read_text(encoding="utf-8")
        self.assertIn("Player references are catalog-only", md_text)
        self.assertIn("BI", md_text)
