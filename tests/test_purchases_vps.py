import asyncio
import json
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import miniapp
from storage import Store

class PurchaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:');self.store.init_shop();miniapp.init_tables(self.store)
        self.oid='a'*24
        self.store.db.execute('INSERT INTO star_orders(id,buyer,product_id,stars,name,file_id,filename,instruction,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)', (self.oid,42,'p',10,'Script','file-identifier','work.lua','hello','paid',time.time()))
        self.store.db.commit()
        self.file=NS(file_size=10,download_to_memory=AsyncMock(side_effect=lambda out: out.write(b'print(1)')))
        self.bot=NS(get_file=AsyncMock(return_value=self.file))
        app=web.Application();app['store']=self.store;app['bot']=self.bot
        app.router.add_get('/purchases',miniapp.purchases);app.router.add_post('/install',miniapp.install_purchase);app.router.add_post('/stars',miniapp.check_stars)
        async def auth(request,require_subscription=True): return int(request.headers['uid'])
        self.patcher=patch('miniapp.authorize',auth);self.patcher.start()
        self.calls=[]
        async def execute(uid,sid,data):
            self.calls.append((uid,sid,data))
            if uid!=42 or sid!='own':raise web.HTTPUnauthorized()
            return {'lua_ready':True} if data['action']=='list' else {'saved':data['name']}
        self.execpatch=patch('vps_lua.execute',side_effect=execute);self.execpatch.start()
        self.client=TestClient(TestServer(app));await self.client.start_server()
    async def asyncTearDown(self):
        await self.client.close();self.execpatch.stop();self.patcher.stop();self.store.db.close()
    async def test_paid_only_and_server_file_installed(self):
        r=await self.client.get('/purchases',headers={'uid':'42'});data=await r.json();self.assertEqual(len(data['purchases']),1);self.assertNotIn('file_id',str(data))
        body={'kind':'stars','order_id':self.oid,'session_id':'own','text':'malicious replacement','filename':'evil.lua'}
        r=await self.client.post('/install',json=body,headers={'uid':'42'});self.assertEqual(r.status,200)
        self.assertEqual(self.calls[-1][2],{'action':'upload','name':'work.lua','text':'print(1)'})
        r=await self.client.post('/install',json=body,headers={'uid':'43'});self.assertEqual(r.status,404)
        self.store.db.execute("UPDATE star_orders SET state='pending'");self.store.db.commit()
        r=await self.client.post('/install',json=body,headers={'uid':'42'});self.assertEqual(r.status,404)
    async def test_cannot_use_another_session(self):
        body={'kind':'stars','order_id':self.oid,'session_id':'other'}
        r=await self.client.post('/install',json=body,headers={'uid':'42'});self.assertEqual(r.status,401)
        self.bot.get_file.assert_not_awaited()
    async def test_missing_loader_does_not_download(self):
        with patch('vps_lua.execute',AsyncMock(return_value={'lua_ready':False})):
            r=await self.client.post('/install',json={'kind':'stars','order_id':self.oid,'session_id':'own'},headers={'uid':'42'})
        self.assertEqual(r.status,409)
        self.bot.get_file.assert_not_awaited()

    async def test_unsupported_file_and_size(self):
        body={'kind':'stars','order_id':self.oid,'session_id':'own'}
        self.file.file_size=300000
        r=await self.client.post('/install',json=body,headers={'uid':'42'});self.assertEqual(r.status,400)
        self.store.db.execute("UPDATE star_orders SET filename='bad.zip'");self.store.db.commit()
        r=await self.client.post('/install',json=body,headers={'uid':'42'});self.assertEqual(r.status,400)
    async def test_external_purchases_require_paid_and_matching_user(self):
        oid='c'*24
        self.store.order_add({'id':oid,'buyer':42,'product_id':'p','name':'External','price_kopecks':5000,
                              'file_id':'external-file','filename':'external.lua','instruction':'',
                              'provider':'crypto','provider_id':'1','payment_url':'https://example.com',
                              'state':'pending','created_at':time.time()})
        self.assertIsNone(miniapp.purchased(self.store,42,'external',oid))
        self.store.mark_paid(oid)
        self.assertEqual(miniapp.purchased(self.store,42,'external',oid)['file_id'],'external-file')
        self.assertIsNone(miniapp.purchased(self.store,43,'external',oid))
        self.assertIsNone(miniapp.purchased(self.store,42,'stars',oid))

    async def test_star_status_owner(self):
        r=await self.client.post('/stars',json={'order_id':self.oid},headers={'uid':'42'});self.assertEqual((await r.json())['state'],'paid')
        r=await self.client.post('/stars',json={'order_id':self.oid},headers={'uid':'43'});self.assertEqual(r.status,404)

class PurchaseIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_paid_file_arrives_in_script_directory(self):
        import tempfile
        from pathlib import Path
        import vps_lua, vps_lua_remote as remote
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'bot.py').write_text('');(root/'logic').mkdir();(root/'logic/lua_runtime.py').write_text('')
            store=Store(':memory:');store.init_shop();miniapp.init_tables(store);oid='b'*24
            store.db.execute('INSERT INTO star_orders(id,buyer,product_id,stars,name,file_id,filename,instruction,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(oid,42,'p',10,'Example','tg_file','demo.lua','', 'delivered',time.time()));store.db.commit()
            class Conn:
                def is_closed(self):return False
                def close(self):pass
                async def run(self,command,input=None,timeout=None):
                    try: response={'ok':True,'result':remote.process(json.loads(input))}
                    except Exception: response={'ok':False,'error':'upload failed'}
                    return NS(exit_status=0,stdout=json.dumps(response))
            vps_lua.SESSIONS['own-test']={'uid':42,'conn':Conn(),'root':True,'expires':time.monotonic()+60,'lock':asyncio.Lock()}
            f=NS(file_size=10,download_to_memory=AsyncMock(side_effect=lambda out:out.write(b'print(1)')))
            app=web.Application();app['store']=store;app['bot']=NS(get_file=AsyncMock(return_value=f));app.router.add_post('/install',miniapp.install_purchase)
            async def auth(req,require_subscription=True):return 42
            with patch('miniapp.authorize',auth),patch.object(remote,'ROOT',root):
                client=TestClient(TestServer(app));await client.start_server()
                try:
                    r=await client.post('/install',json={'kind':'stars','order_id':oid,'session_id':'own-test'});self.assertEqual(r.status,200,await r.text())
                    self.assertEqual((root/'scripts/lua/demo.lua').read_text(),'print(1)')
                    r=await client.post('/install',json={'kind':'stars','order_id':oid,'session_id':'own-test'});self.assertEqual(r.status,200);self.assertTrue((await r.json())['already'])
                    self.assertEqual((root/'scripts/lua/demo.lua').read_text(),'print(1)')
                finally:
                    session=vps_lua.SESSIONS.pop('own-test',None)
                    if session and session.get('timer'):session['timer'].cancel()
                    await client.close();store.db.close()
