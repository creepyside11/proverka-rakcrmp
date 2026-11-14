"""Short-lived pinned SSH sessions for the VPS script manager."""
import asyncio
import base64
import json
import re
import secrets
import time
from pathlib import Path

import asyncssh
from aiohttp import web
import vps_install as vi

SESSIONS = {}
CONNECTING = set()
LIFETIME = 600
REMOTE = Path(__file__).with_name('vps_lua_remote.py')
REMOTE_COMMAND = "runuser -u rakcrmp-black -- /usr/bin/python3 -c 'import base64;exec(base64.b64decode(\"" + base64.b64encode(REMOTE.read_bytes()).decode('ascii') + "\"))'"
ALLOWED = frozenset(('list','upload','get_script_settings','save_script_settings','get_ai','save_ai'))

async def cleanup():
    now=time.monotonic()
    for sid, session in list(SESSIONS.items()):
        if session['expires'] <= now or session['conn'].is_closed():
            SESSIONS.pop(sid,None)
            if session.get('timer'): session['timer'].cancel()
            session['conn'].close()

def renew(sid, session):
    old=session.get('timer')
    if old: old.cancel()
    session['expires']=time.monotonic()+LIFETIME
    def expire():
        if SESSIONS.get(sid) is session:
            SESSIONS.pop(sid,None)
            session['conn'].close()
    session['timer']=asyncio.get_running_loop().call_later(LIFETIME,expire)

async def connect(request, authorize):
    uid=await authorize(request)
    data=await vi.get_json(request)
    await cleanup(); vi.collect_old()
    username,password=data.get('username'),data.get('password')
    probe=vi.PROBES.get(data.get('probe_id')) if isinstance(data.get('probe_id'),str) else None
    if not isinstance(username,str) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}',username): raise web.HTTPBadRequest(text='Укажите корректный SSH-логин.')
    if not isinstance(password,str) or not 1<=len(password)<=512 or data.get('confirmed') is not True: raise web.HTTPBadRequest(text='Сверьте SSH-ключ и укажите пароль.')
    if not probe or probe['uid']!=uid or probe['expires']<time.time(): raise web.HTTPBadRequest(text='SSH-проверка устарела. Подключитесь заново.')
    if len(SESSIONS)+len(CONNECTING)>=20 or uid in CONNECTING or any(s['uid']==uid for s in SESSIONS.values()): raise web.HTTPConflict(text='Сначала закройте прежнее подключение.')
    vi.PROBES.pop(data['probe_id'],None)
    CONNECTING.add(uid)
    conn=None
    try:
        conn=await asyncssh.connect(probe['host'],port=22,username=username,password=password,known_hosts=([probe['key']],[],[]),client_keys=[],agent_path=None,config=None,login_timeout=20)
        result=await conn.run('id -u',check=True,timeout=10)
        root=result.stdout.strip()=='0'
        if not root and (await conn.run('sudo -n true',timeout=10)).exit_status!=0: raise web.HTTPForbidden(text='Нужен root или sudo без пароля.')
        sid=secrets.token_urlsafe(32)
        SESSIONS[sid]={'uid':uid,'conn':conn,'root':root,'expires':time.monotonic()+LIFETIME,'lock':asyncio.Lock()}
        renew(sid,SESSIONS[sid])
        conn=None
        return web.json_response({'session_id':sid,'expires_in':LIFETIME},headers={'Cache-Control':'no-store'})
    except asyncssh.HostKeyNotVerifiable: raise web.HTTPBadGateway(text='SSH-ключ изменился. Подключение остановлено.')
    except asyncssh.PermissionDenied: raise web.HTTPForbidden(text='Неверный SSH-логин или пароль.')
    except (asyncssh.Error,OSError,asyncio.TimeoutError): raise web.HTTPBadGateway(text='Не удалось подключиться к VPS.')
    finally:
        CONNECTING.discard(uid)
        if conn is not None: conn.close()

async def execute(uid, sid, data):
    await cleanup()
    session=SESSIONS.get(sid) if isinstance(sid,str) else None
    if not session or session['uid']!=uid: raise web.HTTPUnauthorized(text='Подключение истекло. Подключитесь к VPS снова.')
    if data.get('action') not in ALLOWED: raise web.HTTPBadRequest(text='Неизвестная операция.')
    try: body=json.dumps(data,ensure_ascii=False,allow_nan=False)
    except (TypeError, ValueError): raise web.HTTPBadRequest(text='Некорректные данные.')
    if len(body.encode())>400000: raise web.HTTPRequestEntityTooLarge(max_size=400000,actual_size=len(body.encode()))
    command=REMOTE_COMMAND if session['root'] else 'sudo -n '+REMOTE_COMMAND
    try:
        async with session['lock']:
            result=await session['conn'].run(command,input=body,timeout=45)
        if result.exit_status!=0 or len(result.stdout)>100000: raise ValueError('remote failed')
        obj=json.loads(result.stdout)
        if not obj.get('ok'): raise web.HTTPBadRequest(text=obj.get('error','Операция не выполнена.'))
    except (asyncssh.Error,OSError,asyncio.TimeoutError,ValueError,json.JSONDecodeError):
        raise web.HTTPBadGateway(text='Не удалось выполнить операцию на VPS. Проверьте подключение и установленную программу.')
    renew(sid,session)
    return obj['result']


async def action(request,authorize):
    uid=await authorize(request)
    data=await vi.get_json(request)
    sid=data.pop('session_id',None)
    result=await execute(uid,sid,data)
    return web.json_response(result,headers={'Cache-Control':'no-store'})

async def close(request,authorize):
    uid=await authorize(request)
    data=await vi.get_json(request)
    sid=data.get('session_id');session=SESSIONS.get(sid) if isinstance(sid,str) else None
    if session and session['uid']==uid:
        SESSIONS.pop(sid,None)
        if session.get('timer'): session['timer'].cancel()
        session['conn'].close()
    return web.json_response({'ok':True},headers={'Cache-Control':'no-store'})
