# PRI-207 — Self-healing meta-refresh при синке задач

**Дата:** 2026-07-03
**Задача:** [PRI-207](https://ru.yougile.com/team/686c049c8af8/#PRI-207)
**Бриф:** `docs/superpowers/briefs/2026-07-03-PRI-207-watermark-sync-no-project-backfill.md`

## Проблема

`search_tasks(project="PRI")` / `get_task(project="PRI")` видят ~3 из ~97 задач: колонка
`project` в таблице `tasks` пуста у 94 задач (`project=''` — 94, `PRI` — 3, `TES` — 1). Это **не
утечка скоупа** (SQL корректен: `store.py:197/123` `AND project = %(project)s`), а **незаполненная
колонка**.

Корень — watermark-гейт инкрементального синка. `SyncService._sync_provider` (`reviewer/tasks/sync.py:48`):

```python
if raw.timestamp <= cursor:
    unchanged += 1
    continue          # задача не доходит до normalize/index
```

Заполнение `project` добавлено в путь индексации в PRI-170 (`normalize_* → project_prefix`,
`index_task`/`index_batch` штампуют его на meta-only пути через `store.update_meta`). Но старые
задачи (timestamp ниже курсора) **никогда не доходят** до этого пути → `project` не
backfill-ится. `content_hash` не спасает: в него входит только текст (`task_content_hash(text)`),
не метаданные.

**Обобщение:** любое обогащение, добавленное в индексацию позже первого синка задачи, не доезжает
до старых задач. `project` — первый пойманный случай; фикс должен решать класс проблемы, а не
частный симптом.

## Ключевое наблюдение (обоснование дизайна)

`normalize()` провайдера **дорогой** (per-task REST): YouGile резолвит подзадачи (`GET /tasks/{sid}`)
и вложения (`GET /chats/.../messages`); YouTrack — аналогично. Поэтому «прогнать `normalize` для
всех enumerate-нутых на каждом синке» неприемлемо.

Но **дешёвые метаданные `RawTask` не требуют I/O.** Module-level `normalize_yougile` /
`normalize_youtrack` документированы как **чистые («Чистая: без I/O»)** — весь I/O вынесен в
обёртку `provider.normalize()`. Все нужные для backfill поля выводятся из `RawTask` без сети:

- `key = raw.key`
- `aliases = [raw.project_code]`
- `title = raw.title`
- `status = "done" if raw.completed else raw.status`
- `url = url_template.replace("{code}", raw.project_code)`
- `project = project_prefix(raw.project_code or key)`

Это ровно колонки, которые обновляет `TaskStore.update_meta(key, title, status, url, aliases,
project)`.

## Решение

Расщепить синк на два тракта:

1. **Дорогой (changed-only)** — без изменений: `timestamp > cursor` → `provider.normalize()` (REST)
   → `index_batch` (embed по `content_hash`).
2. **Дешёвый meta-refresh (all-enumerated)** — **новый**: `timestamp <= cursor` → извлечь дешёвые
   метаданные из `RawTask` (без I/O) → батчем обновить `tasks` (+ граф). Работает на **каждом**
   синке → `project` и любое дешёвое обогащение самозаполняются, без ручного force-режима.

Курсор продвигается по-прежнему только по `max_ts` — инкрементальность дорогого тракта (и его
экономия Voyage) сохранена; дешёвый тракт на курсор не влияет.

### Поток данных

```
iter_raw(board, limit) → per raw:
    timestamp >  cursor → normalize()      [REST]  → changed  → index_batch      [embed]   (как сейчас)
    timestamp <= cursor → normalize_meta() [pure]  → metas    → refresh_meta_batch [update_meta + graph]  (НОВОЕ)
после цикла: cursor ← max_ts (без изменений)
```

## Компоненты и интерфейсы

### 1. `TaskBoardProvider.normalize_meta(raw) -> dict` — новый метод Protocol
`reviewer/tasks/boards/base.py` + реализации в `yougile.py` / `youtrack.py`.

Делегирует в чистый module-level `normalize_*(raw, key_pattern, url_template)` с
`subtask_titles=None, attachments=None` → возвращает **полный** чистый TaskBrief dict, но **без
I/O** (criteria=[], attachments=[], links без title подзадач). `refresh_meta_batch` потребляет из
него только дешёвые поля (key, aliases, title, status, url, project) — остальное игнорируется.
Реализация в каждом провайдере — фактически одна строка (переиспользует существующую чистую
функцию).

### 2. `TaskStore.update_meta_batch(rows) -> None` — новый (executemany)
`reviewer/tasks/store.py`. Батч уже существующего `update_meta`:
`UPDATE tasks SET title=%s, status=%s, url=%s, aliases=%s, project=%s WHERE key=%s` через
`executemany`. Задача не в сторе → 0 строк (безопасный no-op: не создаёт неполных строк, не трогает
embedding). `update_meta` (единичный) остаётся для `index_task`.

### 3. `TaskService.refresh_meta_batch(metas) -> dict` — новый
`reviewer/tasks/service.py`. Для батча дешёвых dict:
- `store.update_meta_batch(...)` — обновить метаданные (fail-soft на весь батч);
- для каждой задачи `graph.upsert_task(key, aliases, title, status, url, project)` — заполнить
  `project` и на графовом узле `:Task` (нужно для scoped `get_task_context`); per-task fail-soft,
  как в `index_task`; при `graph is None` — пропуск с warning.

**Никогда не эмбедит и не роутится через `index_task`** — иначе задача, отсутствующая в сторе
(`existing_hash → None`), ушла бы в embed-путь. PR-автолинковка не выполняется (она только для
`embedded` changed-задач). Возвращает `{"meta_refreshed": n, "warnings": [...]}`.

### 4. `SyncService._sync_provider` — правка `reviewer/tasks/sync.py`
В цикле для unchanged (`raw.timestamp <= cursor`): вместо голого `continue` — собрать
`provider.normalize_meta(raw)` в список `meta_refresh`. После цикла (рядом с `index_batch(changed)`):
`self._tasks.refresh_meta_batch(meta_refresh)`. Добавить в per-provider summary поле
`meta_refreshed`; агрегировать в `run` и в `by_board`.

## Edge cases и что НЕ меняется

- **Курсор / инкрементальность** дорогого тракта — без изменений; дешёвый тракт курсор не двигает.
- **`limit` / partial обход** — meta-refresh идемпотентен, работает и на частичном обходе; purge
  по-прежнему пропускается при `limit`.
- **Задача не в сторе** (напр. ранее purged, но всё ещё на доске ниже курсора) — `update_meta`
  no-op (0 строк); meta-refresh её **не воскрешает** (осознанный gap: фикс про метаданные, не про
  re-add; re-add — это дорогой тракт при изменении задачи).
- **description / text / embedding** — не трогаются (только колонки `update_meta`); вектор остаётся
  консистентным тексту.
- **Стоимость** — N дешёвых `UPDATE` (executemany, один round-trip) + N граф-MERGE на синк
  (N = enumerated). Для ~96 задач пренебрежимо; ноль Voyage, ноль доп. board-REST.

## Тестирование

Все unit-тесты — на фейках (без реальных БД/сети), согласно соглашениям репозитория.

- **`normalize_meta` (оба провайдера):** корректные `project` / `url` / `status` из `RawTask`
  **без I/O** — fake-client, падающий на любой GET, доказывает отсутствие сети.
- **`TaskService.refresh_meta_batch`:** `store.update_meta_batch` вызван с `project`; граф
  `upsert_task` вызван с `project`; fail-soft (сбой стора/графа не валит); `graph is None` → warning;
  никакого embed-вызова.
- **`TaskStore.update_meta_batch`** (integration, `-m integration`): задача в сторе → `project`
  обновлён; задача не в сторе → 0 строк, ничего не создано.
- **`_sync_provider` / `SyncService.run`:** unchanged задачи попадают в meta-refresh; курсор ими не
  двигается; `meta_refreshed` в summary корректен; changed по-прежнему эмбедятся.
- **Правка существующего `test_watermark_skips_unchanged` (`tests/tasks/test_sync.py:69`):**
  семантика меняется — unchanged теперь meta-refreshатся (не эмбедятся). Обновить ассерты
  (`ts.indexed == [["ID-2"]]` сохраняется для embed; добавить проверку meta-refresh для ID-1).
- **`test_index_batch_stamps_project_on_meta_only` (`tests/tasks/test_service_batch.py:208`)** —
  остаётся без изменений (дорогой тракт).

## Rollout и acceptance

- **Требует деплоя** сервера с этим кодом. После деплоя следующий обычный `sync_board` (changed=0,
  все задачи ниже курсора) автоматически backfill-ит `project` у всех 94 задач через meta-refresh.
  Это снимает вопрос задачи «пишет ли задеплоенный сервер project вообще» — фикс включает PRI-170-
  логику `project_prefix` и гарантированно прогоняет её для всех enumerate-нутых на первом же синке.
- **Acceptance (из задачи):**
  - `search_tasks(project="PRI")` возвращает основной PRI-корпус (десятки задач), не 3.
  - Распределение `project` в `tasks`: ~все задачи с валидным кодом имеют непустой `project`, ноль
    пустых среди них.

## Не в скоупе (YAGNI)

- Отдельный force/full-флаг на `sync_board` (систему делает self-healing — ручной force не нужен).
- Re-add ранее purged задач (см. edge case).
- Оптимизация «UPDATE только при реальном отличии метаданных» (нужен доп. SELECT; 96 идемпотентных
  UPDATE и так дёшевы).
- Backfill дорогих полей (criteria/attachments/description) для старых задач — они восстановятся
  через дорогой тракт при следующем изменении задачи.
