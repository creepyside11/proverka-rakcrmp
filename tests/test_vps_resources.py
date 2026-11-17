"""Read-only resource check and authenticated management flow."""
import asyncio
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web
import vps_install as vi
import vps_manage as vm


class ResourceScriptTests(unittest.TestCase):
    def test_capacity_estimate_and_boundaries(self):
        valid={'cpu':28,'ram_total':1024**3,'ram_available':600*1024**2,'disk_total':10*1024**3,'disk_used':3*1024**3,'disk_available':6*1024**3}
        self.assertIn('ещё: 34', vm.format_resources(json.dumps(valid), 23))
        self.assertIn('RAM на бота не измерен', vm.format_resources(json.dumps(valid), 23))
        self.assertIn('Свободной RAM мало', vm.format_resources(json.dumps({**valid,'ram_available':100*1024**2}), 23))
        self.assertIn('слишком мала', vm.format_resources(json.dumps({**valid,'cpu':0}), 23))
        self.assertIn('ещё: 0', vm.format_resources(json.dumps({**valid,'cpu':80}), 23))
        self.assertIn('укажите число', vm.format_resources(json.dumps(valid)))
        for count in (0,-1,True,1.5,100001,'23'):
            with self.subTest(count=count), self.assertRaises(ValueError):
                vm.format_resources(json.dumps(valid), count)

    def test_script_collects_real_metrics_without_touching_processes(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp)/'systemctl_called'
            stub = Path(temp)/'systemctl'
            stub.write_text('#!/bin/sh\ntouch "$TEST_SYSTEMCTL_MARKER"\nexit 99\n')
            stub.chmod(0o755)
            env = dict(os.environ, PATH=temp+os.pathsep+os.environ['PATH'], TEST_SYSTEMCTL_MARKER=str(marker))
            sentinel = subprocess.Popen(['sleep', '20'])
            try:
                result=subprocess.run(['bash','-s'],input=vm.RESOURCE_SCRIPT,text=True,capture_output=True,env=env,timeout=8,check=True)
                self.assertIsNone(sentinel.poll(), 'resource check must not stop existing process')
                self.assertFalse(marker.exists(), 'resource check must not invoke systemctl')
                data=json.loads(result.stdout)
                self.assertTrue(0 <= data['cpu'] <= 100)
                self.assertGreater(data['ram_total'],0)
                self.assertGreater(data['disk_total'],0)
                text=vm.format_resources(result.stdout)
                self.assertIn('CPU:',text);self.assertIn('RAM:',text);self.assertIn('Диск /:',text)
            finally:
                sentinel.terminate();sentinel.wait(timeout=5)

    def test_validate_untrusted_output(self):
        valid={'cpu':10,'ram_total':100,'ram_available':50,'disk_total':500,'disk_used':200,'disk_available':250}
        self.assertIn('10.0%',vm.format_resources(json.dumps(valid)))
        for change in ({'cpu':float('nan')},{'cpu':101},{'ram_total':0},{'ram_available':101},{'disk_used':501},{'cpu':'1'},{'cpu':True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                vm.format_resources(json.dumps({**valid,**change}))
        with self.assertRaises((ValueError,TypeError)):
            vm.format_resources('not json')


class ResourceEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_runs_as_unprivileged_and_result_is_user_scoped(self):
        vi.PROBES.clear();vi.JOBS.clear();vi.ACTIVE.clear();tasks=[];calls=[]
        metrics=json.dumps({'cpu':25.5,'ram_total':1073741824,'ram_available':536870912,'disk_total':10737418240,'disk_used':2147483648,'disk_available':8053063680})
        class Connection:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def run(self,command,**kwargs):
                calls.append((command,kwargs))
                if command=='id -u':return SimpleNamespace(stdout='1000\n',exit_status=0)
                if command=='/bin/bash -s':return SimpleNamespace(stdout=metrics,exit_status=0)
                raise AssertionError('unexpected command '+command)
        async def auth(req):return req.uid
        class Req:
            uid=42
            query={}
            app={'application':SimpleNamespace(create_task=lambda coro: tasks.append(asyncio.create_task(coro)))}
            async def json(self):return {'probe_id':'p','username':'ubuntu','password':'secret-sentinel','confirmed':True,'action':'resources'}
        vi.PROBES['p']={'uid':42,'host':'8.8.8.8','key':'pinned-test-key','expires':time.time()+60}
        with patch.object(vm.asyncssh,'connect',return_value=Connection()):
            response=await vm.start(Req(),auth)
            job=json.loads(response.text)['job_id']
            await asyncio.wait_for(tasks[0],5)
        self.assertEqual([x[0] for x in calls],['id -u','/bin/bash -s'])
        self.assertEqual(calls[1][1]['input'],vm.RESOURCE_SCRIPT)
        self.assertNotIn('secret-sentinel',str(calls))
        request=Req();request.query={'job':job}
        result=json.loads((await vi.status(request,auth)).text)
        self.assertEqual(result['state'],'done');self.assertIn('CPU: 25.5%',result['message']);self.assertNotIn('secret-sentinel',str(result))
        request.uid=43
        with self.assertRaises(web.HTTPNotFound):await vi.status(request,auth)
        self.assertFalse(vi.ACTIVE)

    async def test_rejects_invalid_count_before_connecting(self):
        class Req:
            async def json(self):return {'action':'resources','username':'root','password':'x','confirmed':True,'online_bots':True}
        async def auth(req):return 42
        with self.assertRaises(web.HTTPBadRequest):
            await vm.start(Req(),auth)
