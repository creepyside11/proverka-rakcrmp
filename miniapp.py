"""Telegram Mini App server and Stars purchase flow."""
import asyncio
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import time
from urllib.parse import parse_qsl

from aiohttp import web
from telegram import LabeledPrice
from telegram.error import TelegramError
from admin_features import joined

log = logging.getLogger('miniapp')
INDEX = Path(__file__).with_name('miniapp.html')
delivery_lock = asyncio.Lock()


def init_tables(store):
    store.db.executescript('''
    CREATE TABLE IF NOT EXISTS star_prices (product_id TEXT PRIMARY KEY, stars INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS star_orders (
      id TEXT PRIMARY KEY, buyer INTEGER NOT NULL, product_id TEXT NOT NULL,
      stars INTEGER NOT NULL, name TEXT NOT NULL, file_id TEXT NOT NULL,
      filename TEXT NOT NULL, instruction TEXT NOT NULL, state TEXT NOT NULL,
      charge_id TEXT UNIQUE, created_at REAL NOT NULL);
    ''')
    store.db.commit()


def validate_init_data(raw, token, now=None):
    if not raw or len(raw)>8192: raise ValueError('Invalid init data')
    pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=40)
    data = dict(pairs)
    if len(data)!=len(pairs): raise ValueError('Duplicate parameter')
    digest = data.pop('hash', '')
    if len(digest)!=64: raise ValueError('Invalid hash')
    check = '\n'.join(f'{k}={data[k]}' for k in sorted(data))
    key = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    expected = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,digest): raise ValueError('Invalid signature')
    now = time.time() if now is None else now
    auth = int(data['auth_date'])
    if auth > now+30 or now-auth > 3600: raise ValueError('Expired init data')
    user = json.loads(data['user'])
    uid = user.get('id')
    if isinstance(uid,bool) or not isinstance(uid,int) or uid<=0: raise ValueError('Invalid user')
    return uid


def price(store, product_id):
    row=store.db.execute('SELECT stars FROM star_prices WHERE product_id=?',(product_id,)).fetchone()
    return row['stars'] if row else None


def set_price(store, product_id, amount):
    if not store.product(product_id) or not 1 <= amount <= 1000000: raise ValueError('Invalid product or amount')
    store.db.execute('DELETE FROM star_prices WHERE product_id=?',(product_id,))
    store.db.execute('INSERT INTO star_prices(product_id,stars) VALUES(?,?)',(product_id,amount))
    store.db.commit()


def get_order(store, order_id):
    row=store.db.execute('SELECT * FROM star_orders WHERE id=?',(order_id,)).fetchone()
    return dict(row) if row else None


async def authorize(request):
    app=request.app
    try: uid=validate_init_data(request.headers.get('X-Telegram-Init-Data',''),app['token'])
    except (ValueError,KeyError,TypeError,json.JSONDecodeError): raise web.HTTPUnauthorized(text='Откройте магазин через Telegram.')
    if uid != app['admin_id']:
        for channel in app['store'].channels():
            try: member=await app['bot'].get_chat_member(channel['chat_id'],uid)
            except TelegramError: raise web.HTTPServiceUnavailable(text='Не удалось проверить подписку. Попробуйте позже.')
            if not joined(member): raise web.HTTPForbidden(text='Подпишитесь на обязательные каналы в боте и откройте магазин снова.')
    return uid


async def index(request):
    return web.FileResponse(INDEX, headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})


async def catalog(request):
    await authorize(request)
    store=request.app['store']
    products=store.products()
    out=[{'id':p['id'],'name':p['name'],'description':p['description'],'category':p['category'],
          'stars':price(store,p['id'])} for p in products if p['category'] in ('scripts','bots')]
    return web.json_response({'products':out},headers={'Cache-Control':'no-store'})


async def create_invoice(request):
    uid=await authorize(request)
    try: data=await request.json()
    except (ValueError, json.JSONDecodeError): raise web.HTTPBadRequest(text='Некорректный запрос')
    product_id=data.get('product_id') if isinstance(data,dict) else None
    if not isinstance(product_id,str) or len(product_id)>64: raise web.HTTPBadRequest(text='Некорректный товар')
    store=request.app['store']; product=store.product(product_id); stars=price(store,product_id)
    if not product or not product['active'] or not stars: raise web.HTTPNotFound(text='Товар недоступен для покупки')
    oid=secrets.token_hex(12)
    store.db.execute('INSERT INTO star_orders(id,buyer,product_id,stars,name,file_id,filename,instruction,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
       (oid,uid,product_id,stars,product['name'],product['file_id'],product['filename'],product['instruction'],'pending',time.time()))
    store.db.commit()
    try:
        url=await request.app['bot'].create_invoice_link(title=product['name'][:32],description=(product['description'] or product['name'])[:255],payload='stars:'+oid,provider_token='',currency='XTR',prices=[LabeledPrice('Товар',stars)])
    except TelegramError:
        log.warning('Не удалось создать Stars-счёт')
        raise web.HTTPBadGateway(text='Не удалось создать счёт. Попробуйте позже.')
    return web.json_response({'invoice_url':url},headers={'Cache-Control':'no-store'})


async def precheckout(update, context, store):
    q=update.pre_checkout_query
    payload=q.invoice_payload or ''
    order=get_order(store,payload[6:]) if payload.startswith('stars:') else None
    valid=bool(order and order['state']=='pending' and order['buyer']==q.from_user.id and q.currency=='XTR' and q.total_amount==order['stars'])
    if valid:
        # Reserve this invoice once so simultaneous checkout queries cannot both succeed.
        result=store.db.execute("UPDATE star_orders SET state='checkout' WHERE id=? AND state='pending'",(order['id'],))
        store.db.commit()
        valid=result.rowcount == 1
    await q.answer(ok=valid,error_message=None if valid else 'Счёт недействителен. Создайте новый в магазине.')


async def successful_payment(update, context, store):
    payment=update.effective_message.successful_payment
    payload=payment.invoice_payload or ''
    order=get_order(store,payload[6:]) if payload.startswith('stars:') else None
    if not order or order['buyer']!=update.effective_user.id or payment.currency!='XTR' or order['stars']!=payment.total_amount:
        log.error('Получен платёж с неподходящими данными'); return
    charge=payment.telegram_payment_charge_id
    existing=store.db.execute('SELECT id FROM star_orders WHERE charge_id=?',(charge,)).fetchone()
    if existing and existing['id']!=order['id']:
        log.error('Повторяющийся идентификатор платежа'); return
    if order['state'] in ('pending','checkout'):
        store.db.execute("UPDATE star_orders SET state='paid',charge_id=? WHERE id=? AND state IN ('pending','checkout')",(charge,order['id']))
        store.db.commit()
    elif order['charge_id'] != charge:
        log.error('Получен повторный платёж для счёта %s', order['id']); return
    try: await deliver_order(context.bot,store,order['id'])
    except TelegramError: log.warning('Не удалось выдать оплаченный товар, повторим')


async def deliver_order(bot,store,oid):
    async with delivery_lock:
        order=get_order(store,oid)
        if not order or order['state']!='paid': return
        await bot.send_document(chat_id=order['buyer'],document=order['file_id'],filename=order['filename'],caption=('✅ Оплата подтверждена. '+order['name'])[:1024])
        await bot.send_message(chat_id=order['buyer'],text='Инструкция:\n'+order['instruction'])
        store.db.execute("UPDATE star_orders SET state='delivered' WHERE id=? AND state='paid'",(oid,))
        store.db.commit()


async def process_paid(bot,store):
    rows=store.db.execute("SELECT id FROM star_orders WHERE state='paid'")
    for row in list(rows):
        try: await deliver_order(bot,store,row['id'])
        except TelegramError: log.warning('Не удалось выдать Stars-заказ; повторим')


async def start_server(app,store,token,admin_id):
    server=web.Application(client_max_size=4096)
    server['store']=store; server['bot']=app.bot; server['token']=token; server['admin_id']=admin_id
    server.router.add_get('/',index); server.router.add_get('/api/catalog',catalog); server.router.add_post('/api/invoice',create_invoice)
    runner=web.AppRunner(server,access_log=None); await runner.setup()
    try: await web.TCPSite(runner,'0.0.0.0',int(os.getenv('PORT','8000'))).start()
    except Exception:
        await runner.cleanup(); raise
    app.bot_data['miniapp_runner']=runner
