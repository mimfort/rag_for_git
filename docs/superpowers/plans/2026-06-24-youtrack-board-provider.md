# YouTrack Board Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить YouTrack (JetBrains) как тип доски задач наравне с Yougile — server-side REST-синком (без board-MCP), с per-type связкой ключей в env и выбором доски в `.review.yml`; расширить скиллы configure-review и review-pr.

**Architecture:** Доска — провайдер за `TaskBoardProvider` (Protocol). env держит креды всех досок (форма A: `YOUGILE_API_KEY`, `YOUTRACK_TOKEN`, `YOUTRACK_BASE_URL`); `make_board_providers(settings)` строит все настроенные; `SyncService` обходит их и синкает каждую в общий глобальный пул задач со своим watermark-курсором `tasks:<type>:<board>`. `.review.yml task_board.type` — per-repo client-выбор (key_pattern/url/чтение), серверный синк не трогает.

**Tech Stack:** Python 3.11–3.13, pydantic-settings, httpx, pytest. Граф/вектора — Neo4j/ParadeDB (не затрагиваются этим планом, кроме существующего `index_batch`).

## Global Constraints

- Язык кода/докстрингов/комментариев/CLI — **русский** (стиль существующего кода).
- Коммиты — **Conventional Commits на русском, БЕЗ self-attribution** (никаких `Co-Authored-By`/упоминаний Claude).
- ruff: line-length **100**, target **py311** (`.venv/bin/ruff check .` по затронутым файлам).
- Unit-тесты — на фейках, внешние API (httpx/Voyage/Postgres/Neo4j) **не дёргают**; integration-тесты помечены `@pytest.mark.integration` и по умолчанию исключены (`addopts = -m 'not integration'`).
- Секреты (ключи досок) — **только в env**, никогда в `.review.yml`.
- Тесты гонять через `.venv/bin/pytest -q`.

---

### Task 1: Settings — per-type креды досок

**Files:**
- Modify: `reviewer/config/settings.py:100-107` (поля), `:142-144` (рядом с `task_board_api_base_for`)
- Test: `tests/config/test_settings.py`

**Interfaces:**
- Produces: `Settings.board_creds(type_: str) -> tuple[str, str]` (api_key, api_base); `Settings.configured_board_types() -> list[str]`.
- Новые поля Settings: `yougile_api_key`, `yougile_api_base`, `youtrack_token`, `youtrack_base_url` (str, дефолт `""`).
- Consumes: существующий `Settings.task_board_api_base_for(type_)` и `_BOARD_API_BASE_DEFAULTS`.

- [ ] **Step 1: Написать падающие тесты**

В `tests/config/test_settings.py` добавить в конец файла:

```python
def test_board_creds_yougile_from_per_type(monkeypatch):
    s = Settings(_env_file=None, yougile_api_key="yk",
                 yougile_api_base="https://ru.yougile.com/api-v2")
    assert s.board_creds("yougile") == ("yk", "https://ru.yougile.com/api-v2")


def test_board_creds_yougile_legacy_fallback(monkeypatch):
    # старые деплои: только TASK_BOARD_API_KEY/API_BASE
    s = Settings(_env_file=None, task_board_api_key="legacy", task_board_api_base="")
    assert s.board_creds("yougile") == ("legacy", "https://yougile.com/api-v2")


def test_board_creds_youtrack(monkeypatch):
    s = Settings(_env_file=None, youtrack_token="perm:abc",
                 youtrack_base_url="https://c.youtrack.cloud/api")
    assert s.board_creds("youtrack") == ("perm:abc", "https://c.youtrack.cloud/api")


def test_board_creds_unknown_type_empty():
    assert Settings(_env_file=None).board_creds("jira") == ("", "")


def test_configured_board_types_lists_only_with_key(monkeypatch):
    s = Settings(_env_file=None, yougile_api_key="yk", youtrack_token="")
    assert s.configured_board_types() == ["yougile"]


def test_configured_board_types_both(monkeypatch):
    s = Settings(_env_file=None, yougile_api_key="yk", youtrack_token="perm:x",
                 youtrack_base_url="https://c.youtrack.cloud/api")
    assert s.configured_board_types() == ["yougile", "youtrack"]


def test_configured_board_types_empty_when_nothing(monkeypatch):
    for k in ("TASK_BOARD_API_KEY", "YOUGILE_API_KEY", "YOUTRACK_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert Settings(_env_file=None).configured_board_types() == []
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/config/test_settings.py -q`
Expected: FAIL (`AttributeError: 'Settings' object has no attribute 'board_creds'`).

- [ ] **Step 3: Добавить поля и методы**

В `reviewer/config/settings.py` после строки `task_board_api_base: str = ""` (строка 107) вставить:

```python
    # per-type связка ключей REST-досок (форма A). yougile фолбэчит на legacy
    # TASK_BOARD_API_KEY/API_BASE (обратная совместимость старых деплоев).
    yougile_api_key: str = ""          # ключ yougile (приоритет над legacy TASK_BOARD_API_KEY)
    yougile_api_base: str = ""         # base URL yougile; пусто → дефолт по типу
    youtrack_token: str = ""           # permanent token youtrack (perm:...)
    youtrack_base_url: str = ""        # base URL youtrack API; обязателен (инстанс-специфичен)
```

После метода `task_board_api_base_for` (после строки 144) добавить:

```python
    def board_creds(self, type_: str) -> tuple[str, str]:
        """REST-креды доски по типу: (api_key, api_base). Пустой api_key = доска
        этого типа не настроена. yougile фолбэчит на legacy TASK_BOARD_API_KEY/BASE."""
        if type_ == "yougile":
            api_key = self.yougile_api_key or self.task_board_api_key
            api_base = self.yougile_api_base or self.task_board_api_base_for("yougile")
            return api_key, api_base
        if type_ == "youtrack":
            return self.youtrack_token, self.youtrack_base_url
        return "", ""

    def configured_board_types(self) -> list[str]:
        """Типы досок с заданным REST-ключом — для перебора в make_board_providers."""
        return [t for t in ("yougile", "youtrack") if self.board_creds(t)[0]]
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `.venv/bin/pytest tests/config/test_settings.py -q`
Expected: PASS (включая существующие `task_board_api_base_*` тесты).

- [ ] **Step 5: Коммит**

```bash
.venv/bin/ruff check reviewer/config/settings.py tests/config/test_settings.py
git add reviewer/config/settings.py tests/config/test_settings.py
git commit -m "feat(config): per-type креды досок (board_creds/configured_board_types)"
```

---

### Task 2: Контракт провайдера — RawTask.links + board_type

**Files:**
- Modify: `reviewer/tasks/boards/base.py:14-39`
- Modify: `reviewer/tasks/boards/yougile.py:71-84` (добавить `board_type`)
- Test: `tests/tasks/boards/test_base.py`

**Interfaces:**
- Produces: `RawTask.links: list[dict]` (default `[]`); `TaskBoardProvider.board_type: str`; `YougileBoard.board_type == "yougile"`.
- Consumes: ничего нового.

- [ ] **Step 1: Написать падающий тест**

В `tests/tasks/boards/test_base.py` заменить `test_rawtask_fields` на:

```python
def test_rawtask_fields_and_links_default():
    rt = RawTask(key="ID-1", project_code="PRI-1", title="t", description="d",
                 status="Backlog", subtask_ids=["u1"], timestamp=123)
    assert rt.key == "ID-1" and rt.timestamp == 123
    assert rt.links == []                       # links — необязательное, дефолт пуст


def test_rawtask_links_explicit():
    rt = RawTask(key="A-1", project_code="A-1", title="t", description="",
                 status=None, subtask_ids=[], timestamp=1,
                 links=[{"type": "related", "key": "A-2"}])
    assert rt.links == [{"type": "related", "key": "A-2"}]
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/tasks/boards/test_base.py::test_rawtask_links_default -q`
Expected: FAIL (`TypeError: __init__() got an unexpected keyword argument 'links'`).

- [ ] **Step 3: Расширить RawTask и Protocol**

В `reviewer/tasks/boards/base.py` импорт `field` уже есть (`from dataclasses import dataclass, field`? — проверить; сейчас `from dataclasses import dataclass`). Заменить строку импорта:

```python
from dataclasses import dataclass, field
```

В `RawTask` после `timestamp: int` (строка 23) добавить:

```python
    links: list[dict] = field(default_factory=list)  # предрезолвленные ссылки
    # (youtrack кладёт сразу в iter_raw; yougile оставляет пустым, резолвит в normalize)
```

В Protocol `TaskBoardProvider` (после докстринга, перед `def iter_raw`) добавить атрибут:

```python
    board_type: str  # ключ типа доски для курсора синка (напр. "yougile", "youtrack")
```

В `reviewer/tasks/boards/yougile.py` в класс `YougileBoard` сразу после строки докстринга класса (перед `def __init__`) добавить:

```python
    board_type = "yougile"
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `.venv/bin/pytest tests/tasks/boards/test_base.py tests/tasks/boards/test_yougile_normalize.py -q`
Expected: PASS (yougile-normalize не сломан — `links` дефолтится).

- [ ] **Step 5: Коммит**

```bash
.venv/bin/ruff check reviewer/tasks/boards/base.py reviewer/tasks/boards/yougile.py tests/tasks/boards/test_base.py
git add reviewer/tasks/boards/base.py reviewer/tasks/boards/yougile.py tests/tasks/boards/test_base.py
git commit -m "feat(tasks): RawTask.links + board_type в контракте провайдера"
```

---

### Task 3: YouTrackBoard + normalize_youtrack

**Files:**
- Create: `reviewer/tasks/boards/youtrack.py`
- Test: `tests/tasks/boards/test_youtrack_normalize.py`

**Interfaces:**
- Consumes: `RawTask` (с `links`), `Settings.board_creds` (косвенно через фабрику в Task 4).
- Produces: `YouTrackBoard(token, base_url, key_pattern)` с `board_type == "youtrack"`, методами `iter_raw/normalize/close`; чистые `normalize_youtrack(raw, key_pattern, base_url) -> dict`, `_issue_to_raw(issue: dict) -> RawTask`.

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/tasks/boards/test_youtrack_normalize.py`:

```python
from reviewer.tasks.boards.base import RawTask
from reviewer.tasks.boards.youtrack import _issue_to_raw, normalize_youtrack

KP = r"[A-Z]+-\d+"
BASE = "https://c.youtrack.cloud/api"


def _issue(**kw):
    base = {
        "idReadable": "PRJ-7",
        "summary": "Заголовок",
        "description": "",
        "updated": 1700000000000,
        "customFields": [{"name": "State", "value": {"name": "In Progress"}}],
        "links": [],
    }
    base.update(kw)
    return base


def test_issue_to_raw_basic():
    raw = _issue_to_raw(_issue())
    assert raw.key == "PRJ-7"
    assert raw.project_code == "PRJ-7"
    assert raw.title == "Заголовок"
    assert raw.status == "In Progress"
    assert raw.timestamp == 1700000000000
    assert raw.subtask_ids == []


def test_issue_to_raw_state_missing():
    raw = _issue_to_raw(_issue(customFields=[{"name": "Priority",
                                              "value": {"name": "High"}}]))
    assert raw.status is None


def test_issue_to_raw_links_subtask_vs_related():
    issue = _issue(links=[
        {"direction": "OUTWARD", "linkType": {"name": "Subtask"},
         "issues": [{"idReadable": "PRJ-8"}]},
        {"direction": "OUTWARD", "linkType": {"name": "Relates"},
         "issues": [{"idReadable": "PRJ-9"}]},
    ])
    raw = _issue_to_raw(issue)
    assert {"type": "subtask", "key": "PRJ-8"} in raw.links
    assert {"type": "related", "key": "PRJ-9"} in raw.links


def test_normalize_url_and_aliases():
    raw = _issue_to_raw(_issue())
    b = normalize_youtrack(raw, KP, BASE)
    assert b["key"] == "PRJ-7"
    assert b["aliases"] == []
    assert b["criteria"] == []
    assert b["status"] == "In Progress"
    assert b["url"] == "https://c.youtrack.cloud/issue/PRJ-7"   # /api отброшен


def test_normalize_url_none_without_base():
    raw = _issue_to_raw(_issue())
    assert normalize_youtrack(raw, KP, "")["url"] is None


def test_normalize_related_from_description_excludes_self_and_links():
    issue = _issue(description="зависит от ABC-1 и PRJ-7 и PRJ-8",
                   links=[{"direction": "OUTWARD", "linkType": {"name": "Subtask"},
                           "issues": [{"idReadable": "PRJ-8"}]}])
    raw = _issue_to_raw(issue)
    b = normalize_youtrack(raw, KP, BASE)
    rels = {lk["key"] for lk in b["links"] if lk["type"] == "related"}
    assert rels == {"ABC-1"}            # self PRJ-7 и уже-связанный PRJ-8 исключены


def test_normalize_null_description():
    raw = _issue_to_raw(_issue(description=None))
    b = normalize_youtrack(raw, KP, BASE)
    assert b["description"] == ""
    assert b["links"] == []
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/tasks/boards/test_youtrack_normalize.py -q`
Expected: FAIL (`ModuleNotFoundError: reviewer.tasks.boards.youtrack`).

- [ ] **Step 3: Создать провайдер**

Создать `reviewer/tasks/boards/youtrack.py`:

```python
"""Провайдер доски YouTrack (JetBrains): REST-клиент (httpx) + нормализация.

REST API: base <instance>/api, заголовок Authorization: Bearer perm:<token>.
Один list-эндпоинт /issues с богатым `fields` отдаёт всё (idReadable, summary,
description, updated, State, links) без доп. запросов, поэтому normalize чистая,
а links резолвятся уже в iter_raw. Порт client-side маппинга задачи в TaskBrief.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

import httpx

from reviewer.tasks.boards.base import RawTask

_PAGE = 200

_FIELDS = (
    "idReadable,summary,description,updated,"
    "customFields(name,value(name)),"
    "links(direction,linkType(name),issues(idReadable))"
)


def _state_of(issue: dict) -> str | None:
    """Статус задачи — кастом-поле «State» (его value.name)."""
    for cf in issue.get("customFields") or []:
        if cf.get("name") == "State":
            val = cf.get("value")
            return val.get("name") if isinstance(val, dict) else None
    return None


def _links_of(issue: dict) -> list[dict]:
    """Ссылки из issueLinks: linkType «Subtask» → subtask, иначе related."""
    out: list[dict] = []
    for ln in issue.get("links") or []:
        name = ((ln.get("linkType") or {}).get("name") or "")
        typ = "subtask" if "subtask" in name.lower() else "related"
        for iss in ln.get("issues") or []:
            key = iss.get("idReadable")
            if key:
                out.append({"type": typ, "key": key})
    return out


def _issue_to_raw(issue: dict) -> RawTask:
    """YouTrack issue JSON → RawTask. Чистая: без I/O."""
    key = issue.get("idReadable", "")
    return RawTask(
        key=key,
        project_code=key,                       # один счётчик idReadable, второго кода нет
        title=issue.get("summary", "") or "",
        description=issue.get("description", "") or "",
        status=_state_of(issue),
        subtask_ids=[],
        timestamp=int(issue.get("updated", 0) or 0),
        links=_links_of(issue),
    )


def normalize_youtrack(raw: RawTask, key_pattern: str, base_url: str) -> dict:
    """RawTask → TaskBrief dict. Чистая: без I/O. url выводится из base_url."""
    key = raw.key
    links: list[dict] = list(raw.links)
    covered: set[str] = {key} | {lk["key"] for lk in links}
    if key_pattern:
        seen: set[str] = set()
        for m in re.finditer(key_pattern, raw.description or ""):
            code = m.group(0)
            if code in covered or code in seen:
                continue
            seen.add(code)
            links.append({"type": "related", "key": code})

    web = re.sub(r"/api/?$", "", base_url.rstrip("/"))   # web-база = api-база без /api
    url = f"{web}/issue/{key}" if web and key else None
    return {
        "key": key,
        "aliases": [],
        "title": raw.title,
        "description": raw.description,
        "criteria": [],
        "status": raw.status,
        "url": url,
        "links": links,
    }


class YouTrackBoard:
    """REST-провайдер YouTrack. iter_raw — один пагинированный /issues-запрос
    с богатым `fields`; normalize чистая (всё уже в RawTask)."""

    board_type = "youtrack"

    def __init__(self, *, token: str, base_url: str, key_pattern: str) -> None:
        self._key_pattern = key_pattern
        self._base = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self._base,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )

    def close(self) -> None:
        self._client.close()

    def iter_raw(self, board: str | None, limit: int | None) -> Iterable[RawTask]:
        count = 0
        skip = 0
        while True:
            params: dict = {"fields": _FIELDS, "$top": _PAGE, "$skip": skip}
            if board:
                params["query"] = f"project: {board}"
            r = self._client.get("/issues", params=params)
            r.raise_for_status()
            page = r.json()
            for issue in page:
                yield _issue_to_raw(issue)
                count += 1
                if limit and count >= limit:
                    return
            if len(page) < _PAGE:
                return
            skip += len(page)

    def normalize(self, raw: RawTask) -> dict:
        return normalize_youtrack(raw, self._key_pattern, self._base)
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `.venv/bin/pytest tests/tasks/boards/test_youtrack_normalize.py -q`
Expected: PASS (все 7 тестов).

- [ ] **Step 5: Коммит**

```bash
.venv/bin/ruff check reviewer/tasks/boards/youtrack.py tests/tasks/boards/test_youtrack_normalize.py
git add reviewer/tasks/boards/youtrack.py tests/tasks/boards/test_youtrack_normalize.py
git commit -m "feat(tasks): YouTrackBoard REST-провайдер + normalize_youtrack"
```

---

### Task 4: Фабрика провайдеров по типу + make_board_providers

**Files:**
- Modify: `reviewer/tasks/boards/__init__.py:1-27`
- Test: `tests/tasks/boards/test_base.py`

**Interfaces:**
- Consumes: `Settings.board_creds(type_)`, `Settings.configured_board_types()`, `Settings.task_board_key_pattern`, `Settings.task_board_url_template`, `YougileBoard`, `YouTrackBoard`.
- Produces: `make_board_provider(settings, type_: str) -> TaskBoardProvider | None`; `make_board_providers(settings) -> list[TaskBoardProvider]`.

- [ ] **Step 1: Обновить и дописать тесты**

В `tests/tasks/boards/test_base.py` заменить блок импортов и тесты фабрики (строки 1-2 и 11-27) на:

```python
from reviewer.config.settings import Settings
from reviewer.tasks.boards import (
    RawTask, make_board_provider, make_board_providers,
)


def test_make_provider_none_when_no_api_key():
    s = Settings(_env_file=None, yougile_api_key="")
    assert make_board_provider(s, "yougile") is None


def test_make_provider_unknown_type_none():
    s = Settings(_env_file=None)
    assert make_board_provider(s, "jira") is None


def test_make_provider_yougile():
    s = Settings(_env_file=None, yougile_api_key="k",
                 task_board_key_pattern=r"[A-Z]+-\d+")
    prov = make_board_provider(s, "yougile")
    assert prov is not None and prov.__class__.__name__ == "YougileBoard"
    assert prov.board_type == "yougile"
    prov.close()


def test_make_provider_youtrack():
    s = Settings(_env_file=None, youtrack_token="perm:x",
                 youtrack_base_url="https://c.youtrack.cloud/api",
                 task_board_key_pattern=r"[A-Z]+-\d+")
    prov = make_board_provider(s, "youtrack")
    assert prov is not None and prov.__class__.__name__ == "YouTrackBoard"
    assert prov.board_type == "youtrack"
    prov.close()


def test_make_providers_collects_all_configured():
    s = Settings(_env_file=None, yougile_api_key="yk", youtrack_token="perm:x",
                 youtrack_base_url="https://c.youtrack.cloud/api")
    provs = make_board_providers(s)
    assert {p.board_type for p in provs} == {"yougile", "youtrack"}
    for p in provs:
        p.close()


def test_make_providers_empty_when_nothing_configured():
    s = Settings(_env_file=None, yougile_api_key="", youtrack_token="",
                 task_board_api_key="")
    assert make_board_providers(s) == []
```

(тесты `test_rawtask_fields_and_links_default` / `test_rawtask_links_explicit` из Task 2 остаются.)

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/tasks/boards/test_base.py -q`
Expected: FAIL (`ImportError: cannot import name 'make_board_providers'`).

- [ ] **Step 3: Переписать фабрику**

Заменить тело `reviewer/tasks/boards/__init__.py` (строки 1-27) на:

```python
"""Провайдеры досок задач: фабрика по типу + реэкспорт интерфейса."""
from __future__ import annotations

from reviewer.tasks.boards.base import RawTask, TaskBoardProvider

__all__ = ["RawTask", "TaskBoardProvider", "make_board_provider",
           "make_board_providers"]


def make_board_provider(settings, type_: str) -> TaskBoardProvider | None:
    """Сконструировать провайдер доски заданного типа из его кредов (board_creds).

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
        )
    if type_ == "youtrack":
        from reviewer.tasks.boards.youtrack import YouTrackBoard
        return YouTrackBoard(
            token=api_key,
            base_url=api_base,
            key_pattern=key_pattern,
        )
    return None


def make_board_providers(settings) -> list[TaskBoardProvider]:
    """Все настроенные доски (по configured_board_types) — для мульти-синка."""
    out: list[TaskBoardProvider] = []
    for type_ in settings.configured_board_types():
        prov = make_board_provider(settings, type_)
        if prov is not None:
            out.append(prov)
    return out
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `.venv/bin/pytest tests/tasks/boards/ -q`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
.venv/bin/ruff check reviewer/tasks/boards/__init__.py tests/tasks/boards/test_base.py
git add reviewer/tasks/boards/__init__.py tests/tasks/boards/test_base.py
git commit -m "feat(tasks): фабрика провайдеров по типу + make_board_providers"
```

---

### Task 5: Мульти-провайдерный SyncService + app.py

**Files:**
- Modify: `reviewer/tasks/sync.py:17-93`
- Modify: `reviewer/app.py:14` (импорт), `:63-65` (wiring)
- Test: `tests/tasks/test_sync.py`
- Modify (integration, для корректности): `tests/tasks/test_sync_integration.py:20-52`

**Interfaces:**
- Consumes: `provider.board_type`, `provider.iter_raw`, `provider.normalize`, `task_service.index_batch`, `task_service.purge_orphaned_tasks`, `meta_store.get_index_meta/set_index_meta`, `make_board_providers`.
- Produces: `SyncService(providers: list, task_service, meta_store)`; курсор `tasks:<board_type>:<board or '*'>`; `run(...)` агрегирует counts по всем провайдерам, purge — по объединению active_keys.

- [ ] **Step 1: Переписать unit-тесты sync**

Заменить весь `tests/tasks/test_sync.py` на:

```python
from reviewer.tasks.boards.base import RawTask
from reviewer.tasks.sync import SyncService


class FakeProvider:
    board_type = "fake"

    def __init__(self, raws, board_type="fake"):
        self._raws = raws
        self.board_type = board_type

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
    def __init__(self, init=None):
        self.store = dict(init or {})

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
    summary = SyncService([prov], ts, meta).run()
    assert ts.indexed == [["ID-1", "ID-2"]]
    assert summary["changed"] == 2 and summary["unchanged"] == 0
    assert summary["embedded"] == 2
    assert summary["cursor_advanced"] is True
    assert meta.store[("", "tasks:fake:*")] == "200"


def test_watermark_skips_unchanged():
    prov = FakeProvider([_raw("ID-1", 100), _raw("ID-2", 200)])
    ts = FakeTaskService()
    meta = FakeMeta({("", "tasks:fake:*"): "150"})
    summary = SyncService([prov], ts, meta).run()
    assert ts.indexed == [["ID-2"]]
    assert summary["changed"] == 1 and summary["unchanged"] == 1
    assert meta.store[("", "tasks:fake:*")] == "200"


def test_no_changes_does_not_advance_cursor():
    prov = FakeProvider([_raw("ID-1", 100), _raw("ID-2", 200)])
    ts = FakeTaskService()
    meta = FakeMeta({("", "tasks:fake:*"): "200"})
    summary = SyncService([prov], ts, meta).run()
    assert ts.indexed == []
    assert summary["changed"] == 0 and summary["unchanged"] == 2
    assert summary["cursor_advanced"] is False


def test_purge_uses_full_active_keys():
    prov = FakeProvider([_raw("ID-1", 100), _raw("ID-2", 200)])
    ts = FakeTaskService()
    meta = FakeMeta({("", "tasks:fake:*"): "999"})
    summary = SyncService([prov], ts, meta).run(purge_orphaned=True, keep_with_prs=False)
    assert ts.purged_with == (["ID-1", "ID-2"], False)
    assert summary["purge"]["deleted"] == 2


def test_limit_disables_purge_and_cursor():
    prov = FakeProvider([_raw("ID-1", 100), _raw("ID-2", 200)])
    ts, meta = FakeTaskService(), FakeMeta()
    summary = SyncService([prov], ts, meta).run(limit=1, purge_orphaned=True)
    assert ts.purged_with is None
    assert summary["cursor_advanced"] is False
    assert ("", "tasks:fake:*") not in meta.store
    assert any("limit" in w for w in summary["warnings"])


def test_board_scoped_cursor_ref():
    prov = FakeProvider([_raw("ID-1", 100)])
    ts, meta = FakeTaskService(), FakeMeta()
    SyncService([prov], ts, meta).run(board="MyBoard")
    assert ("", "tasks:fake:MyBoard") in meta.store


def test_multi_provider_separate_cursors_and_union_purge():
    a = FakeProvider([_raw("ID-1", 100)], board_type="yougile")
    b = FakeProvider([_raw("ID-2", 300)], board_type="youtrack")
    ts, meta = FakeTaskService(), FakeMeta()
    summary = SyncService([a, b], ts, meta).run(purge_orphaned=True)
    # каждый провайдер — свой курсор
    assert meta.store[("", "tasks:yougile:*")] == "100"
    assert meta.store[("", "tasks:youtrack:*")] == "300"
    # counts агрегированы
    assert summary["enumerated"] == 2 and summary["changed"] == 2
    # purge — по ОБЪЕДИНЕНИЮ ключей обеих досок (иначе A удалит задачи B)
    assert ts.purged_with == (["ID-1", "ID-2"], True)


def test_empty_providers_no_crash():
    ts, meta = FakeTaskService(), FakeMeta()
    summary = SyncService([], ts, meta).run()
    assert summary["enumerated"] == 0 and summary["changed"] == 0
    assert summary["purge"] is None
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/tasks/test_sync.py -q`
Expected: FAIL (`TypeError` — `SyncService` ждёт один provider / нет `tasks:fake:*`).

- [ ] **Step 3: Переписать SyncService**

Заменить класс `SyncService` в `reviewer/tasks/sync.py` (строки 17-93) на:

```python
class SyncService:
    def __init__(self, providers, task_service, meta_store) -> None:
        self._providers = list(providers)
        self._tasks = task_service
        self._meta = meta_store

    def _cursor_ref(self, board_type: str, board: str | None) -> str:
        return f"tasks:{board_type}:{board or '*'}"

    def _sync_provider(self, provider, board, limit) -> tuple[list[str], dict]:
        """Синк одной доски: enumerate → watermark → normalize → index → курсор.
        purge НЕ делает (он общий по всем доскам — см. run)."""
        warnings: list[str] = []
        ref = self._cursor_ref(provider.board_type, board)
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
        for raw in provider.iter_raw(board, limit):
            active_keys.append(raw.key)
            max_ts = max(max_ts, raw.timestamp)
            if raw.timestamp <= cursor:
                unchanged += 1
                continue
            try:
                changed.append(provider.normalize(raw))
            except Exception as e:
                log.warning("sync: сбой нормализации %s", raw.key, exc_info=True)
                warnings.append(f"normalize {raw.key}: {type(e).__name__}: {e}")

        results = self._tasks.index_batch(changed) if changed else []
        embedded = sum(1 for r in results if r.get("embedded"))
        refreshed = sum(1 for r in results
                        if not r.get("embedded") and not r.get("warnings"))
        failed = sum(1 for r in results if r.get("warnings"))
        for r in results:
            warnings.extend(r.get("warnings") or [])

        cursor_advanced = False
        if limit:
            warnings.append("курсор не продвинут: задан limit (частичный обход)")
        elif max_ts > cursor:
            try:
                self._meta.set_index_meta(_CURSOR_REPO, ref, str(max_ts))
                cursor_advanced = True
            except Exception:
                log.warning("sync: сбой записи курсора", exc_info=True)

        return active_keys, {
            "enumerated": len(active_keys), "changed": len(changed),
            "embedded": embedded, "refreshed": refreshed, "unchanged": unchanged,
            "failed": failed, "warnings": warnings, "cursor_advanced": cursor_advanced,
        }

    def run(self, board=None, limit=None, purge_orphaned=False,
            keep_with_prs=True) -> dict:
        agg = {"enumerated": 0, "changed": 0, "embedded": 0, "refreshed": 0,
               "unchanged": 0, "failed": 0, "warnings": [], "cursor_advanced": False}
        all_active: list[str] = []
        for provider in self._providers:
            active, one = self._sync_provider(provider, board, limit)
            all_active.extend(active)
            for k in ("enumerated", "changed", "embedded", "refreshed",
                      "unchanged", "failed"):
                agg[k] += one[k]
            agg["warnings"].extend(one["warnings"])
            agg["cursor_advanced"] = agg["cursor_advanced"] or one["cursor_advanced"]

        partial = bool(limit)
        purge_summary = None
        if purge_orphaned and partial:
            agg["warnings"].append("purge пропущен: задан limit (active_keys неполный)")
        elif purge_orphaned:
            # purge по ОБЪЕДИНЕНИЮ ключей всех досок: задачи глобальны, иначе
            # одна доска вычистила бы задачи другой.
            pr = self._tasks.purge_orphaned_tasks(all_active, keep_with_prs=keep_with_prs)
            purge_summary = {"deleted": pr["deleted_store"] + pr["deleted_graph"],
                             "protected": pr["protected_prs"]}
            agg["warnings"].extend(pr.get("warnings") or [])
        agg["purge"] = purge_summary
        return agg
```

Также обновить докстринг модуля `reviewer/tasks/sync.py` (строки 1-7): заменить `ref="tasks:<board>"` на `ref="tasks:<type>:<board>"` и добавить «обходит все провайдеры, purge — по объединению active_keys».

- [ ] **Step 4: Обновить wiring в app.py**

В `reviewer/app.py` строка 14: заменить
```python
from reviewer.tasks.boards import make_board_provider
```
на
```python
from reviewer.tasks.boards import make_board_providers
```

Строки 61-65 заменить на:
```python
    # server-side синк досок: все настроенные провайдеры (связка ключей в env).
    # Пустой список → sync_service=None, sync_board вернёт понятный error-summary.
    providers = make_board_providers(settings)
    sync_service = SyncService(providers, task_service, store) if providers else None
```

- [ ] **Step 5: Обновить integration-фейк (корректность)**

В `tests/tasks/test_sync_integration.py`: в класс `FakeProvider` добавить атрибут `board_type = "fake"` (после `def __init__`? — как класс-атрибут перед `__init__`); заменить `_REF = "tasks:ztest"` на `_REF = "tasks:fake:ztest"`; в `test_sync_idempotent_and_pr_edge` заменить `SyncService(FakeProvider(raws), ...)` на `SyncService([FakeProvider(raws)], ...)`.

- [ ] **Step 6: Запустить unit-тесты**

Run: `.venv/bin/pytest tests/tasks/test_sync.py tests/mcp/test_sync_board.py -q`
Expected: PASS (integration-тест test_sync_integration исключён маркером).

- [ ] **Step 7: Коммит**

```bash
.venv/bin/ruff check reviewer/tasks/sync.py reviewer/app.py tests/tasks/test_sync.py tests/tasks/test_sync_integration.py
git add reviewer/tasks/sync.py reviewer/app.py tests/tasks/test_sync.py tests/tasks/test_sync_integration.py
git commit -m "feat(tasks): мульти-провайдерный SyncService (курсор tasks:<type>:<board>)"
```

---

### Task 6: install.py wizard — per-type поля досок

**Files:**
- Modify: `reviewer/install.py:136-201`
- Test: `tests/test_install_wizard.py` (новый)

**Interfaces:**
- Consumes: `EnvGroup`, `EnvField`, `WIZARD_GROUPS` (из `reviewer/install.py`).
- Produces: в группе «Доска задач» появляются поля `YOUGILE_API_KEY`, `YOUTRACK_TOKEN`, `YOUTRACK_BASE_URL`.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_install_wizard.py`:

```python
from reviewer.install import WIZARD_GROUPS


def _keys():
    return {f.key for g in WIZARD_GROUPS for f in g.fields}


def test_wizard_has_per_type_board_creds():
    keys = _keys()
    assert "YOUGILE_API_KEY" in keys
    assert "YOUTRACK_TOKEN" in keys
    assert "YOUTRACK_BASE_URL" in keys


def test_wizard_keeps_board_selectors():
    keys = _keys()
    assert "TASK_BOARD_TYPE" in keys
    assert "TASK_BOARD_KEY_PATTERN" in keys
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/test_install_wizard.py -q`
Expected: FAIL (`YOUTRACK_TOKEN` нет в ключах).

- [ ] **Step 3: Заменить креды-поля в wizard**

В `reviewer/install.py` в группе «Доска задач» заменить два поля `TASK_BOARD_API_KEY` и `TASK_BOARD_API_BASE` (строки 160-170) на:

```python
            EnvField(
                key="YOUGILE_API_KEY",
                prompt_text="YOUGILE_API_KEY (REST-ключ yougile; Ctrl+~ → API)",
                default="",
                secret=True,
            ),
            EnvField(
                key="YOUTRACK_TOKEN",
                prompt_text="YOUTRACK_TOKEN (permanent token: Profile → Account "
                            "Security → New permanent token)",
                default="",
                secret=True,
            ),
            EnvField(
                key="YOUTRACK_BASE_URL",
                prompt_text="YOUTRACK_BASE_URL (напр. https://company.youtrack.cloud/api)",
                default="",
            ),
```

В `_GROUP_HEADERS["Доска задач"]` (строки 195-200) заменить текст на:

```python
    "Доска задач": (
        "# --- Доска задач (опционально; server-side sync_board, связка ключей) ---\n"
        "# Тип доски репо выбирается в его .review.yml (task_board.type); ключи —\n"
        "# здесь, под каждую доску свой. YOUGILE_API_KEY: конфигуратор yougile (Ctrl+~)\n"
        "# → API. YOUTRACK_TOKEN: permanent token, YOUTRACK_BASE_URL инстанс-специфичен.\n"
        "# TASK_BOARD_API_KEY/BASE — legacy-алиас для yougile (обратная совместимость)."
    ),
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `.venv/bin/pytest tests/test_install_wizard.py -q`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
.venv/bin/ruff check reviewer/install.py tests/test_install_wizard.py
git add reviewer/install.py tests/test_install_wizard.py
git commit -m "feat(install): per-type поля досок в wizard (.env)"
```

---

### Task 7: configure-review — настройка блока task_board

**Files:**
- Modify: `plugin/skills/configure-review/SKILL.md`
- Test: `tests/skills/test_configure_review_skill.py`

**Interfaces:**
- Только текст скилла + guard-тесты. Скилл остаётся standalone (git + правка файла).

- [ ] **Step 1: Обновить guard-тесты**

В `tests/skills/test_configure_review_skill.py` заменить `test_skill_preserves_foreign_keys` (строки 51-54) на:

```python
def test_skill_preserves_foreign_keys():
    text = SKILL.read_text(encoding="utf-8")
    assert "categories" in text                    # пример чужого ключа, который беречь
    assert "Never clobber" in text


def test_skill_manages_task_board_block():
    text = SKILL.read_text(encoding="utf-8")
    assert "task_board" in text
    assert "youtrack" in text                       # знает про новый тип доски
    # креды НЕ пишутся скиллом — только напоминание про env
    assert "YOUTRACK_TOKEN" in text or "env деплоя" in text
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/skills/test_configure_review_skill.py -q`
Expected: FAIL (`test_skill_manages_task_board_block` — нет `youtrack`).

- [ ] **Step 3: Обновить SKILL.md — frontmatter description**

В `plugin/skills/configure-review/SKILL.md` заменить строку `description:` (строка 3) на:

```
description: Configure or update a repo's .review.yml context layer (subsystem cluster depth, per-prefix depth overrides, summary top-k threshold, ignore for noisy *tracked* paths) and its task board selection (which board this repo uses — yougile/youtrack — key_pattern, url_template; never credentials) from a draft the skill generates and the user edits. Use when the user asks to set up or tune review config ("настроить .review.yml", "configure review config", "настрой контекст-слой", "tune cluster depth", "что игнорировать в ревью", "выбрать доску для репо", "set up reviewer for this repo"). Standalone — needs only git, no reviewer MCP / DB.
```

- [ ] **Step 4: Обновить SKILL.md — Scope**

Заменить блок Scope (строки 17-28) на:

```markdown
## Scope

Edit **only** these keys of `.review.yml`:
- `summary_cluster_depth` — global subsystem cluster depth.
- `summary_cluster_depth_overrides` — per-prefix depth (longest-prefix-match by directory segments).
- `summary_topk_threshold` — summary-prior scale threshold.
- `paths.ignore` — only for **tracked** noisy paths (eval, fixtures, generated, vendored, migrations, data).
- `task_board` — which board THIS repo uses (`type: yougile|youtrack`), plus `key_pattern` and (yougile only)
  `url_template`. **NEVER** write credentials here — board API keys live only in the reviewer deploy env
  (`YOUGILE_API_KEY` / `YOUTRACK_TOKEN` + `YOUTRACK_BASE_URL`). An empty `task_board:` disables the board for the repo.

Do NOT touch any other key (`categories`, `severity_threshold`, `max_comments`, `min_confidence`, …). Do NOT run a
reindex/resummarize. Do NOT walk the filesystem or try to detect untracked junk: `.venv`,
`node_modules`, `__pycache__`, `dist`, `build` are gitignored, so they never reach the git-tracked
index / graph / summaries — there is nothing to add to ignore for them.
```

- [ ] **Step 5: Обновить SKILL.md — добавить шаг про task_board**

После шага 5 (строки 65-80, генерация контекст-слоя) добавить новый шаг 5b (перед «6. Present draft + diff»):

```markdown
5b. **Task board selection (ask before writing).** Read the existing `task_board` block (keep it
   verbatim if present). Ask the user which board this repo uses:
   - `yougile` → write `{type: yougile, mcp: yougile, key_pattern: '[A-Z]+-\d+', url_template: <ask>}`.
   - `youtrack` → write `{type: youtrack, key_pattern: '[A-Z]+-\d+'}` (NO `url_template` — youtrack derives
     the link from its base URL; NO `mcp` — youtrack is read server-side via sync, not board-MCP).
   - off / none → write an empty `task_board:` (disables the board for this repo).
   - leave unchanged → skip.
   **Never write credentials.** Remind the user (in Russian): ключи доски (`YOUTRACK_TOKEN`/
   `YOUTRACK_BASE_URL` для youtrack, `YOUGILE_API_KEY` для yougile) задаются в env деплоя reviewer-mcp,
   не в `.review.yml`. Changing the board has no effect until those env keys are set and the board is
   synced (`/reviewer_sync-tasks`).
```

- [ ] **Step 6: Запустить — убедиться, что проходит**

Run: `.venv/bin/pytest tests/skills/test_configure_review_skill.py -q`
Expected: PASS (все тесты, включая `test_skill_scope_is_the_four_context_keys`).

- [ ] **Step 7: Коммит**

```bash
git add plugin/skills/configure-review/SKILL.md tests/skills/test_configure_review_skill.py
git commit -m "feat(skills): configure-review настраивает блок task_board"
```

---

### Task 8: review-pr — store-first чтение задачи

**Files:**
- Modify: `plugin/skills/review-pr/SKILL.md:45-53`
- Test: `tests/skills/test_review_pr_store_first.py` (новый)

**Interfaces:**
- Только текст скилла + guard-тест.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/skills/test_review_pr_store_first.py`:

```python
from pathlib import Path

SKILL = (Path(__file__).resolve().parents[2]
         / "plugin" / "skills" / "review-pr" / "SKILL.md")


def test_review_pr_reads_task_store_first():
    text = SKILL.read_text(encoding="utf-8")
    assert "get_task(" in text                       # store-first чтение из стора reviewer


def test_review_pr_board_mcp_is_fallback():
    text = SKILL.read_text(encoding="utf-8")
    # board-MCP плейбук — фолбэк только при промахе И заданном mcp
    assert "store-first" in text.lower() or "store first" in text.lower()
    assert "task_board.mcp" in text


def test_review_pr_youtrack_no_mcp_path():
    text = SKILL.read_text(encoding="utf-8")
    # пустой mcp (youtrack) → пропускаем requirements, не падаем
    assert "mcp" in text and "skip" in text.lower()
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/skills/test_review_pr_store_first.py -q`
Expected: FAIL (`get_task(` отсутствует в шаге 2 в текущем виде).

- [ ] **Step 3: Переписать шаг 2 review-pr**

В `plugin/skills/review-pr/SKILL.md` заменить блок шага 2 (строки 45-53) на:

```markdown
2. **Task context (optional).** Only if `task_board` is non-null. Resolve the task key: an
   explicit key in `$ARGUMENTS` wins; otherwise use `task_keys.primary`. If no key is available,
   skip this step and note in the summary that no task key was found.

   Read the task **store-first** (unifies with solve-task; required for boards synced server-side
   without a board MCP, e.g. youtrack):
   - Call reviewer `get_task(key)` first. **Hit** (object with a `key`) → use it as the `TaskBrief`
     directly; it is already indexed by the server-side sync, so do NOT call `index_task`.
   - **Miss** (`null`) AND `task_board.mcp` is set → fall back to the board-MCP playbook for
     `task_board.type` (`references/task-context-yougile.md` or `references/task-context-jira.md`):
     call the board MCP server named by `task_board.mcp`, build a `TaskBrief`, then `index_task(TaskBrief)`.
   - **Miss** AND `task_board.mcp` is empty (e.g. youtrack — no board MCP) → treat the task as not
     found: skip the requirements dimension and note the reason in the summary.
   In all cases, if the board MCP is not connected, a tool errors, or the task is not found: skip the
   requirements dimension and note the reason — NEVER abort the review.
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `.venv/bin/pytest tests/skills/test_review_pr_store_first.py -q`
Expected: PASS.

- [ ] **Step 5: Прогнать guard-набор скиллов (регрессия)**

Run: `.venv/bin/pytest tests/skills/ -q`
Expected: PASS (assembled-prompts / common-blocks не сломаны).

- [ ] **Step 6: Коммит**

```bash
git add plugin/skills/review-pr/SKILL.md tests/skills/test_review_pr_store_first.py
git commit -m "feat(skills): review-pr store-first чтение задачи (без board-MCP для youtrack)"
```

---

### Task 9: Финальная регрессия

**Files:** —

- [ ] **Step 1: Полный unit-прогон**

Run: `.venv/bin/pytest -q`
Expected: PASS (integration исключён по умолчанию).

- [ ] **Step 2: Линт затронутого**

Run: `.venv/bin/ruff check reviewer/ tests/`
Expected: без новых ошибок в затронутых файлах (репо-wide чистота не гарантируется — см. память про ruff на main; не гнаться за чужими предупреждениями).

- [ ] **Step 3: (опционально, если есть Postgres/Neo4j+Voyage) integration-синк**

Run: `.venv/bin/pytest tests/tasks/test_sync_integration.py -m integration -q`
Expected: PASS (курсор `tasks:fake:ztest`, идемпотентность).

---

## Self-Review (выполнено при написании плана)

**Spec coverage:** §Конфиг (форма A) → Task 1+6; §base.py контракт → Task 2; §YouTrackBoard/normalize_youtrack → Task 3; §фабрика множественная → Task 4; §SyncService мульти + курсор + union purge → Task 5; §install wizard → Task 6; §configure-review → Task 7; §review-pr store-first (плейбук не нужен) → Task 8; §тесты → во всех тасках; §back-compat (legacy fallback, курсор) → Task 1 (board_creds fallback) + Task 5 (курсор). YAGNI-список — ничего не реализуем сверх.

**Placeholder scan:** нет TBD/«handle edge cases»; весь код приведён.

**Type consistency:** `make_board_provider(settings, type_)` (Task 4) ↔ вызовы в Task 4 тестах и `make_board_providers`; `SyncService(providers,…)` (Task 5) ↔ `app.py` (Task 5) и тесты (Task 5); `provider.board_type` определён в Task 2 (Protocol + YougileBoard) и Task 3 (YouTrackBoard), используется в Task 5; `board_creds`/`configured_board_types` (Task 1) ↔ фабрика (Task 4); `RawTask.links` (Task 2) ↔ youtrack (Task 3) и yougile (не использует, дефолт).
