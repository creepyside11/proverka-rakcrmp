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
MODEL_ATTEMPTS=5
MODEL_RETRY_DELAY=10


class EmptyModelResponse(Exception):
    """Provider returned a billed response without usable content."""
    def __init__(self,reason=''):
        super().__init__('empty response')
        self.reason=reason



async def retry_empty_model(call,emit,on_retry=None):
    for attempt in range(1,MODEL_ATTEMPTS+1):
        try:
            return await call()
        except EmptyModelResponse as exc:
            if attempt==MODEL_ATTEMPTS:
                raise web.HTTPBadGateway(text='Модель вернула пустой ответ 5 раз. Задание остановлено; каждая подтверждённая попытка учтена.')
            if on_retry is not None:on_retry(exc,attempt)
            emit('status',f'Модель вернула пустой ответ. Сокращаю контекст; попытка {attempt}/{MODEL_ATTEMPTS}, повтор через {MODEL_RETRY_DELAY} секунд.')
            await asyncio.sleep(MODEL_RETRY_DELAY)



def init(store):
    store.db.executescript('''CREATE TABLE IF NOT EXISTS creator_messages (
      message_id TEXT PRIMARY KEY, owner_id BIGINT NOT NULL, role TEXT NOT NULL, body TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS creator_files (owner_id BIGINT PRIMARY KEY, body TEXT NOT NULL, updated_at DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS creator_chats (chat_id TEXT PRIMARY KEY, owner_id BIGINT NOT NULL, title TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS creator_chat_messages (message_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, owner_id BIGINT NOT NULL, role TEXT NOT NULL, body TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS creator_chat_files (chat_id TEXT NOT NULL, owner_id BIGINT NOT NULL, body TEXT NOT NULL, updated_at DOUBLE PRECISION NOT NULL, PRIMARY KEY(chat_id,owner_id));''')
    store.db.commit()


def add(store,uid,role,body,chat='legacy'):
    if chat=='legacy':
        store.db.execute('INSERT INTO creator_messages(message_id,owner_id,role,body,created_at) VALUES(?,?,?,?,?)',(secrets.token_hex(16),uid,role,body,time.time()))
    else:
        store.db.execute('INSERT INTO creator_chat_messages(message_id,chat_id,owner_id,role,body,created_at) VALUES(?,?,?,?,?,?)',(secrets.token_hex(16),chat,uid,role,body,time.time()))
    store.db.commit()


def history(store,uid,limit=40,*,include_events=True,chat='legacy'):
    clause='' if include_events else " AND role IN ('user','assistant')"
    table='creator_messages' if chat=='legacy' else 'creator_chat_messages'
    scope='' if chat=='legacy' else ' AND chat_id=?'
    args=(uid,limit) if chat=='legacy' else (uid,chat,limit)
    rows=store.db.execute('SELECT role,body FROM '+table+' WHERE owner_id=?'+scope+clause+' ORDER BY created_at DESC,message_id DESC LIMIT ?', args)
    return [dict(r) for r in reversed(list(rows))]


def file_for(store,uid,chat='legacy'):
    row=store.db.execute('SELECT body FROM creator_files WHERE owner_id=?',(uid,)).fetchone() if chat=='legacy' else store.db.execute('SELECT body FROM creator_chat_files WHERE owner_id=? AND chat_id=?',(uid,chat)).fetchone()
    return row['body'] if row else ''


def save_file(store,uid,code,chat='legacy'):
    if chat=='legacy':store.db.execute('INSERT INTO creator_files(owner_id,body,updated_at) VALUES(?,?,?) ON CONFLICT(owner_id) DO UPDATE SET body=excluded.body,updated_at=excluded.updated_at',(uid,code,time.time()))
    else:store.db.execute('INSERT INTO creator_chat_files(chat_id,owner_id,body,updated_at) VALUES(?,?,?,?) ON CONFLICT(chat_id,owner_id) DO UPDATE SET body=excluded.body,updated_at=excluded.updated_at',(chat,uid,code,time.time()))
    store.db.commit()


def checked_chat(store,uid,chat):
    if chat=='legacy':return chat
    if not isinstance(chat,str) or not store.db.execute('SELECT chat_id FROM creator_chats WHERE chat_id=? AND owner_id=?',(chat,uid)).fetchone():
        raise web.HTTPNotFound(text='Чат не найден.')
    return chat


async def chats(request,authorize):
    uid=await authorize(request,require_subscription=False);store=request.app['store']
    if request.method=='POST':
        chat=secrets.token_urlsafe(18)
        store.db.execute('INSERT INTO creator_chats(chat_id,owner_id,title,created_at) VALUES(?,?,?,?)',(chat,uid,'Новый чат',time.time()));store.db.commit()
        return web.json_response({'id':chat,'title':'Новый чат'},headers={'Cache-Control':'no-store'})
    rows=store.db.execute('SELECT chat_id,title FROM creator_chats WHERE owner_id=? ORDER BY created_at DESC',(uid,))
    return web.json_response({'chats':[{'id':r['chat_id'],'title':r['title']} for r in rows]+[{'id':'legacy','title':'Предыдущий чат'}]},headers={'Cache-Control':'no-store'})


async def progress(request,authorize):
    uid=await authorize(request,require_subscription=False)
    run=RUNS.get(uid)
    chat=checked_chat(request.app['store'],uid,request.query.get('chat','legacy'))
    if run and run.get('chat','legacy')!=chat:run=None
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


async def paid_model(store,bot_token,uid,selected,messages,*,context_size=16000,max_output=1024,retry_hint=False):
    if selected not in ai_models.MODELS:raise web.HTTPBadRequest(text='Выберите купленную модель.')
    row=store.db.execute('SELECT order_ref FROM ai_keys WHERE buyer=? AND model_key=? AND balance>0 AND in_flight=0 ORDER BY balance DESC,created_at DESC LIMIT 1',(uid,selected)).fetchone()
    if not row:raise web.HTTPPaymentRequired(text='Нет доступного оплаченного ключа для этой модели.')
    key=ai_gateway.key_for(bot_token,row['order_ref'])
    # Preserve the current task even after long tool transcripts.
    anchor=next((m for m in reversed(messages) if m.get('role')=='user' and m.get('body','').startswith('Задача пользователя: ')),None)
    tail=messages[-18:] if anchor else messages[-19:]
    if anchor:tail=[m for m in tail if m is not anchor]
    reserve=min(len(anchor['body']),max(0,context_size-2000)) if anchor else 0
    bounded=[];size=0
    for m in reversed(tail):
        if m['role'] not in ('user','assistant'):continue
        remaining=context_size-reserve-size
        if remaining<=0:break
        body=m['body']
        if len(body)>remaining:
            if m['role']=='user' and body.startswith('Результат инструмента:') and remaining>200:
                body=body[:remaining-50]+'\n[вывод сокращён; запросите нужные строки]'
            else:
                continue
        bounded.append({'role':m['role'],'content':body});size+=len(body)
    ordered=list(reversed(bounded))
    if reserve:ordered.insert(0,{'role':'user','content':anchor['body'][:reserve]})
    if retry_hint and len(ordered)<19:ordered.append({'role':'user','content':'Продолжи работу. Верни одно следующее действие в корректном JSON.'})
    payload={'model':selected,'max_tokens':max_output,'messages':[{'role':'system','content':PROMPT}]+ordered}
    async def get_json():return payload
    fake=SimpleNamespace(headers={'Authorization':'Bearer '+key},app={'store':store,'token':bot_token},json=get_json)
    result=await ai_gateway.completions(fake,max_tokens_limit=4096)
    value=json.loads(result.body);choices=value.get('choices') or []
    text=(choices[0].get('message') or {}).get('content') if choices else None
    if not isinstance(text,str) or not text.strip():
        reason=choices[0].get('finish_reason','') if choices and isinstance(choices[0],dict) else ''
        raise EmptyModelResponse(reason)
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
    if request.method=='GET':
        chat=checked_chat(store,uid,request.query.get('chat','legacy'))
        return web.json_response({'messages':history(store,uid,chat=chat),'file':file_for(store,uid,chat)},headers={'Cache-Control':'no-store'})
    try:data=await request.json()
    except (TypeError,ValueError):raise web.HTTPBadRequest(text='Некорректный запрос.')
    text=data.get('text') if isinstance(data,dict) else None
    if not isinstance(text,str) or not 1<=len(text.strip())<=4000:raise web.HTTPBadRequest(text='Введите сообщение длиной до 4000 символов.')
    chat=checked_chat(store,uid,data.get('chat','legacy'))
    selected=data.get('model','')
    if selected not in ai_models.MODELS:raise web.HTTPBadRequest(text='Выберите модель, для которой куплены токены.')
    async def generate(items):
        settings={'context_size':16000,'max_output':1024,'retry_hint':False}
        async def once():
            async with support_ai.SLOTS:
                return await paid_model(store,request.app['token'],uid,selected,items,**settings)
        def adapt(exc,attempt):
            settings['context_size']=max(6000,settings['context_size']//2)
            settings['retry_hint']=True
            if exc.reason=='length':settings['max_output']=min(4096,settings['max_output']*2)
        return await retry_empty_model(once,emit,adapt)
    now=time.monotonic();recent=[x for x in RATE.get(uid,[]) if x>now-60]
    if len(recent)>=3:raise web.HTTPTooManyRequests(text='Подождите минуту перед следующим запросом.')
    if RUNS.get(uid,{}).get('state')=='running':raise web.HTTPConflict(text='Предыдущее задание ещё выполняется.')
    if len(RATE)>10000:
        for k,v in list(RATE.items()):
            if not v or v[-1]<=now-60:RATE.pop(k,None)
    for k,v in list(RUNS.items()):
        if v.get('state')!='running' and v.get('started',0)<now-3600:RUNS.pop(k,None)
    RATE[uid]=recent+[now]
    run={'id':secrets.token_urlsafe(18),'state':'running','events':[],'started':now,'sequence':0,'chat':chat};RUNS[uid]=run
    def emit(kind,message):
        run['sequence']+=1
        event={'id':run['id']+':'+str(run['sequence']),'kind':kind,'message':str(message)[:1200]}
        run['events'].append(event)
        if len(run['events'])>32:del run['events'][:-32]
        # Visible action summaries are persisted as chat events, without model reasoning.
        try:add(store,uid,'event',json.dumps(event,ensure_ascii=False),chat)
        except Exception:pass
    async def execute():
        try:
            async with LOCKS[uid%len(LOCKS)]:
                add(store,uid,'user',text.strip(),chat);items=history(store,uid,16,include_events=False,chat=chat)
                if chat!='legacy':
                    store.db.execute("UPDATE creator_chats SET title=? WHERE chat_id=? AND owner_id=? AND title=?",(text.strip()[:45],chat,uid,'Новый чат'));store.db.commit()
                result=await creator_agent.run(generate,check_syntax,text.strip(),file_for(store,uid,chat),items[:-1],emit)
                if result['changed']:
                    save_file(store,uid,result['code'],chat)
                answer=result['answer'];code=result['code'];check=result['check']
                add(store,uid,'assistant',answer,chat)
                options=result.get('options') or []
                if options:add(store,uid,'options',json.dumps(options,ensure_ascii=False),chat)
                run['state']='done'
                run['result']={'ok':True,'answer':answer,'code':code,'check':check,'options':options,'events':list(run['events'])}
        except asyncio.CancelledError:
            emit('error','Задание прервано; ранее сохранённый файл не изменён.');run['state']='failed'
            run['result']={'ok':False,'message':'Задание прервано.'}
            raise
        except web.HTTPException as exc:
            message=exc.text or 'Не удалось получить ответ ИИ.'
            add(store,uid,'notice',message,chat);emit('error',message);run['state']='failed'
            run['result']={'ok':False,'message':message}
        except Exception:
            add(store,uid,'notice','Не удалось получить ответ ИИ. Попробуйте ещё раз.',chat)
            emit('error','Не удалось завершить задание. Проверьте историю перед повтором.');run['state']='failed'
            run['result']={'ok':False,'message':'Не удалось получить ответ ИИ. Попробуйте ещё раз.'}
        return run.get('result')
    if data.get('background') is True:
        run['task']=asyncio.create_task(execute())
        return web.json_response({'ok':True,'started':True,'run_id':run['id']},headers={'Cache-Control':'no-store'})
    result=await execute()
    return web.json_response(result,headers={'Cache-Control':'no-store'})
