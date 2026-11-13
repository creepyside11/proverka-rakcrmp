import json,time,unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer
from storage import Store
import captcha,miniapp
from test_miniapp import sign,TOKEN

class WebCaptchaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:'); self.store.init_shop();miniapp.init_tables(self.store)
        self.bot=NS(get_chat_member=AsyncMock(return_value=NS(status='left')),restrict_chat_member=AsyncMock(),ban_chat_member=AsyncMock(),unban_chat_member=AsyncMock(),edit_message_text=AsyncMock())
        self.store.add_channel(-100,'Channel','https://t.me/abcde','abcde')
        a=web.Application();a['store']=self.store;a['token']=TOKEN;a['bot']=self.bot;a['admin_id']=99
        a.router.add_get('/api/captcha',miniapp.captcha_get);a.router.add_post('/api/captcha/answer',miniapp.captcha_answer)
        a.router.add_post('/api/payment',miniapp.create_external_payment);a.router.add_post('/api/payment/check',miniapp.check_external_payment)
        self.c=TestClient(TestServer(a));await self.c.start_server()
        self.h={'X-Telegram-Init-Data':sign()};self.token='0123456789abcdef'
        self.options=[{'label':'1','value':1},{'label':'2','value':2}]
        self.store.create_captcha(-200,42,self.token,2,time.time()+600,0);self.store.captcha_update(-200,42,state='pending',message_id=123)
        self.store.create_web_challenge(self.token,42,'Выберите',json.dumps(self.options),2,time.time()+600)
    async def asyncTearDown(self): await self.c.close();self.store.db.close()
    async def test_captcha_does_not_require_subscription_and_checks_identity(self):
        r=await self.c.get('/api/captcha?token='+self.token,headers=self.h);self.assertEqual(r.status,200)
        self.bot.get_chat_member.assert_not_awaited()
        self.assertNotIn('correct',await r.json())
        r=await self.c.get('/api/captcha?token='+self.token,headers={'X-Telegram-Init-Data':sign(uid=43)});self.assertEqual(r.status,404)
        r=await self.c.post('/api/captcha/answer',json={'token':self.token,'value':2},headers={'X-Telegram-Init-Data':sign(uid=43)});self.assertEqual(r.status,404)
        self.assertEqual(self.store.captcha(-200,42)['state'],'pending')
    async def test_wrong_then_correct_and_replay(self):
        r=await self.c.post('/api/captcha/answer',json={'token':self.token,'value':1},headers=self.h);self.assertEqual((await r.json())['attempts_left'],2)
        r=await self.c.post('/api/captcha/answer',json={'token':self.token,'value':2},headers=self.h);self.assertEqual((await r.json())['result'],'passed')
        self.bot.restrict_chat_member.assert_awaited_once();self.assertIsNone(self.store.captcha(-200,42))
        r=await self.c.post('/api/captcha/answer',json={'token':self.token,'value':2},headers=self.h);self.assertEqual(r.status,404)
    async def test_three_wrong_attempts_remove_user(self):
        for i in range(3):
            r=await self.c.post('/api/captcha/answer',json={'token':self.token,'value':1},headers=self.h)
            self.assertEqual((await r.json())['result'],'failed' if i==2 else 'retry')
        self.bot.ban_chat_member.assert_awaited_once_with(-200,42)
        self.bot.unban_chat_member.assert_awaited_once()
        self.assertEqual(self.store.statistics()['failed'],1)
    async def test_expired_challenge_not_accepted(self):
        self.store.db.execute('UPDATE web_challenges SET expires=? WHERE token=?',(time.time()-1,self.token));self.store.db.commit()
        r=await self.c.post('/api/captcha/answer',json={'token':self.token,'value':2},headers=self.h)
        self.assertEqual(r.status,404);self.bot.restrict_chat_member.assert_not_awaited()
    async def test_admin_demo_does_not_touch_group(self):
        token='abcdef0123456789';self.store.create_web_challenge(token,99,'?',json.dumps(self.options),2,time.time()+60,demo=1)
        r=await self.c.post('/api/captcha/answer',json={'token':token,'value':2},headers={'X-Telegram-Init-Data':sign(uid=99)})
        self.assertEqual((await r.json())['result'],'passed');self.bot.restrict_chat_member.assert_not_awaited()
    async def test_external_payment_verifies_provider_and_order_owner(self):
        self.bot.get_chat_member.return_value=NS(status='member')
        self.store.product_add({'id':'p1','name':'Test','category':'bots','description':'D','price_kopecks':20000,'file_id':'file','filename':'file.lua','instruction':'I'})
        with patch('miniapp.payments.create',new=AsyncMock(return_value=('provider1','https://pay.example'))):
            r=await self.c.post('/api/payment',json={'product_id':'p1','provider':'card'},headers=self.h);self.assertEqual(r.status,200);oid=(await r.json())['order_id']
        self.assertEqual(self.store.order(oid)['buyer'],42)
        with patch('miniapp.shop.check_order',new=AsyncMock(return_value='not_found')) as check:
            r=await self.c.post('/api/payment/check',json={'order_id':oid},headers={'X-Telegram-Init-Data':sign(uid=43)});self.assertEqual((await r.json())['state'],'not_found');self.assertEqual(check.await_args.args[-1],43)
