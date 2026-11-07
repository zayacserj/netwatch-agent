"""Regression coverage for the history labels shown in Telegram."""
import ast
from pathlib import Path
import unittest


class HistoryLabels(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'netwatch_telegram_bot.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        formatter = next(node for node in tree.body if getattr(node, 'name', '') == 'format_history_event')
        self.namespace = {'fmt_time': lambda _: '12:00'}
        exec(compile(ast.Module(body=[formatter], type_ignores=[]), str(path), 'exec'), self.namespace)

    def test_legacy_disconnect_is_offline(self):
        row = dict(event='AP-STA-DISCONNECTED', timestamp=1, router_name='WR3000', router_id='r',
                   mac='aa:bb:cc:dd:ee:ff', custom_name='Телефон', known_hostname='iPhone')
        self.assertEqual(self.namespace['format_history_event'](row),
                         '12:00 · 🔴 Устройство оффлайн · Телефон (aa:bb:cc:dd:ee:ff) · WR3000')

    def test_router_transition_has_no_device_mac(self):
        row = dict(event='router_online', timestamp=1, router_name='TR3000', router_id='r', mac=None)
        self.assertEqual(self.namespace['format_history_event'](row),
                         '12:00 · 🟢 Роутер онлайн · TR3000')
