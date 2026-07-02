# Дизайн — Конфигурируемая done-цель на доске (configurable done target)

- **Дата:** 2026-07-02
- **Статус:** утверждается
- **Поверх:** фичи `finish_task` (rag-reviewer 0.2.21) — расширяет её.

## 1. Цель

`finish_task` (закрытие задачи после PR) должен уметь переводить задачу в **правильную**
«выполнено»-ячейку доски, имя которой задаётся per-repo в `.review.yml`:

- **YouTrack:** доска может быть построена не на дефолтном поле `State`, а на кастомном (напр.
  `Stage` в проекте TES). Сейчас имя поля зашито `State` — и в записи (команда `finish`), и в
  чтении (`_state_of` в `normalize`), поэтому для Stage-проектов `finish` даёт 400 «Unknown field
  State», а `get_task` отдаёт `status: null`. Нужен конфиг имени поля, действующий на **обе**
  стороны.
- **YouGile:** сейчас `finish` ставит только булев `completed:true` и не переносит задачу в
  колонку. Нужна опция «перетянуть в заданную done-колонку» (+ `completed:true`).

Выявлено live-acceptance-прогоном 0.2.21 (YouGile — полностью зелёно; YouTrack TES — запись
PR-ссылки и re-index работают, но флип статуса упал на кастом-поле `Stage`).

## 2. Не-цели (YAGNI)

- Не поддерживаем несколько done-полей/значений и произвольные переходы состояний — только одну
  done-цель.
- Не резолвим «в работе»/промежуточные колонки, не двигаем задачу на других этапах.
- Не парсим `.review.yml` на сервере: конфиг читает клиентский скилл и передаёт параметрами (сервер
  репо-агностичен) — как уже сделано с `done_state`.
- Секретов в `.review.yml` по-прежнему нет.

## 3. Конфиг (`.review.yml`, блок `task_board`)

Все ключи опциональны; `.review.yml` per-repo, у репо ровно одна доска → используются только ключи
своей доски.

```yaml
task_board:
  # YouTrack: кастом-поле, на котором построена доска (дефолт State). Управляет И чтением
  # статуса (normalize/_state_of), И командой перевода в finish. YouGile игнорирует.
  status_field: Stage
  done_state: Готово       # YouTrack: целевое значение status_field на finish (дефолт Fixed)
  # YouGile: колонка, в которую перетянуть задачу на finish (+ completed:true).
  # Не задана → как сейчас (только completed:true). YouTrack игнорирует.
  done_column: Готово
```

`done_state` — существующий ключ (значение); `status_field` и `done_column` — новые.

## 4. Компоненты (units)

### 4.1 Провайдер несёт `status_field`

`make_board_provider(settings, type_, *, status_field: str | None = None)` пробрасывает
`status_field` в конструктор `YouTrackBoard(status_field=status_field or "State")` → `self._status_field`.
YouGile его игнорирует (у него статус = колонка). `make_board_providers` (мульти-синк без per-repo
конфига) зовёт с дефолтом `State`.

### 4.2 YouTrack: чтение + запись через `self._status_field`

- **Чтение:** `_state_of(issue, field="State")` — параметризуется; `iter_raw` зовёт
  `_state_of(issue, self._status_field)`. `customFields(name,value(name))` уже тянет ВСЕ поля →
  доп. запросов нет. Так `RawTask.status` = значение нужного поля (для TES — `Готово`), а не null.
- **Запись:** `finish` шлёт команду `f"{self._status_field} {{{state}}}"` → `Stage {Готово}`
  (скобки — анти-DSL-инъекция, уже есть; `state` санитизируется как сейчас). Ошибка команды
  (нет поля/значения) → `warnings`, fail-soft (описание+PR всё равно записаны).
- Конструктор `YouTrackBoard.__init__` получает `status_field: str = "State"`, хранит `self._status_field`.

### 4.3 YouGile: перенос в колонку + completed

`YougileBoard.finish` получает новый параметр `done_column: str | None = None`:
1. Как сейчас: `GET /tasks/{key}` → uuid, description, completed; идемпотентный append PR-ссылки.
2. Если `done_column` задан и задача ещё не в ней: резолв id колонки — `GET /columns/{task.columnId}`
   → `boardId`; `GET /columns` c `{"boardId": boardId, "title": done_column}` → id целевой колонки
   (match по точному `title`). Не найдено → `warnings`, колонку не трогаем (fail-soft).
3. `PUT /tasks/{uuid}` c `{completed: true (если mark_done и не был), columnId: <target> (если резолвнут
   и отличается), description: <new если PR добавлен>}`. Пустой payload → PATCH/PUT не шлём (`already_closed`).
Возврат расширяется полем `column_moved: bool` (в дополнение к текущим `done_set`/`pr_link_added`/
`already_closed`/`warnings`).

### 4.4 MCP-тул `finish_task` + `sync_board`

- `finish_task(key, pr_url, note=None, mark_done=True, board_type=None, done_state=None,
  status_field=None, done_column=None)` — новые проброс-параметры `status_field`, `done_column`;
  `make_board_provider(settings, board_type, status_field=status_field)`, затем
  `provider.finish(..., done_state=done_state, done_column=done_column)`. YouGile-провайдер игнорирует
  `status_field`; YouTrack-провайдер игнорирует `done_column`.
- `sync_board(board=None, board_type=None, limit=None, purge_orphaned=False, keep_with_prs=True,
  status_field=None)` — новый параметр `status_field` пробрасывается в `make_board_provider`, чтобы
  normalize при синке читал верное поле → стор после синка показывает реальный статус.

### 4.5 Клиентские скилы

`finish-task`, `sync-tasks`, `solve-task` (preflight `sync_board`) читают `task_board.status_field`
и `task_board.done_column` из `.review.yml` (там же, где уже читают `type`/`project`/`done_state`) и
передают в соответствующие тулы. Нет блока/ключей → передают `null` (дефолты сервера: `State`, без
переноса колонки).

## 5. Поток данных

```
ЗАПИСЬ (finish):
  .review.yml {status_field, done_state, done_column}
  → finish_task(key, pr_url, board_type, done_state, status_field, done_column)
      make_board_provider(status_field) → provider
      YouTrack: POST /issues (desc+PR); POST /commands "{status_field} {done_state}"   # Stage {Готово}
      YouGile:  GET /tasks; резолв columnId по done_column; PUT {completed:true, columnId, description}

ЧТЕНИЕ (sync → store):
  .review.yml {status_field}
  → sync_board(board_type, board, status_field)
      make_board_provider(status_field) → provider
      iter_raw: _state_of(issue, status_field) → RawTask.status = "Готово"           # больше не null
      normalize → store.status                                                        # стор = done-статус
```

## 6. Семантика статуса в сторе

- **YouTrack:** стор.status = значение `status_field` (напр. `Готово`) — у YouTrack нет булева done.
- **YouGile:** `normalize_yougile` уже мапит `completed → "done"` (canonical), поэтому стор.status =
  `"done"` (не имя колонки), даже после переноса в колонку «Готово». Колонка обновляется для доски/людей;
  каноничный done-статус в сторе — `"done"`. **Открытый вопрос (см. §9):** оставить canonical `"done"`
  или показывать имя колонки; дефолт — `"done"` (уже отгружено в 0.2.21, кросс-бордово единообразно).

## 7. Обратная совместимость и fail-soft

- Дефолты: `status_field="State"`, `done_column=None` → поведение 0.2.21 без изменений.
- Все новые ветки fail-soft: поле/значение/колонка не резолвятся → `warnings`, без краха; PR-ссылка и
  доступные части записи всё равно применяются.
- Инвариант «креды только в env» цел: новые ключи — не секреты.

## 8. Тестирование

### 8.1 Unit (моки httpx, без сети)
- `youtrack`: `_state_of(issue, "Stage")` читает кастом-поле; `finish` c `status_field="Stage"` шлёт
  `Stage {Готово}`; дефолт остаётся `State`.
- `yougile`: `finish(done_column="Готово")` резолвит колонку (GET /columns/{cur}→board, GET /columns→
  match title) и шлёт PUT c `columnId`+`completed`; колонка не найдена → warning, `completed` всё равно;
  `done_column=None` → поведение как сейчас (только completed).
- `boards/__init__`: `make_board_provider(status_field=...)` доходит до YouTrack-конструктора.
- `mcp`: `finish_task`/`sync_board` пробрасывают `status_field`/`done_column` в провайдер (monkeypatch).
- `skills`: guard — `finish-task`/`sync-tasks`/`solve-task` упоминают `status_field`/`done_column`.

### 8.2 Live acceptance (обе доски) — повтор round-trip
- **YouTrack TES** (`status_field: Stage`, `done_state: Готово`): `finish_task` → на доске `Stage=Готово`;
  `sync_board(status_field=Stage)` → `get_task` показывает `status="Готово"` (не null) + PR-ссылка;
  `get_task_context` → PR-ребро; повторный `finish_task` — без дублей PR.
- **YouGile PRI** (`done_column: Готово`): `finish_task` → задача в колонке «Готово» + `completed`;
  `sync_board` → стор `status="done"` + PR-ссылка; идемпотентность.

## 9. Риски / открытые вопросы

- **Резолв колонки YouGile** — 2 доп. GET-запроса в `finish` (по колонке → доска → колонки). Приемлемо
  (finish не hot-path). Названия колонок неуникальны между досками, но резолвим в пределах доски задачи.
- **Семантика YouGile-статуса (§6):** canonical `"done"` vs имя колонки — дефолт `"done"`; подтвердить на
  ревью спеки.
- **Мульти-тип sync:** `status_field` — один параметр; при синке нескольких типов применяется к YouTrack,
  YouGile игнорирует. Клиент скоупит синк своим `board_type` → неоднозначности нет в штатном пути.
- **Проброс `status_field` в несколько скилов** (finish-task/sync-tasks/solve-task) — расширение уже
  существующего чтения `task_board`; риск — забыть один скилл (покрывается guard-тестами §8.1).
