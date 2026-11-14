"""Separate Lua authoring conversation. Never executes generated code."""
import asyncio
import re
import shutil
import tempfile
from pathlib import Path
import secrets
import time
import json
import importlib.util
import sys
from types import SimpleNamespace
import ai_gateway
import creator_agent
import ai_models
from aiohttp import web
import support_ai

PROMPT = '''Ты помощник по созданию Lua-скриптов для проекта RakCRMP Black. Отвечай по-русски. Не выдумывай API: если не уверен в имени функции или данных игрового протокола, явно скажи об этом и попроси сведения. Скрипт рассчитан на существующий загрузчик с обработчиками on_load(e), on_spawn(e), on_chat(e), где текст чата находится в e.message. Доступны br.log(text), br.after(seconds, name, data), br.walk(x,y,z), br.drive(x,y,z), br.dialog(id,response,item,text), br.chat(text), br.setting(key,default). Изменения игровых действий должны проходить через API загрузчика. Не обещай работоспособность по результатам проверки синтаксиса. Не проси ключи и пароли.''' + '\n' + creator_agent.AGENT_PROTOCOL
LOCKS=[asyncio.Lock() for _ in range(64)]
RATE={}
RUNS={}
MAX_CODE=128*1024


def init(store):
    store.db.executescript('''CREATE TABLE IF NOT EXISTS creator_messages (
      message_id TEXT PRIMARY KEY, owner_id BIGINT NOT NULL, role TEXT NOT NULL, body TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS creator_files (owner_id BIGINT PRIMARY KEY, body TEXT NOT NULL, updated_at DOUBLE PRECISION NOT NULL);''')
    store.db.commit()


def add(store,uid,role,body):
    store.db.execute('INSERT INTO creator_messages(message_id,owner_id,role,body,created_at) VALUES(?,?,?,?,?)',(secrets.token_hex(16),uid,role,body,time.time()))
    store.db.commit()


def history(store,uid,limit=40):
    rows=store.db.execute('SELECT role,body FROM creator_messages WHERE owner_id=? ORDER BY created_at DESC,message_id DESC LIMIT ?', (uid,limit))
    return [dict(r) for r in reversed(list(rows))]


def file_for(store,uid):
    row=store.db.execute('SELECT body FROM creator_files WHERE owner_id=?',(uid,)).fetchone()
    return row['body'] if row else ''


def save_file(store,uid,code):
    store.db.execute('INSERT INTO creator_files(owner_id,body,updated_at) VALUES(?,?,?) ON CONFLICT(owner_id) DO UPDATE SET body=excluded.body,updated_at=excluded.updated_at',(uid,code,time.time()))
    store.db.commit()


async def progress(request,authorize):
    uid=await authorize(request,require_subscription=False)
    run=RUNS.get(uid)
    requested=request.query.get('run','')
    if requested and (not run or run['id']!=requested):raise web.HTTPNotFound(text='Задание не найдено или уже устарело.')
    return web.json_response({'id':run['id'] if run else None,'state':run['state'] if run else 'idle','events':list(run['events']) if run else [],'result':run.get('result') if run else None},headers={'Cache-Control':'no-store'})


def extract_code(text):
    match=re.search(r'```(?:lua)?[ \t]*\n([\s\S]*?)```',text,re.IGNORECASE)
    return match.group(1) if match else None


async def check_syntax(code):
    if len(code.encode('utf-8')) > MAX_CODE:return {'status':'unavailable','message':'Скрипт слишком большой для проверки.'}
    binary=shutil.which('luac5.4') or shutil.which('luac54')
    if not binary:
        try: lua_available=importlib.util.find_spec('lupa.lua54') is not None
        except (ImportError,ModuleNotFoundError,ValueError): lua_available=False
        if not lua_available:return {'status':'unavailable','message':'Проверка синтаксиса недоступна: на сервере нет Lua 5.4.'}

    with tempfile.TemporaryDirectory(prefix='rak-lua-check-') as tmp:
        path=Path(tmp)/'script.lua';path.write_text(code,encoding='utf-8')
        if binary:
            command=[binary,'-p',str(path)]
        else:
            # LuaRuntime.compile compiles text only; it never invokes the chunk.
            parser="from lupa.lua54 import LuaRuntime; import sys; LuaRuntime(register_eval=False,register_builtins=False,max_memory=16777216).compile(open(sys.argv[1],encoding='utf-8').read(),name='script.lua',mode='t')"
            command=[sys.executable,'-c',parser,str(path)]
        proc=await asyncio.create_subprocess_exec(*command,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        try: out,err=await asyncio.wait_for(proc.communicate(),timeout=3)
        except asyncio.TimeoutError:
            proc.kill();await proc.communicate();return {'status':'unavailable','message':'Превышено время проверки синтаксиса.'}
        detail=err.decode('utf-8','replace').replace(str(path),'script.lua')[:600]
        return {'status':'ok' if proc.returncode==0 else 'error','message':'Синтаксис Lua 5.4 корректен. Поведение и игровые действия не проверены.' if proc.returncode==0 else detail or 'Ошибка синтаксиса Lua.'}


async def paid_model(store,bot_token,uid,selected,messages):
    if selected not in ai_models.MODELS:raise web.HTTPBadRequest(text='Выберите купленную модель.')
    row=store.db.execute('SELECT order_ref FROM ai_keys WHERE buyer=? AND model_key=? AND balance>0 AND in_flight=0 ORDER BY balance DESC,created_at DESC LIMIT 1',(uid,selected)).fetchone()
    if not row:raise web.HTTPPaymentRequired(text='Нет доступного оплаченного ключа для этой модели.')
    key=ai_gateway.key_for(bot_token,row['order_ref'])
    bounded=[];size=0
    for m in reversed(messages[-19:]):
        if m['role'] not in ('user','assistant'):continue
        remaining=16000-size
        if remaining<=0:break
        body=m['body'][-remaining:];bounded.append({'role':m['role'],'content':body});size+=len(body)
    payload={'model':selected,'max_tokens':1024,'messages':[{'role':'system','content':PROMPT}]+list(reversed(bounded))}
    async def get_json():return payload
    fake=SimpleNamespace(headers={'Authorization':'Bearer '+key},app={'store':store,'token':bot_token},json=get_json)
    result=await ai_gateway.completions(fake)
    value=json.loads(result.body);choices=value.get('choices') or []
    text=(choices[0].get('message') or {}).get('content') if choices else None
    if not isinstance(text,str) or not text.strip():raise ValueError('empty response')
    return text.strip()[:24000]


async def models(request,authorize):
    uid=await authorize(request,require_subscription=False)
    rows=request.app['store'].db.execute('SELECT model_key,balance FROM ai_keys WHERE buyer=? AND in_flight=0 AND balance>0 ORDER BY balance DESC',(uid,))
    best={}
    for row in rows:
        key=row['model_key']
        if key in ai_models.MODELS and key not in best:best[key]={'model':key,'name':ai_models.MODELS[key],'balance':row['balance']}
    return web.json_response({'models':list(best.values())},headers={'Cache-Control':'no-store'})


async def messages(request,authorize):
    uid=await authorize(request,require_subscription=False);store=request.app['store']
    if request.method=='GET':return web.json_response({'messages':history(store,uid)},headers={'Cache-Control':'no-store'})
    try:data=await request.json()
    except (TypeError,ValueError):raise web.HTTPBadRequest(text='Некорректный запрос.')
    text=data.get('text') if isinstance(data,dict) else None
    if not isinstance(text,str) or not 1<=len(text.strip())<=4000:raise web.HTTPBadRequest(text='Введите сообщение длиной до 4000 символов.')
    selected=data.get('model','')
    if selected not in ai_models.MODELS:raise web.HTTPBadRequest(text='Выберите модель, для которой куплены токены.')
    async def generate(items):
        async with support_ai.SLOTS:
            return await paid_model(store,request.app['token'],uid,selected,items)
    now=time.monotonic();recent=[x for x in RATE.get(uid,[]) if x>now-60]
    if len(recent)>=3:raise web.HTTPTooManyRequests(text='Подождите минуту перед следующим запросом.')
    if RUNS.get(uid,{}).get('state')=='running':raise web.HTTPConflict(text='Предыдущее задание ещё выполняется.')
    if len(RATE)>10000:
        for k,v in list(RATE.items()):
            if not v or v[-1]<=now-60:RATE.pop(k,None)
    for k,v in list(RUNS.items()):
        if v.get('state')!='running' and v.get('started',0)<now-3600:RUNS.pop(k,None)
    RATE[uid]=recent+[now]
    run={'id':secrets.token_urlsafe(18),'state':'running','events':[],'started':now};RUNS[uid]=run
    def emit(kind,message):
        run['events'].append({'kind':kind,'message':str(message)[:1200]})
        if len(run['events'])>32:del run['events'][:-32]
    async def execute():
        try:
            async with LOCKS[uid%len(LOCKS)]:
                add(store,uid,'user',text.strip());items=history(store,uid,16)
                result=await creator_agent.run(generate,check_syntax,text.strip(),file_for(store,uid),items[:-1],emit)
                if result['changed']:
                    save_file(store,uid,result['code'])
                answer=result['answer'];code=result['code'];check=result['check']
                add(store,uid,'assistant',answer)
                emit('status','Задание завершено.')
                run['state']='done'
                run['result']={'ok':True,'answer':answer,'code':code,'check':check,'events':list(run['events'])}
        except asyncio.CancelledError:
            emit('error','Задание прервано; ранее сохранённый файл не изменён.');run['state']='failed'
            run['result']={'ok':False,'message':'Задание прервано.'}
            raise
        except web.HTTPException as exc:
            message=exc.text or 'Не удалось получить ответ ИИ.'
            add(store,uid,'notice',message);emit('error',message);run['state']='failed'
            run['result']={'ok':False,'message':message}
        except Exception:
            add(store,uid,'notice','Не удалось получить ответ ИИ. Попробуйте ещё раз.')
            emit('error','Не удалось завершить задание. Проверьте историю перед повтором.');run['state']='failed'
            run['result']={'ok':False,'message':'Не удалось получить ответ ИИ. Попробуйте ещё раз.'}
        return run.get('result')
    if data.get('background') is True:
        run['task']=asyncio.create_task(execute())
        return web.json_response({'ok':True,'started':True,'run_id':run['id']},headers={'Cache-Control':'no-store'})
    result=await execute()
    return web.json_response(result,headers={'Cache-Control':'no-store'})
