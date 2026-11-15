"""Fixed-purpose management of an already installed RakCRMP VPS."""
import asyncio
import re
import secrets
import time

import asyncssh
from aiohttp import web
import vps_install as vi
import vps_singbox

ACTIONS = {
    'stop_bot': ('rakcrmp-black.service', 'disable --now', 'Программа остановлена. Её можно снова запустить в управлении VPS.'),
    'start_bot': ('rakcrmp-black.service', 'enable --now', 'Программа запущена.'),
    'stop_panel': ('rakcrmp-panel.service', 'disable --now', 'Mini App остановлено. Кнопка в боте останется, но панель будет недоступна до повторного запуска.'),
    'start_panel': ('rakcrmp-panel.service', 'enable --now', 'Mini App запущено. Откройте кнопку «Панель» в установленном боте.'),
}


def command_script(action, replace_accounts=False):
    if action == 'update_bot':
        return UPDATE_SCRIPT.replace('allow_replace=0 # confirmation marker', 'allow_replace=1 # confirmation marker' if replace_accounts else 'allow_replace=0 # confirmation marker')
    if action == 'setup_bridge':
        return vps_singbox.BRIDGE_SCRIPT
    unit, operation, _ = ACTIONS[action]
    verify = f'sleep 2\nsystemctl is-active --quiet {unit} || exit 26' if action.startswith('start_') else ''
    return f'''set -eu
[ "$(id -u)" -eq 0 ] || exit 21
[ -d /etc/rakcrmp-black ] || exit 22
[ -f /etc/systemd/system/{unit} ] || exit 23
exec 9>/etc/rakcrmp-black/manage.lock
flock -n 9 || exit 24
systemctl {operation} {unit} >/dev/null 2>&1 || exit 25
{verify}
'''


# Other local files are never force-reset. An account file is replaced only
# after explicit confirmation and a private, retained backup has been created.
UPDATE_SCRIPT = r'''set -eu
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=l
umask 077
[ "$(id -u)" -eq 0 ] || exit 21
root=/opt/rakcrmp-black
[ -d /etc/rakcrmp-black ] && [ -d "$root/.git" ] && [ -d "$root/.venv" ] && [ ! -L "$root/.venv" ] && [ -f /etc/systemd/system/rakcrmp-black.service ] || exit 22
exec 9>/etc/rakcrmp-black/manage.lock
flock -n 9 || exit 24
asowner() { runuser -u rakcrmp-black -- "$@"; }
cd "$root"
old=$(asowner git rev-parse --verify HEAD) || exit 30
asowner git fetch -q --depth=100 https://github.com/creepyside11/black.git refs/heads/main || exit 31
new=$(asowner git rev-parse --verify FETCH_HEAD) || exit 31
[ "$new" != "$old" ] || exit 10
asowner git merge-base --is-ancestor "$old" "$new" || exit 32
allow_replace=0 # confirmation marker
accounts_changed=0
if asowner git diff --quiet "$old" "$new" -- accounts.json; then :; else
    diff_code=$?
    [ "$diff_code" -eq 1 ] || exit 30
    accounts_changed=1
fi
if [ "$accounts_changed" -eq 1 ] && [ "$allow_replace" -ne 1 ]; then exit 40; fi
asowner git diff --quiet HEAD -- . ':(exclude)accounts.json' || exit 33
[ -d /opt/rakcrmp-black-runtime ] || exit 22
backup=$(mktemp -d /opt/rakcrmp-black-runtime/update-backup.XXXXXXXX) || exit 34
if ! cp -a "$root/.venv" "$backup/venv"; then rm -rf -- "$backup"; exit 34; fi
was_active=0
if systemctl is-active --quiet rakcrmp-black.service; then
    was_active=1
    if ! systemctl stop rakcrmp-black.service >/dev/null 2>&1; then rm -rf -- "$backup"; exit 35; fi
fi
account_backup=''
account_existed=0
account_touched=0
restore() {
    code=$1
    # Preserve any account changes made by a briefly restarted new version,
    # then reset only that path so checkout of the old revision can proceed.
    if [ "$account_touched" = 1 ]; then
        if [ -L "$root/accounts.json" ] || { [ -e "$root/accounts.json" ] && [ ! -f "$root/accounts.json" ]; }; then exit 39; fi
        if [ -f "$root/accounts.json" ]; then
            cp -a -- "$root/accounts.json" "$account_backup/after-failure.json" || exit 39
            chmod 0600 "$account_backup/after-failure.json" || exit 39
        fi
        current=$(asowner git rev-parse --verify HEAD) || exit 39
        if asowner git cat-file -e "$current:accounts.json" 2>/dev/null; then
            asowner git checkout -q --force "$current" -- accounts.json || exit 39
        else
            asowner rm -f -- "$root/accounts.json" || exit 39
        fi
    fi
    if ! asowner git checkout -q --detach --no-overwrite-ignore "$old"; then exit 39; fi
    if [ "$account_touched" = 1 ]; then
        if [ "$account_existed" = 1 ]; then
            cp -a -- "$account_backup/accounts.json" "$root/accounts.json" || exit 39
        else
            asowner rm -f -- "$root/accounts.json" || exit 39
        fi
    fi
    if [ -d "$backup/venv" ]; then
        if [ -d "$root/.venv" ]; then mv "$root/.venv" "$backup/venv-failed" || exit 39; fi
        mv "$backup/venv" "$root/.venv" || exit 39
    fi
    if [ "$was_active" = 1 ]; then systemctl start rakcrmp-black.service >/dev/null 2>&1 || exit 39; fi
    rm -rf -- "$backup"
    exit "$code"
}
trap 'restore 38' HUP INT TERM
asowner git diff --quiet HEAD -- . ':(exclude)accounts.json' || restore 33
if [ "$accounts_changed" = 1 ]; then
    # Never follow an account-file symlink or overwrite a directory.
    if [ -L "$root/accounts.json" ] || { [ -e "$root/accounts.json" ] && [ ! -f "$root/accounts.json" ]; }; then restore 41; fi
    if [ -L /etc/rakcrmp-black/account-backups ] || { [ -e /etc/rakcrmp-black/account-backups ] && [ ! -d /etc/rakcrmp-black/account-backups ]; }; then restore 34; fi
    install -d -m 0700 /etc/rakcrmp-black/account-backups || restore 34
    account_backup=$(mktemp -d /etc/rakcrmp-black/account-backups/update.XXXXXXXX) || restore 34
    chmod 0700 "$account_backup" || restore 34
    if [ -f "$root/accounts.json" ]; then
        account_existed=1
        cp -a -- "$root/accounts.json" "$account_backup/accounts.json" || restore 34
        chmod 0600 "$account_backup/accounts.json" || restore 34
    fi
    # The service is stopped, so the backup corresponds to its last saved state.
    account_touched=1
    if asowner git cat-file -e "$old:accounts.json" 2>/dev/null; then
        asowner git checkout -q --force "$old" -- accounts.json || restore 36
    else
        asowner rm -f -- "$root/accounts.json" || restore 36
    fi
fi
asowner git checkout -q --detach --no-overwrite-ignore "$new" || restore 36
if ! command -v lua5.4 >/dev/null 2>&1; then
    apt-get update -qq >/dev/null 2>&1 || restore 42
    apt-get install -y -qq lua5.4 >/dev/null 2>&1 || restore 42
fi
command -v lua5.4 >/dev/null 2>&1 || restore 42
asowner "$root/.venv/bin/python" -m pip install --disable-pip-version-check -q -r "$root/requirements.txt" >/dev/null 2>&1 || restore 37
asowner "$root/.venv/bin/python" -m pip check >/dev/null 2>&1 || restore 37
if [ "$was_active" = 1 ]; then
    if ! systemctl start rakcrmp-black.service >/dev/null 2>&1; then systemctl stop rakcrmp-black.service >/dev/null 2>&1 || true; restore 38; fi
    sleep 3
    if ! systemctl is-active --quiet rakcrmp-black.service; then systemctl stop rakcrmp-black.service >/dev/null 2>&1 || true; restore 38; fi
fi
rm -rf -- "$backup"
'''


async def start(request, authorize):
    uid=await authorize(request);data=await vi.get_json(request);vi.collect_old()
    action=data.get('action');user=data.get('username');password=data.get('password')
    if action not in (*ACTIONS, 'update_bot', 'setup_bridge'):
        raise web.HTTPBadRequest(text='Выберите доступное действие.')
    if not isinstance(user,str) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}',user):
        raise web.HTTPBadRequest(text='Укажите корректный логин SSH.')
    if not isinstance(password,str) or not 1<=len(password)<=512 or data.get('confirmed') is not True:
        raise web.HTTPBadRequest(text='Укажите пароль и подтвердите SSH-ключ.')
    probe_id=data.get('probe_id')
    probe=vi.PROBES.get(probe_id) if isinstance(probe_id,str) else None
    if not probe or probe['uid']!=uid or probe['expires']<time.time():
        raise web.HTTPBadRequest(text='Проверка ключа устарела. Начните заново.')
    if uid in vi.ACTIVE: raise web.HTTPConflict(text='Другая операция уже выполняется.')
    if len(vi.ACTIVE)>=3: raise web.HTTPServiceUnavailable(text='Все подключения заняты. Попробуйте позже.')
    replace_accounts=data.get('replace_accounts',False)
    if not isinstance(replace_accounts,bool) or (replace_accounts and action!='update_bot'):
        raise web.HTTPBadRequest(text='Некорректное подтверждение замены файла.')
    script=command_script(action,replace_accounts=replace_accounts)
    vi.PROBES.pop(probe_id,None);vi.ACTIVE.add(uid)
    jid=secrets.token_urlsafe(24)
    vi.JOBS[jid]={'uid':uid,'state':'running','message':'Подключаемся к VPS и выполняем действие.','finished':None}
    async def run():
        try:
            async with vi.SLOTS:
                async with asyncssh.connect(probe['host'],port=22,username=user,password=password,known_hosts=([probe['key']],[],[]),client_keys=[],agent_path=None,config=None,login_timeout=20) as conn:
                    check=await conn.run('id -u',check=True,timeout=10)
                    root=check.stdout.strip()=='0'
                    if not root and (await conn.run('sudo -n true',timeout=10)).exit_status!=0:
                        vi.JOBS[jid].update(state='failed',message='Нужен root или sudo без пароля.');return
                    if action=='update_bot': vi.JOBS[jid]['message']='Проверяем обновление и сохраняем резервную копию окружения.'
                    elif action=='setup_bridge': vi.JOBS[jid]['message']='Устанавливаем и проверяем sing-box и локальный SOCKS5-мост.'
                    result=await conn.run('/bin/bash -s' if root else 'sudo -n /bin/bash -s',input=script,timeout=900)
                    messages={21:'Нужны права администратора VPS.',22:'Установка программы на VPS не найдена.',23:'Служба не найдена. Сначала установите программу или запустите Mini App.',24:'На этом VPS уже выполняется другая операция.',25:'Служба не выполнила команду.',26:'Служба не запустилась или сразу остановилась.',30:'Не удалось определить установленную версию.',31:'Не удалось получить обновление с GitHub.',32:'Новая версия не является продолжением установленной. Автоматическое обновление остановлено.',33:'В файлах программы обнаружены локальные изменения. Обновление остановлено, чтобы не потерять данные.',34:'Не удалось создать резервную копию окружения. Программа не изменена.',35:'Не удалось остановить программу для обновления.',36:'Обновление конфликтует с локальными файлами. Предыдущая версия восстановлена.',37:'Не удалось установить зависимости. Предыдущая версия восстановлена.',38:'Обновлённая программа не запустилась. Предыдущая версия восстановлена.',39:'Не удалось автоматически восстановить предыдущую версию; проверьте VPS и резервную копию.',40:'Для замены файла аккаунтов требуется отдельное подтверждение. Никакие данные не изменены.',41:'Файл аккаунтов является ссылкой или не обычным файлом. Обновление отменено.',42:'Не удалось установить Lua 5.4. Предыдущая версия и аккаунты восстановлены.',43:'Путь sing-box в конфигурации отличается от установочного. Существующие настройки не изменены.',44:'Архитектура VPS не поддерживается.',45:'Не удалось скачать или проверить sing-box. Существующий бинарник не изменён.',46:'Тестовый SOCKS5-мост не запустился. Существующий бинарник и настройки не изменены.',47:'Не удалось установить новый sing-box.'}
                    if action=='update_bot' and result.exit_status==10:
                        vi.JOBS[jid].update(state='done',message='Программа уже обновлена до последней версии.')
                    elif action=='setup_bridge' and result.exit_status in (12,13,14):
                        notice={12:'sing-box установлен и проверен. Программа сейчас остановлена: запустите её, затем проверьте VPN-узлы.',13:'sing-box установлен и проверен. Ранее он отсутствовал: перезапустите программу, чтобы она увидела мост, затем проверьте VPN-узлы.',14:'sing-box установлен и проверен, но запустить проверку VPN-узлов через программу не удалось. Откройте её меню VPN и нажмите «Проверить сейчас».'}
                        vi.JOBS[jid].update(state='done',message=notice[result.exit_status])
                    elif result.exit_status!=0:
                        vi.JOBS[jid].update(state='failed',message=messages.get(result.exit_status,'Не удалось выполнить действие на VPS.'))
                    else:
                        text=('Программа обновлена. Если файл аккаунтов был заменён, резервная копия сохранена на VPS в /etc/rakcrmp-black/account-backups/. Если программа была остановлена, она осталась остановленной.' if action=='update_bot' else 'sing-box установлен и проверен; программе отправлен запрос на проверку сохранённых VPN-узлов. Добавьте подписку, если её ещё нет.' if action=='setup_bridge' else ACTIONS[action][2])
                        vi.JOBS[jid].update(state='done',message=text)
        except asyncssh.HostKeyNotVerifiable:
            vi.JOBS[jid].update(state='failed',message='SSH-ключ изменился. Подключение остановлено.')
        except asyncssh.PermissionDenied:
            vi.JOBS[jid].update(state='failed',message='SSH отклонил логин или пароль.')
        except (asyncio.TimeoutError,OSError,asyncssh.Error):
            vi.JOBS[jid].update(state='failed',message='Соединение прервано. Операция могла продолжиться на VPS; проверьте состояние перед повтором.')
        except Exception:
            vi.JOBS[jid].update(state='failed',message='Не удалось выполнить действие. Проверьте состояние VPS.')
        finally:
            vi.ACTIVE.discard(uid);vi.JOBS[jid]['finished']=time.time()
    request.app['application'].create_task(run())
    return web.json_response({'job_id':jid},headers={'Cache-Control':'no-store'})
