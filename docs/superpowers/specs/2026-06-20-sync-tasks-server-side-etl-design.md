# Дизайн: server-side ETL для `sync-tasks` (PRI-140 / ID-140)

**Статус:** утверждён к реализации
**Ветка:** `feat/pri-140-sync-board-etl`
**Оценка:** M

## Контекст и проблема

`sync-tasks` сейчас — LLM-скилл: он читает доску через board-MCP (`get_tasks`) и
**дословно переписывает** все описания задач в `index_tasks_batch([...])`. Текст задач
дважды проходит через LLM (вход = `get_tasks`, выход = `index_tasks_batch`), причём
выходной проход — основная стоимость: реальный прогон 30 задач ≈ 73k output-токенов
(~$3.7 на Opus). LLM работает копировальной машиной, не добавляя ценности. PRI-96 уже
убрал O(N)→O(1) по Voyage-вызовам (batch-эмбеддинги + дедуп по `content_hash`), но текст
всё ещё гонится через LLM.

**Цель:** убрать текст задач из тракта LLM целиком. `reviewer-mcp` сам перечисляет →
нормализует → индексирует задачи; LLM лишь дёргает один тул без payload и печатает
компактный summary.

## Принятые решения (развилки brainstorming)

1. **Транспорт к доске — прямой REST за интерфейсом `TaskBoardProvider`** (не
   MCP-клиент-в-сервере). Обоснование: yougile MCP — тонкая обёртка над REST API yougile,
   аутентифицируется через `YOUGILE_API_KEY`; reviewer-mcp всё равно держит этот ключ,
   поэтому «переиспользование готового MCP-сервера» не экономит на кредах, а подпроцесс +
   MCP-хендшейк внутри MCP-сервера добавляют движущихся частей и хуже для headless/CI.
   REST-провайдер проще, надёжнее и отвязан от MCP-сессии.
2. **Триггер — только MCP-тул `sync_board` в этой задаче.** CLI-команда
   `reviewer sync-tasks` (для cron/CI) — тривиальное добавление позже поверх того же
   оркестратора; провайдер/оркестратор проектируются CLI-готовыми, но CLI не в скоупе.
3. **Инкрементальность — timestamp-курсор + `content_hash`.** Enumerate всегда полный
   (нужно для purge active-keys и свежести статусов), но для задач с `timestamp <= cursor`
   пропускаем normalize/index. `content_hash`-дедуп (уже есть) решает embed vs meta-only.
4. **Курсор хранится в существующей таблице `index_meta`** (`ref="tasks:<board>"`),
   2 метода на `TaskStore`. Без миграций схемы.
5. **Порядок реализации — снизу вверх с TDD** (provider+normalize → SyncService+watermark
   → MCP-тул → скилл/доки).

## Критерии приёмки (из задачи)

- `/sync-tasks` на доске из N задач тратит O(1) LLM-токенов (один tool call + компактный
  summary, без переписывания описаний на выход). Замер: output-токены прогона на порядок
  ниже baseline (~73k на 30 задачах).
- Результат идентичен текущему скиллу: те же `TaskBrief`, `links[]`, PR-рёбра
  `IMPLEMENTED_BY`, идемпотентность по `content_hash`.
- Повторный синк без изменений трогает ~0 задач (watermark).
- Сохранены `--limit` / фильтр по доске / `--purge-orphaned` (+ `keep_with_prs`).
- Существующие тесты не ломаются; добавлены unit на нормализацию `TaskBrief` и
  интеграционный на `sync_board`.

## Архитектура

Поток (всё внутри процесса `reviewer-mcp`):

```
sync_board (MCP-тул, без payload)
  → MCPReviewService.sync_board
    → SyncService.run(repo, board, limit, purge_orphaned, keep_with_prs)
        ├─ TaskBoardProvider.iter_raw(board, limit)        # REST enumerate
        ├─ watermark: skip raw с timestamp <= cursor
        ├─ provider.normalize(raw) → TaskBrief             # порт плейбука, per-type
        ├─ TaskService.index_batch(changed_briefs)         # СУЩЕСТВУЮЩИЙ: 1 Voyage-вызов
        │                                                  #   + content_hash + авто-PR-линковка
        ├─ TaskService.purge_orphaned_tasks(active_keys, keep_with_prs)  # СУЩЕСТВУЮЩИЙ
        └─ cursor_store.set(repo, board, new_max_ts)
  ← compact summary (counts)
```

LLM делает ровно один tool-call и печатает summary → критерий O(1) токенов. Индексатор
(`TaskService.index_batch`), дедуп по `content_hash` и авто-линковка PR
(`extract_pr_refs`/`link_review`) **переиспользуются как есть** → «результат идентичен
текущему скиллу».

## Модули (новые / изменённые)

| Файл | Изменение |
|---|---|
| `reviewer/tasks/boards/base.py` | **NEW** — Protocol `TaskBoardProvider` + dataclass `RawTask` |
| `reviewer/tasks/boards/yougile.py` | **NEW** — `YougileBoard` (httpx REST) + чистая `normalize_yougile(...)` |
| `reviewer/tasks/boards/__init__.py` | **NEW** — фабрика провайдера по `task_board.type` |
| `reviewer/tasks/sync.py` | **NEW** — `SyncService`: оркестрация + watermark + summary |
| `reviewer/tasks/store.py` | `+get_sync_cursor` / `+set_sync_cursor` (поверх `index_meta`) |
| `reviewer/mcp/service.py` | `+sync_board()` прокси в `components.sync_service` |
| `reviewer/entrypoints/mcp_server.py` | `+@mcp.tool sync_board(...)` |
| `reviewer/app.py` | `build_components`: собрать provider + `SyncService` из `Settings` |
| `reviewer/config/settings.py` | `+task_board_api_key`, `+task_board_api_base` |
| `plugin/skills/sync-tasks/SKILL.md` | упростить до одного `sync_board(...)` + печать summary |
| `plugin/skills/sync-tasks/references/sync-tasks-yougile.md` | удалить (enumerate теперь серверный) |
| `CLAUDE.md` / `README.md` | зафиксировать инверсию инварианта |

**Разделение ответственности.** `YougileBoard` = транспорт (REST I/O, резолв
column-title из одного прохода `/columns`, best-effort subtask-titles). `normalize_yougile`
= **чистая** функция без I/O — основной unit-target на паритет с плейбуком. `SyncService`
board-агностичен (видит только Protocol). jira/прочие доски — вторая реализация Protocol
позже.

## Provider-интерфейс

```python
@dataclass
class RawTask:
    key: str            # idTaskCommon (ID-N) — канонический
    project_code: str   # idTaskProject (PRI-N)
    title: str
    description: str
    status: str | None  # резолвнутый title колонки
    subtasks: list[str] # UUID подзадач (titles резолвятся в normalize, best-effort)
    timestamp: int      # epoch ms последнего изменения — для watermark

class TaskBoardProvider(Protocol):
    def iter_raw(self, board: str | None, limit: int | None) -> Iterable[RawTask]: ...
    def normalize(self, raw: RawTask) -> dict: ...   # → TaskBrief
```

- `iter_raw` дёшев (listing-эндпоинты дают полные объекты задач + один проход `/columns`
  для title статусов). `normalize` дороже (per-subtask GET для title) → watermark
  пропускает `normalize` для неизменённых.
- `YougileBoard`: httpx, base `TASK_BOARD_API_BASE` (дефолт `https://yougile.com/api-v2`),
  заголовок `Authorization: Bearer <TASK_BOARD_API_KEY>`, пагинация
  projects → boards → columns → tasks, фильтр по `--board` (по имени проекта/доски).

### Паритет нормализации (порт плейбука `task-context-yougile.md`)

`normalize_yougile(raw, key_pattern, url_template) -> TaskBrief`:
- `key` ← `idTaskCommon` (`ID-N`); `aliases` ← `[idTaskProject]` (`PRI-N`).
- `title` ← `title`; `description` ← `description`.
- `status` ← title колонки (уже резолвнут в `iter_raw`).
- `criteria[]` ← `[]` (критерии живут inline в `description`; `build_task_text` их и так
  читает).
- `links[]` ← union, дедуп по ключу:
  - по одному `{type:"subtask", key:<idTaskCommon>, title}` на каждый UUID из `subtasks`
    (резолв title через `get_task` по REST, best-effort — упавший fetch пропускается);
  - по одному `{type:"related", key}` на каждый матч `key_pattern` в `description`,
    исключая собственный `key`/`aliases` и ключи уже покрытых подзадач.
- `url` ← `url_template` с подстановкой **проектного** кода (`PRI-N`), иначе `null`.

## Watermark (инкрементальность)

Курсор = `max(timestamp)` обработанных задач, в `index_meta`:
`repo=<repo>`, `ref="tasks:<board|*>"`, `sha=str(timestamp_ms)`.

Логика `SyncService.run`:
1. `cursor = store.get_sync_cursor(repo, board)` (0/None при первом синке).
2. Полный `iter_raw` (всегда): для каждой `raw` — добавить `raw.key` в `active_keys`,
   обновить `new_cursor = max(new_cursor, raw.timestamp)`.
3. `raw.timestamp <= cursor` → skip (счётчик `unchanged`).
4. `raw.timestamp > cursor` → `normalize(raw)` → в `changed_briefs`.
5. `TaskService.index_batch(changed_briefs)` — один Voyage-вызов; `content_hash` решает
   embed vs meta-only (статус-онли изменения корректно обновляются через `update_meta`,
   т.к. yougile бампит `timestamp` при перемещении колонки).
6. При `purge_orphaned` → `TaskService.purge_orphaned_tasks(active_keys, keep_with_prs)`.
7. `store.set_sync_cursor(repo, board, new_cursor)`.

**Guard'ы корректности:**
- При заданном `--limit` (частичный обход) — **НЕ** продвигаем курсор и **НЕ** запускаем
  purge (active_keys неполный → иначе ложно удалит). В summary — варнинг.
- Первый синк (нет курсора) → всё «changed» → полный индекс; `content_hash` защищает от
  ре-эмбеда уже залитых старым скиллом задач.

## Конфиг и креды

- `Settings`: `task_board_api_key` (env `TASK_BOARD_API_KEY`), `task_board_api_base`
  (env `TASK_BOARD_API_BASE`; пусто + type==yougile → `https://yougile.com/api-v2`).
  Server-internal — **не** возвращаются `board_config()` (клиентам креды не утекают).
- `task_board_default()` (type/mcp/key_pattern/url_template) **не меняется**. Поле `mcp`
  остаётся нужным `solve-task`/`review-pr`: они читают **одну** задачу через board-MCP
  по-прежнему. Скоуп этой задачи — **только болк-синк**; одиночное чтение задачи в других
  скиллах не трогаем.
- Нет ключа / доска недоступна → `sync_board` возвращает понятный error-summary
  (fail-soft), не падает. Headless/CI: подпроцесса нет (REST), нужен лишь
  `TASK_BOARD_API_KEY`.

## MCP-тул и summary

```python
@mcp.tool()
def sync_board(board: str | None = None, limit: int | None = None,
               purge_orphaned: bool = False, keep_with_prs: bool = True) -> dict:
    """Server-side ETL: enumerate the configured board via REST, normalize, index.
    Returns a compact counts summary (no task text)."""
    return service.sync_board(board, limit, purge_orphaned, keep_with_prs)
```

Summary: `{enumerated, changed, embedded, refreshed, unchanged, failed,
purge:{deleted, protected}, warnings[], cursor_advanced}`. Только числа, без текста задач.

## Скилл и документация

- `SKILL.md`: один вызов `sync_board(...)` (маппинг `--board` / `--limit` /
  `--purge-orphaned` / `--no-keep-with-prs` → параметры) + печать summary. Поштучный обход
  доски и `index_tasks_batch([...])` из скилла убираются.
- `references/sync-tasks-yougile.md`: удаляется (enumerate серверный, нормализация в Python).
- `index_tasks_batch` MCP-тул: **оставляем** (безвреден, не дёргается скиллом). Удаление —
  опциональный cleanup отдельной задачей.
- `CLAUDE.md` / `README.md`: зафиксировать, что болк-синк теперь ходит на доску по REST в
  MCP-слое (`TaskBoardProvider`), креды в env reviewer-mcp (`TASK_BOARD_API_KEY`); инвариант
  «reviewer Python никогда не трогает доску» получает документированное исключение для
  болк-синка (одиночное чтение задачи в `solve-task`/`review-pr` по-прежнему через board-MCP
  на стороне LLM).

## Тестирование

- **Unit — паритет нормализации:** `normalize_yougile` на raw-задаче (+ column map) →
  ожидаемый `TaskBrief`. Покрыть: `key`/`aliases` из кодов; `status` из title колонки;
  `url` из проектного кода; `links` = union(subtasks, related по `key_pattern` минус
  self/aliases); дедуп ключей; пустые/отсутствующие поля.
- **Unit — watermark:** фейковый provider с timestamp'ами → changed vs skipped, продвижение
  курсора, `active_keys` для purge, guard'ы под `--limit` (нет продвижения курсора, нет
  purge).
- **Integration (`-m integration`):** `sync_board` против фейкового provider'а + Postgres/
  Neo4j → идемпотентность (второй прогон: `changed≈0`), рёбра `IMPLEMENTED_BY` из PR-ссылок
  в `description`.
- Существующие тесты `index_batch` / `purge` / `board_config` не трогаются.

## Риски

- **Инверсия инварианта** «reviewer Python никогда не трогает доску»: доступ к доске
  появляется в MCP-слое (не в чистом движке за интерфейсами). Митигировано: за Protocol,
  креды в env reviewer-mcp, документировано в CLAUDE.md/README.
- **Паритет нормализации с плейбуком** — основной риск «идентичности результата».
  Митигировано прицельными unit-тестами на `normalize_yougile`.
- **`--limit` + `--purge-orphaned`** взаимоисключающи по корректности (частичный обход не
  даёт полного active-keys). Митигировано guard'ом: под `--limit` purge выключается с
  варнингом.

## Вне скоупа (YAGNI)

- CLI-команда `reviewer sync-tasks` (cron/CI) — позже поверх того же `SyncService`.
- Миграция `solve-task` / `review-pr` на REST для одиночного чтения задачи.
- Удаление `index_tasks_batch` MCP-тула.
- Server-side `since`-фильтр REST (тянуть только изменённые) — выгода маргинальна, ломает
  purge.
- Провайдеры jira/прочих досок (интерфейс к ним готов, реализаций нет).
