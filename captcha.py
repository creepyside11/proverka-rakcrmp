"""New-member challenge. No user-submitted code or third-party captcha service."""
import asyncio
import logging
import secrets
import time

from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

log = logging.getLogger('moderator.captcha')
TIME_LIMIT = 600
MAX_ATTEMPTS = 3
# Serializes callbacks, member changes and timeout recovery in a single running bot.
lock = asyncio.Lock()

def present(member):
    return member.status in ('member', 'administrator', 'creator') or (member.status == 'restricted' and member.is_member)

def keyboard(token, answer, username):
    choices = {answer}
    while len(choices) < 4:
        choices.add(max(0, answer + secrets.choice((-5, -4, -3, -2, -1, 1, 2, 3, 4, 5))))
    choices = list(choices)
    secrets.SystemRandom().shuffle(choices)
    return InlineKeyboardMarkup([[InlineKeyboardButton('🧩 Открыть виджет капчи', url=f'https://t.me/{username}?start=cap_{token}')], [InlineKeyboardButton(str(n), callback_data=f'cap:{token}:{n}') for n in choices]])

async def finish_message(bot, row, text):
    if row['message_id'] is None: return
    try:
        await bot.edit_message_text(chat_id=row['chat'], message_id=row['message_id'], text=text)
    except TelegramError as exc:
        log.warning('Не удалось обновить сообщение капчи: %s', type(exc).__name__)

async def settle(bot, store, row):
    """Idempotent staged recovery: save each successful transition before the next call."""
    chat, user, state = row['chat'], row['user'], row['state']
    try:
        if state == 'passed':
            await bot.restrict_chat_member(chat, user, ChatPermissions.all_permissions(), use_independent_chat_permissions=True)
            store.captcha_remove(chat,user)
            await finish_message(bot,row,'✅ Проверка пройдена. Добро пожаловать!')
        elif state == 'failed':
            await bot.ban_chat_member(chat,user)
            store.captcha_update(chat,user,state='banned')
            row = store.captcha(chat,user)
            state = 'banned'
        if state == 'banned':
            # A kick is a ban followed by unban; the participant can then rejoin.
            await bot.unban_chat_member(chat,user,only_if_banned=True)
            store.captcha_remove(chat,user)
            await finish_message(bot,row,'❌ Проверка не пройдена. Участник удалён; можно присоединиться и попробовать снова.')
    except TelegramError as exc:
        log.warning('Не удалось завершить капчу chat=%s user=%s state=%s: %s; повторим', chat,user,state,type(exc).__name__)
        return False
    return True

async def member_update(update, context, store, admin_id):
    change = update.chat_member
    if not change or change.chat.type != 'supergroup': return
    old, new = change.old_chat_member, change.new_chat_member
    user = new.user
    if user.is_bot or user.id == admin_id: return
    chat = change.chat.id
    async with lock:
        if present(old) and not present(new):
            previous = store.captcha(chat,user.id)
            # A ban triggered by our own kick must not remove its recovery state.
            if previous and previous['state'] not in ('failed','banned'):
                store.captcha_remove(chat,user.id)
                await finish_message(context.bot,previous,'Проверка отменена: участник покинул группу.')
            return
        if present(old) or not present(new): return
        # Avoid overriding a pre-existing individual restriction or an administrator.
        if new.status != 'member':
            log.warning('Капча пропущена для участника с особыми правами chat=%s user=%s status=%s',chat,user.id,new.status)
            return
        if store.captcha(chat,user.id): return
        a, b = secrets.randbelow(9)+1, secrets.randbelow(9)+1
        answer = a+b
        token = secrets.token_hex(8)
        store.create_captcha(chat,user.id,token,answer,time.time()+TIME_LIMIT,a)
        try:
            await context.bot.restrict_chat_member(chat,user.id,ChatPermissions.no_permissions(),use_independent_chat_permissions=True)
        except TelegramError as exc:
            store.captcha_remove(chat,user.id)
            log.warning('Не удалось ограничить пользователя chat=%s user=%s: %s',chat,user.id,type(exc).__name__)
            try:
                await context.bot.send_message(chat_id=chat,text='⚠️ Не удалось включить капчу для нового участника: проверьте право бота «Блокировка пользователей».')
            except TelegramError: pass
            return
        try:
            name = (user.full_name or 'новый участник')[:100]
            sent = await context.bot.send_message(chat_id=chat,text=f'👋 {name}, подтвердите, что вы человек.\nСколько будет {a} + {b}?\nОткройте виджет или выберите ответ ниже. Есть 10 минут и 3 попытки.',reply_markup=keyboard(token,answer,context.bot.username))
            store.captcha_update(chat,user.id,message_id=sent.message_id,state='pending')
        except TelegramError as exc:
            # Do not silently leave a user muted without a question. Persist until restore succeeds.
            log.warning('Не удалось отправить капчу: %s',type(exc).__name__)
            store.captcha_update(chat,user.id,state='passed')
            await settle(context.bot,store,store.captcha(chat,user.id))

async def answer(update, context, store):
    query = update.callback_query
    parts = (query.data or '').split(':')
    if len(parts) != 3 or not parts[2].isdigit():
        await query.answer('Некорректный ответ.',show_alert=True)
        return
    async with lock:
        row = store.captcha_token(parts[1])
        if not row or row['state'] != 'pending':
            await query.answer('Проверка уже завершена.',show_alert=True)
            return
        if not update.effective_user or update.effective_user.id != row['user']:
            await query.answer('Эта проверка предназначена другому участнику.',show_alert=True)
            return
        if not query.message or query.message.chat.id != row['chat'] or query.message.message_id != row['message_id']:
            await query.answer('Это устаревшая проверка.',show_alert=True)
            return
        if time.time() >= row['expires']:
            store.captcha_update(row['chat'],row['user'],state='failed')
            await query.answer('Время вышло.',show_alert=True)
            await settle(context.bot,store,store.captcha(row['chat'],row['user']))
            return
        if int(parts[2]) == row['correct']:
            store.captcha_update(row['chat'],row['user'],state='passed')
            await query.answer('Верно! Завершаем проверку.')
            await settle(context.bot,store,store.captcha(row['chat'],row['user']))
            return
        attempts = row['attempts']+1
        store.captcha_update(row['chat'],row['user'],attempts=attempts,state='failed' if attempts >= MAX_ATTEMPTS else 'pending')
        if attempts >= MAX_ATTEMPTS:
            await query.answer('Три ошибки. Проверка не пройдена.',show_alert=True)
            await settle(context.bot,store,store.captcha(row['chat'],row['user']))
        else:
            await query.answer(f'Неверно. Осталось попыток: {MAX_ATTEMPTS-attempts}.',show_alert=True)

async def recover(bot, store):
    async with lock:
        for row in store.captcha_waiting():
            if row['state'] == 'preparing':
                # Interrupted before the challenge became visible: safely undo the mute.
                store.captcha_update(row['chat'],row['user'],state='passed')
                row = store.captcha(row['chat'],row['user'])
            if row['state'] == 'pending' and time.time() >= row['expires']:
                store.captcha_update(row['chat'],row['user'],state='failed')
                row = store.captcha(row['chat'],row['user'])
            if row['state'] != 'pending':
                await settle(bot,store,row)

async def open_widget(update, context, store, token):
    """Deep link must be opened by the same account that joined the group."""
    async with lock:
        row = store.captcha_token(token)
        user = update.effective_user
        if not row or not user or row['user'] != user.id or row['state'] != 'pending':
            await update.effective_message.reply_text('Проверка не найдена или принадлежит другому участнику.')
            return
        if time.time() >= row['expires']:
            store.captcha_update(row['chat'],row['user'],state='failed')
            await update.effective_message.reply_text('Время проверки вышло.')
            await settle(context.bot,store,store.captcha(row['chat'],row['user']))
            return
        answer, a = row['correct'], row['question_a']
        choices = {answer}
        while len(choices) < 4:
            choices.add(max(0, answer + secrets.choice((-5,-4,-3,-2,-1,1,2,3,4,5))))
        choices = list(choices)
        secrets.SystemRandom().shuffle(choices)
        buttons = ''.join(f'<button onclick="send({n})">{n}</button> ' for n in choices)
        # The answer is only in server-side state, not in the widget's source.
        widget = f'''<h3>🧩 Проверка участника</h3><p>Сколько будет {a} + {answer-a}?</p>
{buttons}<p id="status">Осталось попыток: {MAX_ATTEMPTS-row['attempts']}</p>
<script>function send(n){{TelegramWidget.request('{token}:'+n).then(a=>{{document.getElementById('status').textContent=a.result||'Ответ получен';}},()=>{{document.getElementById('status').textContent='Ошибка связи. Попробуйте снова.';}});}}</script>'''
        try:
            sent = await context.bot._post('sendWidget', {'chat_id': user.id, 'html': widget, 'allow_queries': True, 'caption': 'Если виджет не отображается, вернитесь в группу и используйте кнопки под сообщением.'})
            store.captcha_update(row['chat'],row['user'],widget_message_id=sent['message_id'])
        except (TelegramError, KeyError) as exc:
            log.warning('Не удалось отправить виджет: %s',type(exc).__name__)
            await update.effective_message.reply_text('Не удалось открыть виджет. Вернитесь в группу и ответьте кнопками под сообщением.')

async def widget_query(update, context, store):
    raw = update.api_kwargs.get('widget_query')
    if not raw: return
    query_id = raw.get('id')
    response = 'Проверка недоступна.'
    settle_row = None
    async with lock:
        parts = (raw.get('data') or '').split(':')
        row = store.captcha_token(parts[0]) if len(parts) == 2 else None
        if (row and row['state'] == 'pending' and parts[1].isdigit()
            and raw.get('from',{}).get('id') == row['user']
            and raw.get('chat',{}).get('type') == 'private'
            and raw.get('chat',{}).get('id') == row['user']
            and raw.get('message_id') == row['widget_message_id']):
            if time.time() >= row['expires']:
                store.captcha_update(row['chat'],row['user'],state='failed')
                response = 'Время вышло. Проверка не пройдена.'
                settle_row = store.captcha(row['chat'],row['user'])
            elif int(parts[1]) == row['correct']:
                store.captcha_update(row['chat'],row['user'],state='passed')
                response = 'Верно! Завершаем проверку; вернитесь в группу.'
                settle_row = store.captcha(row['chat'],row['user'])
            else:
                attempts = row['attempts']+1
                store.captcha_update(row['chat'],row['user'],attempts=attempts,state='failed' if attempts >= MAX_ATTEMPTS else 'pending')
                if attempts >= MAX_ATTEMPTS:
                    response = 'Три ошибки. Проверка не пройдена.'
                    settle_row = store.captcha(row['chat'],row['user'])
                else:
                    response = f'Неверно. Осталось попыток: {MAX_ATTEMPTS-attempts}.'
        try:
            await context.bot._post('answerWidgetQuery', {'widget_query_id':query_id, 'data':response})
        except TelegramError as exc:
            log.warning('Не удалось ответить виджету: %s',type(exc).__name__)
        if settle_row:
            await settle(context.bot,store,settle_row)
