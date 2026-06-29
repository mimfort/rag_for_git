# Дизайн — PRI-196: вложения (attachments) в sync_board для YouTrack и YouGile

- **Задача:** [PRI-196](https://ru.yougile.com/team/686c049c8af8/#PRI-196) — Поддержка вложений (attachments) в sync_board для YouTrack и YouGile
- **Бриф:** `docs/superpowers/briefs/2026-06-28-PRI-196-attachments-sync-board.md`
- **Дата:** 2026-06-28

## Проблема

`sync_board` (server-side ETL синка задач с доски) полностью игнорирует вложения. ТЗ/спеки
часто лежат в `.md`/`.docx`/`.pdf` вложениях (а в YouGile — нередко в файлах сообщений чата),
поэтому контекст задачи для `solve-task`/`review-pr` оказывается неполным. Нужно: скачивать
вложения, парсить текст, складывать в Postgres, включать текст в эмбеддинг и отдавать через
`get_task`. **Приоритет — корректная работа YouTrack.**

## Скоуп (зафиксировано на брейнсторме)

- **Форматы парсинга:** `.md`/`.txt` (декод как текст), `.docx` (`python-docx`), `.pdf` (`pypdf`).
  Непарсимое/бинарное — сохраняем только метаданные (имя, размер, mime) без контента.
- **YouGile-источники:** файлы задачи **И** файлы сообщений чата, каждый источник best-effort
  (нет эндпоинта / пусто / сбой → пропускаем, не роняем задачу).
- **Усечение, не summary:** в эмбеддинг идёт усечённый текст (summary потребовал бы LLM-вызов и
  сломал бы инвариант «синк ≈ 0 LLM-токенов»).

## Архитектура и поток данных

### Главный шов — `normalize`, не `iter_raw`

Сохраняем инвариант подсистемы: `iter_raw` дёшев (listing-эндпоинты), I/O — в `normalize`.
`normalize` зовётся оркестратором (`SyncService`) **только для изменившихся задач** (watermark по
`timestamp`), поэтому скачивание вложений точечное и инкрементальное.

### Чистые нормализаторы остаются чистыми (инъекция контента)

`normalize_youtrack` / `normalize_yougile` (модульные функции) **остаются чистыми — без I/O**.
Скачивание и парсинг живут в методе класса-провайдера (`YouTrackBoard.normalize` /
`YougileBoard.normalize`, у них есть `self._client`), а готовый текст вложений **инжектится** в
чистый нормализатор аргументом — ровно как `subtask_titles` инжектятся в `normalize_yougile`
сейчас. Это сохраняет существующий стиль unit-тестов (чистая функция на фикстурах) и держит I/O в
тестируемом-с-fake-клиентом методе провайдера.

### Поток поля `attachments` (сквозит 6 слоёв — как `project` в ID-170)

```
iter_raw            → RawTask.attachments: list[dict]   # ТОЛЬКО метаданные {name,mime,size,url|id}
                                                          #   youtrack: inline из _FIELDS; yougile: []
Board.normalize()   → скачать+распарсить (self._client, fail-soft по каждому файлу)
                    → normalize_*(raw, ..., attachment_contents=[...])   # инъекция текста
normalize_*         → TaskBrief["attachments"] = [{name, mime_type, size, content_text|None}]  # чистая
TaskService         → build_task_text(..., attachments) + TaskRow.attachments
.index_task/_batch
TaskStore           → tasks.attachments jsonb (upsert + SELECT в get_task)
get_task            → возвращает attachments в ответе (solve-task / review-pr)
```

### Распределение источников по доскам

**YouTrack (приоритет):**
- `_FIELDS` += `attachments(name,size,mimeType,extension,url)` → метаданные приходят бесплатно в
  существующем пагинированном `/issues`-запросе (`iter_raw`), кладутся в `RawTask.attachments`.
- В `YouTrackBoard.normalize` контент скачивается по полному URL = `origin + относительный url`.
  - `url` относительный с подписью: `/api/files/7-2?sign=...&updated=...`.
  - **Bearer не нужен** при скачивании (клиент опознаётся по `sign=`); посылать заголовок
    безвредно, но проще GET по абсолютному URL.
  - `origin` (scheme+host) выводится из `base_url` (`base_url` оканчивается на `/api`; брать
    именно origin, а не base, чтобы не задвоить `/api`). Покрывает cloud и self-hosted под
    контекст-путём (`/youtrack/...`).
- Альтернатива `base64Content` (data-URI inline в listing) **отвергнута**: раздула бы дешёвый
  `iter_raw` для всех задач; ленивое скачивание в `normalize` (только изменившиеся) эффективнее.

**YouGile:**
- `iter_raw` не знает файлы дёшево → `RawTask.attachments = []`.
- В `YougileBoard.normalize` точечно (только для изменившейся задачи):
  - **(а) файлы задачи** — проверить ответ `GET /tasks/{id}` на поле со списком файлов
    (`files`/`attachments`), если API его отдаёт.
  - **(б) файлы чата** — сообщения через `ChatMessageController` (файловые сообщения).
  - Каждый источник независимо fail-soft. Точную схему объекта файла подтвердить эмпирически на
    живом API (MCP `yougile` подключён) в фазе реализации.

## Компоненты

### Новый модуль `reviewer/tasks/boards/attachments.py` (board-агностичный)

Единственное место логики «скачать → распарсить». Оба провайдера зовут его.

**Чистая часть (парсинг, без I/O — полностью unit-тестируется на fixture-байтах):**
```python
def extract_text(name: str, mime: str | None, data: bytes) -> str | None:
    # диспатч: расширение (primary) → mime (fallback)
    #   .md/.markdown/.txt → data.decode("utf-8", errors="replace")
    #   .docx              → python-docx (Document(BytesIO(data)), собрать параграфы)
    #   .pdf               → pypdf (PdfReader, extract_text по страницам)
    #   иначе / пусто      → None  (метаданные-only)
    # любое исключение парсера → None + log.warning (fail-soft)
```

**I/O-часть (скачивание, тестируется с fake-клиентом):**
```python
def download(client, url: str, *, timeout: float, max_bytes: int) -> bytes | None:
    # GET с таймаутом; Content-Length > max_bytes → None (skip без полного скачивания);
    # стриминг с обрывом при превышении лимита; любой сбой → None + log.warning
```

**Склейка:**
```python
def fetch_attachment(client, *, name, mime, size, url, timeout, max_bytes) -> dict:
    # → {name, mime_type, size, content_text}  (content_text=None если skip/непарсимо/сбой)
```

**Свойства:**
- Fail-soft гранулярно по файлу: сбой одного → `content_text=None` (метаданные сохранены), задача
  и остальные файлы не страдают.
- Лимиты — из `Settings` (env-дефолты деплоя), без хардкода в логике.

### Хранение (`reviewer/index/schema.sql`, `reviewer/tasks/store.py`)

**Схема (forward-only, как `project` на `schema.sql:70`):**
```sql
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS attachments jsonb NOT NULL DEFAULT '[]';
```
Формат: `[{"name":"spec.md","mime_type":"text/markdown","size":1234,"content_text":"…"}]`.

**Два разных потолка текста:**
- **jsonb — полный текст** (санити-кап `task_attachment_store_chars` ~200K/файл): контекст для
  солвера, отдаётся через `get_task`.
- **эмбеддинг — усечённый текст** (`task_attachment_embed_chars` ~8K/файл): не выбивать Voyage TPM.

**`store.py`:**
- `build_task_text(title, description, criteria, attachments=None)` (`:25`) — добавляет имя +
  усечённый `content_text` каждого вложения. Дефолт `None` (обратная совместимость).
- `TaskRow` (`:39`) += `attachments: list[dict] = field(default_factory=list)`.
- `upsert_task` SQL (`:124`) += колонка `attachments` (psycopg сериализует через `Json`-адаптер).
- `TaskStore.get_task` SELECT (`:100`) += колонка `attachments`.
- `content_hash`: текст вложений входит в `build_task_text` → хэш меняется при смене контента
  файла → переэмбед триггерится корректно (когда `normalize` отработал).

### Сервис (`reviewer/tasks/service.py`)

- `index_task` (`:26`) / `index_batch` (`:86`) — тянут `attachments` из TaskBrief в `TaskRow` и
  передают в `build_task_text`.
- `get_task` (`:245`) — добавляет `"attachments"` в возвращаемый dict.

### MCP-поверхность (`reviewer/entrypoints/mcp_server.py`)

Тулы `index_task`/`get_task` уже типизированы как `dict`/`dict|None` → схема FastMCP **не
меняется**, только докстринги (`:77`, `:128`).

### Конфиг (`reviewer/config/settings.py`)

Новые env-настройки деплоя:

| Ключ | Дефолт | Смысл |
|---|---|---|
| `task_attachment_max_bytes` | `10485760` (10 MB) | пропуск файлов больше |
| `task_attachment_timeout` | `10.0` | таймаут скачивания одного файла |
| `task_attachment_embed_chars` | `8000` | потолок текста на файл в эмбеддинге |
| `task_attachment_store_chars` | `200000` | санити-кап текста на файл в jsonb |

### Зависимости (`pyproject.toml`)

`python-docx>=1.1` (импорт `docx`), `pypdf>=4.0` — в основные deps (нужны на сервере при синке;
обе — чистый Python, лёгкие).

## Обработка ошибок (fail-soft на каждом уровне)

- Парсинг файла упал / неизвестный формат → `content_text=None`, метаданные сохраняются.
- Скачивание упало / таймаут / >лимита → файл пропускается (метаданные при наличии сохраняются).
- Источник YouGile (задача или чат) недоступен → его файлы пропускаются, второй источник работает.
- Сбой по любому файлу не роняет синк задачи; сбой задачи не роняет синк доски (как сейчас).

## Граница применимости (watermark) — осознанное ограничение

Скачивание идёт в `normalize`, который зовётся только при `timestamp > cursor`. Если файл/сообщение
добавлено без бампа `timestamp` задачи — новый файл не подхватится инкрементально.
- **YouTrack:** загрузка вложения = update issue → `updated` бампается. ОК.
- **YouGile:** файл в карточке обычно бампает `timestamp`; файл в **чате** может не бампать —
  точечный риск.
- **Решение:** документировать ограничение; принудительный ре-синк — сбросом курсора (или полным
  проходом). **Не** тащить чат/файлы в `iter_raw` (это сделало бы дешёвый листинг дорогим для всех
  задач) и **не** бампать watermark по чат-таймстампам (потребовало бы фетча чата в `iter_raw`).

## Тестирование

Unit — на фейках, без реальных сетей/БД (как принято в подсистеме):
- `tests/tasks/boards/test_attachments.py` (**новый, TDD-ядро**) — `extract_text` на fixture-байтах
  (`.md` → текст; крошечный реальный `.docx`/`.pdf` → текст; неизвестный тип → None; битый файл →
  None fail-soft); `download` с fake httpx-клиентом (таймаут, Content-Length > лимит → skip).
- `tests/tasks/boards/test_youtrack_normalize.py` — fixture issue с `attachments` + fake httpx-клиент
  в `YouTrackBoard.normalize`; assert `TaskBrief.attachments` (контент для текстовых +
  метаданные-only для бинарных); проверка сборки полного URL из `origin + relative`.
- `tests/tasks/boards/test_yougile_normalize.py` — то же + кейс файла из сообщения чата.
- `tests/tasks/test_text.py` — `build_task_text` включает усечённый контент вложений.
- `tests/tasks/test_sync_integration.py` + store round-trip (integration) — attachments проходят
  весь пайплайн RawTask→normalize→index→store→`get_task` (jsonb upsert/SELECT).

## Критерии приёмки (из PRI-196)

- [ ] `sync_board` для YouTrack скачивает+парсит `.md`/`.txt`/`.docx`/`.pdf`, метаданные для прочего.
- [ ] `sync_board` для YouGile скачивает файлы из задачи И из сообщений чата (best-effort каждый).
- [ ] Таблица `tasks` имеет колонку `attachments jsonb`.
- [ ] `get_task` возвращает вложения в ответе.
- [ ] `build_task_text` включает (усечённый) текст вложений в эмбеддинг.
- [ ] Fail-soft: сбой парсинга/скачивания одного файла не ломает синк задачи.
- [ ] Тесты: `test_yougile_normalize`/`test_youtrack_normalize` с fixtures вложений + сквозной
      `test_sync_integration`.

## Затрагиваемые файлы

- `reviewer/tasks/boards/attachments.py` — **новый** модуль скачивания/парсинга.
- `reviewer/tasks/boards/base.py` — `RawTask` += `attachments`; докстринг `TaskBoardProvider.normalize`.
- `reviewer/tasks/boards/youtrack.py` — `_FIELDS`, `_issue_to_raw`, `YouTrackBoard.normalize`,
  `normalize_youtrack` (+ инъекция).
- `reviewer/tasks/boards/yougile.py` — `YougileBoard.normalize` (фетч задачи+чата),
  `normalize_yougile` (+ инъекция).
- `reviewer/tasks/store.py` — `build_task_text`, `TaskRow`, `upsert_task`, `get_task` SELECT.
- `reviewer/tasks/service.py` — `index_task`, `index_batch`, `get_task`.
- `reviewer/index/schema.sql` — `ALTER TABLE tasks ADD COLUMN attachments`.
- `reviewer/config/settings.py` — 4 новые настройки.
- `reviewer/entrypoints/mcp_server.py` — докстринги `index_task`/`get_task`.
- `pyproject.toml` — `python-docx`, `pypdf`.
- Тесты: новый `test_attachments.py` + правки `test_youtrack_normalize`/`test_yougile_normalize`/
  `test_text`/`test_sync_integration` + store round-trip.
