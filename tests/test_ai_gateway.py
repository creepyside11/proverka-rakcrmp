import asyncio
import io
import os
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer
from PIL import Image
import ai_gateway as gateway
import ai_models
import miniapp
import shop
import support_ai
from storage import Store

TOKEN='123:example-token'
class AITests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.store=Store(':memory:');self.store.init_shop();miniapp.init_tables(self.store)
  cipher=support_ai.crypt(TOKEN).encrypt(b'provider-secret').decode()
  ai_models.save(self.store,TOKEN,'glm','https://example.com/v1','provider-id',cipher,10000)
  self.order={'id':'a'*24,'buyer':42,'file_id':'ai:glm','state':'paid','name':'GLM','filename':'ai_tokens','instruction':'','product_id':'ai_glm'}
  self.key=gateway.issue(self.store,TOKEN,'stars',self.order)
  app=web.Application();app['store']=self.store;app['token']=TOKEN
  app.router.add_post('/v1/chat/completions',gateway.completions);app.router.add_get('/v1/balance',gateway.balance);app.router.add_get('/v1/models',gateway.models)
  self.client=TestClient(TestServer(app));await self.client.start_server()
 async def asyncTearDown(self):await self.client.close();self.store.db.close()
 def auth(self,key=None):return {'Authorization':'Bearer '+(key or self.key)}
 async def test_issue_is_idempotent_and_private(self):
  gateway.issue(self.store,TOKEN,'stars',self.order);self.assertEqual(gateway.keys(self.store,TOKEN,42)[0]['balance'],1000000)
  self.assertEqual(gateway.keys(self.store,TOKEN,43),[])
  r=await self.client.get('/v1/balance',headers=self.auth('rak-'+'0'*64));self.assertEqual(r.status,401)
 async def test_text_usage_charged_and_model_scoped(self):
  data={'model':'glm','messages':[{'role':'user','content':'привет'}]}
  fake=AsyncMock(return_value={'id':'x','model':'provider-secret-id','choices':[],'usage':{'total_tokens':123}})
  with patch.object(gateway,'upstream',fake):
   r=await self.client.post('/v1/chat/completions',headers=self.auth(),json=data)
   self.assertEqual(r.status,200);self.assertEqual((await r.json())['model'],'glm');self.assertEqual(fake.await_args.args[1],'provider-secret')
  self.assertEqual(gateway.keys(self.store,TOKEN,42)[0]['balance'],999877)
  data['model']='gpt_astra';r=await self.client.post('/v1/chat/completions',headers=self.auth(),json=data);self.assertEqual(r.status,400)
 async def test_provider_ambiguous_failure_locks_and_manual_settle_is_idempotent(self):
  data={'model':'glm','messages':[{'role':'user','content':'hi'}]}
  with patch.object(gateway,'upstream',AsyncMock(side_effect=TimeoutError)):
   r=await self.client.post('/v1/chat/completions',headers=self.auth(),json=data);self.assertEqual(r.status,502)
  r=await self.client.post('/v1/chat/completions',headers=self.auth(),json=data);self.assertEqual(r.status,409)
  record=self.store.db.execute('SELECT id FROM ai_usage').fetchone();self.assertTrue(gateway.settle(self.store,record['id'],100));self.assertFalse(gateway.settle(self.store,record['id'],100))
  self.assertEqual(gateway.keys(self.store,TOKEN,42)[0]['balance'],999900)
 async def test_parallel_requests_do_not_share_key(self):
  entered=asyncio.Event();release=asyncio.Event()
  async def slow(*args):
   entered.set();await release.wait();return {'choices':[],'usage':{'total_tokens':10}}
  data={'model':'glm','messages':[{'role':'user','content':'hi'}]}
  with patch.object(gateway,'upstream',slow):
   first=asyncio.create_task(self.client.post('/v1/chat/completions',headers=self.auth(),json=data))
   await entered.wait()
   second=await self.client.post('/v1/chat/completions',headers=self.auth(),json=data)
   self.assertEqual(second.status,409);release.set();self.assertEqual((await first).status,200)
  self.assertEqual(gateway.keys(self.store,TOKEN,42)[0]['balance'],999990)
 async def test_request_does_not_start_without_reservation(self):
  self.store.db.execute('UPDATE ai_keys SET balance=10');self.store.db.commit()
  with patch.object(gateway,'upstream',AsyncMock()) as upstream:
   r=await self.client.post('/v1/chat/completions',headers=self.auth(),json={'model':'glm','messages':[{'role':'user','content':'hello'}]})
   self.assertEqual(r.status,409);upstream.assert_not_awaited()
 async def test_photo_passed_and_streaming_rejected(self):
  import base64
  out=io.BytesIO();Image.new('RGB',(4,4)).save(out,format='PNG')
  img='data:image/png;base64,'+base64.b64encode(out.getvalue()).decode()
  data={'model':'glm','messages':[{'role':'user','content':[{'type':'text','text':'что это?'},{'type':'image_url','image_url':{'url':img}}]}]}
  with patch.object(gateway,'upstream',AsyncMock(return_value={'choices':[],'usage':{'total_tokens':100}})) as fn:
   r=await self.client.post('/v1/chat/completions',headers=self.auth(),json=data);self.assertEqual(r.status,200)
   self.assertEqual(fn.await_args.args[2][0]['content'][1]['image_url']['url'],img)
  data['stream']=True;r=await self.client.post('/v1/chat/completions',headers=self.auth(),json=data);self.assertEqual(r.status,400)
 async def test_stars_delivery_creates_once(self):
  self.store.db.execute("INSERT INTO star_orders(id,buyer,product_id,stars,name,file_id,filename,instruction,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",('a'*24,42,'ai_glm',20,'GLM','ai:glm','ai_tokens','','paid',1));self.store.db.commit()
  bot=NS(send_document=AsyncMock(),send_message=AsyncMock())
  with patch.dict(os.environ,{'BOT_TOKEN':TOKEN}):
   await miniapp.deliver_order(bot,self.store,'a'*24);await miniapp.deliver_order(bot,self.store,'a'*24)
  self.assertEqual(bot.send_message.await_count,1);self.assertEqual(len(gateway.keys(self.store,TOKEN,42)),1)

class CreatorPaidTests(unittest.IsolatedAsyncioTestCase):
 async def test_paid_chat_charges_buyer_only(self):
  from tests.test_miniapp import sign
  import script_creator
  store=Store(':memory:');store.init_shop();miniapp.init_tables(store)
  cfg=support_ai.crypt(TOKEN).encrypt(b'secret').decode()
  ai_models.save(store,TOKEN,'glm','https://example.com/v1','provider',cfg,10000)
  order={'id':'b'*24,'buyer':42,'file_id':'ai:glm'};gateway.issue(store,TOKEN,'stars',order)
  app=web.Application();app['store']=store;app['token']=TOKEN;app['admin_id']=99
  app.router.add_post('/api/creator/messages',miniapp.creator_messages)
  client=TestClient(TestServer(app));await client.start_server()
  fake={'choices':[{'message':{'content':'```lua\nprint(1)\n```'}}],'usage':{'total_tokens':200}}
  with patch.object(gateway,'upstream',AsyncMock(return_value=fake)),patch.object(script_creator,'check_syntax',AsyncMock(return_value={'status':'ok','message':'syntax only'})):
   r=await client.post('/api/creator/messages',headers={'X-Telegram-Init-Data':sign(42)},json={'text':'Сделай Lua','model':'glm'})
   self.assertEqual(r.status,200);self.assertTrue((await r.json())['ok']);self.assertEqual(gateway.keys(store,TOKEN,42)[0]['balance'],999800)
   r=await client.post('/api/creator/messages',headers={'X-Telegram-Init-Data':sign(43)},json={'text':'Сделай Lua','model':'glm'})
   data=await r.json();self.assertFalse(data['ok']);self.assertEqual(gateway.keys(store,TOKEN,42)[0]['balance'],999800)
  await client.close();store.db.close()

class PaymentTests(unittest.IsolatedAsyncioTestCase):
 async def test_external_payment_delivery_idempotent(self):
  store=Store(':memory:');store.init_shop();miniapp.init_tables(store)
  order={'id':'e'*24,'buyer':42,'product_id':'ai_glm','name':'GLM','price_kopecks':10000,'file_id':'ai:glm','filename':'ai_tokens','instruction':'','provider':'crypto','provider_id':'42','payment_url':'https://example.com','state':'pending','created_at':1}
  store.order_add(order);bot=NS(send_message=AsyncMock(),send_document=AsyncMock())
  with patch.dict(os.environ,{'BOT_TOKEN':TOKEN}),patch('payments.paid',AsyncMock(return_value=True)):
   self.assertEqual(await shop.check_order(bot,store,order['id'],42),'delivered')
   self.assertEqual(await shop.check_order(bot,store,order['id'],42),'delivered')
  self.assertEqual(bot.send_message.await_count,1);self.assertEqual(len(gateway.keys(store,TOKEN,42)),1)
  self.assertEqual(await shop.check_order(bot,store,order['id'],99),'not_found');store.db.close()

class RealSdkTests(unittest.IsolatedAsyncioTestCase):
 async def test_gateway_sends_real_sdk_text_and_image_shape(self):
  received={}
  async def endpoint(request):
   received.update(await request.json());received['auth']=request.headers.get('Authorization')
   return web.json_response({'id':'test','object':'chat.completion','created':1,'model':'actual','choices':[{'index':0,'message':{'role':'assistant','content':'ok'},'finish_reason':'stop'}],'usage':{'prompt_tokens':7,'completion_tokens':2,'total_tokens':9}})
  app=web.Application();app.router.add_post('/v1/chat/completions',endpoint)
  client=TestClient(TestServer(app));await client.start_server()
  try:
   result=await gateway.upstream({'base_url':str(client.make_url('/v1')).rstrip('/'),'model_id':'actual'},'upstream-key',[{'role':'user','content':[{'type':'text','text':'hello'},{'type':'image_url','image_url':{'url':'data:image/png;base64,AAAA'}}]}],100)
   self.assertEqual(received['auth'],'Bearer upstream-key');self.assertEqual(received['model'],'actual')
   self.assertEqual(received['messages'][0]['content'][1]['type'],'image_url');self.assertEqual(result['usage']['total_tokens'],9)
  finally:await client.close()

class CreatorModelChoiceTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  from tests.test_miniapp import sign
  import script_creator
  self.sign=sign;self.creator=script_creator;script_creator.RATE.clear()
  self.store=Store(':memory:');self.store.init_shop();miniapp.init_tables(self.store)
  cipher=support_ai.crypt(TOKEN).encrypt(b'provider-secret').decode()
  for key,model in [('glm','provider-glm'),('gpt_sol','provider-sol')]:
   ai_models.save(self.store,TOKEN,key,'https://example.com/v1',model,cipher,10000)
  gateway.issue(self.store,TOKEN,'stars',{'id':'1'*24,'buyer':42,'file_id':'ai:glm'})
  gateway.issue(self.store,TOKEN,'stars',{'id':'2'*24,'buyer':42,'file_id':'ai:gpt_sol'})
  gateway.issue(self.store,TOKEN,'stars',{'id':'3'*24,'buyer':43,'file_id':'ai:glm'})
  app=web.Application();app['store']=self.store;app['token']=TOKEN;app['admin_id']=42
  app.router.add_get('/api/creator/models',miniapp.creator_models);app.router.add_post('/api/creator/messages',miniapp.creator_messages)
  self.client=TestClient(TestServer(app));await self.client.start_server()
 async def asyncTearDown(self):await self.client.close();self.store.db.close()
 async def test_select_model_debits_only_selected_key(self):
  fake={'choices':[{'message':{'content':'```lua\nprint(1)\n```'}}],'usage':{'total_tokens':77}}
  with patch.object(gateway,'upstream',AsyncMock(return_value=fake)) as upstream,patch.object(self.creator,'check_syntax',AsyncMock(return_value={'status':'ok','message':'ok'})):
   r=await self.client.post('/api/creator/messages',headers={'X-Telegram-Init-Data':self.sign(42)},json={'text':'Сделай бота','model':'gpt_sol'})
   self.assertTrue((await r.json())['ok']);self.assertEqual(upstream.await_args.args[0]['model_id'],'provider-sol')
  balances={x['model']:x['balance'] for x in gateway.keys(self.store,TOKEN,42)}
  self.assertEqual(balances,{'glm':1000000,'gpt_sol':999923});self.assertEqual(gateway.keys(self.store,TOKEN,43)[0]['balance'],1000000)
 async def test_no_unpaid_admin_bypass_and_no_secrets_in_models(self):
  r=await self.client.post('/api/creator/messages',headers={'X-Telegram-Init-Data':self.sign(42)},json={'text':'Бот','model':'support'})
  self.assertEqual(r.status,400)
  r=await self.client.get('/api/creator/models',headers={'X-Telegram-Init-Data':self.sign(42)})
  data=await r.json();self.assertEqual({x['model'] for x in data['models']},{'glm','gpt_sol'});self.assertNotIn('api_token',str(data))
 async def test_syntax_retry_charges_twice(self):
  answers=[{'choices':[{'message':{'content':'```lua\nbroken\n```'}}],'usage':{'total_tokens':30}}, {'choices':[{'message':{'content':'```lua\nfixed\n```'}}],'usage':{'total_tokens':40}}]
  checks=[{'status':'error','message':'syntax error'},{'status':'ok','message':'ok'}]
  with patch.object(gateway,'upstream',AsyncMock(side_effect=answers)),patch.object(self.creator,'check_syntax',AsyncMock(side_effect=checks)):
   r=await self.client.post('/api/creator/messages',headers={'X-Telegram-Init-Data':self.sign(42)},json={'text':'Бот','model':'glm'})
   data=await r.json();self.assertEqual(data['code'],'fixed\n')
  balances={x['model']:x['balance'] for x in gateway.keys(self.store,TOKEN,42)};self.assertEqual(balances['glm'],999930)
