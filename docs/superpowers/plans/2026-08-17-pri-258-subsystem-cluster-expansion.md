# PRI-258 — Разворот кластеров subsystems в файлы-кандидаты: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Подмешивать в секцию `code` контекста задачи файлы релевантных кластеров сводок
подсистем — четвёртым источником кандидатов рядом с гибридом, graph-expansion и similar-diffs.

**Architecture:** Источник разворота сам зовёт `SummaryStore.search_summaries` с малым `top_n`
(не доверяя бэк-компат-режиму `get_subsystem_summaries`), берёт `member_node_ids` отобранных
кластеров, режет `path#fqn` до пути и отдаёт пути как именованный `AugmentSource` со своей
файловой квотой. Механика бюджета целиком переиспользуется из PRI-257: фактический резерв
слотов, известность по итоговой гибридной выдаче, квота по реально найденным в сторе файлам.

**Tech Stack:** Python 3.11+, psycopg 3, pgvector, pytest. Без новых зависимостей.

**Spec:** `docs/superpowers/specs/2026-08-17-pri-258-subsystem-cluster-expansion-design.md`

## Global Constraints

- Язык проекта — русский: комментарии, докстринги, сообщения. Conventional Commits на русском,
  **без self-attribution** (никаких `Co-Authored-By`/упоминаний Claude).
- Unit-тесты идут без Postgres, Neo4j, localhost-сокетов и внешней сети. Всё, что требует
  реальной БД, помечается `@pytest.mark.integration`.
- Прогон unit: `.venv/bin/pytest -q`. Линт: `.venv/bin/ruff check <файлы>`.
- `SUBSYSTEM_TOPN = 3` — модульная константа в `reviewer/retrieval/augment.py`, НЕ ключ политики.
- `CodeSectionLimits.max_subsystem_files` — дефолт `2`, ключ `.review.yml`
  `context_limits.code_section.max_subsystem_files`, значение `0` полностью выключает рычаг.
- Ни один источник подмешивания не бросает наружу: сбой пишет причину в `augment_gaps` →
  `gaps` секции `code.augment`.
- Ветка работы: `feat/pri-258-subsystem-cluster-expansion` (уже создана от `dev`).

---

### Task 1: Обобщить подмешивание на список именованных источников

Единственный источник (`augment_paths`) заменяется списком `AugmentSource`. Поведение при одном
источнике обязано остаться побайтово прежним — это чистый рефакторинг под второй источник.

**Files:**
- Modify: `reviewer/retrieval/augment.py` (добавить `AugmentSource`)
- Modify: `reviewer/retrieval/multiquery.py:130-187` (`_augment_items`), `:213-289` (`search_multi`)
- Modify: `reviewer/mcp/service.py:1797-1825` (`_search_codebase_multi`), `:3611-3614` (`code`)
- Modify: `eval/solve_task_metrics/live.py:126-163` (`code_multi`)
- Test: `tests/retrieval/test_multiquery.py`

**Interfaces:**
- Produces: `AugmentSource(name: str, paths: list[str], quota: int)` в
  `reviewer.retrieval.augment`; `search_multi(..., augment_sources: list[AugmentSource] | None)`;
  `_augment_items(retriever, repo, *, sources, bref, known_paths, include_tests=False)
  -> tuple[list, str | None]`; `MCPReviewService._search_codebase_multi(repo, queries, branch,
  include_tests, augment_sources=None)`.
- Consumes: ничего (первая задача).

- [ ] **Step 1: Написать падающий тест на два источника с раздельными квотами**

В `tests/retrieval/test_multiquery.py` добавить импорт `AugmentSource` в существующий блок
импортов (`from reviewer.retrieval.augment import AugmentSource`) и тест:

```python
def test_two_sources_get_separate_quotas_and_named_note():
    store = _FakeStore(
        {"q0": [_bm25("a.py#f")]},
        nodes_by_path={
            "x.py": _hit("x.py#s"), "y.py": _hit("y.py#s"),
            "s1.py": _hit("s1.py#s"), "s2.py": _hit("s2.py#s"),
        },
    )
    pack = search_multi(
        _Retriever(store, _FakeEmbedder()), "o/n", ["q0"], limits=CodebaseLimits(),
        section_limits=CodeSectionLimits(), branch="dev",
        augment_sources=[
            AugmentSource(name="similar-diffs", paths=["x.py", "y.py"], quota=1),
            AugmentSource(name="subsystems", paths=["s1.py", "s2.py"], quota=1),
        ])
    paths = [it.path for it in pack.items]
    assert paths == ["a.py", "x.py", "s1.py"], "по одному файлу из каждого источника, гибрид первым"
    assert pack.augment_note == (
        "— подмешано 2 файлов: similar-diffs 1 (квота 1), subsystems 1 (квота 1)")


def test_second_source_does_not_repeat_path_taken_by_first():
    store = _FakeStore(
        {"q0": [_bm25("a.py#f")]},
        nodes_by_path={"x.py": _hit("x.py#s"), "y.py": _hit("y.py#s")},
    )
    pack = search_multi(
        _Retriever(store, _FakeEmbedder()), "o/n", ["q0"], limits=CodebaseLimits(),
        section_limits=CodeSectionLimits(), branch="dev",
        augment_sources=[
            AugmentSource(name="similar-diffs", paths=["x.py"], quota=2),
            AugmentSource(name="subsystems", paths=["x.py", "y.py"], quota=2),
        ])
    paths = [it.path for it in pack.items]
    assert paths.count("x.py") == 1, "путь первого источника второму не достаётся"
    assert "y.py" in paths
    assert pack.augment_note == (
        "— подмешано 2 файлов: similar-diffs 1 (квота 2), subsystems 1 (квота 2)")
```

- [ ] **Step 2: Запустить тесты и убедиться, что падают**

Run: `.venv/bin/pytest -q tests/retrieval/test_multiquery.py -k "two_sources or second_source"`
Expected: FAIL — `TypeError: search_multi() got an unexpected keyword argument 'augment_sources'`
и `ImportError` на `AugmentSource`.

- [ ] **Step 3: Добавить `AugmentSource` в `reviewer/retrieval/augment.py`**

Сразу после импортов, перед `AugmentResult`:

```python
@dataclass(frozen=True)
class AugmentSource:
    """Именованный источник подмешанных путей со своей файловой квотой.

    Имя не косметика: нота видимости секции перечисляет источники поимённо —
    иначе вклад источников в выдачу неотличим на глаз (и в отчёте replay).
    """

    name: str
    paths: list[str] = field(default_factory=list)
    quota: int = 0
```

- [ ] **Step 4: Переписать `_augment_items` на цикл по источникам**

В `reviewer/retrieval/multiquery.py` заменить `_augment_items` целиком (сигнатура и тело):

```python
def _augment_items(retriever, repo: str, *, sources, bref: str, known_paths: set,
                   include_tests: bool = False) -> tuple[list, str | None]:
    """Подмешанные кандидаты по именованным источникам. Fail-soft.

    Известность НАКОПИТЕЛЬНАЯ: путь, занятый источником выше по списку,
    следующему уже не достаётся — иначе два источника, знающие один и тот же
    файл, потратили бы на него две квоты и два файловых слота.

    Квота считает ФАЙЛЫ, реально подмешанные в выдачу, а не рассмотренных
    кандидатов: списки путей часто открываются непроиндексированными файлами
    (docs/*, *.jsonl, README, CLAUDE.md — чанки есть только у .py), и урезание
    списка кандидатов до quota ДО похода в стор выжигало квоту на путях,
    которые физически не могли попасть в выдачу (PRI-257, третий фикс).

    Тестовые пути отсеиваются здесь же, ДО подсчёта квоты: git-фолбэк
    similar-diffs и состав кластера отдают файлы без core/test-фильтрации, а
    постфактум-фильтр съедал бы уже выбранный тестовый слот квоты.
    """
    seen = set(known_paths)
    items: list = []
    notes: list[str] = []
    for source in sources or []:
        if source.quota <= 0:
            continue
        candidates: dict[str, None] = {}
        for path in source.paths or []:
            if len(candidates) >= AUGMENT_FETCH_LIMIT:
                break
            if path in seen:
                continue
            if not include_tests and _is_test_path(path):
                continue
            candidates.setdefault(path, None)
        if not candidates:
            continue
        try:
            fetched = {item.path: item for item in retriever.store.fetch_retrieved_at_paths(
                repo, list(candidates), base_ref=bref)}
        except Exception:  # noqa: BLE001 — стор недоступен, это штатный случай
            log.warning("multiquery: выборка подмешанных путей источника %s недоступна",
                        source.name, exc_info=True)
            continue
        taken = 0
        for path in candidates:
            if taken >= source.quota:
                break
            if path in fetched:
                items.append(fetched[path])
                seen.add(path)
                taken += 1
        if taken:
            notes.append(f"{source.name} {taken} (квота {source.quota})")
    if not items:
        return [], None
    return items, f"— подмешано {len(items)} файлов: " + ", ".join(notes)
```

- [ ] **Step 5: Перевести `search_multi` на `augment_sources`**

В сигнатуре `search_multi` заменить `augment_paths=None` на `augment_sources=None`, а блок
подмешивания (строки 276-287) — на:

```python
    augmented, note = _augment_items(
        retriever, repo, sources=augment_sources, bref=bref,
        known_paths={item.path for item in hybrid_final},
        include_tests=include_tests)
```

Остальное (расчёт `reserved`, срез `hybrid_final`, сборка `items`) не меняется. В докстринге
`search_multi` заменить абзац про `augment_paths` на:

```
    Источники подмешивания (PRI-257, PRI-258) приходят списком AugmentSource:
    у каждого своё имя и своя файловая квота — РЕЗЕРВ слотов внутри max_files,
    а не потолок на остаток. Резерв фактический: без найденных кандидатов
    гибрид получает бюджет целиком. Известность считается по ИТОГОВОЙ гибридной
    выдаче hybrid_final, а не по сырому пулу: кандидат, которого гибрид нашёл,
    но ранжировал слишком низко для попадания в max_files, — это и есть тот
    файл, который рычаг обязан промоутить.
```

- [ ] **Step 6: Обновить трёх вызывающих**

`reviewer/mcp/service.py:1797-1825` — в сигнатуре `_search_codebase_multi` заменить
`augment_paths: list[str] | None = None` на `augment_sources: list | None = None`, в вызове
`search_multi` — `augment_paths=augment_paths` на `augment_sources=augment_sources`, в докстринге
заменить абзац про `augment_paths` на «`augment_sources` (PRI-257/258) — именованные источники
кандидатов со своими квотами; см. `search_multi`».

`reviewer/mcp/service.py:3611-3614` — метод `code` `_TaskContextDeps`:

```python
    def code(self, repo: str, branch: str, queries: list) -> str:
        from reviewer.retrieval.augment import AugmentSource
        sources = [AugmentSource(name="similar-diffs",
                                 paths=self._augment_paths(repo),
                                 quota=self._service._resolve_context_limits(
                                     repo, branch).code_section.max_augmented_files)]
        return self._service._search_codebase_multi(
            repo, queries, branch, False, augment_sources=sources)
```

`eval/solve_task_metrics/live.py:126-163` — в `code_multi` эффективные лимиты вычисляются
ДО сборки источников (иначе оверрайд `--set` не доехал бы до квоты и сторона «до» замера совпала
бы со стороной «после» — тот же дефект, что чинил PRI-256 для `section_limits`):

```python
        base = limits_to_yaml(self._service._resolve_context_limits(repo, branch))
        effective = ContextLimits.from_review_yaml(
            {"context_limits": _merge(base, limits or {})}
        )
        sources = []
        if similar_paths:
            from reviewer.mcp.service import _TaskContextDeps
            from reviewer.retrieval.augment import AugmentSource
            deps = _TaskContextDeps(self._service, None)
            # Хиты похожих задач наполняются вызовом similar; первый подзапрос —
            # это и есть продакшн-запрос задачи целиком (см. _queries).
            deps.similar(queries[0], None)
            sources.append(AugmentSource(name="similar-diffs", paths=deps._augment_paths(repo),
                                         quota=effective.code_section.max_augmented_files))
```

Ниже по методу переиспользовать уже посчитанный `effective` вместо повторного расчёта в ветке
`if not limits:` / `else`, а оба вызова заменить: `augment_paths=augment` →
`augment_sources=sources or None`.

- [ ] **Step 7: Запустить тесты — новые проходят, старые PRI-257 обновлены**

Run: `.venv/bin/pytest -q tests/retrieval/ tests/mcp/ tests/eval/`
Expected: новые PASS. Старые тесты PRI-257, зовущие `augment_paths=[...]`, упадут —
переписать их на `augment_sources=[AugmentSource(name="similar-diffs", paths=[...],
quota=<прежний max_augmented_files>)]`, оставив утверждения прежними; ноту в утверждениях
привести к новому формату (`"— подмешано 2 файлов: similar-diffs 2 (квота 2)"`).

- [ ] **Step 8: Прогнать весь unit-набор и линт**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check reviewer/retrieval reviewer/mcp eval`
Expected: PASS, линт чист по изменённым файлам.

- [ ] **Step 9: Коммит**

```bash
git add reviewer/retrieval/augment.py reviewer/retrieval/multiquery.py \
        reviewer/mcp/service.py eval/solve_task_metrics/live.py tests/retrieval/test_multiquery.py
git commit -m "refactor(retrieval): подмешивание списком именованных источников с квотами"
```

---

### Task 2: Ключ политики `max_subsystem_files`

**Files:**
- Modify: `reviewer/policy/context_limits.py:30-57` (`CodeSectionLimits`), `:93-101`
  (`from_review_yaml`)
- Test: `tests/policy/test_context_limits.py`

**Interfaces:**
- Consumes: ничего.
- Produces: `CodeSectionLimits.max_subsystem_files: int = 2`; ключ `.review.yml`
  `context_limits.code_section.max_subsystem_files`.

- [ ] **Step 1: Написать падающий тест**

```python
def test_max_subsystem_files_default_and_override():
    assert CodeSectionLimits().max_subsystem_files == 2
    limits = ContextLimits.from_review_yaml(
        {"context_limits": {"code_section": {"max_subsystem_files": 0}}})
    assert limits.code_section.max_subsystem_files == 0
    assert limits.code_section.max_augmented_files == 3, "резервы источников независимы"
```

- [ ] **Step 2: Запустить и убедиться, что падает**

Run: `.venv/bin/pytest -q tests/policy/test_context_limits.py -k max_subsystem_files`
Expected: FAIL — `AttributeError: 'CodeSectionLimits' object has no attribute 'max_subsystem_files'`.

- [ ] **Step 3: Добавить поле и его чтение**

В `CodeSectionLimits` после `max_augmented_files`:

```python
    max_subsystem_files: int = 2  # резерв под разворот кластеров subsystems (PRI-258)
```

В докстринг `CodeSectionLimits` добавить абзац:

```
    Резервы источников подмешивания независимы (max_augmented_files,
    max_subsystem_files): вклад каждого чисто измерим, и рычаг снимается
    одним значением ключа — общий делённый резерв смешал бы их в замере.
    Суммарный резерв верхнего предохранителя не имеет: политика доверяет
    оператору, симметрично search_codebase.ceiling.
```

В `from_review_yaml`, в блок `code_section=CodeSectionLimits(...)`:

```python
                max_subsystem_files=int(
                    cs.get("max_subsystem_files", CodeSectionLimits.max_subsystem_files)),
```

**Внимание:** `max_chars` не меняется — он производный от `max_files`, а не от резервов.

- [ ] **Step 4: Запустить тест**

Run: `.venv/bin/pytest -q tests/policy/test_context_limits.py`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add reviewer/policy/context_limits.py tests/policy/test_context_limits.py
git commit -m "feat(policy): ключ max_subsystem_files — резерв под разворот кластеров"
```

---

### Task 3: `member_node_ids` в списочных ридерах стора сводок

**Files:**
- Modify: `reviewer/index/summary_store.py:422-433` (`get_summaries`), `:450-464`
  (`search_summaries`)
- Modify: `reviewer/mcp/service.py:2782-2812` (`get_subsystem_summaries`)
- Test: `tests/mcp/test_subsystem_summaries.py`

**Interfaces:**
- Consumes: ничего.
- Produces: `SummaryStore.get_summaries` и `SummaryStore.search_summaries` возвращают ключ
  `member_node_ids: list[str]` (как `get_summary`); `MCPReviewService.get_subsystem_summaries`
  это поле в СПИСОЧНЫХ путях вырезает, а в пути по `cluster_key` отдаёт как прежде.

- [ ] **Step 1: Написать падающий тест**

В `tests/mcp/test_subsystem_summaries.py`:

```python
def test_list_paths_hide_member_node_ids_but_cluster_key_path_keeps_them():
    c = MagicMock()
    c.store.list_base_members.return_value = []
    c.summary_store.count_summaries.return_value = 0
    c.summary_store.get_summaries.return_value = [
        {"cluster_key": "reviewer/index", "title": "Индекс", "summary": "...",
         "member_node_ids": ["reviewer/index/a.py#A"], "source_hash": "h",
         "updated_at": "2026-06-23T00:00:00+00:00"}]
    c.summary_store.get_summary.return_value = {
        "cluster_key": "reviewer/index", "title": "Индекс", "summary": "...",
        "member_node_ids": ["reviewer/index/a.py#A"], "source_hash": "h",
        "updated_at": "2026-06-23T00:00:00+00:00"}
    svc = _svc(c)

    listed = svc.get_subsystem_summaries("o/n", "dev")
    assert "member_node_ids" not in listed["summaries"][0], \
        "состав кластеров не льётся в LLM-выдачу секции subsystems"

    single = svc.get_subsystem_summaries("o/n", "dev", "reviewer/index")
    assert single["summary"]["member_node_ids"] == ["reviewer/index/a.py#A"], \
        "путь по cluster_key поведение не меняет (обратная совместимость)"
```

- [ ] **Step 2: Запустить и убедиться, что падает**

Run: `.venv/bin/pytest -q tests/mcp/test_subsystem_summaries.py -k member_node_ids`
Expected: FAIL — `member_node_ids` присутствует в списочной выдаче (сейчас он туда просто не
попадает из стора, но после шага 3 попадёт; тест фиксирует ОБЕ половины контракта, и до правки
`get_subsystem_summaries` падает вторая половина — `KeyError: 'member_node_ids'` в single, если
мок не соответствует; убедись, что падает именно утверждение, а не сборка мока).

- [ ] **Step 3: Расширить SELECT обоих списочных ридеров**

`get_summaries`:

```python
    def get_summaries(self, repo: str, branch: str) -> list[dict]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT cluster_key, title, summary, member_node_ids, source_hash, updated_at "
                    "FROM subsystem_summaries "
                    "WHERE repo=%s AND branch=%s ORDER BY cluster_key",
                    (repo, branch)).fetchall()
        except psycopg.errors.UndefinedTable:
            return []
        return [{"cluster_key": k, "title": t, "summary": s,
                 "member_node_ids": list(m or []), "source_hash": h,
                 "updated_at": u.isoformat()}
                for k, t, s, m, h, u in rows]
```

`search_summaries` — аналогично: добавить `member_node_ids` в список колонок между `summary` и
`source_hash`, распаковку сделать `for k, t, s, m, h, u in rows` и вернуть ключ
`"member_node_ids": list(m or [])`. В докстринг `search_summaries` дописать: «Состав кластера
(`member_node_ids`) отдаётся вместе со сводкой — его читает разворот кластеров в файлы-кандидаты
(PRI-258); в LLM-выдачу секции `subsystems` он не попадает, там его вырезает
`get_subsystem_summaries`.»

- [ ] **Step 4: Вырезать поле в списочных путях `get_subsystem_summaries`**

В `reviewer/mcp/service.py` добавить приватный хелпер рядом с `_annotate_summary_staleness`:

```python
    @staticmethod
    def _without_members(summaries: list[dict]) -> list[dict]:
        """Убрать состав кластера из LLM-выдачи: там он чистый расход токенов.

        Читатель состава — разворот кластеров в файлы-кандидаты (PRI-258), он
        ходит в стор напрямую. Путь по cluster_key поле отдаёт как прежде.
        """
        return [{k: v for k, v in summary.items() if k != "member_node_ids"}
                for summary in summaries]
```

и обернуть им обе списочные ветки: в query-ветке —
`"summaries": self._without_members([{**s, "stale": None} for s in summaries])`, в финальной —
`"summaries": self._without_members(self._annotate_summary_staleness(repo, resolved, summaries))`.
Ветку `if cluster_key:` не трогать.

- [ ] **Step 5: Запустить тесты**

Run: `.venv/bin/pytest -q tests/mcp/test_subsystem_summaries.py tests/index/`
Expected: PASS.

- [ ] **Step 6: Коммит**

```bash
git add reviewer/index/summary_store.py reviewer/mcp/service.py \
        tests/mcp/test_subsystem_summaries.py
git commit -m "feat(index): состав кластера в списочных ридерах сводок, вырезан из LLM-выдачи"
```

---

### Task 4: Источник путей — разворот релевантных кластеров

**Files:**
- Modify: `reviewer/retrieval/augment.py`
- Test: `tests/retrieval/test_augment.py`

**Interfaces:**
- Consumes: `AugmentResult`, `AugmentSource` (Task 1); ключ `member_node_ids` в
  `search_summaries` (Task 3).
- Produces: `SUBSYSTEM_TOPN = 3`; `collect_subsystem_paths(*, summary_store, embedder, repo,
  branch, query, limit) -> AugmentResult` (`by_source` ключ — `"subsystems"`).

- [ ] **Step 1: Написать падающие тесты**

```python
class _FakeSummaryStore:
    def __init__(self, rows=None, fail=False):
        self._rows = rows or []
        self._fail = fail
        self.calls: list = []

    def search_summaries(self, repo, branch, query_embedding, top_k):
        self.calls.append((repo, branch, top_k))
        if self._fail:
            raise RuntimeError("Postgres недоступен")
        return list(self._rows)


class _FakeEmbedder:
    def __init__(self, fail=False):
        self._fail = fail

    def embed_query(self, text):
        if self._fail:
            raise RuntimeError("нет квоты")
        return [0.1] * 8


def test_subsystem_paths_are_cluster_members_without_symbol_part():
    store = _FakeSummaryStore([
        {"cluster_key": "reviewer/retrieval",
         "member_node_ids": ["reviewer/retrieval/a.py#A", "reviewer/retrieval/a.py#B",
                             "reviewer/retrieval/b.py#C"]},
    ])
    result = collect_subsystem_paths(
        summary_store=store, embedder=_FakeEmbedder(), repo="o/n", branch="dev",
        query="q", limit=10)
    assert result.paths == ["reviewer/retrieval/a.py", "reviewer/retrieval/b.py"], \
        "path#fqn срезан до пути, дубли схлопнуты с сохранением порядка"
    assert result.by_source["subsystems"] == 2
    assert store.calls == [("o/n", "dev", 3)], "свой ANN top-N, а не выдача секции subsystems"


def test_subsystem_paths_are_empty_when_summaries_are_cold():
    result = collect_subsystem_paths(
        summary_store=_FakeSummaryStore([]), embedder=_FakeEmbedder(), repo="o/n",
        branch="dev", query="q", limit=10)
    assert result.paths == []
    assert any("сводки" in gap for gap in result.gaps)


def test_subsystem_paths_survive_store_failure_with_gap():
    result = collect_subsystem_paths(
        summary_store=_FakeSummaryStore(fail=True), embedder=_FakeEmbedder(), repo="o/n",
        branch="dev", query="q", limit=10)
    assert result.paths == []
    assert any("сводки подсистем недоступны" in gap for gap in result.gaps)


def test_subsystem_paths_survive_embedder_failure_with_gap():
    result = collect_subsystem_paths(
        summary_store=_FakeSummaryStore([{"cluster_key": "x", "member_node_ids": ["x/a.py#A"]}]),
        embedder=_FakeEmbedder(fail=True), repo="o/n", branch="dev", query="q", limit=10)
    assert result.paths == []
    assert any("эмбеддинг" in gap for gap in result.gaps)


def test_subsystem_paths_respect_limit():
    store = _FakeSummaryStore([
        {"cluster_key": "c", "member_node_ids": [f"c/f{i}.py#S" for i in range(30)]},
    ])
    result = collect_subsystem_paths(
        summary_store=store, embedder=_FakeEmbedder(), repo="o/n", branch="dev",
        query="q", limit=5)
    assert len(result.paths) == 5
```

Импорт в шапке файла дополнить: `from reviewer.retrieval.augment import (AugmentResult,
collect_similar_task_paths, collect_subsystem_paths)`.

- [ ] **Step 2: Запустить и убедиться, что падают**

Run: `.venv/bin/pytest -q tests/retrieval/test_augment.py -k subsystem`
Expected: FAIL — `ImportError: cannot import name 'collect_subsystem_paths'`.

- [ ] **Step 3: Реализовать источник**

В конец `reviewer/retrieval/augment.py`:

```python
SUBSYSTEM_TOPN = 3
"""Сколько кластеров сводок разворачивать в файлы.

Модульная константа, а не ключ политики: файловый бюджет уже регулируется
CodeSectionLimits.max_subsystem_files, и второй регулятор той же величины мог
бы с ним рассинхронизироваться. Отбор кластеров намеренно шире резерва —
отсечка происходит на файловом бюджете, где приоритет задан рангом гибрида.
"""


def collect_subsystem_paths(*, summary_store, embedder, repo: str, branch: str,
                            query: str, limit: int) -> AugmentResult:
    """Файлы релевантных кластеров сводок как пути-кандидаты (PRI-258).

    Релевантность считается СВОИМ ANN top-N, а не выдачей секции subsystems:
    get_subsystem_summaries ранжирует по близости только при числе сводок выше
    summary_topk_threshold, а ниже порога отдаёт все сводки по алфавиту — их
    разворот развернул бы весь репозиторий.

    Состав берётся из member_node_ids сводки (он посчитан при её построении и
    потому учитывает summary_cluster_depth, per-prefix overrides и
    summary_paths.ignore), а не из префикса пути в base-индексе — тот дал бы
    «файлы каталога» вместо «состава подсистемы».
    """
    if limit <= 0 or summary_store is None:
        return AugmentResult()
    gaps: list[str] = []
    try:
        qvec = embedder.embed_query(query)
    except Exception as exc:  # noqa: BLE001 — квота Voyage кончилась, штатный случай
        return AugmentResult(gaps=[f"эмбеддинг запроса недоступен: {type(exc).__name__}"])
    try:
        summaries = summary_store.search_summaries(repo, branch, qvec, SUBSYSTEM_TOPN)
    except Exception as exc:  # noqa: BLE001 — стор недоступен, штатный случай
        return AugmentResult(gaps=[f"сводки подсистем недоступны: {type(exc).__name__}"])
    ordered: dict[str, None] = {}
    for summary in summaries or []:
        for node_id in summary.get("member_node_ids") or []:
            ordered.setdefault(str(node_id).split("#", 1)[0], None)
    if not ordered:
        gaps.append("сводки подсистем не построены — разворот кластеров пропущен")
    paths = list(ordered)[:limit]
    return AugmentResult(paths=paths,
                         by_source={"subsystems": len(paths)} if paths else {},
                         gaps=gaps)
```

- [ ] **Step 4: Запустить тесты**

Run: `.venv/bin/pytest -q tests/retrieval/test_augment.py`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add reviewer/retrieval/augment.py tests/retrieval/test_augment.py
git commit -m "feat(retrieval): разворот релевантных кластеров сводок в пути-кандидаты"
```

---

### Task 5: Проводка источника в сборку контекста задачи

**Files:**
- Modify: `reviewer/mcp/service.py:3510-3614` (`_TaskContextDeps`)
- Test: `tests/mcp/test_prepare_task_context.py`, `tests/mcp/test_service.py`

**Interfaces:**
- Consumes: `collect_subsystem_paths`, `SUBSYSTEM_TOPN` (Task 4); `AugmentSource` (Task 1);
  `max_subsystem_files` (Task 2).
- Produces: `_TaskContextDeps._subsystem_paths(repo, branch, query) -> list[str]`; метод `code`
  отдаёт оба источника; причины сбоя копятся в `self.augment_gaps` → `gaps` секции
  `code.augment`.

- [ ] **Step 1: Написать падающий тест на холодные сводки (критерий приёмки 2)**

В `tests/mcp/test_prepare_task_context.py`:

```python
def test_cold_summaries_do_not_break_brief_assembly():
    """Критерий приёмки 2 PRI-258: сводки не построены → gap, а не сбой сборки."""
    class _ColdSummaryStore:
        def search_summaries(self, repo, branch, query_embedding, top_k):
            return []

    from reviewer.retrieval.augment import collect_subsystem_paths
    result = collect_subsystem_paths(
        summary_store=_ColdSummaryStore(), embedder=_StubEmbedder(), repo="o/n",
        branch="dev", query="q", limit=2)
    assert result.paths == []
    assert result.gaps, "пробел записан"
```

где `_StubEmbedder` — локальный класс с `embed_query = lambda self, text: [0.1] * 8`.
Дополнительно — тест проводки в `tests/mcp/test_service.py`:

```python
def test_task_context_code_passes_two_named_augment_sources():
    svc = _svc_with_fakes()  # существующий хелпер файла
    deps = _TaskContextDeps(svc, None)
    captured = {}

    def _fake_multi(repo, queries, branch, include_tests, augment_sources=None):
        captured["sources"] = augment_sources
        return "ok"

    svc._search_codebase_multi = _fake_multi
    deps.subsystems("o/n", "dev", "q")
    deps.code("o/n", "dev", ["q"])
    assert [s.name for s in captured["sources"]] == ["similar-diffs", "subsystems"], \
        "similar-diffs первым: он измеренно точнее"
    assert captured["sources"][1].quota == 2
```

- [ ] **Step 2: Запустить и убедиться, что падают**

Run: `.venv/bin/pytest -q tests/mcp/test_prepare_task_context.py tests/mcp/test_service.py -k "cold_summaries or two_named"`
Expected: FAIL — `AssertionError` на именах источников (`code` отдаёт только similar-diffs).

- [ ] **Step 3: Добавить `_subsystem_paths` и расширить `code`**

В `reviewer/mcp/service.py` рядом с `AUGMENT_LOOKUP_LIMIT`:

```python
SUBSYSTEM_LOOKUP_LIMIT = 40
"""Сколько путей разворот отдаёт ДО квоты: квота режет позже, в search_multi (PRI-258)."""
```

В `_TaskContextDeps` после `_augment_paths`:

```python
    def _subsystem_paths(self, repo: str, branch: str, query: str) -> list[str]:
        """Файлы релевантных кластеров сводок. Пробелы копятся в augment_gaps.

        Обёрнуто целиком по образцу _augment_paths: сбой сигнала подмешивания
        не должен обнулять всю секцию code через общий _safe в task_context.py.
        """
        from reviewer.retrieval.augment import collect_subsystem_paths
        try:
            result = collect_subsystem_paths(
                summary_store=getattr(self._service.components, "summary_store", None),
                embedder=self._service.components.embedder,
                repo=repo, branch=branch, query=query, limit=SUBSYSTEM_LOOKUP_LIMIT)
            for reason in result.gaps:
                log.warning("_TaskContextDeps._subsystem_paths: %s", reason)
            self.augment_gaps.extend(result.gaps)
            return result.paths
        except Exception:  # noqa: BLE001 — источник разворота недоступен целиком
            log.warning("_TaskContextDeps._subsystem_paths: сбой разворота кластеров",
                        exc_info=True)
            self.augment_gaps.append("разворот кластеров подсистем недоступен")
            return []
```

и переписать `code` (заменив версию из Task 1):

```python
    def code(self, repo: str, branch: str, queries: list) -> str:
        from reviewer.retrieval.augment import AugmentSource
        section = self._service._resolve_context_limits(repo, branch).code_section
        query = queries[0] if queries else ""
        sources = [
            AugmentSource(name="similar-diffs", paths=self._augment_paths(repo),
                          quota=section.max_augmented_files),
            AugmentSource(name="subsystems",
                          paths=self._subsystem_paths(repo, branch, query),
                          quota=section.max_subsystem_files),
        ]
        return self._service._search_codebase_multi(
            repo, queries, branch, False, augment_sources=sources)
```

Порядок источников фиксирован: similar-diffs измеренно точнее (28/35 попаданий в ядро), поэтому
известность накапливает он первым.

- [ ] **Step 4: Запустить тесты**

Run: `.venv/bin/pytest -q tests/mcp/`
Expected: PASS.

- [ ] **Step 5: Прогнать весь unit-набор и линт**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check reviewer`
Expected: PASS.

- [ ] **Step 6: Коммит**

```bash
git add reviewer/mcp/service.py tests/mcp/test_prepare_task_context.py tests/mcp/test_service.py
git commit -m "feat(mcp): разворот кластеров subsystems вторым источником секции code"
```

---

### Task 6: Приоритет внутри кластера — RRF-ранг сырого пула

**Files:**
- Modify: `reviewer/retrieval/multiquery.py` (`_augment_items`, `search_multi`)
- Test: `tests/retrieval/test_multiquery.py`

**Interfaces:**
- Consumes: `_augment_items` (Task 1).
- Produces: `_augment_items(..., rank_by_path: dict[str, int] | None = None)` — карта
  «путь → лучший ранг в сыром пуле», по ней сортируются кандидаты источника.

- [ ] **Step 1: Написать падающий тест**

```python
def test_augmented_candidates_ordered_by_raw_pool_rank():
    """Файл кластера, найденный гибридом (пусть и низко), идёт раньше ненайденного."""
    hits = [_bm25(f"f{i}.py#s") for i in range(12)] + [_bm25("late.py#s")]
    store = _FakeStore(
        {"q0": hits},
        nodes_by_path={"late.py": _hit("late.py#s"), "never.py": _hit("never.py#s")},
    )
    pack = search_multi(
        _Retriever(store, _FakeEmbedder()), "o/n", ["q0"], limits=CodebaseLimits(),
        section_limits=CodeSectionLimits(max_augmented_files=0, max_subsystem_files=1),
        branch="dev",
        augment_sources=[AugmentSource(name="subsystems",
                                       paths=["never.py", "late.py"], quota=1)])
    paths = [it.path for it in pack.items]
    assert "late.py" in paths, "низко ранжированный гибридом файл приоритетнее ненайденного"
    assert "never.py" not in paths
```

- [ ] **Step 2: Запустить и убедиться, что падает**

Run: `.venv/bin/pytest -q tests/retrieval/test_multiquery.py -k raw_pool_rank`
Expected: FAIL — квоту занимает `never.py` (входной порядок источника).

- [ ] **Step 3: Реализовать сортировку по рангу сырого пула**

В `_augment_items` добавить параметр `rank_by_path: dict | None = None` и заменить сборку
`candidates` на сортировку до среза:

```python
        ranked = sorted(
            ((rank_by_path or {}).get(path, len(rank_by_path or {}) + 1), index, path)
            for index, path in enumerate(dict.fromkeys(source.paths or [])))
        candidates: dict[str, None] = {}
        for _rank, _index, path in ranked:
            if len(candidates) >= AUGMENT_FETCH_LIMIT:
                break
            if path in seen:
                continue
            if not include_tests and _is_test_path(path):
                continue
            candidates.setdefault(path, None)
```

Ключ сортировки — пара (ранг, исходная позиция): при равных рангах (оба пути гибрид не нашёл)
порядок источника сохраняется, поэтому выдача детерминирована.

В докстринг `_augment_items` дописать:

```
    Порядок кандидатов задаётся рангом в СЫРОМ пуле гибрида: путь, который
    гибрид нашёл, но не поднял до max_files, приоритетнее пути, которого он не
    нашёл вовсе. Сырой пул здесь определяет ПОРЯДОК, но не известность:
    известность считается по итоговой выдаче (иначе рычаг терял бы ровно те
    файлы, которые обязан промоутить, — PRI-257, второй фикс).
```

В `search_multi` посчитать карту сразу после `items = [*merged, *_graph_items(...)]`:

```python
    rank_by_path: dict[str, int] = {}
    for rank, item in enumerate(items):
        rank_by_path.setdefault(item.path, rank)
```

и передать её в вызов `_augment_items` (`rank_by_path=rank_by_path`).

- [ ] **Step 4: Запустить тесты**

Run: `.venv/bin/pytest -q tests/retrieval/test_multiquery.py`
Expected: PASS (включая все тесты PRI-257 — при одном источнике сортировка стабильна).

- [ ] **Step 5: Коммит**

```bash
git add reviewer/retrieval/multiquery.py tests/retrieval/test_multiquery.py
git commit -m "feat(retrieval): приоритет подмешанных кандидатов по рангу сырого пула"
```

---

### Task 7: Вариант замера в replay-харнессе

**Files:**
- Modify: `eval/solve_task_metrics/variants.py:70-90`
- Modify: `eval/solve_task_metrics/live.py` (`code_multi`)
- Test: `tests/eval/test_variants.py`, `tests/eval/test_live_boundary.py`

**Interfaces:**
- Consumes: `AugmentSource`, `collect_subsystem_paths`, `max_subsystem_files`.
- Produces: варианты `subsystem_paths` и `similar_paths+subsystem_paths` в `_REGISTRY`;
  `LiveRetrieval.code_multi(..., similar_paths: bool = False, subsystem_paths: bool = False)`.

- [ ] **Step 1: Написать падающие тесты**

В `tests/eval/test_variants.py`:

```python
def test_subsystem_paths_variant_requests_only_subsystem_source():
    provider = FakeProvider(HEADER)
    task, target = _inputs()
    assert variants.get_variant("subsystem_paths")(provider, task, target) == {"reviewer/a.py"}
    assert provider.multi_calls[-1]["subsystem_paths"] is True
    assert provider.multi_calls[-1]["similar_paths"] is False


def test_combined_variant_requests_both_sources():
    provider = FakeProvider(HEADER)
    task, target = _inputs()
    variants.get_variant("similar_paths+subsystem_paths")(provider, task, target)
    assert provider.multi_calls[-1]["similar_paths"] is True
    assert provider.multi_calls[-1]["subsystem_paths"] is True
```

`FakeProvider` в этом файле дополнить методом, если его ещё нет:

```python
    def code_multi(self, repo, branch, queries, limits, *,
                   similar_paths=False, subsystem_paths=False):
        self.multi_calls.append({"queries": list(queries), "limits": limits,
                                 "similar_paths": similar_paths,
                                 "subsystem_paths": subsystem_paths})
        return self.text
```

и `self.multi_calls: list = []` в `__init__`.

- [ ] **Step 2: Запустить и убедиться, что падают**

Run: `.venv/bin/pytest -q tests/eval/test_variants.py -k subsystem`
Expected: FAIL — `UnknownVariant: неизвестный вариант 'subsystem_paths'`.

- [ ] **Step 3: Добавить варианты в реестр**

В `eval/solve_task_metrics/variants.py` после `_similar_paths`:

```python
def _subsystem_paths(provider, task: TaskInput, target: ReplayTarget) -> set:
    """Мультизапрос плюс разворот релевантных кластеров сводок (PRI-258).

    Изолированный вариант: similar-diffs выключен, поэтому дельта относится к
    развороту, а не к сумме двух рычагов (критерий приёмки 1).
    """
    queries = build_subqueries(task.task, task.query)
    text = provider.code_multi(target.repo, target.branch, queries, target.limits,
                               subsystem_paths=True)
    return extract_context_paths(text)


def _similar_and_subsystem_paths(provider, task: TaskInput, target: ReplayTarget) -> set:
    """Оба источника подмешивания — продакшн-конфигурация после мержа PRI-258."""
    queries = build_subqueries(task.task, task.query)
    text = provider.code_multi(target.repo, target.branch, queries, target.limits,
                               similar_paths=True, subsystem_paths=True)
    return extract_context_paths(text)
```

и в `_REGISTRY`:

```python
    "subsystem_paths": _subsystem_paths,
    "similar_paths+subsystem_paths": _similar_and_subsystem_paths,
```

- [ ] **Step 4: Провести флаг через `LiveRetrieval.code_multi`**

Сигнатура: `def code_multi(self, repo, branch, queries, limits, *, similar_paths=False,
subsystem_paths=False) -> str:`. В сборку `sources` (из Task 1) добавить:

```python
        if subsystem_paths:
            from reviewer.mcp.service import SUBSYSTEM_LOOKUP_LIMIT
            from reviewer.retrieval.augment import AugmentSource, collect_subsystem_paths
            result = collect_subsystem_paths(
                summary_store=getattr(self._components, "summary_store", None),
                embedder=self._components.embedder,
                repo=repo, branch=branch, query=queries[0] if queries else "",
                limit=SUBSYSTEM_LOOKUP_LIMIT)
            sources.append(AugmentSource(name="subsystems", paths=result.paths,
                                         quota=effective.code_section.max_subsystem_files))
```

Продакшн-функция сбора и продакшн-предел выборки берутся напрямую: своей копии разворота и
своего числа в эвале не заводится, иначе replay мерил бы не тот вход, что видит прод.

- [ ] **Step 5: Дополнить граничный тест эвала**

В `tests/eval/test_live_boundary.py` существующие фейки объявляют
`_search_codebase_multi(self, repo, queries, branch, include_tests, *, augment_paths=None)` —
после Task 1 их сигнатуры уже переписаны на `augment_sources=None`. Добавить:

```python
def test_code_multi_subsystem_flag_passes_named_source(monkeypatch):
    """subsystem_paths=True собирает источник продакшн-функцией collect_subsystem_paths."""
    from reviewer.policy.context_limits import CodeSectionLimits
    from reviewer.retrieval import augment as augment_mod

    from eval.solve_task_metrics.live import LiveRetrieval

    monkeypatch.setattr(
        augment_mod, "collect_subsystem_paths",
        lambda **kwargs: augment_mod.AugmentResult(paths=["reviewer/x.py"]))

    class FakeService:
        def _search_codebase_multi(self, repo, queries, branch, include_tests,
                                   *, augment_sources=None):
            self.captured = augment_sources
            return "текст"

    class FakeComponents:
        summary_store = object()
        embedder = object()

    service = FakeService()
    provider = LiveRetrieval(object(), FakeComponents(), service)
    provider.code_multi("o/n", "dev", ["q1"], None, subsystem_paths=True)

    assert [s.name for s in service.captured] == ["subsystems"]
    assert service.captured[0].paths == ["reviewer/x.py"]
    assert service.captured[0].quota == CodeSectionLimits().max_subsystem_files
```

Тест на оверрайд квоты (`--set code_section.max_subsystem_files=1` доезжает до источника, а не
теряется) — по образцу существующего `test_code_multi_with_overrides_forwards_augment_signals`.

Сверить порядок позиционных аргументов конструктора `LiveRetrieval` с существующими тестами
файла (там второй аргумент — components) перед запуском.

- [ ] **Step 6: Запустить тесты и линт**

Run: `.venv/bin/pytest -q tests/eval/ && .venv/bin/ruff check eval`
Expected: PASS.

- [ ] **Step 7: Коммит**

```bash
git add eval/solve_task_metrics/variants.py eval/solve_task_metrics/live.py \
        tests/eval/test_variants.py tests/eval/test_live_boundary.py
git commit -m "feat(eval): варианты replay для разворота кластеров subsystems"
```

---

### Task 8: Замер и вердикт по критерию приёмки 1

Это gate-задача: её результат решает, мержится рычаг или снимается.

**Files:**
- Modify: `eval/replay_report.md` (раздел «Приёмка PRI-258»)
- Modify: `eval/replay_history.jsonl` (добавляется прогоном харнесса)

**Interfaces:**
- Consumes: варианты `similar_paths`, `subsystem_paths`, `similar_paths+subsystem_paths`
  (Task 7).
- Produces: вердикт «мержим / снимаем» и раздел отчёта.

- [ ] **Step 1: Убедиться, что инфраструктура поднята и индекс свеж**

Run: `docker compose up -d && .venv/bin/reviewer status --json`
Expected: `drift == 0` для рабочей ветки; число сводок > 0 (иначе разворот нечему разворачивать —
сначала `/rag-reviewer:summarize-subsystems`).

- [ ] **Step 2: Прогнать три варианта на одном `indexed_sha`**

Run (модель прогона — Sonnet/Haiku, не Opus):

```bash
.venv/bin/python -m eval.solve_task_metrics replay --variant similar_paths
.venv/bin/python -m eval.solve_task_metrics replay --variant subsystem_paths
.venv/bin/python -m eval.solve_task_metrics replay --variant similar_paths+subsystem_paths
```

Точные флаги CLI сверить с `eval/solve_task_metrics/__main__.py` перед запуском.
Expected: три записи в `eval/replay_history.jsonl` с одинаковым `indexed_sha`.

- [ ] **Step 3: Записать раздел «Приёмка PRI-258» в `eval/replay_report.md`**

Раздел обязан содержать: медиану и bulk core-recall по каждому варианту, precision, число
подмешанных разворотом путей и попаданий в ядро, число задач с падением recall, и явный вердикт.
Формат — по образцу раздела «Приёмка PRI-257».

- [ ] **Step 4: Вердикт**

Дельта bulk core-recall варианта `similar_paths+subsystem_paths` относительно `similar_paths`
**положительна и не за счёт вытеснения core-файлов гибрида** → рычаг мержится, переходим к
Task 9.

Дельта нулевая или отрицательная → рычаг снимается: удалить `collect_subsystem_paths`,
`_subsystem_paths`, `max_subsystem_files`, оба варианта эвала и их тесты; **оставить** Task 1
(обобщение источников), Task 3 (состав в ридерах — он полезен сам по себе) и раздел отчёта с
отрицательным результатом. Прецедент — снятие co-change в PRI-257.

Перед выпиливанием проверить механику бюджета: три ошибки PRI-257 давали ровно нулевую дельту при
работающем сигнале. Убедиться по ноте видимости в сырой выдаче, что файлы разворота вообще
доезжают до секции.

- [ ] **Step 5: Коммит**

```bash
git add eval/replay_report.md eval/replay_history.jsonl
git commit -m "docs(eval): приёмка PRI-258 — замер разворота кластеров subsystems"
```

---

### Task 9: Документация (только при положительном вердикте)

**Files:**
- Modify: `CLAUDE.md` (раздел «Неочевидные факты»)
- Modify: `README.md`, `README.ru.md`
- Modify: `plugin/skills/configure-review/SKILL.md` (профиль retrieval-лимитов)

**Interfaces:**
- Consumes: результат Task 8.
- Produces: описание рычага и ключа `max_subsystem_files` в документации.

- [ ] **Step 1: Дописать факт в `CLAUDE.md`**

Абзац рядом с фактом PRI-257 — что подмешивание идёт списком именованных источников с
раздельными резервами; что релевантность кластеров считается своим ANN top-N (`SUBSYSTEM_TOPN`),
а не выдачей секции `subsystems`, и почему (бэк-компат-режим отдаёт все сводки по алфавиту);
что состав берётся из `member_node_ids` и вырезается из LLM-выдачи; что приоритет внутри
кластера — ранг сырого пула, а известность — по итоговой выдаче; измеренные числа из Task 8.

- [ ] **Step 2: Синхронно обновить оба README**

`README.md` (EN) и `README.ru.md` (RU) — ключ `context_limits.code_section.max_subsystem_files`
в описании политики.

- [ ] **Step 3: Обновить скилл configure-review**

Добавить `max_subsystem_files` в профиль retrieval-лимитов рядом с `max_augmented_files`.

- [ ] **Step 4: Прогнать guard-тесты скиллов и весь набор**

Run: `.venv/bin/pytest -q`
Expected: PASS (в том числе `tests/skills/test_configure_review_skill.py`).

- [ ] **Step 5: Коммит**

```bash
git add CLAUDE.md README.md README.ru.md plugin/skills/configure-review/SKILL.md
git commit -m "docs(pri-258): разворот кластеров subsystems в кандидаты секции code"
```

---

## Порядок и зависимости

```
Task 1 (источники списком) ─┬─> Task 5 (проводка) ─> Task 6 (приоритет) ─> Task 7 (эвал) ─> Task 8 (замер) ─> Task 9 (доки)
Task 2 (ключ политики) ─────┤
Task 3 (состав в ридерах) ──┤
Task 4 (сбор путей) ────────┘
```

Задачи 2, 3, 4 независимы друг от друга и от Task 1 (Task 4 использует только форму ответа стора
из Task 3, но тестируется на фейке) — их можно вести параллельно. Task 5 требует все четыре.
