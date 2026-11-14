# Установка RakCRMP / Black с Lua в терминале

Инструкция относится к **игровой сборке** `black_lua_guide_full.zip`, а не к репозиторию Telegram-магазина `proverka-rakcrmp`. Ветка `creepyside11/black`, которую сейчас берёт VPS-установщик, не содержит все возможности этой сборки. Скачать сборку: [black_lua_guide_full.zip](https://chatgpt.com/library?file_id=libfile_781148ebd1dc8191a42c9d1e339d30eb&account_id=personal). Распакуйте архив: внутри папка `black-lua-ai`. Не заменяйте ею работающую установку автоматически — сначала сохраните её данные и проверьте совместимость.

Инструкции ниже показывают ручную установку в терминале. Реальный запуск на вашей ОС, у провайдера ИИ и на игровом сервере не проверен. Для Windows и macOS описаны команды установки, но они не проверялись на этих системах.

## Что потребуется

| Компонент | Зачем | Обязателен? |
|---|---|---|
| Python 3.10+ и pip | Запуск проекта и установка Python-библиотек | Да |
| Архив проекта | Сам код программы | Да |
| `pyTelegramBotAPI` — модуль `telebot` | Telegram-управление | Ставится из `requirements.txt` |
| OpenAI SDK и `python-dotenv` | Автокапча через выбранную vision-модель | Ставятся из `requirements.txt`; ключ нужен только для ИИ |
| Lua 5.4 | Запуск Lua-ботов и скриптов | Для Lua-функций |
| sing-box | Мост от поддерживаемых VPN-узлов к SOCKS5 | Для этого моста; не обязателен для прямого подключения |
| Токен Telegram-бота и ваш числовой Telegram ID | Управление через Telegram | Только если нужно Telegram-управление |

`pip install -r requirements.txt` устанавливает перечисленные там Python-библиотеки, включая `pyTelegramBotAPI`, OpenAI SDK и `python-dotenv`. Он **не устанавливает** системные программы Lua и sing-box. В конкретной среде установка зависимостей может завершиться ошибкой — проверяйте вывод, особенно в Termux, где не для всех нативных зависимостей доступны готовые пакеты.

## 1. Android / Termux

1. Установите Termux из [официальных источников проекта](https://termux.dev/en/docs/), откройте его и обновите пакеты:

```sh
pkg update && pkg upgrade
pkg install python lua54 unzip
```

2. Поместите ZIP в папку «Загрузки» Android. Разрешите доступ к общему хранилищу, распакуйте проект в домашний каталог Termux и перейдите в папку:

```sh
termux-setup-storage
unzip ~/storage/downloads/black_lua_guide_full.zip -d ~/rakcrmp
cd ~/rakcrmp/black-lua-ai
```

Если архив называется иначе, замените имя в команде `unzip`. Для повторной установки используйте новую папку: не перезаписывайте рабочие данные.

3. Создайте окружение и установите библиотеки:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -c "import telebot, openai, dotenv; print('Python-зависимости: OK')"
lua54 -v
```

4. Если нужны VPN-узлы через sing-box:

```sh
pkg install sing-box
sing-box version
```

Если версия Python или Termux не позволяет установить OpenAI SDK, сохраните полный текст ошибки. Без SDK ИИ-капча работать не будет; не считайте установку успешной только потому, что скачались другие пакеты. При закрытии Termux или ограничении фоновой работы Android программа может остановиться.

## 2. Linux: Ubuntu / Debian

Распакуйте ZIP через файловый менеджер или `unzip`. Пример предполагает файл в `~/Downloads`:

```sh
sudo apt update
sudo apt install python3 python3-venv python3-pip lua5.4 unzip
mkdir -p ~/rakcrmp
unzip ~/Downloads/black_lua_guide_full.zip -d ~/rakcrmp
cd ~/rakcrmp/black-lua-ai
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -c "import telebot, openai, dotenv; print('Python-зависимости: OK')"
lua5.4 -v
```

Для sing-box используйте [официальные инструкции разработчика](https://sing-box.sagernet.org/installation/package-manager/) для своего дистрибутива. После установки проверьте `sing-box version`. В сборке есть `deploy/install_singbox.sh` для Linux/Termux; ознакомьтесь с ним перед запуском, поскольку он устанавливает системный файл и его версии могут устареть.

## 3. macOS

Установите [Homebrew](https://brew.sh/) по официальной инструкции. В Terminal выполните:

```sh
brew install python@3.12 lua@5.4
mkdir -p ~/rakcrmp
unzip ~/Downloads/black_lua_guide_full.zip -d ~/rakcrmp
cd ~/rakcrmp/black-lua-ai
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
export BR_LUA_BIN="$(brew --prefix lua@5.4)/bin/lua"
"$BR_LUA_BIN" -v
python -c "import telebot, openai, dotenv; print('Python-зависимости: OK')"
```

`lua@5.4` в Homebrew может не добавляться в общий PATH, поэтому путь задан через `BR_LUA_BIN` для текущего окна терминала. Для моста отдельно:

```sh
brew install sing-box
sing-box version
```

Не запускайте `deploy/install_singbox.sh` на macOS: он скачивает Linux-бинарник. Работа проекта на Intel и Apple Silicon отдельно не проверялась.

## 4. Windows: PowerShell

1. Установите [Python для Windows](https://www.python.org/downloads/windows/). Откройте PowerShell и проверьте `py -3 --version`. Распакуйте архив, например в `%USERPROFILE%\\rakcrmp`.
2. В PowerShell:

```powershell
cd "$env:USERPROFILE\rakcrmp\black-lua-ai"
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -c "import telebot, openai, dotenv; print('Python dependencies: OK')"
```

Активация окружения не нужна: команды вызывают его Python напрямую. Для Lua установите Lua **5.4** для Windows; [Lua](https://www.lua.org/manual/5.4/readme.html) указывает на готовые сборки LuaBinaries. Укажите полный путь к исполняемому файлу, например:

```powershell
$env:BR_LUA_BIN = 'C:\путь\к\lua54.exe'
& $env:BR_LUA_BIN -v
```

Это пример — замените путь на фактический. Для sing-box можно использовать менеджер из [официальной инструкции sing-box](https://sing-box.sagernet.org/installation/package-manager/), например `winget install sing-box`; после установки откройте новое окно PowerShell и проверьте `sing-box version`. При необходимости задайте `$env:SING_BOX_BIN = 'C:\путь\к\sing-box.exe'`. Команда `deploy/install_singbox.sh` предназначена для Linux/Termux, не для Windows.

## 5. Настройка ИИ-капчи

В папке проекта создайте `.env` из `.env.example`, только если `.env` ещё нет. На Linux/macOS/Termux:

```sh
[ -f .env ] || cp .env.example .env
nano .env
```

В PowerShell:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

Заполните `CAPTCHA_BASE_URL`, `CAPTCHA_API_TOKEN`, `CAPTCHA_MODEL`. Нужна модель, которая принимает изображения через совместимый `chat/completions`. SDK отправляет изображение выбранному провайдеру через его API; услуга может быть платной. Не публикуйте `.env`, токены и пароли.

## 6. Запуск программы

На Linux, macOS и Termux, когда окружение активировано:

```sh
python bot.py
```

На Windows:

```powershell
.\.venv\Scripts\python.exe bot.py
```

При обычном запуске откроется консольное меню. Для Telegram-управления сначала задайте `TG_BOT_TOKEN` и `TG_ADMIN_ID` как переменные окружения и запустите программу; переменные должны быть заданы в том же терминале. Это **не** те же параметры, что API-токен ИИ. В Unix-подобных оболочках используйте `export`, в PowerShell — `$env:TG_BOT_TOKEN` и `$env:TG_ADMIN_ID`. Не передавайте значения посторонним. Команда с токеном может сохраниться в истории терминала.

Для остановки программы нажмите Ctrl+C. Для повторного запуска сначала перейдите в папку проекта; на Unix снова активируйте окружение или вызывайте `.venv/bin/python bot.py`.

## 7. Lua-скрипт

Положите доверенный `.lua` в `scripts/lua/` или отправьте его на совместимый VPS через Mini App. В консоли используйте `/lua help` и `LUA_GUIDE_RU.md`: загрузчик поддерживает разные режимы и сначала позволяет проверить сценарий без действий. Lua-файл не запускается автоматически после копирования.

## 8. Частые проблемы

- **`No module named telebot`** — установите `requirements.txt` тем же Python, которым запускаете `bot.py`; на Windows используйте путь `.venv\\Scripts\\python.exe`.
- **Lua не найден** — установите Lua 5.4 и задайте `BR_LUA_BIN` полным путём к исполняемому файлу; `lua54 -v` / `lua5.4 -v` показывает версию.
- **sing-box не найден** — установите бинарник под свою ОС и архитектуру или задайте `SING_BOX_BIN`; проверьте `sing-box version`.
- **ИИ не распознаёт** — проверьте интернет, Base URL, токен и точный ID модели с поддержкой изображений. Просмотрите `[captcha-ai]` в журнале; не отправляйте токен в поддержку.
- **Файл куплен, но нельзя поставить на VPS** — текущая автоустановка поддерживает текстовые `.lua` до 256 КиБ и требует Lua-загрузчик на сервере; архивы, `.luac` и другие форматы устанавливаются по инструкции товара.
- **Платёж оплачен, файл в чат не пришёл** — откройте бота, разрешите ему писать и проверьте «Мои покупки»; при необходимости обратитесь в поддержку с ID заказа без платёжных секретов.

Источники: [Python venv](https://docs.python.org/3/library/venv.html), [sing-box: установка](https://sing-box.sagernet.org/installation/package-manager/), [Termux](https://termux.dev/en/docs/), [Lua 5.4](https://www.lua.org/manual/5.4/readme.html).
