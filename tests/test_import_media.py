from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from asmrlib_archiver.config import AppConfig
from asmrlib_archiver.db import ArchiveDb
from asmrlib_archiver.import_media import LocalMediaImporter
from asmrlib_archiver.storage import Storage


class ImportMediaTests(unittest.TestCase):
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
        self.source = "https://asmrlib.com/posts/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        self.db.add_seed(self.source)
        self.db.update_item_result(
            self.source,
            title="Importable track",
            status="archived",
        )
        self.src_file = root / "owned.mp3"
        self.src_file.write_bytes(b"ID3owned-audio-bytes")
        self.importer = LocalMediaImporter(self.db, Storage(self.data))

    def tearDown(self) -> None:
        self.db.close()
        self.temp_dir.cleanup()

    def test_import_copies_and_marks_downloaded(self) -> None:
        result = self.importer.import_file(
            source=self.source,
            file_path=self.src_file,
            label="My copy",
        )
        self.assertTrue(result.copied)
        self.assertTrue(result.stored_path.is_file())
        self.assertTrue(str(result.stored_path).endswith(".mp3"))
        rows = self.db.list_media_for_item(self.source)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "downloaded")
        self.assertEqual(rows[0]["label"], "My copy")
        self.assertEqual(rows[0]["file_path"], str(result.stored_path))

    def test_import_by_post_id_and_no_copy(self) -> None:
        post_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        result = self.importer.import_file(
            source=post_id,
            file_path=self.src_file,
            copy=False,
        )
        self.assertFalse(result.copied)
        self.assertEqual(result.stored_path, self.src_file.resolve())
        playable = self.db.list_playable_media()
        self.assertEqual(len(playable), 1)
        self.assertEqual(playable[0]["source_url"], self.source)
