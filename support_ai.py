"""Persistent support conversations and an OpenAI-compatible model client."""
import asyncio
import base64
import hashlib
import ipaddress
import io
import logging
import secrets
import time
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from aiohttp import web
from telegram.error import TelegramError

log=logging.getLogger('support_ai')
BASE_PROMPT = '''Ты — ИИ-помощник поддержки RakCRMP. Отвечай по-русски, дружелюбно, короткими понятными шагами, если пользователь не попросил другой язык. Помогай устанавливать и запускать игровой проект Black с Lua, разбирать сообщения об ошибках и пользоваться магазином и мини-приложением. Если не хватает информации, спроси ОС, версию сборки и точный текст ошибки без секретов. Не утверждай, что ты видишь VPS, состояние заказа, логи, платежи, версии или файлы пользователя: у тебя нет инструментов и доступа к ним. Не обещай, что команды точно сработают без проверки.

О проекте: есть игровой Python-проект Black, а также отдельный Telegram-бот/магазин proverka-rakcrmp с Mini App. Полная игровая сборка содержит загрузчик Lua, папку scripts/lua, тестовый полигон test_lua_scripts, ИИ-автокапчу и опциональный мост sing-box для VPN. Python-зависимости устанавливаются через python -m pip install -r requirements.txt; модуль telebot поставляется пакетом pyTelegramBotAPI. Lua 5.4 и sing-box — отдельные системные программы, их не устанавливает pip. sing-box нужен для поддерживаемых VPN-узлов, а не для любого прямого подключения. Начинай с инструкции в Mini App «Инструкция: Windows, Linux, Termux и macOS» или /install-guide. Не предполагай, что ссылка на архив или пакет последней версии уже установлен на устройстве.

VPS: пользователь подключается по SSH в разделе VPS, сверяет отпечаток ключа с панелью хостинга и явно подтверждает действия. Установка рассчитана на заявленную в интерфейсе систему. Не проси прислать пароль SSH, токен Telegram, токен ИИ, куки, ключи или содержимое .env. Не предлагай отключать проверку SSH-ключа. Не предлагай удалять файлы пользователя или переустанавливать рабочую систему без резервной копии и явного согласия. Различай срок работы Bothost, установленный Telegram-бот и игровой процесс на VPS.

Lua: купленные текстовые .lua файлы можно добавить на совместимый VPS через раздел VPS → управление → Lua → Мои покупки после подтверждённой оплаты и подключения. Загрузка не запускает скрипт. Нужен игровой проект с Lua-загрузчиком; текущий закреплённый в установщике исходный Black может его не содержать. ZIP и .luac этим путём не устанавливаются. Настройки работают только если скрипт их читает через br.setting/br.settings; отключение настроек не останавливает скрипт.

ИИ-капча: Base URL должен соответствовать совместимому API, модель — поддерживать изображения; результат не гарантирован. Токен не является ID модели. При ошибке не проси токен: проверь название модели, адрес и тип ошибки без секрета. Для подключения к модели поддержки также нужны адрес, токен и ID модели, задаваемые администратором.

Оплата: статус заказа определяется сервером, а не словами пользователя. Если подтверждение запаздывает, попроси проверить покупку в Mini App, открыть чат с ботом и обратиться к администратору с номером заказа. Не подтверждай и не обещай возврат сам. Telegram Stars — предусмотренный Telegram способ оплаты цифровых товаров. Не придумывай цены, реквизиты, время ответа или статус заказа.\n'''

STRIPES=[asyncio.Lock() for _ in range(64)]
SLOTS=asyncio.Semaphore(6)
LAST={}
MAX_CONTEXT=20


def init(store):
    store.db.executescript('''
    CREATE TABLE IF NOT EXISTS support_tickets (
      number TEXT PRIMARY KEY, owner_id BIGINT NOT NULL, created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS support_messages (
      message_id TEXT PRIMARY KEY, number TEXT NOT NULL, role TEXT NOT NULL, body TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS support_settings (
      id INTEGER PRIMARY KEY, base_url TEXT NOT NULL, model TEXT NOT NULL, token_cipher TEXT NOT NULL, extra_prompt TEXT NOT NULL);
    ''')
    store.db.commit()


def crypt(bot_token):
    key=hashlib.sha256(('rakcrmp-support-key-v1:'+bot_token).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def config(store):
    row=store.db.execute('SELECT * FROM support_settings WHERE id=1').fetchone()
    return dict(row) if row else None


def validate_base(value):
    if not isinstance(value,str) or not 1<=len(value)<=500: raise ValueError('Укажите HTTPS Base URL.')
    try:
        p=urlsplit(value.strip())
        port=p.port
    except ValueError: raise ValueError('Некорректный адрес или порт.')
    if p.scheme!='https' or not p.hostname or p.username or p.password or p.query or p.fragment: raise ValueError('Нужен HTTPS Base URL без пароля и параметров.')
    if p.path.rstrip('/').endswith('/chat/completions'): raise ValueError('Укажите Base URL без /chat/completions на конце.')
    host=p.hostname.lower().rstrip('.')
    if host=='localhost' or host.endswith(('.localhost','.local','.internal')): raise ValueError('Нужен публичный адрес провайдера.')
    try:
        ip=ipaddress.ip_address(host)
        if not ip.is_global or ip.is_multicast: raise ValueError('Нужен публичный адрес провайдера.')
    except ValueError as exc:
        if str(exc)=='Нужен публичный адрес провайдера.': raise
    return value.strip().rstrip('/')


def save_config(store, bot_token, data):
    base=validate_base(data.get('base_url'))
    model=data.get('model');token=data.get('api_token','');extra=data.get('extra_prompt','')
    if not isinstance(model,str) or not 1<=len(model.strip())<=200 or any(c in model for c in '\r\n\x00'): raise ValueError('Укажите ID модели.')
    if not isinstance(token,str) or len(token)>2048 or '\x00' in token: raise ValueError('Некорректный токен.')
    if not isinstance(extra,str) or len(extra)>6000: raise ValueError('Дополнительная инструкция слишком длинная.')
    token=token.strip()
    if token.lower().startswith('bearer '): token=token[7:].strip()
    if token and any(c.isspace() for c in token): raise ValueError('Токен не должен содержать пробелы.')
    current=config(store)
    encrypted=crypt(bot_token).encrypt(token.encode()).decode() if token else (current['token_cipher'] if current else '')
    if not encrypted: raise ValueError('Укажите API Token.')
    if not token:
        try: crypt(bot_token).decrypt(encrypted.encode())
        except (InvalidToken,ValueError): raise ValueError('После смены токена бота укажите API Token заново.')
    store.db.execute('INSERT OR IGNORE INTO support_settings(id,base_url,model,token_cipher,extra_prompt) VALUES(1,?,?,?,?)',(base,model.strip(),encrypted,extra.strip()))
    store.db.execute('UPDATE support_settings SET base_url=?,model=?,token_cipher=?,extra_prompt=? WHERE id=1',(base,model.strip(),encrypted,extra.strip()))
    store.db.commit()


def new_ticket(store,uid):
    now=time.time()
    for _ in range(8):
        number=str(secrets.randbelow(900000000)+100000000)
        row=store.db.execute('INSERT OR IGNORE INTO support_tickets(number,owner_id,created_at,updated_at) VALUES(?,?,?,?)',(number,uid,now,now))
        if row.rowcount:
            store.db.commit();return number
    raise RuntimeError('Не удалось создать обращение')


def get_ticket(store,number):
    if not isinstance(number,str) or not number.isdigit() or len(number)!=9:return None
    row=store.db.execute('SELECT * FROM support_tickets WHERE number=?',(number,)).fetchone()
    return dict(row) if row else None


def get_messages(store,number,limit=200,before=None):
    sql='SELECT message_id,role,body,created_at FROM support_messages WHERE number=?'
    params=[number]
    if before is not None:
        if not isinstance(before,str) or len(before)!=32 or any(c not in '0123456789abcdef' for c in before): raise ValueError('Некорректная страница.')
        cursor=store.db.execute('SELECT created_at,message_id FROM support_messages WHERE number=? AND message_id=?',(number,before)).fetchone()
        if not cursor: return []
        sql+=' AND (created_at<? OR (created_at=? AND message_id<?))'
        params.extend([cursor['created_at'],cursor['created_at'],cursor['message_id']])
    rows=store.db.execute(sql+' ORDER BY created_at DESC,message_id DESC LIMIT ?',(*params,limit))
    return [dict(r) for r in reversed(list(rows))]


def add_message(store,number,role,body):
    now=time.time()
    store.db.execute('INSERT INTO support_messages(message_id,number,role,body,created_at) VALUES(?,?,?,?,?)',(secrets.token_hex(16),number,role,body,now))
    store.db.execute('UPDATE support_tickets SET updated_at=? WHERE number=?',(now,number));store.db.commit()


def histories(store,uid,all_tickets=False):
    if all_tickets:
        rows=store.db.execute('SELECT number,owner_id,created_at,updated_at FROM support_tickets ORDER BY updated_at DESC LIMIT 200')
    else:
        rows=store.db.execute('SELECT number,owner_id,created_at,updated_at FROM support_tickets WHERE owner_id=? ORDER BY updated_at DESC LIMIT 200',(uid,))
    return [dict(x) for x in rows]


async def request_model(settings,token,messages):
    from openai import AsyncOpenAI
    prompt=BASE_PROMPT + ('\nДополнительная информация от администратора:\n'+settings['extra_prompt'] if settings['extra_prompt'] else '')
    # Bounded history: full conversations remain in the database.
    history=[{'role':m['role'],'content':m['body']} for m in messages if m['role'] in ('user','assistant')][-MAX_CONTEXT:]
    async with AsyncOpenAI(base_url=settings['base_url'],api_key=token,timeout=45.0,max_retries=0) as client:
        response=await client.chat.completions.create(model=settings['model'],messages=[{'role':'system','content':prompt}]+history,max_tokens=800)
    if not response.choices or not isinstance(response.choices[0].message.content,str): raise ValueError('empty response')
    text=response.choices[0].message.content.strip()
    if not text: raise ValueError('empty response')
    return text[:6000]


def error_details(exc):
    # Never expose the provider response body or exception text, which may contain a token.
    try:
        from openai import AuthenticationError, PermissionDeniedError, NotFoundError, RateLimitError, APITimeoutError, APIConnectionError
    except ImportError:
        return 'sdk_missing', 'На хостинге не установлен OpenAI SDK. Обновите зависимости бота.'
    if isinstance(exc,AuthenticationError): return 'auth', 'Провайдер отклонил авторизацию. Проверьте API Token и Base URL; токен Telegram не подходит.'
    if isinstance(exc,PermissionDeniedError): return 'permission', 'Провайдер запретил доступ. Проверьте права ключа и доступность модели.'
    if isinstance(exc,NotFoundError): return 'not_found', 'Провайдер не нашёл модель или адрес API. Проверьте Base URL и ID модели.'
    if isinstance(exc,RateLimitError): return 'rate_limit', 'Провайдер отклонил запрос из-за лимита или квоты. Проверьте лимиты у провайдера.'
    if isinstance(exc,APITimeoutError): return 'timeout', 'Провайдер слишком долго отвечает. Попробуйте ещё раз.'
    if isinstance(exc,APIConnectionError): return 'network', 'Не удалось подключиться к провайдеру. Проверьте Base URL и сеть хостинга.'
    return 'provider_error', 'Не удалось получить ответ. Проверьте совместимость модели и настройки провайдера.'


async def test_api(request,authorize):
    uid=await authorize(request,require_subscription=False);app=request.app
    if uid!=app['admin_id']: raise web.HTTPForbidden(text='Только администратор может проверить настройки.')
    cfg=config(app['store'])
    if not cfg or not cfg['token_cipher']: raise web.HTTPServiceUnavailable(text='Сначала сохраните настройки ИИ.')
    try: token=crypt(app['token']).decrypt(cfg['token_cipher'].encode()).decode()
    except (InvalidToken,ValueError): raise web.HTTPServiceUnavailable(text='Сохраните API Token заново.')
    try: validate_base(cfg['base_url'])
    except ValueError as exc: raise web.HTTPBadRequest(text=str(exc))
    now=time.monotonic();key=('test',uid);recent=[x for x in LAST.get(key,[]) if x>now-60]
    if len(recent)>=3: raise web.HTTPTooManyRequests(text='Подождите минуту перед следующей проверкой.')
    LAST[key]=recent+[now]
    try:
        async with SLOTS: answer=await request_model(cfg,token,[{'role':'user','body':'Ответь одним словом: OK'}])
    except Exception as exc:
        code,message=error_details(exc)
        log.warning('Проверка ИИ поддержки не прошла: %s',code)
        return web.json_response({'ok':False,'code':code,'message':message},headers={'Cache-Control':'no-store'})
    return web.json_response({'ok':True,'message':'Модель ответила: подключение работает.'},headers={'Cache-Control':'no-store'})


async def info(request,authorize):
    uid=await authorize(request,require_subscription=False); app=request.app
    settings=config(app['store']); result={'user_id':uid,'is_admin':uid==app['admin_id'],'configured':bool(settings and settings['token_cipher'])}
    if result['is_admin'] and settings:
        result.update(base_url=settings['base_url'],model=settings['model'],extra_prompt=settings['extra_prompt'],token_set=bool(settings['token_cipher']))
    return web.json_response(result,headers={'Cache-Control':'no-store'})


async def settings_api(request,authorize):
    uid=await authorize(request,require_subscription=False);app=request.app
    if uid!=app['admin_id']: raise web.HTTPForbidden(text='Только администратор может менять настройки.')
    try: data=await request.json()
    except (ValueError,TypeError): raise web.HTTPBadRequest(text='Некорректные данные.')
    if not isinstance(data,dict): raise web.HTTPBadRequest(text='Некорректные данные.')
    try: save_config(app['store'],app['token'],data)
    except ValueError as exc: raise web.HTTPBadRequest(text=str(exc))
    return web.json_response({'ok':True},headers={'Cache-Control':'no-store'})


async def tickets(request,authorize):
    uid=await authorize(request,require_subscription=False);app=request.app
    if request.method=='POST':
        count=app['store'].db.execute('SELECT COUNT(*) AS total FROM support_tickets WHERE owner_id=? AND created_at>?',(uid,time.time()-86400)).fetchone()['total']
        if count>=100: raise web.HTTPTooManyRequests(text='Достигнут лимит новых обращений за сутки.')
        return web.json_response({'number':new_ticket(app['store'],uid)},headers={'Cache-Control':'no-store'})
    all_tickets=request.query.get('all')=='1'
    if all_tickets and uid!=app['admin_id']: raise web.HTTPForbidden(text='Доступ запрещён.')
    return web.json_response({'tickets':histories(app['store'],uid,all_tickets)},headers={'Cache-Control':'no-store'})


async def thread(request,authorize):
    uid=await authorize(request,require_subscription=False);app=request.app
    ticket=get_ticket(app['store'],request.match_info['number'])
    if not ticket or (ticket['owner_id']!=uid and uid!=app['admin_id']): raise web.HTTPNotFound(text='Обращение не найдено.')
    try: messages=get_messages(app['store'],ticket['number'],201,request.query.get('before'))
    except ValueError as exc: raise web.HTTPBadRequest(text=str(exc))
    has_more=len(messages)>200
    return web.json_response({'ticket':ticket,'messages':messages[-200:],'has_more':has_more},headers={'Cache-Control':'no-store'})


class AvatarBuffer(io.BytesIO):
    def write(self, value):
        if self.tell()+len(value)>512*1024: raise ValueError('avatar too large')
        return super().write(value)


async def avatar(request,authorize):
    uid=await authorize(request,require_subscription=False);app=request.app
    ticket=get_ticket(app['store'],request.match_info['number'])
    if not ticket or (ticket['owner_id']!=uid and uid!=app['admin_id']): raise web.HTTPNotFound()
    try:
        photos=await app['bot'].get_user_profile_photos(ticket['owner_id'],limit=1)
        if not photos.photos or not photos.photos[0]: return web.Response(status=204,headers={'Cache-Control':'no-store'})
        photo=photos.photos[0][0]
        if photo.file_size is not None and photo.file_size>512*1024: return web.Response(status=204,headers={'Cache-Control':'no-store'})
        remote=await app['bot'].get_file(photo.file_id)
        buf=AvatarBuffer();await remote.download_to_memory(out=buf);data=buf.getvalue()
        if data.startswith(b'\xff\xd8\xff'): mime='image/jpeg'
        elif data.startswith(b'\x89PNG\r\n\x1a\n'): mime='image/png'
        elif data.startswith(b'RIFF') and data[8:12]==b'WEBP': mime='image/webp'
        else:return web.Response(status=204,headers={'Cache-Control':'no-store'})
        return web.Response(body=data,content_type=mime,headers={'Cache-Control':'private, max-age=300','X-Content-Type-Options':'nosniff'})
    except (TelegramError,OSError,ValueError,RuntimeError):
        return web.Response(status=204,headers={'Cache-Control':'no-store'})


async def send(request,authorize):
    uid=await authorize(request,require_subscription=False);app=request.app;store=app['store']
    ticket=get_ticket(store,request.match_info['number'])
    if not ticket or ticket['owner_id']!=uid: raise web.HTTPNotFound(text='Обращение не найдено.')
    try: data=await request.json()
    except (ValueError,TypeError): raise web.HTTPBadRequest(text='Некорректные данные.')
    text=data.get('text') if isinstance(data,dict) else None
    if not isinstance(text,str) or not 1<=len(text.strip())<=2500: raise web.HTTPBadRequest(text='Сообщение должно содержать от 1 до 2500 символов.')
    cfg=config(store)
    if not cfg or not cfg['token_cipher']: raise web.HTTPServiceUnavailable(text='ИИ-поддержка ещё не настроена администратором.')
    try: token=crypt(app['token']).decrypt(cfg['token_cipher'].encode()).decode()
    except (InvalidToken,ValueError): raise web.HTTPServiceUnavailable(text='Администратору нужно заново сохранить токен модели.')
    now=time.monotonic();recent=[x for x in LAST.get(uid,[]) if x>now-60]
    if len(recent)>=5: raise web.HTTPTooManyRequests(text='Слишком много сообщений. Попробуйте через минуту.')
    if len(LAST)>10000:
        for key,items in list(LAST.items()):
            if not items or items[-1]<=now-60: LAST.pop(key,None)
    LAST[uid]=recent+[now]
    lock=STRIPES[int(ticket['number'])%len(STRIPES)]
    async with lock:
        add_message(store,ticket['number'],'user',text.strip())
        history=get_messages(store,ticket['number'],MAX_CONTEXT)
        try:
            async with SLOTS: answer=await request_model(cfg,token,history)
        except Exception as exc:
            log.warning('Ошибка модели поддержки: %s',type(exc).__name__)
            add_message(store,ticket['number'],'notice','Не удалось получить ответ ИИ. Попробуйте ещё раз позже.')
            return web.json_response({'ok':False,'message':'Не удалось получить ответ ИИ. Попробуйте ещё раз позже.'},headers={'Cache-Control':'no-store'})
        add_message(store,ticket['number'],'assistant',answer)
    return web.json_response({'ok':True,'answer':answer},headers={'Cache-Control':'no-store'})
