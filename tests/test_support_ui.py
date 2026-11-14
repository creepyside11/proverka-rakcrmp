import shutil
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer
import support_ai as support
from storage import Store

class RenderingTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'),'Node.js is unavailable')
    def test_safe_markdown(self):
        subprocess.run(['node','tests/test_support_render.js'],check=True,capture_output=True,text=True)
    @unittest.skipUnless(shutil.which('node'),'Node.js is unavailable')
    def test_chat_send_and_automatic_response(self):
        subprocess.run(['node','tests/test_support_chat_flow.js'],check=True,capture_output=True,text=True,timeout=10)
    def test_markup_has_no_manual_refresh(self):
        text=Path('miniapp.html').read_text()
        self.assertNotIn('id="support-refresh"',text)
        self.assertIn('chat-typing',text)
        self.assertIn('/support-render.js',text)

class AvatarTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:');support.init(self.store);self.number=support.new_ticket(self.store,42)
        self.photo=NS(file_size=8,file_id='photo-id')
        self.file=NS(download_to_memory=AsyncMock(side_effect=lambda out:out.write(b'\xff\xd8\xffabc')))
        self.bot=NS(get_user_profile_photos=AsyncMock(return_value=NS(photos=[[self.photo]])),get_file=AsyncMock(return_value=self.file))
        app=web.Application();app['store']=self.store;app['bot']=self.bot;app['admin_id']=99
        async def auth(req,require_subscription=True):return int(req.headers['uid'])
        async def avatar(req):return await support.avatar(req,auth)
        app.router.add_get('/{number}',avatar);self.client=TestClient(TestServer(app));await self.client.start_server()
    async def asyncTearDown(self):await self.client.close();self.store.db.close()
    async def test_owner_and_admin_can_see_avatar_others_cannot(self):
        r=await self.client.get('/'+self.number,headers={'uid':'43'});self.assertEqual(r.status,404);self.bot.get_file.assert_not_awaited()
        for uid in ('42','99'):
            r=await self.client.get('/'+self.number,headers={'uid':uid});self.assertEqual(r.status,200);self.assertEqual(r.headers['Content-Type'],'image/jpeg');self.assertEqual(await r.read(),b'\xff\xd8\xffabc')
    async def test_no_photo_or_non_image_is_empty(self):
        self.bot.get_user_profile_photos.return_value=NS(photos=[])
        r=await self.client.get('/'+self.number,headers={'uid':'42'});self.assertEqual(r.status,204)
        self.bot.get_user_profile_photos.return_value=NS(photos=[[self.photo]])
        self.file.download_to_memory.side_effect=lambda out:out.write(b'<svg onload=x>')
        r=await self.client.get('/'+self.number,headers={'uid':'42'});self.assertEqual(r.status,204)
