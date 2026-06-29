# Brief — PRI-196 Поддержка вложений (attachments) в sync_board для YouTrack и YouGile
url: https://ru.yougile.com/team/686c049c8af8/#PRI-196

## Task
- **Проблема:** `sync_board` (server-side ETL синка досок) полностью игнорирует вложения. ТЗ/спеки часто лежат в `.md`/`.docx`/`.pdf` вложениях → контекст задачи неполный для solve-task/review-pr.
- **Цель:** скачивать вложения с YouTrack и YouGile, парсить текст (`.md`/`.txt` как текст, `.docx` через `python-docx`, `.pdf` опц. через `pypdf`), складывать в Postgres (`tasks.attachments jsonb`), включать текст в эмбеддинг и отдавать через `get_task`. **Приоритет пользователя — чтобы у YouTrack всё работало.**
- **YouGile-нюанс:** файлы в ДВУХ местах — вложения задачи И файлы в сообщениях чата (`.md` в чате — часто и есть ТЗ).
- **Бинарные/непарсимые форматы** — сохранять только метаданные (имя, размер, mime) без контента.
- **Критерии приёмки (из задачи):** YouTrack качает+парсит `.md`/`.txt` (метаданные для прочего); YouGile качает из задачи И чата; колонка `attachments jsonb`; `get_task` отдаёт вложения; `build_task_text` включает `.md`/`.txt`; fail-soft (сбой одного файла не валит синк задачи); тесты `test_yougile_normalize`/`test_youtrack_normalize` с fixtures вложений + `test_sync_integration` сквозной.

## Related work
- **ID-140** — server-side ETL `sync_board` (reviewer-mcp как REST-клиент доски): задаёт сам пайплайн `iter_raw → watermark → normalize → index_batch`, в который встраиваются вложения. Расширять его, не ломать инкрементальность.
- **ID-170** — скоуп задач по project: образец *того же типа* правки — новое сквозное поле (`project`) протянули через `RawTask → normalize-dict → TaskRow → schema (ALTER ADD IF NOT EXISTS) → store SELECT`. Поле `attachments` идёт ровно теми же слоями — копировать паттерн.
- (dropped 3: ID-171 баг sync/get_task, ID-114 timeout индексации, ID-191 skip sync без доски — про механику синка, не про вложения)

## Subsystems
- `reviewer/tasks` — жизненный цикл задач (sync→normalize→index→graph); `TaskBoardProvider` (yougile/youtrack), `build_task_text`/`task_content_hash`, `SyncService` watermark. **Главная зона правок.**
- `reviewer/index` — `schema.sql` (таблица `tasks`), pgvector+BM25; сюда `attachments jsonb` + (опц.) лимит размера текста под эмбеддинг.
- `reviewer/entrypoints` — MCP-тулы `index_task`/`get_task` (схемы дополнить полем attachments).
- `reviewer/config` — `Settings`/board_creds (токены `YOUTRACK_TOKEN`/`YOUGILE` уже есть; новые лимиты — таймаут/размер — env-настройки деплоя).

## Relevant code
- `reviewer/tasks/boards/base.py:24` — `RawTask` dataclass: добавить `attachments: list[dict]` (field default `[]`); `TaskBoardProvider.normalize` docstring (`:50`) перечисляет ключи TaskBrief — дополнить.
- `reviewer/tasks/boards/youtrack.py:19,48,118,137` — **(приоритет)** `_FIELDS` (+`attachments(name,url,size,mimeType)`), `_issue_to_raw` (пробросить метаданные в RawTask), download+parse контента. ⚠️ `normalize_youtrack` (`:63`) сейчас ЧИСТАЯ (без I/O) — скачивание контента = I/O → делать в методе `YouTrackBoard.normalize` (`:137`, есть `self._client`), пробрасывая текст в чистый нормализатор (как yougile с subtask_titles). URL вложения у YouTrack относительный/подписанный — качать тем же Bearer+base.
- `reviewer/tasks/boards/yougile.py:108,134` — `iter_raw` (`:108`) проверить `GET /tasks/{id}` на поле `files`/`attachments`; `normalize` (`:134`) уже делает I/O (резолв подзадач) → добавить фетч сообщений чата (REST за `get_task_chat`/`get_task_messages`) + скачивание/парсинг файлов; пробросить в чистый `normalize_yougile` (`:25`).
- `reviewer/tasks/store.py:25,39,124` + `reviewer/index/schema.sql:56,70` — `build_task_text` (`:25`, +контент `.md`/`.txt` с лимитом), `TaskRow` (`:39`, +`attachments: list[dict]`), `upsert_task` SQL (`:124`), таблица `tasks` — `ALTER TABLE tasks ADD COLUMN IF NOT EXISTS attachments jsonb NOT NULL DEFAULT '[]'` (forward-only, как `project` на `schema.sql:70`); `TaskStore.get_task` SELECT (`:100`) +колонка.
- `reviewer/tasks/service.py:26,86,245` — `index_task`/`index_batch` (протянуть attachments в `TaskRow` и в `build_task_text`), `get_task` (`:245`, вернуть `attachments` в dict-ответе). Blast radius: поле сквозит 6 слоёв (RawTask→normalize→TaskRow→schema→build_task_text→get_task/index_*) + MCP-схема `mcp_server.py:77,128`.

## Test exemplars
- `tests/tasks/boards/test_youtrack_normalize.py:48` — `_issue()` fixture → `_issue_to_raw` → `normalize_youtrack`, чистые dict-asserts на бриф. Mirror: добавить fixture с `attachments`, проверить парсинг текста + метаданные-only для бинарных.
- `tests/tasks/boards/test_yougile_normalize.py` — то же для yougile (+кейс файла из сообщения чата).
- `tests/tasks/test_sync_integration.py` + `tests/tasks/test_text.py` — сквозной синк (attachments проходят весь пайплайн) и `build_task_text` (включение текста вложения в эмбед-строку).

## Constraints / open questions
- **YouTrack (приоритет):** проверить формат `url` вложения (относительный/подписанный) и нужен ли Bearer при скачивании; YouTrack может слать pre-signed ссылку. Это главный риск-узел.
- **YouGile чат-файлы:** найти точный REST-эндпоинт сообщений с файлами (MCP-подсказки: `get_task_chat`/`get_task_messages`/`send_task_file`/`upload_file`) и схему объекта файла (url/имя/тип) — требует уточнения по API.
- **Идемпотентность vs watermark:** `normalize` (а значит и скачивание) зовётся только для задач с `timestamp > cursor`. Проверить, бампается ли `timestamp` задачи при добавлении вложения/сообщения — иначе новый файл не подхватится без полного ресинка.
- **content_hash:** включение контента вложений в `build_task_text` меняет хэш → корректно триггерит переэмбед при смене файла (желаемое поведение).
- **Новые зависимости:** `python-docx` (docx), опц. `pypdf` (pdf) — добавить в `pyproject.toml` (сейчас только `httpx`). Решить, входит ли pdf в скоуп (в задаче — опционально).
- **Лимиты надёжности (env-деплой):** таймаут скачивания одного файла (~10с), пропуск файлов >10 MB, лимит символов текста под эмбеддинг (ср. `TaskService.max_chars=8000`). Fail-soft на каждом файле (warning, задача синкается без него).
- **Доска подключена (yougile, project=PRI), индекс свежий (drift=0), корпус задач прогрет (87 задач), сводки построены** — контекст-слой полный.

## Токены (этап solve-task)
Модель: claude-opus-4-8
fresh-in 51.2K · out 45.6K · cache-write 419.1K · cache-read 2.8M
Всего: 3.3M токенов
