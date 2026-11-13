"""Admin controls, private-chat subscription gate, and opt-in broadcast."""
import asyncio
import re
import secrets
import time
from urllib.parse import urlsplit

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError, RetryAfter
from telegram.ext import ApplicationHandlerStop


def admin(update, admin_id):
    return bool(update.effective_user and update.effective_user.id == admin_id and update.effective_chat and update.effective_chat.type == 'private')


def joined(member):
    return member.status in ('member', 'administrator', 'creator', 'owner') or (member.status == 'restricted' and bool(getattr(member, 'is_member', False)))


async def subscription_gate(update, context, store, admin_id):
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if not chat or chat.type != 'private' or not user or user.id == admin_id:
        return
    # A completed payment must never be dropped when subscription checking fails.
    if msg and getattr(msg, 'successful_payment', None):
        return
    query = update.callback_query
    text = (msg.text or '') if msg and not query else ''
    command = text.split(maxsplit=1)[0].split('@')[0].lower() if text else ''
    if command == '/paysupport':
        return
    if command == '/start' and len(text.split()) > 1 and text.split()[1].startswith('cap_'):
        return
    if command == '/stop':
        store.opt_out(user.id)
        await msg.reply_text('Рассылка отключена. Чтобы снова её получать, отправьте /subscribe.')
        raise ApplicationHandlerStop
    if command == '/subscribe':
        store.opt_in(user.id)
        await msg.reply_text('Рассылка включена. Для отмены: /stop.')
        raise ApplicationHandlerStop
    if command == '/start':
        store.register_user(user.id)
    channels = store.channels()
    missing, errors = [], False
    for channel in channels:
        try:
            member = await context.bot.get_chat_member(channel['chat_id'], user.id)
            if not joined(member): missing.append(channel)
        except TelegramError:
            errors = True
    if errors or missing:
        rows = [[InlineKeyboardButton('📢 ' + c['title'][:50], url=c['url'])] for c in channels]
        rows.append([InlineKeyboardButton('✅ Проверить подписку', callback_data='subscription:check')])
        message = ('⚠️ Не удалось проверить подписку. Попробуйте позже или обратитесь к администратору.' if errors
                   else 'Чтобы пользоваться ботом, подпишитесь на каналы и нажмите «Проверить подписку».')
        if query:
            await query.answer('Не удалось проверить подписку.' if errors else 'Сначала подпишитесь на каналы.', show_alert=True)
            if msg: await msg.reply_text(message, reply_markup=InlineKeyboardMarkup(rows))
        elif msg:
            await msg.reply_text(message, reply_markup=InlineKeyboardMarkup(rows))
        raise ApplicationHandlerStop
    if query and query.data == 'subscription:check':
        await query.answer('Подписка подтверждена!')
        await msg.reply_text('✅ Подписка подтверждена. Отправьте /start, чтобы открыть меню.')
        raise ApplicationHandlerStop


async def admin_command(update, context, store, admin_id):
    if not admin(update, admin_id): return
    msg = update.effective_message
    command = msg.text.split()[0].split('@')[0].lower()
    args = context.args
    if command == '/stats':
        s = store.statistics()
        await msg.reply_text('📊 Статистика\nПользователей бота: {users}\nПолучателей рассылки: {broadcast}\nКапча пройдена: {passed}\nКапча не пройдена: {failed}\nОжидают капчу: {pending}\n\nУчёт начинается с установки обновления.'.format(**s))
    elif command == '/channels':
        channels = store.channels()
        rows = [f"{c['title']} — {c['chat_id']} — {c['url']}" for c in channels]
        await msg.reply_text('📢 Обязательные каналы для личного бота:\n' + ('\n'.join(rows) if rows else 'Список пуст.'))
    elif command == '/addchannel':
        if not 1 <= len(args) <= 2:
            await msg.reply_text('Использование: /addchannel @username или /addchannel -100ID https://t.me/+ссылка\nДобавьте бота администратором канала.')
            return
        if len(store.channels()) >= 20:
            await msg.reply_text('Можно добавить не более 20 каналов.'); return
        target = args[0]
        if not (re.fullmatch(r'@[A-Za-z0-9_]{5,32}', target) or re.fullmatch(r'-100\d+', target)):
            await msg.reply_text('Укажите @username канала или его числовой ID вида -100...')
            return
        if not target.startswith('@'): target = int(target)
        try:
            channel = await context.bot.get_chat(target)
            if channel.type != 'channel':
                await msg.reply_text('Можно добавить только канал.'); return
            me = await context.bot.get_chat_member(channel.id, context.bot.id)
            if me.status not in ('administrator','creator','owner'):
                await msg.reply_text('Сначала добавьте бота администратором этого канала.'); return
        except TelegramError:
            await msg.reply_text('Не удалось получить канал. Проверьте ID и права администратора бота.'); return
        url = args[1] if len(args) == 2 else ('https://t.me/'+channel.username if channel.username else '')
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc.lower() not in ('t.me','telegram.me') or not parsed.path.strip('/'):
            await msg.reply_text('Для канала нужна публичная ссылка или ссылка-приглашение https://t.me/...'); return
        store.add_channel(channel.id, (channel.title or 'Канал')[:80], url, channel.username)
        await msg.reply_text('✅ Канал добавлен: ' + channel.title)
    elif command == '/delchannel':
        if len(args) != 1:
            await msg.reply_text('Использование: /delchannel @username или /delchannel -100ID'); return
        value = args[0].lower()
        found = next((c for c in store.channels() if str(c['chat_id']) == value or (c['username'] and '@'+c['username'].lower() == value)), None)
        if not found: await msg.reply_text('Канал не найден. Список: /channels'); return
        store.delete_channel(found['chat_id'])
        await msg.reply_text('Канал удалён из обязательных: '+found['title'])
    elif command == '/broadcast':
        text = msg.text.partition(' ')[2].strip()
        if not text or len(text)>3500:
            await msg.reply_text('Использование: /broadcast текст сообщения (до 3500 символов). Отправка начнётся после подтверждения.')
            return
        if context.bot_data.get('broadcast_active'):
            await msg.reply_text('Рассылка уже выполняется.'); return
        token = secrets.token_hex(8)
        context.user_data['broadcast_draft'] = {'token':token,'text':text,'expires':time.time()+600}
        count = len(store.broadcast_users())
        await msg.reply_text(f'Предпросмотр для {count} получателей:\n\n{text}\n\nОтправить?', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('✅ Отправить',callback_data='broadcast:send:'+token),InlineKeyboardButton('Отмена',callback_data='broadcast:cancel:'+token)]]))


async def broadcast_callback(update, context, store, admin_id):
    q = update.callback_query
    if not admin(update,admin_id):
        await q.answer('Только для администратора.',show_alert=True); return
    parts = (q.data or '').split(':')
    draft = context.user_data.get('broadcast_draft')
    if len(parts)!=3 or not draft or draft['token']!=parts[2] or time.time()>draft['expires']:
        await q.answer('Черновик устарел.',show_alert=True); return
    if parts[1]=='cancel':
        context.user_data.pop('broadcast_draft',None)
        await q.answer('Отменено'); await q.edit_message_reply_markup(reply_markup=None); return
    if parts[1]!='send' or context.bot_data.get('broadcast_active'):
        await q.answer('Рассылка уже выполняется.',show_alert=True); return
    context.bot_data['broadcast_active']=True
    context.user_data.pop('broadcast_draft',None)
    recipients=store.broadcast_users()
    await q.answer('Рассылка запущена')
    await q.edit_message_reply_markup(reply_markup=None)
    async def send_all():
        sent = failed = 0
        try:
            for uid in recipients:
                # Respect opt-outs received while the campaign is running.
                row=store.db.execute('SELECT subscribed FROM bot_users WHERE user_id=?',(uid,)).fetchone()
                if not row or not row['subscribed']: continue
                try:
                    try:
                        await context.bot.send_message(uid, draft['text'])
                    except RetryAfter as exc:
                        delay=exc.retry_after.total_seconds() if hasattr(exc.retry_after,'total_seconds') else float(exc.retry_after)
                        await asyncio.sleep(min(delay+1, 60))
                        await context.bot.send_message(uid,draft['text'])
                    sent += 1
                except TelegramError:
                    failed += 1
                await asyncio.sleep(0.05)
            await context.bot.send_message(admin_id, f'📣 Рассылка завершена. Доставлено: {sent}. Ошибок: {failed}.')
        finally:
            context.bot_data['broadcast_active']=False
    context.application.create_task(send_all())
