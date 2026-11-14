import json
import unittest
from unittest.mock import AsyncMock, patch
from aiohttp import web
import creator_agent
import script_creator


def action(kind,**values):return json.dumps({'action':kind,**values},ensure_ascii=False)

class AgentToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_find_replace_diff_checkpoint_restore_and_lines(self):
        answers=[action('plan',steps=['Найти','Исправить','Проверить']),action('read_lines',start=1,count=5),action('search_file',text='print'),action('outline'),action('checkpoint'),action('replace_text',old='print(1)',new='print(2)'),action('show_diff'),action('restore_checkpoint'),action('replace_text',old='print(1)',new='print(3)'),action('check_file'),action('finish',message='Готово')]
        model=AsyncMock(side_effect=answers);check=AsyncMock(return_value={'status':'ok','message':'ok'});events=[]
        code='function on_load(e)\n  print(1)\nend\n'
        result=await creator_agent.run(model,check,'исправь',code,[],lambda *x:events.append(x))
        self.assertIn('print(3)',result['code']);self.assertNotIn('print(2)',result['code']);check.assert_awaited_once()
        messages=model.await_args.args[0];combined='\n'.join(x['body'] for x in messages)
        self.assertIn('2:   print(1)',combined);self.assertIn('-  print(1)',combined);self.assertIn('+  print(2)',combined)
        self.assertTrue(any('восстановлена' in msg.lower() for _,msg in events))

    async def test_ambiguous_replace_is_not_applied(self):
        answers=[action('plan',steps=['Исправить']),action('replace_text',old='x',new='y'),action('finish',message='Остановлено')]
        model=AsyncMock(side_effect=answers)
        result=await creator_agent.run(model,AsyncMock(),'задача','x+x',[],lambda *x:None)
        self.assertFalse(result['changed']);self.assertIn('Найдено совпадений: 2',str(model.await_args.args))

class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_five_attempts_wait_exactly_ten_seconds(self):
        call=AsyncMock(side_effect=[script_creator.EmptyModelResponse() for _ in range(4)]+['ok']);events=[]
        with patch('script_creator.asyncio.sleep',new_callable=AsyncMock) as sleep:
            self.assertEqual(await script_creator.retry_empty_model(call,lambda *e:events.append(e)),'ok')
        self.assertEqual(call.await_count,5);self.assertEqual(sleep.await_count,4)
        self.assertTrue(all(x.args==(10,) for x in sleep.await_args_list));self.assertEqual(len(events),4)

    async def test_exhausted_and_ambiguous_fail_closed(self):
        call=AsyncMock(side_effect=script_creator.EmptyModelResponse())
        with patch('script_creator.asyncio.sleep',new_callable=AsyncMock) as sleep:
            with self.assertRaises(web.HTTPBadGateway):await script_creator.retry_empty_model(call,lambda *e:None)
        self.assertEqual(call.await_count,5);self.assertEqual(sleep.await_count,4)
        ambiguous=AsyncMock(side_effect=web.HTTPBadGateway(text='usage unknown'))
        with patch('script_creator.asyncio.sleep',new_callable=AsyncMock) as sleep:
            with self.assertRaises(web.HTTPBadGateway):await script_creator.retry_empty_model(ambiguous,lambda *e:None)
        ambiguous.assert_awaited_once();sleep.assert_not_awaited()

class RetryBillingTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_responses_are_individually_charged(self):
        from storage import Store
        import miniapp, ai_models, ai_gateway, support_ai
        store=Store(':memory:');store.init_shop();miniapp.init_tables(store);token='test-bot-token'
        cipher=support_ai.crypt(token).encrypt(b'test-provider-key').decode()
        ai_models.save(store,token,'glm','https://example.com/v1','provider-model',cipher,10000)
        ai_gateway.issue(store,token,'stars',{'id':'e'*24,'buyer':42,'file_id':'ai:glm'})
        responses=[{'choices':[],'usage':{'total_tokens':100}},{'choices':[{'message':{'content':'  '}}],'usage':{'total_tokens':100}},{'choices':[{'message':{'content':'ok'}}],'usage':{'total_tokens':100}}]
        async def once():return await script_creator.paid_model(store,token,42,'glm',[{'role':'user','body':'тест'}])
        with patch.object(ai_gateway,'upstream',AsyncMock(side_effect=responses)) as upstream,patch('script_creator.asyncio.sleep',new_callable=AsyncMock):
            result=await script_creator.retry_empty_model(once,lambda *e:None)
        self.assertEqual(result,'ok');self.assertEqual(upstream.await_count,3)
        self.assertEqual(ai_gateway.keys(store,token,42)[0]['balance'],999700)
        store.db.close()

class LongTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_request_survives_long_tool_history(self):
        from storage import Store
        from aiohttp import web
        import miniapp, ai_gateway
        store=Store(':memory:');store.init_shop();miniapp.init_tables(store);token='bot-token'
        ai_gateway.issue(store,token,'stars',{'id':'f'*24,'buyer':42,'file_id':'ai:glm'})
        task={'role':'user','body':'Задача пользователя: сделать важную задачу'}
        messages=[task]+[{'role':'user','body':f'Вывод инструмента {i}: '+('a'*1000)} for i in range(30)]
        async def fake(request):
            payload=await request.json()
            return web.json_response({'choices':[{'message':{'content':'ok'}}]})
        with patch.object(ai_gateway,'completions',side_effect=fake) as completion:
            await script_creator.paid_model(store,token,42,'glm',messages)
            request=completion.await_args.args[0];payload=await request.json()
        self.assertTrue(any('важную задачу' in x['content'] for x in payload['messages']))
        self.assertLessEqual(len(payload['messages']),20)
        store.db.close()
