import asyncio
import runpy
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import vps_panel


class PanelTests(unittest.TestCase):
    def test_script_preserves_bot_and_contains_guards(self):
        script = vps_panel.panel_script('8.8.8.8')
        self.assertIn('8-8-8-8.sslip.io', script)
        self.assertIn('127.0.0.1:18743', script)
        self.assertIn('/api/key /api/login', script)
        self.assertNotIn('restart rakcrmp-black.service', script)
        self.assertIn('chat_id\':admin', script)
        result = subprocess.run(['bash', '-n'], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_private_and_ipv6_hosts(self):
        for addr in ('127.0.0.1', '10.0.0.1', '2001:4860:4860::8888'):
            with self.subTest(addr=addr), self.assertRaises(ValueError):
                vps_panel.panel_script(addr)

    def test_guard_disables_other_logins(self):
        class Handler:
            def __init__(self, path): self.path=path; self.result=None
            def _json(self, body, code): self.result=(code, body)
            def do_POST(self): self.result='upstream'
        fake_web=types.ModuleType('web');fake_web.__path__=[]
        fake_server=types.ModuleType('web.server');fake_server.Handler=Handler
        fake_web.server=fake_server
        with patch.dict(sys.modules, {'web':fake_web,'web.server':fake_server}), patch.dict('os.environ', {'TG_ADMIN_ID':'123','PANEL_TOKEN':'secret'}):
            ns=runpy.run_path(str(Path(__file__).parent.parent/'panel_guard.py'))
            import os
            self.assertEqual(os.environ['TG_ADMIN_IDS'],'123')
            self.assertNotIn('PANEL_TOKEN',os.environ)
            cls=ns['TelegramOnlyHandler']
            for path in ('/api/login','/api/key?x=1'):
                h=cls(path);h.do_POST();self.assertEqual(h.result[0],403)
            h=cls('/api/tg');h.do_POST();self.assertEqual(h.result,'upstream')

class PanelEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from types import SimpleNamespace
        import asyncssh, vps_install
        self.vi=vps_install; self.vi.PROBES.clear();self.vi.JOBS.clear();self.vi.ACTIVE.clear()
        self.tasks=[];self.scripts=[]
        class SSHServer(asyncssh.SSHServer):
            def begin_auth(self, username): return False
        async def process(p):
            if p.command=='id -u': p.stdout.write('0\n')
            elif p.command=='/bin/bash -s': self.scripts.append(await p.stdin.read())
            else: p.exit(1);return
            p.exit(0)
        self.ssh=await asyncssh.create_server(SSHServer,'127.0.0.1',0,server_host_keys=[asyncssh.generate_private_key('ssh-ed25519')],process_factory=process)
        self.key=await asyncssh.get_server_host_key('127.0.0.1',port=self.ssh.get_port(),config=None)
        connect=asyncssh.connect
        def route(host,**kwargs): kwargs['port']=self.ssh.get_port();return connect('127.0.0.1',**kwargs)
        self.patches=[patch.object(asyncssh,'connect',route),patch.object(vps_panel,'panel_script',lambda host:'safe-panel-script')]
        for p in self.patches:p.start()
        app=web.Application();app['application']=SimpleNamespace(create_task=lambda c:self.tasks.append(asyncio.create_task(c)))
        async def auth(req):return int(req.headers.get('uid','42'))
        async def start(req):return await vps_panel.start(req,auth)
        async def status(req):return await self.vi.status(req,auth)
        app.router.add_post('/start',start);app.router.add_get('/status',status)
        self.client=TestClient(TestServer(app));await self.client.start_server()
        self.vi.PROBES['p']={'uid':42,'host':'8.8.8.8','key':self.key,'expires':__import__('time').time()+300}
    async def asyncTearDown(self):
        for t in self.tasks:
            if not t.done():t.cancel()
            try:await t
            except asyncio.CancelledError:pass
        for p in reversed(self.patches):p.stop()
        await self.client.close();self.ssh.close();await self.ssh.wait_closed()
    async def test_panel_job_uses_confirmed_key_and_hides_password(self):
        data={'probe_id':'p','username':'root','password':'private-ssh-password','confirmed':True}
        denied=await self.client.post('/start',json=data,headers={'uid':'43'});self.assertEqual(denied.status,400)
        response=await self.client.post('/start',json=data);self.assertEqual(response.status,200)
        jid=(await response.json())['job_id'];await asyncio.wait_for(self.tasks[0],10)
        result=await (await self.client.get('/status',params={'job':jid})).json()
        self.assertEqual(result['state'],'done');self.assertNotIn('private-ssh-password',str(result));self.assertEqual(self.scripts,['safe-panel-script'])
        other=await self.client.get('/status',params={'job':jid},headers={'uid':'43'});self.assertEqual(other.status,404)
