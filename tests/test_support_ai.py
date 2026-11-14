import json,time,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer
import httpx
from openai import AsyncOpenAI
import miniapp
import support_ai as support
from storage import Store
import hashlib,hmac
from urllib.parse import urlencode
TOKEN='123:example-token'
def sign(uid=42, now=None):
    data={'auth_date':str(int(time.time() if now is None else now)),'user':json.dumps({'id':uid},separators=(',',':')),'query_id':'abc'}
    check='\n'.join(f'{k}={data[k]}' for k in sorted(data))
    secret=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest()
    data['hash']=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
    return urlencode(data)

class SupportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:');support.init(self.store);support.LAST.clear()
        app=web.Application();app['store']=self.store;app['token']=TOKEN;app['admin_id']=99
        async def info(r):return await support.info(r,miniapp.authorize)
        async def settings(r):return await support.settings_api(r,miniapp.authorize)
        async def tickets(r):return await support.tickets(r,miniapp.authorize)
        async def check(r):return await support.test_api(r,miniapp.authorize)
        async def thread(r):return await support.thread(r,miniapp.authorize)
        async def send(r):return await support.send(r,miniapp.authorize)
        app.router.add_get('/info',info);app.router.add_post('/settings',settings);app.router.add_post('/test',check);app.router.add_get('/tickets',tickets);app.router.add_post('/tickets',tickets)
        app.router.add_get('/tickets/{number}',thread);app.router.add_post('/tickets/{number}/messages',send)
        self.client=TestClient(TestServer(app));await self.client.start_server()
    async def asyncTearDown(self):await self.client.close();self.store.db.close()
    def h(self,uid):return {'X-Telegram-Init-Data':sign(uid)}
    async def configure(self):return await self.client.post('/settings',headers=self.h(99),json={'base_url':'https://example.com/v1','model':'vision-1','api_token':'private-secret','extra_prompt':'Говори кратко'})
    async def test_admin_config_only_and_secret_hidden(self):
        payload={'base_url':'https://example.com/v1','model':'m','api_token':'x'}
        self.assertEqual((await self.client.post('/settings',headers=self.h(42),json=payload)).status,403)
        self.assertEqual((await self.configure()).status,200)
        row=support.config(self.store);self.assertNotIn('private-secret',str(row));self.assertEqual(support.crypt(TOKEN).decrypt(row['token_cipher'].encode()).decode(),'private-secret')
        info=await (await self.client.get('/info',headers=self.h(99))).json();self.assertTrue(info['token_set']);self.assertNotIn('private-secret',str(info))
        info=await (await self.client.get('/info',headers=self.h(42))).json();self.assertNotIn('base_url',info)
        r=await self.client.post('/settings',headers=self.h(99),json={'base_url':'https://example.com/v1','model':'m2','api_token':'','extra_prompt':''});self.assertEqual(r.status,200)
        self.assertEqual(support.crypt(TOKEN).decrypt(support.config(self.store)['token_cipher'].encode()).decode(),'private-secret')
        for base in ('http://example.com','https://127.0.0.1/v1','https://user:pw@example.com','https://localhost'):
            payload={'base_url':base,'model':'m','api_token':'x'}
            self.assertEqual((await self.client.post('/settings',headers=self.h(99),json=payload)).status,400)
    async def test_admin_can_run_safe_diagnostic(self):
        from openai import AuthenticationError
        await self.configure()
        self.assertEqual((await self.client.post('/test',headers=self.h(42),json={})).status,403)
        req=httpx.Request('POST','https://example.com/v1/chat/completions')
        response=httpx.Response(401,request=req)
        error=AuthenticationError('private-secret',response=response,body={'secret':'private-secret'})
        with patch('support_ai.request_model',side_effect=error):
            r=await self.client.post('/test',headers=self.h(99),json={})
        result=await r.json();self.assertEqual(result['code'],'auth');self.assertIn('API Token',result['message']);self.assertNotIn('private-secret',str(result))
        with patch('support_ai.request_model',return_value='OK'):
            r=await self.client.post('/test',headers=self.h(99),json={})
        self.assertTrue((await r.json())['ok'])

    async def test_bearer_token_is_normalized(self):
        r=await self.client.post('/settings',headers=self.h(99),json={'base_url':'https://example.com/v1','model':'m','api_token':'  Bearer actual-key\n ','extra_prompt':''})
        self.assertEqual(r.status,200)
        self.assertEqual(support.crypt(TOKEN).decrypt(support.config(self.store)['token_cipher'].encode()).decode(),'actual-key')
        r=await self.client.post('/settings',headers=self.h(99),json={'base_url':'https://example.com/v1/chat/completions','model':'m','api_token':'x'})
        self.assertEqual(r.status,400)

    async def test_bot_token_rotation_requires_model_token_again(self):
        await self.configure()
        self.assertRaises(ValueError,support.save_config,self.store,'different-bot-token',{'base_url':'https://example.com/v1','model':'m','api_token':'','extra_prompt':''})
        support.save_config(self.store,'different-bot-token',{'base_url':'https://example.com/v1','model':'m','api_token':'new-secret','extra_prompt':''})
        self.assertEqual(support.crypt('different-bot-token').decrypt(support.config(self.store)['token_cipher'].encode()).decode(),'new-secret')

    async def test_owner_admin_and_context(self):
        await self.configure();n=(await (await self.client.post('/tickets',headers=self.h(42),json={})).json())['number'];self.assertEqual(len(n),9)
        self.assertEqual((await self.client.get('/tickets/'+n,headers=self.h(43))).status,404)
        self.assertEqual((await self.client.get('/tickets?all=1',headers=self.h(43))).status,403)
        all_tickets=await (await self.client.get('/tickets?all=1',headers=self.h(99))).json();self.assertEqual(all_tickets['tickets'][0]['number'],n)
        history=[]
        async def model(cfg,token,messages):history.append(messages);self.assertEqual(token,'private-secret');return 'Инструкция: установите Python.'
        with patch('support_ai.request_model',side_effect=model):
            for text in ('Как установить?', 'А Lua?'):
                r=await self.client.post('/tickets/'+n+'/messages',headers=self.h(42),json={'text':text});self.assertEqual(r.status,200);self.assertTrue((await r.json())['ok'])
        self.assertEqual([m['role'] for m in history[1]],['user','assistant','user'])
        self.assertEqual((await self.client.post('/tickets/'+n+'/messages',headers=self.h(99),json={'text':'bad'})).status,404)
        view=await (await self.client.get('/tickets/'+n,headers=self.h(99))).json();self.assertEqual(len(view['messages']),4)
    async def test_old_messages_are_pageable(self):
        n=(await (await self.client.post('/tickets',headers=self.h(42),json={})).json())['number']
        for i in range(205): support.add_message(self.store,n,'user','message '+str(i))
        first=await (await self.client.get('/tickets/'+n,headers=self.h(42))).json()
        self.assertEqual(len(first['messages']),200);self.assertTrue(first['has_more'])
        old=await (await self.client.get('/tickets/'+n,headers=self.h(42),params={'before':first['messages'][0]['message_id']})).json()
        self.assertEqual(len(old['messages']),5);self.assertFalse(old['has_more'])
        ids={m['message_id'] for m in first['messages']};self.assertTrue(all(m['message_id'] not in ids for m in old['messages']))
        bad=await self.client.get('/tickets/'+n,headers=self.h(42),params={'before':'invalid'});self.assertEqual(bad.status,400)

    async def test_failure_and_unconfigured(self):
        n=(await (await self.client.post('/tickets',headers=self.h(42),json={})).json())['number'];endpoint='/tickets/'+n+'/messages'
        self.assertEqual((await self.client.post(endpoint,headers=self.h(42),json={'text':'Hi'})).status,503);await self.configure()
        with patch('support_ai.request_model',side_effect=ValueError('private-secret')):r=await self.client.post(endpoint,headers=self.h(42),json={'text':'Hi'})
        self.assertEqual(r.status,200);self.assertFalse((await r.json())['ok'])
        view=await (await self.client.get('/tickets/'+n,headers=self.h(42))).json();self.assertEqual([x['role'] for x in view['messages']],['user','notice']);self.assertNotIn('private-secret',str(view))
    async def test_real_sdk_format(self):
        seen=[]
        def transport(req):
            seen.append(req);return httpx.Response(200,json={'id':'a','object':'chat.completion','created':1,'model':'m','choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'Проверьте Python.'}}]})
        def factory(**kwargs):return AsyncOpenAI(**kwargs,http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        cfg={'base_url':'https://example.com/v1','model':'m','extra_prompt':'Для теста'};messages=[{'role':'user','body':'Как установить?'}]
        with patch('openai.AsyncOpenAI',factory):result=await support.request_model(cfg,'secret',messages)
        self.assertEqual(result,'Проверьте Python.');self.assertEqual(str(seen[0].url),'https://example.com/v1/chat/completions')
        data=json.loads(seen[0].content);self.assertIn('Lua',data['messages'][0]['content']);self.assertIn('Для теста',data['messages'][0]['content']);self.assertEqual(data['messages'][1]['content'],'Как установить?')

class PersistenceTests(unittest.TestCase):
    def test_ticket_and_messages_survive_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'test.db';s=Store(str(path));support.init(s);n=support.new_ticket(s,42);support.add_message(s,n,'user','Привет');s.db.close()
            s=Store(str(path));support.init(s);self.assertEqual(support.get_ticket(s,n)['owner_id'],42);self.assertEqual(support.get_messages(s,n)[0]['body'],'Привет');s.db.close()
