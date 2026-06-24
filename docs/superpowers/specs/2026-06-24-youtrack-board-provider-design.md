# YouTrack-провайдер доски + связка ключей в env + выбор доски в .review.yml

**Статус:** дизайн утверждён · **Дата:** 2026-06-24 · **Оценка:** M
**Слой:** Движок (reviewer CLI/MCP) + плагин (скиллы configure-review, review-pr).

## Проблема

Доска задач сейчас **одна на деплой**: `make_board_provider(settings)` строит ровно один провайдер
из глобального env-типа (`TASK_BOARD_TYPE`), креды — единичные (`TASK_BOARD_API_KEY`/`API_BASE`),
единственная реализация — `YougileBoard` (REST). Нужно:

1. Добавить **YouTrack** (JetBrains) как тип доски наравне с Yougile, работающий **как Yougile — server-side
   REST-скриптами** (`make_board_provider` → `SyncService.sync_board`), **не через board-MCP** — чтобы
   синк стоил O(1) LLM-токенов (LLM не перечисляет доску и не переносит текст задач).
2. Позволить **связку ключей в env** (под каждую доску свой ключ) и **выбор доски в `.review.yml`**
   проекта из тех, что настроены.
3. Расширить скилл `reviewer_configure-review` (редактирует `.review.yml`), чтобы он умел настраивать
   блок `task_board` (сейчас явно исключён).

## Ключевое архитектурное напряжение (и его снятие)

**Задачи в reviewer глобальны** — таблица `tasks` (Postgres) и граф `:Task` (Neo4j) без repo-скоупа;
`sync_board` — глобальный server-side ETL. При этом пользователь хочет per-repo выбор доски. Эти две
вещи примиряются так:

- **env — связка кредов всех досок**; `sync_board` синкает **все настроенные доски** в общий
  глобальный пул, каждую со своим watermark-курсором. Инвариант «задачи глобальны» сохраняется,
  синк остаётся O(1) токенов.
- **`.review.yml task_board.type` репо** — это **client-side** выбор: какой `key_pattern` извлекать
  из PR, какой `url`, какую доску читать при одиночном чтении задачи. Серверный синк он **не трогает**.

То есть «выбор доски в `.review.yml`» не протекает в bulk-синк (и не должен) — синк глобален по env,
а yml лишь настраивает, как PR конкретного репо мапится на задачи из общего пула.

## Конфиг (форма A — per-type префиксы, только ENV деплоя)

Секреты только в env, в `.review.yml` кредов нет (инвариант, как у VCS — нет блока `vcs:`).

| ENV | Дефолт | Назначение |
|---|---|---|
| `TASK_BOARD_TYPE` | `""` | **client-дефолт** типа доски для политики (`task_board_default()`), переопределяемый per-repo в `.review.yml`. **Не** управляет синком — синк обходит все настроенные доски. Остаётся как сейчас. |
| `YOUGILE_API_KEY` | `""` | REST-ключ yougile (back-compat: фолбэк на `TASK_BOARD_API_KEY`) |
| `YOUGILE_API_BASE` | `https://yougile.com/api-v2` | base URL yougile (back-compat: фолбэк на `TASK_BOARD_API_BASE`) |
| `YOUTRACK_TOKEN` | `""` | permanent token youtrack (`perm:...`) |
| `YOUTRACK_BASE_URL` | `""` | base URL youtrack API; **обязателен** (инстанс-специфичен), напр. `https://company.youtrack.cloud/api` |
| `TASK_BOARD_KEY_PATTERN` | `[A-Z]+-\d+` | **общий** регэксп ключа (годится и для yougile `ID-N`/`PRI-N`, и для youtrack `PROJ-123`) |
| `TASK_BOARD_URL_TEMPLATE` | `""` | **только yougile** (его API не отдаёт ссылку); youtrack строит ссылку из base URL сам |

**Back-compat.** Существующие деплои (`TASK_BOARD_API_KEY` + `TASK_BOARD_TYPE=yougile`) работают
1-в-1: `board_creds("yougile")` фолбэчит на legacy-переменные. Почему `key_pattern`/`url_template`
остаются общими, а не per-type: оба типа используют один паттерн ключа; `url_template` нужен только
yougile, youtrack ссылку выводит из `YOUTRACK_BASE_URL` (`<host>/issue/<idReadable>`).

## Компоненты

### 1. `reviewer/config/settings.py` — резолв кредов по типу

- Новый метод `board_creds(type_) -> tuple[str, str]` (api_key, api_base):
  - `yougile` → `YOUGILE_API_KEY`/`YOUGILE_API_BASE`, фолбэк на legacy `task_board_api_key`/
    `task_board_api_base`, затем на дефолт `_BOARD_API_BASE_DEFAULTS["yougile"]`.
  - `youtrack` → `YOUTRACK_TOKEN`/`YOUTRACK_BASE_URL` (дефолта base нет — обязателен).
- Новый метод `configured_board_types() -> list[str]` — типы, у которых есть api_key (для перебора
  в `make_board_providers`).
- Новые поля Settings: `yougile_api_key`, `yougile_api_base`, `youtrack_token`, `youtrack_base_url`
  (pydantic читает из env по имени; legacy `task_board_api_key`/`api_base` остаются для фолбэка).
- `_BOARD_API_BASE_DEFAULTS` дополняется только yougile (как сейчас); youtrack дефолта не имеет.
- `task_board_default()` (client-конфиг для политики) не меняется по форме.

### 2. `reviewer/tasks/boards/base.py` — обобщение контракта

- В `RawTask` добавить `links: list[dict] = field(default_factory=list)` — предрезолвленные ссылки.
  yougile продолжает резолвить через `subtask_ids` в `normalize` (дорогой best-effort); youtrack
  кладёт `links` уже в `iter_raw` (всё в одном list-запросе). Обратносовместимо (default — пусто).
- В Protocol `TaskBoardProvider` добавить свойство `board_type: str` — для ключа курсора синка.

### 3. `reviewer/tasks/boards/youtrack.py` (новый) — `YouTrackBoard`

Реализует `TaskBoardProvider` по образцу `YougileBoard`. YouTrack REST API:

- **Клиент:** `base_url = YOUTRACK_BASE_URL`, заголовок `Authorization: Bearer perm:<token>`,
  httpx, timeout 30s. `board_type = "youtrack"`.
- **`iter_raw(board, limit)`** — один list-эндпоинт отдаёт всё (дешевле yougile, без доп. запросов):
  ```
  GET /issues?fields=idReadable,summary,description,updated,
      customFields(name,value(name)),links(linkType(name),direction,issues(idReadable))
      &query=project: <board>&$top=<page>&$skip=<offset>
  ```
  Пагинация через `$top`/`$skip`. Маппинг в `RawTask`:
  - `key` ← `idReadable` (`PROJ-123`); `project_code` = `idReadable` (один счётчик, без второго кода).
  - `title` ← `summary`; `description` ← `description` (может быть null → `""`).
  - `status` ← `customFields` где `name == "State"` → `value.name` (иначе `None`).
  - `timestamp` ← `updated` (epoch ms) — watermark.
  - `subtask_ids` = `[]`; `links` ← из `links`: для каждого `issues[].idReadable` →
    `{type: "subtask"|"related", key: idReadable, title}` (тип из `linkType.name`/`direction`:
    «Subtask»/outward → subtask, иначе related). `board` фильтрует по проекту (query `project: <board>`).
  - `limit` обрезает обход (как yougile).
- **`normalize(raw)`** → делегирует чистой `normalize_youtrack(raw, key_pattern, base_url)`.
- **`normalize_youtrack(raw, key_pattern, base_url)`** — чистая, без I/O:
  - `key`/`aliases`: `key = raw.key`, `aliases = []` (один код).
  - `links`: берёт `raw.links` как есть + досканивает `description` по `key_pattern` на related-ключи,
    исключая уже покрытые (как `normalize_yougile`).
  - `url`: `<web_base>/issue/<idReadable>`, где `web_base` = `base_url` без хвоста `/api`.
  - `criteria`: `[]` (требования живут в `description`).
  - возвращает TaskBrief dict `{key, aliases, title, description, criteria, status, url, links}`.
- **`close()`** закрывает httpx-клиент.

### 4. `reviewer/tasks/boards/__init__.py` — множественная фабрика

- `make_board_provider(settings, type_) -> TaskBoardProvider | None` — строит **один** провайдер по
  явному типу из его кредов (`board_creds(type_)`); `None`, если кредов нет. Сигнатура расширяется
  параметром `type_`.
- `make_board_providers(settings) -> list[TaskBoardProvider]` — перебирает
  `settings.configured_board_types()`, строит провайдер на каждый, отбрасывает `None`.
- `__all__` дополняется `make_board_providers`.

### 5. `reviewer/tasks/sync.py` — мульти-провайдерный `SyncService`

- Конструктор принимает `providers: list[TaskBoardProvider]` (вместо одного `provider`).
- `_cursor_ref(provider, board)` → `tasks:<provider.board_type>:<board or '*'>` (было `tasks:<board>`).
- `run(...)` обходит все провайдеры, синкает каждую доску своим курсором, **агрегирует** counts
  (enumerated/changed/embedded/unchanged/failed/purge) в один summary. Пустой список провайдеров →
  тот же понятный error-summary, что и сейчас при `provider is None`.
- `app.py:63` передаёт `make_board_providers(settings)`.

### 6. `reviewer/install.py` — wizard/`.env.example`

Группа «Доска задач» расширяется per-type полями (`YOUGILE_API_KEY`, `YOUTRACK_TOKEN`,
`YOUTRACK_BASE_URL`) с пояснениями: где взять permanent token youtrack (Profile → Account Security →
New permanent token), что base URL инстанс-специфичен. `TASK_BOARD_API_KEY` помечается legacy/алиасом.

### 7. Плагин — скилл `reviewer_configure-review`

`plugin/skills/configure-review/SKILL.md` — расширить скоуп на блок `task_board`:
- В Scope убрать `task_board` из запрета; добавить шаг: спросить тип доски репо
  (`yougile`/`youtrack`/выключить) и записать блок:
  ```yaml
  task_board:
    type: youtrack
    key_pattern: '[A-Z]+-\d+'
    # url_template нужен только yougile; youtrack строит ссылку из base URL
  ```
  Пустой `task_board:` — явно выключить доску для репо.
- Скилл остаётся **standalone** (только git, без MCP/БД): пишет лишь **несекретный** выбор +
  `key_pattern`/`url_template`. **Креды никогда не пишет** — напоминает (по-русски), что
  `YOUTRACK_TOKEN`/`YOUTRACK_BASE_URL` задаются в env деплоя reviewer-mcp.
- Обновить frontmatter `description` (триггеры «настроить доску»/«выбрать доску для репо»);
  merge-preserve остальных ключей (как сейчас).

### 8. Плагин — скилл `reviewer_review-pr`: store-first чтение задачи

YouTrack без board-MCP → текущее чтение задачи (шаг 2, только через `mcp__<task_board.mcp>__*`) не
работает. Унифицируем review-pr с solve-task — **store-first**:
- Сначала `get_task(key)` из стора reviewer (доска уже синкнута глобально). **Hit** → используем.
- **Miss** И `task_board.mcp` задан → фолбэк на board-MCP плейбук (как сейчас, yougile/jira).
- **Miss** И `mcp` пуст (youtrack) → задача не найдена: пропускаем requirements-измерение (как при
  любом промахе), ревью не прерываем.

**Отдельный `task-context-youtrack.md` НЕ нужен** — маппинг (idReadable→key, «State»→status, links)
живёт в `normalize_youtrack` (Python, серверно). Плейбуки нужны только доскам с board-MCP read-path.

## Тесты

- `tests/tasks/boards/test_youtrack_normalize.py` (новый) — чистый `normalize_youtrack`: маппинг
  idReadable/State/links/url-деривация, доскан related из description, null-description.
- `tests/tasks/boards/test_base.py` — расширить: `make_board_provider(settings, "youtrack")` строит
  провайдер; `make_board_providers` собирает все настроенные; `None` при отсутствии кредов.
- `tests/config/` — `board_creds(type)` (per-type + legacy-фолбэк yougile), `configured_board_types()`.
- `tests/tasks/test_sync.py` — мульти-провайдер: обход >1 доски, курсор `tasks:<type>:<board>`,
  агрегация counts, инкрементальность по watermark на провайдер.
- `tests/skills/` — guard: configure-review пишет валидный блок `task_board`, не клоберит другие ключи,
  не пишет креды; включить ключ `task_board` в валидатор `.review.yml`.

## Back-compat / миграция

- Legacy `TASK_BOARD_API_KEY`/`API_BASE` + `TASK_BOARD_TYPE=yougile` → работают через фолбэк в
  `board_creds`.
- Курсор `tasks:<board>` → `tasks:<type>:<board>`: одноразовый ре-скан доски (старый курсор не
  совпадёт по ключу). Ре-эмбеддинг ~0 (дедуп по `content_hash` в `index_task`), курсор быстро
  переустанавливается. Без миграции данных.
- Задачи остаются глобальными; кросс-репо ретрив не вводится.

## Не делаем (YAGNI)

- Per-type `key_pattern`/`url_template` в env — общий паттерн достаточен, youtrack-url деривируется.
- Per-repo выбор провайдера для серверного синка — синк глобален по env (см. напряжение выше).
- `task-context-youtrack.md` плейбук — youtrack без board-MCP, маппинг серверный.
- Блок `vcs:`/любые секреты в `.review.yml`.
