"""Fixed-purpose management of an already installed RakCRMP VPS."""
import asyncio
import re
import secrets
import time

import asyncssh
from aiohttp import web
import vps_install as vi

ACTIONS = {
    'stop_bot': ('rakcrmp-black.service', 'disable --now', 'Программа остановлена. Её можно снова запустить в управлении VPS.'),
    'start_bot': ('rakcrmp-black.service', 'enable --now', 'Программа запущена.'),
    'stop_panel': ('rakcrmp-panel.service', 'disable --now', 'Mini App остановлено. Кнопка в боте останется, но панель будет недоступна до повторного запуска.'),
    'start_panel': ('rakcrmp-panel.service', 'enable --now', 'Mini App запущено. Откройте кнопку «Панель» в установленном боте.'),
}


def command_script(action):
    if action == 'update_bot':
        return UPDATE_SCRIPT
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


# Only tracked source files are updated; user data is never force-reset.
# A changed accounts.json is explicitly permitted and Git will refuse checkout
# if a newer revision would overwrite it. Untracked/ignored files get the same
# protection using --no-overwrite-ignore.
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
asowner git diff --quiet "$old" "$new" -- accounts.json || exit 40
asowner git diff --quiet HEAD -- . ':(exclude)accounts.json' || exit 33
[ -d /opt/rakcrmp-black-runtime ] || exit 22
backup=$(mktemp -d /opt/rakcrmp-black-runtime/update-backup.XXXXXXXX) || exit 34
if ! cp -a "$root/.venv" "$backup/venv"; then rm -rf -- "$backup"; exit 34; fi
was_active=0
if systemctl is-active --quiet rakcrmp-black.service; then
    was_active=1
    if ! systemctl stop rakcrmp-black.service >/dev/null 2>&1; then rm -rf -- "$backup"; exit 35; fi
fi
restore() {
    code=$1
    # Do not discard any runtime data or force checkout over local changes.
    if ! asowner git checkout -q --detach --no-overwrite-ignore "$old"; then exit 39; fi
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
asowner git checkout -q --detach --no-overwrite-ignore "$new" || restore 36
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
    if action not in (*ACTIONS, 'update_bot'):
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
    script=command_script(action)
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
                    result=await conn.run('/bin/bash -s' if root else 'sudo -n /bin/bash -s',input=script,timeout=900)
                    messages={21:'Нужны права администратора VPS.',22:'Установка программы на VPS не найдена.',23:'Служба не найдена. Сначала установите программу или запустите Mini App.',24:'На этом VPS уже выполняется другая операция.',25:'Служба не выполнила команду.',26:'Служба не запустилась или сразу остановилась.',30:'Не удалось определить установленную версию.',31:'Не удалось получить обновление с GitHub.',32:'Новая версия не является продолжением установленной. Автоматическое обновление остановлено.',33:'В файлах программы обнаружены локальные изменения. Обновление остановлено, чтобы не потерять данные.',34:'Не удалось создать резервную копию окружения. Программа не изменена.',35:'Не удалось остановить программу для обновления.',36:'Обновление конфликтует с локальными файлами. Предыдущая версия восстановлена.',37:'Не удалось установить зависимости. Предыдущая версия восстановлена.',38:'Обновлённая программа не запустилась. Предыдущая версия восстановлена.',39:'Не удалось автоматически восстановить предыдущую версию; проверьте VPS и резервную копию.',40:'В новой версии изменён файл аккаунтов. Обновление остановлено, чтобы сохранить ваши данные.'}
                    if action=='update_bot' and result.exit_status==10:
                        vi.JOBS[jid].update(state='done',message='Программа уже обновлена до последней версии.')
                    elif result.exit_status!=0:
                        vi.JOBS[jid].update(state='failed',message=messages.get(result.exit_status,'Не удалось выполнить действие на VPS.'))
                    else:
                        text='Программа обновлена; данные сохранены. Если она была остановлена, она осталась остановленной.' if action=='update_bot' else ACTIONS[action][2]
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
