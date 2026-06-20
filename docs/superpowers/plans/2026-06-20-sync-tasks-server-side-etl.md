# Sync-Tasks Server-Side ETL — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Перенести болк-синк задач доски из LLM-скилла в server-side ETL внутри `reviewer-mcp`: один MCP-тул `sync_board` сам перечисляет доску по REST, нормализует в `TaskBrief`, индексирует через существующий `TaskService.index_batch`; LLM лишь дёргает тул без payload.

**Architecture:** Прямой REST за интерфейсом `TaskBoardProvider` (yougile — первая реализация). Оркестратор `SyncService` board-агностичен. Инкрементальность — timestamp-watermark в `index_meta` (переиспользуем существующие `ChunkStore.get_index_meta`/`set_index_meta`, `repo=""`, `ref="tasks:<board>"`). Индексатор, content_hash-дедуп и авто-PR-линковка переиспользуются как есть.

**Tech Stack:** Python 3.11+, httpx, pydantic-settings, FastMCP, pgvector/ParadeDB, Neo4j, pytest. Линт ruff (line-length 100).

## Global Constraints

- Язык кода: русские комментарии/докстринги/сообщения (стиль репо).
- Коммиты: Conventional Commits на русском, **без** self-attribution (никаких Co-Authored-By/Claude).
- Внешние сервисы за интерфейсами, мокаются в unit; реальные вызовы — только в `-m integration`.
- `pytest` по умолчанию исключает integration (`addopts = -m 'not integration'`).
- ruff line-length 100, target py311.
- Задачи **глобальны** (таблица `tasks`, граф `:Task` — без repo-скоупа). Курсор ключуется по доске, не по репо.
- Паритет нормализации с плейбуком `plugin/skills/review-pr/references/task-context-yougile.md` — обязателен (критерий «результат идентичен»).
- `TaskBrief` = `dict {key, aliases[], title, description, criteria[], status, url, links[]}`.

---

### Task 1: Settings — креды и base URL REST-доски

**Files:**
- Modify: `reviewer/config/settings.py` (после строки 87, блок task board)
- Test: `tests/config/test_settings.py` (если нет — создать)

**Interfaces:**
- Produces: `Settings.task_board_api_key: str`, `Settings.task_board_api_base: str`, метод `Settings.task_board_api_base_for(type_: str) -> str` (дефолт по типу).

- [ ] **Step 1: Написать падающий тест**

```python
# tests/config/test_settings.py
from reviewer.config.settings import Settings


def test_task_board_api_base_default_yougile():
    s = Settings(task_board_api_key="k", task_board_api_base="")
    assert s.task_board_api_base_for("yougile") == "https://yougile.com/api-v2"


def test_task_board_api_base_explicit_overrides_default():
    s = Settings(task_board_api_base="https://ru.yougile.com/api-v2")
    assert s.task_board_api_base_for("yougile") == "https://ru.yougile.com/api-v2"


def test_task_board_api_base_unknown_type_empty():
    s = Settings(task_board_api_base="")
    assert s.task_board_api_base_for("jira") == ""
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/config/test_settings.py -q`
Expected: FAIL (`AttributeError: ... task_board_api_base_for`).

- [ ] **Step 3: Реализация**

В `reviewer/config/settings.py` после `task_board_url_template` (строка 87):

```python
    task_board_api_key: str = ""       # креды REST API доски (server-side sync); не утекают клиентам
    task_board_api_base: str = ""      # base URL REST API доски; пусто → дефолт по типу
```

Дефолты по типу — модульная константа рядом с классом:

```python
_BOARD_API_BASE_DEFAULTS = {"yougile": "https://yougile.com/api-v2"}
```

Метод в классе `Settings` (рядом с `task_board_default`):

```python
    def task_board_api_base_for(self, type_: str) -> str:
        """Base URL REST API доски: явный task_board_api_base или дефолт по типу."""
        return self.task_board_api_base or _BOARD_API_BASE_DEFAULTS.get(type_, "")
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `.venv/bin/pytest tests/config/test_settings.py -q && .venv/bin/ruff check reviewer/config/settings.py`
Expected: PASS, ruff clean.

- [ ] **Step 5: Коммит**

```bash
git add reviewer/config/settings.py tests/config/test_settings.py
git commit -m "feat(config): креды и base URL REST-доски (TASK_BOARD_API_KEY/BASE)"
```

---

### Task 2: Board Protocol + RawTask + фабрика

**Files:**
- Create: `reviewer/tasks/boards/__init__.py`
- Create: `reviewer/tasks/boards/base.py`
- Test: `tests/tasks/boards/__init__.py` (пустой), `tests/tasks/boards/test_base.py`

**Interfaces:**
- Produces:
  - `RawTask` (dataclass): `key: str`, `project_code: str`, `title: str`, `description: str`, `status: str | None`, `subtask_ids: list[str]`, `timestamp: int`.
  - `TaskBoardProvider` (Protocol): `iter_raw(board: str | None, limit: int | None) -> Iterable[RawTask]`, `normalize(raw: RawTask) -> dict`.
  - `make_board_provider(settings) -> TaskBoardProvider | None` (фабрика по `task_board_default()["type"]`; `None` если доска/ключ не настроены).

- [ ] **Step 1: Написать падающий тест**

```python
# tests/tasks/boards/test_base.py
from reviewer.tasks.boards import RawTask, make_board_provider
from reviewer.config.settings import Settings


def test_rawtask_fields():
    rt = RawTask(key="ID-1", project_code="PRI-1", title="t", description="d",
                 status="Backlog", subtask_ids=["u1"], timestamp=123)
    assert rt.key == "ID-1" and rt.timestamp == 123


def test_make_provider_none_when_no_board():
    s = Settings(task_board_type="", task_board_api_key="")
    assert make_board_provider(s) is None


def test_make_provider_none_when_no_api_key():
    s = Settings(task_board_type="yougile", task_board_api_key="")
    assert make_board_provider(s) is None


def test_make_provider_yougile():
    s = Settings(task_board_type="yougile", task_board_api_key="k",
                 task_board_key_pattern=r"[A-Z]+-\d+")
    prov = make_board_provider(s)
    assert prov is not None
    assert prov.__class__.__name__ == "YougileBoard"
```

- [ ] **Step 2: Запустить — падает**

Run: `.venv/bin/pytest tests/tasks/boards/test_base.py -q`
Expected: FAIL (`ModuleNotFoundError: reviewer.tasks.boards`).

- [ ] **Step 3: Реализация**

`reviewer/tasks/boards/base.py`:

```python
"""Интерфейс провайдера доски задач для server-side синка (enumerate+normalize).

Транспорт изолирован за Protocol: yougile — референсная реализация (REST).
Нормализация в TaskBrief — ответственность конкретной доски (порт плейбука
task-context-<type>.md), оркестратор SyncService остаётся board-агностичным.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol


@dataclass
class RawTask:
    """Сырая задача доски до нормализации. timestamp — для watermark."""
    key: str               # канонический код (idTaskCommon, ID-N)
    project_code: str      # проектный код (idTaskProject, PRI-N)
    title: str
    description: str
    status: str | None     # резолвнутый title колонки
    subtask_ids: list[str] # UUID подзадач (titles резолвятся в normalize)
    timestamp: int         # epoch ms последнего изменения


class TaskBoardProvider(Protocol):
    """Перечисление и нормализация задач доски (без I/O в normalize, кроме
    best-effort резолва title подзадач)."""

    def iter_raw(self, board: str | None, limit: int | None) -> Iterable[RawTask]:
        ...

    def normalize(self, raw: RawTask) -> dict:
        """RawTask → TaskBrief dict {key, aliases, title, description,
        criteria, status, url, links}."""
        ...
```

`reviewer/tasks/boards/__init__.py`:

```python
"""Провайдеры досок задач: фабрика по типу + реэкспорт интерфейса."""
from __future__ import annotations

from reviewer.tasks.boards.base import RawTask, TaskBoardProvider

__all__ = ["RawTask", "TaskBoardProvider", "make_board_provider"]


def make_board_provider(settings) -> TaskBoardProvider | None:
    """Сконструировать провайдер по task_board_default()["type"] и кредам.

    None, если доска не настроена (нет блока task_board) или нет API-ключа —
    server-side синк недоступен, sync_board вернёт понятный error-summary.
    """
    cfg = settings.task_board_default()
    if not cfg or not settings.task_board_api_key:
        return None
    type_ = cfg.get("type", "")
    if type_ == "yougile":
        from reviewer.tasks.boards.yougile import YougileBoard
        return YougileBoard(
            api_key=settings.task_board_api_key,
            api_base=settings.task_board_api_base_for(type_),
            key_pattern=cfg.get("key_pattern", ""),
            url_template=cfg.get("url_template", ""),
        )
    return None
```

> Тест `test_make_provider_yougile` импортирует `YougileBoard` — он появится в Task 3.
> Чтобы Task 2 коммитился зелёным независимо, временно держим заглушку:
> создать `reviewer/tasks/boards/yougile.py` с минимальным классом-заглушкой
> `class YougileBoard:  def __init__(self, *, api_key, api_base, key_pattern, url_template): ...`
> — Task 3 заменит её полной реализацией.

- [ ] **Step 4: Запустить — проходит**

Run: `.venv/bin/pytest tests/tasks/boards/test_base.py -q && .venv/bin/ruff check reviewer/tasks/boards/`
Expected: PASS, ruff clean.

- [ ] **Step 5: Коммит**

```bash
git add reviewer/tasks/boards/ tests/tasks/boards/
git commit -m "feat(tasks): интерфейс TaskBoardProvider + RawTask + фабрика провайдера"
```

---

### Task 3: YougileBoard — REST-клиент + чистая нормализация (паритет с плейбуком)

**Files:**
- Modify/replace: `reviewer/tasks/boards/yougile.py` (заменить заглушку из Task 2)
- Test: `tests/tasks/boards/test_yougile_normalize.py`

**Interfaces:**
- Consumes: `RawTask` (Task 2).
- Produces:
  - чистая `normalize_yougile(raw: RawTask, key_pattern: str, url_template: str, subtask_titles: dict[str, str] | None = None) -> dict` (без I/O — `subtask_titles` инжектится).
  - класс `YougileBoard(api_key, api_base, key_pattern, url_template)` с `iter_raw` (REST) и `normalize` (резолвит titles подзадач по REST best-effort, затем зовёт `normalize_yougile`).

**Паритет с плейбуком** (`task-context-yougile.md`):
- `key` ← `raw.key` (ID-N); `aliases` ← `[raw.project_code]` (PRI-N, если не пуст и ≠ key).
- `title`/`description`/`status` ← как в `raw`. `criteria` ← `[]` (живут inline в description).
- `links` = union, дедуп по ключу:
  - `{type:"subtask", key, title}` на каждый id из `subtask_ids` (title из `subtask_titles`, иначе пропуск title-резолва — но ребро всё равно по key);
  - `{type:"related", key}` на каждый матч `key_pattern` в `description`, исключая `key`, `aliases` и ключи подзадач.
- `url` ← `url_template` с подстановкой `{code}`=project_code (PRI-N), если шаблон задан, иначе `None`.

- [ ] **Step 1: Написать падающие тесты (чистая нормализация)**

```python
# tests/tasks/boards/test_yougile_normalize.py
from reviewer.tasks.boards.base import RawTask
from reviewer.tasks.boards.yougile import normalize_yougile

KP = r"[A-Z]+-\d+"
URL = "https://ru.yougile.com/team/T/#{code}"


def _raw(**kw):
    base = dict(key="ID-10", project_code="PRI-10", title="T", description="",
                status="Backlog", subtask_ids=[], timestamp=1)
    base.update(kw)
    return RawTask(**base)


def test_key_aliases_status_url():
    b = normalize_yougile(_raw(), KP, URL)
    assert b["key"] == "ID-10"
    assert b["aliases"] == ["PRI-10"]
    assert b["status"] == "Backlog"
    assert b["criteria"] == []
    assert b["url"] == "https://ru.yougile.com/team/T/#PRI-10"


def test_url_none_without_template():
    b = normalize_yougile(_raw(), KP, "")
    assert b["url"] is None


def test_related_links_from_description_excluding_self():
    raw = _raw(description="связано с PRI-96 и ID-10 и PRI-10 и ABC-7")
    b = normalize_yougile(raw, KP, URL)
    rels = {lk["key"] for lk in b["links"] if lk["type"] == "related"}
    assert rels == {"PRI-96", "ABC-7"}        # self key ID-10 и alias PRI-10 исключены


def test_subtask_links_with_titles_and_dedup_related():
    raw = _raw(description="см. ID-55", subtask_ids=["u1"])
    b = normalize_yougile(raw, KP, URL, subtask_titles={"u1": "ID-55:Подзадача"})
    sub = [lk for lk in b["links"] if lk["type"] == "subtask"]
    assert sub == [{"type": "subtask", "key": "u1", "title": "ID-55:Подзадача"}]
    # related-матч ID-55 в description не дублирует уже покрытое подзадачей —
    # но подзадача покрыта по UUID u1, а ID-55 — отдельный код; remains related:
    rels = {lk["key"] for lk in b["links"] if lk["type"] == "related"}
    assert "ID-55" in rels


def test_alias_omitted_when_equals_key():
    b = normalize_yougile(_raw(project_code="ID-10"), KP, URL)
    assert b["aliases"] == []
```

- [ ] **Step 2: Запустить — падает**

Run: `.venv/bin/pytest tests/tasks/boards/test_yougile_normalize.py -q`
Expected: FAIL (`ImportError: normalize_yougile` / заглушка без функции).

- [ ] **Step 3: Реализация `reviewer/tasks/boards/yougile.py`**

```python
"""Провайдер доски Yougile: REST-клиент (httpx) + нормализация в TaskBrief.

REST API v2: base https://yougile.com/api-v2, заголовок Authorization: Bearer <key>.
Перечисление: projects → boards → columns → tasks (listing-эндпоинты дают полные
объекты задач + проход columns для title статусов). normalize резолвит title
подзадач best-effort и зовёт чистую normalize_yougile.

Нормализация — порт плейбука plugin/skills/review-pr/references/task-context-yougile.md.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable

import httpx

from reviewer.tasks.boards.base import RawTask

log = logging.getLogger(__name__)


def normalize_yougile(
    raw: RawTask,
    key_pattern: str,
    url_template: str,
    subtask_titles: dict[str, str] | None = None,
) -> dict:
    """RawTask → TaskBrief dict. Чистая: без I/O (titles подзадач инжектятся)."""
    subtask_titles = subtask_titles or {}
    key = raw.key
    aliases = [raw.project_code] if raw.project_code and raw.project_code != key else []

    links: list[dict] = []
    covered: set[str] = {key, *aliases}
    for sid in raw.subtask_ids:
        link = {"type": "subtask", "key": sid}
        title = subtask_titles.get(sid)
        if title:
            link["title"] = title
        links.append(link)
        covered.add(sid)

    if key_pattern:
        seen_rel: set[str] = set()
        for m in re.finditer(key_pattern, raw.description or ""):
            code = m.group(0)
            if code in covered or code in seen_rel:
                continue
            seen_rel.add(code)
            links.append({"type": "related", "key": code})

    url = None
    if url_template and raw.project_code:
        url = url_template.replace("{code}", raw.project_code)

    return {
        "key": key,
        "aliases": aliases,
        "title": raw.title,
        "description": raw.description,
        "criteria": [],
        "status": raw.status,
        "url": url,
        "links": links,
    }


class YougileBoard:
    """REST-провайдер Yougile. iter_raw перечисляет доску; normalize резолвит
    title подзадач (best-effort) и нормализует через normalize_yougile."""

    def __init__(self, *, api_key: str, api_base: str, key_pattern: str,
                 url_template: str) -> None:
        self._key_pattern = key_pattern
        self._url_template = url_template
        self._base = (api_base or "https://yougile.com/api-v2").rstrip("/")
        self._client = httpx.Client(
            base_url=self._base,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )

    def close(self) -> None:
        self._client.close()

    def _get_all(self, path: str, params: dict | None = None) -> list[dict]:
        """Пагинированный GET listing-эндпоинта (yougile: {content, paging})."""
        out: list[dict] = []
        offset = 0
        while True:
            p = dict(params or {})
            p.update({"limit": 1000, "offset": offset})
            r = self._client.get(path, params=p)
            r.raise_for_status()
            data = r.json()
            content = data.get("content", [])
            out.extend(content)
            if len(content) < 1000:
                break
            offset += len(content)
        return out

    def iter_raw(self, board: str | None, limit: int | None) -> Iterable[RawTask]:
        projects = self._get_all("/projects")
        count = 0
        for proj in projects:
            boards = self._get_all("/boards", {"projectId": proj["id"]})
            for brd in boards:
                if board and board not in (brd.get("title", ""), proj.get("title", "")):
                    continue
                col_title = {c["id"]: c.get("title") for c in
                             self._get_all("/columns", {"boardId": brd["id"]})}
                for col_id in col_title:
                    for t in self._get_all("/tasks", {"columnId": col_id}):
                        yield RawTask(
                            key=t.get("idTaskCommon") or t["id"],
                            project_code=t.get("idTaskProject", ""),
                            title=t.get("title", ""),
                            description=t.get("description", "") or "",
                            status=col_title.get(t.get("columnId")),
                            subtask_ids=list(t.get("subtasks", []) or []),
                            timestamp=int(t.get("timestamp", 0) or 0),
                        )
                        count += 1
                        if limit and count >= limit:
                            return

    def normalize(self, raw: RawTask) -> dict:
        subtask_titles: dict[str, str] = {}
        for sid in raw.subtask_ids:
            try:
                r = self._client.get(f"/tasks/{sid}")
                r.raise_for_status()
                st = r.json()
                key = st.get("idTaskCommon") or sid
                subtask_titles[sid] = f"{key}:{st.get('title', '')}"
            except Exception:
                log.warning("yougile: не резолвится подзадача %s", sid, exc_info=True)
        return normalize_yougile(raw, self._key_pattern, self._url_template,
                                 subtask_titles)
```

> Примечание по `subtask` link: в плейбуке `key` подзадачи = её `idTaskCommon`.
> Здесь ребро строится по UUID `sid` (граф принимает любой стабильный ключ), а
> человекочитаемый код кладётся в `title`. Если позже потребуется ровно
> `idTaskCommon` в `key` — резолвить его в `iter_raw`/`normalize` и подменять.
> Для критерия «PR-рёбра IMPLEMENTED_BY идентичны» это неважно: subtask-рёбра —
> это TASK_LINK, а не IMPLEMENTED_BY.

- [ ] **Step 4: Запустить — проходит**

Run: `.venv/bin/pytest tests/tasks/boards/test_yougile_normalize.py tests/tasks/boards/test_base.py -q && .venv/bin/ruff check reviewer/tasks/boards/`
Expected: PASS, ruff clean.

- [ ] **Step 5: Коммит**

```bash
git add reviewer/tasks/boards/yougile.py tests/tasks/boards/test_yougile_normalize.py
git commit -m "feat(tasks): YougileBoard REST-клиент + чистая normalize_yougile (паритет с плейбуком)"
```

---

### Task 4: SyncService — оркестрация + watermark + guards

**Files:**
- Create: `reviewer/tasks/sync.py`
- Test: `tests/tasks/test_sync.py`

**Interfaces:**
- Consumes: `TaskBoardProvider`/`RawTask` (Task 2); `TaskService.index_batch(tasks) -> list[dict]` и `TaskService.purge_orphaned_tasks(active_keys, *, keep_with_prs) -> dict` (существующие); `meta_store.get_index_meta(repo, ref) -> str|None` и `meta_store.set_index_meta(repo, ref, sha)` (существующий `ChunkStore`).
- Produces: `SyncService(provider, task_service, meta_store)` с `run(board=None, limit=None, purge_orphaned=False, keep_with_prs=True) -> dict` (summary).

**Watermark:** `repo=""`, `ref=f"tasks:{board or '*'}"`, значение = `str(max_timestamp_ms)`.

**Summary dict:** `{enumerated, changed, embedded, refreshed, unchanged, failed, purge: {deleted, protected} | None, warnings: [...], cursor_advanced: bool}`.

**Guards:** при `limit` — НЕ продвигаем курсор и НЕ запускаем purge (active_keys неполный); в warnings — пометка.

- [ ] **Step 1: Написать падающие тесты (фейки, без I/O)**

```python
# tests/tasks/test_sync.py
from reviewer.tasks.boards.base import RawTask
from reviewer.tasks.sync import SyncService


class FakeProvider:
    def __init__(self, raws):
        self._raws = raws
    def iter_raw(self, board, limit):
        n = 0
        for r in self._raws:
            yield r
            n += 1
            if limit and n >= limit:
                return
    def normalize(self, raw):
        return {"key": raw.key, "aliases": [raw.project_code], "title": raw.title,
                "description": raw.description, "criteria": [], "status": raw.status,
                "url": None, "links": []}


class FakeTaskService:
    def __init__(self):
        self.indexed = []
        self.purged_with = None
    def index_batch(self, tasks):
        self.indexed.append([t["key"] for t in tasks])
        return [{"key": t["key"], "embedded": True, "links_upserted": 0,
                 "prs_linked": 0, "warnings": []} for t in tasks]
    def purge_orphaned_tasks(self, active_keys, *, keep_with_prs=True):
        self.purged_with = (sorted(active_keys), keep_with_prs)
        return {"deleted_store": 1, "deleted_graph": 1, "protected_prs": 0,
                "warnings": []}


class FakeMeta:
    def __init__(self, val=None):
        self.store = {}
        if val is not None:
            self.store[("", "tasks:*")] = val
    def get_index_meta(self, repo, ref):
        return self.store.get((repo, ref))
    def set_index_meta(self, repo, ref, sha):
        self.store[(repo, ref)] = sha


def _raw(key, ts):
    return RawTask(key=key, project_code=key.replace("ID", "PRI"), title=key,
                   description="", status="S", subtask_ids=[], timestamp=ts)


def test_first_sync_indexes_all_and_advances_cursor():
    prov = FakeProvider([_raw("ID-1", 100), _raw("ID-2", 200)])
    ts, meta = FakeTaskService(), FakeMeta()
    svc = SyncService(prov, ts, meta)
    summary = svc.run()
    assert ts.indexed == [["ID-1", "ID-2"]]
    assert summary["changed"] == 2 and summary["unchanged"] == 0
    assert summary["cursor_advanced"] is True
    assert meta.store[("", "tasks:*")] == "200"


def test_watermark_skips_unchanged():
    prov = FakeProvider([_raw("ID-1", 100), _raw("ID-2", 200)])
    ts, meta = FakeTaskService(), FakeMeta(val="150")
    svc = SyncService(prov, ts, meta)
    summary = svc.run()
    assert ts.indexed == [["ID-2"]]            # ID-1 (ts=100<=150) пропущена
    assert summary["changed"] == 1 and summary["unchanged"] == 1
    assert meta.store[("", "tasks:*")] == "200"


def test_purge_uses_full_active_keys():
    prov = FakeProvider([_raw("ID-1", 100), _raw("ID-2", 200)])
    ts, meta = FakeTaskService(), FakeMeta(val="999")  # обе unchanged
    svc = SyncService(prov, ts, meta)
    summary = svc.run(purge_orphaned=True, keep_with_prs=False)
    assert ts.purged_with == (["ID-1", "ID-2"], False)   # полный набор ключей
    assert summary["purge"]["deleted"] == 2


def test_limit_disables_purge_and_cursor():
    prov = FakeProvider([_raw("ID-1", 100), _raw("ID-2", 200)])
    ts, meta = FakeTaskService(), FakeMeta()
    svc = SyncService(prov, ts, meta)
    summary = svc.run(limit=1, purge_orphaned=True)
    assert ts.purged_with is None                # purge выключен под limit
    assert summary["cursor_advanced"] is False
    assert ("", "tasks:*") not in meta.store     # курсор не записан
    assert any("limit" in w for w in summary["warnings"])
```

- [ ] **Step 2: Запустить — падает**

Run: `.venv/bin/pytest tests/tasks/test_sync.py -q`
Expected: FAIL (`ModuleNotFoundError: reviewer.tasks.sync`).

- [ ] **Step 3: Реализация `reviewer/tasks/sync.py`**

```python
"""Оркестратор server-side синка доски: enumerate → watermark → normalize →
index_batch → purge. Board-агностичен (видит только TaskBoardProvider).

Курсор инкрементальности — в index_meta (repo="", ref="tasks:<board>"), значение
= str(max timestamp ms). Полный enumerate всегда (нужно для purge active-keys и
свежести статусов); normalize/index пропускаются для timestamp <= cursor.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_CURSOR_REPO = ""  # задачи глобальны (таблица tasks без repo-скоупа)


class SyncService:
    def __init__(self, provider, task_service, meta_store) -> None:
        self._provider = provider
        self._tasks = task_service
        self._meta = meta_store

    def _cursor_ref(self, board: str | None) -> str:
        return f"tasks:{board or '*'}"

    def run(self, board=None, limit=None, purge_orphaned=False,
            keep_with_prs=True) -> dict:
        warnings: list[str] = []
        ref = self._cursor_ref(board)
        try:
            prev = self._meta.get_index_meta(_CURSOR_REPO, ref)
            cursor = int(prev) if prev else 0
        except Exception:
            log.warning("sync: сбой чтения курсора", exc_info=True)
            cursor = 0

        active_keys: list[str] = []
        changed: list[dict] = []
        max_ts = cursor
        unchanged = 0

        for raw in self._provider.iter_raw(board, limit):
            active_keys.append(raw.key)
            max_ts = max(max_ts, raw.timestamp)
            if raw.timestamp <= cursor:
                unchanged += 1
                continue
            try:
                changed.append(self._provider.normalize(raw))
            except Exception as e:
                log.warning("sync: сбой нормализации %s", raw.key, exc_info=True)
                warnings.append(f"normalize {raw.key}: {type(e).__name__}: {e}")

        results = self._tasks.index_batch(changed) if changed else []
        embedded = sum(1 for r in results if r.get("embedded"))
        refreshed = sum(1 for r in results if not r.get("embedded")
                        and not r.get("warnings"))
        failed = sum(1 for r in results if r.get("warnings"))
        for r in results:
            warnings.extend(r.get("warnings") or [])

        partial = bool(limit)
        purge_summary = None
        if purge_orphaned and partial:
            warnings.append("purge пропущен: задан --limit (active_keys неполный)")
        elif purge_orphaned:
            pr = self._tasks.purge_orphaned_tasks(active_keys,
                                                  keep_with_prs=keep_with_prs)
            purge_summary = {"deleted": pr["deleted_store"] + pr["deleted_graph"],
                             "protected": pr["protected_prs"]}
            warnings.extend(pr.get("warnings") or [])

        cursor_advanced = False
        if partial:
            warnings.append("курсор не продвинут: задан --limit (частичный обход)")
        elif max_ts > cursor:
            try:
                self._meta.set_index_meta(_CURSOR_REPO, ref, str(max_ts))
                cursor_advanced = True
            except Exception:
                log.warning("sync: сбой записи курсора", exc_info=True)

        return {
            "enumerated": len(active_keys),
            "changed": len(changed),
            "embedded": embedded,
            "refreshed": refreshed,
            "unchanged": unchanged,
            "failed": failed,
            "purge": purge_summary,
            "warnings": warnings,
            "cursor_advanced": cursor_advanced,
        }
```

> `refreshed`/`failed` в фейк-тестах не проверяются точечно — фейк всегда
> `embedded=True`, так что `changed==embedded`. Реальные значения покрывает
> интеграционный тест (Task 6).

- [ ] **Step 4: Запустить — проходит**

Run: `.venv/bin/pytest tests/tasks/test_sync.py -q && .venv/bin/ruff check reviewer/tasks/sync.py`
Expected: PASS (4 теста), ruff clean.

- [ ] **Step 5: Коммит**

```bash
git add reviewer/tasks/sync.py tests/tasks/test_sync.py
git commit -m "feat(tasks): SyncService — оркестрация синка с watermark и guard'ами"
```

---

### Task 5: Wiring — build_components + MCPReviewService.sync_board + MCP-тул

**Files:**
- Modify: `reviewer/app.py` (Components + build_components)
- Modify: `reviewer/mcp/service.py` (+ `sync_board`)
- Modify: `reviewer/entrypoints/mcp_server.py` (+ `@mcp.tool sync_board`; обновить docstring счётчик тулов)
- Test: `tests/mcp/test_server_tools.py` (добавить кейс) или новый `tests/mcp/test_sync_board.py`

**Interfaces:**
- Consumes: `make_board_provider(settings)` (Task 2), `SyncService` (Task 4).
- Produces: `Components.sync_service: SyncService | None`; `MCPReviewService.sync_board(board, limit, purge_orphaned, keep_with_prs) -> dict`; MCP-тул `sync_board`.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/mcp/test_sync_board.py
from reviewer.mcp.service import MCPReviewService


class _Svc(MCPReviewService):
    def __init__(self, sync_service):
        # обходим тяжёлый __init__: ставим только нужное для sync_board
        self.components = type("C", (), {"sync_service": sync_service})()


def test_sync_board_no_provider_returns_error():
    svc = _Svc(None)
    out = svc.sync_board()
    assert out["status"] == "error"
    assert "board" in out["reason"].lower()


def test_sync_board_delegates_to_sync_service():
    class FakeSync:
        def run(self, board=None, limit=None, purge_orphaned=False,
                keep_with_prs=True):
            return {"enumerated": 3, "changed": 1, "warnings": []}
    svc = _Svc(FakeSync())
    out = svc.sync_board(board="B", limit=5)
    assert out["enumerated"] == 3 and out["changed"] == 1
```

- [ ] **Step 2: Запустить — падает**

Run: `.venv/bin/pytest tests/mcp/test_sync_board.py -q`
Expected: FAIL (`AttributeError: sync_board` / `sync_service`).

- [ ] **Step 3a: `reviewer/app.py`**

Импорты:

```python
from reviewer.tasks.boards import make_board_provider
from reviewer.tasks.sync import SyncService
```

В `Components` добавить поле:

```python
    sync_service: "SyncService | None"
```

В конце `build_components` перед `return`:

```python
    provider = make_board_provider(settings)
    sync_service = SyncService(provider, task_service, store) \
        if provider is not None else None
```

И в `return Components(...)` добавить `sync_service` последним аргументом.

- [ ] **Step 3b: `reviewer/mcp/service.py`** (рядом с `purge_orphaned_tasks`, ~строка 331):

```python
    def sync_board(self, board: str | None = None, limit: int | None = None,
                   purge_orphaned: bool = False, keep_with_prs: bool = True) -> dict:
        """Server-side ETL: перечислить доску по REST, нормализовать, проиндексировать.

        Доска/ключ не настроены → понятный error-summary (fail-soft), без падения.
        """
        sync = getattr(self.components, "sync_service", None)
        if sync is None:
            return {"status": "error",
                    "reason": "task board REST not configured "
                              "(set TASK_BOARD_TYPE + TASK_BOARD_API_KEY)"}
        try:
            return sync.run(board=board, limit=limit,
                            purge_orphaned=purge_orphaned,
                            keep_with_prs=keep_with_prs)
        except Exception as e:
            log.warning("sync_board: сбой синка", exc_info=True)
            return {"status": "error",
                    "reason": f"{type(e).__name__}: {e}"}
```

- [ ] **Step 3c: `reviewer/entrypoints/mcp_server.py`** (рядом с `purge_orphaned_tasks`, ~строка 80):

```python
    @mcp.tool()
    def sync_board(board: str | None = None, limit: int | None = None,
                   purge_orphaned: bool = False, keep_with_prs: bool = True) -> dict:
        """Server-side ETL: enumerate the configured task board via REST, normalize,
        and index it (vector store + task graph). The LLM passes no task payload —
        all enumeration/normalization happens server-side. Returns a compact counts
        summary. Incremental via a per-board timestamp watermark."""
        return service.sync_board(board, limit, purge_orphaned, keep_with_prs)
```

Обновить docstring `create_server` («с 19 тулов» → «с 20 тулами»).

- [ ] **Step 4: Запустить — проходит (+ не сломаны существующие)**

Run: `.venv/bin/pytest tests/mcp/test_sync_board.py tests/mcp/ tests/tasks/ tests/config/ -q && .venv/bin/ruff check reviewer/`
Expected: PASS, ruff clean.

- [ ] **Step 5: Коммит**

```bash
git add reviewer/app.py reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp/test_sync_board.py
git commit -m "feat(mcp): тул sync_board + проводка SyncService в build_components"
```

---

### Task 6: Интеграционный тест sync_board (идемпотентность + PR-рёбра)

**Files:**
- Test: `tests/tasks/test_sync_integration.py` (маркер `integration`)

**Interfaces:**
- Consumes: `SyncService`, `build_components`, реальные Postgres/Neo4j.

Тест строит `SyncService` с **фейковым provider'ом** (чтобы не дёргать живой yougile), но реальными `TaskService`/`ChunkStore`/`TaskGraph` из `build_components`, и проверяет:
1. Первый `run()` индексирует все задачи (`changed == N`, `embedded == N`).
2. Второй `run()` без изменений: `changed == 0`, `unchanged == N`, `cursor_advanced == False`.
3. Задача с PR-URL в description → ребро `:Task-[:IMPLEMENTED_BY]->:PR` в Neo4j.

- [ ] **Step 1: Написать тест**

```python
# tests/tasks/test_sync_integration.py
import pytest

from reviewer.app import build_components
from reviewer.config.settings import Settings
from reviewer.tasks.boards.base import RawTask
from reviewer.tasks.sync import SyncService

pytestmark = pytest.mark.integration


class FakeProvider:
    def __init__(self, raws):
        self._raws = raws
    def iter_raw(self, board, limit):
        for r in self._raws:
            yield r
    def normalize(self, raw):
        return {"key": raw.key, "aliases": [raw.project_code], "title": raw.title,
                "description": raw.description, "criteria": [], "status": raw.status,
                "url": None, "links": []}


def _raw(key, ts, desc=""):
    return RawTask(key=key, project_code=key.replace("ID", "PRI"), title=key,
                   description=desc, status="S", subtask_ids=[], timestamp=ts)


def test_sync_idempotent_and_pr_edge():
    s = Settings()
    comps = build_components(s, connect=True)
    raws = [_raw("ZID-901", 1000),
            _raw("ZID-902", 1000,
                 desc="impl https://github.com/o/r/pull/7")]
    prov = FakeProvider(raws)
    svc = SyncService(prov, comps.task_service, comps.store)

    first = svc.run(board="ztest")
    assert first["changed"] == 2 and first["embedded"] == 2

    second = svc.run(board="ztest")
    assert second["changed"] == 0 and second["unchanged"] == 2
    assert second["cursor_advanced"] is False

    # PR-ребро создано
    if comps.task_graph is not None:
        ctx = comps.task_service.get_task_context("ZID-902")
        assert "o/r" in ctx or "pull/7" in ctx

    # cleanup
    comps.task_service.purge_orphaned_tasks([], keep_with_prs=False)
    comps.store.set_index_meta("", "tasks:ztest", "0")
```

- [ ] **Step 2: Запустить (нужны Postgres/Neo4j + Voyage)**

Run: `.venv/bin/pytest tests/tasks/test_sync_integration.py -m integration -q`
Expected: PASS (или SKIP, если инфра/ключ недоступны — тогда зафиксировать в отчёте).

- [ ] **Step 3: Коммит**

```bash
git add tests/tasks/test_sync_integration.py
git commit -m "test(tasks): интеграционный sync_board — идемпотентность и PR-рёбра"
```

---

### Task 7: Скилл sync-tasks → тонкий триггер + документация

**Files:**
- Modify: `plugin/skills/sync-tasks/SKILL.md` (упростить до вызова `sync_board`)
- Delete: `plugin/skills/sync-tasks/references/sync-tasks-yougile.md`
- Modify: `CLAUDE.md` (раздел «Неочевидные факты» — инверсия инварианта)
- Modify: `README.md` (упоминание sync-tasks/доски, если есть)

- [ ] **Step 1: Переписать `SKILL.md`**

Тело скилла — на английском (токены), но инструктирует отвечать пользователю по-русски.
Новый Pipeline:
1. Распарсить `$ARGUMENTS`: `--board <name>`, `--limit <N>`, `--purge-orphaned`, `--no-keep-with-prs`.
2. Один вызов `sync_board(board=..., limit=..., purge_orphaned=..., keep_with_prs=...)`.
3. Напечатать summary (counts) по-русски; при `status=="error"` — показать `reason` и подсказку про `TASK_BOARD_API_KEY`.
Убрать: ручной обход доски, `index_tasks_batch([...])`, нормализацию в LLM, плейбук-ссылку на enumerate.

- [ ] **Step 2: Удалить устаревший reference**

```bash
git rm plugin/skills/sync-tasks/references/sync-tasks-yougile.md
```

- [ ] **Step 3: Обновить `CLAUDE.md`**

В раздел «Неочевидные факты» — заметка: болк-синк (`sync_board`) теперь ходит на доску по REST в MCP-слое (`reviewer/tasks/boards/`, `TaskBoardProvider`), креды в env reviewer-mcp (`TASK_BOARD_API_KEY`/`TASK_BOARD_API_BASE`); инкрементальность — timestamp-watermark в `index_meta` (`ref="tasks:<board>"`). Инвариант «reviewer Python никогда не трогает доску» получает документированное исключение **для болк-синка**; одиночное чтение задачи в `solve-task`/`review-pr` по-прежнему через board-MCP на стороне LLM. `--limit` отключает purge и продвижение курсора.

- [ ] **Step 4: Проверить ссылки/упоминания в README.md**

Если README описывает старый sync-tasks-поток — обновить на `sync_board`. Если нет — пропустить (зафиксировать в отчёте).

- [ ] **Step 5: Коммит**

```bash
git add plugin/skills/sync-tasks/ CLAUDE.md README.md
git commit -m "docs(sync-tasks): скилл → тонкий триггер sync_board; зафиксировать REST-синк в CLAUDE.md"
```

---

### Task 8: Полный прогон тестов + PR

- [ ] **Step 1: Unit-прогон целиком**

Run: `.venv/bin/pytest -q`
Expected: PASS (integration исключены по умолчанию).

- [ ] **Step 2: Линт**

Run: `.venv/bin/ruff check reviewer/ tests/`
Expected: clean на новых/изменённых файлах (repo-wide clean не гнаться — см. память).

- [ ] **Step 3: Push + PR**

```bash
git push -u origin feat/pri-140-sync-board-etl
gh pr create --base dev --title "feat: server-side ETL для sync-tasks (sync_board) — PRI-140" \
  --body "<сводка: что, зачем, критерии, тесты>"
```

PR body: проблема (LLM-копировальная машина, ~73k токенов), решение (REST-провайдер + sync_board + watermark), переиспользование index_batch/purge/PR-линковки, критерии приёмки, тестовое покрытие, вне скоупа (CLI, jira, удаление index_tasks_batch).

---

## Self-Review (выполнено при написании плана)

- **Spec coverage:** O(1)-токенов → Task 5/7 (тул без payload + тонкий скилл). Идентичность TaskBrief/links/IMPLEMENTED_BY → Task 3 (паритет) + Task 6 (интеграция). Watermark → Task 4. `--limit`/board/`--purge-orphaned`+`keep_with_prs` → Task 4/5. Unit+integration → Task 3/4/6. Инверсия инварианта в доках → Task 7. ✔
- **Placeholder scan:** код во всех шагах конкретный. ✔
- **Type consistency:** `RawTask`(Task2) поля = `iter_raw`/`normalize_yougile`(Task3) = фейки(Task4/6). `index_batch`/`purge_orphaned_tasks` сигнатуры сверены с `reviewer/tasks/service.py`. `get_index_meta`/`set_index_meta` сверены с `reviewer/index/store.py`. ✔
- **Коррекция против спеки:** курсор хранится через существующий `ChunkStore.get/set_index_meta` (а не новые методы `TaskStore`) — таблица `index_meta` та же, кода меньше (DRY). Ключ курсора `repo=""` (задачи глобальны), `ref="tasks:<board>"`.
