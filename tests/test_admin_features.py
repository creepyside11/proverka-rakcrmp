import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from telegram.error import BadRequest
from telegram.ext import ApplicationHandlerStop
import admin_features as features
import captcha
from storage import Store

class NewFeatures(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store=Store(':memory:')
        self.msg=NS(text='/start', reply_text=AsyncMock())
        self.update=NS(effective_chat=NS(type='private'),effective_user=NS(id=5),effective_message=self.msg,callback_query=None)
        self.bot=NS(get_chat_member=AsyncMock(return_value=NS(status='left')),send_message=AsyncMock())
        self.context=NS(bot=self.bot,bot_data={},user_data={},args=[])
    def tearDown(self): self.store.db.close()
    async def test_subscriptions_apply_to_private_not_group_or_admin(self):
        self.store.add_channel(-100123,'Канал','https://t.me/example','example')
        with self.assertRaises(ApplicationHandlerStop):
            await features.subscription_gate(self.update,self.context,self.store,9)
        self.assertEqual(self.store.statistics()['users'],1)
        self.assertIn('подпишитесь',self.msg.reply_text.await_args.args[0])
        self.update.effective_chat.type='supergroup'
        await features.subscription_gate(self.update,self.context,self.store,9)
        self.update.effective_chat.type='private'
        self.update.effective_user.id=9
        await features.subscription_gate(self.update,self.context,self.store,9)
    async def test_subscription_lookup_error_blocks_and_member_passes(self):
        self.store.add_channel(-100123,'Канал','https://t.me/example','example')
        self.bot.get_chat_member.side_effect=BadRequest('no rights')
        with self.assertRaises(ApplicationHandlerStop): await features.subscription_gate(self.update,self.context,self.store,9)
        self.assertIn('Не удалось проверить', self.msg.reply_text.await_args.args[0])
        self.bot.get_chat_member.side_effect=None
        self.bot.get_chat_member.return_value=NS(status='member')
        await features.subscription_gate(self.update,self.context,self.store,9)
    async def test_opt_out_works_without_subscription(self):
        self.store.register_user(5)
        self.store.add_channel(-100123,'Канал','https://t.me/example','example')
        self.msg.text='/stop'
        with self.assertRaises(ApplicationHandlerStop): await features.subscription_gate(self.update,self.context,self.store,9)
        self.assertEqual(self.store.broadcast_users(),[])
    async def test_admin_commands_and_broadcast_confirmation(self):
        self.store.register_user(5)
        self.update.effective_user.id=9
        self.msg.text='/broadcast Привет'
        await features.admin_command(self.update,self.context,self.store,9)
        self.bot.send_message.assert_not_awaited()
        draft=self.context.user_data['broadcast_draft']
        self.assertEqual(draft['text'],'Привет')
        self.assertEqual(self.store.broadcast_users(),[5])
        q=NS(data='broadcast:cancel:'+draft['token'],message=self.msg,answer=AsyncMock(),edit_message_reply_markup=AsyncMock())
        self.update.callback_query=q
        await features.broadcast_callback(self.update,self.context,self.store,9)
        self.assertNotIn('broadcast_draft',self.context.user_data)
        self.bot.send_message.assert_not_awaited()

    async def test_broadcast_sends_only_after_confirmation(self):
        self.store.register_user(5)
        self.update.effective_user.id=9
        self.msg.text='/broadcast Тест'
        await features.admin_command(self.update,self.context,self.store,9)
        draft=self.context.user_data['broadcast_draft']
        q=NS(data='broadcast:send:'+draft['token'],message=self.msg,answer=AsyncMock(),edit_message_reply_markup=AsyncMock())
        self.update.callback_query=q
        tasks=[]
        import asyncio
        self.context.application=NS(create_task=lambda coro: tasks.append(asyncio.create_task(coro)))
        await features.broadcast_callback(self.update,self.context,self.store,9)
        await tasks[0]
        self.bot.send_message.assert_any_await(5,'Тест')
        self.assertFalse(self.context.bot_data['broadcast_active'])
    def test_challenge_types_and_stats(self):
        for kind in (0,1):
            with patch('captcha.secrets.randbelow', side_effect=lambda n: kind if n==2 else 0):
                question,answer,_,buttons=captcha.new_challenge('abcd')
            self.assertEqual(len(buttons),4)
            self.assertIn('Сколько' if kind==0 else 'Нажмите',question)
            self.assertIn('cap:abcd:'+str(answer),[b.callback_data for b in buttons])
        self.store.record_captcha('a','passed'); self.store.record_captcha('a','failed')
        self.assertEqual(self.store.statistics()['passed'],1)
        self.assertEqual(self.store.statistics()['failed'],0)
