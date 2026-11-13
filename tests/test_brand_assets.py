import ast
import unittest
from pathlib import Path
from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer

root=Path(__file__).resolve().parents[1]
tree=ast.parse((root/'miniapp.py').read_text())
function=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='asset')
ns={'web':web,'ASSETS':root/'assets','ASSET_NAMES':frozenset(('characters.jpeg','background.jpeg','black-russia.jpeg','rakcrmp.webp','characters-cutout.webp'))}
exec(compile(ast.Module(body=[function],type_ignores=[]),'miniapp.py','exec'),ns)

class AssetsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app=web.Application();app.router.add_get('/assets/{name}',ns['asset'])
        self.client=TestClient(TestServer(app));await self.client.start_server()
    async def asyncTearDown(self): await self.client.close()
    async def test_files_and_unknown_file(self):
        for name in ns['ASSET_NAMES']:
            r=await self.client.get('/assets/'+name)
            self.assertEqual(r.status,200)
            self.assertGreater(len(await r.read()),1000)
            self.assertEqual(r.headers['X-Content-Type-Options'],'nosniff')
        self.assertEqual((await self.client.get('/assets/bot.py')).status,404)
    def test_html_references_and_functional_elements(self):
        html=(root/'miniapp.html').read_text()
        assign=next(n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='ASSET_NAMES' for t in n.targets))
        self.assertEqual(ns['ASSET_NAMES'],frozenset(ast.literal_eval(assign.value.args[0])))
        for name in ns['ASSET_NAMES'] - {'characters.jpeg'}: self.assertIn('/assets/'+name,html)
        for ident in ('catalog','buy','captcha-view','payment-check'):self.assertIn('id="'+ident+'"',html)
