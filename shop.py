"""Private storefront and admin product management."""
import asyncio
import logging
import secrets
import time
from decimal import Decimal, InvalidOperation
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup
from telegram.error import TelegramError
import payments

log = logging.getLogger('shop')
lock = asyncio.Lock()
DOWNLOAD = 'https://github.com/creepyside11/black'
SUPPORT = 'https://t.me/RakCRMP_Support'


def b(text, *, data=None, url=None, color='primary'):
    return Button(text, callback_data=data, url=url, api_kwargs={'style':color})

def home_markup():
    return Markup([[b('📥 Скачать программу',url=DOWNLOAD,color='success')],
        [b('📜 Купить скрипт',data='shop:catalog:scripts'), b('🤖 Купить бота',data='shop:catalog:bots')],
        [b('💜 Поддержать проект',data='shop:donate')], [b('🛟 Поддержка',url=SUPPORT)]])

def admin_markup():
    return Markup([[b('➕ Добавить товар',data='shop:admin:add',color='success')],
        [b('📦 Управление товарами',data='shop:admin:list')],
        [b('🧩 Тест виджета',data='shop:admin:widget')]])

async def admin_input(update, context, store, admin_id):
    msg = update.effective_message
    if not msg or msg.chat.type != 'private' or update.effective_user.id != admin_id: return False
    draft = context.user_data.get('product_draft')
    if not draft: return False
    if msg.text and msg.text.startswith('/'):
        if msg.text.split()[0] == '/cancel':
            context.user_data.pop('product_draft',None)
            await msg.reply_text('Добавление товара отменено.')
            return True
        return False
    step = draft['step']; text = (msg.text or '').strip()
    if step == 'category':
        if text not in ('1','2'):
            await msg.reply_text('Отправь 1 для скрипта или 2 для бота.')
            return True
        draft.update(category='scripts' if text=='1' else 'bots',step='name')
        await msg.reply_text('Напиши название товара (до 100 символов).')
    elif step == 'name':
        if not 1 <= len(text) <= 100:
            await msg.reply_text('Название должно содержать от 1 до 100 символов.')
            return True
        draft.update(name=text,step='description')
        await msg.reply_text('Напиши описание товара (до 1000 символов).')
    elif step == 'description':
        if not 1 <= len(text) <= 1000:
            await msg.reply_text('Описание должно содержать от 1 до 1000 символов.')
            return True
        draft.update(description=text,step='price')
        await msg.reply_text('Укажи цену в рублях, например 299.50.')
    elif step == 'price':
        try:
            value = Decimal(text.replace(',','.'))
            if not value.is_finite() or value <= 0 or value > 1000000 or value*100 != (value*100).to_integral_value(): raise ValueError
            amount = int(value*100)
        except (ValueError, InvalidOperation):
            await msg.reply_text('Укажи цену от 0,01 до 1 000 000 ₽, не более двух знаков после запятой.')
            return True
        draft.update(price_kopecks=amount,step='instruction')
        await msg.reply_text('Отправь инструкцию по установке и использованию (до 3000 символов).')
    elif step == 'instruction':
        if not 1 <= len(text) <= 3000:
            await msg.reply_text('Инструкция должна содержать от 1 до 3000 символов.')
            return True
        draft.update(instruction=text,step='file')
        await msg.reply_text('Отправь готовый файл .lua документом.')
    elif step == 'file':
        doc = msg.document
        if not doc or not (doc.file_name or '').lower().endswith('.lua') or not doc.file_size or doc.file_size > 20*1024*1024:
            await msg.reply_text('Нужен документ .lua размером до 20 МБ.')
            return True
        draft.update(file_id=doc.file_id,filename=doc.file_name,id=secrets.token_hex(6))
        store.product_add(draft)
        context.user_data.pop('product_draft',None)
        await msg.reply_text('✅ Товар добавлен в каталог.',reply_markup=admin_markup())
    return True

async def donation_input(update, context):
    msg = update.effective_message
    if not msg or msg.chat.type != 'private' or 'donation_draft' not in context.user_data:
        return False
    text = (msg.text or '').strip()
    if text.split('@')[0] == '/cancel':
        context.user_data.pop('donation_draft', None)
        await msg.reply_text('Поддержка отменена.')
        return True
    if text.startswith('/'):
        return False
    draft = context.user_data['donation_draft']
    if draft.get('step') != 'amount':
        return False
    try:
        # Limit input length before Decimal arithmetic to avoid expensive inputs.
        if not text or len(text) > 16:
            raise ValueError
        value = Decimal(text.replace(',', '.'))
        if not value.is_finite() or value < 1 or value > 1000000000000 or value * 100 != (value * 100).to_integral_value():
            raise ValueError
        amount = int(value * 100)
    except (ValueError, InvalidOperation, OverflowError):
        await msg.reply_text('Введи сумму от 1 ₽, не более двух знаков после запятой. Например: 150 или 150,50.')
        return True
    token = secrets.token_hex(6)
    context.user_data['donation_draft'] = {'step':'method', 'amount':amount, 'token':token}
    markup = Markup([[b('Crypto Bot', data='shop:donatepay:crypto:'+token)],
                     [b('СБП', data='shop:donatepay:sbp:'+token), b('Карта', data='shop:donatepay:card:'+token)]])
    await msg.reply_text(f'Сумма поддержки: {amount // 100}.{amount % 100:02d} ₽. Выбери способ оплаты:', reply_markup=markup)
    return True

async def check_donation(bot, store, donation_id, buyer=None):
    async with lock:
        donation = store.donation(donation_id)
        if not donation or (buyer is not None and donation['buyer'] != buyer): return 'not_found'
        if donation['state'] == 'notified': return 'notified'
        if donation['state'] == 'pending':
            if not await payments.paid(donation): return 'pending'
            store.mark_donation_paid(donation_id)
            donation = store.donation(donation_id)
        if donation['state'] == 'paid':
            await bot.send_message(chat_id=donation['buyer'], text='💜 Спасибо за поддержку проекта! Оплата подтверждена.')
            store.mark_donation_notified(donation_id)
            return 'notified'
        return 'pending'

async def deliver(bot, store, order):
    # The database is updated only after Telegram confirms send_document. A crash
    # immediately after sending can cause a repeat: Telegram has no idempotency key.
    await bot.send_document(chat_id=order['buyer'], document=order['file_id'], filename=order['filename'],
        caption=('✅ Оплата подтверждена. '+order['name'])[:1024])
    await bot.send_message(chat_id=order['buyer'], text='Инструкция:\n'+order['instruction'])
    store.mark_delivered(order['id'])

async def check_order(bot, store, order_id, buyer=None):
    async with lock:
        order = store.order(order_id)
        if not order or (buyer is not None and order['buyer'] != buyer): return 'not_found'
        if order['state'] == 'delivered': return 'delivered'
        if order['state'] == 'pending':
            if not await payments.paid(order): return 'pending'
            store.mark_paid(order_id)
            order = store.order(order_id)
        if order['state'] == 'paid':
            await deliver(bot,store,order)
            return 'delivered'
        return 'pending'

async def process_pending(bot, store):
    for order in store.pending_orders():
        try: await check_order(bot,store,order['id'])
        except (payments.PaymentError, TelegramError):
            log.warning('Не удалось проверить или выдать заказ %s; повторим',order['id'])
    for donation in store.pending_donations():
        try: await check_donation(bot, store, donation['id'])
        except (payments.PaymentError, TelegramError):
            log.warning('Не удалось проверить поддержку %s; повторим', donation['id'])

async def callback(update, context, store, admin_id):
    q = update.callback_query
    if not q.message or q.message.chat.type != 'private':
        await q.answer('Открой бота в личном чате.', show_alert=True); return
    data = (q.data or '').split(':'); action = data[1] if len(data)>1 else ''
    user_id = update.effective_user.id
    if action == 'admin':
        if user_id != admin_id:
            await q.answer('Доступ только администратору.',show_alert=True); return
        sub = data[2] if len(data)>2 else ''
        await q.answer()
        if sub == 'add':
            context.user_data.pop('donation_draft', None)
            context.user_data['product_draft']={'step':'category'}
            await q.message.reply_text('Новый товар. Отправь 1 — скрипт, 2 — бот. Для отмены: /cancel.')
        elif sub == 'list':
            products = store.products(all_items=True)
            if not products: await q.message.reply_text('Товаров пока нет.'); return
            rows = [[b(('✅ ' if p['active'] else '⏸ ') + p['name'][:55],data='shop:adminproduct:'+p['id'])] for p in products[:100]]
            await q.message.reply_text('Выбери товар, чтобы включить или скрыть:',reply_markup=Markup(rows))
        elif sub == 'widget':
            import captcha
            await captcha.test_widget(update, context)
        return
    if action == 'adminproduct':
        if user_id != admin_id:
            await q.answer('Доступ только администратору.',show_alert=True); return
        product = store.product(data[2]) if len(data)>2 else None
        if not product: await q.answer('Товар не найден.',show_alert=True); return
        store.product_set_active(product['id'],not product['active'])
        await q.answer('Товар скрыт.' if product['active'] else 'Товар опубликован.',show_alert=True)
        return
    if action == 'catalog':
        category = data[2] if len(data)>2 else ''
        if category not in ('bots','scripts'): await q.answer(); return
        products = store.products(category)
        await q.answer()
        if not products: await q.message.reply_text('В этом каталоге пока нет товаров.'); return
        rows = [[b(f"{p['name'][:40]} · {p['price_kopecks']/100:.2f} ₽",data='shop:product:'+p['id'])] for p in products[:100]]
        await q.message.reply_text('Выбери товар:',reply_markup=Markup(rows)); return
    if action == 'donate':
        if context.user_data.get('product_draft'):
            await q.answer('Сначала закончи добавление товара или отмени его командой /cancel.',show_alert=True)
            return
        context.user_data['donation_draft'] = {'step':'amount'}
        await q.answer()
        await q.message.reply_text('💜 Спасибо за поддержку! Введи сумму в рублях от 1 ₽. Для отмены: /cancel.')
        return
    if action == 'donatepay':
        if len(data) != 4 or data[2] not in ('crypto','sbp','card'):
            await q.answer('Неизвестный способ оплаты.',show_alert=True); return
        draft = context.user_data.get('donation_draft')
        if not draft or draft.get('step') != 'method' or not secrets.compare_digest(draft.get('token',''), data[3]):
            await q.answer('Этот выбор устарел. Начни поддержку заново.',show_alert=True); return
        # Consume before awaiting the provider so repeated taps cannot create invoices concurrently.
        context.user_data.pop('donation_draft', None)
        await q.answer('Создаём счёт…')
        donation_id = secrets.token_hex(12)
        try:
            provider_id, url = await payments.create(data[2], donation_id, user_id, draft['amount'], 'Поддержка проекта RakCRMP')
        except payments.PaymentError:
            context.user_data['donation_draft'] = draft
            log.warning('Не удалось создать счёт поддержки %s', data[2])
            await q.message.reply_text('Не удалось создать счёт. Выбери другой способ или попробуй позже: платёжный сервис может устанавливать собственный минимум.')
            return
        store.donation_add({'id':donation_id, 'buyer':user_id, 'price_kopecks':draft['amount'],
                            'provider':data[2], 'provider_id':provider_id, 'payment_url':url,
                            'state':'pending', 'created_at':time.time()})
        await q.message.reply_text('Счёт создан. После подтверждения оплаты бот поблагодарит тебя 💜',
            reply_markup=Markup([[b('Оплатить',url=url,color='success')], [b('Проверить оплату',data='shop:donatecheck:'+donation_id)]]))
        return
    if action == 'donatecheck':
        await q.answer('Проверяем оплату…')
        try:
            state = await check_donation(context.bot, store, data[2], user_id) if len(data) == 3 else 'not_found'
        except (payments.PaymentError, TelegramError):
            await q.message.reply_text('Не удалось проверить оплату. Попробуй позже или обратись в поддержку.'); return
        await q.message.reply_text({'pending':'Оплата ещё не подтверждена.', 'notified':'Поддержка подтверждена. Спасибо! 💜', 'not_found':'Платёж не найден.'}[state])
        return
    if action == 'product':
        product = store.product(data[2]) if len(data)>2 else None
        if not product or not product['active']:
            await q.answer('Товар недоступен.',show_alert=True); return
        await q.answer()
        markup = Markup([[b('Crypto Bot',data='shop:buy:crypto:'+product['id'])],
                         [b('СБП',data='shop:buy:sbp:'+product['id']),b('Карта',data='shop:buy:card:'+product['id'])]])
        await q.message.reply_text(f"{product['name']}\n\n{product['description']}\n\nЦена: {product['price_kopecks']/100:.2f} ₽\nВыбери способ оплаты:",reply_markup=markup); return
    if action == 'buy':
        if len(data)!=4 or data[2] not in ('crypto','sbp','card'):
            await q.answer('Неизвестный способ оплаты.',show_alert=True); return
        product=store.product(data[3])
        if not product or not product['active']:
            await q.answer('Товар недоступен.',show_alert=True); return
        await q.answer('Создаём счёт…')
        order_id=secrets.token_hex(12)
        try: provider_id,url=await payments.create(data[2],order_id,user_id,product['price_kopecks'],product['name'])
        except payments.PaymentError:
            log.warning('Не удалось создать платёж %s',data[2])
            await q.message.reply_text('Не удалось создать счёт. Попробуй позже или выбери другой способ оплаты.'); return
        order={**product,'id':order_id,'buyer':user_id,'product_id':product['id'],'provider':data[2],
               'provider_id':provider_id,'payment_url':url,'state':'pending','created_at':time.time()}
        store.order_add(order)
        await q.message.reply_text('Оплати счёт. После подтверждения файл и инструкция придут автоматически.',
            reply_markup=Markup([[b('Оплатить',url=url,color='success')],[b('Проверить оплату',data='shop:check:'+order_id)]])); return
    if action == 'check':
        await q.answer('Проверяем оплату…')
        try: state=await check_order(context.bot,store,data[2],user_id) if len(data)>2 else 'not_found'
        except (payments.PaymentError,TelegramError):
            await q.message.reply_text('Не удалось проверить платёж или отправить файл. Попробуй позже или обратись в поддержку.'); return
        await q.message.reply_text({'pending':'Оплата ещё не подтверждена.', 'delivered':'Заказ выдан.', 'not_found':'Заказ не найден.'}[state]); return
    await q.answer()
