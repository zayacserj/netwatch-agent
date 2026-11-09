from contextlib import closing
from datetime import datetime, timezone
import importlib.util
import os
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
            copied = backup.copy_to_usb(self.root / "backup.tar.gz", self.root, "wanted")
        self.assertFalse(copied)
        self.assertFalse((self.root / "netwatch-backups").exists())

    @unittest.skipUnless(os.name == "posix", "USB copy uses Linux directory descriptors")
    def test_matching_usb_gets_the_verified_plain_archive(self):
        archive = self.root / "backup.tar.gz"
        archive.write_bytes(b"test archive")
        mount = self.root / "usb"
        mount.mkdir()
        with patch.object(backup, "mount_uuid", return_value="wanted"):
            self.assertTrue(backup.copy_to_usb(archive, mount, "WANTED"))
        directory = mount / "netwatch-backups"
        self.assertEqual([p.name for p in directory.iterdir()], [archive.name])
        self.assertEqual((directory / archive.name).read_bytes(), archive.read_bytes())

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

    def test_age_rotation_uses_archive_timestamp_and_keeps_cutoff(self):
        old = self.root / "netwatch-20261007T020000000000Z-aaaaaaaa.tar.gz"
        boundary = self.root / "netwatch-20261008T020000000000Z-bbbbbbbb.tar.gz"
        manual = self.root / "before-maintenance.tar.gz"
        for path in (old, boundary, manual):
            path.touch()
        backup.prune(self.root, keep=0, cutoff=datetime(2026, 10, 8, 2, tzinfo=timezone.utc))
        self.assertFalse(old.exists())
        self.assertTrue(boundary.exists())
        self.assertTrue(manual.exists())

    def test_prepare_restore_checks_runtime_and_uses_private_staging(self):
        database = self.root / "live.db"
        fixture = self.root / "fixture"
        fixture.write_text("placeholder", encoding="utf-8")
        with closing(sqlite3.connect(database)) as conn:
            conn.execute("CREATE TABLE devices (mac TEXT)")
        paths = {
            f"opt/netwatch/{name}": fixture for name in (
                "api.py", "db.py", "netwatch_telegram_bot.py", ".env", "agent3.sh",
                "monitor/pi_monitor.py", "monitor/index.html", "monitor/style.css",
                "monitor/dashboard.js",
            )
        }
        paths.update({"etc/caddy/Caddyfile": fixture, "var/www/netwatch/agent.sh": fixture})
        with patch.object(backup, "sync_directory"):
            archive = backup.create_archive(
                self.root / "scheduled", paths, {"opt/netwatch/netwatch.db": database},
                runtime={"python": "3.13", "packages": {"aiohttp": "3.14.5", "aiogram": "3.31.0"}},
            )
        stage, profile = backup.prepare_restore(archive, self.root)
        self.assertEqual(stage.parent, self.root)
        self.assertEqual(profile["python"], "3.13")
        self.assertEqual((stage / "requirements.txt").read_text(),
                         "aiohttp==3.14.5\naiogram==3.31.0\n")
        self.assertTrue((stage / "var/www/netwatch/agent.sh").is_file())
        self.assertFalse((self.root / "etc").exists())
        with self.assertRaises(RuntimeError):
            backup.safe_restore_path("opt/netwatch/../../outside")
