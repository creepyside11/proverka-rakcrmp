"""Exercise actual psycopg placeholder parsing while retaining a local test DB."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from aiohttp import web
from psycopg._queries import _split_query
from storage import Store, PostgresConnection
import ai_gateway, ai_models, free_tokens, miniapp, script_creator, support_ai

class PsycopgCheckedSQLite:
    def __init__(self,inner):self.inner=inner
    def execute(self,sql,params=()):
        if params:_split_query(PostgresConnection.convert(sql).encode('utf-8'),'utf8')
        return self.inner.execute(sql,params)
    def commit(self):return self.inner.commit()
    def close(self):return self.inner.close()

class FreePostgresQueryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:');self.store.init_shop();miniapp.init_tables(self.store)
        self.store.db=PsycopgCheckedSQLite(self.store.db)
        self.token='test-bot-token';cipher=support_ai.crypt(self.token).encrypt(b'provider-token').decode()
        ai_models.save(self.store,self.token,'glm','https://example.com/v1','glm',cipher,1)
        ai_gateway.issue(self.store,self.token,'stars',{'id':'a'*24,'buyer':42,'file_id':'ai:glm'})
    async def asyncTearDown(self):self.store.db.close()
    async def test_claim_list_paid_keys_and_creator_all_parse_as_psycopg(self):
        free_tokens.claim(self.store,self.token,42,'glm')
        request=SimpleNamespace(app={'store':self.store,'token':self.token},method='GET')
        async def auth(request,require_subscription=False):return 42
        status=json.loads((await free_tokens.endpoint(request,auth)).body)['models']
        self.assertEqual(next(x for x in status if x['model']=='glm')['balance'],4_000_000)
        self.assertEqual(len(json.loads((await ai_gateway.account(request,auth)).body)['keys']),1)
        request=SimpleNamespace(app={'store':self.store})
        available=await script_creator.models(request,auth)
        self.assertEqual(json.loads(available.body)['models'][0]['balance'],4_000_000)
        provider={'choices':[{'message':{'content':'Ответ'}}],'usage':{'total_tokens':100}}
        with patch.object(ai_gateway,'upstream',AsyncMock(return_value=provider)):
            result=await script_creator.paid_model(self.store,self.token,42,'glm',[{'role':'user','body':'Привет'}])
        self.assertEqual(result,'Ответ')
        self.assertEqual(next(x for x in free_tokens.state(self.store,42) if x['model']=='glm')['balance'],3_999_900)
