import os
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
        app.router.add_get('/messages',handler);app.router.add_post('/messages',handler)
        self.client=TestClient(TestServer(app));await self.client.start_server();creator.RATE.clear()
    async def asyncTearDown(self):
        await self.client.close();self.store.db.close()
    async def test_chat_saves_separately_and_no_cross_user_history(self):
        with patch.object(creator,'paid_model',AsyncMock(return_value='```lua\nbr.log("hi")\n```')), patch.object(creator,'check_syntax',AsyncMock(return_value={'status':'ok','message':'syntactic only'})):
            resp=await self.client.post('/messages',headers={'uid':'42'},json={'text':'сделай скрипт','model':'glm'})
            data=await resp.json();self.assertTrue(data['ok']);self.assertEqual(data['code'],'br.log("hi")\n');self.assertEqual(data['check']['status'],'ok')
        other=await self.client.get('/messages',headers={'uid':'43'});self.assertEqual((await other.json())['messages'],[])
        own=await self.client.get('/messages',headers={'uid':'42'});self.assertEqual(len((await own.json())['messages']),2)
    async def test_corrects_failed_syntax_once(self):
        model=AsyncMock(side_effect=['```lua\nbroken\n```','```lua\nfixed\n```'])
        checks=AsyncMock(side_effect=[{'status':'error','message':'syntax error'},{'status':'ok','message':'syntax only'}])
        with patch.object(creator,'paid_model',model),patch.object(creator,'check_syntax',checks):
            r=await self.client.post('/messages',headers={'uid':'42'},json={'text':'create','model':'glm'})
            data=await r.json();self.assertEqual(data['code'],'fixed\n');self.assertEqual(model.await_count,2)
    async def test_missing_compiler_is_explicit(self):
        with patch('script_creator.shutil.which',return_value=None):
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
