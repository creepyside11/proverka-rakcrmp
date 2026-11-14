"""Bounded, single-file Lua agent. Generated code is never executed."""
import json
import re
from pathlib import Path

MAX_STEPS = 8
MAX_CODE = 32 * 1024
AGENT_PROTOCOL = '''Ты агент создания Lua-скриптов. Используй только следующие инструменты, отвечая ОДНИМ JSON-объектом без markdown:
{"action":"plan","steps":["шаг 1","шаг 2"]} — план из 1–6 коротких пунктов;
{"action":"read_reference","name":"api"} — прочитать API загрузчика; name="example" — посмотреть пример;
{"action":"read_file"} — прочитать текущий script.lua;
{"action":"write_file","content":"полный текст Lua"} — создать или заменить script.lua;
{"action":"append_file","content":"фрагмент Lua"} — добавить фрагмент в конец script.lua;
{"action":"check_file"} — проверить синтаксис текущего script.lua;
{"action":"finish","message":"краткий итог пользователю"} — завершить;
{"action":"ask","message":"каких данных не хватает"} — задать вопрос.
Сначала составь план. Если изменяешь код, запиши его полностью; большой файл записывай частями через append_file. Проверь и исправь ошибки. Используй только инструменты выше. В одном ответе — одно действие. Не выдумывай результат проверки. Никаких скрытых рассуждений в ответе, только действия и краткие пояснения. Не запрашивай пароли и API-ключи. Каждый вызов модели расходует оплаченные токены. Если исходных данных недостаточно, спроси, а не придумывай протокол.'''


def parse_action(text):
    text=text.strip()
    if text.startswith('```'):
        text=re.sub(r'^```(?:json)?\s*', '', text, flags=re.I)
        text=re.sub(r'\s*```\s*$', '', text)
    try: result=json.loads(text)
    except (TypeError,ValueError): return None
    if not isinstance(result,dict) or not isinstance(result.get('action'),str):return None
    return result


def extract_code(text):
    match=re.search(r'```(?:lua)?[ \t]*\n([\s\S]*?)```',text,re.I)
    return match.group(1) if match else None


async def run(generate, check_syntax, user_text, previous_code, history, emit):
    """Interpret the model's restricted actions; return candidate and honest checks."""
    messages=list(history)+[{'role':'user','body':'Задача пользователя: '+user_text}]
    code=previous_code or ''
    changed=False;last_check=None;last_checked_code=None;planned=False
    emit('status','Анализирую задачу и составляю план.')
    for step in range(MAX_STEPS):
        text=await generate(messages)
        action=parse_action(text)
        if action is None:
            # Some compatible providers do not follow JSON; preserve usable text.
            legacy=extract_code(text)
            if legacy is not None and len(legacy.encode('utf-8'))<=MAX_CODE:
                if not planned:
                    emit('plan','1. Подготовить скрипт\n2. Проверить синтаксис')
                    planned=True
                code=legacy;changed=True;last_check=None
                emit('file','Создан script.lua.')
                emit('check','Проверяю синтаксис Lua.')
                last_check=await check_syntax(code);last_checked_code=code
                emit('check',last_check['message'])
                if last_check['status']=='error' and step<MAX_STEPS-1:
                    messages += [{'role':'assistant','body':text},{'role':'user','body':'Ошибка синтаксиса: '+last_check['message']+' Исправь и запиши полный script.lua, затем проверь.'}]
                    continue
                return {'answer':text,'code':code,'changed':changed,'check':last_check}
            if text.lstrip().startswith(('{','```json')):
                emit('status','Уточняю ответ модели и продолжаю работу.')
                messages += [{'role':'assistant','body':text[:4000]},{'role':'user','body':'Ответ должен быть одним корректным JSON-объектом действия. Повтори действие.'}]
                continue
            return {'answer':text[:12000],'code':None,'changed':False,'check':None}
        kind=action['action'];messages.append({'role':'assistant','body':text})
        if not planned and kind in ('write_file','append_file','check_file','finish'):
            messages.append({'role':'user','body':'Сначала составь план действием plan, затем выполняй инструменты.'})
            continue
        if kind=='plan':
            steps=action.get('steps')
            if not isinstance(steps,list) or not 1<=len(steps)<=6 or not all(isinstance(x,str) and 1<=len(x.strip())<=160 for x in steps):
                result='Некорректный план. Передай от 1 до 6 коротких пунктов.'
            else:
                planned=True;emit('plan','\n'.join(f'{i+1}. {item.strip()}' for i,item in enumerate(steps)))
                result='План принят. Выполняй следующий шаг.'
        elif kind=='read_reference':
            name=action.get('name')
            base=Path(__file__).resolve().parent/'black_loader'
            paths={'api':base/'logic/lua/harness.lua','example':base/'scripts/lua/hello_addon.lua'}
            path=paths.get(name) if isinstance(name,str) else None
            if path is None:
                result='Доступны только справочники api и example.'
            else:
                try:
                    reference=path.read_text(encoding='utf-8')
                    if name=='api':
                        reference=reference[reference.find('br.walk ='):reference.find('br.walk =')+11000]
                    result='Справочник '+name+':\n'+reference[:11000]
                    emit('file','Читаю справочник API загрузчика.' if name=='api' else 'Читаю пример скрипта.')
                except OSError:
                    result='Справочник недоступен.'
        elif kind=='read_file':
            emit('file','Читаю текущий script.lua.' if code else 'Текущий script.lua пока не создан.')
            result=('Содержимое script.lua:\n'+code) if code else 'Файл пока не создан.'
        elif kind=='write_file':
            value=action.get('content')
            if not isinstance(value,str) or not value.strip() or len(value.encode('utf-8'))>MAX_CODE:
                result='Файл пуст или больше 32 КБ; изменения не применены.'
            else:
                code=value;changed=True;last_check=None;last_checked_code=None
                emit('file','Обновлён script.lua.');result='Файл обновлён. Проверь синтаксис.'
        elif kind=='append_file':
            value=action.get('content')
            if not isinstance(value,str) or not value or not code or len((code+value).encode('utf-8'))>MAX_CODE:
                result='Нельзя добавить фрагмент: файл отсутствует, фрагмент пуст или превышен размер 32 КБ.'
            else:
                code+=value;changed=True;last_check=None;last_checked_code=None
                emit('file','Добавлен фрагмент в script.lua.');result='Фрагмент добавлен. Продолжай или проверь файл.'
        elif kind=='check_file':
            if not code:result='Файл отсутствует; проверять нечего.'
            else:
                emit('check','Проверяю синтаксис Lua.')
                last_check=await check_syntax(code);last_checked_code=code
                result=f"Проверка: {last_check['status']}. {last_check['message']}"
                emit('check',result)
        elif kind=='finish':
            message=action.get('message')
            if not isinstance(message,str) or not message.strip():message='Работа завершена.'
            if changed and last_checked_code!=code:
                emit('check','Проверяю синтаксис Lua.')
                last_check=await check_syntax(code);last_checked_code=code;emit('check',last_check['message'])
            if changed and last_check and last_check['status']=='error' and step<MAX_STEPS-1:
                result='Файл содержит синтаксическую ошибку: '+last_check['message']+'. Исправь файл.'
            else:
                if changed: message+='\n\n```lua\n'+code.rstrip()+'\n```'
                return {'answer':message[:MAX_CODE+12000], 'code':code if changed else None,'changed':changed,'check':last_check if changed else None}
        elif kind=='ask':
            message=action.get('message')
            if not isinstance(message,str) or not message.strip():message='Уточните недостающие данные для скрипта.'
            return {'answer':message[:4000],'code':None,'changed':False,'check':None}
        else:result='Неизвестное действие. Используй только разрешённые инструменты.'
        if not planned and kind!='plan':result+=' Сначала составь план действием plan.'
        messages.append({'role':'user','body':'Результат инструмента: '+result[:MAX_CODE]})
    if changed:
        if last_checked_code!=code:
            emit('check','Проверяю синтаксис Lua.')
            last_check=await check_syntax(code);emit('check',last_check['message'])
        return {'answer':'Достигнут лимит шагов. Текущая версия скрипта:\n\n```lua\n'+code.rstrip()+'\n```','code':code,'changed':True,'check':last_check}
    return {'answer':'Не удалось завершить задачу за отведённое число шагов. Уточните задачу и попробуйте ещё раз.','code':None,'changed':False,'check':None}
