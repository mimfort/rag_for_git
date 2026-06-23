# PR #52 Follow-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дошлифовать три фолоу-апа PR #52: walkthrough читает summaries по целевой ветке PR; `member_node_ids` персистится (server-side re-derive); `get_summaries` отдаёт `updated_at`.

**Architecture:** Три независимые правки в трёх слоях (skill / service / store). Публичные сигнатуры MCP-тулов не меняются. #2 переиспользует чистые функции `cluster_key()`/`compute_source_hash()` из `reviewer/graph/summaries.py`; member-список пишется только при совпадении пере-вычисленного `source_hash` с переданным (иначе `[]` + note, fail-soft).

**Tech Stack:** Python 3.11, pytest, psycopg/pgvector (Postgres ParadeDB :5433), FastMCP.

## Global Constraints

- Язык проекта — **русский**: докстринги/комментарии/сообщения на русском.
- Коммиты — **Conventional Commits на русском, без self-attribution** (никаких `Co-Authored-By`/упоминаний Claude).
- Ruff: line-length 100, target py311.
- Ветка работы: `feat/graphrag-summaries-walkthrough` (дослать в открытый PR #52 → dev).
- **Не трогать:** `build_clusters`, `upsert_summary`, единичный `get_summary`, схему БД (`subsystem_summaries` уже имеет колонки `member_node_ids` и `updated_at`), публичные сигнатуры MCP-тулов.
- Integration-тесты требуют поднятый Postgres :5433 (`docker compose up -d`).

---

### Task 1: `get_summaries` отдаёт `updated_at` (store)

**Files:**
- Modify: `reviewer/index/summary_store.py` (метод `get_summaries`)
- Test: `tests/index/test_summary_store.py` (модифицировать существующий `test_upsert_then_get_roundtrip`)

**Interfaces:**
- Consumes: ничего нового.
- Produces: `SummaryStore.get_summaries(repo, branch) -> list[dict]`, где каждый dict теперь `{"cluster_key", "title", "summary", "updated_at"}` (`updated_at` — ISO-строка). `service.get_subsystem_summaries` пробрасывает результат as-is, правок не требует.

- [ ] **Step 1: Обновить существующий тест под новое поле (он сейчас падает на точном равенстве)**

В `tests/index/test_summary_store.py`, в `test_upsert_then_get_roundtrip`, заменить блок проверки `get_summaries`:

```python
    rows = store.get_summaries("t/t", "dev")
    assert len(rows) == 1
    row = rows[0]
    assert row["cluster_key"] == "reviewer/index"
    assert row["title"] == "Индекс"
    assert row["summary"] == "Хранилище чанков и ретрив."
    assert "T" in row["updated_at"]        # ISO-таймстамп (зеркало единичного get_summary)
```

(остальное тело теста — `upsert_summary`, `get_source_hashes`, `get_summary` — без изменений)

- [ ] **Step 2: Прогнать тест — убедиться, что падает**

Run: `.venv/bin/pytest -m integration tests/index/test_summary_store.py::test_upsert_then_get_roundtrip -v`
Expected: FAIL — `KeyError: 'updated_at'` (текущий `get_summaries` не отдаёт поле).

- [ ] **Step 3: Добавить `updated_at` в `get_summaries`**

В `reviewer/index/summary_store.py` заменить метод `get_summaries` на:

```python
    def get_summaries(self, repo: str, branch: str) -> list[dict]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT cluster_key, title, summary, updated_at FROM subsystem_summaries "
                    "WHERE repo=%s AND branch=%s ORDER BY cluster_key",
                    (repo, branch)).fetchall()
        except psycopg.errors.UndefinedTable:
            return []
        return [{"cluster_key": k, "title": t, "summary": s, "updated_at": u.isoformat()}
                for k, t, s, u in rows]
```

- [ ] **Step 4: Прогнать тест — убедиться, что проходит**

Run: `.venv/bin/pytest -m integration tests/index/test_summary_store.py -v`
Expected: PASS (все тесты файла, включая `test_upsert_is_idempotent_update` — он читает `["summary"]`, доп. поле его не ломает).

- [ ] **Step 5: Линт + коммит**

```bash
.venv/bin/ruff check reviewer/index/summary_store.py tests/index/test_summary_store.py
git add reviewer/index/summary_store.py tests/index/test_summary_store.py
git commit -m "feat(index): get_summaries отдаёт updated_at (PR #52 follow-up)"
```

---

### Task 2: `member_node_ids` через server-side re-derive (service)

**Files:**
- Modify: `reviewer/mcp/service.py` (метод `index_subsystem_summary`)
- Test: `tests/mcp/test_subsystem_summaries.py` (модифицировать `test_index_and_get_subsystem_summaries_roundtrip_via_store`; добавить тест mismatch)

**Interfaces:**
- Consumes: `ChunkStore.list_base_members(repo, branch) -> list[tuple[path, symbol_fqn, content_hash, start_line]]`; чистые `cluster_key(path, depth) -> str` и `compute_source_hash(list[tuple[node_id, content_hash]]) -> str` из `reviewer.graph.summaries`; `Settings.summary_cluster_depth: int` (=2); `SummaryStore.upsert_summary(repo, branch, cluster_key, title, summary, member_node_ids, source_hash)`.
- Produces: `index_subsystem_summary(repo, branch, cluster_key, title, summary, source_hash) -> dict` теперь возвращает `{"cluster_key", "stored": True, "members": <int>}` (+`"note"` при рассинхроне). **Сигнатура тула не меняется** — `member_node_ids` сервер выводит сам.

- [ ] **Step 1: Обновить существующий roundtrip-тест под server-side re-derive (happy-path)**

В `tests/mcp/test_subsystem_summaries.py` заменить `test_index_and_get_subsystem_summaries_roundtrip_via_store` на:

```python
def test_index_and_get_subsystem_summaries_roundtrip_via_store():
    from reviewer.graph.summaries import compute_source_hash
    c = MagicMock()
    # base-состав кластера reviewer/index (depth=2) — сервер выведет member_node_ids из него
    c.store.list_base_members.return_value = [
        ("reviewer/index/a.py", "A", "h1", 1),
        ("reviewer/index/b.py", "B", "h2", 2),
    ]
    sh = compute_source_hash([("reviewer/index/a.py#A", "h1"),
                              ("reviewer/index/b.py#B", "h2")])
    c.summary_store.get_summaries.return_value = [
        {"cluster_key": "reviewer/index", "title": "Индекс", "summary": "...",
         "updated_at": "2026-06-23T00:00:00+00:00"}]
    svc = _svc(c)

    out = svc.index_subsystem_summary("o/n", "dev", "reviewer/index", "Индекс", "...", sh)
    assert out == {"cluster_key": "reviewer/index", "stored": True, "members": 2}
    # upsert получил выведенный (отсортированный) member_node_ids, а не []
    args = c.summary_store.upsert_summary.call_args.args
    assert args[5] == ["reviewer/index/a.py#A", "reviewer/index/b.py#B"]

    got = svc.get_subsystem_summaries("o/n", "dev")
    assert got["summaries"][0]["cluster_key"] == "reviewer/index"
```

- [ ] **Step 2: Добавить тест рассинхрона (stale source_hash → [] + note)**

В тот же файл добавить:

```python
def test_index_subsystem_summary_stale_hash_empties_members():
    c = MagicMock()
    c.store.list_base_members.return_value = [("reviewer/index/a.py", "A", "h1", 1)]
    svc = _svc(c)
    # передан неактуальный source_hash → пере-вычисленный не совпадёт
    out = svc.index_subsystem_summary("o/n", "dev", "reviewer/index", "Индекс", "...", "STALE")
    assert out["stored"] is True
    assert out["members"] == 0
    assert "note" in out
    assert c.summary_store.upsert_summary.call_args.args[5] == []   # member_node_ids пуст
```

- [ ] **Step 3: Прогнать тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/mcp/test_subsystem_summaries.py -v`
Expected: FAIL — старая реализация зашивает `[]` и возвращает `{"cluster_key","stored":True}` без `members`; `list_base_members` не вызывается (happy-path ждёт `members: 2`).

- [ ] **Step 4: Реализовать server-side re-derive в `index_subsystem_summary`**

В `reviewer/mcp/service.py` заменить метод `index_subsystem_summary` на (импорт под алиасом — параметр `cluster_key` затеняет одноимённую функцию):

```python
    def index_subsystem_summary(self, repo: str, branch: str, cluster_key: str,
                                title: str, summary: str, source_hash: str) -> dict:
        """Персистнуть один summary подсистемы (idempotent upsert).

        member_node_ids выводятся сервером (re-derive по cluster_key над base-составом)
        и пишутся только при совпадении пере-вычисленного source_hash с переданным —
        иначе [] + note (состав базы изменился между list и index; самозалечивается
        следующим проходом summarize-subsystems)."""
        from reviewer.graph.summaries import cluster_key as cluster_key_of, compute_source_hash
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return {"stored": False, "note": rb}
        repo, resolved = rb
        depth = self.settings.summary_cluster_depth
        raw = self.components.store.list_base_members(repo, resolved)
        members = [(f"{p}#{s}", h) for p, s, h, _ in raw
                   if cluster_key_of(p, depth) == cluster_key]
        consistent = compute_source_hash(members) == source_hash
        member_node_ids = sorted(nid for nid, _ in members) if consistent else []
        self.components.summary_store.upsert_summary(
            repo, resolved, cluster_key, title, summary, member_node_ids, source_hash)
        out = {"cluster_key": cluster_key, "stored": True, "members": len(member_node_ids)}
        if not consistent:
            out["note"] = "состав кластера изменился с момента list — member_node_ids не сохранены"
        return out
```

- [ ] **Step 5: Прогнать тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/mcp/test_subsystem_summaries.py -v`
Expected: PASS (все 5 тестов файла: 3 про list_subsystem_clusters без изменений + 2 обновлённых/новых).

- [ ] **Step 6: Линт + коммит**

```bash
.venv/bin/ruff check reviewer/mcp/service.py tests/mcp/test_subsystem_summaries.py
git add reviewer/mcp/service.py tests/mcp/test_subsystem_summaries.py
git commit -m "feat(mcp): index_subsystem_summary выводит member_node_ids server-side (PR #52 follow-up)"
```

---

### Task 3: pr-walkthrough читает summaries по `pr.base_ref` (skill)

**Files:**
- Modify: `plugin/skills/pr-walkthrough/SKILL.md` (убрать include branch-selection; шаг 5)
- Test: `tests/skills/test_pr_walkthrough_skill.py` (добавить guard-тест)

**Interfaces:**
- Consumes: `prepare_review` уже возвращает `pr.base_ref` в payload (`_prepared_payload`).
- Produces: только промпт-скилл (markdown). Машинных сигнатур не задаёт.

- [ ] **Step 1: Добавить guard-тест на использование base_ref**

В `tests/skills/test_pr_walkthrough_skill.py` добавить:

```python
def test_step5_summaries_use_pr_base_ref():
    text = SKILL.read_text(encoding="utf-8")
    # summaries индексируются по целевой ветке PR, не по локальной git-ветке
    assert "base_ref" in text
    # include branch-selection убран — неверная абстракция для PR-скоупного скилла
    assert "branch-selection" not in text
```

- [ ] **Step 2: Прогнать тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/skills/test_pr_walkthrough_skill.py::test_step5_summaries_use_pr_base_ref -v`
Expected: FAIL — сейчас `base_ref` в тексте нет, а `branch-selection` есть.

- [ ] **Step 3: Убрать include branch-selection**

В `plugin/skills/pr-walkthrough/SKILL.md` удалить include-блок между шагом 1 и шагом 2. Заменить:

```
   and stop.

<!-- include: _common/branch-selection.md -->

2. **Reading order (centrality).**
```

на:

```
   and stop.

2. **Reading order (centrality).**
```

- [ ] **Step 4: Переписать шаг 5 на `pr.base_ref`**

В том же файле заменить шаг 5:

```
5. **Subsystem prior (optional).** `get_subsystem_summaries(repo, branch)` → name the touched
   subsystem(s) in one line. Empty / unavailable → skip (fail-open).
```

на:

```
5. **Subsystem prior (optional).** `get_subsystem_summaries(repo, pr.base_ref)` → name the touched
   subsystem(s) in one line. Pass the PR's target branch `pr.base_ref` (from the `prepare_review`
   response), NOT the local git branch — subsystem summaries are indexed per target branch
   (`base:<branch>`). Empty / unavailable → skip (fail-open).
```

- [ ] **Step 5: Прогнать guard-тесты скилла — убедиться, что проходят**

Run: `.venv/bin/pytest tests/skills/test_pr_walkthrough_skill.py -v`
Expected: PASS — новый тест зелёный; `test_skill_includes_resolve_to_existing_common_files` остаётся зелёным (остаются 2 include: `tool-usage.md`, `anti-hallucination.md`).

- [ ] **Step 6: Коммит**

```bash
git add plugin/skills/pr-walkthrough/SKILL.md tests/skills/test_pr_walkthrough_skill.py
git commit -m "fix(skills): pr-walkthrough читает summaries по pr.base_ref (PR #52 follow-up)"
```

---

### Task 4: Финальная верификация всей ветки

**Files:** нет (только прогон).

- [ ] **Step 1: Полный unit-прогон**

Run: `.venv/bin/pytest -q`
Expected: PASS (включая все guard-тесты `tests/skills/` — удаление include из одного скилла не должно ломать кросс-скилльные guard'ы).

- [ ] **Step 2: Integration-прогон затронутых SQL-путей**

Run: `.venv/bin/pytest -m integration tests/index/test_summary_store.py -v`
Expected: PASS (Postgres :5433 поднят).

- [ ] **Step 3: Линт по всему диффу**

Run: `.venv/bin/ruff check reviewer/ tests/ plugin/`
Expected: чисто по затронутым файлам (репо-wide грязь, не относящаяся к диффу, — не блокер).

---

## Self-Review

**Spec coverage:**
- #1 (walkthrough base_ref) → Task 3. ✓
- #2 (member_node_ids server re-derive + consistency guard) → Task 2. ✓
- #3 (get_summaries updated_at) → Task 1. ✓
- Прогон unit+integration → Task 4. ✓
- Границы (не трогать build_clusters/upsert_summary/get_summary/схему/сигнатуры тулов) → соблюдены во всех тасках. ✓

**Placeholder scan:** плейсхолдеров нет — весь код приведён дословно.

**Type consistency:**
- `list_base_members` → 4-tuple `(path, symbol_fqn, content_hash, start_line)`, распаковка `for p, s, h, _ in raw`. ✓
- `compute_source_hash(list[tuple[str, str]])` — вход `members` (node_id, content_hash); сортировка внутренняя, поэтому совпадает с `list_subsystem_clusters`. ✓
- `upsert_summary(...)` — `member_node_ids` 6-й позиционный аргумент (`call_args.args[5]`). ✓
- Алиас `cluster_key as cluster_key_of` снимает коллизию с параметром `cluster_key: str`. ✓
- Возврат `index_subsystem_summary` — `{"cluster_key","stored","members"(,"note")}`; тесты ассертят ровно это. ✓
