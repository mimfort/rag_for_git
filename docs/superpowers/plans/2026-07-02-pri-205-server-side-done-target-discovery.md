# PRI-205 Server-side discovery done-цели доски — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only server-side reviewer MCP tool `get_board_targets(board_type, project)` that returns done-target candidates (YouGile board columns / YouTrack status fields + values) so `configure-review` shows a pick-list instead of manual entry and drops its dependency on the client-side yougile MCP; `finish-task` then names the resolved done target explicitly.

**Architecture:** One board-agnostic Protocol method `list_done_targets(project)` implemented per board (YouGile walks projects→boards→columns, scoping boards to the project by task code-prefix — equivalent to the spec's task-sampling, reusing `iter_raw`'s walk; YouTrack tries the admin customFields API then falls back to aggregating distinct values from a sample of project issues). A thin `MCPReviewService.get_board_targets` resolves the provider from env creds (mirroring `finish_task`), calls the method fail-soft, and never returns credentials. Two skills consume/mention it.

**Tech Stack:** Python 3.11+, httpx (mocked in unit tests), FastMCP, pytest, ruff. Design spec: `docs/superpowers/specs/2026-07-02-pri-205-server-side-done-target-discovery-design.md`.

## Global Constraints

- **Language:** all new comments/docstrings/CLI text in Russian (project convention). `SKILL.md` bodies stay English, but instruct answering the user in Russian.
- **Credentials only in env** — never in `.review.yml`, never in `get_board_targets` return.
- **Discovery is read-only** — no writes to the board; no task moves.
- **Repo-agnostic server** — the server never parses `.review.yml`; `board_type`+`project` arrive as tool params.
- **Fail-soft everywhere** — board/creds/permission/network failure → empty list + `warnings`, never raise, never block.
- **Tests:** unit tests mock httpx (no network); `pytest` excludes `integration` by default.
- **Ruff:** line-length 100, target py311. Run `.venv/bin/ruff check <touched files>` (repo-wide may be pre-dirty — only new/edited files must be clean).
- **Commits:** Conventional Commits in Russian, **no self-attribution** (no `Co-Authored-By`/Claude).
- **Session scope:** units 4.1–4.6 + §7.1 tests + version bump `0.2.23`→`0.2.24`. Out of session: PyPI publish, `reviewer update`, live acceptance (§7.2).

---

### Task 1: YouGile `list_done_targets` + Protocol method

**Files:**
- Modify: `reviewer/tasks/boards/base.py` (add `list_done_targets` to the `TaskBoardProvider` Protocol, after `finish`)
- Modify: `reviewer/tasks/boards/yougile.py` (add `list_done_targets` method to `YougileBoard`, after `_resolve_column_id`/`finish`)
- Test: `tests/tasks/boards/test_yougile_targets.py` (create)

**Interfaces:**
- Produces: `YougileBoard.list_done_targets(self, project: str | None) -> dict` returning
  `{"columns": [{"title": str, "id": str, "board_id": str, "board_title": str}], "warnings": [str]}`.
- Consumes: existing `self._get_all(path, params)` (`yougile.py:139`) and `project_prefix` (`base.py:17`, already imported in yougile.py), module `log`.

- [ ] **Step 1: Write the failing test**

Create `tests/tasks/boards/test_yougile_targets.py`:

```python
from reviewer.tasks.boards.yougile import YougileBoard


class _Resp:
    def __init__(self, status=200, content=None):
        self.status_code = status
        self._content = content if content is not None else []

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return {"content": self._content}


class _Client:
    """Фейк httpx: роутит GET по (path + дискриминирующий param). Неизвестный путь → 500
    (для проверки fail-soft). _get_all читает .json()['content']."""

    def __init__(self, routes):
        self.routes = routes  # ключ "path" или "path?disc=val" -> list[dict]
        self.calls = []

    def get(self, path, params=None):
        params = params or {}
        self.calls.append((path, dict(params)))
        for disc in ("projectId", "boardId", "columnId"):
            if disc in params:
                key = f"{path}?{disc}={params[disc]}"
                return _Resp(200, self.routes[key]) if key in self.routes else _Resp(500)
        return _Resp(200, self.routes[path]) if path in self.routes else _Resp(500)

    def close(self):
        pass


def _board(routes):
    b = YougileBoard.__new__(YougileBoard)  # обойти httpx.Client в __init__
    b._client = _Client(routes)
    return b


_TWO_BOARDS = {
    "/projects": [{"id": "p1", "title": "Proj"}],
    "/boards?projectId=p1": [{"id": "b1", "title": "Board One"},
                             {"id": "b2", "title": "Board Two"}],
    "/columns?boardId=b1": [{"id": "c1", "title": "В работе"},
                            {"id": "c2", "title": "Готово"}],
    "/columns?boardId=b2": [{"id": "c3", "title": "Todo"},
                            {"id": "c4", "title": "Done"}],
    "/tasks?columnId=c1": [{"idTaskProject": "PRI-1"}],  # b1 хостит PRI
    "/tasks?columnId=c2": [],
    "/tasks?columnId=c3": [{"idTaskProject": "TES-1"}],  # b2 хостит только TES
    "/tasks?columnId=c4": [],
}


def test_yougile_targets_scopes_to_project_boards():
    res = _board(_TWO_BOARDS).list_done_targets("PRI")
    titles = {(c["title"], c["board_title"]) for c in res["columns"]}
    assert titles == {("В работе", "Board One"), ("Готово", "Board One")}
    assert res["warnings"] == []
    assert all(c["board_id"] == "b1" for c in res["columns"])


def test_yougile_targets_empty_project_returns_all_boards():
    b = _board(_TWO_BOARDS)
    res = b.list_done_targets(None)
    assert {c["title"] for c in res["columns"]} == {"В работе", "Готово", "Todo", "Done"}
    # без project задачи не сканируются вовсе
    assert not any(path == "/tasks" for path, _ in b._client.calls)


def test_yougile_targets_no_project_boards_warns():
    res = _board(_TWO_BOARDS).list_done_targets("ZZZ")
    assert res["columns"] == []
    assert res["warnings"]  # «колонки для проекта 'ZZZ' не найдены»


def test_yougile_targets_failsoft_on_error():
    # отсутствует роут /boards?projectId=p1 → 500 внутри обхода → warning, без падения
    routes = {"/projects": [{"id": "p1", "title": "Proj"}]}
    res = _board(routes).list_done_targets("PRI")
    assert res["columns"] == []
    assert res["warnings"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/tasks/boards/test_yougile_targets.py -q`
Expected: FAIL with `AttributeError: 'YougileBoard' object has no attribute 'list_done_targets'`.

- [ ] **Step 3: Add the Protocol method to `base.py`**

In `reviewer/tasks/boards/base.py`, inside `class TaskBoardProvider(Protocol)`, add after the `finish` method:

```python
    def list_done_targets(self, project: str | None) -> dict:
        """Кандидаты done-цели доски (read-only, fail-soft, НИКОГДА не бросает).

        YouGile → {"columns": [{"title", "id", "board_id", "board_title"}], "warnings": [...]}
        YouTrack → {"status_fields": [{"field", "values": [...], "$type"?}],
                    "source": "admin"|"sample", "warnings": [...]}

        Ошибка/нет прав/сеть → пустой список + warnings (скилл откатывается на ручной ввод)."""
        ...
```

- [ ] **Step 4: Implement `list_done_targets` in `yougile.py`**

In `reviewer/tasks/boards/yougile.py`, add to `class YougileBoard` (after `finish`):

```python
    def list_done_targets(self, project: str | None) -> dict:
        """Колонки досок проекта (read-only, fail-soft). project — код-префикс задач
        (напр. PRI): доска включается, если на ней есть хоть одна задача проекта. Пустой
        project → все доски всех проектов. НИКОГДА не бросает."""
        warnings: list[str] = []
        boards: list[dict] = []           # [{board_id, board_title, columns:[{id,title}]}]
        hosts: set[str] = set()           # board_id, где встречена задача проекта
        scanned = 0
        _CAP = 500                        # предохранитель на число просканированных задач
        try:
            for proj in self._get_all("/projects"):
                for brd in self._get_all("/boards", {"projectId": proj["id"]}):
                    bid = brd["id"]
                    cols = [{"id": c["id"], "title": c.get("title", "")}
                            for c in self._get_all("/columns", {"boardId": bid})]
                    boards.append({"board_id": bid, "board_title": brd.get("title", ""),
                                   "columns": cols})
                    if not project:
                        continue
                    for c in cols:
                        if scanned >= _CAP:
                            break
                        hit = False
                        for t in self._get_all("/tasks", {"columnId": c["id"]}):
                            scanned += 1
                            if project_prefix(t.get("idTaskProject", "")) == project:
                                hit = True
                                break
                            if scanned >= _CAP:
                                break
                        if hit:
                            hosts.add(bid)
                            break
        except Exception:
            log.warning("yougile: discovery колонок не удался", exc_info=True)
            warnings.append("не удалось перечислить колонки доски")
        kept = boards if not project else [b for b in boards if b["board_id"] in hosts]
        columns = [{"title": col["title"], "id": col["id"],
                    "board_id": b["board_id"], "board_title": b["board_title"]}
                   for b in kept for col in b["columns"]]
        if project and not hosts and not warnings:
            warnings.append(f"колонки для проекта {project!r} не найдены")
        return {"columns": columns, "warnings": warnings}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/tasks/boards/test_yougile_targets.py -q`
Expected: PASS (4 tests).

- [ ] **Step 6: Lint**

Run: `.venv/bin/ruff check reviewer/tasks/boards/yougile.py reviewer/tasks/boards/base.py tests/tasks/boards/test_yougile_targets.py`
Expected: no errors on these files.

- [ ] **Step 7: Commit**

```bash
git add reviewer/tasks/boards/base.py reviewer/tasks/boards/yougile.py tests/tasks/boards/test_yougile_targets.py
git commit -m "feat(tasks): YouGile list_done_targets — discovery колонок доски проекта (PRI-205)"
```

---

### Task 2: YouTrack `list_done_targets` (try-admin → fallback aggregation)

**Files:**
- Modify: `reviewer/tasks/boards/youtrack.py` (add `_SAMPLE` constant; add `list_done_targets` + two private helpers to `YouTrackBoard`)
- Test: `tests/tasks/boards/test_youtrack_targets.py` (create)

**Interfaces:**
- Produces: `YouTrackBoard.list_done_targets(self, project: str | None) -> dict` returning
  `{"status_fields": [{"field": str, "values": [str], "$type": str | None}], "source": "admin"|"sample", "warnings": [str]}`.
- Consumes: `self._client.get(path, params)`, `quote` (already imported), module `log`.

- [ ] **Step 1: Write the failing test**

Create `tests/tasks/boards/test_youtrack_targets.py`:

```python
from reviewer.tasks.boards.youtrack import YouTrackBoard


class _Resp:
    def __init__(self, status=200, json_data=None):
        self.status_code = status
        self._json = json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class _Client:
    """Фейк httpx: роутит GET по path (admin id уже в пути). .json() отдаёт список."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        r = self.routes.get(path)
        if r is None:
            return _Resp(500, None)
        return r

    def close(self):
        pass


def _board(routes):
    b = YouTrackBoard.__new__(YouTrackBoard)  # обойти httpx.Client в __init__
    b._client = _Client(routes)
    b._status_field = "State"
    return b


def test_youtrack_targets_admin_success():
    routes = {
        "/admin/projects": _Resp(200, [{"id": "0-5", "shortName": "TES"}]),
        "/admin/projects/0-5/customFields": _Resp(200, [
            {"$type": "StateProjectCustomField", "field": {"name": "Stage"},
             "bundle": {"values": [{"name": "Open"}, {"name": "Готово"}]}},
            {"$type": "TextProjectCustomField", "field": {"name": "Descr"},
             "bundle": None},  # не bundle-поле → пропуск
        ]),
    }
    res = _board(routes).list_done_targets("TES")
    assert res["source"] == "admin"
    assert res["status_fields"] == [
        {"field": "Stage", "values": ["Open", "Готово"], "$type": "StateProjectCustomField"}]
    assert res["warnings"] == []


def test_youtrack_targets_fallback_to_sample_on_admin_403():
    routes = {
        "/admin/projects": _Resp(403, None),  # нет admin-прав
        "/issues": _Resp(200, [
            {"customFields": [{"name": "Stage", "value": {"name": "Open"},
                               "$type": "StateIssueCustomField"}]},
            {"customFields": [{"name": "Stage", "value": {"name": "Готово"},
                               "$type": "StateIssueCustomField"}]},
        ]),
    }
    res = _board(routes).list_done_targets("TES")
    assert res["source"] == "sample"
    assert res["status_fields"] == [
        {"field": "Stage", "values": ["Open", "Готово"], "$type": "StateIssueCustomField"}]
    assert res["warnings"]  # предупреждение про недоступный admin


def test_youtrack_targets_sample_ignores_non_dict_values():
    routes = {
        "/admin/projects": _Resp(403, None),
        "/issues": _Resp(200, [
            {"customFields": [{"name": "Stage", "value": {"name": "Open"}},
                              {"name": "Assignee", "value": "текст"},   # не dict → пропуск
                              {"name": "Sprints", "value": [{"name": "S1"}]}]},  # list → пропуск
        ]),
    }
    res = _board(routes).list_done_targets("TES")
    assert [f["field"] for f in res["status_fields"]] == ["Stage"]


def test_youtrack_targets_total_failure_empty_failsoft():
    routes = {"/admin/projects": _Resp(403, None), "/issues": _Resp(500, None)}
    res = _board(routes).list_done_targets("TES")
    assert res["status_fields"] == []
    assert res["source"] == "sample"
    assert len(res["warnings"]) >= 1


def test_youtrack_targets_empty_project_uses_sample_no_admin_call():
    routes = {"/issues": _Resp(200, [
        {"customFields": [{"name": "State", "value": {"name": "Open"},
                           "$type": "StateIssueCustomField"}]}])}
    b = _board(routes)
    res = b.list_done_targets(None)
    assert res["source"] == "sample"
    assert [f["field"] for f in res["status_fields"]] == ["State"]
    assert not any(path.startswith("/admin") for path, _ in b._client.calls)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/tasks/boards/test_youtrack_targets.py -q`
Expected: FAIL with `AttributeError: 'YouTrackBoard' object has no attribute 'list_done_targets'`.

- [ ] **Step 3: Add the `_SAMPLE` constant**

In `reviewer/tasks/boards/youtrack.py`, next to the existing `_PAGE` module constant, add:

```python
_SAMPLE = 200  # число задач в выборке для fallback-агрегации значений полей статуса
```

- [ ] **Step 4: Implement the method + helpers in `youtrack.py`**

Add to `class YouTrackBoard` (after `finish`):

```python
    def list_done_targets(self, project: str | None) -> dict:
        """Поля статуса + значения (read-only, fail-soft). Try admin customFields;
        при недоступности — агрегация distinct значений из выборки задач проекта.
        НИКОГДА не бросает."""
        warnings: list[str] = []
        try:
            fields = self._admin_status_fields(project)
            if fields:
                return {"status_fields": fields, "source": "admin", "warnings": warnings}
        except Exception:
            log.warning("youtrack: admin customFields недоступны — fallback", exc_info=True)
            warnings.append("admin customFields недоступны (нет прав?) — "
                            "значения собраны из задач")
        try:
            fields = self._sampled_status_fields(project)
        except Exception:
            log.warning("youtrack: discovery полей из выборки не удался", exc_info=True)
            warnings.append("не удалось собрать поля статуса из задач")
            fields = []
        return {"status_fields": fields, "source": "sample", "warnings": warnings}

    def _admin_status_fields(self, project: str | None) -> list[dict]:
        """Bundle-поля (state/enum) проекта из admin API + их значения. [] если project пуст
        или проект не найден. Бросает при ошибке HTTP — вызывающий ловит и фолбэкает."""
        if not project:
            return []
        pr = self._client.get("/admin/projects",
                              params={"fields": "id,shortName", "query": project})
        pr.raise_for_status()
        pid = next((p["id"] for p in (pr.json() or [])
                    if p.get("shortName") == project), None)
        if not pid:
            return []
        r = self._client.get(
            f"/admin/projects/{quote(str(pid), safe='')}/customFields",
            params={"fields": "field(name),$type,bundle(values(name,$type))"})
        r.raise_for_status()
        out: list[dict] = []
        for pcf in (r.json() or []):
            bundle = pcf.get("bundle") or {}
            values = [v.get("name") for v in (bundle.get("values") or []) if v.get("name")]
            if not values:
                continue  # не bundle-поле — не кандидат статуса
            name = (pcf.get("field") or {}).get("name")
            if name:
                out.append({"field": name, "values": values, "$type": pcf.get("$type")})
        return out

    def _sampled_status_fields(self, project: str | None) -> list[dict]:
        """Distinct значения single-value кастом-полей из выборки задач проекта.
        Бросает при ошибке HTTP — вызывающий ловит."""
        params: dict = {"fields": "customFields(name,value(name),$type)", "$top": _SAMPLE}
        if project:
            params["query"] = f"project: {project}"
        r = self._client.get("/issues", params=params)
        r.raise_for_status()
        agg: dict[str, dict] = {}  # field name -> {"values": [...], "$type": ...}
        for issue in (r.json() or []):
            for cf in issue.get("customFields") or []:
                name = cf.get("name")
                val = cf.get("value")
                vname = val.get("name") if isinstance(val, dict) else None
                if not name or not vname:
                    continue
                slot = agg.setdefault(name, {"values": [], "$type": cf.get("$type")})
                if vname not in slot["values"]:
                    slot["values"].append(vname)
        return [{"field": n, "values": s["values"], "$type": s["$type"]}
                for n, s in agg.items()]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/tasks/boards/test_youtrack_targets.py -q`
Expected: PASS (5 tests).

- [ ] **Step 6: Lint**

Run: `.venv/bin/ruff check reviewer/tasks/boards/youtrack.py tests/tasks/boards/test_youtrack_targets.py`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add reviewer/tasks/boards/youtrack.py tests/tasks/boards/test_youtrack_targets.py
git commit -m "feat(tasks): YouTrack list_done_targets — admin customFields + fallback-агрегация (PRI-205)"
```

---

### Task 3: MCP tool `get_board_targets` (service + server)

**Files:**
- Modify: `reviewer/mcp/service.py` (add `get_board_targets` method after `finish_task`, ~line 356)
- Modify: `reviewer/entrypoints/mcp_server.py` (register `@mcp.tool() get_board_targets`, near `get_board_config` at ~line 166, before `return mcp` at line 317)
- Test: `tests/mcp/test_get_board_targets.py` (create)

**Interfaces:**
- Consumes: `make_board_provider(self.settings, board_type)` (already imported, `service.py:31`), `self.settings.configured_board_types()`, `provider.list_done_targets(project)` (Tasks 1–2).
- Produces: `MCPReviewService.get_board_targets(self, board_type: str | None = None, project: str | None = None) -> dict` → `{"board_type", "project", **targets}` on success, or `{"status": "error", "reason": …}`.

- [ ] **Step 1: Write the failing test**

Create `tests/mcp/test_get_board_targets.py`:

```python
import reviewer.mcp.service as svc_mod
from reviewer.mcp.service import MCPReviewService


class _Provider:
    def __init__(self, targets):
        self.targets = targets
        self.project = "UNSET"
        self.closed = False

    def list_done_targets(self, project):
        self.project = project
        return self.targets

    def close(self):
        self.closed = True


class _Svc(MCPReviewService):
    """Обходим тяжёлый __init__: только settings с configured_board_types."""
    def __init__(self, configured):
        self.settings = type("S", (), {
            "configured_board_types": staticmethod(lambda: configured)})()


def test_get_board_targets_single_board_threads_project(monkeypatch):
    prov = _Provider({"columns": [{"title": "Готово", "id": "c1",
                                   "board_id": "b1", "board_title": "B"}], "warnings": []})
    monkeypatch.setattr(svc_mod, "make_board_provider", lambda s, t: prov)
    out = _Svc(["yougile"]).get_board_targets(project="PRI")
    assert out["board_type"] == "yougile"
    assert out["project"] == "PRI"
    assert out["columns"][0]["title"] == "Готово"
    assert prov.project == "PRI"
    assert prov.closed is True
    # креды наружу не отдаются
    assert "api_key" not in out and "token" not in out


def test_get_board_targets_ambiguous_requires_type():
    out = _Svc(["yougile", "youtrack"]).get_board_targets()
    assert out["status"] == "error"
    assert "board_type" in out["reason"]


def test_get_board_targets_not_configured():
    out = _Svc([]).get_board_targets(board_type="youtrack")
    assert out["status"] == "error"


def test_get_board_targets_explicit_type(monkeypatch):
    prov = _Provider({"status_fields": [{"field": "Stage", "values": ["Готово"],
                                         "$type": "X"}], "source": "admin", "warnings": []})
    monkeypatch.setattr(svc_mod, "make_board_provider", lambda s, t: prov)
    out = _Svc(["yougile", "youtrack"]).get_board_targets(board_type="youtrack",
                                                          project="TES")
    assert out["board_type"] == "youtrack"
    assert out["source"] == "admin"
    assert prov.project == "TES"


def test_get_board_targets_failsoft(monkeypatch):
    class Boom:
        def list_done_targets(self, project):
            raise RuntimeError("kaboom")

        def close(self):
            pass

    monkeypatch.setattr(svc_mod, "make_board_provider", lambda s, t: Boom())
    out = _Svc(["yougile"]).get_board_targets()
    assert out["status"] == "error"
    assert "kaboom" in out["reason"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/mcp/test_get_board_targets.py -q`
Expected: FAIL with `AttributeError: 'MCPReviewService' object has no attribute 'get_board_targets'`.

- [ ] **Step 3: Implement `get_board_targets` in `service.py`**

In `reviewer/mcp/service.py`, add after the `finish_task` method (after line 356, before `_resolve_repo_branch`):

```python
    def get_board_targets(self, board_type: str | None = None,
                          project: str | None = None) -> dict:
        """Кандидаты done-цели доски для configure-review (read-only, server-side).

        Резолвит провайдера по board_type (или единственному настроенному), зовёт
        list_done_targets(project) fail-soft. YouGile → columns; YouTrack → status_fields
        (+source). Креды из env; наружу НЕ отдаются."""
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
            targets = provider.list_done_targets(project)
        except Exception as e:  # fail-soft: discovery — вторичная функция
            log.warning("get_board_targets: сбой discovery", exc_info=True)
            return {"status": "error", "reason": f"{type(e).__name__}: {e}"}
        finally:
            try:
                provider.close()
            except Exception:
                pass
        return {"board_type": board_type, "project": project, **targets}
```

- [ ] **Step 4: Register the MCP tool in `mcp_server.py`**

In `reviewer/entrypoints/mcp_server.py`, add after the `get_board_config` tool (after line 173), before `return mcp`:

```python
    @mcp.tool()
    def get_board_targets(board_type: str | None = None,
                          project: str | None = None) -> dict:
        """Discover done-target candidates for a repo's board, server-side (read-only).
        YouGile → board columns; YouTrack → status fields + their values (bundle via the
        admin API, else aggregated from a sample of project issues). board_type and
        project come from the repo's .review.yml task_board block. Credentials are NEVER
        returned; fail-soft — empty list + warnings when the board/creds/permissions are
        unavailable, so configure-review can fall back to asking."""
        return service.get_board_targets(board_type, project)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/mcp/test_get_board_targets.py -q`
Expected: PASS (5 tests).

- [ ] **Step 6: Lint**

Run: `.venv/bin/ruff check reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp/test_get_board_targets.py`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp/test_get_board_targets.py
git commit -m "feat(mcp): server-side тул get_board_targets — discovery done-цели доски (PRI-205)"
```

---

### Task 4: `configure-review` skill — pick-list via `get_board_targets`, drop client `get_columns`

**Files:**
- Modify: `plugin/skills/configure-review/SKILL.md` (step 5b done-target block, lines ~125–137)
- Test: `tests/skills/test_configure_review_skill.py` (add two guard tests)

**Interfaces:**
- Consumes: MCP tool `get_board_targets(board_type, project)` (Task 3).
- Produces: skill text that mentions `get_board_targets`, presents a pick-list, falls back to asking, and no longer references `get_columns`.

- [ ] **Step 1: Write the failing guard tests**

Append to `tests/skills/test_configure_review_skill.py`:

```python
def test_skill_uses_server_side_done_target_discovery():
    text = SKILL.read_text(encoding="utf-8")
    assert "get_board_targets" in text            # server-side discovery тул
    assert "pick-list" in text                    # предъявляет список кандидатов
    # больше не зависит от клиентского yougile-MCP
    assert "get_columns" not in text


def test_skill_done_target_discovery_falls_back_to_asking():
    text = SKILL.read_text(encoding="utf-8")
    # тул отсутствует/пусто/ошибка → спросить пользователя (fail-open)
    assert "fall back to asking" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/skills/test_configure_review_skill.py -q`
Expected: FAIL — `get_board_targets`/`pick-list` absent; `get_columns` currently present.

- [ ] **Step 3: Edit the skill**

In `plugin/skills/configure-review/SKILL.md`, replace the done-target block (the paragraph starting `**Then ask the finish-task done target**` through the end of the **youtrack** bullet, lines ~125–137) with:

```markdown
   **Then ask the finish-task done target** (closing a task after its PR — skill `/reviewer_finish-task`
   moves the finished task into the board's "done" cell). First **discover candidates server-side**: call
   the reviewer MCP tool `get_board_targets(board_type=<type>, project=<project>)` (read-only; creds live in
   the reviewer env — nothing to configure on the client, and **no yougile/youtrack board-MCP is needed**).
   Show the result as a **pick-list**; if the tool is absent (older deploy), returns an empty list, or
   errors, **fall back to asking** the user for the value. Write only the key(s) matching the board type;
   comment out the other board's keys with a one-line note (mirror the root `.review.yml`). All are optional
   and fail-soft — a wrong/absent column or value only warns, the PR link is still written:
   - **yougile** → `done_column`: the exact column **title** finish-task moves the finished task into (plus
     `completed:true`). `get_board_targets` returns `columns: [{title, board_title, …}]` — present them as a
     pick-list and disambiguate same-named columns by `board_title`; the user picks the done column by title.
     Empty / tool absent → ask for the title. Not set → finish-task only flips `completed:true` without
     moving the card.
   - **youtrack** → `status_field` (name of the custom field the board is built on — default `State`; it
     **also** governs status reading on sync, so set it when the board runs on a custom field like `Stage`)
     and `done_state` (target value of that field — default `Fixed`). `get_board_targets` returns
     `status_fields: [{field, values: […]}]` — let the user pick the field, then a value from that field's
     `values` as `done_state`. Empty / tool absent → ask for both. YouTrack-only; a yougile board ignores them.
```

Then update the intro «Standalone baseline» sentence (around lines 12–15) to note the new optional tool without removing the existing `count_tasks` guarded phrases — change the sentence:

> The single optional exception is sizing `context_limits.search_tasks`: the skill may call the reviewer MCP tool `count_tasks(project)` when it is connected; if not (fresh repo / no reviewer MCP / older deploy / empty graph) it **falls back to asking** the user. Everything else needs **no reviewer MCP / Postgres / Neo4j**.

to:

> Two optional exceptions use the reviewer MCP when connected: sizing `context_limits.search_tasks` via `count_tasks(project)`, and the finish-task done-target pick-list via `get_board_targets(board_type, project)`; if the reviewer MCP is absent (fresh repo / older deploy) or a tool errors, the skill **falls back to asking** the user. Everything else needs **no reviewer MCP / Postgres / Neo4j**.

(Verify `no reviewer MCP`, `count_tasks`, and `falls back to asking` all remain present — the existing guard `test_skill_standalone_baseline_with_optional_count_tasks` depends on them.)

- [ ] **Step 4: Run the full configure-review guard suite**

Run: `.venv/bin/pytest tests/skills/test_configure_review_skill.py -q`
Expected: PASS (all existing tests + 2 new).

- [ ] **Step 5: Commit**

```bash
git add plugin/skills/configure-review/SKILL.md tests/skills/test_configure_review_skill.py
git commit -m "feat(skills): configure-review — pick-list done-цели через get_board_targets, без клиентского get_columns (PRI-205)"
```

---

### Task 5: `finish-task` skill — name the resolved done target in the confirmation

**Files:**
- Modify: `plugin/skills/finish-task/SKILL.md` (step 4 «Offer + confirm», lines ~32–34)
- Test: `tests/skills/test_finish_task_skill.py` (add one guard test)

**Interfaces:**
- Produces: skill text where step 4 names the resolved done target explicitly and preserves the confirm-before-write gate.

- [ ] **Step 1: Write the failing guard test**

Append to `tests/skills/test_finish_task_skill.py`:

```python
def test_finish_task_names_resolved_done_target():
    t = SKILL.read_text(encoding="utf-8")
    assert "resolved done target" in t   # шаг 4 явно называет цель, не обобщённое mark done
    # гейт подтверждения не регрессирует
    assert "only after explicit confirmation" in t
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/skills/test_finish_task_skill.py -q`
Expected: FAIL — phrases absent.

- [ ] **Step 3: Edit the skill**

In `plugin/skills/finish-task/SKILL.md`, replace step 4 (lines ~32–34) with:

```markdown
4. **Offer + confirm.** Show what will be written — the PR link + the **resolved done target, named
   explicitly** (not a generic "mark done"): for yougile «перенесу задачу в колонку „<done_column>" +
   отмечу completed» (or just «отмечу completed» when `done_column` is unset); for youtrack «выставлю
   <status_field> = <done_state>» — plus any optional note. Ask the user to **confirm** before writing,
   and whether they want to add an optional note (details under the task). **Never write to the board
   silently** — the move / mark-done happens **only after explicit confirmation**, even when the values
   are already set in `.review.yml`.
```

- [ ] **Step 4: Run the full finish-task guard suite**

Run: `.venv/bin/pytest tests/skills/test_finish_task_skill.py -q`
Expected: PASS (all existing tests + 1 new).

- [ ] **Step 5: Commit**

```bash
git add plugin/skills/finish-task/SKILL.md tests/skills/test_finish_task_skill.py
git commit -m "feat(skills): finish-task — явно называть резолвнутую done-цель в подтверждении (PRI-205)"
```

---

### Task 6: Version bump + full verification

**Files:**
- Modify: `pyproject.toml:3`

- [ ] **Step 1: Bump the version**

In `pyproject.toml`, change line 3 from `version = "0.2.23"` to `version = "0.2.24"`.

- [ ] **Step 2: Run the full unit suite**

Run: `.venv/bin/pytest -q`
Expected: PASS (all tests; `integration` excluded by default per `pyproject.toml addopts`).

- [ ] **Step 3: Lint all touched files**

Run:
```bash
.venv/bin/ruff check reviewer/tasks/boards/base.py reviewer/tasks/boards/yougile.py \
  reviewer/tasks/boards/youtrack.py reviewer/mcp/service.py \
  reviewer/entrypoints/mcp_server.py tests/tasks/boards/test_yougile_targets.py \
  tests/tasks/boards/test_youtrack_targets.py tests/mcp/test_get_board_targets.py
```
Expected: no errors on these files (repo-wide ruff may be pre-dirty — do not chase unrelated warnings).

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "chore: bump 0.2.23 → 0.2.24"
```

---

## Out-of-session follow-ups (§7.2 / §8 of the spec)

Not part of this plan's session; require a redeploy:
1. Publish `0.2.24` to PyPI.
2. `reviewer update` on the deploy.
3. Live acceptance on both boards:
   - **YouGile PRI:** `get_board_targets("yougile","PRI")` → real columns; configure-review без клиентского yougile-MCP предлагает список → выбор `done_column`.
   - **YouTrack TES:** `get_board_targets("youtrack","TES")` → поля статуса (напр. `Stage`) + значения (напр. `Готово`); проверить оба пути (admin и fallback).
4. After the PR is created, offer `/reviewer_finish-task` to close PRI-205.

---

## Self-Review

**Spec coverage:**
- §3 Protocol `list_done_targets` → Task 1 Step 3. ✅
- §4.1 YouGile discovery (scope by project, board_id/board_title, fail-soft) → Task 1. ✅
- §4.2 YouTrack try-admin → fallback (source, distinct values, fail-soft) → Task 2. ✅
- §4.3 `make_board_provider` reuse (no status_field) → Task 3 service code. ✅
- §4.4 MCP tool (resolve type, creds not returned, fail-soft) → Task 3. ✅
- §4.5 configure-review pick-list + ask-fallback, drop `get_columns` → Task 4. ✅
- §4.6 finish-task explicit target, gate preserved → Task 5. ✅
- §7.1 unit tests (yougile/youtrack/init-via-provider/mcp/skills) → Tasks 1–5 tests. ✅
  (boards/__init__ path is exercised indirectly via the mcp monkeypatch of `make_board_provider`; the factory signature is unchanged, so no separate test is added.)
- §8 version bump → Task 6. ✅
- §7.2 live acceptance → out-of-session follow-ups (per session scope). ✅

**Placeholder scan:** No TBD/TODO; every code step shows full code. No "handle edge cases"/"add validation" hand-waves.

**Type consistency:** `list_done_targets(project)` signature identical across base.py Protocol, YougileBoard, YouTrackBoard, and the `_Provider` test doubles. Return keys consistent: YouGile `{columns, warnings}`, YouTrack `{status_fields, source, warnings}`; service wraps with `{board_type, project, **targets}`. `make_board_provider(self.settings, board_type)` called without `status_field` in Task 3 (matches the `lambda s, t:` monkeypatch), distinct from `finish_task`'s `status_field=` call — intentional (discovery returns candidates for status_field, so it needn't be set).
