import tempfile
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from telegram import Update
from telegram.error import BadRequest
import captcha
from storage import Store

class CaptchaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.api = NS(username='TestBot', restrict_chat_member=AsyncMock(), ban_chat_member=AsyncMock(),
                      unban_chat_member=AsyncMock(), edit_message_text=AsyncMock(),
                      send_message=AsyncMock(return_value=NS(message_id=42)), _post=AsyncMock(return_value={'message_id':99}))
        self.ctx = NS(bot=self.api)
        self.user = NS(id=5,is_bot=False,full_name='Участник')

    def tearDown(self): self.store.db.close()

    async def join(self):
        old = NS(status='left',user=self.user)
        new = NS(status='member',user=self.user)
        await captcha.member_update(NS(chat_member=NS(old_chat_member=old,new_chat_member=new,chat=NS(id=-100,type='supergroup'))),self.ctx,self.store,123)
        return self.store.captcha(-100,5)

    def callback(self,row,value,user=5):
        q = NS(data=f"cap:{row['token']}:{value}",answer=AsyncMock(),message=NS(chat=NS(id=-100),message_id=42))
        return NS(callback_query=q,effective_user=NS(id=user))

    async def test_join_success_and_other_user(self):
        row=await self.join()
        self.assertEqual(row['state'],'pending')
        self.api.restrict_chat_member.assert_awaited_once()
        other=self.callback(row,row['correct'],99)
        await captcha.answer(other,self.ctx,self.store)
        self.assertIsNotNone(self.store.captcha(-100,5))
        await captcha.answer(self.callback(row,row['correct']),self.ctx,self.store)
        self.assertIsNone(self.store.captcha(-100,5))
        self.assertEqual(self.api.restrict_chat_member.await_count,2)
        self.api.ban_chat_member.assert_not_awaited()

    async def test_three_mistakes_kick_and_unban(self):
        row=await self.join()
        for _ in range(2):
            await captcha.answer(self.callback(row,row['correct']+1),self.ctx,self.store)
            self.assertIsNotNone(self.store.captcha(-100,5))
        await captcha.answer(self.callback(row,row['correct']+1),self.ctx,self.store)
        self.api.ban_chat_member.assert_awaited_once_with(-100,5)
        self.api.unban_chat_member.assert_awaited_once()
        self.assertIsNone(self.store.captcha(-100,5))

    async def test_timeout_recovery_and_retry_unban(self):
        row=await self.join()
        self.store.db.execute('UPDATE captchas SET expires=?',(time.time()-1,)); self.store.db.commit()
        self.api.unban_chat_member.side_effect = [BadRequest('failed'),None]
        await captcha.recover(self.api,self.store)
        self.assertEqual(self.store.captcha(-100,5)['state'],'banned')
        await captcha.recover(self.api,self.store)
        self.assertIsNone(self.store.captcha(-100,5))
        self.api.ban_chat_member.assert_awaited_once()

    async def test_interrupted_setup_does_not_kick(self):
        self.store.create_captcha(-100,5,'abc',3,time.time()-100,1)
        await captcha.recover(self.api,self.store)
        self.api.ban_chat_member.assert_not_awaited()
        self.assertIsNone(self.store.captcha(-100,5))

    async def test_widget_deep_link_and_raw_update(self):
        row=await self.join()
        msg=NS(reply_text=AsyncMock())
        await captcha.open_widget(NS(effective_user=self.user,effective_message=msg),self.ctx,self.store,row['token'])
        self.assertEqual(self.store.captcha(-100,5)['widget_message_id'],99)
        self.assertEqual(self.api._post.await_args.args[0],'sendWidget')
        raw={'update_id':1,'widget_query':{'id':'q1','from':{'id':5,'is_bot':False,'first_name':'T'}, 'chat':{'id':5,'type':'private'}, 'message_id':99, 'type':{'type':'data'},'data':row['token']+':'+str(row['correct'])}}
        update=Update.de_json(raw,None)
        await captcha.widget_query(update,self.ctx,self.store)
        self.assertIsNone(self.store.captcha(-100,5))
        self.assertEqual(self.api._post.await_args.args[0],'answerWidgetQuery')

    async def test_widget_wrong_identity_does_not_consume_attempt(self):
        row=await self.join()
        self.store.captcha_update(-100,5,widget_message_id=99)
        update=NS(api_kwargs={'widget_query':{'id':'q1','from':{'id':9},'chat':{'id':9,'type':'private'},'message_id':99,'data':row['token']+':0'}})
        await captcha.widget_query(update,self.ctx,self.store)
        self.assertEqual(self.store.captcha(-100,5)['attempts'],0)

    async def test_missing_admin_rights_no_stuck_record(self):
        self.api.restrict_chat_member.side_effect=BadRequest('missing rights')
        await self.join()
        self.assertIsNone(self.store.captcha(-100,5))
        self.assertIn('право бота',self.api.send_message.await_args.kwargs['text'])
