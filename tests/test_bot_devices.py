"""Test how /devices selects records and builds a page without Telegram."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest


class Keyboard:
    def __init__(self):
        self.buttons = []

    def button(self, **fields):
        self.buttons.append(SimpleNamespace(**fields))

    def adjust(self, size):
        assert size == 1

    def row(self, *buttons):
        self.buttons.extend(buttons)

    def as_markup(self):
        return self.buttons


def device(mac, router, online, seen):
    return dict(
        mac=mac, router_id=router, router_name=router.upper(), online=online,
        last_seen=seen, custom_name=None, hostname=None, watch_online=1,
    )


class DevicePages(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'netwatch_telegram_bot.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in ('unique_devices', 'render_devices_page')]
        self.rows = []
        self.namespace = dict(
            get_user_devices=lambda _: self.rows, DEVICES_PER_PAGE=10,
            InlineKeyboardBuilder=Keyboard, InlineKeyboardButton=SimpleNamespace,
        )
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), self.namespace)

    def test_roaming_prefers_online_then_most_recent_record(self):
        mac = 'aa:bb:cc:dd:ee:ff'
        self.rows = [
            device(mac, 'old', 1, 10),
            device(mac.upper(), 'stale', 0, 30),
            device(mac, 'current', 1, 20),
        ]
        text, buttons = self.namespace['render_devices_page'](1)
        self.assertIn('CURRENT', text)
        self.assertNotIn('STALE', text)
        self.assertNotIn('OLD', text)
        self.assertEqual([button.callback_data for button in buttons],
                         [f'device_detail|{mac}|current'])

    def test_list_paginates_unique_devices_without_losing_callback_target(self):
        self.rows = [device(f'aa:bb:cc:dd:ee:{i:02x}', 'r', 1, 30 - i) for i in range(11)]
        self.rows.append(device(self.rows[0]['mac'], 'older', 0, 1))
        render = self.namespace['render_devices_page']
        first, first_buttons = render(1)
        second, second_buttons = render(1, 1)
        self.assertIn('Страница 1 из 2', first)
        self.assertIn('Страница 2 из 2', second)
        self.assertEqual(len(first_buttons), 11)
        self.assertEqual(first_buttons[-1].callback_data, 'device_page|1')
        self.assertEqual([button.callback_data for button in second_buttons],
                         ['device_detail|aa:bb:cc:dd:ee:0a|r', 'device_page|0'])
        self.assertEqual(render(1, 99)[0], second)
