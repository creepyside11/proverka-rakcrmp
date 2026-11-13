import io
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from telegram.error import BadRequest
import bot
from storage import Store

class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_store = bot.store
        bot.store = Store(':memory:')
        bot.ADMIN_ID = 123
        self.context = NS(bot=NS(send_message=AsyncMock(), get_file=AsyncMock()))

    def tearDown(self):
        bot.store.db.close()
        bot.store = self.old_store

    def message(self, doc=None, text='', user=456, topic=10):
        msg = NS(chat=NS(id=-100, type='supergroup'), message_thread_id=topic,
                 message_id=42, text=text, caption=None, entities=[], caption_entities=[],
                 document=doc, effective_attachment=doc, delete=AsyncMock(), reply_text=AsyncMock())
        return NS(effective_message=msg, effective_user=NS(id=user)), msg

    async def test_topic_rule_and_delete_failure(self):
        bot.store.toggle(-100,10,'luaonly',True)
        update, msg = self.message(text='hello')
        msg.delete.side_effect = BadRequest('no rights')
        await bot.moderate(update,self.context)
        msg.delete.assert_awaited_once()
        self.assertIn('Не удалось удалить', self.context.bot.send_message.await_args.kwargs['text'])
        update2, msg2 = self.message(text='hello',topic=11)
        await bot.moderate(update2,self.context)
        msg2.delete.assert_not_awaited()

    async def test_safe_suspicious_and_unknown(self):
        doc = NS(file_name='test.lua',file_size=100,file_id='x')
        update,msg = self.message(doc=doc)
        remote = NS(download_to_memory=AsyncMock(side_effect=lambda out: out.write(b'print(1)')))
        self.context.bot.get_file.return_value = remote
        await bot.moderate(update,self.context)
        self.assertIn('не гарантия',msg.reply_text.await_args.args[0])
        remote.download_to_memory.side_effect = lambda out: out.write(b'os.execute("id")')
        await bot.moderate(update,self.context)
        msg.delete.assert_awaited_once()
        self.assertIn('опасные признаки',self.context.bot.send_message.await_args.kwargs['text'])
        msg.delete.reset_mock()
        doc.file_name = 'test.luac'
        remote.download_to_memory.side_effect = lambda out: out.write(b'\x1bLua\0')
        await bot.moderate(update,self.context)
        msg.delete.assert_awaited_once()
        bot.store.toggle(-100,10,'unknown_delete',False)
        msg.delete.reset_mock()
        await bot.moderate(update,self.context)
        msg.delete.assert_not_awaited()
        self.assertIn('не удалось надёжно проверить',msg.reply_text.await_args.args[0])

    async def test_owner_files_are_scanned(self):
        doc = NS(file_name='test.lua',file_size=9999999,file_id='x')
        update,msg = self.message(doc=doc,user=123)
        await bot.moderate(update,self.context)
        msg.delete.assert_awaited_once()

    async def test_panel_cannot_be_used_by_other_user(self):
        query = NS(answer=AsyncMock(), data='toggle:ads', message=None)
        await bot.button(NS(callback_query=query,effective_user=NS(id=456)),self.context)
        query.answer.assert_awaited_once()
        self.assertEqual(bot.store.get(-100,10)['ads'],0)

if __name__ == '__main__': unittest.main()
