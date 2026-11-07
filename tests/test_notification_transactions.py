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
            conn.execute("INSERT INTO routers(router_id,chat_id,online) VALUES ('r',1,1)")
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
        self.assertEqual(self.count('events'), 0)
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

    def test_history_records_snapshot_transitions_even_with_notifications_muted(self):
        report = dict(router_id='r', devices=[{'mac': 'aa', 'hostname': 'Phone'}])
        self.run_handler('post_devices', report)
        self.run_handler('post_devices', report)
        self.assertEqual(db.disable_device_notifications(1), 1)
        with db.db_session() as conn:
            conn.execute("UPDATE devices SET last_seen=0 WHERE mac='aa'")
        self.offline()
        self.offline()
        self.run_handler('post_devices', report)
        self.run_handler('post_devices', report)
        rows = db.get_user_events(1)
        self.assertEqual([row['event'] for row in rows], [
            'device_online', 'device_offline', 'device_new',
        ])
        self.assertEqual(rows[0]['known_hostname'], 'Phone')
        self.assertEqual(len(self.sent), 1)

    def test_history_keeps_same_mac_separate_for_different_users(self):
        self.seed_device()
        with db.db_session() as conn:
            conn.execute("INSERT INTO routers(router_id,chat_id,online) VALUES ('second',1,1)")
            conn.execute("INSERT INTO users(chat_id) VALUES (2)")
            conn.execute("INSERT INTO routers(router_id,chat_id,online) VALUES ('other',2,1)")
        self.run_handler('post_devices', dict(router_id='second', devices=[{'mac': 'aa'}]))
        self.run_handler('post_devices', dict(router_id='other', devices=[{'mac': 'aa'}]))
        self.assertEqual(db.get_user_events(1), [])
        self.assertEqual([r['event'] for r in db.get_user_events(2)], ['device_new'])

    def test_history_ignores_roaming_both_ways_and_records_real_departure(self):
        with db.db_session() as conn:
            conn.execute("INSERT INTO routers(router_id,chat_id,online) VALUES ('second',1,1)")

        def snapshot(router_id):
            self.run_handler('post_devices', dict(router_id=router_id, devices=[{'mac': 'aa'}]))

        def disconnect(router_id):
            self.run_handler('post_event', dict(router_id=router_id, mac='aa', event='DISCONNECTED'))

        snapshot('r')
        snapshot('second')
        disconnect('r')
        self.offline()
        disconnect('second')
        self.offline()
        snapshot('r')
        self.offline()
        self.assertEqual([row['event'] for row in db.get_user_events(1)], ['device_new'])

        with db.db_session() as conn:
            conn.execute("UPDATE devices SET last_seen=0 WHERE mac='aa'")
        self.offline()
        self.offline()
        snapshot('second')
        snapshot('second')
        self.assertEqual([row['event'] for row in db.get_user_events(1)], [
            'device_online', 'device_offline', 'device_new',
        ])

    def test_presence_migration_uses_existing_online_status_without_history(self):
        self.seed_device()
        with db.db_session() as conn:
            conn.execute("DROP TABLE device_history_presence")
        db.init_db()
        db.init_db()
        self.run_handler('post_devices', dict(router_id='r', devices=[{'mac': 'aa'}]))
        with db.db_session() as conn:
            self.assertEqual(conn.execute("SELECT online FROM device_history_presence WHERE chat_id=1 AND mac='aa'").fetchone()[0], 1)
        self.assertEqual(db.get_user_events(1), [])

    def test_bulk_disable_only_changes_owned_devices_and_keeps_discovery(self):
        self.seed_device(online=0)
        with db.db_session() as conn:
            conn.execute("INSERT INTO users(chat_id) VALUES (2)")
            conn.execute("INSERT INTO routers(router_id,chat_id) VALUES ('other',2)")
            conn.execute("INSERT INTO devices(mac,router_id) VALUES ('aa','other')")

        self.assertEqual(db.disable_device_notifications(1), 1)
        self.assertEqual(db.disable_device_notifications(1), 0)
        self.run_handler('post_devices', dict(router_id='r', devices=[{'mac': 'aa'}, {'mac': 'bb'}]))
        self.assertEqual(len(self.sent), 1)
        self.assertIn('Новое устройство: `bb`', self.sent[0][1])
        with db.db_session() as conn:
            self.assertEqual(conn.execute("SELECT watch_online FROM devices WHERE router_id='other'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT online, watch_online FROM devices WHERE router_id='r' AND mac='aa'").fetchone()[:], (1, 0))

    def test_settings_migration_keeps_existing_preferences(self):
        self.seed_device(watch=0)
        with db.db_session() as conn:
            conn.execute("ALTER TABLE user_settings DROP COLUMN watch_podkop_proxy")
            conn.execute("ALTER TABLE user_settings DROP COLUMN watch_router_online")
            conn.execute("ALTER TABLE routers DROP COLUMN podkop_proxy_tag")
            conn.execute("ALTER TABLE routers DROP COLUMN podkop_proxy_sample_at")
            conn.execute("INSERT INTO user_settings(chat_id, watch_online) VALUES (1, 0)")
        db.init_db()
        db.init_db()
        self.assertEqual(db.get_notification_settings(1), {'podkop': False, 'router': False})
        with db.db_session() as conn:
            self.assertEqual(conn.execute("SELECT watch_online FROM user_settings WHERE chat_id=1").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT watch_online FROM devices WHERE router_id='r'").fetchone()[0], 0)

    def test_proxy_alert_uses_last_valid_tag_and_respects_setting(self):
        def report(tag, timestamp, status='running'):
            self.run_handler('post_health', dict(router_id='r', internet=True, wifi=True,
                             cpu='1%', ram='2', wifi_clients=1, podkop_status=status,
                             podkop_test=None, podkop_proxy_tag=tag, timestamp=timestamp))

        with patch.object(api, 'send_telegram_notification') as send:
            report('server-a', 100)
            report('server-b', 110)
            send.assert_not_called()
            self.assertTrue(db.toggle_notification_setting(1, 'podkop'))
            report(None, 120, 'unknown')
            report('older', 105)
            report('server-b', 130)
            send.assert_not_called()
            report('server-c', 140)
            report('server-c', 150)
            send.assert_called_once_with(1, '🔀 Podkop сменил прокси на r\nserver-b → server-c')

    def test_router_offline_and_recovery_alert_each_sent_once(self):
        self.assertEqual(db.get_notification_settings(1), {'podkop': False, 'router': False})
        self.assertTrue(db.toggle_notification_setting(1, 'router'))
        with db.db_session() as conn:
            conn.execute("UPDATE routers SET online=1,last_seen=0 WHERE router_id='r'")
        with patch.object(api, 'send_telegram_notification') as send:
            self.offline()
            self.offline()
            self.run_handler('post_devices', dict(router_id='r', devices=[]))
            self.run_handler('post_devices', dict(router_id='r', devices=[]))
            self.assertEqual([call.args for call in send.call_args_list], [
                (1, '🔴 Роутер оффлайн: r'), (1, '🟢 Роутер онлайн: r'),
            ])
            self.assertFalse(db.toggle_notification_setting(1, 'router'))
            with db.db_session() as conn:
                conn.execute("UPDATE routers SET last_seen=0 WHERE router_id='r'")
            self.offline()
            with db.db_session() as conn:
                self.assertEqual(conn.execute("SELECT online FROM routers WHERE router_id='r'").fetchone()[0], 0)
            self.assertEqual(send.call_count, 2)
        self.assertEqual([(row['event'], row['mac']) for row in db.get_user_events(1)], [
            ('router_offline', None), ('router_online', None), ('router_offline', None),
        ])

    def test_telemetry_secret_is_bound_to_payload_router(self):
        with db.db_session() as conn:
            conn.execute("INSERT INTO router_secrets(router_id,secret) VALUES ('r','secret')")
        request = Request({'router_id': 'other'})
        request.method = 'POST'
        request.path = '/health'
        request.headers = {'X-Agent-Secret': 'secret'}
        result = asyncio.run(api.auth_middleware(request, api.post_health))
        self.assertEqual(result.status, 403)
        self.assertEqual(self.count('health_checks'), 0)

    def test_health_preserves_unknown_proxy_test(self):
        self.run_handler('post_health', dict(router_id='r', internet=True, wifi=True,
                         cpu='13.7%', ram='192MiB/485MiB', wifi_clients=6,
                         podkop_status='running', podkop_test=None))
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT podkop_status, podkop_test FROM health_checks').fetchone(),
                             ('running', None))
        self.assertTrue(all(c.closed for c in self.connections))

    def test_health_stores_proxy_latency(self):
        self.run_handler('post_health', dict(router_id='r', internet=True, wifi=True,
                         cpu='13.7%', ram='192MiB/485MiB', wifi_clients=6,
                         podkop_status='running', podkop_test=1, podkop_latency_ms=159))
        row = db.get_latest_health('r')
        self.assertEqual((row['podkop_test'], row['podkop_latency_ms']), (1, 159))

    def test_health_rejects_invalid_latency_without_writing(self):
        for value in (-1, 1.5, True, '159', 60001):
            with self.subTest(value=value):
                result = self.run_handler('post_health', dict(router_id='r', internet=True, wifi=True,
                                         cpu='1%', ram='192MiB/485MiB', wifi_clients=6,
                                         podkop_status='running', podkop_test=1, podkop_latency_ms=value))
                self.assertEqual(result.status, 400)
                self.assertEqual(self.count('health_checks'), 0)

    def test_health_latency_migration_preserves_history_and_is_repeatable(self):
        with db.db_session() as conn:
            schema = conn.execute("SELECT sql FROM sqlite_master WHERE name='health_checks'").fetchone()['sql']
            self.assertIn('podkop_latency_ms INTEGER,', schema)
            conn.execute('DROP TABLE health_checks')
            conn.execute(schema.replace('podkop_latency_ms INTEGER,', ''))
            conn.execute("INSERT INTO health_checks(router_id, cpu, podkop_test, timestamp) VALUES ('r','2%',1,123)")
        db.init_db()
        db.init_db()
        row = db.get_latest_health('r')
        self.assertEqual((row['cpu'], row['timestamp'], row['podkop_test'], row['podkop_latency_ms']),
                         ('2%', 123, 1, None))

    def test_health_helper_accepts_nullable_probe_and_latency(self):
        db.add_health_check('r', True, True, '2%', '192MiB/485MiB', 6, 'running', None, timestamp=123)
        row = db.get_latest_health('r')
        self.assertEqual((row['timestamp'], row['podkop_test'], row['podkop_latency_ms']), (123, None, None))

    def test_pair_and_auth(self):
        db.create_pairing_code('code', 1)
        result = self.run_handler('post_pair', dict(code='code', router_id='r'))
        secret = json.loads(result.text)['agent_secret']
        self.assertTrue(db.is_valid_router_secret(secret))
        self.assertIsNone(db.get_pairing_code('code'))
        self.assertTrue(all(c.closed for c in self.connections))

    def test_pair_commit_failure_keeps_code_unused_and_router_absent(self):
        db.create_pairing_code('code', 1)
        self.fail_commit = True
        with self.assertRaises(sqlite3.OperationalError):
            self.run_handler('post_pair', dict(code='code', router_id='new'))
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT used FROM pairing_codes WHERE code='code'").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM routers WHERE router_id='new'").fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT count(*) FROM router_secrets').fetchone()[0], 0)
        self.assertTrue(self.connections[-1].rolled_back)
        self.assertTrue(self.connections[-1].closed)

    def test_pair_failure_after_router_upsert_rolls_everything_back(self):
        db.create_pairing_code('code', 1)
        with patch.object(api.secrets, 'token_hex', side_effect=RuntimeError('injected failure')):
            with self.assertRaises(RuntimeError):
                self.run_handler('post_pair', dict(code='code', router_id='new'))
        self.assertIsNotNone(db.get_pairing_code('code'))
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM routers WHERE router_id='new'").fetchone()[0], 0)

    def test_pair_replay_cannot_rotate_secret_or_add_router(self):
        db.create_pairing_code('code', 1)
        first = self.run_handler('post_pair', dict(code='code', router_id='new'))
        secret = json.loads(first.text)['agent_secret']
        replay = self.run_handler('post_pair', dict(code='code', router_id='another'))
        self.assertEqual(replay.status, 403)
        self.assertTrue(db.is_valid_router_secret(secret))
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM routers WHERE router_id='another'").fetchone()[0], 0)

    def test_pair_rejects_another_owners_router_without_consuming_code(self):
        with db.db_session() as conn:
            conn.execute('INSERT INTO users(chat_id) VALUES (2)')
            conn.execute("INSERT INTO router_secrets VALUES ('r','original')")
        db.create_pairing_code('other-code', 2)
        result = self.run_handler('post_pair', dict(code='other-code', router_id='r'))
        self.assertEqual(result.status, 409)
        self.assertIsNotNone(db.get_pairing_code('other-code'))
        self.assertTrue(db.is_valid_router_secret('original'))

    def test_pair_returns_canonical_router_id_and_records_it_on_code(self):
        with db.db_session() as conn:
            conn.execute("UPDATE routers SET mac='aa:bb:cc:dd:ee:ff' WHERE router_id='r'")
        db.create_pairing_code('code', 1)
        result = self.run_handler('post_pair', dict(code='code', router_id='new', mac='AA:BB:CC:DD:EE:FF',
                                                  name='Cudy "Router"'))
        self.assertEqual(json.loads(result.text)['router_id'], 'r')
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT router_id, used FROM pairing_codes WHERE code='code'").fetchone(), ('r', 1))
            self.assertEqual(conn.execute('SELECT count(*) FROM routers').fetchone()[0], 1)

    def test_pair_expired_code_is_not_consumed(self):
        db.create_pairing_code('expired', 1, expires_sec=-1)
        result = self.run_handler('post_pair', dict(code='expired', router_id='new'))
        self.assertEqual(result.status, 403)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT used FROM pairing_codes WHERE code='expired'").fetchone()[0], 0)

    def test_pair_invalid_payloads_are_rejected_without_writes(self):
        for data in ([], {'code': 'x', 'router_id': 'bad id'}, {'code': [], 'router_id': 'new'},
                     {'code': 'x', 'router_id': 'new', 'mac': 'bad-mac'}):
            with self.subTest(data=data):
                result = self.run_handler('post_pair', data)
                self.assertEqual(result.status, 400)
                self.assertEqual(self.count('routers'), 1)
                self.assertEqual(self.count('router_secrets'), 0)

    def test_identity_returns_only_the_authenticated_router(self):
        with db.db_session() as conn:
            conn.execute("INSERT INTO router_secrets VALUES ('r','private-secret')")
        request = Request({})
        request.headers = {'X-Agent-Secret': 'private-secret'}
        result = asyncio.run(api.get_agent_identity(request))
        self.assertEqual(json.loads(result.text), {'router_id': 'r'})
        request.headers = {'X-Agent-Secret': 'invalid'}
        self.assertEqual(asyncio.run(api.get_agent_identity(request)).status, 403)
        self.assertTrue(all(c.closed for c in self.connections))

    def test_only_one_concurrent_request_can_consume_a_pairing_code(self):
        from concurrent.futures import ThreadPoolExecutor
        db.create_pairing_code('shared-code', 1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.run_handler, 'post_pair', dict(code='shared-code', router_id=router))
                       for router in ('first', 'second')]
            statuses = [f.result().status for f in futures]
        self.assertEqual(sorted(statuses), [200, 403])
        self.assertEqual(self.count('router_secrets'), 1)
        self.assertEqual(self.count('routers'), 2)

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

    def test_snapshot_reconnect_notifies_once_and_preserves_name(self):
        self.seed_device(online=0)
        db.mark_device_notified(1, 'aa', 'r', notification_state=0)
        data = dict(router_id='r', devices=[{'mac': 'AA'}, {'mac': 'aa'}])
        result = self.run_handler('post_devices', data)
        self.assertEqual(json.loads(result.text)['new_devices'], [])
        self.assertEqual(self.sent, [(1, '🟢 Устройство онлайн: `aa` — My device')])
        self.assertEqual(self.online(), 1)
        self.assertEqual(db.get_device(1, 'aa', 'r')['custom_name'], 'My device')
        self.run_handler('post_devices', data)
        self.run_handler('post_event', dict(router_id='r', mac='aa', event='CONNECTED'))
        self.assertEqual(len(self.sent), 1)

    def test_notifications_use_known_name_from_another_router_of_same_user(self):
        with db.db_session() as conn:
            conn.execute("INSERT INTO users(chat_id) VALUES (2)")
            conn.executemany("INSERT INTO routers(router_id,chat_id) VALUES (?, ?)",
                             [('own', 1), ('other', 2)])
            conn.executemany("""
                INSERT INTO devices(mac,router_id,hostname,last_seen) VALUES (?, ?, ?, ?)
            """, [('aa', 'own', 'iPhone15pro', 1), ('aa', 'other', 'Private name', 2),
                  ('bb', 'other', 'Private name', 2)])
        self.run_handler('post_devices', dict(router_id='r', devices=[
            {'mac': 'aa', 'hostname': 'unknown'}, {'mac': 'bb', 'hostname': '*'},
        ]))
        self.run_handler('post_event', dict(router_id='r', mac='aa', event='DISCONNECTED'))
        self.run_handler('post_devices', dict(router_id='r', devices=[
            {'mac': 'aa', 'hostname': 'unknown'},
        ]))
        self.assertEqual(self.sent, [
            (1, '🆕 Новое устройство: `aa` — iPhone15pro'),
            (1, '🆕 Новое устройство: `bb` — bb'),
            (1, '🔴 Устройство оффлайн: `aa` — iPhone15pro'),
            (1, '🟢 Устройство онлайн: `aa` — iPhone15pro'),
        ])

    def test_snapshot_reconnect_without_watch_updates_silently(self):
        self.seed_device(online=0, watch=0)
        self.run_handler('post_devices', dict(router_id='r', devices=[{'mac': 'aa'}]))
        self.assertEqual(self.online(), 1)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.count('device_notifications'), 0)

    def test_snapshot_already_online_is_silent(self):
        self.seed_device()
        self.run_handler('post_devices', dict(router_id='r', devices=[{'mac': 'aa'}]))
        self.assertEqual(self.sent, [])

    def test_connected_event_respects_disabled_notifications(self):
        self.seed_device(online=0, watch=0)
        self.run_handler('post_event', dict(router_id='r', mac='aa', event='CONNECTED'))
        self.assertEqual(self.online(), 1)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.count('device_notifications'), 0)

    def test_opposite_transitions_are_not_suppressed_by_recent_notification(self):
        self.seed_device()
        self.run_handler('post_event', dict(router_id='r', mac='aa', event='DISCONNECTED'))
        self.run_handler('post_devices', dict(router_id='r', devices=[{'mac': 'aa'}]))
        self.run_handler('post_event', dict(router_id='r', mac='aa', event='DISCONNECTED'))
        self.run_handler('post_event', dict(router_id='r', mac='aa', event='CONNECTED'))
        self.assertEqual(len(self.sent), 4)
        self.assertTrue(self.sent[0][1].startswith('🔴'))
        self.assertTrue(self.sent[1][1].startswith('🟢'))
        self.assertTrue(self.sent[2][1].startswith('🔴'))
        self.assertTrue(self.sent[3][1].startswith('🟢'))

    def test_same_state_and_legacy_notification_cooldown_preserved(self):
        self.seed_device()
        db.mark_device_notified(1, 'aa', 'r', notification_state=1)
        self.assertFalse(db.can_notify_device(1, 'aa', 'r', notification_state=1))
        self.assertTrue(db.can_notify_device(1, 'aa', 'r', notification_state=0))
        self.assertFalse(db.can_notify_device(1, 'aa', 'r'))
        with patch.object(db.time, 'time', return_value=db.time.time() + 301):
            self.assertTrue(db.can_notify_device(1, 'aa', 'r', notification_state=1))

    def test_snapshot_reconnect_commit_failure_keeps_offline_and_marker(self):
        self.seed_device(online=0)
        db.mark_device_notified(1, 'aa', 'r', notification_state=0)
        self.fail_commit = True
        with self.assertRaises(sqlite3.OperationalError):
            self.run_handler('post_devices', dict(router_id='r', devices=[{'mac': 'aa'}]))
        self.assertEqual(self.online(), 0)
        self.assertEqual(self.sent, [])
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT last_notified_state FROM device_notifications').fetchone()[0], 0)
        self.assertTrue(self.connections[-1].rolled_back)
        self.assertTrue(self.connections[-1].closed)

    def test_snapshot_reconnect_send_failure_keeps_committed_state(self):
        self.seed_device(online=0)
        db.mark_device_notified(1, 'aa', 'r', notification_state=0)
        with patch.object(api, 'send_telegram_notification', side_effect=RuntimeError('network')):
            with self.assertRaises(RuntimeError):
                self.run_handler('post_devices', dict(router_id='r', devices=[{'mac': 'aa'}]))
        self.assertEqual(self.online(), 1)
        self.assertTrue(self.connections[-1].closed)
        self.assertFalse(self.connections[-1].rolled_back)

    def test_timeout_without_watch_updates_status_without_notification(self):
        self.seed_device(watch=0)
        with db.db_session() as conn:
            conn.execute("UPDATE devices SET last_seen=0 WHERE mac='aa'")
        self.offline()
        self.assertEqual(self.online(), 0)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.count('device_notifications'), 0)

    def test_timeout_then_snapshot_reconnect_notifies_both_directions(self):
        self.seed_device()
        with db.db_session() as conn:
            conn.execute("UPDATE devices SET last_seen=0 WHERE mac='aa'")
        self.offline()
        self.assertEqual(self.online(), 0)
        self.run_handler('post_devices', dict(router_id='r', devices=[{'mac': 'aa'}]))
        self.assertEqual(self.online(), 1)
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(self.sent[0][1].startswith('🔴'))
        self.assertTrue(self.sent[1][1].startswith('🟢'))

    def test_notification_migration_preserves_legacy_data_and_is_repeatable(self):
        self.seed_device()
        with db.db_session() as conn:
            conn.execute('DROP TABLE device_notifications')
            conn.execute('''CREATE TABLE device_notifications (
                chat_id INTEGER, mac TEXT, router_id TEXT, last_notified_at INTEGER,
                PRIMARY KEY(chat_id, mac, router_id),
                FOREIGN KEY(chat_id) REFERENCES users(chat_id),
                FOREIGN KEY(mac, router_id) REFERENCES devices(mac, router_id))''')
            conn.execute("INSERT INTO device_notifications VALUES (1,'aa','r',?)", (int(db.time.time()),))
        db.init_db()
        db.init_db()
        with closing(sqlite3.connect(self.path)) as conn:
            row = conn.execute('SELECT chat_id, mac, router_id, last_notified_state FROM device_notifications').fetchone()
        self.assertEqual(row, (1, 'aa', 'r', None))
        self.assertFalse(db.can_notify_device(1, 'aa', 'r', notification_state=1))
        db.mark_device_notified(1, 'aa', 'r', notification_state=0)
        self.assertTrue(db.can_notify_device(1, 'aa', 'r', notification_state=1))

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
