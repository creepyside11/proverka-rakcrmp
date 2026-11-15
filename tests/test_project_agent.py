import json,unittest
from unittest.mock import AsyncMock,patch
import project_agent

class ProjectAgentTests(unittest.IsolatedAsyncioTestCase):
    def test_url_and_path_validation(self):
        self.assertEqual(project_agent.repo_parts('https://github.com/example/repo.git'),('example','repo'))
        for url in ('http://github.com/a/b','https://github.com.evil.test/a/b','https://github.com/a/b/tree/main','https://user@github.com/a/b'):
            with self.assertRaises(ValueError):project_agent.repo_parts(url)
        for path in ('../app.py','.env','secret.py','x/../../app.py','/etc/passwd'):
            self.assertFalse(project_agent.safe_path(path))
    async def test_read_write_and_archive(self):
        import base64,io,zipfile
        async def fake(session,url,params=None):
            if url.endswith('/repos/example/repo'):return json.dumps({'default_branch':'main'}).encode()
            if '/commits/' in url:return json.dumps({'sha':'a'*40}).encode()
            if '/git/trees/' in url:return json.dumps({'tree':[{'path':'app.py','type':'blob','size':8}]}).encode()
            if '/contents/' in url:return json.dumps({'encoding':'base64','content':base64.b64encode(b'print(1)').decode()}).encode()
            raise AssertionError(url)
        model=AsyncMock(side_effect=[json.dumps({'action':'plan','steps':['Improve']}),json.dumps({'action':'tree'}),json.dumps({'action':'read','path':'app.py'}),json.dumps({'action':'write','path':'app.py','content':'print(2)'}),json.dumps({'action':'finish','message':'Done'})])
        with patch.object(project_agent,'fetch',fake):result=await project_agent.run(model,'https://github.com/example/repo','Improve',[],lambda *v:None)
        self.assertEqual(result['files'],{'app.py':'print(2)'})
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(project_agent.archive(result)))) as z:
            self.assertEqual(z.read('app.py'),b'print(2)');self.assertIn('RAKCRMP_CHANGES.txt',z.namelist())

    async def test_large_file_is_edited_without_truncating_other_content(self):
        import base64
        original='x=1\n'*45000+'target = 1\n'+'y=2\n'*20000
        async def fake(session,url,params=None):
            if url.endswith('/repos/example/repo'):return json.dumps({'default_branch':'main'}).encode()
            if '/commits/' in url:return json.dumps({'sha':'a'*40}).encode()
            if '/git/trees/' in url:return json.dumps({'tree':[{'path':'bot.py','type':'blob','size':len(original)}]}).encode()
            if '/contents/' in url:return json.dumps({'encoding':'base64','content':base64.b64encode(original.encode()).decode()}).encode()
            raise AssertionError(url)
        model=AsyncMock(side_effect=[json.dumps({'action':'plan','steps':['Edit']}),json.dumps({'action':'read','path':'bot.py'}),json.dumps({'action':'search_file','path':'bot.py','text':'target'}),json.dumps({'action':'replace','path':'bot.py','old':'target = 1','new':'target = 2'}),json.dumps({'action':'finish','message':'Done'})])
        with patch.object(project_agent,'fetch',fake):result=await project_agent.run(model,'https://github.com/example/repo','Improve',[],lambda *v:None)
        self.assertEqual(result['files']['bot.py'],original.replace('target = 1','target = 2'))

    async def test_search_links_and_private_targets_are_blocked(self):
        parser=project_agent.SearchParser()
        parser.feed('<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdocs">Docs</a>')
        self.assertIn('https://example.com/docs',parser.links)
        for url in ['https://127.0.0.1/','https://169.254.169.254/','https://localhost/','https://10.1.2.3/','https://example.com:8443/']:
            self.assertFalse(project_agent.public_url(url))
            self.assertIn('только',await project_agent.open_search_result(url,{url}))
        self.assertIn('только',await project_agent.open_search_result('https://example.com/',set()))

    async def test_dns_resolver_rejects_private_answers(self):
        import asyncio,socket
        fake=AsyncMock(return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))])
        with patch.object(asyncio.get_running_loop(),'getaddrinfo',fake):
            with self.assertRaises(OSError):await project_agent.PublicResolver().resolve('example.com',443)
