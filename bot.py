import asyncio
import io
import logging
import os
import time

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, ChatMemberHandler, CommandHandler, MessageHandler, TypeHandler, filters

import captcha
from moderation import check_text, is_lua
from scanner import inspect, MAX_BYTES
from storage import Store, TOGGLES

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logging.getLogger('httpx').setLevel(logging.WARNING)
log = logging.getLogger('moderator')
store = Store()
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
    if name == '/start' and msg.chat.type == 'private' and context.args and context.args[0].startswith('cap_'):
        await captcha.open_widget(update, context, store, context.args[0][4:])
        return
    if not owner(update):
        return
    if name in ('/start', '/help'):
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

async def scheduler(app):
    while True:
        try:
            await captcha.recover(app.bot, store)
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
    app.bot_data['scheduler'] = asyncio.create_task(scheduler(app))

async def stop_scheduler(app):
    task = app.bot_data.get('scheduler')
    if task:
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
    store.db.close()

async def on_error(update, context):
    log.error('Ошибка обработки: %s', type(context.error).__name__, exc_info=context.error)

def main():
    global ADMIN_ID
    token = os.getenv('BOT_TOKEN', '').strip()
    try: ADMIN_ID = int(os.getenv('ADMIN_ID', ''))
    except ValueError: raise SystemExit('Укажите числовой ADMIN_ID в .env')
    if ADMIN_ID <= 0 or not token or token == 'сюда_токен_бота':
        raise SystemExit('Укажите BOT_TOKEN и положительный ADMIN_ID в .env')
    app = Application.builder().token(token).post_init(start_scheduler).post_shutdown(stop_scheduler).build()
    app.add_handler(TypeHandler(Update, on_widget_query), group=-1)
    app.add_handler(ChatMemberHandler(on_member, ChatMemberHandler.CHAT_MEMBER), group=0)
    app.add_handler(CallbackQueryHandler(on_captcha_answer, pattern=r'^cap:'), group=0)
    app.add_handler(CommandHandler(['start','help','panel','config','filter','word','words','autopost'],admin_command), group=0)
    app.add_handler(CallbackQueryHandler(button, pattern=r'^toggle:'), group=0)
    # Process commands as content too; owner commands are exempt from text restrictions.
    app.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, moderate), group=1)
    app.add_error_handler(on_error)
    log.info('Бот запущен')
    app.run_polling(allowed_updates=list(Update.ALL_TYPES) + ['widget_query'], drop_pending_updates=False)

if __name__ == '__main__':
    main()
