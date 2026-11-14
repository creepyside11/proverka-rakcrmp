"""Fixed-purpose VPS installer; SSH credentials are never logged or persisted locally."""
import asyncio
import ipaddress
import logging
import re
import secrets
import shlex
import time

import asyncssh
from aiohttp import web

# Avoid routine SSH connection logs containing customer VPS addresses/usernames.
logging.getLogger('asyncssh').setLevel(logging.WARNING)

PINNED_COMMIT = 'ad324c9eeb6a0022aed4ac31203c04b670d324fc'
PROBES = {}
JOBS = {}
ACTIVE = set()
SLOTS = asyncio.Semaphore(3)
LAST_PROBE = {}


def public_ip(value):
    if not isinstance(value,str) or len(value)>45: raise ValueError('Укажите публичный IP VPS.')
    try: ip=ipaddress.ip_address(value.strip())
    except ValueError: raise ValueError('Укажите числовой публичный IP VPS.')
    if not ip.is_global or ip.is_multicast or ip.is_unspecified or ip.is_loopback:
        raise ValueError('Разрешён только публичный адрес VPS.')
    return str(ip)


def collect_old():
    now=time.time()
    for key,val in list(PROBES.items()):
        if val['expires']<now: PROBES.pop(key,None)
    for key,val in list(JOBS.items()):
        if val.get('finished') and val['finished']<now-3600: JOBS.pop(key,None)
    for key,val in list(LAST_PROBE.items()):
        if val<now-600: LAST_PROBE.pop(key,None)


async def get_json(request):
    try: data=await request.json()
    except (ValueError,TypeError): raise web.HTTPBadRequest(text='Некорректный запрос.')
    if not isinstance(data,dict): raise web.HTTPBadRequest(text='Некорректный запрос.')
    return data


async def probe(request, authorize):
    uid=await authorize(request); data=await get_json(request);collect_old()
    try: host=public_ip(data.get('host'))
    except ValueError as exc: raise web.HTTPBadRequest(text=str(exc))
    if uid in ACTIVE: raise web.HTTPConflict(text='Установка уже выполняется.')
    now=time.time()
    if now-LAST_PROBE.get(uid,0)<10: raise web.HTTPTooManyRequests(text='Подождите 10 секунд и попробуйте снова.')
    LAST_PROBE[uid]=now
    try:
        async with SLOTS:
            key=await asyncio.wait_for(asyncssh.get_server_host_key(host,port=22,config=None),timeout=12)
        if key is None: raise OSError('No host key')
    except (OSError,asyncio.TimeoutError,asyncssh.Error):
        raise web.HTTPBadGateway(text='Не удалось получить SSH-ключ. Проверьте IP и доступность порта 22.')
    key_id=secrets.token_urlsafe(24)
    for k,v in list(PROBES.items()):
        if v['uid']==uid: PROBES.pop(k,None)
    PROBES[key_id]={'uid':uid,'host':host,'key':key,'expires':time.time()+300}
    return web.json_response({'probe_id':key_id,'host':host,'fingerprint':key.get_fingerprint('sha256')},headers={'Cache-Control':'no-store'})


def validate(data):
    user=data.get('username');password=data.get('password');token=data.get('bot_token');admin=data.get('admin_id')
    if not isinstance(user,str) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}',user): raise ValueError('Укажите корректный логин SSH.')
    if not isinstance(password,str) or not 1<=len(password)<=512: raise ValueError('Укажите пароль SSH.')
    if not isinstance(token,str) or not re.fullmatch(r'\d{5,15}:[A-Za-z0-9_-]{20,100}',token): raise ValueError('Неверный формат токена бота.')
    if not isinstance(admin,str) or not re.fullmatch(r'[1-9]\d{0,19}',admin): raise ValueError('Укажите числовой ID администратора.')
    if data.get('confirmed') is not True or data.get('os')!='ubuntu-26.04': raise ValueError('Подтвердите проверку ключа и условия установки.')
    return user,password,token,admin


def installation_script(token,admin):
    # The only substitutions are prevalidated and shell-quoted. This script is sent
    # through SSH stdin, never argv, logs, a web response, or a temporary file here.
    return '''set -eu
export DEBIAN_FRONTEND=noninteractive
umask 077
if [ "$(id -u)" -ne 0 ]; then exit 21; fi
if [ ! -r /etc/os-release ]; then exit 22; fi
. /etc/os-release
if [ "$ID" != ubuntu ] || [ "$VERSION_ID" != 26.04 ]; then exit 22; fi
if [ -e /opt/rakcrmp-black ] || [ -e /opt/rakcrmp-black-runtime ] || [ -e /etc/rakcrmp-black ] || id rakcrmp-black >/dev/null 2>&1 || [ -e /etc/systemd/system/rakcrmp-black.service ]; then exit 23; fi
command -v systemctl >/dev/null 2>&1 || exit 24
[ -d /run/systemd/system ] || exit 24
if systemctl cat rakcrmp-black.service >/dev/null 2>&1; then exit 23; fi
case "$(uname -m)" in
  x86_64|amd64) sb_arch=amd64; sb_sha=950af37eb2d7e55dddae34a18411cd617303fd99d2dc75bc76b6dd9fcd97d9c5 ;;
  aarch64|arm64) sb_arch=arm64; sb_sha=20a6a9cd259a95411599f811a5066513a98db63705a51121252ad27daf96c029 ;;
  armv7l|armv7) sb_arch=armv7; sb_sha=490461d5a107847080b896d575a6cb74b31b64e2ad8bbb47635f6d227e73d7c8 ;;
  *) exit 29 ;;
esac
apt-get update -qq >/dev/null 2>&1 || exit 27
apt-get install -y -qq git python3 python3-venv curl ca-certificates >/dev/null 2>&1 || exit 27
python3 -c 'import sys; assert sys.version_info >= (3,10)' || exit 25
tmp_dir=$(mktemp -d)
trap 'rm -rf -- "$tmp_dir"' EXIT
sb_archive="sing-box-1.11.15-linux-${sb_arch}.tar.gz"
sb_url="https://github.com/SagerNet/sing-box/releases/download/v1.11.15/${sb_archive}"
curl --proto '=https' --tlsv1.2 -fsSL --retry 2 --connect-timeout 15 -o "$tmp_dir/$sb_archive" "$sb_url" || exit 28
printf '%s  %s\\n' "$sb_sha" "$tmp_dir/$sb_archive" | sha256sum -c --status || exit 28
tar -xOzf "$tmp_dir/$sb_archive" "sing-box-1.11.15-linux-${sb_arch}/sing-box" > "$tmp_dir/sing-box" || exit 28
chmod 0755 "$tmp_dir/sing-box"
"$tmp_dir/sing-box" version >/dev/null 2>&1 || exit 28
useradd --system --user-group --home-dir /opt/rakcrmp-black --shell /usr/sbin/nologin rakcrmp-black
mkdir /opt/rakcrmp-black
cd /opt/rakcrmp-black
git init -q
git remote add origin https://github.com/creepyside11/black.git
git fetch -q --depth 1 origin '''+PINNED_COMMIT+'''
git checkout -q --detach FETCH_HEAD
python3 -m venv /opt/rakcrmp-black/.venv || exit 30
/opt/rakcrmp-black/.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt >/dev/null 2>&1 || exit 30
/opt/rakcrmp-black/.venv/bin/python -m pip check >/dev/null 2>&1 || exit 30
install -d -m 0755 /opt/rakcrmp-black-runtime
install -m 0755 "$tmp_dir/sing-box" /opt/rakcrmp-black-runtime/sing-box
chown -R rakcrmp-black:rakcrmp-black /opt/rakcrmp-black
runuser -u rakcrmp-black -- env SING_BOX_BIN=/opt/rakcrmp-black-runtime/sing-box /opt/rakcrmp-black/.venv/bin/python -c 'import telebot, requests; from logic.vpn_bridge import bridge_available; assert bridge_available()' || exit 30
install -d -m 0700 /etc/rakcrmp-black
printf '%s\\n' '''+shlex.quote('TG_BOT_TOKEN='+token)+''' '''+shlex.quote('TG_ADMIN_ID='+admin)+''' 'SING_BOX_BIN=/opt/rakcrmp-black-runtime/sing-box' > /etc/rakcrmp-black/bot.env
chmod 0600 /etc/rakcrmp-black/bot.env
cat > /etc/systemd/system/rakcrmp-black.service <<'SERVICE'
[Unit]
Description=RakCRMP Black Telegram controller
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=rakcrmp-black
Group=rakcrmp-black
WorkingDirectory=/opt/rakcrmp-black
EnvironmentFile=/etc/rakcrmp-black/bot.env
ExecStart=/opt/rakcrmp-black/.venv/bin/python -u /opt/rakcrmp-black/bot.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
[Install]
WantedBy=multi-user.target
SERVICE
systemctl daemon-reload
systemctl enable --now rakcrmp-black.service >/dev/null 2>&1
sleep 3
systemctl is-active --quiet rakcrmp-black.service || exit 26
'''


async def start(request, authorize):
    uid=await authorize(request);data=await get_json(request);collect_old()
    try: user,password,token,admin=validate(data)
    except ValueError as exc: raise web.HTTPBadRequest(text=str(exc))
    probe_id=data.get('probe_id')
    if not isinstance(probe_id,str): raise web.HTTPBadRequest(text='Сначала проверьте SSH-ключ.')
    probe=PROBES.get(probe_id)
    if not probe or probe['uid']!=uid or probe['expires']<time.time(): raise web.HTTPBadRequest(text='Проверка ключа устарела. Начните заново.')
    if uid in ACTIVE: raise web.HTTPConflict(text='Установка уже выполняется.')
    if len(ACTIVE)>=3: raise web.HTTPServiceUnavailable(text='Все установщики заняты. Попробуйте позже.')
    PROBES.pop(probe_id,None);ACTIVE.add(uid)
    jid=secrets.token_urlsafe(24)
    JOBS[jid]={'uid':uid,'state':'running','message':'Подключаемся к VPS и проверяем систему.','finished':None}
    async def run():
        try:
            async with SLOTS:
                async with asyncssh.connect(probe['host'],port=22,username=user,password=password,known_hosts=([probe['key']],[],[]),client_keys=[],agent_path=None,config=None,login_timeout=20) as conn:
                    # Login without sudo requires either root or configured passwordless sudo.
                    check=await conn.run('id -u',check=True,timeout=10)
                    is_root=check.stdout.strip()=='0'
                    if not is_root:
                        sudo=await conn.run('sudo -n true',timeout=10)
                        if sudo.exit_status!=0:
                            JOBS[jid].update(state='failed',message='Нужен вход root или настроенный sudo без пароля.')
                            return
                    command='/bin/bash -s' if is_root else 'sudo -n /bin/bash -s'
                    JOBS[jid]['message']='Устанавливаем программу и настраиваем автозапуск. Это может занять несколько минут.'
                    result=await conn.run(command,input=installation_script(token,admin),timeout=600)
                    messages={21:'Нет прав администратора. Нужен root или sudo без пароля.',22:'На сервере не обнаружена Ubuntu 26.04.',23:'На сервере уже существует установка или служба rakcrmp-black. Ничего не перезаписано.',24:'На сервере не найден systemd.',25:'Нужен Python 3.10 или новее.',26:'Служба создана, но не запустилась. Проверьте журнал на VPS.',27:'Не удалось установить системные пакеты. Проверьте интернет и пакетный менеджер VPS.',28:'Не удалось скачать или проверить sing-box. Установка остановлена.',29:'Архитектура VPS не поддерживается установщиком sing-box.',30:'Не удалось установить или проверить Python-зависимости и мост sing-box. На VPS могли остаться файлы установки.'}
                    if result.exit_status!=0:
                        message=messages.get(result.exit_status,'Не удалось завершить установку. На VPS могли остаться созданные файлы; проверьте их перед повтором.')
                        JOBS[jid].update(state='failed',message=message)
                    else:
                        JOBS[jid].update(state='done',message='Служба установлена и запущена. Откройте вашего Telegram-бота и отправьте /start.')
        except asyncssh.HostKeyNotVerifiable:
            JOBS[jid].update(state='failed',message='SSH-ключ изменился. Соединение остановлено; проверьте ключ сервера.')
        except asyncssh.PermissionDenied:
            JOBS[jid].update(state='failed',message='SSH отклонил логин или пароль.')
        except (asyncio.TimeoutError, OSError, asyncssh.Error):
            JOBS[jid].update(state='failed',message='Ошибка подключения или тайм-аут. Проверьте VPS и попробуйте снова.')
        except Exception:
            # Never stringify exceptions: they can contain user-supplied secrets.
            JOBS[jid].update(state='failed',message='Ошибка установки. Проверьте настройки VPS.')
        finally:
            ACTIVE.discard(uid);JOBS[jid]['finished']=time.time()
    request.app['application'].create_task(run())
    return web.json_response({'job_id':jid},headers={'Cache-Control':'no-store'})


async def status(request, authorize):
    uid=await authorize(request);collect_old();jid=request.query.get('job','')
    job=JOBS.get(jid)
    if not job or job['uid']!=uid: raise web.HTTPNotFound(text='Установка не найдена или уже устарела.')
    return web.json_response({'state':job['state'],'message':job['message']},headers={'Cache-Control':'no-store'})
