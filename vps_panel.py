"""Optional VPS panel setup, independent of the existing bot installer."""
import asyncio
import base64
import ipaddress
import re
import secrets
import time
from pathlib import Path

import asyncssh
from aiohttp import web
import vps_install as vi

GUARD = Path(__file__).with_name('panel_guard.py')


def panel_script(host):
    ip = ipaddress.ip_address(host)
    if ip.version != 4 or not ip.is_global:
        raise ValueError('Для автоматического HTTPS нужен публичный IPv4.')
    domain = str(ip).replace('.', '-') + '.sslip.io'
    encoded = base64.b64encode(GUARD.read_bytes()).decode('ascii')
    return r'''set -eu
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=l
umask 077
[ "$(id -u)" -eq 0 ] || exit 31
[ -f /opt/rakcrmp-black/web/server.py ] && [ -f /etc/rakcrmp-black/bot.env ] || exit 32
systemctl is-active --quiet rakcrmp-black.service || exit 33
for path in /etc/caddy /etc/systemd/system/rakcrmp-panel.service /opt/rakcrmp-black-panel; do
  [ ! -e "$path" ] || exit 34
done
if command -v caddy >/dev/null 2>&1 || systemctl cat caddy.service >/dev/null 2>&1; then exit 34; fi
admin=$(sed -n 's/^TG_ADMIN_ID=//p' /etc/rakcrmp-black/bot.env)
case "$admin" in ''|*[!0-9]*) exit 35;; esac
[ "$admin" -gt 0 ] || exit 35
python3 - <<'CHECK_PORTS' || exit 36
import socket
for port in (80, 443, 18743):
    with socket.socket() as sock:
        sock.bind(('0.0.0.0' if port != 18743 else '127.0.0.1', port))
CHECK_PORTS
apt-get update -qq >/dev/null 2>&1 || exit 37
apt-get install -y -qq caddy ca-certificates curl >/dev/null 2>&1 || exit 37
install -d -m 0755 /opt/rakcrmp-black-panel
printf %s 'PANEL_GUARD_BASE64' | base64 -d > /opt/rakcrmp-black-panel/runner.py
chmod 0644 /opt/rakcrmp-black-panel/runner.py
cat > /etc/systemd/system/rakcrmp-panel.service <<EOF
[Unit]
Description=RakCRMP Telegram panel
After=network-online.target rakcrmp-black.service
Wants=network-online.target
[Service]
Type=simple
User=rakcrmp-black
Group=rakcrmp-black
WorkingDirectory=/opt/rakcrmp-black
EnvironmentFile=/etc/rakcrmp-black/bot.env
Environment=TG_ADMIN_IDS=$admin
Environment=BR_WEB_HOST=127.0.0.1
Environment=BR_WEB_PORT=18743
Environment=PYTHONPATH=/opt/rakcrmp-black
ExecStart=/opt/rakcrmp-black/.venv/bin/python -u /opt/rakcrmp-black-panel/runner.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/caddy/Caddyfile <<'CADDY'
{
    admin off
}
PANEL_DOMAIN {
    @disabled path /api/key /api/login
    respond @disabled 403
    reverse_proxy 127.0.0.1:18743
}
CADDY
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 || exit 38
systemctl daemon-reload
systemctl enable --now rakcrmp-panel.service >/dev/null 2>&1 || exit 39
systemctl restart caddy >/dev/null 2>&1 || exit 40
systemctl enable caddy >/dev/null 2>&1 || exit 40
sleep 2
systemctl is-active --quiet rakcrmp-panel.service || exit 39
# A valid public certificate is required before the Telegram button is changed.
ok=0
for i in $(seq 1 45); do
  if curl --silent --show-error --fail --max-time 3 --resolve PANEL_DOMAIN:443:127.0.0.1 https://PANEL_DOMAIN/api/session >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
[ "$ok" = 1 ] || exit 41
python3 - >/dev/null 2>&1 <<'TELEGRAM' || exit 42
import json, urllib.request
values = {}
with open('/etc/rakcrmp-black/bot.env', encoding='utf-8') as f:
    for line in f:
        key, sep, value = line.strip().partition('=')
        if sep: values[key] = value
admin = int(values['TG_ADMIN_ID'])
token = values['TG_BOT_TOKEN']
body = json.dumps({'chat_id':admin,'menu_button':{'type':'web_app','text':'Панель','web_app':{'url':'https://PANEL_DOMAIN/'}}}).encode()
request = urllib.request.Request('https://api.telegram.org/bot'+token+'/setChatMenuButton', data=body, headers={'Content-Type':'application/json'}, method='POST')
with urllib.request.urlopen(request, timeout=20) as response:
    if not json.load(response).get('ok'): raise RuntimeError('Telegram rejected menu')
TELEGRAM
'''.replace('PANEL_GUARD_BASE64',encoded).replace('PANEL_DOMAIN',domain)


async def start(request, authorize):
    uid = await authorize(request)
    data = await vi.get_json(request)
    username, password = data.get('username'), data.get('password')
    if not isinstance(username,str) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}',username):
        raise web.HTTPBadRequest(text='Укажите корректный логин SSH.')
    if not isinstance(password,str) or not 1 <= len(password) <= 512 or data.get('confirmed') is not True:
        raise web.HTTPBadRequest(text='Укажите пароль и подтвердите SSH-ключ.')
    probe_id = data.get('probe_id')
    probe = vi.PROBES.get(probe_id) if isinstance(probe_id,str) else None
    if not probe or probe['uid'] != uid or probe['expires'] < time.time():
        raise web.HTTPBadRequest(text='Проверка ключа устарела. Начните заново.')
    try: script = panel_script(probe['host'])
    except ValueError as e: raise web.HTTPBadRequest(text=str(e))
    if uid in vi.ACTIVE: raise web.HTTPConflict(text='Операция уже выполняется.')
    if len(vi.ACTIVE) >= 3: raise web.HTTPServiceUnavailable(text='Все установщики заняты. Попробуйте позже.')
    vi.PROBES.pop(probe_id,None); vi.ACTIVE.add(uid)
    jid = secrets.token_urlsafe(24)
    vi.JOBS[jid] = {'uid':uid,'state':'running','message':'Подключаемся к VPS и проверяем панель.','finished':None}
    async def run():
        try:
            async with vi.SLOTS:
                async with asyncssh.connect(probe['host'],port=22,username=username,password=password,known_hosts=([probe['key']],[],[]),client_keys=[],agent_path=None,config=None,login_timeout=20) as conn:
                    check=await conn.run('id -u',check=True,timeout=10)
                    root=check.stdout.strip()=='0'
                    if not root and (await conn.run('sudo -n true',timeout=10)).exit_status != 0:
                        vi.JOBS[jid].update(state='failed',message='Нужен root или sudo без пароля.');return
                    vi.JOBS[jid]['message']='Настраиваем защищённую панель и HTTPS. Это может занять несколько минут.'
                    result=await conn.run('/bin/bash -s' if root else 'sudo -n /bin/bash -s',input=script,timeout=600)
                    messages={31:'Нужны права администратора VPS.',32:'Установка RakCRMP на VPS не найдена.',33:'Сначала запустите службу установленного бота.',34:'Найден существующий веб-сервер или панель. Для защиты текущих сервисов ничего не перезаписано.',35:'Не удалось определить администратора установленного бота.',36:'Порт 80, 443 или 18743 уже занят. Существующие сервисы не изменены.',37:'Не удалось установить Caddy или зависимости.',38:'Не удалось проверить конфигурацию HTTPS.',39:'Панель создана, но не запустилась.',40:'Не удалось запустить HTTPS-сервер.',41:'Не удалось получить HTTPS-сертификат. Проверьте публичный IPv4 и доступность портов 80 и 443.',42:'Панель работает, но Telegram не принял кнопку. Проверьте доступ VPS к Telegram.'}
                    if result.exit_status:
                        vi.JOBS[jid].update(state='failed',message=messages.get(result.exit_status,'Не удалось настроить панель.')+' При повторной попытке могут потребоваться проверка и очистка созданных файлов панели; служба бота не изменялась.')
                    else:
                        vi.JOBS[jid].update(state='done',message='Панель запущена. Откройте своего бота: у поля сообщения появилась кнопка «Панель».')
        except asyncssh.HostKeyNotVerifiable:
            vi.JOBS[jid].update(state='failed',message='SSH-ключ изменился. Подключение остановлено.')
        except asyncssh.PermissionDenied:
            vi.JOBS[jid].update(state='failed',message='SSH отклонил логин или пароль.')
        except (asyncio.TimeoutError,OSError,asyncssh.Error):
            vi.JOBS[jid].update(state='failed',message='Ошибка подключения или тайм-аут. Проверьте VPS.')
        except Exception:
            vi.JOBS[jid].update(state='failed',message='Не удалось настроить панель. Проверьте VPS.')
        finally:
            vi.ACTIVE.discard(uid);vi.JOBS[jid]['finished']=time.time()
    request.app['application'].create_task(run())
    return web.json_response({'job_id':jid},headers={'Cache-Control':'no-store'})
