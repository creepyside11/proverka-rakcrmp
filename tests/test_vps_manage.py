import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import vps_manage

class ScriptTests(unittest.TestCase):
    def test_fixed_commands_and_syntax(self):
        for action in (*vps_manage.ACTIONS, 'update_bot'):
            script=vps_manage.command_script(action)
            result=subprocess.run(['bash','-n'],input=script,text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('caddy',vps_manage.command_script('stop_panel'))
        self.assertIn('disable --now rakcrmp-panel.service',vps_manage.command_script('stop_panel'))
        self.assertIn('disable --now rakcrmp-black.service',vps_manage.command_script('stop_bot'))

    def test_update_preserves_accounts_and_rolls_back_failure(self):
        for scenario in ('success', 'pip_failure', 'account_conflict', 'stopped'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as dirname:
                pip_failure=scenario=='pip_failure'
                t=Path(dirname);up=t/'upstream';up.mkdir();app=t/'app';runtime=t/'runtime';runtime.mkdir();config=t/'config';config.mkdir();units=t/'units';units.mkdir();bins=t/'bin';bins.mkdir()
                def run(*args,cwd=None):
                    return subprocess.run(args,cwd=cwd,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True).stdout.strip()
                run('git','init','-q','-b','main',str(up));run('git','config','user.email','test@example.invalid',cwd=up);run('git','config','user.name','Test',cwd=up)
                (up/'accounts.json').write_text('{"accounts": []}')
                (up/'bot.py').write_text('old')
                (up/'requirements.txt').write_text('')
                run('git','add','.',cwd=up)
                run('git','commit','-qm','old',cwd=up);old=run('git','rev-parse','HEAD',cwd=up)
                run('git','clone','-q',str(up),str(app));(app/'accounts.json').write_text('{"accounts": ["my-account"]}')
                (up/'bot.py').write_text('new')
                if scenario=='account_conflict': (up/'accounts.json').write_text('{\"accounts\": [\"other\"]}')
                run('git','add','.',cwd=up);run('git','commit','-qm','new',cwd=up);new=run('git','rev-parse','HEAD',cwd=up)
                (app/'.venv/bin').mkdir(parents=True)
                python=app/'.venv/bin/python';python.write_text('#!/bin/sh\nprintf changed > "$(dirname "$0")/marker"\n[ "${PIP_FAIL:-0}" != 1 ]\n');python.chmod(0o755)
                (app/'.venv/bin/marker').write_text('original')
                (units/'rakcrmp-black.service').write_text('test')
                active=t/'active'
                if scenario!='stopped':active.write_text('1')
                def stub(name,body):
                    p=bins/name;p.write_text('#!/bin/sh\n'+body+'\n');p.chmod(0o755)
                stub('id','echo 0');stub('runuser','shift 3; exec "$@"');stub('sleep','exit 0')
                stub('systemctl','case "$1" in is-active) test -e "$ACTIVE_FILE";; stop) rm -f "$ACTIVE_FILE";; start) touch "$ACTIVE_FILE";; *) exit 1;; esac')
                script=vps_manage.UPDATE_SCRIPT.replace('/opt/rakcrmp-black-runtime',str(runtime)).replace('/opt/rakcrmp-black',str(app)).replace('/etc/rakcrmp-black',str(config)).replace('/etc/systemd/system',str(units)).replace('https://github.com/creepyside11/black.git',str(up))
                env=os.environ.copy();env.update(PATH=str(bins)+':'+env['PATH'],ACTIVE_FILE=str(active),PIP_FAIL='1' if pip_failure else '0')
                r=subprocess.run(['bash'],input=script,text=True,capture_output=True,env=env,timeout=30)
                self.assertEqual(r.returncode,37 if pip_failure else 40 if scenario=='account_conflict' else 0,r.stderr)
                self.assertEqual(run('git','rev-parse','HEAD',cwd=app),old if scenario in ('pip_failure','account_conflict') else new)
                self.assertEqual((app/'accounts.json').read_text(),'{"accounts": ["my-account"]}')
                self.assertEqual(active.exists(),scenario!='stopped')
                self.assertEqual((app/'.venv/bin/marker').read_text(),'original' if scenario in ('pip_failure','account_conflict') else 'changed')

class EndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncio, asyncssh, time
        from aiohttp import web
        from aiohttp.test_utils import TestClient,TestServer
        from types import SimpleNamespace
        from unittest.mock import patch
        import vps_install as vi
        self.vi=vi;vi.PROBES.clear();vi.JOBS.clear();vi.ACTIVE.clear();self.tasks=[];self.scripts=[]
        class SSHServer(asyncssh.SSHServer):
            def begin_auth(self,username):return False
        async def process(p):
            if p.command=='id -u':p.stdout.write('0\n')
            elif p.command=='/bin/bash -s':self.scripts.append(await p.stdin.read())
            else:p.exit(1);return
            p.exit(0)
        self.ssh=await asyncssh.create_server(SSHServer,'127.0.0.1',0,server_host_keys=[asyncssh.generate_private_key('ssh-ed25519')],process_factory=process)
        key=await asyncssh.get_server_host_key('127.0.0.1',port=self.ssh.get_port(),config=None)
        original=asyncssh.connect
        def connect(host,**kwargs):kwargs['port']=self.ssh.get_port();return original('127.0.0.1',**kwargs)
        self.patcher=patch.object(asyncssh,'connect',connect);self.patcher.start()
        app=web.Application();app['application']=SimpleNamespace(create_task=lambda c:self.tasks.append(asyncio.create_task(c)))
        async def auth(req):return int(req.headers.get('uid','42'))
        async def start(req):return await vps_manage.start(req,auth)
        async def status(req):return await vi.status(req,auth)
        app.router.add_post('/start',start);app.router.add_get('/status',status)
        self.client=TestClient(TestServer(app));await self.client.start_server()
        vi.PROBES['test']={'uid':42,'host':'8.8.8.8','key':key,'expires':time.time()+300}
    async def asyncTearDown(self):
        import asyncio
        for task in self.tasks:
            if not task.done():task.cancel()
            try:await task
            except asyncio.CancelledError:pass
        self.patcher.stop();await self.client.close();self.ssh.close();await self.ssh.wait_closed()
    async def test_command_is_allowed_confirmed_and_bound_to_user(self):
        import asyncio
        data={'probe_id':'test','username':'root','password':'private-pass','confirmed':True,'action':'stop_bot'}
        for changes in ({'action':'stop_bot; id'}, {'confirmed':False}):
            r=await self.client.post('/start',json={**data,**changes});self.assertEqual(r.status,400)
        r=await self.client.post('/start',json=data,headers={'uid':'43'});self.assertEqual(r.status,400)
        r=await self.client.post('/start',json=data);self.assertEqual(r.status,200);job=(await r.json())['job_id']
        await asyncio.wait_for(self.tasks[0],10)
        result=await (await self.client.get('/status',params={'job':job})).json()
        self.assertEqual(result['state'],'done');self.assertNotIn('private-pass',str(result));self.assertNotIn('private-pass',self.scripts[0]);self.assertIn('disable --now rakcrmp-black.service',self.scripts[0])
        r=await self.client.get('/status',params={'job':job},headers={'uid':'43'});self.assertEqual(r.status,404)
