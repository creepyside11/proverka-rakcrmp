# Справочник ИИ: Lua API v2 RakCRMP

Источник истины — установленный код и руководство https://github.com/creepyside11/black/blob/main/docs/LUA_API_V2_RU.md. `br.api_version == 2` означает доступность расширений ниже; старая установка может их не иметь. Не выдумывай RPC, идентификаторы диалогов, механики и подтверждения сервера. Тест синтаксиса не доказывает работу в игре.

Сценарий обрабатывает функции `on_load(e)`, `on_spawn(e)`, `on_dialog(e)`, `on_chat(e)` и другие документированные события. Событие `on_dialog` содержит id, style, title, text, button1, button2; on_chat — message. Режим bot отвечает за своё меню спавна, addon работает вместе с задачей. Вызов br-действия помещает его в очередь; `sent` в `on_action_result` означает локальное выполнение вызова, но не ответ сервера. Действия из on_load сейчас не формируют on_action_result. Не ожидай фактического подтверждения от возвращаемого true.

## Новое
- `br.account()` — снимок nickname, server, status, level, target_level; пароль не включён, level может устареть.
- `br.last_dialog()` — последний полученный диалог, не гарантия что он ещё открыт. `br.checkpoint()` — последняя полученная метка `{x,y,z,radius}` или nil; оба очищаются при переподключении.
- `br.timer_start(id, seconds, event, data, interval)` — именованный таймер; id 1–64 байта, начальная задержка 0–3600 с, event начинается с on_, interval 0/отсутствует для одноразового или 0.05–3600 для повторного. При совпадении id таймер заменяется. При срабатывании поля e.timer_id, e.count с 1, e.data. `br.timer_cancel(id)` отменяет; до 256 таймеров вместе с br.after. Таймеры не переживают перезапуск.
- `br.strip_colors(text)` удаляет {FFFFFF}; `br.lower` поддерживает русские и латинские буквы; `br.contains(text, needle, ignore_case)` — буквальный поиск. `br.dialog_rows(text, style)` возвращает строки с index с нуля, text, raw; style 5 пропускает заголовок. `br.find_dialog_row(text, needle, style, ignore_case)` возвращает индекс и строку или nil. Встроенный обработчик может конкурировать за диалог.
- `br.distance(a,b)` в 3D, `br.distance2d(a,b)` в 2D, `br.in_radius(a,b,r)`; координаты таблицы с x,y,z. Расстояние вернёт nil, если точки недоступны.
- `br.hex_encode(bytes)` до 2048 байт; `br.hex_decode(hex)` до 4096 символов. `br.json_encode(value)` и `br.json_decode(text)` до 16 КиБ, ошибки nil и текст. Пустые массивы `br.array()`, JSON null `br.null`.
- `br.command('/mm')` — постановка команды чата. `br.request({kind='chat',text='/mm'}, 'tag')` — действие с корреляционной меткой; `e.results[i].tag` приходит в on_action_result. Изменяющие действия не выполняются в --dry.
- `br.storage_get(key,default)` и `br.storage_all()` читают снимок на начало события; `br.storage_set(key,value)`, `br.storage_delete(key)`, `br.storage_clear()` ставят запись. Данные раздельны по проекту, нику, серверу и пути Lua-файла; ключ до 64 байт, максимум 64 ключа и 32 КиБ, до 6 уровней вложенности, строка до 4000 символов. Результат записи в следующем событии; `--dry` не пишет, полигон хранит виртуально. Это не шифрование, не записывай пароли и токены. Данные не подтверждают состояние сервера после переподключения.

Для проверки `python -m logic.lua_sandbox api_v2_example.lua --events api_v2_example_events.json`; только офлайн-симуляция. Пример скрипта `scripts/lua/api_v2_example.lua`. Полная ссылка и ограничения — в документации v2.
