import asyncio
import io
import json
import logging
import os
import secrets
import time

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, ChatMemberHandler, CommandHandler, MessageHandler, PreCheckoutQueryHandler, TypeHandler, filters

import captcha
import shop
import admin_features
import miniapp
from moderation import check_text, is_lua
from scanner import inspect, MAX_BYTES
from storage import Store, TOGGLES

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logging.getLogger('httpx').setLevel(logging.WARNING)
log = logging.getLogger('moderator')
store = None
ADMIN_ID = 0
LABELS = {'insults': 'Оскорбления', 'ads': 'Ссылки/реклама', 'luaonly': 'Только Lua/Luac', 'unknown_delete': 'Удалять непроверенное', 'scan': 'Проверка Lua/Luac'}
HELP = '''Команды владельца (отправляйте в нужной теме):
/panel — кнопки настроек темы
/config — текущие настройки и ID темы
/filter insults|ads|luaonly|unknown_delete|scan on|off — изменить фильтр
/word add слово — добавить запрещённое слово
/word del слово — удалить слово
/words — список слов
/autopost 60 Текст — публиковать текст каждые 60 минут
/autopost off — выключить сообщение
/help — эта справка

Проверка файлов эвристическая: «не найдены известные признаки» не гарантирует безопасность. Байткод, обфускация и ошибки проверки имеют статус «неизвестно».'''

def topic_id(message):
    return message.message_thread_id or 0

def owner(update):
    return bool(update.effective_user and update.effective_user.id == ADMIN_ID)

def group(message):
    return message and message.chat.type in ('group', 'supergroup')

def panel_markup(settings):
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"{'✅' if settings[k] else '❌'} {label}", callback_data=f'toggle:{k}')] for k, label in LABELS.items()])

def describe(settings):
    status = '\n'.join(f"{label}: {'вкл' if settings[k] else 'выкл'}" for k,label in LABELS.items())
    interval = settings['interval_sec'] // 60
    return f"Чат: {settings['chat']}\nТема: {settings['topic']}\n{status}\nАвтосообщение: {str(interval)+' мин' if interval else 'выкл'}" + (f"\nТекст: {settings['notice']}" if interval else '')

async def admin_command(update, context):
    msg = update.effective_message
    if not msg:
        return
    name = (msg.text or '').split()[0].split('@')[0].lower()
    if name == '/paysupport' and msg.chat.type == 'private':
        await msg.reply_text('По вопросам оплаты напишите в поддержку: https://t.me/RakCRMP_Support')
        return
    if name == '/start' and msg.chat.type == 'private' and context.args and context.args[0].startswith('cap_'):
        token = context.args[0][4:]
        row = store.web_challenge(token)
        if not row or row['user_id'] != update.effective_user.id or row['expires'] <= time.time():
            await msg.reply_text('Проверка не найдена, истекла или предназначена другому участнику.'); return
        await msg.reply_text('🧩 Откройте проверку:', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Открыть капчу', web_app=WebAppInfo(url='https://rakcrmp.bothost.tech/?captcha='+token))]]))
        return
    if name == '/start' and msg.chat.type == 'private':
        await msg.reply_text('👋 Добро пожаловать в RakCRMP! Выбери нужный раздел:', reply_markup=shop.home_markup())
        return
    if not owner(update):
        return
    if name == '/admin' and msg.chat.type == 'private':
        await msg.reply_text('⚙️ Панель администратора\n\n' + HELP + '\n\n/test_captcha — тест капчи\n/stats — статистика\n/broadcast текст — рассылка\n/channels — каналы\n/addchannel @username — добавить канал\n/delchannel @username — удалить канал\n/stars — цены магазина в Stars\n/cancel — отмена добавления товара.', reply_markup=shop.admin_markup())
        return
    if name == '/test_widget' and msg.chat.type == 'private':
        await test_captcha(update, context)
        return
    if name == '/test_captcha' and msg.chat.type == 'private':
        await test_captcha(update, context)
        return
    if name == '/help':
        await msg.reply_text(HELP)
        return
    if not group(msg):
        await msg.reply_text('Отправьте эту команду в нужной теме супергруппы.')
        return
    chat, topic = msg.chat.id, topic_id(msg)
    args = context.args
    if name == '/panel':
        cfg = store.get(chat,topic)
        await msg.reply_text('Настройки этой темы:\n' + describe(cfg), reply_markup=panel_markup(cfg))
    elif name == '/config':
        await msg.reply_text(describe(store.get(chat,topic)))
    elif name == '/filter':
        if len(args) != 2 or args[0] not in TOGGLES or args[1].lower() not in ('on', 'off'):
            await msg.reply_text('Пример: /filter ads on\nФильтры: ' + ', '.join(TOGGLES))
            return
        store.toggle(chat,topic,args[0],args[1].lower() == 'on')
        await msg.reply_text(describe(store.get(chat,topic)))
    elif name == '/word':
        if len(args) != 2 or args[0] not in ('add','del') or not 1 <= len(args[1]) <= 100:
            await msg.reply_text('Пример: /word add слово или /word del слово')
            return
        store.word(chat,topic,args[1].casefold(),args[0] == 'add')
        await msg.reply_text('Список слов обновлён.')
    elif name == '/words':
        words = ', '.join(store.words(chat,topic)) or 'пусто'
        await msg.reply_text(('Слова: ' + words)[:4000])
    elif name == '/autopost':
        if args and args[0].lower() == 'off':
            store.schedule(chat,topic,0,'')
            await msg.reply_text('Автосообщение выключено.')
            return
        try:
            minutes = int(args[0])
            notice = ' '.join(args[1:]).strip()
            if not (1 <= minutes <= 525600 and 1 <= len(notice) <= 3000): raise ValueError
        except (ValueError, IndexError):
            await msg.reply_text('Пример: /autopost 60 Не скачивайте файлы без проверки. Или /autopost off')
            return
        store.schedule(chat,topic,minutes,notice)
        await msg.reply_text(f'Автосообщение настроено: каждые {minutes} мин. Первое — через {minutes} мин.')

async def button(update, context):
    query = update.callback_query
    if not owner(update):
        await query.answer('Менять настройки может только владелец бота.', show_alert=True)
        return
    msg = query.message
    key = (query.data or '').removeprefix('toggle:')
    if not group(msg) or key not in TOGGLES:
        await query.answer('Недоступно', show_alert=True)
        return
    chat, topic = msg.chat.id, topic_id(msg)
    current = store.get(chat,topic)
    store.toggle(chat,topic,key,not current[key])
    current = store.get(chat,topic)
    await query.answer('Настройка изменена')
    await query.edit_message_text('Настройки этой темы:\n' + describe(current), reply_markup=panel_markup(current))

async def delete_and_notice(message, context, reason):
    try:
        await message.delete()
        reason = '🗑 Сообщение удалено. ' + reason
    except TelegramError as exc:
        log.warning('Не удалось удалить сообщение chat=%s id=%s: %s', message.chat.id, message.message_id, type(exc).__name__)
        reason = '⚠️ Не удалось удалить сообщение. Проверьте права бота. ' + reason
    await context.bot.send_message(chat_id=message.chat.id, message_thread_id=message.message_thread_id, text=reason)

async def moderate(update, context):
    msg = update.effective_message
    if not group(msg): return
    # Service messages are not user content.
    if not (msg.text or msg.caption or msg.effective_attachment): return
    cfg = store.get(msg.chat.id, topic_id(msg))
    doc = msg.document
    text = msg.text or msg.caption or ''
    entities = msg.entities if msg.text else msg.caption_entities
    # The configured owner can manage the topic, but their files are still scanned.
    if not owner(update):
        if cfg['luaonly'] and (not doc or not is_lua(doc.file_name)):
            await delete_and_notice(msg,context,'В этой теме разрешены только файлы .lua и .luac.')
            return
        reason = check_text(text,entities,cfg,store.words(msg.chat.id,topic_id(msg)))
        if reason:
            await delete_and_notice(msg,context,'Причина: ' + reason + '.')
            return
    if not doc or not is_lua(doc.file_name) or not cfg['scan']:
        return
    if doc.file_size is None or doc.file_size > MAX_BYTES:
        result_status, reasons = 'unknown', ('размер файла неизвестен или превышает 5 МиБ',)
    else:
        try:
            remote = await context.bot.get_file(doc.file_id)
            stream = io.BytesIO()
            await remote.download_to_memory(out=stream)
            data = stream.getvalue()
            result = inspect(data, doc.file_name or '')
            result_status, reasons = result.status, result.reasons
        except (TelegramError, OSError, ValueError, RuntimeError) as exc:
            log.warning('Проверка файла не выполнена: %s', type(exc).__name__)
            result_status, reasons = 'unknown', ('не удалось скачать или проверить файл',)
    if result_status == 'suspicious':
        await delete_and_notice(msg,context,'⛔ Обнаружены опасные признаки (' + '; '.join(reasons) + '). Возможны ложные срабатывания.')
    elif result_status == 'unknown':
        notice = '⚠️ Файл не удалось надёжно проверить: ' + '; '.join(reasons) + '. Не запускайте без ручного анализа.'
        if cfg['unknown_delete']:
            await delete_and_notice(msg,context,notice)
        else:
            await msg.reply_text(notice)
    else:
        await msg.reply_text('✅ Проверка завершена: известных опасных признаков не обнаружено. Это не гарантия безопасности файла.')

async def on_member(update, context):
    await captcha.member_update(update, context, store, ADMIN_ID)

async def on_captcha_answer(update, context):
    await captcha.answer(update, context, store)

async def on_widget_query(update, context):
    await captcha.widget_query(update, context, store)

async def gate(update, context):
    await admin_features.subscription_gate(update, context, store, ADMIN_ID)

async def extra_admin(update, context):
    await admin_features.admin_command(update, context, store, ADMIN_ID)

async def broadcast_callback(update, context):
    await admin_features.broadcast_callback(update, context, store, ADMIN_ID)

async def test_captcha(update, context):
    token = secrets.token_hex(8)
    question, answer, _, buttons = captcha.new_challenge(token)
    options = [{'label': b.text, 'value': int(b.callback_data.rsplit(':',1)[1])} for b in buttons]
    store.create_web_challenge(token,update.effective_user.id,question,json.dumps(options,ensure_ascii=False),answer,time.time()+600,demo=1)
    await update.effective_message.reply_text('🧩 Тест капчи. Права участников не изменяются.', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Открыть тест капчи', web_app=WebAppInfo(url='https://rakcrmp.bothost.tech/?captcha='+token))]]))

async def test_captcha_answer(update, context):
    q = update.callback_query
    if not owner(update) or not q.message or q.message.chat.type != 'private':
        await q.answer('Только для администратора.', show_alert=True); return
    parts = (q.data or '').split(':')
    demo = context.user_data.get('captcha_demo')
    if len(parts)!=3 or not demo or parts[1]!=demo['token'] or time.time()>demo['expires'] or not parts[2].isdigit():
        await q.answer('Тест устарел.',show_alert=True); return
    if int(parts[2]) == demo['answer']:
        context.user_data.pop('captcha_demo',None)
        await q.answer('Верно! Тест пройден.',show_alert=True)
        await q.edit_message_reply_markup(reply_markup=None)
    else:
        await q.answer('Неверно. Попробуйте ещё раз.',show_alert=True)

async def admin_ui(update, context):
    q = update.callback_query
    if not admin_features.admin(update, ADMIN_ID):
        await q.answer('Только для администратора.',show_alert=True); return
    await q.answer()
    action = (q.data or '').split(':')[-1]
    if action == 'test': await test_captcha(update,context)
    elif action == 'stats':
        s = store.statistics()
        await q.message.reply_text('📊 Пользователей: {users}\nПолучателей рассылки: {broadcast}\nКапча пройдена: {passed}\nНе пройдена: {failed}\nОжидают: {pending}\nУчёт начинается с установки обновления.'.format(**s))
    elif action == 'channels':
        channels = store.channels()
        text = '\n'.join(f"{c['title']} — {c['chat_id']}" for c in channels) or 'Список пуст.'
        await q.message.reply_text('📢 Каналы:\n'+text+'\n\nДобавить: /addchannel @username\nУдалить: /delchannel @username\nДля частного канала: /addchannel -100ID https://t.me/+ссылка')
    elif action == 'broadcast': await q.message.reply_text('Отправьте /broadcast и текст сообщения. Перед отправкой появится подтверждение.')

async def shop_button(update, context):
    await shop.callback(update, context, store, ADMIN_ID)

async def shop_message(update, context):
    if not await shop.donation_input(update, context):
        await shop.admin_input(update, context, store, ADMIN_ID)

async def stars_admin(update, context):
    if not owner(update) or update.effective_chat.type != 'private': return
    if not context.args:
        products=store.products(all_items=True)
        if not products:
            await update.effective_message.reply_text('Сначала добавьте товары через /admin.'); return
        rows=[f"{p['id']} — {p['name'][:70]}: {miniapp.price(store,p['id']) or 'не задано'} ⭐" for p in products[:40]]
        await update.effective_message.reply_text('Цены магазина:\n'+'\n'.join(rows)+'\n\nУстановить: /stars ID количество'); return
    if len(context.args)!=2 or not context.args[1].isdigit():
        await update.effective_message.reply_text('Пример: /stars ID 100'); return
    try: miniapp.set_price(store,context.args[0],int(context.args[1]))
    except (ValueError,OverflowError):
        await update.effective_message.reply_text('Товар не найден или цена вне диапазона 1–1000000 ⭐.'); return
    await update.effective_message.reply_text('✅ Цена в Stars сохранена.')

async def stars_checkout(update, context):
    await miniapp.precheckout(update,context,store)

async def stars_paid(update, context):
    await miniapp.successful_payment(update,context,store)

async def scheduler(app):
    while True:
        try:
            await captcha.recover(app.bot, store)
            await shop.process_pending(app.bot, store)
            await miniapp.process_paid(app.bot, store)
            for row in store.due():
                # Reserve before sending: a crash may skip one announcement, but avoids a restart flood.
                store.advance(row['chat'],row['topic'])
                try:
                    await app.bot.send_message(chat_id=row['chat'], message_thread_id=row['topic'] or None, text=row['notice'])
                except TelegramError as exc:
                    log.warning('Не удалось отправить автосообщение chat=%s topic=%s: %s', row['chat'],row['topic'],type(exc).__name__)
        except Exception:
            log.exception('Ошибка планировщика')
        await asyncio.sleep(10)

async def start_scheduler(app):
    await miniapp.start_server(app,store,os.environ['BOT_TOKEN'].strip(),ADMIN_ID)
    app.bot_data['scheduler'] = asyncio.create_task(scheduler(app))

async def stop_scheduler(app):
    task = app.bot_data.get('scheduler')
    if task:
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
    runner = app.bot_data.get('miniapp_runner')
    if runner: await runner.cleanup()
    store.db.close()

async def on_error(update, context):
    log.error('Ошибка обработки: %s', type(context.error).__name__, exc_info=context.error)

def main():
    global ADMIN_ID, store
    token = os.getenv('BOT_TOKEN', '').strip()
    try: ADMIN_ID = int(os.getenv('ADMIN_ID', ''))
    except ValueError: raise SystemExit('Укажите числовой ADMIN_ID в .env')
    if ADMIN_ID <= 0 or not token or token == 'сюда_токен_бота':
        raise SystemExit('Укажите BOT_TOKEN и положительный ADMIN_ID в .env')
    store = Store()
    store.init_shop()
    miniapp.init_tables(store)
    app = Application.builder().token(token).post_init(start_scheduler).post_shutdown(stop_scheduler).build()
    app.add_handler(TypeHandler(Update, gate), group=-1)
    app.add_handler(ChatMemberHandler(on_member, ChatMemberHandler.CHAT_MEMBER), group=0)
    app.add_handler(CallbackQueryHandler(on_captcha_answer, pattern=r'^cap:'), group=0)
    app.add_handler(CommandHandler(['start','admin','test_widget','test_captcha','help','panel','config','filter','word','words','autopost','paysupport'],admin_command), group=0)
    app.add_handler(CommandHandler('stars',stars_admin), group=0)
    app.add_handler(PreCheckoutQueryHandler(stars_checkout), group=0)
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, stars_paid), group=0)
    app.add_handler(CommandHandler(['stats','channels','addchannel','delchannel','broadcast'],extra_admin), group=0)
    app.add_handler(CallbackQueryHandler(broadcast_callback, pattern=r'^broadcast:'), group=0)
    app.add_handler(CallbackQueryHandler(test_captcha_answer, pattern=r'^capdemo:'), group=0)
    app.add_handler(CallbackQueryHandler(admin_ui, pattern=r'^adminui:'), group=0)
    app.add_handler(CallbackQueryHandler(button, pattern=r'^toggle:'), group=0)
    app.add_handler(CallbackQueryHandler(shop_button, pattern=r'^shop:'), group=0)
    app.add_handler(CommandHandler('cancel', shop_message), group=0)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.TEXT | filters.Document.ALL) & ~filters.COMMAND, shop_message), group=0)
    # Process commands as content too; owner commands are exempt from text restrictions.
    app.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, moderate), group=1)
    app.add_error_handler(on_error)
    log.info('Бот запущен')
    app.run_polling(allowed_updates=list(Update.ALL_TYPES), drop_pending_updates=False)

if __name__ == '__main__':
    main()
