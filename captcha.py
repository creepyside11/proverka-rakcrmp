"""New-member challenge. No user-submitted code or third-party captcha service."""
import asyncio
import logging
import secrets
import time
from urllib.parse import urlsplit

import httpx

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

def new_challenge(token):
    """Return a question, numeric correct value and buttons; no external service."""
    if secrets.randbelow(2) == 0:
        a, b = secrets.randbelow(9)+1, secrets.randbelow(9)+1
        answer = a+b
        choices = {answer}
        while len(choices) < 4:
            choices.add(max(0, answer + secrets.choice((-5,-4,-3,-2,-1,1,2,3,4,5))))
        values = list(choices)
        secrets.SystemRandom().shuffle(values)
        buttons = [InlineKeyboardButton(str(n), callback_data=f'cap:{token}:{n}') for n in values]
        return f'Сколько будет {a} + {b}?', answer, a, buttons
    icons = [('🍎','яблоко'), ('🚗','машину'), ('⭐','звезду'), ('🌵','кактус'), ('🐱','кота'), ('🍋','лимон')]
    selected = secrets.SystemRandom().sample(icons, 4)
    answer = secrets.randbelow(4)
    buttons = [InlineKeyboardButton(icon, callback_data=f'cap:{token}:{i}') for i,(icon,_) in enumerate(selected)]
    return f'Нажмите на {selected[answer][1]}:', answer, 0, buttons

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
        token = secrets.token_hex(8)
        question, answer, a, buttons = new_challenge(token)
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
            sent = await context.bot.send_message(chat_id=chat,text=f'👋 {name}, подтвердите, что вы человек.\n{question}\nЕсть 10 минут и 3 попытки.',reply_markup=InlineKeyboardMarkup([buttons]))
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
            store.record_captcha(row['token'],'failed')
            store.captcha_update(row['chat'],row['user'],state='failed')
            await query.answer('Время вышло.',show_alert=True)
            await settle(context.bot,store,store.captcha(row['chat'],row['user']))
            return
        if int(parts[2]) == row['correct']:
            store.record_captcha(row['token'],'passed')
            store.captcha_update(row['chat'],row['user'],state='passed')
            await query.answer('Верно! Завершаем проверку.')
            await settle(context.bot,store,store.captcha(row['chat'],row['user']))
            return
        attempts = row['attempts']+1
        store.captcha_update(row['chat'],row['user'],attempts=attempts,state='failed' if attempts >= MAX_ATTEMPTS else 'pending')
        if attempts >= MAX_ATTEMPTS:
            store.record_captcha(row['token'],'failed')
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
                store.record_captcha(row['token'],'failed')
                store.captcha_update(row['chat'],row['user'],state='failed')
                row = store.captcha(row['chat'],row['user'])
            if row['state'] != 'pending':
                await settle(bot,store,row)


def render_widget(token, a, b, choices, attempts, demo=False):
    """Build a self-contained Telegram Widget; the expected answer stays in the bot."""
    if not token.isalnum() or len(token) > 24:
        raise ValueError('Invalid widget token')
    prefix = 'demo:' if demo else ''
    buttons = ''.join(
        f'<button type="button" onclick="send({n})">{n}</button>' for n in choices
    )
    title = 'Тест капчи' if demo else 'Проверка участника'
    note = 'Тест не меняет права и не удаляет участников.' if demo else 'Выберите правильный ответ.'
    initial_status = 'Выберите ответ' if demo else f'Осталось попыток: {attempts}'
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{{box-sizing:border-box}}body{{margin:0;padding:16px 16px max(16px,env(safe-area-inset-bottom));font-family:system-ui,sans-serif;background:var(--tg-theme-bg-color,#fff);color:var(--tg-theme-text-color,#222)}}
.badge{{font-size:12px;font-weight:700;color:var(--tg-theme-hint-color,#777)}}h3{{font-size:20px;margin:8px 0}}p{{margin:8px 0;line-height:1.35}}.note{{color:var(--tg-theme-hint-color,#777)}}
.question{{margin:14px 0;padding:14px;border-radius:14px;text-align:center;font-size:24px;font-weight:700;background:var(--tg-theme-secondary-bg-color,#f2f2f5)}}
.choices{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}}button{{min-height:44px;border:0;border-radius:12px;background:var(--tg-theme-button-color,#8774e1);color:var(--tg-theme-button-text-color,#fff);font:600 17px system-ui,sans-serif}}button:disabled{{opacity:.55}}#status{{min-height:20px;font-size:13px;overflow-wrap:anywhere}}
</style></head><body><div class="badge">RAKCRMP · КАПЧА</div><h3>{title}</h3>
<p class="note">{note}</p><div class="question">{a} + {b} = ?</div>
<div class="choices">{buttons}</div><p id="status" role="status">{initial_status}</p>
<script>
let busy=false;
function send(n){{
 if(busy)return;
 if(typeof TelegramWidget==='undefined'){{document.getElementById('status').textContent='Откройте виджет в Telegram.';return}}
 busy=true;
 const buttons=document.querySelectorAll('button');
 buttons.forEach(b=>b.disabled=true);
 document.getElementById('status').textContent='Проверяем…';
 TelegramWidget.request('{prefix}{token}:'+n).then(
  answer=>{{document.getElementById('status').textContent=answer.result||'Ответ получен.'}},
  error=>{{document.getElementById('status').textContent='Ошибка связи ('+(error.code||'FAILED')+'). Попробуйте снова.'}}
 ).finally(()=>{{busy=false;buttons.forEach(b=>b.disabled=false)}});
}}
if(typeof TelegramWidget!=='undefined')TelegramWidget.ready();
</script></body></html>"""

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
        widget = render_widget(token, a, answer-a, choices, MAX_ATTEMPTS-row['attempts'])
        try:
            sent = await context.bot._post('sendWidget', {'chat_id': user.id, 'html': widget, 'allow_queries': True, 'caption': 'Если виджет не отображается, вернитесь в группу и используйте кнопки под сообщением.'})
            store.captcha_update(row['chat'],row['user'],widget_message_id=sent['message_id'])
        except (TelegramError, KeyError) as exc:
            log.warning('Не удалось отправить виджет: %s: %s', type(exc).__name__, str(exc))
            await update.effective_message.reply_text('Не удалось открыть виджет. Вернитесь в группу и ответьте кнопками под сообщением.')

async def probe_widget_json(bot, chat_id):
    """Compare PTB's request with Bot API JSON; never log the token-bearing URL."""
    base = bot.base_url.rstrip('/')
    parsed = urlsplit(base)
    if parsed.scheme != 'https' or parsed.hostname != 'api.telegram.org' or parsed.port not in (None, 443):
        return 'skipped', 'Используется нестандартный адрес Bot API.'
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(base + '/sendWidget', json={
                'chat_id': chat_id, 'html': '<p>Проверка виджета</p>'})
        result = response.json()
        if response.is_success and result.get('ok') is True:
            return 'ok', ''
        # Telegram error descriptions normally contain no credentials. Do not log
        # the raw body: intermediary servers can return sensitive details.
        error = str(result.get('description', 'неизвестная ошибка'))
        if 'INPUT_MEDIA_WIDGET_NOT_ALLOWED' in error.upper():
            return 'rejected', 'INPUT_MEDIA_WIDGET_NOT_ALLOWED'
        return 'rejected', f'HTTP {response.status_code}'
    except (httpx.HTTPError, ValueError):
        return 'unavailable', 'Не удалось связаться с Bot API напрямую.'

async def test_widget(update, context):
    """Admin-only caller creates a real widget without changing group membership."""
    a, b = secrets.randbelow(9) + 1, secrets.randbelow(9) + 1
    token = secrets.token_hex(8)
    choices = [a+b, a+b+1, a+b+2, a+b-1]
    secrets.SystemRandom().shuffle(choices)
    html = render_widget(token, a, b, choices, MAX_ATTEMPTS, demo=True)
    try:
        sent = await context.bot._post('sendWidget', {'chat_id': update.effective_user.id, 'html': html, 'allow_queries': True,
            'caption': 'Тест не ограничивает и не удаляет пользователя.'})
        context.bot_data['widget_demo'] = {'token': token, 'user': update.effective_user.id,
                                           'message_id': sent['message_id'], 'answer': a+b}
    except (TelegramError, KeyError) as exc:
        log.warning('Ошибка создания тестового виджета: %s: %s', type(exc).__name__, str(exc))
        if 'input_media_widget_not_allowed' not in str(exc).casefold():
            await update.effective_message.reply_text('Не удалось создать виджет. Подробности ошибки смотрите в журнале бота.')
            return
        # Probe the smallest static widget: distinguishes a general restriction from
        # a rejection specific to the interactive request. Never change the API host.
        try:
            await context.bot._post('sendWidget', {'chat_id': update.effective_user.id,
                'html': '<p>Тест виджета Telegram</p>', 'caption': 'Диагностика виджета'})
        except TelegramError as probe_exc:
            log.warning('Ошибка минимального виджета: %s: %s', type(probe_exc).__name__, str(probe_exc))
            status, detail = await probe_widget_json(context.bot, update.effective_user.id)
            log.info('Проверка виджета прямым JSON: status=%s detail=%s', status, detail)
            if status == 'ok':
                await update.effective_message.reply_text(
                    'Прямой JSON-запрос отправил простой виджет, а запрос через библиотеку — нет. '
                    'Сообщите администратору: JSON успешно, библиотека отклонила запрос.')
            elif status == 'rejected':
                await update.effective_message.reply_text(
                    f'Прямой JSON-запрос тоже отклонён: {detail}. '
                    'Проверьте, что чат личный, и передайте этот результат разработчику.')
            else:
                await update.effective_message.reply_text(
                    f'Основной запрос отклонён; независимая проверка недоступна: {detail}')
        else:
            await update.effective_message.reply_text(
                'Простой виджет отправлен, но интерактивный Telegram отклонил. '
                'Это помогает проверить ограничение для запросов из виджета.')

async def widget_query(update, context, store):
    raw = update.api_kwargs.get('widget_query')
    if not raw: return
    query_id = raw.get('id')
    data = raw.get('data') or ''
    if data.startswith('demo:'):
        demo = context.bot_data.get('widget_demo', {})
        parts = data.split(':')
        valid = (len(parts) == 3 and parts[1] == demo.get('token')
                 and raw.get('from', {}).get('id') == demo.get('user')
                 and raw.get('chat', {}).get('id') == demo.get('user')
                 and raw.get('chat', {}).get('type') == 'private'
                 and raw.get('message_id') == demo.get('message_id'))
        response = ('Верно! Тест пройден.' if valid and parts[2] == str(demo.get('answer'))
                    else 'Неверно. Попробуйте ещё раз.' if valid else 'Тест недоступен.')
        try:
            await context.bot._post('answerWidgetQuery', {'widget_query_id': query_id, 'data': response})
        except TelegramError as exc:
            log.warning('Не удалось ответить тестовому виджету: %s', type(exc).__name__)
        return
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
