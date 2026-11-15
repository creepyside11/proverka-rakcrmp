import asyncio
import errno
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer
import asyncssh
import vps_install as v

TOKEN='123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789'
class Server(asyncssh.SSHServer):
    def begin_auth(self,username): return False

class InstallerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        v.PROBES.clear();v.JOBS.clear();v.ACTIVE.clear();v.LAST_PROBE.clear()
        self.scripts=[]
        async def process(p):
            if p.command=='id -u': p.stdout.write('0\n')
            elif p.command=='/bin/bash -s': self.scripts.append(await p.stdin.read())
            else: p.exit(1); return
            p.exit(0)
        self.ssh=await asyncssh.create_server(Server,'127.0.0.1',0,server_host_keys=[asyncssh.generate_private_key('ssh-ed25519')],process_factory=process)
        self.port=self.ssh.get_port();self.tasks=[]
        app=web.Application();app['application']=SimpleNamespace(create_task=lambda c:self.tasks.append(asyncio.create_task(c)))
        async def auth(req): return int(req.headers.get('uid','42'))
        async def probe(req): return await v.probe(req,auth)
        async def start(req): return await v.start(req,auth)
        async def status(req): return await v.status(req,auth)
        app.router.add_post('/probe',probe);app.router.add_post('/start',start);app.router.add_get('/status',status)
        self.client=TestClient(TestServer(app));await self.client.start_server()
        self.orig_key=asyncssh.get_server_host_key;self.orig_connect=asyncssh.connect
        self.p1=patch.object(v,'public_ip',lambda host:'127.0.0.1');self.p1.start()
        self.p2=patch.object(asyncssh,'get_server_host_key',lambda host,**kw:self.orig_key(host,port=self.port,config=None));self.p2.start()
        def connect(host,**kw): kw['port']=self.port;return self.orig_connect(host,**kw)
        self.p3=patch.object(asyncssh,'connect',connect);self.p3.start()
    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done(): task.cancel()
            try: await task
            except asyncio.CancelledError: pass
        self.p3.stop();self.p2.stop();self.p1.stop();await self.client.close();self.ssh.close();await self.ssh.wait_closed()
    async def test_install_pins_host_and_never_returns_secrets(self):
        r=await self.client.post('/probe',json={'host':'8.8.8.8'});self.assertEqual(r.status,200);probe=await r.json();self.assertTrue(probe['fingerprint'].startswith('SHA256:'))
        data={'probe_id':probe['probe_id'],'os':'ubuntu-26.04','username':'root','password':'MySecretPassword','bot_token':TOKEN,'admin_id':'1234','confirmed':True}
        r=await self.client.post('/start',json=data);self.assertEqual(r.status,200);job=(await r.json())['job_id']
        await asyncio.wait_for(self.tasks[0],10)
        r=await self.client.get('/status',params={'job':job});body=await r.json();self.assertEqual(body['state'],'done')
        self.assertNotIn('MySecretPassword',str(body));self.assertNotIn(TOKEN,str(body))
        self.assertEqual(len(self.scripts),1);self.assertIn('git fetch -q --depth 1 origin refs/heads/main',self.scripts[0]);self.assertIn('lua5.4',self.scripts[0]);self.assertIn('TG_BOT_TOKEN=',self.scripts[0]);self.assertNotIn('MySecretPassword',self.scripts[0])
        script=self.scripts[0]
        self.assertEqual(subprocess.run(['bash','-n'],input=script,text=True,capture_output=True).returncode,0)
        for part in ('python3-venv', 'pip install', '-r requirements.txt', 'import telebot, requests',
                     'sha256sum -c --status', 'sing-box-1.12.25-linux-', 'SING_BOX_BIN=/opt/rakcrmp-black-runtime/sing-box',
                     'ExecStart=/opt/rakcrmp-black/.venv/bin/python'):
            self.assertIn(part,script)
        r=await self.client.get('/status',params={'job':job},headers={'uid':'43'});self.assertEqual(r.status,404)
        r=await self.client.post('/start',json=data);self.assertEqual(r.status,400)
    async def test_unconfirmed_does_not_connect(self):
        r=await self.client.post('/probe',json={'host':'8.8.8.8'});probe=await r.json()
        r=await self.client.post('/start',json={'probe_id':probe['probe_id'],'os':'ubuntu-26.04','username':'root','password':'password','bot_token':TOKEN,'admin_id':'1234','confirmed':False})
        self.assertEqual(r.status,400);self.assertEqual(len(self.tasks),0)
    async def test_probe_timeout_is_explained_without_credentials(self):
        async def timeout(*args,**kwargs): raise asyncio.TimeoutError()
        with patch.object(asyncssh,'get_server_host_key',timeout):
            r=await self.client.post('/probe',json={'host':'8.8.8.8'})
        self.assertEqual(r.status,504);text=await r.text()
        self.assertIn('не ответил',text);self.assertNotIn(TOKEN,text)
    async def test_probe_connection_refused_is_distinct(self):
        async def refused(*args,**kwargs): raise ConnectionRefusedError(errno.ECONNREFUSED,'refused')
        with patch.object(asyncssh,'get_server_host_key',refused):
            r=await self.client.post('/probe',json={'host':'8.8.8.8'})
        self.assertEqual(r.status,502);self.assertIn('отклонил',await r.text())
    async def test_probe_cannot_be_used_by_another_user(self):
        r=await self.client.post('/probe',json={'host':'8.8.8.8'});probe=await r.json()
        data={'probe_id':probe['probe_id'],'os':'ubuntu-26.04','username':'root','password':'password','bot_token':TOKEN,'admin_id':'1234','confirmed':True}
        r=await self.client.post('/start',json=data,headers={'uid':'43'})
        self.assertEqual(r.status,400);self.assertEqual(len(self.tasks),0)
    async def test_changed_host_key_blocks_install(self):
        r=await self.client.post('/probe',json={'host':'8.8.8.8'});probe=await r.json()
        async def process(p):
            self.scripts.append('unexpected');p.exit(0)
        other=await asyncssh.create_server(Server,'127.0.0.1',0,server_host_keys=[asyncssh.generate_private_key('ssh-ed25519')],process_factory=process)
        self.port=other.get_port()
        data={'probe_id':probe['probe_id'],'os':'ubuntu-26.04','username':'root','password':'password','bot_token':TOKEN,'admin_id':'1234','confirmed':True}
        r=await self.client.post('/start',json=data);self.assertEqual(r.status,200);job=(await r.json())['job_id']
        await asyncio.wait_for(self.tasks[0],10)
        r=await self.client.get('/status',params={'job':job});body=await r.json()
        self.assertEqual(body['state'],'failed');self.assertIn('SSH-ключ изменился',body['message']);self.assertEqual(self.scripts,[])
        other.close();await other.wait_closed()
    def test_address_and_inputs(self):
        self.p1.stop()
        for addr in ['127.0.0.1','10.0.0.1','169.254.169.254','::1','8.8.8.8;id','localhost','224.0.0.1']:
            with self.assertRaises(ValueError): v.public_ip(addr)
        self.assertEqual(v.public_ip('8.8.8.8'),'8.8.8.8')
        with self.assertRaises(ValueError): v.validate({'username':'root;echo','password':'p','bot_token':TOKEN,'admin_id':'123','confirmed':True,'os':'ubuntu-26.04'})
        self.p1.start()


class InstallationScriptSimulation(unittest.TestCase):
    def test_clean_install_fetches_current_main_and_sets_up_lua(self):
        import os
        import tempfile
        from pathlib import Path
        import vps_install as vi
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);app=root/'app';runtime=root/'runtime';config=root/'config';units=root/'units';bins=root/'bin';bins.mkdir();units.mkdir();systemd=root/'systemd';systemd.mkdir();osrelease=root/'os-release';osrelease.write_text('ID=ubuntu\nVERSION_ID=26.04\n');calls=root/'calls'
            def stub(name,body):
                file=bins/name;file.write_text('#!/bin/sh\n'+body+'\n');file.chmod(0o755)
            stub('id','[ "$1" = "-u" ] && { echo 0; exit 0; }; exit 1')
            stub('systemctl','[ "$1" = "cat" ] && exit 1; exit 0')
            stub('apt-get','echo "apt-get $*" >> "$TEST_CALLS"; exit 0')
            stub('git','echo "git $*" >> "$TEST_CALLS"; exit 0')
            stub('useradd','exit 0');stub('chown','exit 0');stub('sleep','exit 0')
            stub('runuser','shift 3; exec "$@"')
            stub('curl','while [ "$#" -gt 0 ]; do if [ "$1" = "-o" ]; then shift; : > "$1"; exit 0; fi; shift; done; exit 1')
            stub('sha256sum','exit 0')
            stub('tar','printf "#!/bin/sh\\nexit 0\\n"')
            stub('python3','if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then mkdir -p "$3/bin"; printf "#!/bin/sh\\nexit 0\\n" > "$3/bin/python"; chmod +x "$3/bin/python"; fi; exit 0')
            script=vi.installation_script('123456:ExampleTokenForTesting','42').replace('/opt/rakcrmp-black-runtime',str(runtime)).replace('/opt/rakcrmp-black',str(app)).replace('/etc/rakcrmp-black',str(config)).replace('/etc/systemd/system',str(units)).replace('/run/systemd/system',str(systemd)).replace('/etc/os-release',str(osrelease))
            env=os.environ.copy();env.update(PATH=str(bins)+':'+env['PATH'],TEST_CALLS=str(calls))
            import subprocess
            result=subprocess.run(['bash'],input=script,text=True,capture_output=True,env=env,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            log=calls.read_text();self.assertIn('git fetch -q --depth 1 origin refs/heads/main',log);self.assertIn('lua5.4',log)
            self.assertTrue((units/'rakcrmp-black.service').is_file());self.assertTrue((config/'bot.env').is_file())
            self.assertNotIn('ad324c9',log)
