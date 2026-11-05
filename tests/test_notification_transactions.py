"""Run with: python -m unittest discover -s tests -v. No live services."""
from contextlib import closing
import asyncio
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

DEPLOY = Path(__file__).resolve().parents[1] / 'deploy_files'


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, DEPLOY / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


db = load('db', 'db_fixed.py')
api = load('api_fixed_test', 'api_fixed.py')


class Request:
    host = 'localhost'

    def __init__(self, data):
        self.data = data

    async def json(self):
        return self.data


class TrackedConnection(sqlite3.Connection):
    closed = False
    rolled_back = False
    fail_commit = False

    def commit(self):
        if self.fail_commit:
            raise sqlite3.OperationalError('injected commit failure')
        super().commit()

    def rollback(self):
        self.rolled_back = True
        super().rollback()

    def close(self):
        self.closed = True
        super().close()


class Transactions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.path = Path(self.tmp.name) / 'isolated.sqlite'
        self.path_patch = patch.object(db, 'DB_PATH', self.path)
        self.path_patch.start()
        db.init_db()
        with db.db_session() as conn:
            conn.execute('INSERT INTO users(chat_id) VALUES (1)')
            conn.execute("INSERT INTO routers(router_id,chat_id) VALUES ('r',1)")
        self.connections = []
        self.fail_commit = False

        def connect():
            conn = sqlite3.connect(self.path, factory=TrackedConnection, timeout=0.1)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            conn.fail_commit = self.fail_commit
            self.connections.append(conn)
            return conn

        self.conn_patch = patch.object(db, 'get_conn', connect)
        self.conn_patch.start()
        self.sent = []

        def send(chat_id, text):
            self.assertTrue(all(c.closed for c in self.connections))
            # A separate writer must be able to acquire a lock during send.
            with closing(sqlite3.connect(self.path, timeout=0.1)) as other:
                other.execute('BEGIN IMMEDIATE')
                self.assertGreater(other.execute('SELECT count(*) FROM device_notifications').fetchone()[0], 0)
            self.sent.append((chat_id, text))

        self.send_patch = patch.object(api, 'send_telegram_notification', send)
        self.send_patch.start()

    def tearDown(self):
        self.send_patch.stop()
        self.conn_patch.stop()
        self.path_patch.stop()
        self.tmp.cleanup()

    def run_handler(self, name, data):
        return asyncio.run(getattr(api, name)(Request(data)))

    def count(self, table):
        with closing(sqlite3.connect(self.path)) as conn:
            return conn.execute('SELECT count(*) FROM ' + table).fetchone()[0]

    def test_event_commits_before_send(self):
        self.run_handler('post_event', dict(router_id='r', mac='AA', event='CONNECTED'))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.count('events'), 1)

    def seed_device(self, mac='aa', online=1, watch=1):
        with db.db_session() as conn:
            conn.execute("""INSERT INTO devices
                (mac,router_id,online,watch_online,custom_name,hostname)
                VALUES (?,'r',?,?, 'My device','host')""", (mac, online, watch))

    def online(self, mac='aa'):
        with closing(sqlite3.connect(self.path)) as conn:
            row = conn.execute('SELECT online FROM devices WHERE mac=?', (mac,)).fetchone()
            return row[0] if row else None

    def test_disconnect_formats_mark_offline_and_notify_once(self):
        for index, event in enumerate(('DISCONNECTED', 'disconnected', 'AP-STA-DISCONNECTED')):
            with self.subTest(event=event):
                mac = 'device-' + str(index)
                self.seed_device(mac)
                self.run_handler('post_event', dict(router_id='r', mac=mac, event=event))
                self.assertEqual(self.online(mac), 0)
                self.assertEqual(self.sent[-1], (1, f'🔴 Устройство оффлайн: `{mac}` — My device'))
                self.assertEqual(len(self.sent), index + 1)
                self.run_handler('post_event', dict(router_id='r', mac=mac, event=event))
                self.assertEqual(len(self.sent), index + 1)

    def test_disconnect_without_watch_or_already_offline_is_silent(self):
        for mac, online, watch in [('unwatched', 1, 0), ('offline', 0, 1)]:
            self.seed_device(mac, online, watch)
            self.run_handler('post_event', dict(router_id='r', mac=mac, event='DISCONNECTED'))
            self.assertEqual(self.online(mac), 0)
        self.assertEqual(self.sent, [])

    def test_disconnect_unknown_device_does_not_create_online_device(self):
        self.run_handler('post_event', dict(router_id='r', mac='aa', event='DISCONNECTED'))
        self.assertIsNone(self.online())
        self.assertEqual(self.count('events'), 1)
        self.assertEqual(self.sent, [])

    def test_disconnect_respects_recent_notification(self):
        self.seed_device()
        db.mark_device_notified(1, 'aa', 'r')
        self.run_handler('post_event', dict(router_id='r', mac='aa', event='DISCONNECTED'))
        self.assertEqual(self.online(), 0)
        self.assertEqual(self.sent, [])

    def test_disconnect_commit_failure_keeps_online_without_notification(self):
        self.seed_device()
        self.fail_commit = True
        with self.assertRaises(sqlite3.OperationalError):
            self.run_handler('post_event', dict(router_id='r', mac='aa', event='DISCONNECTED'))
        self.assertEqual(self.online(), 1)
        self.assertEqual(self.count('events'), 0)
        self.assertEqual(self.count('device_notifications'), 0)
        self.assertEqual(self.sent, [])
        self.assertTrue(self.connections[-1].rolled_back)
        self.assertTrue(self.connections[-1].closed)

    def test_connected_formats_still_mark_online(self):
        for index, event in enumerate(('CONNECTED', 'connected', 'AP-STA-CONNECTED')):
            mac = 'device-' + str(index)
            self.seed_device(mac, online=0)
            self.run_handler('post_event', dict(router_id='r', mac=mac, event=event))
            self.assertEqual(self.online(mac), 1)
            self.assertTrue(self.sent[-1][1].startswith('🟢 Устройство онлайн:'))
        self.assertEqual(len(self.sent), 3)

    def test_snapshot_batch_and_dedup(self):
        data = dict(router_id='r', devices=[{'mac': 'aa'}, {'mac': 'aa'}, {'mac': 'bb'}])
        result = self.run_handler('post_devices', data)
        self.assertEqual(json.loads(result.text)['saved_devices'], 3)
        self.assertEqual(len(self.sent), 2)
        self.run_handler('post_devices', data)
        self.assertEqual(len(self.sent), 2)

    def test_commit_failure_rolls_back_without_send(self):
        for handler, data in [('post_event', dict(router_id='r', mac='aa', event='CONNECTED')),
                              ('post_devices', dict(router_id='r', devices=[{'mac': 'aa'}]))]:
            with self.subTest(handler=handler):
                self.fail_commit = True
                with self.assertRaises(sqlite3.OperationalError):
                    self.run_handler(handler, data)
                self.assertTrue(self.connections[-1].closed)
                self.assertTrue(self.connections[-1].rolled_back)
                self.assertEqual(self.sent, [])
                self.assertEqual(self.count('devices'), 0)
                self.assertEqual(self.count('device_notifications'), 0)
                self.assertEqual(self.count('events'), 0)

    def test_batch_processing_failure_rolls_back(self):
        with self.assertRaises(AttributeError):
            self.run_handler('post_devices', dict(router_id='r', devices=[{'mac': 'aa'}, None]))
        self.assertEqual(self.count('devices'), 0)
        self.assertEqual(self.count('device_notifications'), 0)
        self.assertTrue(self.connections[-1].closed)
        self.assertTrue(self.connections[-1].rolled_back)
        self.assertEqual(self.sent, [])

    def test_upsert_dedup_still_commits_update(self):
        self.assertTrue(db.upsert_device_with_notification(1, 'aa', 'r', {'event': 'CONNECTED'}))
        self.assertFalse(db.upsert_device_with_notification(1, 'aa', 'r', {'ip': 'new'}))
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT ip FROM devices').fetchone()[0], 'new')
        self.assertTrue(all(c.closed for c in self.connections))

    def test_external_connection_owned_by_caller(self):
        conn = db.get_conn()
        db.get_user_by_router_id('r', conn=conn)
        db.can_notify_device(1, 'aa', 'r', conn=conn)
        self.assertFalse(conn.closed)
        conn.close()

    def test_health(self):
        self.run_handler('post_health', dict(router_id='r', internet=True, wifi=True, cpu='1', ram='2', wifi_clients=1, podkop_status='ok', podkop_test=True))
        self.assertEqual(self.count('health_checks'), 1)
        self.assertTrue(all(c.closed for c in self.connections))

    def test_pair_and_auth(self):
        db.create_pairing_code('code', 1)
        result = self.run_handler('post_pair', dict(code='code', router_id='r'))
        secret = json.loads(result.text)['agent_secret']
        self.assertTrue(db.is_valid_router_secret(secret))
        self.assertIsNone(db.get_pairing_code('code'))
        self.assertTrue(all(c.closed for c in self.connections))

    def test_standalone_mark_commit_failure(self):
        with db.db_session() as conn:
            conn.execute("INSERT INTO devices(mac,router_id) VALUES ('aa','r')")
        self.fail_commit = True
        with self.assertRaises(sqlite3.OperationalError):
            db.mark_device_notified(1, 'aa', 'r')
        self.assertEqual(self.count('device_notifications'), 0)
        self.assertTrue(self.connections[-1].rolled_back)
        self.assertTrue(self.connections[-1].closed)

    def test_cancellation_rolls_back_and_closes(self):
        with self.assertRaises(asyncio.CancelledError):
            with db.db_session() as conn:
                conn.execute("INSERT INTO devices(mac,router_id) VALUES ('aa','r')")
                raise asyncio.CancelledError
        self.assertEqual(self.count('devices'), 0)
        self.assertTrue(self.connections[-1].rolled_back)
        self.assertTrue(self.connections[-1].closed)

    def test_network_failure_preserves_commit(self):
        with patch.object(api, 'send_telegram_notification', side_effect=RuntimeError('network')):
            with self.assertRaises(RuntimeError):
                self.run_handler('post_event', dict(router_id='r', mac='aa', event='CONNECTED'))
        self.assertEqual(self.count('events'), 1)
        self.assertEqual(self.count('device_notifications'), 1)
        self.assertFalse(self.connections[-1].rolled_back)
        self.assertTrue(self.connections[-1].closed)

    def offline(self):
        async def stop(_):
            raise asyncio.CancelledError
        with patch.object(api.asyncio, 'sleep', stop):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(api.check_offline_devices(None))

    def test_offline_commits_before_send(self):
        with db.db_session() as conn:
            conn.execute("INSERT INTO devices(mac,router_id,online,last_seen) VALUES ('aa','r',1,0)")
        self.offline()
        self.assertEqual(len(self.sent), 1)

    def test_offline_commit_failure(self):
        with db.db_session() as conn:
            conn.execute("INSERT INTO devices(mac,router_id,online,last_seen) VALUES ('aa','r',1,0)")
        original_commit = TrackedConnection.commit
        calls = []

        def fail_second(conn):
            calls.append(1)
            if len(calls) == 2:
                raise sqlite3.OperationalError('offline commit failure')
            return original_commit(conn)

        with patch.object(TrackedConnection, 'commit', fail_second):
            self.offline()
        self.assertEqual(self.sent, [])
        self.assertTrue(self.connections[-1].rolled_back)
        self.assertTrue(self.connections[-1].closed)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT online FROM devices').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
