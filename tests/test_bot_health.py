"""Exercise the actual /health handler with fake Telegram/DB dependencies."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest


BOT = Path(__file__).resolve().parents[1] / 'netwatch_telegram_bot.py'


class BotHealth(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(BOT.read_text(encoding='utf-8'))
        handler = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'cmd_health')
        handler.decorator_list = []
        for arg in handler.args.args:
            arg.annotation = None
        module = ast.Module(body=[handler], type_ignores=[])
        self.routers = [dict(router_id='r', name='Home')]
        self.health = dict(internet=1, wifi=1, cpu='13.7%', ram='192MiB/485MiB',
                           wifi_clients=6, podkop_status='running', podkop_test=None)
        self.records = dict(r=self.health)
        namespace = dict(upsert_user=lambda *a: None,
                         get_user_routers=lambda chat: self.routers,
                         get_latest_health=lambda router: self.records.get(router))
        exec(compile(module, str(BOT), 'exec'), namespace)
        self.handler = namespace['cmd_health']
        self.replies = []

        async def reply(text, **kwargs):
            self.replies.append(text)

        self.message = SimpleNamespace(from_user=SimpleNamespace(id=1, username='test', first_name='Test'),
                                       reply=reply)

    def render(self):
        asyncio.run(self.handler(self.message))
        return self.replies[-1]

    def test_client_total_next_to_wifi_without_duplicate_count(self):
        text = self.render()
        self.assertIn('Wi-Fi: ✅ (клиентов: 6)', text)
        self.assertEqual(text.count('клиентов:'), 1)
        self.assertNotIn('| Клиенты:', text)
        self.assertIn('CPU: 13.7%', text)
        self.assertIn('RAM: 192MiB/485MiB', text)

    def test_zero_clients_does_not_mean_wifi_disabled(self):
        self.health['wifi_clients'] = 0
        self.assertIn('Wi-Fi: ✅ (клиентов: 0)', self.render())

    def test_unknown_proxy_probe_is_not_reported_as_failure(self):
        text = self.render()
        self.assertIn('Podkop: sing-box работает', text)
        self.assertIn('Прокси: не проверялся', text)
        self.assertNotIn('⚠️', text)

    def test_known_probe_success_and_failure_remain_distinct(self):
        for value, expected in ((1, '✅'), (0, '⚠️')):
            with self.subTest(value=value):
                self.health['podkop_test'] = value
                self.assertIn('Прокси: ' + expected, self.render())

    def test_successful_proxy_latency_is_shown_in_milliseconds(self):
        self.health.update(podkop_test=1, podkop_latency_ms=159)
        self.assertIn('Прокси: ✅ (159 мс)', self.render())

    def test_failed_or_unmeasured_probe_does_not_show_stale_latency(self):
        for state in (0, None):
            with self.subTest(state=state):
                self.health.update(podkop_test=state, podkop_latency_ms=159)
                self.assertNotIn('159 мс', self.render())

    def test_counts_are_separate_for_each_router(self):
        self.routers.append(dict(router_id='r2', name='Other'))
        self.records['r2'] = dict(self.health, wifi_clients=3)
        text = self.render()
        self.assertIn('Home:', text)
        self.assertIn('Other:', text)
        self.assertIn('(клиентов: 6)', text)
        self.assertIn('(клиентов: 3)', text)

    def test_no_health_record(self):
        self.records.clear()
        self.assertIn('нет данных о health-check', self.render())

    def test_no_routers(self):
        self.routers.clear()
        self.assertIn('нет роутеров', self.render())
