"""Administrator configuration for future prepaid model products."""
from telegram import InlineKeyboardMarkup
from cryptography.fernet import InvalidToken
import support_ai
import shop

MODELS={'deepseek_flash':'DeepSeek V4.1 Flash','glm':'GLM 5.3','gpt_sol':'GPT 6.1 Sol','gpt_astra':'GPT 6 Astra'}


def init(store):
    store.db.executescript('''CREATE TABLE IF NOT EXISTS ai_model_settings (
      id TEXT PRIMARY KEY, base_url TEXT NOT NULL, model_id TEXT NOT NULL,
      token_cipher TEXT NOT NULL, price_kopecks BIGINT NOT NULL);''')
    store.db.commit()


def config(store,key):
    row=store.db.execute('SELECT * FROM ai_model_settings WHERE id=?',(key,)).fetchone()
    return dict(row) if row else None


def save(store, bot_token, key, base, model, cipher, amount):
    if key not in MODELS:raise ValueError('Неизвестная модель.')
    base=support_ai.validate_base(base)
    if not isinstance(model,str) or not 1<=len(model)<=200 or any(c in model for c in '\r\n\x00'):raise ValueError('Некорректный ID модели.')
    if not isinstance(amount,int) or isinstance(amount,bool) or not 1<=amount<=100000000:raise ValueError('Некорректная цена.')
    try:
        token=support_ai.crypt(bot_token).decrypt(cipher.encode()).decode()
    except (ValueError,InvalidToken,UnicodeError):raise ValueError('Укажите токен заново.')
    if not token or len(token)>2048 or any(c.isspace() for c in token):raise ValueError('Некорректный токен.')
    store.db.execute('INSERT OR IGNORE INTO ai_model_settings(id,base_url,model_id,token_cipher,price_kopecks) VALUES(?,?,?,?,?)',(key,base,model,cipher,amount))
    store.db.execute('UPDATE ai_model_settings SET base_url=?,model_id=?,token_cipher=?,price_kopecks=? WHERE id=?',(base,model,cipher,amount,key))
    store.db.commit()


async def menu(update,context,store,admin_id):
    if update.effective_user.id!=admin_id or not update.effective_message or update.effective_message.chat.type!='private':return
    rows=[[shop.b(('✅ ' if config(store,k) else '⚙️ ')+name,data='aimodel:'+k)] for k,name in MODELS.items()]
    await update.effective_message.reply_text('Настройки моделей ИИ. Выберите модель и настройте Base URL, ID модели и токен провайдера. Пользователи получают бесплатные токены в Mini App.',reply_markup=InlineKeyboardMarkup(rows))


async def choose(update,context,store,admin_id):
    q=update.callback_query
    if update.effective_user.id!=admin_id or not q.message or q.message.chat.type!='private':await q.answer('Доступ запрещён.',show_alert=True);return
    key=(q.data or '').partition(':')[2]
    if key not in MODELS:await q.answer('Неизвестная модель.',show_alert=True);return
    if context.user_data.get('product_draft') or context.user_data.get('donation_draft'):
        await q.answer('Сначала завершите текущее действие или отправьте /cancel.',show_alert=True);return
    context.user_data['ai_model_draft']={'key':key,'step':'base'}
    await q.answer();await q.message.reply_text('Модель: '+MODELS[key]+'\nВведите публичный HTTPS Base URL провайдера. Для отмены: /cancel.')


async def input(update,context,store,admin_id,bot_token):
    msg=update.effective_message
    if not msg or msg.chat.type!='private' or update.effective_user.id!=admin_id:return False
    draft=context.user_data.get('ai_model_draft')
    if not draft:return False
    text=(msg.text or '').strip()
    if text.split('@')[0]=='/cancel':
        context.user_data.pop('ai_model_draft',None);await msg.reply_text('Настройка модели отменена.');return True
    if text.startswith('/'):return False
    if draft['step']=='base':
        try:draft['base']=support_ai.validate_base(text)
        except ValueError as exc:await msg.reply_text(str(exc));return True
        draft['step']='model';await msg.reply_text('Введите ID модели у этого провайдера.');return True
    if draft['step']=='model':
        if not 1<=len(text)<=200 or any(c in text for c in '\r\n\x00'):await msg.reply_text('ID модели должен содержать от 1 до 200 символов.');return True
        draft.update(model=text,step='token');await msg.reply_text('Введите API Token провайдера. Отправьте «-», чтобы сохранить прежний токен.');return True
    if draft['step']=='token':
        current=config(store,draft['key'])
        if text=='-' and not current:
            await msg.reply_text('Сохранённого токена нет. Укажите API Token.');return True
        if text=='-':cipher=current['token_cipher']
        else:
            if text.lower().startswith('bearer '):text=text[7:].strip()
            if not text or len(text)>2048 or any(c.isspace() for c in text):await msg.reply_text('Нужен API Token без пробелов.');return True
            cipher=support_ai.crypt(bot_token).encrypt(text.encode()).decode()
        try:
            save(store,bot_token,draft['key'],draft['base'],draft['model'],cipher,current['price_kopecks'] if current else 1)
        except ValueError:
            await msg.reply_text('Не удалось сохранить настройки. Проверьте токен и попробуйте снова.');return True
        context.user_data.pop('ai_model_draft',None);await msg.reply_text('✅ Данные модели сохранены. Токены выдаются бесплатно в Mini App.');return True
    return False
