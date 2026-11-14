"""Fixed operations executed as the RakCRMP service user on the customer's VPS."""
import json
import ast
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path('/opt/rakcrmp-black')
MAX_SCRIPT = 256 * 1024
NAME = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}\.lua$', re.I)


def valid_name(name):
    return isinstance(name, str) and name.endswith('.lua') and bool(NAME.fullmatch(name)) and '..' not in name


def regular(path):
    try: return stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError: return False


def check_dir(path):
    if path.is_symlink(): raise ValueError('Символические ссылки запрещены')
    if path.exists() and not path.is_dir(): raise ValueError('Некорректный каталог')
    path.mkdir(mode=0o700, parents=False, exist_ok=True)


def atomic(path, text, mode=0o600):
    if path.is_symlink() or (path.exists() and not regular(path)): raise ValueError('Некорректный файл')
    fd, temp = tempfile.mkstemp(prefix='.rakcrmp-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def valid_value(value, depth=0):
    if depth>6: return False
    if value is None or isinstance(value, bool): return True
    if isinstance(value, (int, float)):
        import math
        try: return abs(value)<1e100 and math.isfinite(value)
        except (OverflowError, ValueError): return False
    if isinstance(value, str): return len(value)<=2048
    if isinstance(value, list): return len(value)<=128 and all(valid_value(x,depth+1) for x in value)
    if isinstance(value, dict):
        return len(value)<=64 and all(isinstance(k,str) and 1<=len(k)<=128 and valid_value(v,depth+1) for k,v in value.items())
    return False


def process(data):
    root = ROOT
    if root.is_symlink() or not (root/'bot.py').is_file(): raise ValueError('Установка RakCRMP не найдена')
    action = data.get('action')
    scripts = root/'scripts'
    if action in ('list','upload','get_script_settings','save_script_settings'):
        check_dir(scripts)
        folder = scripts/'lua'; check_dir(folder)
    if action == 'list':
        names = sorted(p.name for p in folder.iterdir() if valid_name(p.name) and regular(p) and not p.is_symlink())[:500]
        return {'files':names,'lua_ready':(root/'logic/lua_runtime.py').is_file(),
                'ai_ready':(root/'logic/openai_captcha.py').is_file(), 'settings_ready':(root/'logic/lua_settings.py').is_file()}
    if action == 'upload':
        name, text = data.get('name'), data.get('text')
        if not valid_name(name) or not isinstance(text,str) or not text or len(text.encode('utf-8')) > MAX_SCRIPT or '\x00' in text:
            raise ValueError('Нужен текстовый .lua файл до 256 КиБ и корректное имя')
        path = folder/name
        if path.exists() or path.is_symlink():
            if not path.is_symlink() and regular(path) and path.stat().st_size<=MAX_SCRIPT and path.read_bytes()==text.encode('utf-8'):
                return {'saved':name,'already':True}
            raise ValueError('Файл уже существует; выберите другое имя')
        # No overwrite, even if two uploads race.
        fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as f: f.write(text)
        return {'saved':name}
    if action in ('get_script_settings','save_script_settings'):
        name=data.get('name')
        if not valid_name(name) or not regular(folder/name) or (folder/name).is_symlink(): raise ValueError('Скрипт не найден')
        config_dir=root/'.lua-settings';check_dir(config_dir)
        path=config_dir/(name+'.json')
        if action=='get_script_settings':
            if not path.exists(): return {'enabled':True, 'settings':{}}
            if not regular(path) or path.is_symlink() or path.stat().st_size>16384: raise ValueError('Некорректный файл настроек')
            stored=json.loads(path.read_text(encoding='utf-8'))
            if isinstance(stored,dict) and isinstance(stored.get('values'),dict) and isinstance(stored.get('enabled'),bool):
                return {'enabled':stored['enabled'],'settings':stored['values']}
            return {'enabled':True,'settings':stored if isinstance(stored,dict) else {}}
        settings=data.get('settings')
        if not isinstance(settings,dict) or len(settings)>64: raise ValueError('Настройки должны быть JSON-объектом')
        if not valid_value(settings): raise ValueError('Некорректные настройки или превышена вложенность')
        enabled=data.get('enabled',True)
        if not isinstance(enabled,bool): raise ValueError('Некорректное состояние настроек')
        text=json.dumps({'enabled':enabled,'values':settings},ensure_ascii=False,allow_nan=False,indent=2)+'\n'
        if len(text.encode())>16384: raise ValueError('Слишком много настроек')
        atomic(path,text)
        return {'saved':name}
    if action in ('get_ai','save_ai'):
        path=root/'.env'
        if path.is_symlink() or (path.exists() and (not regular(path) or path.stat().st_size>65536)): raise ValueError('Некорректный .env')
        values={}
        if path.exists():
            for line in path.read_text(encoding='utf-8').splitlines():
                match=re.match(r'^\s*(?:export\s+)?(CAPTCHA_BASE_URL|CAPTCHA_MODEL|CAPTCHA_API_TOKEN)\s*=\s*(.*)$',line)
                if not match: continue
                key,value=match.groups()
                try: values[key]=ast.literal_eval(value) if value.startswith(('"',"'")) else value.strip()
                except (ValueError,SyntaxError): raise ValueError('Некорректный .env')
        keys=('CAPTCHA_BASE_URL','CAPTCHA_MODEL','CAPTCHA_API_TOKEN')
        if action=='get_ai':
            return {'base_url':values.get(keys[0]) or '', 'model':values.get(keys[1]) or '', 'token_set':bool(values.get(keys[2])), 'ai_ready':(root/'logic/openai_captcha.py').is_file()}
        base, model, token=data.get('base_url'), data.get('model'), data.get('token','')
        if not all(isinstance(x,str) and '\n' not in x and '\r' not in x and '\x00' not in x for x in (base,model,token)):
            raise ValueError('Некорректные параметры')
        url=urlsplit(base.strip())
        if url.scheme!='https' or not url.hostname or url.username or url.password or url.query or url.fragment or not 1<=len(model.strip())<=200 or len(token)>1024:
            raise ValueError('Нужен HTTPS Base URL и ID модели')
        if not token and not values.get(keys[2]): raise ValueError('Укажите API Token')
        # Update only our lines; preserve all unrelated environment settings.
        existing=path.read_text(encoding='utf-8').splitlines() if path.exists() else []
        existing=[line for line in existing if not re.match(r'^\s*(?:export\s+)?(?:CAPTCHA_BASE_URL|CAPTCHA_MODEL|CAPTCHA_API_TOKEN)\s*=',line)]
        values[keys[0]]=base.strip();values[keys[1]]=model.strip()
        if token: values[keys[2]]=token
        # JSON strings are valid double-quoted dotenv values and escape quotes/backslashes.
        existing += [k+'='+json.dumps(values[k],ensure_ascii=False) for k in keys]
        atomic(path,'\n'.join(existing)+'\n')
        return {'saved':True,'ai_ready':(root/'logic/openai_captcha.py').is_file()}
    raise ValueError('Неизвестная операция')


def main():
    try:
        data=json.load(sys.stdin)
        result={'ok':True,'result':process(data)}
    except (ValueError,TypeError,KeyError,OSError,ImportError,json.JSONDecodeError):
        result={'ok':False,'error':'Операция не выполнена. Проверьте файлы, права и введённые данные.'}
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))

if __name__=='__main__': main()
