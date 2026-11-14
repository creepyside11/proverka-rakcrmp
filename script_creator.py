"""Separate Lua authoring conversation. Never executes generated code."""
import asyncio
import re
import shutil
import tempfile
from pathlib import Path
import secrets
import time
from aiohttp import web
from cryptography.fernet import InvalidToken
import support_ai

PROMPT = '''Ты помощник по созданию Lua-скриптов для проекта RakCRMP Black. Отвечай по-русски. Давай полный скрипт в одном блоке ```lua. Не выдумывай API: если не уверен в имени функции или данных игрового протокола, явно скажи об этом и попроси сведения. Скрипт рассчитан на существующий загрузчик с обработчиками on_load(e), on_spawn(e), on_chat(e), где текст чата находится в e.message. Доступны br.log(text), br.after(seconds, name, data), br.walk(x,y,z), br.drive(x,y,z), br.dialog(id,response,item,text), br.chat(text), br.setting(key,default). Изменения игровых действий должны проходить через API загрузчика. Не обещай работоспособность по результатам проверки синтаксиса. Не проси ключи и пароли.'''
LOCKS=[asyncio.Lock() for _ in range(64)]
RATE={}
MAX_CODE=128*1024


def init(store):
    store.db.executescript('''CREATE TABLE IF NOT EXISTS creator_messages (
      message_id TEXT PRIMARY KEY, owner_id BIGINT NOT NULL, role TEXT NOT NULL, body TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL);''')
    store.db.commit()


def add(store,uid,role,body):
    store.db.execute('INSERT INTO creator_messages(message_id,owner_id,role,body,created_at) VALUES(?,?,?,?,?)',(secrets.token_hex(16),uid,role,body,time.time()))
    store.db.commit()


def history(store,uid,limit=40):
    rows=store.db.execute('SELECT role,body FROM creator_messages WHERE owner_id=? ORDER BY created_at DESC,message_id DESC LIMIT ?', (uid,limit))
    return [dict(r) for r in reversed(list(rows))]


def extract_code(text):
    match=re.search(r'```(?:lua)?[ \t]*\n([\s\S]*?)```',text,re.IGNORECASE)
    return match.group(1) if match else None


async def check_syntax(code):
    if len(code.encode('utf-8')) > MAX_CODE:return {'status':'unavailable','message':'Скрипт слишком большой для проверки.'}
    binary=shutil.which('luac5.4') or shutil.which('luac54')
    if not binary:return {'status':'unavailable','message':'Проверка синтаксиса недоступна: на сервере нет luac 5.4.'}
    with tempfile.TemporaryDirectory(prefix='rak-lua-check-') as tmp:
        path=Path(tmp)/'script.lua';path.write_text(code,encoding='utf-8')
        proc=await asyncio.create_subprocess_exec(binary,'-p',str(path),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        try: out,err=await asyncio.wait_for(proc.communicate(),timeout=3)
        except asyncio.TimeoutError:
            proc.kill();await proc.communicate();return {'status':'unavailable','message':'Превышено время проверки синтаксиса.'}
        detail=err.decode('utf-8','replace').replace(str(path),'script.lua')[:600]
        return {'status':'ok' if proc.returncode==0 else 'error','message':'Синтаксис Lua 5.4 корректен. Поведение и игровые действия не проверены.' if proc.returncode==0 else detail or 'Ошибка синтаксиса Lua.'}


async def model(cfg,token,messages):
    from openai import AsyncOpenAI
    async with AsyncOpenAI(base_url=cfg['base_url'],api_key=token,timeout=90,max_retries=0) as client:
        result=await client.chat.completions.create(model=cfg['model'],messages=[{'role':'system','content':PROMPT}]+[{'role':m['role'],'content':m['body']} for m in messages if m['role'] in ('user','assistant')][-16:],max_tokens=3000)
    text=result.choices[0].message.content if result.choices else None
    if not isinstance(text,str) or not text.strip():raise ValueError('empty response')
    return text.strip()[:24000]


async def messages(request,authorize):
    uid=await authorize(request,require_subscription=False);store=request.app['store']
    # Until purchased quota exists, do not let arbitrary users spend the support provider's balance.
    if uid!=request.app['admin_id']:raise web.HTTPForbidden(text='Создание скриптов пока доступно только администратору: платный баланс ещё не подключён.')
    if request.method=='GET':return web.json_response({'messages':history(store,uid)},headers={'Cache-Control':'no-store'})
    try:data=await request.json()
    except (TypeError,ValueError):raise web.HTTPBadRequest(text='Некорректный запрос.')
    text=data.get('text') if isinstance(data,dict) else None
    if not isinstance(text,str) or not 1<=len(text.strip())<=4000:raise web.HTTPBadRequest(text='Введите сообщение длиной до 4000 символов.')
    cfg=support_ai.config(store)
    if not cfg or not cfg['token_cipher']:raise web.HTTPServiceUnavailable(text='Администратору нужно настроить ИИ поддержки.')
    try:token=support_ai.crypt(request.app['token']).decrypt(cfg['token_cipher'].encode()).decode()
    except (InvalidToken,ValueError):raise web.HTTPServiceUnavailable(text='Администратору нужно сохранить токен ИИ заново.')
    now=time.monotonic();recent=[x for x in RATE.get(uid,[]) if x>now-60]
    if len(recent)>=3:raise web.HTTPTooManyRequests(text='Подождите минуту перед следующим запросом.')
    if len(RATE)>10000:
        for k,v in list(RATE.items()):
            if not v or v[-1]<=now-60:RATE.pop(k,None)
    RATE[uid]=recent+[now]
    async with LOCKS[uid%len(LOCKS)]:
        add(store,uid,'user',text.strip());items=history(store,uid,16)
        try:
            async with support_ai.SLOTS:answer=await model(cfg,token,items)
            code=extract_code(answer);check=await check_syntax(code) if code else None
            if check and check['status']=='error':
                feedback='Компилятор Lua 5.4 обнаружил ошибку синтаксиса: '+check['message']+'\nИсправь и верни полный скрипт в блоке lua.'
                async with support_ai.SLOTS:corrected=await model(cfg,token,items+[{'role':'assistant','body':answer},{'role':'user','body':feedback}])
                corrected_code=extract_code(corrected)
                if corrected_code:answer=corrected;code=corrected_code;check=await check_syntax(code)
        except Exception:
            add(store,uid,'notice','Не удалось получить ответ ИИ. Попробуйте ещё раз.')
            return web.json_response({'ok':False,'message':'Не удалось получить ответ ИИ. Попробуйте ещё раз.'},headers={'Cache-Control':'no-store'})
        add(store,uid,'assistant',answer)
    return web.json_response({'ok':True,'answer':answer,'code':code,'check':check},headers={'Cache-Control':'no-store'})
