# Post-PR task closeout (finish_task) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** После создания PR плагин предлагает закрыть задачу на доске — дописать PR-ссылку в описание и пометить выполненной так, чтобы инкрементальный синк reviewer сохранил обновлённую версию.

**Architecture:** Первый write у провайдеров досок — метод `finish()` на протоколе `TaskBoardProvider` (YouGile: `completed:true` + PR-ссылка в описание; YouTrack: правка описания + команда `State`). Запись зовётся через новый server-side MCP-тул `finish_task` (креды в env, портируется во все клиенты). Новый тонкий скилл `/reviewer_finish-task` резолвит ключ/PR, зовёт `finish_task`, затем `sync_board` для ре-индекса.

**Tech Stack:** Python 3.11–3.13, httpx (REST-клиенты досок), FastMCP (тулы), pytest, ruff.

## Global Constraints

- Python 3.11–3.13; ruff line-length 100, target py311.
- Язык кода/докстрингов/сообщений — **русский**. Тело SKILL.md — английский (токены), но скилл инструктирует отвечать пользователю по-русски.
- Тесты: `.venv/bin/pytest -q` (по умолчанию `-m 'not integration'`). Unit — на фейках/моках, внешние API не дёргают.
- Внешние сервисы (доски) изолированы за интерфейсами и мокаются в unit; реальные вызовы — только в live-acceptance (Task 6).
- Коммиты: **без self-attribution**, Conventional Commits на русском.
- Идемпотентность: повторный `finish_task` не плодит дубли PR-ссылки. Fail-soft: сбой доски → отчёт, без краха. board-less/нет ключа → graceful no-op.
- Провайдеры сейчас read-only — `finish()` их первый write. Креды берутся из env (`board_creds`), `board_config()` их не отдаёт (инвариант цел).

---

### Task 1: YouGile `completed` → status "done" в normalize

Синк читает `status` из имени колонки; чтобы reviewer-стор видел «done» после ре-синка, добавляем поле `RawTask.completed` и мапим его в `status="done"`.

**Files:**
- Modify: `reviewer/tasks/boards/base.py` (dataclass `RawTask`)
- Modify: `reviewer/tasks/boards/yougile.py:63-109` (`normalize_yougile`) и `:168-177` (`iter_raw` RawTask)
- Test: `tests/tasks/boards/test_yougile_normalize.py`

**Interfaces:**
- Produces: `RawTask.completed: bool = False`; `normalize_yougile(...)["status"] == "done"` когда `raw.completed`.

- [ ] **Step 1: Write the failing tests** — добавить в конец `tests/tasks/boards/test_yougile_normalize.py`:

```python
def test_normalize_completed_maps_to_done_status():
    b = normalize_yougile(_raw(status="In progress", completed=True), KP, URL)
    assert b["status"] == "done"


def test_normalize_not_completed_keeps_column_status():
    b = normalize_yougile(_raw(status="In progress", completed=False), KP, URL)
    assert b["status"] == "In progress"
```

- [ ] **Step 2: Run to verify FAIL**

Run: `.venv/bin/pytest tests/tasks/boards/test_yougile_normalize.py::test_normalize_completed_maps_to_done_status -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'completed'` (поля ещё нет).

- [ ] **Step 3: Add `completed` field to `RawTask`** — в `reviewer/tasks/boards/base.py`, после `board_id`:

```python
    board_id: str = ""  # внутренний id задачи у провайдера (yougile UUID для чат-эндпоинта;
    # youtrack не использует — там везде idReadable)
    completed: bool = False  # YouGile: булев чекбокс «выполнено» (мапится в status="done")
```

- [ ] **Step 4: Map `completed` в `normalize_yougile`** — в `reviewer/tasks/boards/yougile.py`, в `return` блоке `normalize_yougile` заменить строку `"status": raw.status,` на:

```python
        "status": "done" if raw.completed else raw.status,
```

- [ ] **Step 5: Пробросить `completed` в `iter_raw`** — в `reviewer/tasks/boards/yougile.py`, в `RawTask(...)` внутри `iter_raw` добавить поле (после `timestamp=...`):

```python
                            timestamp=int(t.get("timestamp", 0) or 0),
                            board_id=t["id"],
                            completed=bool(t.get("completed", False)),
```

- [ ] **Step 6: Run to verify PASS**

Run: `.venv/bin/pytest tests/tasks/boards/test_yougile_normalize.py -q`
Expected: PASS (все, включая два новых).

- [ ] **Step 7: Lint + commit**

```bash
.venv/bin/ruff check reviewer/tasks/boards/base.py reviewer/tasks/boards/yougile.py
git add reviewer/tasks/boards/base.py reviewer/tasks/boards/yougile.py tests/tasks/boards/test_yougile_normalize.py
git commit -m 'feat(tasks): YouGile completed → status "done" в normalize'
```

---

### Task 2: `finish()` на протоколе + `YougileBoard.finish`

Первый write-метод провайдера: идемпотентно дописать PR-ссылку в описание + `completed:true`.

**Files:**
- Modify: `reviewer/tasks/boards/base.py` (Protocol `TaskBoardProvider` — добавить сигнатуру `finish`)
- Modify: `reviewer/tasks/boards/yougile.py` (метод `YougileBoard.finish`)
- Test: `tests/tasks/boards/test_yougile_finish.py` (создать)

**Interfaces:**
- Produces: `provider.finish(key: str, pr_url: str, *, note: str | None = None, mark_done: bool = True, done_state: str | None = None) -> dict` → `{key, board_id, done_set, pr_link_added, already_closed, warnings}`.
- Consumes (в тесте): httpx-клиент провайдера через фейк с методами `get`/`put`/`close`.

- [ ] **Step 1: Write the failing tests** — создать `tests/tasks/boards/test_yougile_finish.py`:

```python
from reviewer.tasks.boards.yougile import YougileBoard

PR = "https://github.com/o/r/pull/7"


class _Resp:
    def __init__(self, status=200, json_data=None):
        self.status_code = status
        self._json = json_data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class _Client:
    def __init__(self, get_routes):
        self._get = get_routes
        self.calls = []  # (method, path, json)

    def get(self, path, params=None):
        self.calls.append(("GET", path, None))
        return self._get[path]

    def put(self, path, json=None):
        self.calls.append(("PUT", path, json))
        return _Resp(200, {})

    def close(self):
        pass


def _board(get_routes):
    b = YougileBoard.__new__(YougileBoard)  # обойти httpx.Client в __init__
    b._client = _Client(get_routes)
    return b


def test_yougile_finish_marks_done_and_adds_pr():
    b = _board({"/tasks/PRI-10": _Resp(200, {"id": "u1", "description": "тело",
                                             "completed": False})})
    res = b.finish("PRI-10", PR)
    assert res["done_set"] is True
    assert res["pr_link_added"] is True
    assert res["already_closed"] is False
    put = next(c for c in b._client.calls if c[0] == "PUT")
    assert put[1] == "/tasks/u1"
    assert put[2]["completed"] is True
    assert PR in put[2]["description"]
    assert "тело" in put[2]["description"]


def test_yougile_finish_idempotent_when_pr_present_and_done():
    desc = f'тело<div>PR: <a href="{PR}">{PR}</a></div>'
    b = _board({"/tasks/PRI-10": _Resp(200, {"id": "u1", "description": desc,
                                             "completed": True})})
    res = b.finish("PRI-10", PR)
    assert res["already_closed"] is True
    assert res["pr_link_added"] is False
    assert res["done_set"] is False
    assert not [c for c in b._client.calls if c[0] == "PUT"]  # записи нет


def test_yougile_finish_note_appended():
    b = _board({"/tasks/PRI-10": _Resp(200, {"id": "u1", "description": "",
                                             "completed": False})})
    b.finish("PRI-10", PR, note="закрыто автоматически")
    put = next(c for c in b._client.calls if c[0] == "PUT")
    assert "закрыто автоматически" in put[2]["description"]
```

- [ ] **Step 2: Run to verify FAIL**

Run: `.venv/bin/pytest tests/tasks/boards/test_yougile_finish.py -q`
Expected: FAIL — `AttributeError: 'YougileBoard' object has no attribute 'finish'`.

- [ ] **Step 3: Add `finish` to the Protocol** — в `reviewer/tasks/boards/base.py`, в классе `TaskBoardProvider`, после `normalize`:

```python
    def finish(self, key: str, pr_url: str, *, note: str | None = None,
               mark_done: bool = True, done_state: str | None = None) -> dict:
        """Закрыть задачу: пометить done + идемпотентно дописать PR-ссылку в описание.
        Любая правка двигает last-modified (timestamp/updated) → инкрементальный синк
        переиндексирует обновлённую задачу. done_state — целевое состояние (YouTrack;
        YouGile игнорирует, у него булев completed). Возвращает
        {key, board_id, done_set, pr_link_added, already_closed, warnings}."""
        ...
```

- [ ] **Step 4: Implement `YougileBoard.finish`** — в `reviewer/tasks/boards/yougile.py`, метод класса `YougileBoard` (после `normalize`):

```python
    def finish(self, key: str, pr_url: str, *, note: str | None = None,
               mark_done: bool = True, done_state: str | None = None) -> dict:
        """Закрыть задачу YouGile: completed:true + PR-ссылка в описание (идемпотентно).

        GET /tasks/{key} резолвит проектный/компанийный код в объект (+ uuid). PUT
        обновляет задачу — двигает её timestamp (watermark синка). done_state не
        применим (у YouGile булев completed).
        """
        r = self._client.get(f"/tasks/{key}")
        r.raise_for_status()
        task = r.json()
        uuid = task.get("id") or key
        desc = task.get("description", "") or ""
        completed = bool(task.get("completed", False))

        payload: dict = {}
        pr_link_added = False
        if pr_url and pr_url not in desc:
            block = f'\n<div>PR: <a href="{pr_url}">{pr_url}</a></div>'
            if note:
                block += f"\n<div>{note}</div>"
            payload["description"] = desc + block
            pr_link_added = True
        done_set = False
        if mark_done and not completed:
            payload["completed"] = True
            done_set = True

        if payload:
            rr = self._client.put(f"/tasks/{uuid}", json=payload)
            rr.raise_for_status()
        return {"key": key, "board_id": uuid, "done_set": done_set,
                "pr_link_added": pr_link_added, "already_closed": not payload,
                "warnings": []}
```

- [ ] **Step 5: Run to verify PASS**

Run: `.venv/bin/pytest tests/tasks/boards/test_yougile_finish.py -q`
Expected: PASS (3 теста).

- [ ] **Step 6: Lint + commit**

```bash
.venv/bin/ruff check reviewer/tasks/boards/base.py reviewer/tasks/boards/yougile.py
git add reviewer/tasks/boards/base.py reviewer/tasks/boards/yougile.py tests/tasks/boards/test_yougile_finish.py
git commit -m 'feat(tasks): write-метод finish() на протоколе + YougileBoard.finish'
```

> **Примечание для live-теста (Task 6):** YouGile REST v2 обновляет задачу через `PUT /api-v2/tasks/{id}`. Если live-прогон вернёт 405 — заменить `self._client.put` на `self._client.patch` в `finish` и перепроверить.

---

### Task 3: `YouTrackBoard.finish` (описание + команда State)

**Files:**
- Modify: `reviewer/tasks/boards/youtrack.py` (метод `YouTrackBoard.finish`)
- Test: `tests/tasks/boards/test_youtrack_finish.py` (создать)

**Interfaces:**
- Produces: тот же контракт `finish(...) -> dict`. YouTrack: `POST /issues/{key}` правит описание; `POST /commands` шлёт `State <done_state or 'Fixed'>` (fail-soft при ошибке команды).

- [ ] **Step 1: Write the failing tests** — создать `tests/tasks/boards/test_youtrack_finish.py`:

```python
from reviewer.tasks.boards.youtrack import YouTrackBoard

PR = "https://github.com/o/r/pull/7"


class _Resp:
    def __init__(self, status=200, json_data=None):
        self.status_code = status
        self._json = json_data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class _Client:
    def __init__(self, get_routes, post_status=None):
        self._get = get_routes
        self._post_status = post_status or {}  # path -> status
        self.calls = []  # (method, path, json)

    def get(self, path, params=None):
        self.calls.append(("GET", path, None))
        return self._get[path]

    def post(self, path, json=None):
        self.calls.append(("POST", path, json))
        return _Resp(self._post_status.get(path, 200), {})

    def close(self):
        pass


def _board(get_routes, post_status=None):
    b = YouTrackBoard.__new__(YouTrackBoard)  # обойти httpx.Client в __init__
    b._client = _Client(get_routes, post_status)
    return b


def test_youtrack_finish_edits_desc_and_runs_state_command():
    b = _board({"/issues/TES-1": _Resp(200, {"description": "тело"})})
    res = b.finish("TES-1", PR, done_state="Fixed")
    assert res["pr_link_added"] is True
    assert res["done_set"] is True
    posts = [c for c in b._client.calls if c[0] == "POST"]
    edit = next(c for c in posts if c[1] == "/issues/TES-1")
    assert PR in edit[2]["description"]
    cmd = next(c for c in posts if c[1] == "/commands")
    assert cmd[2]["query"] == "State Fixed"
    assert cmd[2]["issues"] == [{"idReadable": "TES-1"}]


def test_youtrack_finish_default_state_fixed():
    b = _board({"/issues/TES-1": _Resp(200, {"description": ""})})
    b.finish("TES-1", PR)  # done_state не задан
    cmd = next(c for c in b._client.calls if c[1] == "/commands")
    assert cmd[2]["query"] == "State Fixed"


def test_youtrack_finish_command_failsoft():
    b = _board({"/issues/TES-1": _Resp(200, {"description": ""})},
               post_status={"/commands": 400})
    res = b.finish("TES-1", PR, done_state="NoSuchState")
    assert res["done_set"] is False
    assert res["warnings"]  # предупреждение о неуспешной команде, без краха


def test_youtrack_finish_idempotent_pr_link():
    b = _board({"/issues/TES-1": _Resp(200, {"description": f"тело\n\nPR: {PR}"})})
    res = b.finish("TES-1", PR, mark_done=False)
    assert res["pr_link_added"] is False
    assert not [c for c in b._client.calls if c[1] == "/issues/TES-1" and c[0] == "POST"]
```

- [ ] **Step 2: Run to verify FAIL**

Run: `.venv/bin/pytest tests/tasks/boards/test_youtrack_finish.py -q`
Expected: FAIL — `AttributeError: 'YouTrackBoard' object has no attribute 'finish'`.

- [ ] **Step 3: Implement `YouTrackBoard.finish`** — в `reviewer/tasks/boards/youtrack.py`, метод класса `YouTrackBoard` (после `normalize`):

```python
    def finish(self, key: str, pr_url: str, *, note: str | None = None,
               mark_done: bool = True, done_state: str | None = None) -> dict:
        """Закрыть задачу YouTrack: правка описания (PR-ссылка) + команда State.

        POST /issues/{key} правит описание (двигает `updated` — watermark синка).
        POST /commands шлёт `State <done_state or 'Fixed'>` — YouTrack сам резолвит
        значение в проекте; неуспех команды fail-soft (warnings, без краха).
        """
        r = self._client.get(f"/issues/{key}", params={"fields": "description"})
        r.raise_for_status()
        desc = r.json().get("description", "") or ""

        pr_link_added = False
        if pr_url and pr_url not in desc:
            block = f"\n\nPR: {pr_url}" + (f"\n\n{note}" if note else "")
            new_desc = desc + block if desc else block.lstrip("\n")
            rr = self._client.post(f"/issues/{key}", json={"description": new_desc})
            rr.raise_for_status()
            pr_link_added = True

        warnings: list[str] = []
        done_set = False
        if mark_done:
            state = done_state or "Fixed"
            cmd = self._client.post(
                "/commands",
                json={"query": f"State {state}", "issues": [{"idReadable": key}]})
            if getattr(cmd, "status_code", 200) >= 400:
                warnings.append(f"команда 'State {state}' не выполнена: HTTP {cmd.status_code}")
            else:
                done_set = True

        return {"key": key, "board_id": key, "done_set": done_set,
                "pr_link_added": pr_link_added,
                "already_closed": not pr_link_added and not done_set,
                "warnings": warnings}
```

- [ ] **Step 4: Run to verify PASS**

Run: `.venv/bin/pytest tests/tasks/boards/test_youtrack_finish.py -q`
Expected: PASS (4 теста).

- [ ] **Step 5: Lint + commit**

```bash
.venv/bin/ruff check reviewer/tasks/boards/youtrack.py
git add reviewer/tasks/boards/youtrack.py tests/tasks/boards/test_youtrack_finish.py
git commit -m 'feat(tasks): YouTrackBoard.finish — описание + команда State'
```

---

### Task 4: MCP-тул `finish_task` (service + регистрация)

**Files:**
- Modify: `reviewer/mcp/service.py` (импорт `make_board_provider` + метод `finish_task`)
- Modify: `reviewer/entrypoints/mcp_server.py` (регистрация тула; счётчик тулов в docstring)
- Test: `tests/mcp/test_finish_task.py` (создать)

**Interfaces:**
- Consumes: `provider.finish(...)` (Task 2/3); `settings.configured_board_types() -> list[str]`; `make_board_provider(settings, board_type) -> TaskBoardProvider | None`.
- Produces: `MCPReviewService.finish_task(key, pr_url, note=None, mark_done=True, board_type=None, done_state=None) -> dict` → `{"status": "ok"|"error", "board_type"?, ...provider.finish result}`.

- [ ] **Step 1: Write the failing tests** — создать `tests/mcp/test_finish_task.py`:

```python
import reviewer.mcp.service as svc_mod
from reviewer.mcp.service import MCPReviewService


class _Provider:
    def __init__(self):
        self.finished = None
        self.closed = False

    def finish(self, key, pr_url, *, note=None, mark_done=True, done_state=None):
        self.finished = (key, pr_url, note, mark_done, done_state)
        return {"key": key, "board_id": "u1", "done_set": True,
                "pr_link_added": True, "already_closed": False, "warnings": []}

    def close(self):
        self.closed = True


class _Svc(MCPReviewService):
    """Обходим тяжёлый __init__: только settings с configured_board_types."""
    def __init__(self, configured):
        self.settings = type("S", (), {
            "configured_board_types": staticmethod(lambda: configured)})()


def test_finish_task_resolves_single_board(monkeypatch):
    prov = _Provider()
    monkeypatch.setattr(svc_mod, "make_board_provider", lambda s, t: prov)
    out = _Svc(["yougile"]).finish_task("PRI-10", "https://github.com/o/r/pull/7")
    assert out["status"] == "ok"
    assert out["board_type"] == "yougile"
    assert out["done_set"] is True
    assert prov.finished == ("PRI-10", "https://github.com/o/r/pull/7", None, True, None)
    assert prov.closed is True


def test_finish_task_no_board_configured():
    out = _Svc([]).finish_task("PRI-10", "url")
    assert out["status"] == "error"
    assert "board_type" in out["reason"]


def test_finish_task_ambiguous_requires_board_type():
    out = _Svc(["yougile", "youtrack"]).finish_task("PRI-10", "url")
    assert out["status"] == "error"


def test_finish_task_explicit_board_type(monkeypatch):
    prov = _Provider()
    monkeypatch.setattr(svc_mod, "make_board_provider", lambda s, t: prov)
    out = _Svc(["yougile", "youtrack"]).finish_task(
        "TES-1", "url", board_type="youtrack", done_state="Done")
    assert out["status"] == "ok"
    assert out["board_type"] == "youtrack"
    assert prov.finished[4] == "Done"


def test_finish_task_failsoft(monkeypatch):
    class Boom:
        def finish(self, *a, **k):
            raise RuntimeError("kaboom")

        def close(self):
            pass

    monkeypatch.setattr(svc_mod, "make_board_provider", lambda s, t: Boom())
    out = _Svc(["yougile"]).finish_task("PRI-10", "url")
    assert out["status"] == "error"
    assert "kaboom" in out["reason"]
```

- [ ] **Step 2: Run to verify FAIL**

Run: `.venv/bin/pytest tests/mcp/test_finish_task.py -q`
Expected: FAIL — `AttributeError: 'MCPReviewService' object has no attribute 'finish_task'` (или ImportError на `make_board_provider`).

- [ ] **Step 3: Add module import** — в `reviewer/mcp/service.py`, рядом с другими импортами вверху файла добавить:

```python
from reviewer.tasks.boards import make_board_provider
```

- [ ] **Step 4: Implement `finish_task`** — в `reviewer/mcp/service.py`, добавить метод в класс `MCPReviewService` (например, сразу после `sync_board`):

```python
    def finish_task(self, key: str, pr_url: str, note: str | None = None,
                    mark_done: bool = True, board_type: str | None = None,
                    done_state: str | None = None) -> dict:
        """Закрыть задачу на доске (server-side write): пометить done + дописать
        PR-ссылку в описание. Резолвит провайдера по board_type (или единственному
        настроенному), fail-soft. Креды из env; наружу не отдаются."""
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
        provider = make_board_provider(self.settings, board_type)
        if provider is None:
            return {"status": "error", "reason": f"board '{board_type}' not configured"}
        try:
            result = provider.finish(key, pr_url, note=note, mark_done=mark_done,
                                     done_state=done_state)
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

- [ ] **Step 5: Register the MCP tool** — в `reviewer/entrypoints/mcp_server.py`, после блока `sync_board` (строка ~114) добавить:

```python
    @mcp.tool()
    def finish_task(key: str, pr_url: str, note: str | None = None,
                    mark_done: bool = True, board_type: str | None = None,
                    done_state: str | None = None) -> dict:
        """Close a task on the board after its PR is created (server-side write):
        idempotently append the PR link to the description and mark it done, so the
        task's last-modified bumps and the next sync_board re-indexes the updated task.
        board_type and done_state come from the repo's .review.yml (YouGile ignores
        done_state — it has a boolean completed; YouTrack sets State via command,
        default 'Fixed'). Credentials come from env; fail-soft."""
        return service.finish_task(key, pr_url, note, mark_done, board_type, done_state)
```

- [ ] **Step 6: Bump tool-count docstring** — в `reviewer/entrypoints/mcp_server.py`, в docstring `create_server` (строка ~19) заменить `с 32 тулами` на `с 33 тулами`. Затем проверить, не пинит ли тест число тулов:

Run: `grep -rn "32" tests/mcp/test_server.py`
Если найдётся ассерт на `32` тула — обновить на `33` в том же тесте.

- [ ] **Step 7: Run to verify PASS**

Run: `.venv/bin/pytest tests/mcp/test_finish_task.py tests/mcp/test_server.py -q`
Expected: PASS.

- [ ] **Step 8: Lint + commit**

```bash
.venv/bin/ruff check reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py
git add reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp/test_finish_task.py
git commit -m 'feat(mcp): тул finish_task — server-side закрытие задачи на доске'
```

---

### Task 5: Скилл `finish-task` + указатель из solve-task + docs + guard-тесты

**Files:**
- Create: `plugin/skills/finish-task/SKILL.md`
- Modify: `plugin/skills/solve-task/SKILL.md` (указатель на finish-task в конце Step 5)
- Modify: `.review.yml` (закомментированный пример `done_state`)
- Modify: `CLAUDE.md` (факт про finish_task в «Неочевидные факты»)
- Test: `tests/skills/test_finish_task_skill.py` (создать)

**Interfaces:**
- Consumes: MCP-тулы `finish_task(...)` (Task 4), `sync_board(...)`, `get_board_config()`.

- [ ] **Step 1: Write the failing guard tests** — создать `tests/skills/test_finish_task_skill.py`:

```python
"""Guardrail: скилл finish-task — тонкий триггер server-side тула finish_task."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "plugin" / "skills" / "finish-task" / "SKILL.md"
SOLVE = ROOT / "plugin" / "skills" / "solve-task" / "SKILL.md"


def test_finish_task_calls_write_tool_and_resyncs():
    t = SKILL.read_text(encoding="utf-8")
    assert "finish_task(" in t          # зовёт серверный write-тул
    assert "sync_board(" in t           # ре-индекс закрытой задачи после записи


def test_finish_task_confirms_and_noops_boardless():
    t = SKILL.read_text(encoding="utf-8").lower()
    assert "confirm" in t                # никогда не пишет молча
    assert "board-less" in t or "no-op" in t   # graceful no-op без ключа/доски


def test_finish_task_resolves_key_and_pr():
    t = SKILL.read_text(encoding="utf-8")
    assert "key_pattern" in t            # резолв ключа по паттерну
    assert "briefs" in t                 # восстановление ключа из брифа
    assert "gh pr view" in t             # резолв pr_url


def test_solve_task_points_to_finish_task():
    assert "finish-task" in SOLVE.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run to verify FAIL**

Run: `.venv/bin/pytest tests/skills/test_finish_task_skill.py -q`
Expected: FAIL — `FileNotFoundError` (SKILL.md ещё нет) / нет токена в solve-task.

- [ ] **Step 3: Create the skill** — создать `plugin/skills/finish-task/SKILL.md` с содержимым:

````markdown
---
name: finish-task
description: After a task's PR is created, offer to close the task on the board — append the PR link to the task description and mark it done — so the reviewer's incremental sync re-indexes the updated task. Use when the user says the PR is up / asks to close/finish the task ("закрой задачу", "PR готов", "finish the task", "mark task done", "заверши задачу"). Server-side write via the reviewer MCP tool finish_task (works in any client). Requires the reviewer MCP server + a configured board.
---

# Finish Task

After a PR is created for a task, close that task on the board: idempotently append the
PR link to its description and mark it done. The write bumps the task's last-modified, so
the next `sync_board` re-indexes the updated task (done status + PR edge). Reply to the user
in Russian.

## Pipeline

1. **Config.** Read the `task_board` block (`type`, `project`, `done_state`) from the repo's
   `.review.yml`; if there is no block, fall back to `get_board_config()`. No board resolved /
   board MCP not needed here (write is server-side) — but no board type at all → **board-less no-op**:
   tell the user (in Russian) the task is not linked to a board and stop.

2. **Resolve the task key.** In order, stop at the first hit:
   - current branch: `git branch --show-current`, match the board's `key_pattern` (e.g. `PRI-\d+`);
   - the most recent brief: newest `docs/superpowers/briefs/*<KEY>*.md` (its heading carries the key);
   - the PR body/title (`gh pr view --json title,body`), match `key_pattern`;
   - else ask the user for the key. No key → **no-op** (nothing to close).

3. **Resolve the PR URL.** `gh pr view --json url -q .url` (GitHub) or `glab mr view` (GitLab).
   If none is found, ask the user for the PR URL.

4. **Offer + confirm.** Show what will be written — the PR link + "mark done" + any optional note —
   and ask the user to **confirm** before writing. Ask whether they want to add an optional note
   (details under the task). **Never write to the board silently.**

5. **Write.** Call `finish_task(key=<key>, pr_url=<url>, note=<note or null>, board_type=<type>,
   done_state=<done_state or null>)`. `status == "error"` → report the reason (in Russian), fail-open.

6. **Re-index.** Call `sync_board(board=<project or null>, board_type=<type>)` (incremental) so the
   just-closed task is re-indexed (its last-modified is now past the cursor). Cheap when the corpus is warm.

7. **Report.** Tell the user (in Russian) what was written (done + PR link) and the sync result. If
   `already_closed` is true, say the task was already closed (no duplicate PR link added).

## Failure handling (fail-open)

- No board configured / no task key → board-less no-op with a short Russian note; never abort.
- `finish_task` error (board unreachable, key unresolved on the board, State command failed) → report
  the reason and stop; the PR is already created, the board write is a secondary effect.
- Read-only intent everywhere except the single `finish_task` write, which is explicitly confirmed.
````

- [ ] **Step 4: Point solve-task at finish-task** — в `plugin/skills/solve-task/SKILL.md`, в конце секции `5. **Hand off to development.**` (последний абзац перед `## Failure handling`) добавить абзац:

```markdown
   **After the PR is created (later in the dev cycle):** offer to close the task with the
   `/reviewer_finish-task` skill — it appends the PR link to the task and marks it done (bumping
   last-modified so the sync re-indexes the closed task). Skip in board-less mode (no task key).
```

- [ ] **Step 5: Document `done_state` in `.review.yml`** — в `.review.yml`, в блок `task_board:` добавить закомментированную строку (этот репо на yougile — ключ документирует knob для youtrack-репозиториев):

```yaml
  # done_state: Fixed                 # YouTrack: значение State для «выполнено» в finish-task (дефолт Fixed); YouGile игнорирует
```

- [ ] **Step 6: Document the invariant in CLAUDE.md** — в `CLAUDE.md`, в раздел «Неочевидные факты» добавить пункт:

```markdown
- **Закрытие задачи после PR (`finish_task`).** Скилл `/reviewer_finish-task` после создания PR
  предлагает закрыть задачу на доске: идемпотентно дописывает PR-ссылку в описание и помечает
  выполненной через server-side MCP-тул `finish_task` (креды в env, портируется во все клиенты).
  YouGile — `completed:true` (+ `normalize_yougile` мапит `completed→status="done"`); YouTrack —
  команда `State <done_state>` (`task_board.done_state` в `.review.yml`, дефолт `Fixed`). Любая
  правка двигает last-modified (`timestamp`/`updated`) → следующий `sync_board` сохраняет
  обновлённую задачу. **Это расширяет разворот инварианта «reviewer Python не трогает доску»:
  теперь Python пишет в доску не только болк-синком, но и одиночным `finish_task`.**
```

- [ ] **Step 7: Run to verify PASS**

Run: `.venv/bin/pytest tests/skills/test_finish_task_skill.py -q`
Expected: PASS (4 теста).

- [ ] **Step 8: Full test sweep + lint**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
```
Expected: PASS (новые тесты зелёные; ruff чист по изменённым файлам — repo-wide ruff мог быть не чист и до правок, ориентируйся на свои файлы).

- [ ] **Step 9: Commit**

```bash
git add plugin/skills/finish-task/SKILL.md plugin/skills/solve-task/SKILL.md .review.yml CLAUDE.md tests/skills/test_finish_task_skill.py
git commit -m 'feat(skills): скилл finish-task + указатель из solve-task + docs'
```

---

### Task 6: Live-acceptance на обеих досках (ручная проверка)

Не автоматизируется (нужны живые доски + токены в env reviewer-mcp). Выполняется с пользователем на **выбрасываемых** задачах. Прогнать дважды: YouGile (проект PRI, текущее репо) и YouTrack (проект TES, тестовое репо).

**Precondition:** env reviewer-mcp имеет `YOUGILE_API_KEY` и `YOUTRACK_TOKEN` + `YOUTRACK_BASE_URL` → перезапустить/переподключить reviewer-mcp, чтобы `configured_board_types()` содержал оба типа.

- [ ] **Step 1: Выбрать тестовую задачу и зафиксировать «до».** Для доски вызвать `get_task(key=<K>, project=<P>)` — записать текущий `status` (≠ done) и убедиться, что PR-ссылки в описании нет.

- [ ] **Step 2: Закрыть.** Вызвать `finish_task(key=<K>, pr_url="https://github.com/mimfort/rag_for_git/pull/<N>", board_type=<type>, done_state=<для youtrack, напр. подобрать из состояний проекта TES>)`. Ожидание: `status=="ok"`, `pr_link_added==true`, `done_set==true`.

- [ ] **Step 3: Проверить доску вручную.** В UI: у YouGile задача отмечена completed + в описании строка `PR: …`; у YouTrack State = целевое + PR-ссылка в описании.

- [ ] **Step 4: Ре-синк.** `sync_board(board=<P>, board_type=<type>)`. Ожидание: `changed >= 1` (last-modified > cursor).

- [ ] **Step 5: Проверить, что стор сохранил обновление.** `get_task(key=<K>, project=<P>)` → `status=="done"` И описание содержит PR-ссылку. `get_task_context(key=<K>, project=<P>)` → PR-ребро (extract_pr_refs из описания).

- [ ] **Step 6: Идемпотентность.** Повторить `finish_task(...)` с тем же PR-URL. Ожидание: `pr_link_added==false` (YouGile: `already_closed==true`, PUT не шлётся); дублей PR-ссылки в описании нет.

- [ ] **Step 7: YouTrack done_state.** Если на Step 2 команда `State Fixed` вернула warning (нет такого состояния в TES) — подобрать корректное имя из состояний проекта, прописать `task_board.done_state` в `.review.yml` тестового репо и повторить Step 2. Зафиксировать финальное значение.

- [ ] **Step 8: Отчёт пользователю** — что прошло на каждой доске, финальный `done_state` для TES, любые расхождения (напр. метод YouGile PUT vs PATCH из примечания Task 2).

---

## Self-Review

**Spec coverage:**
- §3.1 write-метод `finish` на протоколе + YouGile/YouTrack → Tasks 2, 3. ✓
- §3.2 `RawTask.completed` + `normalize_yougile` done → Task 1. ✓
- §3.3 MCP-тул `finish_task` + регистрация → Task 4. ✓
- §3.4 скилл `finish-task` → Task 5. ✓
- §3.5 указатель из solve-task → Task 5 Step 4. ✓
- §5 конфиг `task_board.done_state` → Task 5 Step 5 (+ читается скиллом, передаётся в finish_task). ✓
- §6 идемпотентность/no-op/confirm/fail-soft → Tasks 2,3 (idempotent tests), 4 (failsoft/no-board), 5 (skill confirm/no-op). ✓
- §7 креды/инвариант → Task 4 (env creds) + Task 5 Step 6 (CLAUDE.md). ✓
- §8.1 unit → Tasks 1–5 tests. §8.2 live → Task 6. ✓
- §9 риски (YouGile PUT/PATCH, done_state в TES, resolve кода) → примечания Task 2 + Task 6 Steps 7–8. ✓

**Placeholder scan:** нет TBD/«handle errors»; весь код и команды приведены дословно.

**Type consistency:** `finish(key, pr_url, *, note, mark_done, done_state) -> {key, board_id, done_set, pr_link_added, already_closed, warnings}` одинаков в base.py Protocol, YougileBoard, YouTrackBoard, фейках тестов и `finish_task`. `finish_task(key, pr_url, note, mark_done, board_type, done_state)` совпадает в service.py и mcp_server.py. `configured_board_types()`/`make_board_provider(settings, type)` — по фактическим сигнатурам (settings.py:173, boards/__init__.py:10).
