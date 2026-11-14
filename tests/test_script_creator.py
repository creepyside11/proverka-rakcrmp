import os
import asyncio
import json
import tempfile
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from storage import Store
import support_ai
import script_creator as creator

class CreatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:');support_ai.init(self.store);creator.init(self.store)
        support_ai.save_config(self.store,'bot-token',{'base_url':'https://example.com/v1','model':'test','api_token':'test-key'})
        app=web.Application();app['store']=self.store;app['token']='bot-token';app['admin_id']=42
        async def auth(request,require_subscription=False):return int(request.headers['uid'])
        async def handler(request):return await creator.messages(request,auth)
        async def progress(request):return await creator.progress(request,auth)
        app.router.add_get('/progress',progress)
        app.router.add_get('/messages',handler);app.router.add_post('/messages',handler)
        self.client=TestClient(TestServer(app));await self.client.start_server();creator.RATE.clear();creator.RUNS.clear()
    async def asyncTearDown(self):
        await self.client.close();self.store.db.close()
    async def test_chat_saves_separately_and_no_cross_user_history(self):
        with patch.object(creator,'paid_model',AsyncMock(return_value='```lua\nbr.log("hi")\n```')), patch.object(creator,'check_syntax',AsyncMock(return_value={'status':'ok','message':'syntactic only'})):
            resp=await self.client.post('/messages',headers={'uid':'42'},json={'text':'сделай скрипт','model':'glm'})
            data=await resp.json();self.assertTrue(data['ok']);self.assertEqual(data['code'],'br.log("hi")\n');self.assertEqual(data['check']['status'],'ok')
        other=await self.client.get('/messages',headers={'uid':'43'});self.assertEqual((await other.json())['messages'],[])
        own=await self.client.get('/messages',headers={'uid':'42'});self.assertEqual(len((await own.json())['messages']),2)
    async def test_agent_persists_file_and_progress_is_private(self):
        def step(a,**k):return json.dumps({'action':a,**k},ensure_ascii=False)
        model=AsyncMock(side_effect=[step('plan',steps=['Написать','Проверить']),step('write_file',content='print(1)'),step('check_file'),step('finish',message='Готово')])
        check=AsyncMock(return_value={'status':'ok','message':'Синтаксис корректен'})
        with patch.object(creator,'paid_model',model),patch.object(creator,'check_syntax',check):
            r=await self.client.post('/messages',headers={'uid':'42'},json={'text':'скрипт','model':'glm'})
            result=await r.json()
        self.assertTrue(result['ok']);self.assertEqual(creator.file_for(self.store,42),'print(1)')
        self.assertEqual(creator.file_for(self.store,43),'')
        own=await (await self.client.get('/progress',headers={'uid':'42'})).json()
        other=await (await self.client.get('/progress',headers={'uid':'43'})).json()
        self.assertEqual(own['state'],'done');self.assertTrue(any(e['kind']=='plan' for e in own['events']))
        self.assertEqual(other['events'],[])

    async def test_background_progress_and_duplicate_block(self):
        gate=asyncio.Event()
        async def slow(*args):
            await gate.wait()
            return json.dumps({'action':'ask','message':'Какое событие использовать?'},ensure_ascii=False)
        with patch.object(creator,'paid_model',side_effect=slow):
            response=await self.client.post('/messages',headers={'uid':'42'},json={'text':'создай','model':'glm','background':True})
            data=await response.json();self.assertTrue(data['started'])
            await asyncio.sleep(0)
            state=await (await self.client.get('/progress',headers={'uid':'42'},params={'run':data['run_id']})).json()
            self.assertEqual(state['state'],'running')
            other=await self.client.get('/progress',headers={'uid':'43'},params={'run':data['run_id']});self.assertEqual(other.status,404)
            duplicate=await self.client.post('/messages',headers={'uid':'42'},json={'text':'создай снова','model':'glm','background':True});self.assertEqual(duplicate.status,409)
            gate.set();await asyncio.wait_for(creator.RUNS[42]['task'],3)
            result=await (await self.client.get('/progress',headers={'uid':'42'},params={'run':data['run_id']})).json()
            self.assertEqual(result['state'],'done');self.assertIn('событие',result['result']['answer'])

    async def test_corrects_failed_syntax_once(self):
        model=AsyncMock(side_effect=['```lua\nbroken\n```','```lua\nfixed\n```'])
        checks=AsyncMock(side_effect=[{'status':'error','message':'syntax error'},{'status':'ok','message':'syntax only'}])
        with patch.object(creator,'paid_model',model),patch.object(creator,'check_syntax',checks):
            r=await self.client.post('/messages',headers={'uid':'42'},json={'text':'create','model':'glm'})
            data=await r.json();self.assertEqual(data['code'],'fixed\n');self.assertEqual(model.await_count,2)
    async def test_missing_compiler_is_explicit(self):
        with patch('script_creator.shutil.which',return_value=None), patch('script_creator.importlib.util.find_spec',return_value=None):
            self.assertEqual((await creator.check_syntax('print(1)'))['status'],'unavailable')
    async def test_compiler_called_with_parse_only(self):
        with tempfile.TemporaryDirectory() as d:
            bin=Path(d)/'luac5.4';bin.write_text('#!/bin/sh\n[ "$1" = "-p" ] && exit 0\nexit 1\n');bin.chmod(0o755)
            with patch('script_creator.shutil.which',return_value=str(bin)):
                self.assertEqual((await creator.check_syntax('os.execute("bad")'))['status'],'ok')

class CreatorBrowserFlowTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'),'Node.js is unavailable')
    def test_model_selection_and_send(self):
        subprocess.run(['node','tests/test_creator_flow.js'],check=True,capture_output=True,text=True,timeout=10)
