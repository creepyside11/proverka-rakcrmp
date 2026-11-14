import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import creator_agent
import script_creator


def action(kind,**kw):return json.dumps({'action':kind,**kw},ensure_ascii=False)

class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_read_write_check_repair_finish(self):
        model=AsyncMock(side_effect=[action('plan',steps=['Изучить файл','Написать код','Проверить']),action('read_file'),action('write_file',content='broken'),action('check_file'),action('write_file',content='print(1)'),action('check_file'),action('finish',message='Готово')])
        check=AsyncMock(side_effect=[{'status':'error','message':'ошибка синтаксиса'},{'status':'ok','message':'Синтаксис корректен; поведение не проверено.'}])
        events=[]
        result=await creator_agent.run(model,check,'исправь','old code',[],lambda *v:events.append(v))
        self.assertEqual(result['code'],'print(1)');self.assertEqual(result['check']['status'],'ok')
        self.assertEqual(model.await_count,7);self.assertEqual(check.await_count,2)
        self.assertTrue(any(k=='plan' and 'Изучить файл' in v for k,v in events))
        messages=model.await_args_list[2].args[0]
        self.assertTrue(any('old code' in m['body'] for m in messages))

    async def test_append_and_mandatory_check_before_finish(self):
        model=AsyncMock(side_effect=[action('plan',steps=['Написать','Проверить']),action('write_file',content='print('),action('append_file',content='1)'),action('finish',message='Готово')])
        check=AsyncMock(return_value={'status':'ok','message':'ok'})
        result=await creator_agent.run(model,check,'создай','',[],lambda *x:None)
        self.assertEqual(result['code'],'print(1)');check.assert_awaited_once_with('print(1)')

    async def test_reference_access_is_limited_to_project_examples(self):
        model=AsyncMock(side_effect=[action('plan',steps=['Проверить API']),action('read_reference',name='../../.env'),action('read_reference',name='api'),action('ask',message='Уточните событие')])
        events=[]
        result=await creator_agent.run(model,AsyncMock(),'задача','',[],lambda *x:events.append(x))
        self.assertFalse(result['changed'])
        self.assertTrue(any('Доступны только справочники' in m['body'] for m in model.await_args.args[0]))
        self.assertTrue(any('br.walk' in m['body'] for m in model.await_args.args[0]))
        self.assertTrue(any('справочник API' in msg for _,msg in events))

    async def test_unknown_action_cannot_run_commands(self):
        model=AsyncMock(side_effect=[action('run_shell',command='touch /tmp/nope'),action('ask',message='Уточни событие')])
        result=await creator_agent.run(model,AsyncMock(),'задача','',[],lambda *x:None)
        self.assertEqual(result['answer'],'Уточни событие');self.assertFalse(result['changed'])
        self.assertTrue(any('Неизвестное действие' in m['body'] for m in model.await_args.args[0]))

    async def test_bad_json_retries_and_step_limit_is_bounded(self):
        model=AsyncMock(return_value='{broken')
        result=await creator_agent.run(model,AsyncMock(),'задача','',[],lambda *x:None)
        self.assertEqual(model.await_count,creator_agent.MAX_STEPS)
        self.assertFalse(result['changed'])

    async def test_real_lua_compile_without_running_code(self):
        with tempfile.TemporaryDirectory() as d:
            marker=Path(d)/'executed'
            source=f'local f=io.open("{marker}","w"); if f then f:write("bad") end'
            with patch('script_creator.shutil.which',return_value=None):
                good=await script_creator.check_syntax(source)
                bad=await script_creator.check_syntax('local = end')
            self.assertEqual(good['status'],'ok');self.assertEqual(bad['status'],'error')
            self.assertFalse(marker.exists())

class BillingIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_agent_step_charges_actual_usage(self):
        from storage import Store
        import miniapp, ai_models, ai_gateway, support_ai
        store=Store(':memory:');store.init_shop();miniapp.init_tables(store)
        token='test-bot-token'
        cipher=support_ai.crypt(token).encrypt(b'test-provider-key').decode()
        ai_models.save(store,token,'glm','https://example.com/v1','provider-model',cipher,10000)
        ai_gateway.issue(store,token,'stars',{'id':'c'*24,'buyer':42,'file_id':'ai:glm'})
        answers=[action('plan',steps=['Создать','Проверить']),action('write_file',content='print(1)'),action('check_file'),action('finish',message='Готово')]
        responses=[{'choices':[{'message':{'content':a}}],'usage':{'total_tokens':100}} for a in answers]
        async def generate(items):return await script_creator.paid_model(store,token,42,'glm',items)
        with patch.object(ai_gateway,'upstream',AsyncMock(side_effect=responses)) as upstream:
            result=await creator_agent.run(generate,AsyncMock(return_value={'status':'ok','message':'ok'}),'создай','',[],lambda *x:None)
        self.assertEqual(result['code'],'print(1)');self.assertEqual(upstream.await_count,4)
        self.assertEqual(ai_gateway.keys(store,token,42)[0]['balance'],999600)
        store.db.close()
