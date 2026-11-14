import json
import unittest
from unittest.mock import AsyncMock,patch
from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer
from storage import Store
import ai_gateway,ai_models,miniapp,script_creator,support_ai

def response(text,reason='stop'):
    return {'choices':[{'message':{'content':text},'finish_reason':reason}],'usage':{'total_tokens':50}}
def command(kind,**args):return json.dumps({'action':kind,**args},ensure_ascii=False)

class ReadRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:');self.store.init_shop();miniapp.init_tables(self.store)
        self.token='bot-token';cipher=support_ai.crypt(self.token).encrypt(b'test-key').decode()
        ai_models.save(self.store,self.token,'glm','https://example.com/v1','model',cipher,100)
        ai_gateway.issue(self.store,self.token,'stars',{'id':'a'*24,'buyer':42,'file_id':'ai:glm'})
        app=web.Application();app['store']=self.store;app['token']=self.token
        async def auth(req,require_subscription=False):return int(req.headers['uid'])
        async def messages(req):return await script_creator.messages(req,auth)
        app.router.add_post('/messages',messages);app.router.add_get('/messages',messages)
        self.client=TestClient(TestServer(app));await self.client.start_server();script_creator.RATE.clear();script_creator.RUNS.clear()
    async def asyncTearDown(self):
        await self.client.close();self.store.db.close()
    async def test_read_then_empty_length_adapts_and_finishes_with_correct_billing(self):
        answers=[response(command('plan',steps=['Изучить API','Написать','Проверить'])),response(command('read_reference',name='api')),response(None,'length'),response(command('write_file',content='print(1)')),response(command('check_file')),response(command('finish',message='Готово'))]
        with patch.object(ai_gateway,'upstream',AsyncMock(side_effect=answers)) as upstream,patch('script_creator.asyncio.sleep',new_callable=AsyncMock) as sleep,patch.object(script_creator,'check_syntax',AsyncMock(return_value={'status':'ok','message':'OK'})):
            r=await self.client.post('/messages',headers={'uid':'42'},json={'text':'создай','model':'glm'})
            result=await r.json()
        self.assertTrue(result['ok']);self.assertEqual(result['code'],'print(1)')
        self.assertEqual(upstream.await_count,6);self.assertEqual(upstream.await_args_list[2].args[3],1024);self.assertEqual(upstream.await_args_list[3].args[3],2048)
        sleep.assert_awaited_once_with(10)
        self.assertEqual(ai_gateway.keys(self.store,self.token,42)[0]['balance'],999700)
        history=await (await self.client.get('/messages',headers={'uid':'42'})).json()
        events=[json.loads(m['body']) for m in history['messages'] if m['role']=='event']
        self.assertTrue(any('повтор через 10 секунд' in e['message'] for e in events))
        self.assertTrue(any('справочник' in e['message'] for e in events))
    async def test_public_api_still_rejects_maximum_over_1024(self):
        with self.assertRaises(ValueError):ai_gateway._validate({'messages':[{'role':'user','content':'hi'}],'max_tokens':2048})
        self.assertEqual(ai_gateway._validate({'messages':[{'role':'user','content':'hi'}],'max_tokens':2048},max_tokens_limit=4096)[1],2048)
