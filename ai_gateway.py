"""Prepaid, model-bound keys and OpenAI-compatible text/vision endpoint."""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import io
from contextlib import contextmanager
from aiohttp import web
from cryptography.fernet import InvalidToken
import ai_models
import support_ai

PUBLIC=os.getenv('AI_PUBLIC_BASE_URL','https://rakcrmp.bothost.tech/v1').rstrip('/')
QUOTA=1_000_000


def init(store):
    store.db.executescript('''CREATE TABLE IF NOT EXISTS ai_keys (
      order_ref TEXT PRIMARY KEY, buyer BIGINT NOT NULL, model_key TEXT NOT NULL,
      token_hash TEXT UNIQUE NOT NULL, balance BIGINT NOT NULL, in_flight BIGINT NOT NULL DEFAULT 0,
      created_at DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS ai_usage (
      id TEXT PRIMARY KEY, order_ref TEXT NOT NULL, state TEXT NOT NULL, used BIGINT NOT NULL,
      created_at DOUBLE PRECISION NOT NULL);''');store.db.commit()


def key_for(bot_token,ref):
    return 'rak-'+hmac.new(bot_token.encode(),('ai-key-v1:'+ref).encode(),hashlib.sha256).hexdigest()


def digest(value):return hashlib.sha256(value.encode()).hexdigest()


def issue(store,bot_token,kind,order):
    if kind not in ('stars','external') or not order.get('id') or not order.get('file_id','').startswith('ai:'):raise ValueError('not an AI order')
    model=order['file_id'][3:]
    if model not in ai_models.MODELS:raise ValueError('unknown model')
    ref=kind+':'+order['id'];token=key_for(bot_token,ref)
    store.db.execute('INSERT OR IGNORE INTO ai_keys(order_ref,buyer,model_key,token_hash,balance,in_flight,created_at) VALUES(?,?,?,?,?,0,?)',(ref,order['buyer'],model,digest(token),QUOTA,time.time()));store.db.commit()
    return token


def keys(store,bot_token,uid):
    rows=store.db.execute("SELECT order_ref,model_key,balance,in_flight FROM ai_keys WHERE buyer=? AND order_ref NOT LIKE 'free:%' ORDER BY created_at DESC LIMIT 200",(uid,))
    return [{'name':ai_models.MODELS.get(r['model_key'],r['model_key']),'model':r['model_key'],'balance':r['balance'],'pending':bool(r['in_flight']),'base_url':PUBLIC,'api_token':key_for(bot_token,r['order_ref'])} for r in rows]


def _validate(data, *, max_tokens_limit=1024):
    if not isinstance(data,dict) or data.get('stream',False) is not False:raise ValueError('Streaming пока не поддерживается.')
    messages=data.get('messages')
    if not isinstance(messages,list) or not 1<=len(messages)<=20:raise ValueError('Нужно от 1 до 20 сообщений.')
    clean=[];chars=0;images=0
    for message in messages:
        if not isinstance(message,dict) or message.get('role') not in ('system','user','assistant'):raise ValueError('Некорректная роль.')
        content=message.get('content')
        if isinstance(content,str):chars+=len(content);content=content
        elif isinstance(content,list) and message['role']=='user' and len(content)<=10:
            parts=[]
            for part in content:
                if not isinstance(part,dict):raise ValueError('Некорректное содержимое.')
                if part.get('type')=='text' and isinstance(part.get('text'),str):
                    chars+=len(part['text']);parts.append({'type':'text','text':part['text']})
                elif part.get('type')=='image_url' and isinstance(part.get('image_url'),dict):
                    url=part['image_url'].get('url','')
                    if not isinstance(url,str) or len(url)>280000 or not re.fullmatch(r'data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=]+',url):raise ValueError('Изображение нужно передать как data URL PNG/JPEG/WebP размером до 200 КБ.')
                    try:raw=base64.b64decode(url.split(',',1)[1],validate=True)
                    except Exception:raise ValueError('Некорректное изображение.')
                    if len(raw)>200000 or not (raw.startswith(b'\x89PNG\r\n\x1a\n') or raw.startswith(b'\xff\xd8\xff') or raw.startswith(b'RIFF') and raw[8:12]==b'WEBP'):raise ValueError('Некорректное изображение.')
                    try:
                        from PIL import Image
                        with Image.open(io.BytesIO(raw)) as image:
                            if image.width>1024 or image.height>1024:raise ValueError('image dimensions')
                            image.verify()
                    except Exception:raise ValueError('Некорректный или слишком большой файл изображения.')
                    images+=1;parts.append({'type':'image_url','image_url':{'url':url}})
                else:raise ValueError('Поддерживаются только текст и изображения.')
            content=parts
        else:raise ValueError('Некорректное содержимое.')
        clean.append({'role':message['role'],'content':content})
    if chars>32000 or images>1:raise ValueError('Превышен размер текста или число изображений.')
    maximum=data.get('max_tokens',512)
    if isinstance(maximum,bool) or not isinstance(maximum,int) or not 1<=maximum<=max_tokens_limit:raise ValueError(f'max_tokens должен быть от 1 до {max_tokens_limit}.')
    reservation=sum(len((m['content'] if isinstance(m['content'],str) else ''.join(p.get('text','') for p in m['content'])).encode('utf-8')) for m in clean) + images*16384 + maximum + 256
    return clean,maximum,reservation


async def upstream(cfg,token,messages,maximum):
    from openai import AsyncOpenAI
    async with AsyncOpenAI(base_url=cfg['base_url'],api_key=token,timeout=60,max_retries=0) as client:
        result=await client.chat.completions.create(model=cfg['model_id'],messages=messages,max_tokens=maximum)
    return result.model_dump()


@contextmanager
def transaction(store):
    db=store.db
    if hasattr(db,'conn'):
        with db.conn.transaction():yield
    else:
        db.execute('BEGIN IMMEDIATE')
        try:
            yield
            db.commit()
        except Exception:
            db.rollback();raise


def settle(store,usage_id,used):
    if isinstance(used,bool) or not isinstance(used,int) or not 0<=used<=10000000:raise ValueError('invalid usage')
    with transaction(store):
        item=store.db.execute('SELECT * FROM ai_usage WHERE id=?',(usage_id,)).fetchone()
        if not item or item['state']!='pending':return False
        claim=store.db.execute("UPDATE ai_usage SET state='done',used=? WHERE id=? AND state='pending'",(used,usage_id))
        if claim.rowcount!=1:return False
        updated=store.db.execute('UPDATE ai_keys SET balance=balance-?,in_flight=0 WHERE order_ref=? AND in_flight=1',(used,item['order_ref']))
        if updated.rowcount!=1:raise RuntimeError('key state mismatch')
    return True


async def completions(request, *, max_tokens_limit=1024, reservation_limit=None, allow_free=False):
    auth=request.headers.get('Authorization','')
    if not auth.startswith('Bearer ') or not re.fullmatch(r'rak-[0-9a-f]{64}',auth[7:]):raise web.HTTPUnauthorized(text='Invalid API key')
    store=request.app['store'];row=store.db.execute('SELECT * FROM ai_keys WHERE token_hash=?',(digest(auth[7:]),)).fetchone()
    if not row:raise web.HTTPUnauthorized(text='Invalid API key')
    row=dict(row)
    if row['order_ref'].startswith('free:') and not allow_free:raise web.HTTPUnauthorized(text='Invalid API key')
    try:data=await request.json();messages,maximum,reservation=_validate(data,max_tokens_limit=max_tokens_limit)
    except (ValueError,TypeError):raise web.HTTPBadRequest(text='Некорректный запрос: проверьте messages, изображение и max_tokens.')
    if reservation_limit is not None and reservation>reservation_limit:raise web.HTTPPaymentRequired(text='Лимита задачи недостаточно для следующего запроса.')
    if data.get('model')!=row['model_key']:raise web.HTTPBadRequest(text='Ключ привязан к другой модели.')
    cfg=ai_models.config(store,row['model_key'])
    if not cfg:raise web.HTTPServiceUnavailable(text='Модель не настроена.')
    try:token=support_ai.crypt(request.app['token']).decrypt(cfg['token_cipher'].encode()).decode()
    except (InvalidToken,ValueError):raise web.HTTPServiceUnavailable(text='Модель временно недоступна.')
    # Reserve the whole key: only one request can be outstanding. Unknown provider
    # outcomes remain locked for manual reconciliation, not optimistically refunded.
    usage_id=secrets.token_hex(16)
    with transaction(store):
        result=store.db.execute('UPDATE ai_keys SET in_flight=1 WHERE order_ref=? AND in_flight=0 AND balance>=?',(row['order_ref'],reservation))
        if result.rowcount!=1:raise web.HTTPConflict(text='Ключ занят, ожидает сверки или баланса недостаточно для этого запроса.')
        store.db.execute('INSERT INTO ai_usage(id,order_ref,state,used,created_at) VALUES(?,?,?,0,?)',(usage_id,row['order_ref'],'pending',time.time()))
    try:
        answer=await upstream(cfg,token,messages,maximum)
        usage=answer.get('usage') or {};used=usage.get('total_tokens')
        if isinstance(used,bool) or not isinstance(used,int) or not 0<=used<=10_000_000:raise ValueError('invalid usage')
        # Atomic state guard prevents double charging on a retry of reconciliation.
        if not settle(store,usage_id,used):raise RuntimeError('usage reconciliation conflict')
    except Exception:
        # A provider may bill a request even when its response is lost.
        raise web.HTTPBadGateway(text='Нет подтверждённого учёта запроса. Ключ временно заблокирован до сверки с администратором.')
    answer['model']=row['model_key']
    return web.json_response(answer,headers={'Cache-Control':'no-store'})


async def account(request,authorize):
    uid=await authorize(request,require_subscription=False)
    return web.json_response({'keys':keys(request.app['store'],request.app['token'],uid)},headers={'Cache-Control':'no-store'})


async def models(request):
    auth=request.headers.get('Authorization','')
    if not auth.startswith('Bearer ') or not re.fullmatch(r'rak-[0-9a-f]{64}',auth[7:]):raise web.HTTPUnauthorized(text='Invalid API key')
    row=request.app['store'].db.execute('SELECT order_ref,model_key,balance,in_flight FROM ai_keys WHERE token_hash=?',(digest(auth[7:]),)).fetchone()
    if not row or row['order_ref'].startswith('free:'):raise web.HTTPUnauthorized(text='Invalid API key')
    return web.json_response({'object':'list','data':[{'id':row['model_key'],'object':'model','owned_by':'rakcrmp'}]},headers={'Cache-Control':'no-store'})


async def balance(request):
    auth=request.headers.get('Authorization','')
    if not auth.startswith('Bearer ') or not re.fullmatch(r'rak-[0-9a-f]{64}',auth[7:]):raise web.HTTPUnauthorized(text='Invalid API key')
    row=request.app['store'].db.execute('SELECT order_ref,model_key,balance,in_flight FROM ai_keys WHERE token_hash=?',(digest(auth[7:]),)).fetchone()
    if not row or row['order_ref'].startswith('free:'):raise web.HTTPUnauthorized(text='Invalid API key')
    return web.json_response({'model':row['model_key'],'balance_tokens':row['balance'],'pending':bool(row['in_flight'])},headers={'Cache-Control':'no-store'})
