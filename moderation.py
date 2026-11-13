import re
from pathlib import PurePath

def is_lua(filename):
    return PurePath(filename or '').suffix.lower() in ('.lua', '.luac')

def check_text(text, entities, settings, words):
    text = text or ''
    if settings['ads']:
        if any(getattr(e, 'type', '') in ('url', 'text_link', 'mention') for e in (entities or [])) or re.search(r'(?i)(?:https?\s*://|www\.|t\s*\.\s*me/|@[a-z][a-z\d_]{4,}|\b[a-z\d-]+\.(?:ru|com|net|org|gg|io)\b)', text):
            return 'ссылка или реклама запрещена в этой теме'
    if settings['insults']:
        normalized = text.casefold().replace('ё','е')
        for word in words:
            if re.search(r'(?<!\w)' + re.escape(word.casefold().replace('ё','е')) + r'(?!\w)', normalized):
                return 'сообщение содержит запрещённое слово'
    return None
