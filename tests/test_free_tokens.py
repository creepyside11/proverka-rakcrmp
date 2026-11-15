import asyncio
import json
import unittest
from unittest.mock import AsyncMock,patch
from types import SimpleNamespace
from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer
from storage import Store
import ai_gateway,ai_models,free_tokens,miniapp,script_creator,support_ai

TOKEN='test-bot-token'
class FreeTokensTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:');self.store.init_shop();miniapp.init_tables(self.store)
        cipher=support_ai.crypt(TOKEN).encrypt(b'provider').decode()
        for model in ai_models.MODELS:ai_models.save(self.store,TOKEN,model,'https://example.com/v1','model',cipher,1)
        app=web.Application();app['store']=self.store;app['token']=TOKEN
        async def auth(req,require_subscription=True):return int(req.headers['uid'])
        async def endpoint(req):return await free_tokens.endpoint(req,auth)
        app.router.add_get('/free',endpoint);app.router.add_post('/free',endpoint)
        app.router.add_post('/v1/chat/completions',ai_gateway.completions)
        app.router.add_get('/v1/models',ai_gateway.models);app.router.add_get('/v1/balance',ai_gateway.balance)
        self.client=TestClient(TestServer(app));await self.client.start_server()
    async def asyncTearDown(self):
        await self.client.close();self.store.db.close()
    async def test_quotas_cooldown_per_model_and_user(self):
        for model,amount in free_tokens.QUOTAS.items():
            result=free_tokens.claim(self.store,TOKEN,42,model,now=100000);self.assertEqual(result['amount'],amount)
            with self.assertRaises(web.HTTPTooManyRequests):free_tokens.claim(self.store,TOKEN,42,model,now=186399)
            self.assertEqual(free_tokens.claim(self.store,TOKEN,43,model,now=100000)['amount'],amount)
            free_tokens.claim(self.store,TOKEN,42,model,now=186400)
            row=self.store.db.execute('SELECT balance FROM ai_keys WHERE order_ref=?',(free_tokens.ref(42,model),)).fetchone();self.assertEqual(row['balance'],amount*2)
        self.assertEqual(ai_gateway.keys(self.store,TOKEN,42),[])
    async def test_free_balance_is_never_an_external_api_key(self):
        free_tokens.claim(self.store,TOKEN,42,'glm');key=ai_gateway.key_for(TOKEN,free_tokens.ref(42,'glm'));headers={'Authorization':'Bearer '+key}
        for path in ('/v1/models','/v1/balance'):
            self.assertEqual((await self.client.get(path,headers=headers)).status,401)
        fake=AsyncMock(return_value={'choices':[{'message':{'content':'Hello'}}],'usage':{'total_tokens':200}})
        with patch.object(ai_gateway,'upstream',fake):
            r=await self.client.post('/v1/chat/completions',headers=headers,json={'model':'glm','messages':[{'role':'user','content':'hello'}]});self.assertEqual(r.status,401);fake.assert_not_awaited()
            result=await script_creator.paid_model(self.store,TOKEN,42,'glm',[{'role':'user','body':'hello'}]);self.assertEqual(result,'Hello')
        state=free_tokens.state(self.store,42);glm=next(x for x in state if x['model']=='glm');self.assertEqual(glm['balance'],3_999_800)
        self.assertNotIn(key,json.dumps(state));self.assertNotIn('api_token',json.dumps(state))
    async def test_public_claim_endpoint_identity_and_duplicate(self):
        first=await self.client.post('/free',headers={'uid':'42'},json={'model':'glm'});self.assertEqual(first.status,200);self.assertEqual((await first.json())['amount'],4_000_000)
        duplicate=await self.client.post('/free',headers={'uid':'42'},json={'model':'glm'});self.assertEqual(duplicate.status,429)
        other=await self.client.get('/free',headers={'uid':'43'});models=(await other.json())['models'];self.assertTrue(all(x['balance']==0 for x in models))
        invalid=await self.client.post('/free',headers={'uid':'42'},json={'model':'unknown'});self.assertEqual(invalid.status,400)

    async def test_new_purchases_blocked_but_other_products_unchanged(self):
        self.store.product_add({'id':'ai_glm','category':'ai','name':'GLM','description':'AI','price_kopecks':100,'file_id':'ai:glm','filename':'ai','instruction':''})
        miniapp.set_price(self.store,'ai_glm',10)
        self.store.product_add({'id':'script1','category':'scripts','name':'Script','description':'Lua','price_kopecks':100,'file_id':'doc','filename':'a.lua','instruction':'run'})
        miniapp.set_price(self.store,'script1',10)
        app=web.Application();app['store']=self.store;app['token']=TOKEN;app['bot']=SimpleNamespace(create_invoice_link=AsyncMock(return_value='https://t.me/example'))
        app.router.add_get('/catalog',miniapp.catalog);app.router.add_post('/invoice',miniapp.create_invoice);app.router.add_post('/payment',miniapp.create_external_payment)
        client=TestClient(TestServer(app));await client.start_server()
        async def auth(req,require_subscription=True):return 42
        try:
            with patch.object(miniapp,'authorize',auth),patch('payments.create',AsyncMock()) as payment:
                result=await (await client.get('/catalog')).json();self.assertEqual([x['id'] for x in result['products']],['script1'])
                result=await client.post('/invoice',json={'product_id':'ai_glm'});self.assertEqual(result.status,404)
                result=await client.post('/payment',json={'product_id':'ai_glm','provider':'sbp'});self.assertEqual(result.status,404);payment.assert_not_awaited()
                result=await client.post('/invoice',json={'product_id':'script1'});self.assertEqual(result.status,200)
        finally:await client.close()

    async def test_claim_endpoint_requests_subscription_check(self):
        calls=[]
        async def auth(req,require_subscription=True):calls.append(require_subscription);return 42
        req=SimpleNamespace(app={'store':self.store,'token':TOKEN},method='GET')
        await free_tokens.endpoint(req,auth);self.assertEqual(calls,[True])

    async def test_free_is_used_first_and_legacy_paid_key_still_works(self):
        order={'id':'c'*24,'buyer':42,'file_id':'ai:glm'}
        paid=ai_gateway.issue(self.store,TOKEN,'stars',order);free_tokens.claim(self.store,TOKEN,42,'glm')
        answer={'choices':[{'message':{'content':'OK'}}],'usage':{'total_tokens':100}}
        with patch.object(ai_gateway,'upstream',AsyncMock(return_value=answer)):
            await script_creator.paid_model(self.store,TOKEN,42,'glm',[{'role':'user','body':'hello'}])
            paid_balance=await self.client.get('/v1/balance',headers={'Authorization':'Bearer '+paid})
            self.assertEqual(paid_balance.status,200);self.assertEqual((await paid_balance.json())['balance_tokens'],1_000_000)
        free=next(x for x in free_tokens.state(self.store,42) if x['model']=='glm')
        self.assertEqual(free['balance'],3_999_900)
        self.assertEqual(len(ai_gateway.keys(self.store,TOKEN,42)),1)

class FreeConcurrencyTest(unittest.TestCase):
    def test_simultaneous_claims_cannot_double_grant(self):
        import tempfile,os,threading,concurrent.futures
        with tempfile.TemporaryDirectory() as temp:
            path=os.path.join(temp,'claim.sqlite3');store=Store(path);store.init_shop();miniapp.init_tables(store);store.db.close()
            barrier=threading.Barrier(2)
            def claim_once():
                local=Store(path)
                try:
                    barrier.wait(timeout=5)
                    try:free_tokens.claim(local,TOKEN,42,'glm',now=100000);return 'ok'
                    except web.HTTPTooManyRequests:return 'cooldown'
                finally:local.db.close()
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:claim_once(),range(2)))
            self.assertEqual(sorted(results),['cooldown','ok'])
            local=Store(path)
            try:self.assertEqual(local.db.execute('SELECT balance FROM ai_keys WHERE order_ref=?',(free_tokens.ref(42,'glm'),)).fetchone()['balance'],4_000_000)
            finally:local.db.close()
