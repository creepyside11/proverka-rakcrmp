import json
import unittest
import creator_agent

class CreatorReferenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_receives_api_v2_reference(self):
        messages_seen=[]
        actions=[{'action':'plan','steps':['Изучить API']},{'action':'read_reference','name':'api_v2'},{'action':'finish','message':'готово'}]
        async def generate(messages):
            messages_seen.append(list(messages))
            return json.dumps(actions.pop(0),ensure_ascii=False)
        async def check(_):return {'status':'ok','message':'проверено'}
        result=await creator_agent.run(generate,check,'тест','',[],lambda *args:None)
        self.assertEqual(result['answer'],'готово')
        self.assertTrue(any('br.storage_get' in message['body'] and 'br.timer_start' in message['body'] for message in messages_seen[-1]))
        self.assertTrue(any('не гарантия' in message['body'] for message in messages_seen[-1]))
