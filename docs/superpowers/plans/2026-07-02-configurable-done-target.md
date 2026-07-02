# Configurable Done Target — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать «выполнено»-цель доски настраиваемой per-repo в `.review.yml`: имя YouTrack-поля статуса (`status_field`, действует на чтение И запись), значение (`done_state`), и YouGile-колонку для переноса (`done_column`).

**Architecture:** Провайдер YouTrack несёт `self._status_field` (дефолт `State`), используемый и `_state_of` (чтение статуса при синке), и командой `finish`. YouGile `finish` резолвит id done-колонки по title в пределах доски задачи и добавляет `columnId` в PUT. Параметры текут: `.review.yml` → клиентский скилл → MCP-тул (`finish_task`/`sync_board`) → `make_board_provider(status_field=)` / `SyncService.run(status_field=)` → провайдер. Всё fail-soft; сервер репо-агностичен (не парсит `.review.yml`).

**Tech Stack:** Python 3.11–3.13, httpx (REST), FastMCP, pytest (моки httpx, без сети).

## Global Constraints

- Язык проекта — русский: комментарии, докстринги, сообщения. Ответы пользователю по-русски.
- Коммиты: без self-attribution (никаких Co-Authored-By/Claude). Conventional Commits на русском (`feat(tasks): …`, `fix(mcp): …`).
- Ruff: line-length 100, target py311. `.venv/bin/ruff check .` на затронутых файлах чист.
- Дефолты сохраняют поведение 0.2.21: `status_field="State"`, `done_column=None` → без изменений.
- Fail-soft: запись в доску вторична к созданному PR; поле/значение/колонка не резолвятся → `warnings`, без краха.
- Креды только в env; `.review.yml` без секретов; `board_config()` креды не отдаёт.
- Код пишут Opus-субагенты; Fable не использовать.
- Деплой в конце — бамп версии `0.2.21` → `0.2.22` (`pyproject.toml`).

---

### Task 1: YouTrack — конфигурируемое поле статуса (чтение + запись) + Protocol `done_column`

**Files:**
- Modify: `reviewer/tasks/boards/base.py` (Protocol `finish` — параметр `done_column`)
- Modify: `reviewer/tasks/boards/youtrack.py` (`_state_of`, `_issue_to_raw`, `__init__`, `iter_raw`, `finish`, новый `set_status_field`)
- Test: `tests/tasks/boards/test_youtrack_finish.py`, `tests/tasks/boards/test_youtrack_normalize.py`

**Interfaces:**
- Produces:
  - `YouTrackBoard.__init__(*, token, base_url, key_pattern, status_field="State", attachment_max_bytes=..., attachment_timeout=..., attachment_store_chars=...)` → `self._status_field`
  - `YouTrackBoard.set_status_field(field: str | None) -> None` (переустановка для синка singleton-провайдера)
  - `YouTrackBoard.finish(key, pr_url, *, note=None, mark_done=True, done_state=None, done_column=None) -> dict` (команда использует `self._status_field`; `done_column` игнорируется)
  - `_state_of(issue: dict, field: str = "State") -> str | None`
  - Protocol `TaskBoardProvider.finish(..., done_column: str | None = None)`

- [ ] **Step 1: Написать падающие тесты (чтение кастом-поля + запись через status_field)**

Добавить в `tests/tasks/boards/test_youtrack_finish.py`:

```python
from reviewer.tasks.boards.youtrack import YouTrackBoard, _state_of


def test_state_of_reads_custom_field():
    issue = {"customFields": [{"name": "Stage", "value": {"name": "Готово"}}]}
    assert _state_of(issue, "Stage") == "Готово"
    assert _state_of(issue) is None  # дефолт State — поля нет


def test_youtrack_finish_uses_configured_status_field():
    b = _board({"/issues/TES-1": _Resp(200, {"description": ""})})
    b._status_field = "Stage"  # как выставит make_board_provider/set_status_field
    b.finish("TES-1", PR, done_state="Готово")
    cmd = next(c for c in b._client.calls if c[1] == "/commands")
    assert cmd[2]["query"] == "Stage {Готово}"


def test_youtrack_finish_status_field_injection_neutralized():
    b = _board({"/issues/TES-1": _Resp(200, {"description": ""})})
    b._status_field = "Stage} tag x {"  # попытка DSL-инъекции через имя поля
    b.finish("TES-1", PR, done_state="Готово")
    cmd = next(c for c in b._client.calls if c[1] == "/commands")
    assert "}" not in cmd[2]["query"][:-1]  # нет закрывающей скобки внутри
    assert cmd[2]["query"].endswith("{Готово}")


def test_youtrack_set_status_field_defaults_to_state():
    b = _board({"/issues/TES-1": _Resp(200, {"description": ""})})
    b.set_status_field(None)
    b.finish("TES-1", PR, done_state="Fixed")
    cmd = next(c for c in b._client.calls if c[1] == "/commands")
    assert cmd[2]["query"] == "State {Fixed}"
```

Примечание: helper `_board` в этом файле создаёт провайдер через `YouTrackBoard.__new__` (обходит httpx), поэтому `_status_field` не выставлен конструктором — тест `set_status_field`/присваивание выставляют его явно. Существующие тесты (`test_youtrack_finish_edits_desc_and_runs_state_command` и др.) вызывают `finish` без установки `_status_field`; чтобы они не падали на `AttributeError`, helper `_board` должен инициализировать дефолт — см. Step 3.

- [ ] **Step 2: Прогнать — падают**

Run: `.venv/bin/pytest tests/tasks/boards/test_youtrack_finish.py -q`
Expected: FAIL (`_state_of()` takes 1 arg; `finish` query == `State {...}` не `Stage {...}`; `set_status_field` не существует).

- [ ] **Step 3: Реализация — youtrack.py + base.py**

В `reviewer/tasks/boards/youtrack.py`:

`_state_of` (строка 32) — параметризовать имя поля:

```python
def _state_of(issue: dict, field: str = "State") -> str | None:
    """Статус задачи — кастом-поле `field` (дефолт «State»), его value.name."""
    for cf in issue.get("customFields") or []:
        if cf.get("name") == field:
            val = cf.get("value")
            return val.get("name") if isinstance(val, dict) else None
    return None
```

`_issue_to_raw` (строка 72) — принять и пробросить `status_field`:

```python
def _issue_to_raw(issue: dict, status_field: str = "State") -> RawTask:
    """YouTrack issue JSON → RawTask. Чистая: без I/O."""
    key = issue.get("idReadable", "")
    return RawTask(
        key=key,
        project_code=key,                       # один счётчик idReadable, второго кода нет
        title=issue.get("summary", "") or "",
        description=issue.get("description", "") or "",
        status=_state_of(issue, status_field),
        subtask_ids=[],
        timestamp=int(issue.get("updated", 0) or 0),
        links=_links_of(issue),
        attachments=_attachments_of(issue),
    )
```

`__init__` (строка 125) — добавить параметр `status_field` (перед attachment-параметрами) и сохранить:

```python
    def __init__(self, *, token: str, base_url: str, key_pattern: str,
                 status_field: str = "State",
                 attachment_max_bytes: int = 10 * 1024 * 1024,
                 attachment_timeout: float = 10.0,
                 attachment_store_chars: int = 200000) -> None:
```

В теле `__init__` (после `self._key_pattern = key_pattern`) добавить:

```python
        self._status_field = status_field or "State"
```

Новый метод сразу после `close` (после строки 150):

```python
    def set_status_field(self, field: str | None) -> None:
        """Переустановить имя поля статуса (per-repo из .review.yml) для синка.

        Провайдер синка — долгоживущий singleton; SyncService выставляет поле
        перед iter_raw и сбрасывает к «State» при отсутствии конфига.
        """
        self._status_field = field or "State"
```

`iter_raw` (строка 163) — пробросить поле:

```python
                yield _issue_to_raw(issue, self._status_field)
```

`finish` (строки 186-225) — сигнатура + команда через `self._status_field`:

Изменить сигнатуру (строка 186-187):

```python
    def finish(self, key: str, pr_url: str, *, note: str | None = None,
               mark_done: bool = True, done_state: str | None = None,
               done_column: str | None = None) -> dict:
```

Внутри блока `if mark_done:` (строки 209-220) заменить на:

```python
        if mark_done:
            # done_state И имя поля приходят из .review.yml → чужой командной строкой
            # в DSL YouTrack не должны становиться (command-injection). Убираем фигурные
            # скобки и оборачиваем значение в {…} — YouTrack трактует его как единый литерал.
            field = self._status_field.replace("{", "").replace("}", "")
            state = (done_state or "Fixed").replace("{", "").replace("}", "")
            cmd = self._client.post(
                "/commands",
                json={"query": f"{field} {{{state}}}", "issues": [{"idReadable": key}]})
            if getattr(cmd, "status_code", 200) >= 400:
                warnings.append(f"команда '{field} {state}' не выполнена: HTTP {cmd.status_code}")
            else:
                done_set = True
```

(`done_column` в теле YouTrack не используется — принят для совместимости Protocol.)

В `reviewer/tasks/boards/base.py` — расширить докстринг/сигнатуру Protocol `finish` (строки 60-67):

```python
    def finish(self, key: str, pr_url: str, *, note: str | None = None,
               mark_done: bool = True, done_state: str | None = None,
               done_column: str | None = None) -> dict:
        """Закрыть задачу: пометить done + идемпотентно дописать PR-ссылку в описание.
        Любая правка двигает last-modified (timestamp/updated) → инкрементальный синк
        переиндексирует обновлённую задачу. done_state — целевое состояние (YouTrack;
        YouGile игнорирует, у него булев completed). done_column — целевая колонка
        (YouGile: перенос задачи; YouTrack игнорирует). Возвращает
        {key, board_id, done_set, pr_link_added, already_closed, warnings}."""
        ...
```

В `tests/tasks/boards/test_youtrack_finish.py` — сделать helper `_board` инициализирующим дефолт поля (чтобы существующие тесты не падали на отсутствии `_status_field`):

```python
def _board(get_routes, post_status=None):
    b = YouTrackBoard.__new__(YouTrackBoard)  # обойти httpx.Client в __init__
    b._client = _Client(get_routes, post_status)
    b._status_field = "State"
    return b
```

- [ ] **Step 4: Прогнать — зелёные**

Run: `.venv/bin/pytest tests/tasks/boards/test_youtrack_finish.py tests/tasks/boards/test_youtrack_normalize.py -q`
Expected: PASS (все, включая новые).

- [ ] **Step 5: Линт + коммит**

Run: `.venv/bin/ruff check reviewer/tasks/boards/youtrack.py reviewer/tasks/boards/base.py`
Expected: чисто.

```bash
git add reviewer/tasks/boards/youtrack.py reviewer/tasks/boards/base.py tests/tasks/boards/test_youtrack_finish.py
git commit -m "feat(tasks): конфигурируемое поле статуса YouTrack (чтение+запись через status_field)"
```

---

### Task 2: YouGile — перенос в done-колонку в `finish`

**Files:**
- Modify: `reviewer/tasks/boards/yougile.py` (`finish` — параметр `done_column`; новый `_resolve_column_id`)
- Test: `tests/tasks/boards/test_yougile_finish.py`

**Interfaces:**
- Consumes: Protocol `finish(..., done_column=None)` (из Task 1)
- Produces:
  - `YougileBoard.finish(key, pr_url, *, note=None, mark_done=True, done_state=None, done_column=None) -> dict` — возврат дополнен `column_moved: bool`
  - `YougileBoard._resolve_column_id(current_col_id: str, title: str) -> str | None`

- [ ] **Step 1: Написать падающие тесты**

Проверить в `tests/tasks/boards/test_yougile_finish.py` существующий helper клиента (как он маршрутизирует GET/PUT). Добавить тесты (адаптировать под тамошний `_Client`/`_board`; ниже — на типовом фейке с маршрутами по path):

```python
def test_yougile_finish_moves_to_done_column():
    # задача в колонке col-cur (доска brd-1); done-колонка «Готово» = col-done.
    routes = {
        "/tasks/PRI-10": _Resp(200, {"id": "u1", "description": "", "completed": False,
                                     "columnId": "col-cur"}),
        "/columns/col-cur": _Resp(200, {"boardId": "brd-1"}),
        "/columns": _Resp(200, {"content": [
            {"id": "col-cur", "title": "В работе"},
            {"id": "col-done", "title": "Готово"}]}),
    }
    b = _board(routes)
    res = b.finish("PRI-10", PR, done_column="Готово")
    assert res["column_moved"] is True
    put = next(c for c in b._client.calls if c[0] == "PUT")
    assert put[2]["columnId"] == "col-done"
    assert put[2]["completed"] is True


def test_yougile_finish_column_not_found_failsoft():
    routes = {
        "/tasks/PRI-10": _Resp(200, {"id": "u1", "description": "", "completed": False,
                                     "columnId": "col-cur"}),
        "/columns/col-cur": _Resp(200, {"boardId": "brd-1"}),
        "/columns": _Resp(200, {"content": [{"id": "col-cur", "title": "В работе"}]}),
    }
    b = _board(routes)
    res = b.finish("PRI-10", PR, done_column="Нет такой")
    assert res["column_moved"] is False
    assert res["warnings"]                 # предупреждение о ненайденной колонке
    assert res["done_set"] is True         # completed всё равно выставлен
    put = next(c for c in b._client.calls if c[0] == "PUT")
    assert "columnId" not in put[2]


def test_yougile_finish_no_done_column_unchanged():
    routes = {"/tasks/PRI-10": _Resp(200, {"id": "u1", "description": "",
                                           "completed": False, "columnId": "col-cur"})}
    b = _board(routes)
    res = b.finish("PRI-10", PR)  # done_column не задан
    assert res["column_moved"] is False
    assert not any(c[1] == "/columns/col-cur" for c in b._client.calls)  # резолва нет
```

Если в файле нет `_Resp`/`_board`, поддерживающих множественные GET-маршруты и запись `calls`, — расширить helper по образцу `test_youtrack_finish.py` (класс `_Client` с `get`/`put`/`post`, накапливающий `self.calls`).

- [ ] **Step 2: Прогнать — падают**

Run: `.venv/bin/pytest tests/tasks/boards/test_yougile_finish.py -q`
Expected: FAIL (`finish()` got unexpected keyword `done_column`; либо `column_moved` отсутствует).

- [ ] **Step 3: Реализация — yougile.py**

Новый метод перед `finish` (после `normalize`, ~строка 250):

```python
    def _resolve_column_id(self, current_col_id: str, title: str) -> str | None:
        """id колонки с заданным title на той же доске, что и current_col_id.

        GET /columns/{cur} → boardId; GET /columns?boardId=… → match по title.
        fail-soft: сетевой сбой/не найдено → None (задачу не двигаем)."""
        try:
            r = self._client.get(f"/columns/{quote(str(current_col_id), safe='')}")
            r.raise_for_status()
            board_id = r.json().get("boardId")
            if not board_id:
                return None
            for col in self._get_all("/columns", {"boardId": board_id}):
                if col.get("title") == title:
                    return col.get("id")
        except Exception:
            log.warning("yougile: резолв колонки '%s' не удался", title, exc_info=True)
        return None
```

`finish` (строки 251-288) — сигнатура + резолв колонки + `column_moved` в возврате:

```python
    def finish(self, key: str, pr_url: str, *, note: str | None = None,
               mark_done: bool = True, done_state: str | None = None,
               done_column: str | None = None) -> dict:
        """Закрыть задачу YouGile: completed:true + PR-ссылка в описание (идемпотентно)
        + опциональный перенос в done-колонку (done_column).

        GET /tasks/{key} резолвит проектный/компанийный код в объект (+ uuid, columnId).
        PUT обновляет задачу — двигает её timestamp (watermark синка). done_state не
        применим (у YouGile булев completed)."""
        r = self._client.get(f"/tasks/{quote(key, safe='')}")
        r.raise_for_status()
        task = r.json()
        uuid = task.get("id") or key
        desc = task.get("description", "") or ""
        completed = bool(task.get("completed", False))
        cur_col = task.get("columnId")

        payload: dict = {}
        warnings: list[str] = []
        pr_link_added = False
        # PR-ссылка и note уходят в HTML-описание доски — экранируем во избежание
        # HTML/XSS-инъекции (note приходит от пользователя). Idempotency-проверка
        # сравнивает с экранированной формой (для обычных URL совпадает с сырой).
        safe_url = html.escape(pr_url, quote=True)
        if pr_url and safe_url not in desc:
            block = f'\n<div>PR: <a href="{safe_url}">{safe_url}</a></div>'
            if note:
                block += f"\n<div>{html.escape(note)}</div>"
            payload["description"] = desc + block
            pr_link_added = True

        column_moved = False
        if done_column and cur_col:
            target = self._resolve_column_id(cur_col, done_column)
            if target is None:
                warnings.append(f"колонка '{done_column}' не найдена — задача не перенесена")
            elif target != cur_col:
                payload["columnId"] = target
                column_moved = True

        done_set = False
        if mark_done and not completed:
            payload["completed"] = True
            done_set = True

        if payload:
            rr = self._client.put(f"/tasks/{quote(str(uuid), safe='')}", json=payload)
            rr.raise_for_status()
        return {"key": key, "board_id": uuid, "done_set": done_set,
                "pr_link_added": pr_link_added, "column_moved": column_moved,
                "already_closed": not payload, "warnings": warnings}
```

- [ ] **Step 4: Прогнать — зелёные**

Run: `.venv/bin/pytest tests/tasks/boards/test_yougile_finish.py tests/tasks/boards/test_yougile_normalize.py -q`
Expected: PASS.

- [ ] **Step 5: Линт + коммит**

Run: `.venv/bin/ruff check reviewer/tasks/boards/yougile.py`
Expected: чисто.

```bash
git add reviewer/tasks/boards/yougile.py tests/tasks/boards/test_yougile_finish.py
git commit -m "feat(tasks): перенос задачи YouGile в done-колонку (done_column) в finish"
```

---

### Task 3: Фабрика — проброс `status_field` в `make_board_provider`

**Files:**
- Modify: `reviewer/tasks/boards/__init__.py` (`make_board_provider` — kw-only `status_field`)
- Test: `tests/tasks/boards/test_base.py` (или новый `tests/tasks/boards/test_factory.py`)

**Interfaces:**
- Consumes: `YouTrackBoard.__init__(status_field=...)` (Task 1)
- Produces: `make_board_provider(settings, type_, *, status_field: str | None = None) -> TaskBoardProvider | None`

- [ ] **Step 1: Написать падающий тест**

Добавить в `tests/tasks/boards/test_base.py` (использует лёгкий фейковый `settings`):

```python
def test_make_board_provider_threads_status_field(monkeypatch):
    import reviewer.tasks.boards as boards

    captured = {}

    class _FakeYT:
        board_type = "youtrack"

        def __init__(self, *, token, base_url, key_pattern, status_field="State", **kw):
            captured["status_field"] = status_field

    monkeypatch.setattr("reviewer.tasks.boards.youtrack.YouTrackBoard", _FakeYT)

    settings = type("S", (), {
        "board_creds": staticmethod(lambda t: ("tok", "https://yt/api")),
        "task_board_key_pattern": r"TES-\d+",
        "task_board_url_template": "",
        "task_attachment_max_bytes": 1,
        "task_attachment_timeout": 1.0,
        "task_attachment_store_chars": 1,
    })()
    boards.make_board_provider(settings, "youtrack", status_field="Stage")
    assert captured["status_field"] == "Stage"
```

- [ ] **Step 2: Прогнать — падает**

Run: `.venv/bin/pytest tests/tasks/boards/test_base.py::test_make_board_provider_threads_status_field -q`
Expected: FAIL (`make_board_provider() got an unexpected keyword argument 'status_field'`).

- [ ] **Step 3: Реализация — __init__.py**

`make_board_provider` (строка 10) — добавить kw-only параметр и передать в YouTrack:

```python
def make_board_provider(settings, type_: str, *,
                        status_field: str | None = None) -> TaskBoardProvider | None:
    """Сконструировать провайдер доски заданного типа из его кредов (board_creds).

    status_field — имя YouTrack-поля статуса из .review.yml (дефолт «State»);
    YouGile его игнорирует (статус = колонка).

    None, если у типа нет API-ключа (доска этого типа не настроена) или тип
    неизвестен — server-side синк для него недоступен.
    """
    api_key, api_base = settings.board_creds(type_)
    if not api_key:
        return None
    key_pattern = settings.task_board_key_pattern
    if type_ == "yougile":
        from reviewer.tasks.boards.yougile import YougileBoard
        return YougileBoard(
            api_key=api_key,
            api_base=api_base,
            key_pattern=key_pattern,
            url_template=settings.task_board_url_template,
            attachment_max_bytes=settings.task_attachment_max_bytes,
            attachment_timeout=settings.task_attachment_timeout,
            attachment_store_chars=settings.task_attachment_store_chars,
        )
    if type_ == "youtrack":
        from reviewer.tasks.boards.youtrack import YouTrackBoard
        return YouTrackBoard(
            token=api_key,
            base_url=api_base,
            key_pattern=key_pattern,
            status_field=status_field or "State",
            attachment_max_bytes=settings.task_attachment_max_bytes,
            attachment_timeout=settings.task_attachment_timeout,
            attachment_store_chars=settings.task_attachment_store_chars,
        )
    return None
```

(`make_board_providers` — без изменений: стартап строит YouTrack с дефолтом «State»; SyncService переустанавливает поле per-run в Task 4.)

- [ ] **Step 4: Прогнать — зелёный**

Run: `.venv/bin/pytest tests/tasks/boards/test_base.py -q`
Expected: PASS.

- [ ] **Step 5: Линт + коммит**

Run: `.venv/bin/ruff check reviewer/tasks/boards/__init__.py`
Expected: чисто.

```bash
git add reviewer/tasks/boards/__init__.py tests/tasks/boards/test_base.py
git commit -m "feat(tasks): make_board_provider пробрасывает status_field в YouTrack"
```

---

### Task 4: Сервис + синк + MCP-тулы — проброс `status_field`/`done_column`

**Files:**
- Modify: `reviewer/mcp/service.py` (`finish_task`, `sync_board`)
- Modify: `reviewer/tasks/sync.py` (`SyncService.run` — параметр `status_field`, reset YouTrack-провайдеров)
- Modify: `reviewer/entrypoints/mcp_server.py` (тулы `finish_task`, `sync_board` — параметры + докстринги)
- Test: `tests/mcp/test_finish_task.py`, `tests/mcp/test_sync_board.py`, `tests/tasks/test_sync.py` (если есть; иначе добавить в существующий sync-тест)

**Interfaces:**
- Consumes: `make_board_provider(status_field=)` (Task 3); `provider.finish(done_column=)` (Task 1/2); `YouTrackBoard.set_status_field` (Task 1)
- Produces:
  - `MCPReviewService.finish_task(key, pr_url, note=None, mark_done=True, board_type=None, done_state=None, status_field=None, done_column=None) -> dict`
  - `MCPReviewService.sync_board(board=None, limit=None, purge_orphaned=False, keep_with_prs=True, board_type=None, status_field=None) -> dict`
  - `SyncService.run(board=None, limit=None, purge_orphaned=False, keep_with_prs=True, board_type=None, status_field=None) -> dict`

- [ ] **Step 1: Обновить существующие тесты (сломаются на новых kwargs) + добавить новые**

В `tests/mcp/test_finish_task.py`:
- `_Provider.finish` — принять `done_column`:

```python
    def finish(self, key, pr_url, *, note=None, mark_done=True, done_state=None,
               done_column=None):
        self.finished = (key, pr_url, note, mark_done, done_state, done_column)
        return {"key": key, "board_id": "u1", "done_set": True,
                "pr_link_added": True, "already_closed": False, "warnings": []}
```

- каждый `monkeypatch.setattr(svc_mod, "make_board_provider", lambda s, t: prov)` → `lambda s, t, status_field=None: prov` (в тестах `test_finish_task_resolves_single_board`, `test_finish_task_explicit_board_type`, `test_finish_task_failsoft`).
- `test_finish_task_resolves_single_board`: обновить ассерт кортежа:

```python
    assert prov.finished == ("PRI-10", "https://github.com/o/r/pull/7",
                             None, True, None, None)
```

- `test_finish_task_explicit_board_type`: `prov.finished[4] == "Done"` остаётся; сигнатура lambda — `lambda s, t, status_field=None: prov`.
- Добавить новый тест проброса:

```python
def test_finish_task_threads_status_field_and_done_column(monkeypatch):
    prov = _Provider()
    seen = {}
    monkeypatch.setattr(svc_mod, "make_board_provider",
                        lambda s, t, status_field=None: (seen.__setitem__("sf", status_field), prov)[1])
    _Svc(["youtrack"]).finish_task("TES-1", "url", board_type="youtrack",
                                   status_field="Stage", done_column="Готово")
    assert seen["sf"] == "Stage"
    assert prov.finished[5] == "Готово"   # done_column доехал до provider.finish
```

В `tests/mcp/test_sync_board.py`:
- `FakeSync.run` в `test_sync_board_delegates_to_sync_service` и `test_sync_board_threads_board_type` — добавить `status_field=None` в сигнатуру; обновить `called_with`:

```python
        def run(self, board=None, limit=None, purge_orphaned=False,
                keep_with_prs=True, board_type=None, status_field=None):
            self.called_with = (board, limit, purge_orphaned, keep_with_prs,
                                board_type, status_field)
            return {"enumerated": 3, "changed": 1, "warnings": []}
```

```python
    assert fake.called_with == ("B", 5, False, True, "yougile", None)
```

- Добавить тест проброса status_field:

```python
def test_sync_board_threads_status_field():
    class FakeSync:
        def run(self, board=None, limit=None, purge_orphaned=False,
                keep_with_prs=True, board_type=None, status_field=None):
            self.called_with = status_field
            return {"enumerated": 1, "warnings": []}
    fake = FakeSync()
    _Svc(fake).sync_board(board="TES", board_type="youtrack", status_field="Stage")
    assert fake.called_with == "Stage"
```

Для `SyncService.run` reset — добавить тест в `tests/tasks/test_sync.py` (создать файл, если нет):

```python
def test_sync_run_resets_youtrack_status_field():
    class _YT:
        board_type = "youtrack"
        def __init__(self):
            self._status_field = "State"
        def set_status_field(self, f):
            self._status_field = f or "State"
        def iter_raw(self, board, limit):
            return iter([])
    from reviewer.tasks.sync import SyncService
    yt = _YT()
    meta = type("M", (), {"get_index_meta": lambda *a: None,
                          "set_index_meta": lambda *a: None})()
    tasks = type("T", (), {"index_batch": staticmethod(lambda x: [])})()
    svc = SyncService([yt], tasks, meta)
    svc.run(board_type="youtrack", status_field="Stage")
    assert yt._status_field == "Stage"
    svc.run(board_type="youtrack")           # без status_field → сброс к State
    assert yt._status_field == "State"
```

- [ ] **Step 2: Прогнать — падают**

Run: `.venv/bin/pytest tests/mcp/test_finish_task.py tests/mcp/test_sync_board.py tests/tasks/test_sync.py -q`
Expected: FAIL (новые kwargs не приняты сервисом/SyncService; новые тесты падают).

- [ ] **Step 3: Реализация — service.py, sync.py, mcp_server.py**

`reviewer/mcp/service.py` `sync_board` (строки 299-320) — сигнатура + проброс:

```python
    def sync_board(self, board: str | None = None, limit: int | None = None,
                   purge_orphaned: bool = False, keep_with_prs: bool = True,
                   board_type: str | None = None,
                   status_field: str | None = None) -> dict:
        """Server-side ETL: перечислить доску по REST, нормализовать, проиндексировать.

        board_type ограничивает синк одним типом доски (yougile|youtrack); board —
        проектом (префикс кода). status_field — имя YouTrack-поля статуса из .review.yml
        (чтобы синк читал верное поле). Доска/ключ не настроены → error-summary (fail-soft).
        """
        sync = getattr(self.components, "sync_service", None)
        if sync is None:
            return {"status": "error",
                    "reason": "task board REST not configured — set YOUGILE_API_KEY or "
                              "YOUTRACK_TOKEN + YOUTRACK_BASE_URL in the reviewer-mcp env "
                              "(~/.config/rag-reviewer/.env), then reconnect. Yougile key: "
                              "configurator (Ctrl+~ → API) or POST /api-v2/auth/keys"}
        try:
            return sync.run(board=board, board_type=board_type, limit=limit,
                            purge_orphaned=purge_orphaned,
                            keep_with_prs=keep_with_prs, status_field=status_field)
        except Exception as e:
            log.warning("sync_board: сбой синка", exc_info=True)
            return {"status": "error", "reason": f"{type(e).__name__}: {e}"}
```

`reviewer/mcp/service.py` `finish_task` (строки 322-352) — сигнатура + проброс:

```python
    def finish_task(self, key: str, pr_url: str, note: str | None = None,
                    mark_done: bool = True, board_type: str | None = None,
                    done_state: str | None = None, status_field: str | None = None,
                    done_column: str | None = None) -> dict:
        """Закрыть задачу на доске (server-side write): пометить done + дописать
        PR-ссылку в описание. Резолвит провайдера по board_type (или единственному
        настроенному), fail-soft. status_field/done_state — YouTrack (поле+значение);
        done_column — YouGile (колонка). Креды из env; наружу не отдаются."""
        types = self.settings.configured_board_types()
        if board_type is None:
            if len(types) == 1:
                board_type = types[0]
            else:
                return {"status": "error",
                        "reason": f"board_type required (configured: {types or 'none'})"}
        if board_type not in types:
            return {"status": "error",
                    "reason": f"board '{board_type}' not configured (have: {types or 'none'})"}
        provider = make_board_provider(self.settings, board_type, status_field=status_field)
        if provider is None:
            return {"status": "error", "reason": f"board '{board_type}' not configured"}
        try:
            result = provider.finish(key, pr_url, note=note, mark_done=mark_done,
                                     done_state=done_state, done_column=done_column)
        except Exception as e:  # fail-soft: PR уже создан, доска — вторичный эффект
            log.warning("finish_task: сбой записи в доску", exc_info=True)
            return {"status": "error", "reason": f"{type(e).__name__}: {e}"}
        finally:
            try:
                provider.close()
            except Exception:
                pass
        return {"status": "ok", "board_type": board_type, **result}
```

`reviewer/tasks/sync.py` `run` (строки 81-91) — параметр + reset YouTrack-провайдеров:

```python
    def run(self, board=None, limit=None, purge_orphaned=False,
            keep_with_prs=True, board_type=None, status_field=None) -> dict:
        agg = {"enumerated": 0, "changed": 0, "embedded": 0, "refreshed": 0,
               "unchanged": 0, "failed": 0, "warnings": [], "cursor_advanced": False}
        # PRI-170: scoped-синк из репо — только один тип доски (board_type), а не все.
        providers = self._providers
        if board_type is not None:
            providers = [p for p in self._providers if p.board_type == board_type]
            if not providers:
                agg["warnings"].append(
                    f"тип доски '{board_type}' не настроен на сервере")
        # per-repo имя поля статуса YouTrack (из .review.yml). Провайдер синка —
        # singleton; выставляем детерминированно каждый run (None → сброс к «State»).
        for p in providers:
            if getattr(p, "board_type", None) == "youtrack" and hasattr(p, "set_status_field"):
                p.set_status_field(status_field)
```

(остальное тело `run` — без изменений.)

`reviewer/entrypoints/mcp_server.py` `sync_board` (строки 105-114) — параметр + проброс:

```python
    def sync_board(board: str | None = None, limit: int | None = None,
                   purge_orphaned: bool = False, keep_with_prs: bool = True,
                   board_type: str | None = None,
                   status_field: str | None = None) -> dict:
```

(в докстринге добавить строку про `status_field` — имя YouTrack-поля из .review.yml; вызов:)

```python
        return service.sync_board(board, limit, purge_orphaned, keep_with_prs,
                                  board_type, status_field)
```

`reviewer/entrypoints/mcp_server.py` `finish_task` (строки 117-126) — параметры + проброс:

```python
    def finish_task(key: str, pr_url: str, note: str | None = None,
                    mark_done: bool = True, board_type: str | None = None,
                    done_state: str | None = None, status_field: str | None = None,
                    done_column: str | None = None) -> dict:
```

(в докстринге добавить: `status_field`/`done_column` приходят из `.review.yml` — YouTrack (поле статуса) / YouGile (done-колонка); вызов:)

```python
        return service.finish_task(key, pr_url, note, mark_done, board_type,
                                   done_state, status_field, done_column)
```

- [ ] **Step 4: Прогнать — зелёные (+ регресс server-тула)**

Run: `.venv/bin/pytest tests/mcp/test_finish_task.py tests/mcp/test_sync_board.py tests/tasks/test_sync.py tests/mcp/test_server.py -q`
Expected: PASS. Если `test_server.py` ассертит текст докстринга finish_task — обновить ожидание под новую строку (набор имён тулов не меняется).

- [ ] **Step 5: Линт + коммит**

Run: `.venv/bin/ruff check reviewer/mcp/service.py reviewer/tasks/sync.py reviewer/entrypoints/mcp_server.py`
Expected: чисто.

```bash
git add reviewer/mcp/service.py reviewer/tasks/sync.py reviewer/entrypoints/mcp_server.py tests/mcp/test_finish_task.py tests/mcp/test_sync_board.py tests/tasks/test_sync.py
git commit -m "feat(mcp): проброс status_field/done_column через finish_task и sync_board"
```

---

### Task 5: Клиентские скилы + guard-тесты + CLAUDE.md

**Files:**
- Modify: `plugin/skills/finish-task/SKILL.md` (читать `status_field`/`done_column`, передавать в тулы)
- Modify: `plugin/skills/sync-tasks/SKILL.md` (читать `status_field`, передавать в `sync_board`)
- Modify: `plugin/skills/solve-task/SKILL.md` (preflight `sync_board` — передавать `status_field`)
- Modify: `CLAUDE.md` (расширить инвариант `finish_task`/`.review.yml` описанием новых ключей)
- Test: `tests/skills/test_finish_task_skill.py`, `tests/skills/test_sync_tasks_guardrail.py`

**Interfaces:**
- Consumes: MCP-тулы `finish_task(status_field=, done_column=)`, `sync_board(status_field=)` (Task 4)

- [ ] **Step 1: Написать падающие guard-тесты**

В `tests/skills/test_finish_task_skill.py` добавить:

```python
def test_finish_task_reads_status_field_and_done_column():
    t = SKILL.read_text(encoding="utf-8")
    assert "status_field" in t     # читает имя поля YouTrack из .review.yml
    assert "done_column" in t       # читает done-колонку YouGile из .review.yml
```

В `tests/skills/test_sync_tasks_guardrail.py` добавить (проверить фактическое имя переменной пути к SKILL в файле — обычно `SKILL`):

```python
def test_sync_tasks_reads_status_field():
    assert "status_field" in SKILL.read_text(encoding="utf-8")
```

- [ ] **Step 2: Прогнать — падают**

Run: `.venv/bin/pytest tests/skills/test_finish_task_skill.py tests/skills/test_sync_tasks_guardrail.py -q`
Expected: FAIL (`status_field`/`done_column` ещё не упомянуты в скилах).

- [ ] **Step 3: Реализация — правки скилов + CLAUDE.md**

`plugin/skills/finish-task/SKILL.md`:

Step 1 «Config» (строка 15) — расширить список читаемых ключей:

```markdown
1. **Config.** Read the `task_board` block (`type`, `project`, `done_state`, `status_field`,
   `done_column`) from the repo's `.review.yml`; if there is no block, fall back to
   `get_board_config()`. No board resolved / board MCP not needed here (write is server-side) —
   but no board type at all → **board-less no-op**: tell the user (in Russian) the task is not
   linked to a board and stop. `status_field` names the YouTrack status field (default `State`);
   `done_column` names the YouGile column to move the task into. Each is board-specific — the
   other board ignores the irrelevant key.
```

Step 5 «Write» (строки 33-34) — добавить новые аргументы:

```markdown
5. **Write.** Call `finish_task(key=<key>, pr_url=<url>, note=<note or null>, board_type=<type>,
   done_state=<done_state or null>, status_field=<status_field or null>,
   done_column=<done_column or null>)`. `status == "error"` → report the reason (in Russian),
   fail-open.
```

Step 6 «Re-index» (строка 36) — передать status_field в sync:

```markdown
6. **Re-index.** Call `sync_board(board=<project or null>, board_type=<type>,
   status_field=<status_field or null>)` (incremental) so the just-closed task is re-indexed
   with its real status (its last-modified is now past the cursor). Cheap when the corpus is warm.
```

`plugin/skills/sync-tasks/SKILL.md`:

Step 1 (строки 36-42) — после извлечения `task_board.project` добавить извлечение `status_field`:

```markdown
   Similarly extract `task_board.project` → `board` (or `--board` override), and
   `task_board.status_field` → `status_field` (YouTrack status field name; default `State`
   server-side when null).
```

Блок вызова `sync_board(...)` (строки 47-49) — добавить аргумент:

```markdown
   sync_board(
       board_type=<type from step 1 or null>,
       board=<--board or task_board.project or null>,
       status_field=<task_board.status_field or null>,
```

`plugin/skills/solve-task/SKILL.md`:

Preflight `sync_board` (строки 40-42) — добавить `status_field`:

```markdown
      `sync_board(board=<task_board.project or null>, board_type=<task_board.type or null>,
      status_field=<task_board.status_field or null>, limit=null, purge_orphaned=false)` —
      `task_board.type`, `task_board.project` и `task_board.status_field` берутся из
      `<root>/.review.yml` (прочитай здесь, до вызова `sync_board`; при отсутствии файла или
      блока `task_board` — используй `null`).
```

`CLAUDE.md` — расширить инвариант «Закрытие задачи после PR (`finish_task`)» (последний блок раздела «Неочевидные факты») финальной строкой:

```markdown
  **Done-цель настраивается per-repo в `.review.yml` (`task_board`):** `status_field` — имя
  YouTrack-поля статуса (дефолт `State`; действует И на чтение статуса при синке, И на команду
  `finish` — чинит доски на кастом-полях типа `Stage`); `done_state` — целевое значение поля;
  `done_column` — YouGile-колонка, в которую `finish` переносит задачу (+`completed:true`).
  Каждый ключ board-специфичен (другая доска игнорирует нерелевантный). Клиент читает ключи и
  передаёт в `finish_task`/`sync_board`; сервер репо-агностичен (`.review.yml` не парсит).
```

- [ ] **Step 4: Прогнать — зелёные (весь skills-набор)**

Run: `.venv/bin/pytest tests/skills/ -q`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add plugin/skills/finish-task/SKILL.md plugin/skills/sync-tasks/SKILL.md plugin/skills/solve-task/SKILL.md CLAUDE.md tests/skills/test_finish_task_skill.py tests/skills/test_sync_tasks_guardrail.py
git commit -m "feat(skills): скилы читают status_field/done_column из .review.yml и передают в тулы"
```

---

### Task 6: Полный прогон, деплой 0.2.22 и live-acceptance

**Files:**
- Modify: `pyproject.toml` (версия `0.2.21` → `0.2.22`)

**Interfaces:** нет (интеграция + релиз).

- [ ] **Step 1: Полный unit-прогон + линт**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check reviewer/ tests/`
Expected: PASS (предсуществующий сбой сбора `tests/web/test_api.py` из-за fastapi — не наш, игнорировать, если воспроизводится и до правок). Зафиксировать число passed.

- [ ] **Step 2: Финальное whole-branch ревью (Opus-субагент)**

Через subagent-driven-development: whole-branch review диффа `dev..feat/configurable-done-target`. Ожидание — 0 новых Critical/Important. Minor → roll-up/defer с обоснованием.

- [ ] **Step 3: Security-скан fail-soft путей записи**

Проверить: YouTrack команда — имя поля И значение оба через brace-strip (`.replace("{","").replace("}","")`) → нет DSL-инъекции; YouGile — `done_column` только резолвит id (не пишется как HTML/сырой ввод), PR-ссылка/note по-прежнему `html.escape`; пути `/columns/{id}` через `quote(..., safe='')`. Подтвердить, что новые ветки не логируют креды.

- [ ] **Step 4: Бамп версии + деплой в main**

```bash
# на dev после мержа ветки:
# pyproject.toml: version = "0.2.22"
git add pyproject.toml
git commit -m "chore: bump 0.2.21 → 0.2.22"
```

Мерж `dev` → `main` (PyPI-автопубликация workflow «Publish to PyPI»). Merge-правила обходятся админ-правами (локальный `--no-ff` merge + `git push origin main`, т.к. `gh pr merge` API недоступен PAT). Дождаться success workflow → `rag-reviewer 0.2.22` на PyPI. Попросить пользователя выполнить `reviewer update`.

- [ ] **Step 5: Live-acceptance на обеих досках (ручная, с пользователем)**

Предусловие: пользователь сделал `reviewer update` (0.2.22 подтянут). `.review.yml` тестовых репо: TES — `status_field: Stage`, `done_state: Готово`; PRI — `done_column: Готово`.

- **YouTrack TES:** `finish_task(key=TES-3, pr_url=…, board_type=youtrack, status_field="Stage", done_state="Готово")` → на доске `Stage=Готово`, warnings пусты; `sync_board(board="TES", board_type="youtrack", status_field="Stage")` → `get_task(TES-3)` показывает `status="Готово"` (не null) + PR-ссылка; `get_task_context` → PR-ребро; повторный `finish_task` → `already_closed`/без дубля PR.
- **YouGile PRI:** `finish_task(key=<PRI-задача>, pr_url=…, board_type=yougile, done_column="Готово")` → задача в колонке «Готово» + `completed`, `column_moved=true`; `sync_board(board="PRI", board_type="yougile")` → `get_task` `status="done"` + PR-ссылка; идемпотентность.

Любой красный шаг → чинить и повторять. Зелёно на обеих → `superpowers:finishing-a-development-branch`.

- [ ] **Step 6: Финализация**

Обновить память (`finish-task-branch-status.md` → закрыть live-acceptance для configurable-done-target) и ledger. Удалить ветку после мержа.
