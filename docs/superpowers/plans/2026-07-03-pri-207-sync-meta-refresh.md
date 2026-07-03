# PRI-207 Self-healing meta-refresh синка — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** На каждом синке задач дёшево backfill-ить колонку `project` (и прочие плоские метаданные) у задач ниже watermark-курсора, чтобы scoped `search_tasks(project=…)` видел весь корпус, а не 3/97.

**Architecture:** Синк расщепляется на два тракта. Дорогой (changed-only) — как сейчас: `provider.normalize()` (REST-резолв подзадач/вложений) + embed. Новый дешёвый meta-refresh (all-enumerated) — для задач ниже курсора извлекает плоские поля из `RawTask` через **чистый** `normalize_meta()` (без I/O) и обновляет `tasks` (executemany) + графовый узел `:Task`. Watermark-курсор двигается по-прежнему только по `max_ts` — инкрементальность и экономия Voyage дорогого тракта сохранены.

**Tech Stack:** Python 3.11, psycopg3 (Postgres/ParadeDB на :5433), Neo4j, pytest. Провайдеры досок за `TaskBoardProvider` Protocol (yougile — референс, youtrack).

## Global Constraints

- Язык кода — **русский**: комментарии, докстринги, сообщения (сохранять стиль соседнего кода).
- Коммиты: Conventional Commits на русском (`feat(tasks): …`, `fix(tasks): …`); **без self-attribution** (никаких `Co-Authored-By`/упоминаний Claude).
- ruff: line-length 100, target py311. Прогонять `.venv/bin/ruff check .` — но не гнаться за repo-wide clean, только не вносить новых нарушений в трогаемых файлах.
- Внешние сервисы (Postgres/Neo4j) изолированы за интерфейсами и **мокаются в unit-тестах**; реальные вызовы — только в тестах с маркером `@pytest.mark.integration` (исключены из дефолтного `pytest -q`).
- Unit-цикл: `.venv/bin/pytest -q` (без DB). Integration-цикл: `.venv/bin/pytest -m integration` (нужны `docker compose up -d`).
- Инвариант дизайна: meta-refresh **никогда** не эмбедит и не роутится через `index_task`/`upsert_task` (иначе задача-не-в-сторе ушла бы в embed-путь). Только `update_meta_batch` + граф `upsert_task`.

---

### Task 1: `normalize_meta` — дешёвый (без I/O) TaskBrief из RawTask

Добавляет провайдерам метод, возвращающий плоские метаданные задачи (key, aliases, title, status, url, project) **без сетевых вызовов**, делегируя в уже существующие чистые module-level `normalize_yougile` / `normalize_youtrack`. Дорогой `normalize()` (резолв подзадач/вложений) не трогаем.

**Files:**
- Modify: `reviewer/tasks/boards/base.py` (Protocol `TaskBoardProvider` — добавить сигнатуру `normalize_meta`)
- Modify: `reviewer/tasks/boards/yougile.py` (класс `YougileBoard` — добавить `normalize_meta`)
- Modify: `reviewer/tasks/boards/youtrack.py` (класс `YouTrackBoard` — добавить `normalize_meta`)
- Test: `tests/tasks/boards/test_yougile_normalize.py`, `tests/tasks/boards/test_youtrack_normalize.py`

**Interfaces:**
- Consumes: чистые функции `normalize_yougile(raw, key_pattern, url_template)` и `normalize_youtrack(raw, key_pattern, base_url)` (обе задокументированы «Чистая: без I/O»); атрибуты провайдеров `self._key_pattern`, `self._url_template` (yougile), `self._base` (youtrack).
- Produces: `TaskBoardProvider.normalize_meta(self, raw: RawTask) -> dict` — возвращает полный чистый TaskBrief dict (ключи `key, aliases, title, description, criteria, status, url, links, project, attachments`), но БЕЗ I/O (`criteria=[]`, `attachments=[]`, у yougile — links без title подзадач). Потребители используют только плоские поля `key/aliases/title/status/url/project`.

- [ ] **Step 1: Написать падающие тесты (оба провайдера)**

В `tests/tasks/boards/test_yougile_normalize.py` добавить в конец файла:

```python
class _BoomClient:
    """httpx-заглушка: любой сетевой вызов = ошибка (доказывает отсутствие I/O)."""

    def get(self, *a, **k):
        raise AssertionError("normalize_meta не должен делать сетевые вызовы")

    def close(self):
        pass


def test_normalize_meta_no_io_yougile():
    b = YougileBoard(api_key="k", api_base="https://yougile.com/api-v2",
                     key_pattern=KP, url_template=URL)
    b._client = _BoomClient()
    # подзадачи и related-ссылки есть — дорогой normalize полез бы в сеть, meta — нет
    raw = _raw(subtask_ids=["u1"], description="связано с PRI-96")
    meta = b.normalize_meta(raw)
    assert meta["key"] == "ID-10"
    assert meta["aliases"] == ["PRI-10"]
    assert meta["status"] == "Backlog"
    assert meta["url"] == "https://ru.yougile.com/team/T/#PRI-10"
    assert meta["project"] == "PRI"
    assert meta["criteria"] == [] and meta["attachments"] == []
```

В `tests/tasks/boards/test_youtrack_normalize.py` добавить в конец файла:

```python
class _BoomClient:
    """httpx-заглушка: любой сетевой вызов = ошибка (доказывает отсутствие I/O)."""

    def get(self, *a, **k):
        raise AssertionError("normalize_meta не должен делать сетевые вызовы")

    def close(self):
        pass


def test_normalize_meta_no_io_youtrack():
    b = YouTrackBoard(token="perm:x", base_url=BASE, key_pattern=KP)
    b._client = _BoomClient()
    raw = _issue_to_raw(_issue(idReadable="PRJ-7", description="связано с ABC-1"))
    meta = b.normalize_meta(raw)
    assert meta["key"] == "PRJ-7"
    assert meta["status"] == "In Progress"
    assert meta["url"] == "https://c.youtrack.cloud/issue/PRJ-7"
    assert meta["project"] == "PRJ"
    assert meta["criteria"] == [] and meta["attachments"] == []
```

- [ ] **Step 2: Прогнать — убедиться, что падают**

Run: `.venv/bin/pytest tests/tasks/boards/test_yougile_normalize.py::test_normalize_meta_no_io_yougile tests/tasks/boards/test_youtrack_normalize.py::test_normalize_meta_no_io_youtrack -q`
Expected: FAIL — `AttributeError: 'YougileBoard' object has no attribute 'normalize_meta'` (и аналогично для YouTrack).

- [ ] **Step 3: Добавить сигнатуру в Protocol**

В `reviewer/tasks/boards/base.py`, в классе `TaskBoardProvider`, сразу после метода `normalize` (перед `finish`):

```python
    def normalize_meta(self, raw: RawTask) -> dict:
        """Дешёвый TaskBrief из RawTask БЕЗ I/O (PRI-207): только плоские
        метаданные (key, aliases, title, status, url, project). Подзадачи и
        вложения НЕ резолвятся (criteria=[], attachments=[]). Для self-healing
        meta-refresh задач ниже watermark — не дёргает сеть на задачу."""
        ...
```

- [ ] **Step 4: Реализовать в YougileBoard**

В `reviewer/tasks/boards/yougile.py`, в классе `YougileBoard`, сразу после метода `normalize` (перед `fetch_one`):

```python
    def normalize_meta(self, raw: RawTask) -> dict:
        """Дешёвая нормализация без I/O (PRI-207): чистый normalize_yougile без
        резолва подзадач/вложений. Плоские поля (project/url/status) корректны."""
        return normalize_yougile(raw, self._key_pattern, self._url_template)
```

- [ ] **Step 5: Реализовать в YouTrackBoard**

В `reviewer/tasks/boards/youtrack.py`, в классе `YouTrackBoard`, сразу после метода `normalize`:

```python
    def normalize_meta(self, raw: RawTask) -> dict:
        """Дешёвая нормализация без I/O (PRI-207): чистый normalize_youtrack без
        резолва вложений. Плоские поля (project/url/status) корректны."""
        return normalize_youtrack(raw, self._key_pattern, self._base)
```

- [ ] **Step 6: Прогнать — убедиться, что проходят**

Run: `.venv/bin/pytest tests/tasks/boards/test_yougile_normalize.py tests/tasks/boards/test_youtrack_normalize.py -q`
Expected: PASS (все тесты файлов, включая два новых).

- [ ] **Step 7: Линт + коммит**

Run: `.venv/bin/ruff check reviewer/tasks/boards/base.py reviewer/tasks/boards/yougile.py reviewer/tasks/boards/youtrack.py`
Expected: без новых нарушений.

```bash
git add reviewer/tasks/boards/base.py reviewer/tasks/boards/yougile.py \
        reviewer/tasks/boards/youtrack.py tests/tasks/boards/test_yougile_normalize.py \
        tests/tasks/boards/test_youtrack_normalize.py
git commit -m "feat(tasks): normalize_meta — дешёвый TaskBrief из RawTask без I/O (PRI-207)"
```

---

### Task 2: `TaskStore.update_meta_batch` — батч-обновление плоских метаданных

Один executemany-UPDATE для всех задач батча (масштабируется на большие доски). Задача не в сторе → 0 строк (безопасный no-op: не создаёт неполных строк, не трогает embedding). Единичный `update_meta` остаётся для `index_batch`.

**Files:**
- Modify: `reviewer/tasks/store.py` (класс `TaskStore` — добавить `update_meta_batch` после `update_meta`)
- Test: `tests/tasks/test_integration.py` (маркер `integration` — реальный Postgres)

**Interfaces:**
- Consumes: существующие `TaskStore._connect()`, таблица `tasks` (колонки `title, status, url, aliases, project, key`); `get_task(key)` для проверки.
- Produces: `TaskStore.update_meta_batch(self, metas: list[dict]) -> None` — каждый dict `{key, title?, status?, url?, aliases?, project?}`; UPDATE по `key`; отсутствующие ключи → `.get`-дефолты; пустой список / dict без `key` → no-op.

- [ ] **Step 1: Написать падающий интеграционный тест**

В `tests/tasks/test_integration.py` добавить (файл уже под `pytestmark = pytest.mark.integration`, фикстура `store` и `_FakeEmbedder` есть):

```python
def test_update_meta_batch_backfills_project(store):
    emb = _FakeEmbedder()
    text = build_task_text("T1", "d1", [])
    store.upsert_task(TaskRow(
        key="ID-1", aliases=["PRI-1"], title="T1", description="d1",
        status="Open", url=None, content_hash=task_content_hash(text),
        text=text, embedding=emb.embed_documents([text])[0], project=""))
    assert store.get_task("ID-1").project == ""

    store.update_meta_batch([{"key": "ID-1", "title": "T1", "status": "Done",
                              "url": "u", "aliases": ["PRI-1"], "project": "PRI"}])
    row = store.get_task("ID-1")
    assert row.project == "PRI"
    assert row.status == "Done"

    # задача не в сторе → no-op, ничего не создаётся
    store.update_meta_batch([{"key": "ID-404", "project": "PRI"}])
    assert store.get_task("ID-404") is None

    # пустой батч → без ошибок
    store.update_meta_batch([])
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest -m integration tests/tasks/test_integration.py::test_update_meta_batch_backfills_project -q`
Expected: FAIL — `AttributeError: 'TaskStore' object has no attribute 'update_meta_batch'`.
(Если Postgres не поднят — сначала `docker compose up -d`.)

- [ ] **Step 3: Реализовать `update_meta_batch`**

В `reviewer/tasks/store.py`, в классе `TaskStore`, сразу после метода `update_meta`:

```python
    def update_meta_batch(self, metas: list[dict]) -> None:
        """Батч-обновление плоских метаданных (PRI-207 meta-refresh): один
        executemany-UPDATE. Задача не в сторе → 0 строк (no-op, не создаёт
        неполных строк, не трогает embedding). Пустой батч → no-op."""
        rows = [(m.get("title") or "", m.get("status"), m.get("url"),
                 m.get("aliases") or [], m.get("project") or "", m["key"])
                for m in metas if isinstance(m, dict) and m.get("key")]
        if not rows:
            return
        with self._connect() as conn, conn.cursor() as cur:
            cur.executemany(
                "UPDATE tasks SET title=%s, status=%s, url=%s, aliases=%s, "
                "project=%s WHERE key=%s",
                rows,
            )
            conn.commit()
```

- [ ] **Step 4: Прогнать — убедиться, что проходит**

Run: `.venv/bin/pytest -m integration tests/tasks/test_integration.py::test_update_meta_batch_backfills_project -q`
Expected: PASS.

- [ ] **Step 5: Линт + коммит**

Run: `.venv/bin/ruff check reviewer/tasks/store.py`
Expected: без новых нарушений.

```bash
git add reviewer/tasks/store.py tests/tasks/test_integration.py
git commit -m "feat(tasks): TaskStore.update_meta_batch — батч-backfill метаданных (PRI-207)"
```

---

### Task 3: `TaskService.refresh_meta_batch` — оркестрация дешёвого meta-refresh

Обновляет плоские метаданные партии задач в сторе (`update_meta_batch`) и на графе (`upsert_task` per-task, fail-soft). Никогда не эмбедит, не upsert-ит полную строку и не линкует PR.

**Files:**
- Modify: `reviewer/tasks/service.py` (класс `TaskService` — добавить `refresh_meta_batch` после `index_batch`)
- Test: `tests/tasks/test_service_batch.py`

**Interfaces:**
- Consumes: `TaskStore.update_meta_batch(metas)` (Task 2); `self._graph.upsert_task(key, aliases, title, status, url, project)`; `self._graph is None` → граф недоступен; `log` (уже импортирован в модуле).
- Produces: `TaskService.refresh_meta_batch(self, metas: list[dict]) -> dict` → `{"meta_refreshed": int, "warnings": list[str]}`. Каждый meta-dict — как из `provider.normalize_meta` (нужен только `key`, остальное `.get`-дефолтится).

- [ ] **Step 1: Написать падающие тесты**

В `tests/tasks/test_service_batch.py` — сперва добавить в класс `_FakeStore` поддержку батча. В `_FakeStore.__init__` добавить строку `self.meta_batch = None`, и добавить метод:

```python
    def update_meta_batch(self, metas):
        self.meta_batch = list(metas)
```

Затем добавить тесты в конец файла:

```python
def test_refresh_meta_batch_stamps_project_store_and_graph():
    store, graph = _FakeStore(), _FakeGraph()
    metas = [{"key": "ID-1", "aliases": ["PRI-1"], "title": "T", "status": "Open",
              "url": "u", "project": "PRI"}]
    res = TaskService(store, graph, _FakeEmbedder()).refresh_meta_batch(metas)
    assert res["meta_refreshed"] == 1
    assert store.meta_batch == metas               # батч ушёл в стор
    assert graph.tasks == ["ID-1"]
    assert graph.task_projects == ["PRI"]          # project достиг графа


def test_refresh_meta_batch_never_embeds_or_upserts():
    store, graph, emb = _FakeStore(), _FakeGraph(), _FakeEmbedder()
    TaskService(store, graph, emb).refresh_meta_batch([{"key": "ID-1", "project": "PRI"}])
    assert emb.doc_calls == []                     # НИКОГДА не эмбедит
    assert store.upserted == []                    # и не upsert-ит (не воскрешает задачу)


def test_refresh_meta_batch_empty():
    store = _FakeStore()
    res = TaskService(store, _FakeGraph(), _FakeEmbedder()).refresh_meta_batch([])
    assert res == {"meta_refreshed": 0, "warnings": []}
    assert store.meta_batch is None


def test_refresh_meta_batch_graph_none_warns():
    store = _FakeStore()
    res = TaskService(store, None, _FakeEmbedder()).refresh_meta_batch(
        [{"key": "ID-1", "project": "PRI"}])
    assert res["meta_refreshed"] == 1
    assert any("graph unavailable" in w for w in res["warnings"])


def test_refresh_meta_batch_graph_failsoft():
    store, graph = _FakeStore(), _FakeGraph(raise_on=("upsert_task",))
    res = TaskService(store, graph, _FakeEmbedder()).refresh_meta_batch(
        [{"key": "ID-1", "project": "PRI"}])
    assert res["meta_refreshed"] == 1              # store прошёл, граф — fail-soft
    assert any("graph" in w for w in res["warnings"])
```

- [ ] **Step 2: Прогнать — убедиться, что падают**

Run: `.venv/bin/pytest tests/tasks/test_service_batch.py -q -k refresh_meta_batch`
Expected: FAIL — `AttributeError: 'TaskService' object has no attribute 'refresh_meta_batch'`.

- [ ] **Step 3: Реализовать `refresh_meta_batch`**

В `reviewer/tasks/service.py`, в классе `TaskService`, сразу после метода `index_batch` (перед `search_tasks`):

```python
    def refresh_meta_batch(self, metas: list[dict]) -> dict:
        """Дешёвый self-healing meta-refresh (PRI-207): backfill плоских
        метаданных (project/title/status/url/aliases) для задач ниже watermark.
        Стор — один батч update_meta_batch; граф — upsert_task per-task (fail-soft).
        НИКОГДА не эмбедит и не upsert-ит полную строку (не воскрешает задачу,
        отсутствующую в сторе) и не линкует PR. metas — как из normalize_meta."""
        metas = [m for m in metas if isinstance(m, dict) and m.get("key")]
        if not metas:
            return {"meta_refreshed": 0, "warnings": []}
        warnings: list[str] = []
        try:
            self._store.update_meta_batch(metas)
        except Exception as e:
            log.warning("refresh_meta_batch: сбой update_meta_batch", exc_info=True)
            warnings.append(f"store: {type(e).__name__}: {e}")
        if self._graph is None:
            warnings.append("graph unavailable: task projects not refreshed in graph")
        else:
            for m in metas:
                try:
                    self._graph.upsert_task(
                        m["key"], m.get("aliases") or [], m.get("title") or "",
                        m.get("status"), m.get("url"), m.get("project") or "")
                except Exception as e:
                    log.warning("refresh_meta_batch: сбой графа для %s",
                                m["key"], exc_info=True)
                    warnings.append(f"graph {m['key']}: {type(e).__name__}: {e}")
        return {"meta_refreshed": len(metas), "warnings": warnings}
```

- [ ] **Step 4: Прогнать — убедиться, что проходят**

Run: `.venv/bin/pytest tests/tasks/test_service_batch.py -q`
Expected: PASS (новые refresh_meta_batch-тесты + существующие index_batch-тесты не сломаны).

- [ ] **Step 5: Линт + коммит**

Run: `.venv/bin/ruff check reviewer/tasks/service.py`
Expected: без новых нарушений.

```bash
git add reviewer/tasks/service.py tests/tasks/test_service_batch.py
git commit -m "feat(tasks): TaskService.refresh_meta_batch — оркестрация meta-refresh (PRI-207)"
```

---

### Task 4: Встроить meta-refresh в SyncService

Для задач ниже курсора (`raw.timestamp <= cursor`) собирать `provider.normalize_meta(raw)` и после цикла вызывать `refresh_meta_batch`. Курсор двигать по-прежнему только по `max_ts`. В summary добавить `meta_refreshed` (per-provider + агрегат + by_board). MCP-тул `sync_board` прокидывает summary as-is — правок в MCP-слое не нужно.

**Files:**
- Modify: `reviewer/tasks/sync.py` (методы `SyncService._sync_provider` и `SyncService.run`)
- Test: `tests/tasks/test_sync.py`

**Interfaces:**
- Consumes: `provider.normalize_meta(raw) -> dict` (Task 1); `TaskService.refresh_meta_batch(metas) -> {"meta_refreshed", "warnings"}` (Task 3).
- Produces: per-provider summary и агрегат `run(...)` получают ключ `"meta_refreshed": int`; каждый элемент `by_board` — тоже `"meta_refreshed"`.

- [ ] **Step 1: Обновить фейки и написать/поправить тесты**

В `tests/tasks/test_sync.py`:

(a) В класс `FakeProvider` добавить метод (рядом с `normalize`):

```python
    def normalize_meta(self, raw):
        return {"key": raw.key, "aliases": [raw.project_code], "title": raw.title,
                "status": raw.status, "url": None,
                "project": raw.project_code.split("-")[0]}
```

(b) В класс `FakeTaskService`: в `__init__` добавить `self.meta_refreshed = []`, и добавить метод:

```python
    def refresh_meta_batch(self, metas):
        self.meta_refreshed.append([m["key"] for m in metas])
        return {"meta_refreshed": len(metas), "warnings": []}
```

(c) Обновить существующий `test_first_sync_indexes_all_and_advances_cursor` — добавить в конец:

```python
    assert summary["meta_refreshed"] == 0        # все changed, unchanged нет
    assert ts.meta_refreshed == []
```

(d) Обновить существующий `test_watermark_skips_unchanged` — добавить в конец:

```python
    assert summary["meta_refreshed"] == 1        # ID-1 (ниже курсора) meta-refreshнут
    assert ts.meta_refreshed == [["ID-1"]]
```

(e) Обновить существующий `test_no_changes_does_not_advance_cursor` — добавить в конец:

```python
    assert summary["meta_refreshed"] == 2        # обе задачи ниже курсора
    assert ts.meta_refreshed == [["ID-1", "ID-2"]]
    assert summary["cursor_advanced"] is False   # meta-refresh курсор НЕ двигает
```

(f) Добавить новый тест (meta_refreshed в by_board):

```python
def test_by_board_includes_meta_refreshed():
    prov = FakeProvider([_raw("ID-1", 100), _raw("ID-2", 200)], board_type="yougile")
    meta = FakeMeta({("", "tasks:yougile:*"): "150"})   # ID-1 ниже курсора
    ts = FakeTaskService()
    result = SyncService([prov], ts, meta).run()
    assert result["meta_refreshed"] == 1
    assert result["by_board"][0]["meta_refreshed"] == 1
```

- [ ] **Step 2: Прогнать — убедиться, что падают**

Run: `.venv/bin/pytest tests/tasks/test_sync.py -q`
Expected: FAIL — `KeyError: 'meta_refreshed'` (summary ещё не содержит ключа) в обновлённых/новом тестах.

- [ ] **Step 3: Правка `_sync_provider`**

В `reviewer/tasks/sync.py`, метод `_sync_provider`. Рядом с инициализацией `changed: list[dict] = []` добавить:

```python
        meta_refresh: list[dict] = []
```

Заменить блок watermark-skip:

```python
            if raw.timestamp <= cursor:
                unchanged += 1
                continue
```

на:

```python
            if raw.timestamp <= cursor:
                unchanged += 1
                try:
                    meta_refresh.append(provider.normalize_meta(raw))
                except Exception as e:
                    log.warning("sync: сбой normalize_meta %s", raw.key, exc_info=True)
                    warnings.append(f"normalize_meta {raw.key}: {type(e).__name__}: {e}")
                continue
```

После блока подсчёта `embedded/refreshed/failed` (после `for r in results: warnings.extend(r.get("warnings") or [])`), перед `cursor_advanced = False`, добавить:

```python
        meta_refreshed = 0
        if meta_refresh:
            mr = self._tasks.refresh_meta_batch(meta_refresh)
            meta_refreshed = mr.get("meta_refreshed", 0)
            warnings.extend(mr.get("warnings") or [])
```

В возвращаемом словаре `_sync_provider` (`return active_keys, {...}`) добавить ключ (например, после `"unchanged": unchanged,`):

```python
            "meta_refreshed": meta_refreshed,
```

- [ ] **Step 4: Правка `run` (агрегация + by_board)**

В `reviewer/tasks/sync.py`, метод `run`. В инициализации `agg = {...}` добавить `"meta_refreshed": 0,` (в ту же dict-литералу, рядом с `"unchanged": 0,`).

В цикле агрегации расширить кортеж ключей — было:

```python
            for k in ("enumerated", "changed", "embedded", "refreshed",
                      "unchanged", "failed"):
                agg[k] += one[k]
```

стало:

```python
            for k in ("enumerated", "changed", "embedded", "refreshed",
                      "unchanged", "failed", "meta_refreshed"):
                agg[k] += one[k]
```

В сборке `by_board.append({...})` расширить кортеж ключей — было:

```python
                **{k: one[k] for k in ("enumerated", "changed", "embedded",
                                        "refreshed", "unchanged", "failed")},
```

стало:

```python
                **{k: one[k] for k in ("enumerated", "changed", "embedded",
                                        "refreshed", "unchanged", "failed",
                                        "meta_refreshed")},
```

- [ ] **Step 5: Прогнать — убедиться, что проходят**

Run: `.venv/bin/pytest tests/tasks/test_sync.py -q`
Expected: PASS (все тесты файла, включая обновлённые и новый).

- [ ] **Step 6: Регресс всего пакета задач + линт**

Run: `.venv/bin/pytest tests/tasks -q`
Expected: PASS (unit; integration исключены дефолтным маркером).

Run: `.venv/bin/ruff check reviewer/tasks/sync.py`
Expected: без новых нарушений.

- [ ] **Step 7: Коммит**

```bash
git add reviewer/tasks/sync.py tests/tasks/test_sync.py
git commit -m "feat(tasks): self-healing meta-refresh задач ниже watermark при синке (PRI-207)"
```

---

## Финальная проверка (после всех задач)

- [ ] **Полный unit-прогон:** `.venv/bin/pytest -q` — ожидается PASS (integration исключены).
- [ ] **Integration-прогон (нужен `docker compose up -d`):** `.venv/bin/pytest -m integration tests/tasks/test_integration.py -q` — ожидается PASS.
- [ ] **Живая приёмка (после деплоя сервера с этим кодом, вне текущей сессии):** один обычный `sync_board(board="PRI", board_type="yougile")` (changed=0) → в summary `meta_refreshed ≈ 96`; затем `search_tasks(project="PRI")` возвращает десятки PRI-задач (не 3). Это acceptance-критерий задачи. Деплой обязателен — снимает вопрос «пишет ли задеплоенный сервер project вообще».

## Замечания по rollout

- Правок в MCP-слое (`reviewer/entrypoints/mcp_server.py`, `reviewer/mcp/service.py`) не требуется: `sync_board` возвращает summary из `SyncService.run` as-is → `meta_refreshed` появится автоматически.
- Осознанный gap: задача, отсутствующая в сторе (напр. ранее purged, но всё ещё на доске ниже курсора), meta-refresh НЕ воскрешает (`update_meta_batch` → 0 строк). Re-add — это дорогой тракт при изменении задачи. Вне скоупа PRI-207.
- Дорогие поля (criteria/attachments/description/embedding) для старых задач не backfill-ятся — восстановятся через дорогой тракт при следующем изменении задачи. Вне скоупа.
