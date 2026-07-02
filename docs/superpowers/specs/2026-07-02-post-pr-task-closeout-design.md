# Дизайн — Post-PR task closeout (finish_task)

- **Дата:** 2026-07-02
- **Статус:** утверждается
- **Бриф:** `docs/superpowers/briefs/2026-07-02-post-pr-task-closeout.md`

## 1. Цель

После того как задача решена и **создан PR**, плагин rag-reviewer **предлагает закрыть задачу на
доске**: дописать ссылку на PR в описание, пометить выполненной, опционально — добавить заметку.
Так, чтобы `updated_at`/last-modified задачи вырос → инкрементальный синк reviewer **переиндексировал
обновлённую версию** (done-статус + PR-ссылка/ребро). Должно работать **во всех клиентах** (Claude
Code, Cursor, Codex, VS Code и т.п.), не только в Claude Code.

### Факты про «вотермарку» (сверено с кодом)

Синк-вотермарка = per-`(board_type, board)` timestamp-курсор `tasks:<type>:<board>` в `index_meta`
(`reviewer/tasks/sync.py:26,45,68`). Задача переиндексируется, только если `raw.timestamp > cursor`.

- **YouGile:** watermark = поле `timestamp` задачи (`reviewer/tasks/boards/yougile.py:175`,
  last-modified epoch ms). Любой `PATCH` двигает его → **«пробел в названии» НЕ нужен**: запись
  PR-ссылки в описание + `completed:true` уже двигают `timestamp`.
- **YouTrack:** watermark = поле `updated` (`reviewer/tasks/boards/youtrack.py:82`). Любой `PATCH`
  описания / команда State двигают `updated` → **«редактирование описания снимает вотермарку» — да**.

## 2. Не-цели (YAGNI)

- Не переносим задачу в «Done»-колонку YouGile (хрупкий resolve id колонки) — используем булев `completed`.
- Не строим клиентскую запись через board-MCP (не переносимо, у YouTrack board-MCP нет).
- Не добавляем PostToolUse-хук авто-триггера (механизм только Claude Code — ломает кросс-клиентность).
- Не трогаем задачи без ключа / board-less прогоны (graceful no-op).

## 3. Архитектура (units)

### 3.1 Write-метод на `TaskBoardProvider`

Провайдеры сейчас read-only (`iter_raw`/`normalize`/`close`). Добавляем **первый write**:

```python
def finish(self, key: str, pr_url: str, *, note: str | None,
           mark_done: bool, done_state: str | None) -> dict:
    """Закрыть задачу на доске: пометить done + дописать PR-ссылку (+ note).
    Идемпотентно: PR-URL уже в описании → не дублируем; уже done → флаг не трогаем.
    Возвращает {key, board_id, done_set, pr_link_added, already_closed, warnings}."""
```

- **YougileBoard.finish** (`reviewer/tasks/boards/yougile.py`):
  1. `GET /tasks/{key}` → резолв кода (`PRI-N`/`ID-N`/uuid) в объект задачи + `id` (uuid) + текущие
     `description`, `completed`.
  2. Собрать новое описание: если `pr_url` **не** входит в текущее description — дописать блок
     `PR: <pr_url>` (+ `note`); иначе оставить как есть (`pr_link_added=False`).
  3. `PATCH /tasks/{uuid}` с `{completed: true (если mark_done и не был), description: <new>}`.
     Пустой набор изменений (уже done и PR-ссылка есть) → PATCH не шлём (`already_closed=True`).
  `done_state` игнорируется (у YouGile булев флаг).

- **YouTrackBoard.finish** (`reviewer/tasks/boards/youtrack.py`):
  1. `GET /issues/{key}?fields=description` → текущее описание.
  2. `PATCH /issues/{key}` с `{description: <new>}` (тот же idempotent-append PR-ссылки).
  3. `mark_done` → `POST /api/commands` `{query: "State <done_state or 'Fixed'>",
     issues:[{idReadable:key}]}` — YouTrack сам резолвит значение в рамках проекта.
     Ошибка команды (нет такого State) → `warnings`, не валит запись описания (fail-soft).

Оба провайдера используют уже существующий httpx-клиент с env-кредами (`Authorization: Bearer …`).

### 3.2 `normalize_yougile`: `completed` → status

`RawTask` получает поле `completed: bool` (`reviewer/tasks/boards/base.py`); `iter_raw` YouGile
прокидывает `t.get("completed", False)`. `normalize_yougile` (`yougile.py:63`): если `completed` →
`status = "done"` (иначе — имя колонки как сейчас). Так reviewer-стор после ре-синка видит «done»,
а не только имя колонки. (YouTrack уже отдаёт `status` из State — правки не нужны.)

### 3.3 MCP-тул `finish_task`

В `MCPReviewService` (`reviewer/mcp/`) + регистрация в `reviewer/entrypoints/mcp_server.py` рядом с
`sync_board`:

```python
def finish_task(key: str, pr_url: str, note: str | None = None,
                mark_done: bool = True, board_type: str | None = None,
                project: str | None = None, done_state: str | None = None) -> dict:
    """Закрыть задачу на доске (server-side write). Резолвит провайдера по board_type
    (иначе — единственный настроенный), зовёт provider.finish, fail-soft.
    Креды берутся из env (board_creds); наружу не отдаются. Портируется во все клиенты."""
```

Сервер репо-агностичен: `board_type`/`project`/`done_state` приходят от клиента (из `.review.yml`).
Резолв провайдера — по `board_type`; если не задан и настроен ровно один тип — берётся он, иначе
warning. Возвращает результат `provider.finish` + эхо `key`/`board_type`.

### 3.4 Скилл `plugin/skills/finish-task/SKILL.md` → `/reviewer_finish-task`

Тонкий клиентский триггер. Шаги:
1. **Config** — `task_board` из `.review.yml` репо (type/project/`done_state`), фолбэк
   `get_board_config()`. Нет доски / board-less → **graceful no-op** («задача не привязана к доске»).
2. **Resolve key** — по порядку: (a) `git branch --show-current` → `key_pattern`; (b) последний
   бриф `docs/superpowers/briefs/*<key>*`; (c) тело/заголовок PR; (d) спросить пользователя. Нет
   ключа → no-op.
3. **Resolve pr_url** — `gh pr view --json url` (или `glab`), иначе спросить.
4. **Offer + confirm** — показать что впишет (PR-ссылка + пометка done + опц. заметка), спросить про
   опциональную заметку, дождаться подтверждения. **Никогда не писать молча.**
5. **Write** — `finish_task(key, pr_url, note, board_type, project, done_state)`.
6. **Re-index** — `sync_board(board=project, board_type=board_type)` (инкрементально) → закрытая
   задача переиндексируется (её timestamp/updated теперь > cursor).
7. **Report** — что записано + результат ре-синка; при `already_closed`/no-op — сообщить честно.

### 3.5 Указатель из `solve-task`

В хвост хэндоффа `solve-task` (Step 5) и в скелет брифа — строка: «Когда PR создан → закрой задачу
скиллом `/reviewer_finish-task`». Так «плагин сам предлагает» переносимо (без CC-хука).

## 4. Поток данных

```
/reviewer_finish-task
  → .review.yml: {type, project, done_state}     (fallback get_board_config; нет → no-op)
  → resolve key: branch → brief → PR body → ask   (нет → no-op)
  → resolve pr_url: gh pr view --json url          (нет → ask)
  → offer+confirm (+опц. note)                     (никогда молча)
  → finish_task(key, pr_url, note, board_type, project, done_state)
       server (creds из env):
         YouGile:  GET /tasks/{key}→uuid; PATCH {completed:true, description:+PR}   # timestamp++
         YouTrack: PATCH /issues/{key}{description:+PR}; POST /commands "State Fixed" # updated++
         idempotent: PR-URL уже в описании → не дублировать; уже done → no-op
  → sync_board(project, board_type)                # ре-индекс (raw.timestamp/updated > cursor)
  → report
```

## 5. Конфиг (`.review.yml`, блок `task_board`)

Новый опциональный ключ:
```yaml
task_board:
  done_state: Fixed   # YouTrack: целевое значение State для «выполнено» (дефолт Fixed). YouGile игнорирует.
```
Читается клиентским скиллом и передаётся в `finish_task`. Сервер репо-агностичен, значения не хранит.

## 6. Идемпотентность и границы

- **Дубли PR-ссылки:** проверка вхождения `pr_url` в текущее описание (без инъекции маркеров) →
  повтор не дописывает. `completed`/State уже done → флаг/команда не шлются.
- **board-less / нет ключа:** graceful no-op с сообщением, exit 0.
- **Confirm-before-write:** доска — внешний side effect → offer + подтверждение, опц. заметку спросить.
- **Fail-soft:** доска недоступна / ключ не резолвится / команда State падает → отчёт + warnings,
  без краха (PR уже создан).

## 7. Креды, безопасность, инвариант

Write-креды — те же env, что у синка (`YOUGILE_API_KEY`/`YOUTRACK_TOKEN`), `board_config()` их не
отдаёт (инвариант цел). Запись — **server-side MCP** → одинаково во всех клиентах (ставится через
`install.py`). Это **расширяет** разворот инварианта «reviewer Python не трогает доску»: теперь
Python пишет в доску не только болк-синком, но и одиночным `finish_task`. Фиксируем в CLAUDE.md.

## 8. Тестирование

### 8.1 Unit (фейки/моки, без сети)
- `tests/tasks`: `YougileBoard.finish` (мок httpx: GET→uuid, payload PATCH, idempotent-skip при
  наличии URL, no-op при уже-done); `YouTrackBoard.finish` (мок: PATCH description + POST /commands,
  fail-soft при ошибке команды); `normalize_yougile` `completed→status="done"`.
- `tests/mcp`: `finish_task`-тул на фейк-провайдере — резолв board_type/project/done_state, no-op
  board-less/без ключа, проброс результата.
- `tests/skills`: guard `finish-task` SKILL.md (зовёт `finish_task`, confirm-before-write, no-op
  board-less, resolve-key порядок) + `solve-task` ссылается на `finish-task`.

### 8.2 Live acceptance (обязательно, на выбрасываемой задаче) — обе доски
Прогнать на **YouGile (проект PRI, текущее репо)** и **YouTrack (проект TES, тестовое репо)**:
```
Precondition: configured_board_types содержит оба типа (env: YOUGILE_API_KEY, YOUTRACK_TOKEN).
1. get_task(key) до          → status ≠ done, PR-ссылки в описании нет
2. finish_task(key, pr_url)  → доска: completed/State=done + "PR: <url>" в описании
3. sync_board(project,type)  → changed ≥ 1 (timestamp/updated > cursor)
4. get_task(key) после       → status="done" И описание содержит PR-ссылку   ← «стор сохранил обновление»
5. get_task_context(key)     → PR-ребро (extract_pr_refs из описания)
6. повторный finish_task     → already_closed/no-op, дублей PR-ссылки нет
```

## 9. Риски / открытые вопросы

- **YouTrack State-имя:** `done_state` по умолчанию `Fixed` может не существовать в проекте TES →
  проверить на live-тесте, при необходимости выставить корректное значение в `.review.yml` тестового
  репо; ошибка команды fail-soft (описание+PR всё равно записываются, задача переиндексируется).
- **YouGile resolve кода:** `GET /tasks/{PRI-N}` должен принимать проектный код (плейбук утверждает,
  что YouGile резолвит три формы id) — подтвердить на live-тесте.
- **Единственный настроенный тип на сервере vs явный board_type:** скилл всегда передаёт `board_type`
  из `.review.yml`, поэтому неоднозначность не возникает в штатном пути.
