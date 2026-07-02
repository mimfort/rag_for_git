# Дизайн — Server-side discovery done-цели доски (PRI-205)

- **Дата:** 2026-07-02
- **Статус:** утверждён
- **Задача:** PRI-205 (alias ID-205)
- **Поверх:** фичи *configurable-done-target* (rag-reviewer 0.2.23) — расширяет её.
- **Бриф:** `docs/superpowers/briefs/2026-07-02-PRI-205-server-side-done-target-discovery.md`

## 1. Цель

Done-цель для `finish_task` (`done_column` у YouGile; `status_field`+`done_state` у YouTrack) сейчас
заполняется в `.review.yml` **вручную**, подсматривая точные названия в UI доски. Для YouTrack
особенно больно: на клиенте нет YouTrack-MCP, а у reviewer-сервера нет тула отдать поля/значения.
`configure-review` в YouGile-ветке к тому же опирается на **сторонний** `mcp__yougile__get_columns`.

Новый **read-only server-side** reviewer MCP-тул `get_board_targets(board_type, project)` возвращает
кандидатов done-цели, используя REST-креды сервера из env. `configure-review` показывает pick-list
вместо ручного ввода; `finish-task` явно называет резолвнутую цель в подтверждении. Работает и для
YouGile, и для YouTrack — **без** board-MCP на клиенте (симметрично `sync_board`/`finish_task`).

## 2. Не-цели (YAGNI)

- Не двигаем задачу и не резолвим промежуточные («в работе») колонки/состояния — только discovery
  кандидатов done-цели. Перенос/`completed` по-прежнему делает `finish_task` с подтверждением.
- Сервер `.review.yml` не парсит: `board_type`+`project` приходят параметрами тула (репо-агностичность).
- Секретов в `.review.yml` нет; тул креды не возвращает.
- Не кэшируем discovery на сервере (finish/configure — не hot-path).

## 3. Интерфейс провайдера (`reviewer/tasks/boards/base.py`)

**Решение А — один board-агностичный метод Protocol** (вместо раздельных `list_columns`/
`list_status_fields` из текста задачи). Сервисный слой остаётся board-агностичным
(`provider.list_done_targets(project)`, без `isinstance`); каждый провайдер владеет своей
нормализацией — ровно философия `base.py`, и точно повторяет приём `finish()` (единая сигнатура,
доска-специфичное поведение внутри).

```python
def list_done_targets(self, project: str | None) -> dict:
    """Кандидаты done-цели доски (read-only, fail-soft, НИКОГДА не бросает).

    YouGile → {"columns": [{"title", "id", "board_id", "board_title"}], "warnings": [...]}
    YouTrack → {"status_fields": [{"field", "values": [...], "$type"?}],
                "source": "admin"|"sample", "warnings": [...]}

    Ошибка/нет прав/сеть → пустой список + warnings (скилл откатывается на ручной ввод)."""
    ...
```

Добавляется в `TaskBoardProvider` (Protocol) рядом с `finish`. Оба провайдера реализуют метод.

## 4. Компоненты (units)

### 4.1 YouGile — `YougileBoard.list_done_targets(project)`

Скоуп по `project` (код-префикс, напр. `PRI`) решается через фактически используемые доски:

1. Выбрать небольшую пачку задач проекта — как `iter_raw` с фильтром `project_prefix` (`base.py:17`),
   с внутренним лимитом (напр. `limit=200`, достаточно чтобы покрыть доски проекта); собрать
   `columnId` этих задач.
2. Резолв `columnId → boardId`: `GET /columns/{id}` (переиспользует приём `_resolve_column_id`,
   `yougile.py:251`) — собрать distinct `board_id` (+ `board_title` через `GET /boards/{id}` или из
   уже полученных данных).
3. Перечислить **все** колонки этих досок: `_get_all("/columns", {"boardId": …})` (`yougile.py:139`).
   Вернуть `columns: [{title, id, board_id, board_title}]` — `board_id`+`board_title` дают pick-list'у
   различить одноимённые колонки между досками (риск неуникальности).

Пустой `project` → перечислить колонки всех досок всех проектов (`/projects` → `/boards` → `/columns`,
как `iter_raw:157-160`). Любой сбой любого шага → warning в список, продолжаем с тем, что собрали.

### 4.2 YouTrack — `YouTrackBoard.list_done_targets(project)` (try-admin → fallback)

**Решение по discovery — «Try-admin → fallback агрегация»** (максимум полноты, не ломается на
урезанном токене; fail-soft на обоих уровнях):

1. **Try (admin):** резолв внутреннего id проекта по shortName —
   `GET /admin/projects?fields=id,shortName,name` (или `query=<project>`), match по `shortName == project`.
   Затем `GET /admin/projects/{id}/customFields?fields=field(name,$type),bundle(values(name,$type))`
   — вернуть поля-состояния/enum (те, у кого есть `bundle`) + значения. `source="admin"`.
2. **Fallback (403 / нет прав / любая ошибка admin-уровня):**
   `GET /issues?query=project:{project}&fields=customFields(name,value(name),$type)` (тот же `_FIELDS`-путь,
   что у синка — `youtrack.py:24`, `_state_of` семантика), с внутренним `$top`-лимитом; собрать
   **distinct** `value.name` по каждому имени поля (+ `$type` элемента). `source="sample"`.
3. Полный провал обоих → `{status_fields: [], source: "sample", warnings: [...]}`.

Пустой `project` → admin: все проекты пропустить нельзя дёшево → fallback на выборку задач без
`project:`-фильтра (или warning + пусто). Возвращаем только поля с bundle-значениями (кандидаты
статуса), не все кастом-поля.

### 4.3 Фабрика провайдера (`reviewer/tasks/boards/__init__.py`)

Без изменений сигнатуры: `make_board_provider(settings, board_type, status_field=...)` уже строит
провайдер из `board_creds` (env). `get_board_targets` идёт тем же путём, что `finish_task:342`.
`status_field` для discovery не нужен (тул возвращает **кандидатов** для выбора `status_field`) →
зовём `make_board_provider(settings, board_type)` с дефолтом.

### 4.4 MCP-тул `get_board_targets`

**Решение Б — имя `get_board_targets`** (не `describe_board_targets`) — консистентно с
`get_board_config`/`get_task`/`get_task_context` (read-тулы = `get_`).

- `reviewer/mcp/service.py`: метод `get_board_targets(self, board_type, project=None) -> dict`:
  - резолв `board_type` через `configured_board_types()` (паттерн `finish_task:332-341`: если один
    тип — берём его; ноль/несколько без явного — error-summary);
  - `provider = make_board_provider(self.settings, board_type)`; `None` → error-summary;
  - `provider.list_done_targets(project)` в try/except (fail-soft), `provider.close()` в finally;
  - возврат `{"board_type", "project", **targets}` (targets = `columns`/`status_fields`+`source`+`warnings`).
    **Креды никогда не в возврате.**
- `reviewer/entrypoints/mcp_server.py`: тонкий `@mcp.tool() get_board_targets(board_type, project=None)`
  → делегат в `service.get_board_targets` (паттерн `:120`/`:166`). Docstring — назначение, форма
  возврата, «credentials never returned; fail-soft».

### 4.5 Скилл `configure-review`

`plugin/skills/configure-review/SKILL.md`, step 5b «finish-task done target» (`:125-137`):
- Перед вводом done-цели — вызвать `get_board_targets(type, project)` (reviewer MCP, best-effort).
- **yougile:** показать pick-list колонок (`title`, при неоднозначности — `board_title`); пользователь
  выбирает `done_column`. Пусто/ошибка/тул отсутствует (старый деплой) → **ask-фолбэк** (текущий ручной
  ввод) — шаблон best-effort+fallback из `count_tasks` (context-limits, `SKILL.md:178-181`).
- **youtrack:** показать pick-list полей статуса + их значений; пользователь выбирает `status_field`
  и `done_state`. Пусто/ошибка → ask-фолбэк.
- **Убрать** зависимость от клиентского `mcp__yougile__get_columns` (`:131-132`) — заменить на server-тул.
- Всё остальное поведение скилла (merge, «never write credentials», ask-per-candidate) не трогаем.

### 4.6 Скилл `finish-task`

`plugin/skills/finish-task/SKILL.md`, step 4 «Offer + confirm» (`:32-34`):
- В подтверждении **явно называть** резолвнутую done-цель: «перенесу задачу в колонку „Готово"
  + отмечу completed» (yougile) / «выставлю Stage = Готово» (youtrack) — из прочитанных
  `done_column`/`status_field`+`done_state`. Обобщённое «mark done» → конкретика.
- **НЕ регрессить** гейт подтверждения: перенос/отметка выполненной — только после явного согласия
  пользователя, даже если значения заданы в `.review.yml` (инвариант «Never write silently»).
- `finish-task` **не** зовёт `get_board_targets` (цель уже в `.review.yml`); тул нужен только
  configure-review на этапе заполнения `.review.yml`.

## 5. Поток данных

```
DISCOVERY (configure-review, этап заполнения .review.yml):
  configure-review читает task_board.{type, project} из .review.yml
  → get_board_targets(board_type, project)
      make_board_provider(settings, board_type)            # креды из env
      YouGile:  sample задач проекта → columnId → boardId → все колонки досок
                → {columns:[{title,id,board_id,board_title}]}
      YouTrack: try GET /admin/projects/{id}/customFields  → {status_fields, source:"admin"}
                except → GET /issues?query=project:… → distinct values → source:"sample"
  → pick-list → пользователь выбирает → запись done_column / status_field+done_state в .review.yml
                (ask-фолбэк при пустом/ошибке/отсутствии тула)

WRITE (finish-task, без изменений в проводке — только вординг подтверждения):
  .review.yml {done_column | status_field+done_state}
  → confirm: «перенесу в „Готово"+completed» / «выставлю Stage=Готово»  → finish_task(...)
```

## 6. Инварианты и fail-soft

- **Креды только в env** — не в `.review.yml`, не в возврате `get_board_targets`.
- **Discovery — только ЧТЕНИЕ** доски; перенос/`completed` по-прежнему через `finish_task` с
  подтверждением пользователя.
- **Repo-агностичность сервера** — `.review.yml` не парсится сервером; `board_type`+`project`
  приходят параметрами; клиент (configure-review) читает `.review.yml` и передаёт.
- **Fail-soft везде** — недоступна доска/креды/права/сеть → пустой список + warnings; скилл
  откатывается на ручной ввод (никогда не блокирует конфигурацию репо).
- **Обратная совместимость** — новый тул аддитивен; старый деплой без него → configure-review
  фолбэкает на ручной ввод (как сейчас).

## 7. Тестирование

### 7.1 Unit (моки httpx, без сети)
- **yougile** (`tests/tasks/boards/test_yougile_*`): `list_done_targets` резолвит доски из выборки
  задач и перечисляет их колонки (GET `/columns/{cur}`→board, `/columns?boardId`→список); скоуп по
  `project`; сбой шага → warning, частичный результат; пустой `project` → все доски.
- **youtrack** (`tests/tasks/boards/test_youtrack_*`): try-admin успех (`source="admin"`, все значения
  бандла); admin 403 → fallback-агрегация из выборки задач (`source="sample"`, distinct values);
  полный провал → `status_fields=[]`+warnings.
- **boards/__init__**: `make_board_provider(...)` → провайдер с методом `list_done_targets`.
- **mcp** (`tests/mcp/`): `get_board_targets` резолвит board_type, пробрасывает `project` в провайдер
  (monkeypatch), возврат НЕ содержит кредов; неизвестный/ненастроенный тип → error-summary.
- **skills** (`tests/skills/`): guard — `configure-review` упоминает `get_board_targets`/pick-list и
  НЕ ссылается на `mcp__yougile__get_columns`; `finish-task` step 4 называет конкретную done-цель.

### 7.2 Live acceptance (обе доски) — **вне сессии, после передеплоя**
- **YouGile PRI:** `get_board_targets("yougile","PRI")` → реальные колонки доски; configure-review
  без клиентского yougile-MCP предлагает список → выбор `done_column`.
- **YouTrack TES:** `get_board_targets("youtrack","TES")` → поля статуса (напр. `Stage`) + значения
  (напр. `Готово`); выбор `status_field`/`done_state`. Проверить оба пути (admin и fallback).

## 8. Границы (scope сессии)

**Решение В — в сессии: код+тесты (units 4.1–4.6, §7.1) + бамп версии `0.2.23`→`0.2.24`.**
Вне сессии (нужен передеплой, как с фичей-предшественником):
- PyPI-публикация `0.2.24`;
- `reviewer update` на деплое;
- live-приёмка §7.2 на обеих досках.

## 9. Риски / открытые вопросы

- **YouTrack admin API права** — токен может не иметь admin-доступа → покрыто fallback-агрегацией
  (§4.2); поле done может не всплыть в fallback, если ещё ни одна задача не в done-статусе (приемлемо —
  пользователь введёт вручную; отражено в warning).
- **Неуникальность YouGile-колонок** между досками проекта → возвращаем `board_id`+`board_title`,
  pick-list уточняет.
- **YouGile «project» ≠ объект YouGile-проекта** — `project` из `.review.yml` это код-префикс задач
  (`idTaskProject`), не id YouGile-проекта; поэтому доски находим через выборку задач проекта, а не
  через `/projects` по имени (§4.1).
- **Проброс в несколько мест** — тул трогает только configure-review; finish-task меняет лишь вординг.
  Риск забыть скилл минимален (покрывается guard-тестами §7.1).
