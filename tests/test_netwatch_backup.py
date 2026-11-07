from contextlib import closing
import importlib.util
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch


source = Path(__file__).resolve().parents[1] / "ops" / "backup_netwatch.py"
spec = importlib.util.spec_from_file_location("backup_netwatch", source)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


class Backup(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_archive_restores_committed_wal_data_and_keeps_configuration(self):
        database = self.root / "live.db"
        config = self.root / "config"
        config.write_text("placeholder", encoding="utf-8")
        with closing(sqlite3.connect(database)) as live:
            live.execute("PRAGMA journal_mode=WAL")
            live.execute("CREATE TABLE history (value INTEGER)")
            live.execute("INSERT INTO history VALUES (10)")
            live.commit()
            live.execute("INSERT INTO history VALUES (20)")
            with patch.object(backup, "sync_directory"):
                archive = backup.create_archive(
                    self.root / "scheduled", {"opt/netwatch/.env": config},
                    {"opt/netwatch/netwatch.db": database},
                )
            self.assertEqual(backup.verify_archive(archive), (2, 1))
            restored = self.root / "restored.db"
            with tarfile.open(archive, "r:gz") as contents:
                self.assertEqual(contents.extractfile("opt/netwatch/.env").read(), b"placeholder")
                restored.write_bytes(contents.extractfile("opt/netwatch/netwatch.db").read())
            with closing(sqlite3.connect(restored)) as conn:
                self.assertEqual(conn.execute("SELECT value FROM history").fetchall(), [(10,)])
            self.assertEqual(live.execute("SELECT COUNT(*) FROM history").fetchone()[0], 2)

    def test_wrong_usb_is_not_written(self):
        with patch.object(backup, "mount_uuid", return_value="other-device"):
            copied = backup.encrypted_usb_copy(self.root / "backup.tar.gz", self.root, "wanted", "age1-test")
        self.assertFalse(copied)
        self.assertFalse((self.root / "netwatch-backups").exists())

    def test_rotation_does_not_delete_manual_backups(self):
        for filename in (
            "netwatch-20261107T032000000000Z-aaaaaaaa.tar.gz",
            "netwatch-20261108T032000000000Z-bbbbbbbb.tar.gz",
            "before-manual-maintenance.tar.gz",
        ):
            (self.root / filename).touch()
        backup.prune(self.root, keep=1)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), [
            "before-manual-maintenance.tar.gz", "netwatch-20261108T032000000000Z-bbbbbbbb.tar.gz",
        ])
