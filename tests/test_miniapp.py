import hashlib,hmac,json,time,unittest
from urllib.parse import urlencode
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from aiohttp.test_utils import TestClient,TestServer
from aiohttp import web
import miniapp
from storage import Store

TOKEN='123:example-token'
def sign(uid=42, now=None):
    data={'auth_date':str(int(time.time() if now is None else now)),'user':json.dumps({'id':uid},separators=(',',':')),'query_id':'abc'}
    check='\n'.join(f'{k}={data[k]}' for k in sorted(data))
    secret=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest()
    data['hash']=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
    return urlencode(data)

class MiniAppTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:');self.store.init_shop();miniapp.init_tables(self.store)
        self.store.product_add({'id':'p1','name':'Script','category':'scripts','description':'Description','price_kopecks':10000,'file_id':'file','filename':'file.lua','instruction':'Install'})
        miniapp.set_price(self.store,'p1',30)
        self.bot=NS(create_invoice_link=AsyncMock(return_value='https://t.me/invoice'),get_chat_member=AsyncMock(return_value=NS(status='member')),send_document=AsyncMock(),send_message=AsyncMock())
        app=web.Application();app['store']=self.store;app['token']=TOKEN;app['bot']=self.bot;app['admin_id']=99
        app.router.add_get('/api/catalog',miniapp.catalog);app.router.add_post('/api/invoice',miniapp.create_invoice)
        self.client=TestClient(TestServer(app));await self.client.start_server()
    async def asyncTearDown(self): await self.client.close();self.store.db.close()
    def test_signature(self):
        self.assertEqual(miniapp.validate_init_data(sign(),TOKEN),42)
        with self.assertRaises(ValueError): miniapp.validate_init_data(sign()+'x',TOKEN)
        with self.assertRaises(ValueError): miniapp.validate_init_data(sign(now=time.time()-7200),TOKEN)
    async def test_catalog_and_auth(self):
        r=await self.client.get('/api/catalog');self.assertEqual(r.status,401)
        r=await self.client.get('/api/catalog',headers={'X-Telegram-Init-Data':sign()});self.assertEqual(r.status,200)
        p=(await r.json())['products'][0];self.assertEqual(p['stars'],30);self.assertNotIn('file_id',p)
        self.store.add_channel(-100111,'Channel','https://t.me/abcde','abcde')
        self.bot.get_chat_member.return_value=NS(status='left')
        r=await self.client.get('/api/catalog',headers={'X-Telegram-Init-Data':sign()});self.assertEqual(r.status,403)
    async def test_invoice_and_paid_idempotence(self):
        r=await self.client.post('/api/invoice',json={'product_id':'p1'},headers={'X-Telegram-Init-Data':sign()});self.assertEqual(r.status,200)
        oid=self.bot.create_invoice_link.await_args.kwargs['payload'][6:]
        self.assertEqual(miniapp.get_order(self.store,oid)['stars'],30)
        q=NS(invoice_payload='stars:'+oid,from_user=NS(id=42),currency='XTR',total_amount=30,answer=AsyncMock())
        await miniapp.precheckout(NS(pre_checkout_query=q),None,self.store);self.assertTrue(q.answer.await_args.kwargs['ok'])
        await miniapp.precheckout(NS(pre_checkout_query=q),None,self.store);self.assertFalse(q.answer.await_args.kwargs['ok'])
        payment=NS(invoice_payload='stars:'+oid,currency='XTR',total_amount=30,telegram_payment_charge_id='charge')
        update=NS(effective_user=NS(id=42),effective_message=NS(successful_payment=payment))
        await miniapp.successful_payment(update,NS(bot=self.bot),self.store)
        await miniapp.successful_payment(update,NS(bot=self.bot),self.store)
        self.assertEqual(self.bot.send_document.await_count,1);self.assertEqual(miniapp.get_order(self.store,oid)['state'],'delivered')
    async def test_wrong_buyer_rejected(self):
        r=await self.client.post('/api/invoice',json={'product_id':'p1'},headers={'X-Telegram-Init-Data':sign()})
        oid=self.bot.create_invoice_link.await_args.kwargs['payload'][6:]
        q=NS(invoice_payload='stars:'+oid,from_user=NS(id=43),currency='XTR',total_amount=30,answer=AsyncMock())
        await miniapp.precheckout(NS(pre_checkout_query=q),None,self.store)
        self.assertFalse(q.answer.await_args.kwargs['ok'])
