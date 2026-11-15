import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from storage import Store
import ai_models
import support_ai

class ModelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):self.store=Store(':memory:');self.store.init_shop();ai_models.init(self.store);self.context=NS(user_data={})
    def tearDown(self):self.store.db.close()
    def update(self,text='',uid=12):
        msg=NS(chat=NS(type='private'),text=text,reply_text=AsyncMock())
        return NS(effective_message=msg,effective_user=NS(id=uid)),msg
    async def test_configuration_and_token_not_plaintext(self):
        q=NS(data='aimodel:glm',message=NS(chat=NS(type='private'),reply_text=AsyncMock()),answer=AsyncMock())
        await ai_models.choose(NS(callback_query=q,effective_user=NS(id=12)),self.context,self.store,12)
        for text in ('https://example.com/v1','glm-real-id','secret-provider-token'):
            u,_=self.update(text);self.assertTrue(await ai_models.input(u,self.context,self.store,12,'bot-token'))
        cfg=ai_models.config(self.store,'glm')
        self.assertEqual(cfg['price_kopecks'],1);self.assertNotIn('secret-provider-token',str(cfg))
        self.assertEqual(support_ai.crypt('bot-token').decrypt(cfg['token_cipher'].encode()).decode(),'secret-provider-token')
    async def test_only_admin_can_configure_and_cancel(self):
        self.context.user_data['ai_model_draft']={'step':'base','key':'glm'}
        u,_=self.update('https://example.com',uid=99)
        self.assertFalse(await ai_models.input(u,self.context,self.store,12,'bot-token'))
        u,_=self.update('/cancel');self.assertTrue(await ai_models.input(u,self.context,self.store,12,'bot-token'))
        self.assertNotIn('ai_model_draft',self.context.user_data)
    async def test_reject_private_base_and_configure_without_price(self):
        self.context.user_data['ai_model_draft']={'step':'base','key':'glm'}
        u,_=self.update('http://127.0.0.1');await ai_models.input(u,self.context,self.store,12,'bot-token')
        self.assertEqual(self.context.user_data['ai_model_draft']['step'],'base')
        for text in ('https://example.com/v1','glm','token'):
            u,_=self.update(text);await ai_models.input(u,self.context,self.store,12,'bot-token')
        self.assertNotIn('ai_model_draft',self.context.user_data);self.assertIsNotNone(ai_models.config(self.store,'glm'))
