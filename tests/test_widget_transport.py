"""Exercise the real python-telegram-bot request and update serialization."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram import Bot, Update
from telegram.request import BaseRequest

import captcha


class FakeTelegram(BaseRequest):
    def __init__(self):
        self.calls = []

    @property
    def read_timeout(self):
        return 5.0

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        endpoint = url.rsplit('/', 1)[-1]
        self.calls.append((endpoint, request_data.parameters))
        result = {'message_id': 987} if endpoint == 'sendWidget' else True
        return 200, json.dumps({'ok': True, 'result': result}).encode()


class WidgetTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_widget_roundtrip_uses_documented_api(self):
        transport = FakeTelegram()
        bot = Bot('123456:fake-token', request=transport)
        context = SimpleNamespace(bot=bot, bot_data={})
        admin = SimpleNamespace(id=42)
        await captcha.test_widget(SimpleNamespace(effective_user=admin,
                                  effective_message=SimpleNamespace(reply_text=AsyncMock())), context)
        method, params = transport.calls[0]
        self.assertEqual(method, 'sendWidget')
        self.assertEqual(params['chat_id'], 42)
        self.assertTrue(params['allow_queries'])
        self.assertIn('TelegramWidget.request', params['html'])
        demo = context.bot_data['widget_demo']
        update = Update.de_json({'update_id': 1, 'widget_query': {
            'id': 'query1', 'from': {'id': 42, 'is_bot': False, 'first_name': 'Admin'},
            'chat': {'id': 42, 'type': 'private'}, 'message_id': 987,
            'data': f"demo:{demo['token']}:{demo['answer']}"}}, bot)
        await captcha.widget_query(update, context, None)
        method, params = transport.calls[1]
        self.assertEqual(method, 'answerWidgetQuery')
        self.assertEqual(params['widget_query_id'], 'query1')
        self.assertIn('Верно', params['data'])
