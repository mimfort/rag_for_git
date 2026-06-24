# PRI-170 (ID-170) — Скоуп синка задач по `.review.yml`: только указанная доска и проект репозитория

**Дата:** 2026-06-24
**Ключ задачи:** PRI-170 / ID-170 (yougile, проект `PRI`)
**Статус:** дизайн утверждён, готов к writing-plans

## Проблема

Стор задач (таблица `tasks` в Postgres) и граф (`:Task` в Neo4j) **глобальны** — без скоупа по
проекту/доске:

- `sync_board` обходит ВСЕ типы досок (`make_board_providers` строит провайдер на каждый
  настроенный тип) и ВСЕ проекты (yougile `iter_raw` перебирает все `/projects`).
- На чтении (`search_tasks` / `get_task` / `get_task_context` / обход графа связей) задачи разных
  проектов смешиваются.

**Живое подтверждение бага:** `get_task_context(PRI-170)` вернул `Linked tasks: [related] TES-1` —
задача чужого youtrack-проекта (`TES`) протекла в связи yougile-задачи (`PRI`). Утечка возникает,
потому что `normalize` извлекает из описания ключи по `key_pattern` (`[A-Z]+-\d+`) и создаёт
TASK_LINK-стаб на любой совпавший код, включая чужой проект.

## Цель

Вести N репозиториев параллельно, у каждого своя доска+проект в `.review.yml`. Из конкретного репо
использовать ТОЛЬКО его проект — и на запись (синк), и на чтение (поиск/граф). Чужие проекты/доски не
попадают ни в синк, ни в связи/контекст.

## Критерии приёмки

1. `.review.yml` умеет задавать конкретный проект (`task_board.project`); пусто = как сейчас (всё).
2. Синк из репо ограничен указанным проектом и ОДНИМ типом доски (не всеми из `TASK_BOARD_TYPE`).
3. Чтение скоупнуто: `search_tasks` / `get_task_context` / обход графа из репо отдают только задачи и
   связи его проекта; связи в чужие проекты не вылезают.
4. N репо с разными `.review.yml` работают независимо (write + read); репо с одним проектом видят
   общие задачи (модель «задача на N микросервисов» сохранена).
5. `configure-review` спрашивает про проект, пишет его в `task_board.project`, объясняет последствия
   пустого значения.

## Ключевые проектные решения (развилки)

| Развилка | Решение | Почему |
|---|---|---|
| Измерение скоупа | **По ПРОЕКТУ** (метка `project` на задаче), не по репо | Сохраняет инвариант «задача покрывает несколько микросервисных репо»: репо с одним проектом видят общие задачи. Совпадает с формулировкой PRI-170. |
| Резолв скоупа на чтении | **Клиент передаёт** `project` в read-тулы | Скилы уже читают `.review.yml`; сервер при session-less чтении не имеет чекаута. Минимум новой инфраструктуры. Отклонён серверный repo→project маппинг (repo_vcs-style, ID-133) — тяжелее без выигрыша. |
| Кросс-проект связи | **Фильтр на чтении + чистота на записи** | Read-фильтр — обязательный сейфти-нет (покрывает старые утечки); на записи scoped-синк не создаёт стабы на чужой префикс (граф чистый). |
| Хранилище | **Глобальный стор/граф + фильтр по `project`** | Минимум инфраструктуры; согласуется с «мульти-репо через дискриминатор». |

**Контракт значения `project`:** одна и та же строка в `.review.yml task_board.project`, в метке
задачи и в read-фильтре. Для yougile — название проекта (project title, как в UI yougile); для
youtrack — project short name (напр. `TES`).

## Архитектура изменений

### 1. Модель данных и миграция

**Postgres** (`reviewer/index/schema.sql`, после блока `tasks` на стр. 56):
```sql
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS project text NOT NULL DEFAULT '';
```
Идемпотентно, в стиле существующих миграций (`ADD COLUMN IF NOT EXISTS` / `ALTER ... IF EXISTS`),
применяется на старте. Существующие ~62 строки получают `project=''`.

`board_type` отдельной колонкой пока **не** выносим — проект кодируем канонической строкой. Если
понадобится развязка коллизий имён между досками — добавим вторую колонку тем же `ALTER`.

**Neo4j `:Task`** (`reviewer/tasks/graph.py`): `upsert_task` пишет проперти `project`. Стабы
(`upsert_links` / `link_pr` / `link_prs_batch`) проекта не имеют → на чтении трактуются как «не наш
проект».

**Семантика `project=''` (untagged):** «вне проекта», **исключается** из scoped-чтения (строгая
изоляция, критерий 4). Видимость восстанавливается ре-синком/бэкфиллом.

**Авто-бэкфилл (без ручного шага):** watermark-skip-ветка в `SyncService._sync_provider`
(`sync.py:48-49`, сейчас `unchanged += 1; continue`) при scoped-синке делает дешёвый `update`
проекта для уже присутствующих задач (без переэмбеда). После одного синка на доску всё
протегировано; курсор не сбрасываем. (Альтернатива — однократный full re-sync со сбросом курсора;
выбран авто-бэкфилл.)

### 2. Путь записи (синк)

**`sync_board` MCP-тул** (`mcp_server.py:102`, `mcp/service.py:290`): новый параметр
`board_type: str | None = None` рядом с существующим `board` (фильтр проекта).
- `board_type=None, board=None` → текущее поведение: все провайдеры, все проекты (back-compat,
  deploy-wide синк).
- `board_type="yougile", board="<project>"` → только провайдер этого типа, отфильтрован по проекту;
  задачи тегируются `project="<project>"`.

**`SyncService.run`** (`sync.py:81`): принимает `board_type`; когда задан — итерирует только провайдер
с `provider.board_type == board_type` (вместо всех `self._providers`). Курсор уже
per-`(type, board)` (`sync.py:26`) — опираемся, не меняем.

**Тегирование (источник метки):**
- `RawTask` (`boards/base.py:14`) — новое поле `project: str` (канон. идентификатор проекта).
  Провайдер знает проект в момент `iter_raw`: yougile — `proj["title"]` (`yougile.py:109-111`);
  youtrack — значение `board`-фильтра (`query: project: <board>`, `youtrack.py:122-123`).
- `normalize()` обоих провайдеров кладёт `project` в TaskBrief dict.
- `TaskService.index_task` / `index_batch` → `TaskRow.project` → `tasks.project` и `:Task.project`.

**Нюанс yougile project vs board title:** `iter_raw` `board`-фильтр матчит И project title, И board
title (`yougile.py:111`). Штампуем **именно project title** для консистентности с read-фильтром.
Точная нормализация фиксируется в плане.

### 3. Путь чтения

**Read-тулы** `search_tasks` / `get_task` / `get_task_context` (`mcp_server.py:115,120,126`;
`mcp/service.py:256,260,264`) — новый параметр `project: str | None = None`. `None`/`""` → без
фильтра (back-compat). В read-фильтре используется только `project` (не `board_type`).

**`TaskStore`** (`reviewer/tasks/store.py`):
- `search` (стр. 168) — `WHERE project = %(project)s` в обе ветки CTE (`bm25`, `ann`), когда задан.
- `get_task` (стр. 99) — `AND project = %(project)s`, когда задан.

**`TaskGraph.task_context`** (`graph.py:106`): обход `(t)-[l:TASK_LINK]-(n:Task)` фильтрует соседей
`n.project = $project`, когда задан. Сама `t` резолвится по `codes` как сейчас; PR/TOUCHES не
трогаем. Стабы без `project` отсекаются (сейфти-нет против утечек вроде `TES-1`).

**Проброс из скилов (клиент передаёт):** скилы читают `task_board.project` из `.review.yml` (или из
`get_board_config()` — env-дефолт) и передают в тулы:
- **solve-task** — `get_task`, `get_task_context`, `search_tasks`; preflight `sync_board` теперь с
  `board_type`+`board` (scoped, не весь корпус).
- **sync-tasks** — `sync_board(board_type, board, …)` из `.review.yml`.
- **ask / pr-walkthrough / review-pr** — где зовут task-чтение, прокинуть `project`. review-pr
  читает `.review.yml` server-side через политику (`policy.task_board`) — уточнить в плане, передаёт
  ли клиент или сервер резолвит из политики PR-сессии.

**Пустой проект:** скилы зовут тулы без `project` → старое глобальное поведение (с предупреждением
от configure-review).

### 4. Связи (TASK_LINK) и purge

**Чтение** — см. п. 3 (`task_context` обходит только своих соседей).

**Запись (чистота):** scoped-синк создаёт TASK_LINK только на ключи своего проекта. «Свой проект» —
по префиксу ключа (yougile project code, напр. `PRI`; youtrack short name, напр. `TES`). Источник
префикса — `RawTask.project_code` (напр. `PRI-5` → `PRI`) либо сам `project`-идентификатор; точный
источник фиксируется в плане. Правило применяется и к related-ключам из описания (`key_pattern`), и к
явным board-links (`raw.links`).

**Purge** (`sync.py:96-106`): при scoped-синке `purge_orphaned` сверяет active_keys **только своего
проекта** против задач **только своего проекта** (`WHERE project = ...` в `purge_orphaned_tasks` /
`TaskStore.list_keys` / `TaskGraph.list_keys` / `keys_with_prs`). Не-scoped синк (`board_type=None`) →
purge по объединению как сейчас. Защита `keep_with_prs` (IMPLEMENTED_BY) сохраняется.

### 5. `configure-review` + конфиг репо + документация

**`configure-review` SKILL.md, шаг 5b** (`plugin/skills/configure-review/SKILL.md:104-113`):
- Вопрос «какой именно проект использует ЭТОТ репо?» → запись `task_board.project`.
- Предупреждение (RU): пустой `project` → и синк, и выдача/граф затянут ВСЕ проекты вперемешку
  (пример утечки `TES-1` ↔ `PRI`).
- yougile — название проекта (как в UI); youtrack — project short name. Напомнить грабли:
  `YOUTRACK_BASE_URL` обязан оканчиваться на `/api`.
- Существующий `task_board`-блок сохраняем verbatim, `project` добавляем/обновляем.

**`.review.yml` этого репо:** прописать `task_board.project` (yougile, проект `PRI`) — чинит локальную
утечку `TES-1`.

**`CLAUDE.md`:** уточнить раздел про `task_board` и инвариант «задачи глобальны»: хранилище
глобально, но **выдача и синк скоупятся по `project` из `.review.yml`** (пусто = всё).

## Тестирование (TDD)

- **`tests/tasks/test_sync.py`**: scoped-синк итерирует только один тип; теги `project` проставлены;
  purge не трогает чужой проект; `board_type=None` → старое поведение (все провайдеры); авто-бэкфилл
  на skip-ветке тегирует существующие.
- **`tests/tasks/boards/`**: `iter_raw`/`normalize` штампуют `project`; related-линки/board-links
  чужого префикса не создаются.
- **`tests/tasks/` (store/graph)**: `search`/`get_task`/`task_context` с `project` фильтруют; без
  `project` — back-compat; `task_context` отсекает чужих соседей и стабы.
- **`tests/policy/`**: `task_board.project` парсится из `.review.yml` и доезжает в `policy.task_board`.
- **`tests/skills/`**: guard — `configure-review` спрашивает про проект и пишет `task_board.project`;
  solve/sync-скилы прокидывают `project`.
- **integration** (`-m integration`, живые PG+Neo4j): полный цикл scoped sync→search→context на двух
  проектах не смешивает.
- Миграция `ADD COLUMN IF NOT EXISTS` идемпотентна.

## Затронутые файлы (карта)

| Файл | Изменение |
|---|---|
| `reviewer/index/schema.sql` | `ALTER TABLE tasks ADD COLUMN project` |
| `reviewer/tasks/store.py` | `TaskRow.project`; `upsert_task`/`get_task`/`search`/`list_keys` — колонка+фильтр |
| `reviewer/tasks/graph.py` | `:Task.project`; `upsert_task`/`task_context`/`list_keys`/`keys_with_prs` — проперти+фильтр |
| `reviewer/tasks/service.py` | `index_task`/`index_batch` несут `project`; `search_tasks`/`get_task_context`/`get_task`/`purge_orphaned_tasks` — `project`-параметр |
| `reviewer/tasks/sync.py` | `run`/`_sync_provider` — `board_type`-скоуп, авто-бэкфилл на skip, scoped purge |
| `reviewer/tasks/boards/base.py` | `RawTask.project` |
| `reviewer/tasks/boards/yougile.py` | штамп `project` в `iter_raw`/`normalize` + фильтр кросс-проект линков |
| `reviewer/tasks/boards/youtrack.py` | штамп `project` + фильтр кросс-проект линков |
| `reviewer/mcp/service.py` | `sync_board(board_type)`; read-тулы `project`-параметр |
| `reviewer/entrypoints/mcp_server.py` | сигнатуры тулов: `board_type` / `project` |
| `plugin/skills/configure-review/SKILL.md` | шаг 5b: вопрос про проект + предупреждение |
| `plugin/skills/solve-task/SKILL.md` | проброс `project` в read-тулы + scoped preflight sync |
| `plugin/skills/sync-tasks/SKILL.md` | `sync_board(board_type, board)` из `.review.yml` |
| `plugin/skills/{ask,pr-walkthrough,review-pr}/...` | проброс `project` в task-чтение |
| `.review.yml` | `task_board.project: PRI` |
| `CLAUDE.md` | уточнение инварианта скоупа задач |

## Открытые вопросы для плана (не блокеры)

- Точный источник «префикса проекта» для фильтра кросс-проект линков (`RawTask.project_code` vs
  `project`-идентификатор).
- review-pr: клиент передаёт `project` или сервер резолвит из `policy.task_board` PR-сессии.
- yougile: канонизация `project` = project title (а не board title) при штампе.

## Контекст/ограничения

- Индекс `dev` свежий (переиндексирован в этой сессии @ `08e407d`, граф SCIP).
- Язык проекта — русский. Коммиты — Conventional Commits на русском, без self-attribution.
- Грабли youtrack: `YOUTRACK_BASE_URL` обязан оканчиваться на `/api`.
- Связанные задачи: ID-140 (server-side ETL — база, которую расширяем), ID-168 (configure-review),
  ID-133 (repo-скоуп VCS через `repo_vcs` — паттерн-образец).
