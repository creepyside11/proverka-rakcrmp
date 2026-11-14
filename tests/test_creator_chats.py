import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from storage import Store
import script_creator as creator

class ChatsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:');creator.init(self.store);creator.RUNS.clear();creator.RATE.clear()
        app=web.Application();app['store']=self.store;app['token']='test'
        async def auth(req,require_subscription=False):return int(req.headers['uid'])
        async def chat(req):return await creator.chats(req,auth)
        async def msg(req):return await creator.messages(req,auth)
        async def progress(req):return await creator.progress(req,auth)
        app.router.add_get('/chats',chat);app.router.add_post('/chats',chat);app.router.add_get('/messages',msg);app.router.add_post('/messages',msg);app.router.add_get('/progress',progress)
        self.client=TestClient(TestServer(app));await self.client.start_server()
    async def asyncTearDown(self):
        await self.client.close();self.store.db.close()
    async def test_separate_conversations_files_and_ownership(self):
        creator.add(self.store,42,'user','старая задача');creator.save_file(self.store,42,'legacy code')
        a=await (await self.client.post('/chats',headers={'uid':'42'})).json();b=await (await self.client.post('/chats',headers={'uid':'42'})).json()
        self.assertNotEqual(a['id'],b['id'])
        answers=[json.dumps({'action':'plan','steps':['Написать']}),json.dumps({'action':'write_file','content':'print(1)'}),json.dumps({'action':'finish','message':'Готово'})]
        with patch.object(creator,'paid_model',AsyncMock(side_effect=answers)),patch.object(creator,'check_syntax',AsyncMock(return_value={'status':'ok','message':'ok'})):
            r=await self.client.post('/messages',headers={'uid':'42'},json={'chat':a['id'],'text':'первая задача','model':'glm'});self.assertEqual(r.status,200)
        self.assertEqual(creator.file_for(self.store,42,a['id']),'print(1)');saved=await (await self.client.get('/messages',headers={'uid':'42'},params={'chat':a['id']})).json();self.assertEqual(saved['file'],'print(1)');self.assertEqual(creator.file_for(self.store,42,b['id']),'');self.assertEqual(creator.file_for(self.store,42),'legacy code')
        x=await (await self.client.get('/messages',headers={'uid':'42'},params={'chat':b['id']})).json();self.assertEqual(x['messages'],[])
        old=await (await self.client.get('/messages',headers={'uid':'42'})).json();self.assertEqual(old['messages'][0]['body'],'старая задача')
        forbidden=await self.client.get('/messages',headers={'uid':'43'},params={'chat':a['id']});self.assertEqual(forbidden.status,404)
    async def test_questions_persist_with_options(self):
        c=await (await self.client.post('/chats',headers={'uid':'42'})).json()
        answer=json.dumps({'action':'ask','message':'Какой тип?','options':['Ферма','Шахта']},ensure_ascii=False)
        with patch.object(creator,'paid_model',AsyncMock(return_value=answer)):
            r=await self.client.post('/messages',headers={'uid':'42'},json={'chat':c['id'],'text':'создай','model':'glm'});data=await r.json()
        self.assertEqual(data['options'],['Ферма','Шахта'])
        rows=(await (await self.client.get('/messages',headers={'uid':'42'},params={'chat':c['id']})).json())['messages']
        self.assertEqual(json.loads(next(x['body'] for x in rows if x['role']=='options')),['Ферма','Шахта'])
