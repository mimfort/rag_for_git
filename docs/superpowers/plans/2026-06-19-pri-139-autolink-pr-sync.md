# PRI-139 — Авто-привязка PR к задаче при sync-tasks — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** При `sync-tasks` создавать рёбра `IMPLEMENTED_BY` из GitHub-PR-URL в `description` задач, и дать `solve-task` ленивую подтяжку диффа PR через новый тул `get_pr_diff`.

**Architecture:** Серверный парсинг PR-URL в `TaskService.index_task`/`index_batch` для задач с `embedded=True` → переиспользуем существующий fail-soft `link_review`/`link_pr` (граф задач). Дифф PR подтягивается лениво новым session-less тулом `get_pr_diff(repo, number)`, который переиспользует `GitHubProvider.get_changed_files`. Плюс точечный фикс `link_pr`, чтобы пустой sha синка не затирал реальный sha от `publish_review`.

**Tech Stack:** Python 3.11–3.13, Neo4j (граф задач), httpx (GitHub), FastMCP (MCP-сервер), pytest, ruff.

## Global Constraints

- Язык проекта — **русский**: комментарии, докстринги, сообщения. Докстринги MCP-тулов — **английские** (как у остальных тулов в `mcp_server.py`).
- `ruff check .`: line-length 100, target py311.
- Тесты по умолчанию исключают `integration` (`addopts = -m 'not integration'`). Unit: `.venv/bin/pytest -q`. Integration (нужен Neo4j): `.venv/bin/pytest -m integration`.
- Коммиты: Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By`/упоминаний Claude).
- `node_id = "path#fqn"`; `PRRef.id = "repo#number"`.
- Гейтинг рёбер строго по `embedded=True` (не трогаем неизменившиеся задачи — 0 лишних Voyage-вызовов).

---

### Task 1: Парсер GitHub-PR-URL (`extract_pr_refs`)

**Files:**
- Create: `reviewer/tasks/pr_links.py`
- Test: `tests/tasks/test_pr_links.py`

**Interfaces:**
- Consumes: `reviewer.tasks.graph.PRRef` (поля `repo: str`, `number: int`, `url: str`, `sha: str`).
- Produces: `extract_pr_refs(text: str) -> list[PRRef]` — все GitHub-PR-URL из текста, дедуп по `(repo, number)`, порядок первого появления, `sha=""`, `touched` не входит в PRRef.

- [ ] **Step 1: Написать падающий тест**

`tests/tasks/test_pr_links.py`:
```python
"""Unit-тесты парсера GitHub-PR-URL для авто-линковки задач."""
from reviewer.tasks.pr_links import extract_pr_refs


def test_extract_single_pr_url():
    refs = extract_pr_refs("см. https://github.com/mimfort/rag_for_git/pull/20 — детали")
    assert len(refs) == 1
    r = refs[0]
    assert r.repo == "mimfort/rag_for_git"
    assert r.number == 20
    assert r.url == "https://github.com/mimfort/rag_for_git/pull/20"
    assert r.sha == ""


def test_extract_multiple_and_dedup():
    text = (
        "PR https://github.com/o/r/pull/7 и снова https://github.com/o/r/pull/7 "
        "плюс http://github.com/o/r/pull/8"
    )
    refs = extract_pr_refs(text)
    assert [(r.repo, r.number) for r in refs] == [("o/r", 7), ("o/r", 8)]


def test_ignores_non_pr_github_urls():
    text = (
        "issue https://github.com/o/r/issues/5 файл "
        "https://github.com/o/r/blob/main/x.py"
    )
    assert extract_pr_refs(text) == []


def test_empty_and_none_safe():
    assert extract_pr_refs("") == []
    assert extract_pr_refs("без ссылок") == []
```

- [ ] **Step 2: Прогнать тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/tasks/test_pr_links.py -q`
Expected: FAIL с `ModuleNotFoundError: No module named 'reviewer.tasks.pr_links'`

- [ ] **Step 3: Реализовать модуль**

`reviewer/tasks/pr_links.py`:
```python
"""Извлечение ссылок на GitHub PR из произвольного текста (description задачи).

Используется при sync-tasks для авто-линковки (:Task)-[:IMPLEMENTED_BY]->(:PR):
PR-ссылки живут в description многих задач, но без парсинга остаются текстом.
"""
from __future__ import annotations

import re

from reviewer.tasks.graph import PRRef

# https://github.com/<owner>/<repo>/pull/<number>
_PR_URL_RE = re.compile(
    r"https?://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)",
    re.IGNORECASE,
)


def extract_pr_refs(text: str) -> list[PRRef]:
    """PRRef из всех GitHub-PR-URL в тексте. Дедуп по (repo, number), sha=''."""
    if not text:
        return []
    seen: set[tuple[str, int]] = set()
    refs: list[PRRef] = []
    for m in _PR_URL_RE.finditer(text):
        owner, repo, num = m.group(1), m.group(2), int(m.group(3))
        full = f"{owner}/{repo}"
        sig = (full, num)
        if sig in seen:
            continue
        seen.add(sig)
        refs.append(PRRef(repo=full, number=num, url=m.group(0), sha=""))
    return refs
```

- [ ] **Step 4: Прогнать тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/tasks/test_pr_links.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Линт + коммит**

```bash
.venv/bin/ruff check reviewer/tasks/pr_links.py tests/tasks/test_pr_links.py
git add reviewer/tasks/pr_links.py tests/tasks/test_pr_links.py
git commit -m "feat(tasks): парсер GitHub-PR-URL extract_pr_refs для авто-линковки"
```

---

### Task 2: sha-clobber фикс в `TaskGraph.link_pr`

**Files:**
- Modify: `reviewer/tasks/graph.py:61-79` (метод `link_pr`)
- Test: `tests/tasks/test_graph.py` (новый unit-тест), `tests/tasks/test_integration.py` (новый integration-тест)

**Interfaces:**
- Сигнатура `link_pr(self, task_key: str, pr: PRRef, touched_node_ids: list[str]) -> None` не меняется. Меняется только Cypher: sha проставляется условно.

- [ ] **Step 1: Написать падающий unit-тест**

В `tests/tasks/test_graph.py` добавить (рядом с `test_link_pr_params`):
```python
def test_link_pr_sha_set_is_conditional():
    """sha проставляется условно: пустой sha не должен затирать существующий."""
    d = _FakeDriver()
    pr = PRRef(repo="o/r", number=7, url="https://github.com/o/r/pull/7", sha="")
    TaskGraph(d).link_pr("ID-1", pr, [])
    query, params = d.calls[0]
    assert "CASE WHEN $sha" in query
    assert params["sha"] == ""
```

- [ ] **Step 2: Прогнать тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/tasks/test_graph.py::test_link_pr_sha_set_is_conditional -q`
Expected: FAIL на `assert "CASE WHEN $sha" in query` (сейчас безусловный `SET ... p.sha=$sha`)

- [ ] **Step 3: Поправить Cypher в `link_pr`**

В `reviewer/tasks/graph.py`, метод `link_pr`, заменить строку
```python
            "  SET p.repo=$repo, p.number=$number, p.url=$url, p.sha=$sha "
```
на
```python
            "  SET p.repo=$repo, p.number=$number, p.url=$url, "
            "      p.sha = CASE WHEN $sha <> '' THEN $sha ELSE coalesce(p.sha, '') END "
```
И обновить докстринг метода — добавить строку:
```
        sha проставляется условно: пустой sha (линковка исторического PR из
        sync-tasks) не затирает реальный sha, ранее проставленный publish_review.
```

- [ ] **Step 4: Прогнать unit-тесты графа**

Run: `.venv/bin/pytest tests/tasks/test_graph.py -q`
Expected: PASS (включая существующий `test_link_pr_params` — он проверяет params, не текст запроса)

- [ ] **Step 5: Написать integration-тест (реальный Neo4j)**

В `tests/tasks/test_integration.py` добавить (рядом с `test_taskgraph_link_and_context`):
```python
def test_link_pr_empty_sha_preserves_existing(graph):
    """Линковка того же PR с пустым sha не затирает реальный sha."""
    pr_real = PRRef(repo="o/r", number=20,
                    url="https://github.com/o/r/pull/20", sha="realsha")
    graph.link_pr("ID-A", pr_real, ["a.py#foo"])

    pr_empty = PRRef(repo="o/r", number=20,
                     url="https://github.com/o/r/pull/20", sha="")
    graph.link_pr("ID-B", pr_empty, [])  # другая задача, тот же PR, пустой sha

    ctx = graph.task_context("ID-A")
    assert ctx["prs"][0]["id"] == "o/r#20"
    assert ctx["prs"][0]["sha"] == "realsha"  # sha сохранён
```

- [ ] **Step 6: Прогнать integration-тест (если поднят Neo4j)**

Run: `.venv/bin/pytest tests/tasks/test_integration.py::test_link_pr_empty_sha_preserves_existing -m integration -q`
Expected: PASS (требует `docker compose up -d`). Если Neo4j не поднят — пропустить, отметить в отчёте.

- [ ] **Step 7: Линт + коммит**

```bash
.venv/bin/ruff check reviewer/tasks/graph.py tests/tasks/test_graph.py tests/tasks/test_integration.py
git add reviewer/tasks/graph.py tests/tasks/test_graph.py tests/tasks/test_integration.py
git commit -m "fix(tasks): link_pr не затирает непустой sha (условный SET)"
```

---

### Task 3: PR-линковка в `TaskService.index_task`

**Files:**
- Modify: `reviewer/tasks/service.py` (импорт + метод `index_task`, ~строки 6-13 и 25-72)
- Test: `tests/tasks/test_service.py`

**Interfaces:**
- Consumes: `extract_pr_refs(text) -> list[PRRef]` (Task 1); `self.link_review(task_key, pr, touched)` (существует, fail-soft, no-op без графа/ключа).
- Produces: `index_task` возвращает dict с **новым** полем `prs_linked: int` (рядом с `key`/`embedded`/`links_upserted`/`warnings`).

- [ ] **Step 1: Написать падающие тесты**

В `tests/tasks/test_service.py` добавить:
```python
def test_index_task_links_prs_when_embedded():
    graph = _FakeGraph()
    task = {"key": "ID-1", "title": "T",
            "description": "https://github.com/o/r/pull/7 и https://github.com/o/r/pull/8"}
    res = TaskService(_FakeStore(), graph, _FakeEmbedder()).index_task(task)
    assert res["embedded"] is True
    assert res["prs_linked"] == 2
    assert [tk for tk, _, _ in graph.pr_links] == ["ID-1", "ID-1"]
    assert all(touched == [] for _, _, touched in graph.pr_links)


def test_index_task_no_pr_link_when_unchanged():
    text = build_task_text("T", "https://github.com/o/r/pull/7", [])
    store = _FakeStore(hashes={"ID-1": task_content_hash(text)})
    graph = _FakeGraph()
    task = {"key": "ID-1", "title": "T", "description": "https://github.com/o/r/pull/7"}
    res = TaskService(store, graph, _FakeEmbedder()).index_task(task)
    assert res["embedded"] is False
    assert res["prs_linked"] == 0
    assert graph.pr_links == []


def test_index_task_pr_link_noop_without_graph():
    task = {"key": "ID-1", "title": "T",
            "description": "https://github.com/o/r/pull/7"}
    res = TaskService(_FakeStore(), None, _FakeEmbedder()).index_task(task)
    assert res["embedded"] is True
    assert res["prs_linked"] == 0
```

- [ ] **Step 2: Прогнать тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/tasks/test_service.py -k "pr_link or links_prs" -q`
Expected: FAIL с `KeyError: 'prs_linked'`

- [ ] **Step 3: Реализовать**

В `reviewer/tasks/service.py` добавить импорт (после строки `from reviewer.tasks.graph import PRRef`):
```python
from reviewer.tasks.pr_links import extract_pr_refs
```

В методе `index_task` заменить блок раннего возврата для задач без ключа:
```python
        if not key:
            return {"key": None, "embedded": False, "links_upserted": 0,
                    "warnings": ["task has no key"]}
```
на
```python
        if not key:
            return {"key": None, "embedded": False, "links_upserted": 0,
                    "prs_linked": 0, "warnings": ["task has no key"]}
```

Заменить финальный блок метода:
```python
        return {"key": key, "embedded": embedded,
                "links_upserted": links_upserted, "warnings": warnings}
```
на
```python
        # Авто-линковка PR из description — только для изменившихся (embedded)
        # задач и при доступном графе (повторный синк без изменений ничего не делает).
        prs_linked = 0
        if embedded and self._graph is not None:
            refs = extract_pr_refs(description)
            for pr in refs:
                self.link_review(key, pr, [])  # touched=[] — код подтянется лениво
            prs_linked = len(refs)

        return {"key": key, "embedded": embedded,
                "links_upserted": links_upserted, "prs_linked": prs_linked,
                "warnings": warnings}
```

- [ ] **Step 4: Прогнать тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/tasks/test_service.py -q`
Expected: PASS (все, включая новые 3)

- [ ] **Step 5: Линт + коммит**

```bash
.venv/bin/ruff check reviewer/tasks/service.py tests/tasks/test_service.py
git add reviewer/tasks/service.py tests/tasks/test_service.py
git commit -m "feat(tasks): index_task линкует PR из description при embedded=True"
```

---

### Task 4: PR-линковка в `TaskService.index_batch`

**Files:**
- Modify: `reviewer/tasks/service.py` (метод `index_batch`: шаг 1 — результат без ключа; шаг 6 — графовый цикл)
- Test: `tests/tasks/test_service_batch.py` (расширить `_FakeGraph` + новый тест)

**Interfaces:**
- Consumes: `extract_pr_refs` (Task 1, импорт уже добавлен в Task 3); `self.link_review` (существует).
- Produces: каждый элемент результата `index_batch` содержит поле `prs_linked: int`.

- [ ] **Step 1: Расширить `_FakeGraph` и написать падающий тест**

В `tests/tasks/test_service_batch.py` в класс `_FakeGraph` добавить (в `__init__` и новый метод):
```python
class _FakeGraph:
    def __init__(self, raise_on=()):
        self.tasks = []
        self.links = []
        self.pr_links = []
        self._raise_on = set(raise_on)

    def upsert_task(self, key, aliases, title, status, url):
        if "upsert_task" in self._raise_on:
            raise RuntimeError("neo4j down")
        self.tasks.append(key)

    def upsert_links(self, key, links):
        self.links.append((key, links))
        return len(links)

    def link_pr(self, task_key, pr, touched):
        self.pr_links.append((task_key, pr, touched))
```

И добавить тест:
```python
def test_index_batch_links_prs_for_embedded_only():
    unchanged = build_task_text("T2", "https://github.com/o/r/pull/9", [])
    store = _FakeStore(hashes={"ID-2": task_content_hash(unchanged)})
    graph, emb = _FakeGraph(), _FakeEmbedder()
    tasks = [
        {"key": "ID-1", "title": "T1", "description": "https://github.com/o/r/pull/7"},
        {"key": "ID-2", "title": "T2", "description": "https://github.com/o/r/pull/9"},
    ]
    results = TaskService(store, graph, emb).index_batch(tasks)

    assert results[0]["embedded"] is True and results[0]["prs_linked"] == 1
    assert results[1]["embedded"] is False and results[1]["prs_linked"] == 0
    assert [tk for tk, _, _ in graph.pr_links] == ["ID-1"]
```

- [ ] **Step 2: Прогнать тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/tasks/test_service_batch.py::test_index_batch_links_prs_for_embedded_only -q`
Expected: FAIL с `KeyError: 'prs_linked'`

- [ ] **Step 3: Реализовать**

В `reviewer/tasks/service.py`, метод `index_batch`, шаг 1 — заменить результат для задач без ключа:
```python
                results[i] = {"key": None, "embedded": False, "links_upserted": 0,
                              "warnings": ["task has no key"]}
```
на
```python
                results[i] = {"key": None, "embedded": False, "links_upserted": 0,
                              "prs_linked": 0, "warnings": ["task has no key"]}
```

Заменить весь шаг 6 (графовый цикл) на:
```python
        # Шаг 6: граф для всех валидных задач (+ PR-линковка для embedded)
        for i, p in enumerate(parsed):
            if p is None or results[i] is None:
                continue
            links_upserted = 0
            prs_linked = 0
            if self._graph is None:
                results[i]["warnings"].append(
                    "graph unavailable: task not added to task graph")
            else:
                try:
                    self._graph.upsert_task(p["key"], p["aliases"], p["title"],
                                            p["status"], p["url"])
                    if p["links"]:
                        links_upserted = self._graph.upsert_links(p["key"], p["links"])
                except Exception as e:
                    log.warning("index_batch: сбой графа для %s", p["key"], exc_info=True)
                    results[i]["warnings"].append(f"graph: {type(e).__name__}: {e}")
                # PR-линковка (IMPLEMENTED_BY) — только для изменившихся задач.
                if results[i]["embedded"]:
                    refs = extract_pr_refs(p["description"])
                    for pr in refs:
                        self.link_review(p["key"], pr, [])
                    prs_linked = len(refs)
            results[i]["links_upserted"] = links_upserted
            results[i]["prs_linked"] = prs_linked

        return results
```

- [ ] **Step 4: Прогнать тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/tasks/test_service_batch.py -q`
Expected: PASS (все, включая новый)

- [ ] **Step 5: Линт + коммит**

```bash
.venv/bin/ruff check reviewer/tasks/service.py tests/tasks/test_service_batch.py
git add reviewer/tasks/service.py tests/tasks/test_service_batch.py
git commit -m "feat(tasks): index_batch линкует PR из description при embedded=True"
```

---

### Task 5: Session-less метод `MCPReviewService.get_pr_diff`

**Files:**
- Modify: `reviewer/mcp/service.py` (импорт `ChangedFile`; новый метод `get_pr_diff`; модульный хелпер `_format_pr_diff` + константа)
- Test: `tests/mcp/test_service.py`

**Interfaces:**
- Consumes: `self._vcs_factory` (Callable | None — test-override); `self._review_service._create_vcs_provider(owner, name) -> GitHubProvider` (существует); `VCSProvider.get_changed_files(number) -> list[ChangedFile]`; `ChangedFile{path, status, patch}`; `reviewer.services.repo_id.normalize_repo`.
- Produces: `get_pr_diff(self, repo: str, number: int) -> str` — отформатированный unified-diff (усечён), либо fail-soft нота `(...)`.

- [ ] **Step 1: Написать падающие тесты**

В `tests/mcp/test_service.py` добавить (используя существующие `_settings`, `_components`, `_changed`):
```python
def test_get_pr_diff_formats_changed_files():
    vcs = MagicMock()
    vcs.get_changed_files.return_value = [
        _changed(path="a.py", status="modified", patch="@@ -1 +1 @@\n-x\n+y"),
    ]
    svc = MCPReviewService(_settings(), _components(), vcs_factory=lambda o, r: vcs)
    out = svc.get_pr_diff("o/r", 7)
    assert "a.py" in out and "+y" in out
    vcs.get_changed_files.assert_called_once_with(7)
    vcs.close.assert_not_called()  # factory-владелец не закрываем


def test_get_pr_diff_failsoft_on_error():
    vcs = MagicMock()
    vcs.get_changed_files.side_effect = RuntimeError("boom")
    svc = MCPReviewService(_settings(), _components(), vcs_factory=lambda o, r: vcs)
    assert svc.get_pr_diff("o/r", 7) == "(diff PR недоступен)"


def test_get_pr_diff_empty_repo_note():
    svc = MCPReviewService(_settings(), _components(), vcs_factory=lambda o, r: MagicMock())
    assert "repo не задан" in svc.get_pr_diff("", 7)
```

> Примечание: `_changed` в этом модуле имеет сигнатуру `_changed(path="...", status="...", patch="...")` и возвращает `ChangedFile`. `_settings()` даёт `Settings()` с `default_repo=""`.

- [ ] **Step 2: Прогнать тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/mcp/test_service.py -k get_pr_diff -q`
Expected: FAIL с `AttributeError: ... has no attribute 'get_pr_diff'`

- [ ] **Step 3: Реализовать**

В `reviewer/mcp/service.py` расширить импорт VCS-типов:
```python
from reviewer.vcs.base import Finding, VCSProvider
```
на
```python
from reviewer.vcs.base import ChangedFile, Finding, VCSProvider
```

Добавить метод `get_pr_diff` в класс `MCPReviewService` (рядом с другими session-less тулами, после `definition`):
```python
    def get_pr_diff(self, repo: str, number: int) -> str:
        """Unified diff изменённых файлов PR (session-less) — ленивая подтяжка для /solve-task.

        repo обязателен ("owner/name"): PR может быть в другом репозитории, граф
        задач глобален. Дифф усечён до _PR_DIFF_MAX_CHARS. Любая ошибка → fail-soft нота.
        """
        from reviewer.services.repo_id import normalize_repo
        raw = repo or self.settings.default_repo
        if not raw:
            return "(repo не задан: передайте repo или задайте DEFAULT_REPO)"
        try:
            repo = normalize_repo(raw)
        except ValueError:
            return f"(некорректный repo: {raw!r})"
        owner, name = repo.split("/", 1)
        vcs = (self._vcs_factory(owner, name) if self._vcs_factory
               else self._review_service._create_vcs_provider(owner, name))
        try:
            files = vcs.get_changed_files(number)
        except Exception:
            log.warning("get_pr_diff: сбой получения diff для %s#%s",
                        repo, number, exc_info=True)
            return "(diff PR недоступен)"
        finally:
            # Внутренне созданный провайдер закрываем сами (factory-владельца — нет).
            if self._vcs_factory is None:
                try:
                    vcs.close()
                except Exception:
                    log.warning("get_pr_diff: не удалось закрыть VCS", exc_info=True)
        return _format_pr_diff(files) or "(PR без изменённых файлов)"
```

Добавить модульную константу и хелпер (рядом с `_finding_from_dict` в конце модуля):
```python
_PR_DIFF_MAX_CHARS = 20000


def _format_pr_diff(files: list[ChangedFile]) -> str:
    """Список ChangedFile → текстовый unified-diff с символьным капом."""
    blocks: list[str] = []
    for f in files:
        head = f"--- {f.path} [{f.status}]"
        if f.patch is None:
            blocks.append(f"{head}\n(patch недоступен: файл слишком большой или бинарный)")
        else:
            blocks.append(f"{head}\n{f.patch}")
    out = "\n\n".join(blocks)
    if len(out) > _PR_DIFF_MAX_CHARS:
        out = out[:_PR_DIFF_MAX_CHARS] + "\n… (truncated)"
    return out
```

- [ ] **Step 4: Прогнать тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/mcp/test_service.py -k get_pr_diff -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Линт + коммит**

```bash
.venv/bin/ruff check reviewer/mcp/service.py tests/mcp/test_service.py
git add reviewer/mcp/service.py tests/mcp/test_service.py
git commit -m "feat(mcp): session-less get_pr_diff для ленивой подтяжки диффа PR"
```

---

### Task 6: Регистрация MCP-тула `get_pr_diff`

**Files:**
- Modify: `reviewer/entrypoints/mcp_server.py` (новый тул перед `publish_review`, ~строка 139)
- Test: `tests/mcp/test_server_tools.py`

**Interfaces:**
- Consumes: `MCPReviewService.get_pr_diff(repo, number) -> str` (Task 5).
- Produces: MCP-тул `get_pr_diff(repo: str, number: int) -> str`.

- [ ] **Step 1: Написать падающие тесты**

В `tests/mcp/test_server_tools.py` добавить:
```python
def test_get_pr_diff_tool_registered():
    import asyncio

    svc = _service()
    svc.get_pr_diff.return_value = "diff"
    server = create_server(svc)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert "get_pr_diff" in names


def test_get_pr_diff_tool_forwards():
    import asyncio

    svc = _service()
    svc.get_pr_diff.return_value = "diff"
    server = create_server(svc)
    asyncio.run(server.call_tool("get_pr_diff", {"repo": "o/r", "number": 7}))
    svc.get_pr_diff.assert_called_once_with("o/r", 7)
```

- [ ] **Step 2: Прогнать тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/mcp/test_server_tools.py -k get_pr_diff -q`
Expected: FAIL (тул `get_pr_diff` не зарегистрирован → `list_tools` без него / `call_tool` падает)

- [ ] **Step 3: Реализовать**

В `reviewer/entrypoints/mcp_server.py` добавить тул (после `definition`, перед `publish_review`):
```python
    @mcp.tool()
    def get_pr_diff(repo: str, number: int) -> str:
        """Unified diff of a (possibly historical) GitHub PR's changed files, no PR session.
        repo is "owner/name", number is the PR number. Use it (e.g. from /solve-task) to
        lazily inspect what a related task's PR changed. Capped; fail-soft."""
        return service.get_pr_diff(repo, number)
```

- [ ] **Step 4: Прогнать тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/mcp/test_server_tools.py -q`
Expected: PASS (все, включая новые 2)

- [ ] **Step 5: Линт + коммит**

```bash
.venv/bin/ruff check reviewer/entrypoints/mcp_server.py tests/mcp/test_server_tools.py
git add reviewer/entrypoints/mcp_server.py tests/mcp/test_server_tools.py
git commit -m "feat(mcp): регистрация тула get_pr_diff в MCP-сервере"
```

---

### Task 7: Guidance в скилле `solve-task`

**Files:**
- Modify: `plugin/skills/solve-task/SKILL.md` (шаг 3 «Gather context», блок графовых тулов)

**Interfaces:** только документация; код не меняется. Тула `get_pr_diff` уже существует (Tasks 5-6).

- [ ] **Step 1: Внести правку в SKILL.md**

В `plugin/skills/solve-task/SKILL.md`, в шаге 3 (блок «Deepen via the code graph»), после описания `definition`, добавить абзац:
```markdown
   - **Lazy PR diff (optional).** `get_task_context` / `search_tasks` surface related/similar
     tasks and their PRs (id form `owner/name#N`). If a related task passed the relevance
     filter AND its PR is worth inspecting for the implementation, parse `repo`/`number` from
     the PR id and call `get_pr_diff(repo, number)` to see what that PR changed — pull it
     lazily, only when the LLM judges it useful (don't fetch diffs for low-relevance tasks).
     Fail-open: a `(diff PR недоступен)` / `(repo не задан…)` note is non-fatal — continue.
```

- [ ] **Step 2: Проверка целостности**

Run: `grep -n "get_pr_diff" plugin/skills/solve-task/SKILL.md`
Expected: одна строка с упоминанием тула в шаге 3.

- [ ] **Step 3: Коммит**

```bash
git add plugin/skills/solve-task/SKILL.md
git commit -m "docs(skill): solve-task — ленивая подтяжка диффа PR через get_pr_diff"
```

---

### Task 8: Финальная верификация

**Files:** нет правок — только прогон.

- [ ] **Step 1: Полный unit-прогон**

Run: `.venv/bin/pytest -q`
Expected: PASS (integration исключены по умолчанию). Зелёный — фиксируем; красный — чиним до зелёного.

- [ ] **Step 2: Линт всего затронутого**

Run: `.venv/bin/ruff check reviewer/ tests/`
Expected: без новых нарушений в изменённых файлах (репо-wide чистота не гарантирована — не гнаться за ней, см. заметку о ruff на main).

- [ ] **Step 3: Integration-прогон задач (если поднят Neo4j)**

Run: `.venv/bin/pytest tests/tasks -m integration -q`
Expected: PASS при `docker compose up -d`; иначе отметить как пропущенный.

---

## Self-Review

**Spec coverage:**
- Часть A (парсинг + линковка на синке) → Tasks 1, 3, 4. ✓
- Часть Б (`get_pr_diff`) → Tasks 5, 6. ✓
- Часть В (скилл solve-task) → Task 7. ✓
- sha-фикс → Task 2. ✓
- `prs_linked` в результате → Tasks 3, 4. ✓
- Идемпотентность/0 лишних Voyage-вызовов → гейт `embedded=True` в Tasks 3, 4. ✓
- Тесты (pr_links, graph, service, service_batch, server_tools, mcp service) → во всех тасках. ✓
- Бэкфилл существующего корпуса — операционная заметка (вне кода), будет выполнен пользователем после мержа (правка задач на доске). ✓

**Placeholder scan:** плейсхолдеров нет — каждый шаг с конкретным кодом/командой.

**Type consistency:** `extract_pr_refs(text)->list[PRRef]` (Task 1) одинаково потребляется в Tasks 3, 4. `PRRef{repo,number,url,sha}` согласован. `get_pr_diff(repo,number)->str` (Task 5) и тул (Task 6) совпадают по сигнатуре. `prs_linked: int` единообразно. `_format_pr_diff(list[ChangedFile])->str` использует поля `path/status/patch`. ✓
