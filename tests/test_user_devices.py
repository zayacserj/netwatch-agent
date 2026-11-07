"""Regression coverage for the Telegram bot's device queries."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


DEPLOY = Path(__file__).resolve().parents[1] / 'deploy_files'
spec = importlib.util.spec_from_file_location('db_user_devices_test', DEPLOY / 'db_fixed.py')
db = importlib.util.module_from_spec(spec)
spec.loader.exec_module(db)


class UserDevices(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path_patch = patch.object(db, 'DB_PATH', Path(tmp.name) / 'isolated.sqlite')
        path_patch.start()
        self.addCleanup(path_patch.stop)
        db.init_db()
        with db.db_session() as conn:
            conn.executemany('INSERT INTO users(chat_id) VALUES (?)', [(1,), (2,)])
            conn.executemany('INSERT INTO routers(router_id, chat_id, name) VALUES (?,?,?)',
                             [('r1', 1, 'Home'), ('r2', 1, 'Office'), ('other', 2, 'Other')])
            conn.executemany('INSERT INTO devices(mac, router_id, last_seen) VALUES (?,?,?)',
                             [('shared', 'r1', 10), ('new', 'r1', 30),
                              ('shared', 'r2', 20), ('private', 'other', 40)])

    def test_all_user_devices_and_explicit_none(self):
        rows = db.get_user_devices(1)
        self.assertEqual([(r['mac'], r['router_id'], r['router_name']) for r in rows],
                         [('new', 'r1', 'Home'), ('shared', 'r2', 'Office'),
                          ('shared', 'r1', 'Home')])
        self.assertEqual(db.get_user_devices(1, None), rows)
        self.assertEqual(db.get_user_devices(999), [])

    def test_router_filter_positional_and_keyword(self):
        rows = db.get_user_devices(1, 'r1')
        self.assertEqual([(r['mac'], r['router_id'], r['router_name']) for r in rows],
                         [('new', 'r1', 'Home'), ('shared', 'r1', 'Home')])
        self.assertEqual(db.get_user_devices(chat_id=1, router_id='r1'), rows)
        self.assertEqual([r['mac'] for r in db.get_user_devices(1, 'r2')], ['shared'])

    def test_router_filter_preserves_user_isolation(self):
        self.assertEqual(db.get_user_devices(1, 'other'), [])
        self.assertEqual(db.get_user_devices(2, 'r1'), [])
        self.assertEqual(db.get_user_devices(1, 'missing'), [])
        self.assertEqual(db.get_user_devices(1, "r1' OR 1=1 --"), [])

    def test_router_rename_is_owner_scoped_and_survives_pairing(self):
        self.assertFalse(db.rename_user_router(2, 'r1', 'Wrong owner'))
        self.assertTrue(db.rename_user_router(1, 'r1', 'Дом & Wi-Fi'))
        self.assertEqual(db.upsert_router('r1', 1, name='OpenWrt', mac='aa:bb:cc:dd:ee:ff'), 'r1')
        self.assertEqual(db.upsert_router('other-id', 1, name='Cudy', mac='aa:bb:cc:dd:ee:ff'), 'r1')
        self.assertEqual({r['router_name'] for r in db.get_user_devices(1, 'r1')}, {'Дом & Wi-Fi'})
        self.assertEqual(db.get_user_routers(2)[0]['name'], 'Other')

    def test_device_rename_requires_owner_and_targets_selected_router(self):
        self.assertFalse(db.set_device_custom_name('shared', 'r1', 'Denied', chat_id=2))
        self.assertTrue(db.set_device_custom_name('SHARED', 'r1', 'Колонка', chat_id=1))
        self.assertEqual(db.get_device(1, 'shared', 'r1')['custom_name'], 'Колонка')
        self.assertIsNone(db.get_device(1, 'shared', 'r2')['custom_name'])

    def test_unknown_hostname_uses_same_users_other_router(self):
        with db.db_session() as conn:
            conn.execute("UPDATE devices SET hostname='iPhone' WHERE mac='shared' AND router_id='r1'")
            conn.execute("UPDATE devices SET hostname='unknown', ip='192.168.10.55' WHERE mac='shared' AND router_id='r2'")
            conn.execute("INSERT INTO devices(mac, router_id, hostname, last_seen) VALUES ('shared','other','Private',99)")
        row = db.get_device(1, 'shared', 'r2')
        self.assertEqual((row['known_hostname'], row['ip'], row['router_id']), ('iPhone', '192.168.10.55', 'r2'))
        self.assertEqual(db.get_user_devices(1, 'r2')[0]['known_hostname'], 'iPhone')
        with db.db_session() as conn:
            conn.execute("UPDATE devices SET hostname='*' WHERE mac='shared' AND router_id='r1'")
        self.assertIsNone(db.get_device(1, 'shared', 'r2')['known_hostname'])


if __name__ == '__main__':
    unittest.main()
