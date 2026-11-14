import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import vps_lua
import vps_lua_remote as remote

class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        (self.root/'bot.py').write_text('# bot')
        self.patch=patch.object(remote,'ROOT',self.root);self.patch.start()
    def tearDown(self): self.patch.stop();self.tmp.cleanup()
    def test_upload_list_and_collision(self):
        r=remote.process({'action':'list'}); self.assertEqual(r['files'],[]); self.assertFalse(r['lua_ready'])
        self.assertEqual(remote.process({'action':'upload','name':'test.lua','text':'return 1'})['saved'],'test.lua')
        self.assertEqual(remote.process({'action':'list'})['files'],['test.lua'])
        with self.assertRaises(ValueError): remote.process({'action':'upload','name':'test.lua','text':'replace'})
        for name in ('../bad.lua','x.lua.exe','../.env','foo.LUAC'):
            with self.assertRaises(ValueError): remote.process({'action':'upload','name':name,'text':'x'})
        self.assertEqual((self.root/'scripts/lua/test.lua').read_text(),'return 1')
    def test_per_script_settings(self):
        remote.process({'action':'upload','name':'test.lua','text':'return 1'})
        data={'action':'save_script_settings','name':'test.lua','settings':{'timeout':30,'delay':1.5,'enabled':True,'маршрут':{'точки':[1,2]}}}
        remote.process(data)
        self.assertEqual(remote.process({'action':'get_script_settings','name':'test.lua'})['settings'],data['settings'])
        remote.process({**data,'enabled':False})
        self.assertFalse(remote.process({'action':'get_script_settings','name':'test.lua'})['enabled'])
        self.assertEqual(remote.process({'action':'get_script_settings','name':'test.lua'})['settings'],data['settings'])
        with self.assertRaises(ValueError): remote.process({**data,'settings':{'bad':float('nan')}})
    def test_ai_secret_not_returned_and_other_env_preserved(self):
        (self.root/'.env').write_text('OTHER=keep\n')
        remote.process({'action':'save_ai','base_url':'https://example.com/v1','model':'vision','token':'secret-test'})
        response=remote.process({'action':'get_ai'}); self.assertTrue(response['token_set']);self.assertNotIn('secret-test',str(response))
        remote.process({'action':'save_ai','base_url':'https://example.com/v1','model':'new','token':''})
        self.assertIn('OTHER=keep',(self.root/'.env').read_text());self.assertIn('secret-test',(self.root/'.env').read_text())
        self.assertEqual((self.root/'.env').stat().st_mode & 0o777,0o600)
    def test_refuse_symlink(self):
        (self.root/'scripts').symlink_to('/tmp')
        with self.assertRaises(ValueError): remote.process({'action':'list'})

class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_binds_to_user_and_does_not_echo_token(self):
        class Conn:
            def is_closed(self): return False
            def close(self): pass
            async def run(self,command,input=None,timeout=None):
                self.body=input
                return SimpleNamespace(exit_status=0,stdout=json.dumps({'ok':True,'result':{'token_set':True}}))
        conn=Conn(); vps_lua.SESSIONS['test']={'uid':42,'conn':conn,'root':True,'expires':time.monotonic()+100,'lock':asyncio.Lock()}
        async def auth(req):return int(req.headers['uid'])
        async def action(req):return await vps_lua.action(req,auth)
        app=web.Application();app.router.add_post('/action',action)
        client=TestClient(TestServer(app));await client.start_server()
        try:
            r=await client.post('/action',json={'session_id':'test','action':'get_ai'},headers={'uid':'43'});self.assertEqual(r.status,401)
            r=await client.post('/action',json={'session_id':'test','action':'save_ai','token':'secret'},headers={'uid':'42'});self.assertEqual(r.status,200)
            self.assertNotIn('secret',await r.text());self.assertIn('secret',conn.body)
        finally:
            session=vps_lua.SESSIONS.pop('test',None)
            if session and session.get('timer'):session['timer'].cancel()
            await client.close()

class ConnectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncssh
        self.asyncssh=asyncssh
        class SSHServer(asyncssh.SSHServer):
            def begin_auth(self,username): return False
        async def process(proc):
            if proc.command=='id -u': proc.stdout.write('0\n');proc.exit(0)
            else: proc.exit(1)
        self.server=await asyncssh.create_server(SSHServer,'127.0.0.1',0,server_host_keys=[asyncssh.generate_private_key('ssh-ed25519')],process_factory=process)
        key=await asyncssh.get_server_host_key('127.0.0.1',port=self.server.get_port(),config=None)
        vps_lua.vi.PROBES.clear();vps_lua.vi.PROBES['ok']={'uid':42,'host':'8.8.8.8','key':key,'expires':time.time()+300}
        original=asyncssh.connect
        def connect(host,**kwargs): kwargs['port']=self.server.get_port();return original('127.0.0.1',**kwargs)
        self.patcher=patch.object(asyncssh,'connect',connect);self.patcher.start()
        async def auth(req): return int(req.headers['uid'])
        async def do_connect(req): return await vps_lua.connect(req,auth)
        async def do_close(req): return await vps_lua.close(req,auth)
        app=web.Application();app.router.add_post('/connect',do_connect);app.router.add_post('/close',do_close)
        self.client=TestClient(TestServer(app));await self.client.start_server()
    async def asyncTearDown(self):
        for sid,session in list(vps_lua.SESSIONS.items()):
            if session.get('timer'):session['timer'].cancel()
            session['conn'].close();vps_lua.SESSIONS.pop(sid,None)
        await self.client.close();self.patcher.stop();self.server.close();await self.server.wait_closed()
    async def test_verified_user_session_close(self):
        body={'probe_id':'ok','username':'root','password':'x','confirmed':True}
        r=await self.client.post('/connect',json=body,headers={'uid':'43'});self.assertEqual(r.status,400)
        r=await self.client.post('/connect',json=body,headers={'uid':'42'});self.assertEqual(r.status,200)
        sid=(await r.json())['session_id'];self.assertIn(sid,vps_lua.SESSIONS)
        await self.client.post('/close',json={'session_id':sid},headers={'uid':'43'});self.assertIn(sid,vps_lua.SESSIONS)
        await self.client.post('/close',json={'session_id':sid},headers={'uid':'42'});self.assertNotIn(sid,vps_lua.SESSIONS)
