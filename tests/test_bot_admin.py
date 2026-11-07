"""Test confirmation and access checks without contacting Telegram."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest


def run(coro):
    try:
        coro.send(None)
    except StopIteration as result:
        return result.value
    finally:
        coro.close()
    raise AssertionError('Test coroutine unexpectedly suspended')


class AdminState:
    def __init__(self):
        self.data = {}
        self.state = None

    async def set_data(self, data):
        self.data = data

    async def get_data(self):
        return self.data

    async def set_state(self, value):
        self.state = value.state

    async def get_state(self):
        return self.state

    async def clear(self):
        self.data = {}
        self.state = None


class AdminActions(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'netwatch_telegram_bot.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        names = {'admin_message_allowed', 'admin_callback_allowed', 'admin_broadcast_preview',
                 'admin_confirm', 'admin_user_action', 'admin_target', 'send_admin_broadcast'}
        handlers = [node for node in tree.body if getattr(node, 'name', None) in names]
        for node in handlers:
            node.decorator_list = []
            for arg in node.args.args:
                arg.annotation = None

        self.replies, self.writes, self.broadcasts = [], [], []
        self.state = AdminState()
        self.private = SimpleNamespace(type='private')
        self.admin = SimpleNamespace(id=1)

        async def broadcast(bot, users, text):
            self.broadcasts.append(([user['chat_id'] for user in users], text))
            return 1, 1

        self.namespace = dict(
            AdminFlow=SimpleNamespace(confirming=SimpleNamespace(state='confirm')),
            _broadcast_lock=asyncio.Lock(), is_admin=lambda chat_id: chat_id == 1,
            get_all_users=lambda: [{'chat_id': 1}, {'chat_id': 2}],
            get_admin_user=lambda chat_id: {'chat_id': chat_id, 'router_count': 2},
            secrets=SimpleNamespace(token_hex=lambda _: 'nonce'),
            admin_keyboard=lambda: None,
            admin_confirm_keyboard=lambda *args: None,
            send_admin_broadcast=broadcast,
            clean_user_data=lambda chat_id: self.writes.append(('clean', chat_id)) or True,
            delete_user=lambda chat_id: self.writes.append(('delete', chat_id)) or True,
        )
        exec(compile(ast.Module(body=handlers, type_ignores=[]), str(path), 'exec'), self.namespace)
        self.send_admin_broadcast = self.namespace['send_admin_broadcast']
        self.namespace['send_admin_broadcast'] = broadcast

    async def reply(self, text=None, **kwargs):
        self.replies.append(text)

    def callback(self, data, user=None, chat=None):
        return SimpleNamespace(
            data=data, from_user=user or self.admin, bot=object(), answer=self.reply,
            message=SimpleNamespace(chat=chat or self.private, edit_text=self.reply),
        )

    def test_broadcast_requires_private_admin_confirmation_and_cannot_be_replayed(self):
        message = SimpleNamespace(from_user=self.admin, chat=self.private,
                                  text='Всем привет & <текст>', reply=self.reply)
        run(self.namespace['admin_broadcast_preview'](message, self.state))
        self.assertEqual(self.broadcasts, [])
        confirm = self.namespace['admin_confirm']
        for callback in (
            self.callback('admin_confirm|old'),
            self.callback('admin_confirm|nonce', user=SimpleNamespace(id=2)),
            self.callback('admin_confirm|nonce', chat=SimpleNamespace(type='group')),
        ):
            run(confirm(callback, self.state))
        self.assertEqual(self.broadcasts, [])
        callback = self.callback('admin_confirm|nonce')
        run(confirm(callback, self.state))
        run(confirm(callback, self.state))
        self.assertEqual(self.broadcasts, [([1, 2], message.text)])
        self.assertTrue(any('Не доставлено: 1' in text for text in self.replies if text))

    def test_destructive_action_is_applied_only_after_confirmation(self):
        run(self.namespace['admin_user_action'](self.callback('admin_action|clean|2'), self.state))
        self.assertEqual(self.writes, [])
        run(self.namespace['admin_confirm'](self.callback('admin_confirm|nonce'), self.state))
        self.assertEqual(self.writes, [('clean', 2)])

    def test_delivery_counts_failed_chats_and_retries_rate_limit_once(self):
        class APIError(Exception):
            pass

        class RateLimit(APIError):
            retry_after = 2

        calls, pauses = [], []

        async def send(chat_id, text, parse_mode):
            calls.append((chat_id, text, parse_mode))
            if chat_id == 2:
                raise APIError()
            if chat_id == 1 and len(calls) == 1:
                raise RateLimit()

        async def sleep(seconds):
            pauses.append(seconds)

        self.namespace.update(TelegramAPIError=APIError, TelegramRetryAfter=RateLimit,
                              asyncio=SimpleNamespace(sleep=sleep))
        users = [{'chat_id': chat_id} for chat_id in (1, 2, 3)]
        self.assertEqual(run(self.send_admin_broadcast(SimpleNamespace(send_message=send), users, '<текст>')),
                         (2, 1))
        self.assertEqual(calls, [(1, '<текст>', None), (1, '<текст>', None),
                                 (2, '<текст>', None), (3, '<текст>', None)])
        self.assertIn(2, pauses)
