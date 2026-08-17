# PRI-256 — Файловая диверсификация и файловый бюджет секции code

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Секция `code` контекста задачи должна отдавать ~12 различных файлов вместо нынешних 4, не давая кратного роста токенов брифа.

**Architecture:** Внутри `search_multi` (`reviewer/retrieval/multiquery.py`) появляется шаг диверсификации по файлам между `_dedupe_overlapping` и рендером, а символьный бюджет секции становится производным от числа файлов (`max_files × chars_per_file`) и перестаёт зависеть от общего `settings.max_tool_result_chars`. Лимиты живут в новом `CodeSectionLimits` (`reviewer/policy/context_limits.py`), отдельном от чанкового `CodebaseLimits`. `search_base`, `Retriever.retrieve` и публичный `search_codebase` не трогаются.

**Tech Stack:** Python 3, dataclasses (frozen), pytest, pydantic-settings; хранилища в этой задаче не участвуют.

**Spec:** `docs/superpowers/specs/2026-08-17-pri-256-file-diversification-design.md`

## Global Constraints

- Язык кода, комментариев, докстрингов и сообщений коммитов — **русский**.
- Коммиты — Conventional Commits на русском (`feat(retrieval): …`), **без self-attribution**: никаких `Co-Authored-By`, никаких упоминаний Claude/ИИ.
- TDD: сначала падающий тест, затем минимальная реализация.
- Unit-тесты запускаются **без сети, Postgres, Neo4j и localhost-сокетов**. Все тесты этого плана — unit; маркер `integration` не нужен.
- Команда тестов: `.venv/bin/pytest -q` (по умолчанию исключает `-m integration`).
- Линт: `ruff` по staged-файлам через pre-commit хук (`git config core.hooksPath .githooks` — уже настроено на клоне). Repo-wide чистота ruff не гарантирована; не гнаться за ней, отвечать только за свои файлы.
- `README.md` (EN) и `README.ru.md` (RU) правятся **синхронно** в одном коммите.
- Версия в `pyproject.toml` и контент под `plugin/` в этой задаче **не меняются**, поэтому `scripts/update_codex_plugin_manifest.py` прогонять не требуется.
- Дефолты, зафиксированные пользователем и не подлежащие изменению в ходе реализации: `max_files=12`, `max_chunks_per_file=1`, `chars_per_file=1300`.
- Запрещено трогать: `RRF_K` и его три объявления, `build_subqueries`/`MAX_SUBQUERIES`, `Retriever.search_base`, `Retriever.retrieve`, публичный MCP-тул `search_codebase`, механику среза в `ContextPack.as_context`.

---

### Task 1: Лимиты секции code в политике

**Files:**
- Modify: `reviewer/policy/context_limits.py:8-61`
- Test: `tests/policy/test_context_limits.py`

**Interfaces:**
- Consumes: ничего (первая задача).
- Produces: `reviewer.policy.context_limits.CodeSectionLimits` — frozen dataclass с полями `max_files: int = 12`, `max_chunks_per_file: int = 1`, `chars_per_file: int = 1300` и свойством `max_chars: int` (= `max_files * chars_per_file`); поле `ContextLimits.code_section: CodeSectionLimits`; ключ `.review.yml` — `context_limits.code_section`.

- [ ] **Step 1: Написать падающие тесты**

Дописать в конец `tests/policy/test_context_limits.py` (импорт в первой строке файла расширить до `from reviewer.policy.context_limits import CodeSectionLimits, ContextLimits, CodebaseLimits`):

```python
def test_code_section_defaults():
    cl = ContextLimits.from_review_yaml({})
    assert cl.code_section == CodeSectionLimits()
    assert cl.code_section.max_files == 12
    assert cl.code_section.max_chunks_per_file == 1
    assert cl.code_section.chars_per_file == 1300


def test_code_section_max_chars_is_derived():
    """Символьный потолок производный: объём линеен по числу файлов."""
    assert CodeSectionLimits().max_chars == 12 * 1300
    assert CodeSectionLimits(max_files=20, chars_per_file=600).max_chars == 12000


def test_code_section_partial_block_keeps_other_defaults():
    cl = ContextLimits.from_review_yaml(
        {"context_limits": {"code_section": {"max_files": 20}}})
    assert cl.code_section.max_files == 20
    assert cl.code_section.chars_per_file == 1300        # дефолт сохранён
    assert cl.code_section.max_chunks_per_file == 1


def test_code_section_is_independent_of_search_codebase():
    """Бюджет секции code не связан с чанковым потолком общего поиска."""
    cl = ContextLimits.from_review_yaml(
        {"context_limits": {"search_codebase": {"ceiling": 30}}})
    assert cl.search_codebase.ceiling == 30
    assert cl.code_section == CodeSectionLimits()
```

- [ ] **Step 2: Прогнать тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/policy/test_context_limits.py -q`
Expected: FAIL — `ImportError: cannot import name 'CodeSectionLimits'`

- [ ] **Step 3: Реализовать**

В `reviewer/policy/context_limits.py` добавить dataclass после `CodebaseLimits`:

```python
@dataclass(frozen=True)
class CodeSectionLimits:
    """Бюджет секции code контекста задачи (PRI-256). Единица бюджета — файл.

    Отдельный от CodebaseLimits намеренно: тот обслуживает публичный
    search_codebase, /ask и грунтовку, где единица бюджета — чанк, а потолок
    связан с квотой реранкера. Смешение двух шкал в одном dataclass сделало бы
    невыразимым «бюджет секции, независимый от чанкового потолка».
    """
    max_files: int = 12          # различных файлов в секции
    max_chunks_per_file: int = 1  # чанков на один файл
    chars_per_file: int = 1300   # доля символов на файл

    @property
    def max_chars(self) -> int:
        """Символьный потолок секции — производный, а не отдельный ключ.

        Так объём растёт строго линейно по числу файлов: кратный рост токенов
        при росте числа файлов невозможен по построению.
        """
        return self.max_files * self.chars_per_file
```

В `ContextLimits` добавить поле после `search_codebase`:

```python
    code_section: CodeSectionLimits = field(default_factory=CodeSectionLimits)
```

В `from_review_yaml` добавить чтение блока рядом с существующими (`cb`, `st`, `gr`):

```python
    cs = block.get("code_section") or {}
```

и подсекцию в конструктор `cls(...)`:

```python
            code_section=CodeSectionLimits(
                max_files=int(cs.get("max_files", CodeSectionLimits.max_files)),
                max_chunks_per_file=int(
                    cs.get("max_chunks_per_file", CodeSectionLimits.max_chunks_per_file)),
                chars_per_file=int(
                    cs.get("chars_per_file", CodeSectionLimits.chars_per_file)),
            ),
```

- [ ] **Step 4: Прогнать тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/policy/ -q`
Expected: PASS, включая существующие `test_defaults_when_no_block`, `test_partial_block_keeps_other_defaults`, `test_subsections_search_tasks_and_graph`.

- [ ] **Step 5: Коммит**

```bash
git add reviewer/policy/context_limits.py tests/policy/test_context_limits.py
git commit -m "feat(policy): CodeSectionLimits — файловый бюджет секции code"
```

---

### Task 2: Диверсификация по файлам и производный бюджет в search_multi

**Files:**
- Modify: `reviewer/retrieval/multiquery.py:29-35` (удалить `MAX_BLOCK_CHARS`), `:56-75` (`cap_block`), `:128-159` (`search_multi`); добавить `diversify_by_file`
- Test: `tests/retrieval/test_multiquery.py:1-4` (импорты), `:43-61` (тесты `cap_block`), `:184-190` (тест потолка)

**Interfaces:**
- Consumes: `reviewer.policy.context_limits.CodeSectionLimits` из Task 1 (`max_files`, `max_chunks_per_file`, `chars_per_file`, `max_chars`).
- Produces: `reviewer.retrieval.multiquery.diversify_by_file(items: list, *, max_files: int, max_chunks_per_file: int) -> list`; новый kwarg `section_limits: CodeSectionLimits | None = None` у `search_multi`; `cap_block(item, max_chars: int)` — аргумент стал **обязательным**; модульная константа `MAX_BLOCK_CHARS` **удалена**.

- [ ] **Step 1: Написать падающие тесты**

Заменить строку 4 файла `tests/retrieval/test_multiquery.py`:

```python
from reviewer.policy.context_limits import CodebaseLimits, CodeSectionLimits
from reviewer.retrieval.multiquery import (
    cap_block, diversify_by_file, rrf_merge, search_multi,
)
```

(строку `from reviewer.policy.context_limits import CodebaseLimits` на строке 3 удалить — она заменена строкой выше).

Обновить три существующих теста `cap_block` под обязательный аргумент:

```python
def test_short_block_is_untouched():
    item = _hit("a.py#f", 10, 11, "one\ntwo")
    assert cap_block(item, 2000) is item


def test_long_block_is_cut_on_line_boundary_with_honest_end_line():
    body = "\n".join(f"строка {i}" * 20 for i in range(200))
    item = _hit("a.py#f", start_line=100, end_line=299, text=body)
    capped = cap_block(item, 2000)
    assert len(capped.text) <= 2000
    assert capped.text.splitlines() == item.text.splitlines()[: len(capped.text.splitlines())]
    assert capped.end_line == 100 + len(capped.text.splitlines()) - 1
    assert capped.end_line < 299


def test_cap_block_does_not_mutate_source():
    item = _hit("a.py#f", 1, 400, "x" * 5000)
    cap_block(item, 2000)
    assert len(item.text) == 5000
```

Заменить `test_ceiling_caps_merged_output` (строки 184-190) — чанковый потолок выдачи снят, его роль занял файловый:

```python
def test_file_budget_caps_distinct_files():
    """Выдачу режет файловый бюджет секции, а не чанковый ceiling общего поиска."""
    hits = [_bm25(f"f{i}.py#s") for i in range(40)]
    store = _FakeStore({"q0": hits})
    pack = search_multi(_Retriever(store, _FakeEmbedder()), "o/n", ["q0"],
                        limits=CodebaseLimits(ceiling=5), branch="dev")
    assert len({it.path for it in pack.items}) == 12, "дефолт max_files, а не ceiling=5"
```

Дописать новые тесты в конец файла:

```python
def test_diversify_keeps_one_chunk_per_file_by_default():
    items = [_hit("a.py#f1"), _hit("a.py#f2"), _hit("b.py#g")]
    kept = diversify_by_file(items, max_files=10, max_chunks_per_file=1)
    assert [it.node_id for it in kept] == ["a.py#f1", "b.py#g"]


def test_diversify_allows_several_chunks_when_configured():
    items = [_hit("a.py#f1"), _hit("a.py#f2"), _hit("a.py#f3")]
    kept = diversify_by_file(items, max_files=10, max_chunks_per_file=2)
    assert [it.node_id for it in kept] == ["a.py#f1", "a.py#f2"]


def test_diversify_caps_distinct_files_and_keeps_input_order():
    items = [_hit(f"f{i}.py#s") for i in range(10)]
    kept = diversify_by_file(items, max_files=3, max_chunks_per_file=1)
    assert [it.path for it in kept] == ["f0.py", "f1.py", "f2.py"]


def test_diversify_degenerate_values_do_not_crash():
    items = [_hit("a.py#f"), _hit("b.py#g")]
    assert diversify_by_file(items, max_files=0, max_chunks_per_file=1) == []
    assert diversify_by_file([], max_files=5, max_chunks_per_file=1) == []


def test_graph_only_tail_yields_to_hybrid_files():
    """Приоритет входного порядка: hybrid-файлы занимают бюджет раньше graph-only."""
    graph_node = _hit("graph.py#z")
    store = _FakeStore({"q0": [_bm25("hyb.py#a")]},
                       nodes_by_id={"graph.py#z": graph_node})
    retriever = _Retriever(store, _FakeEmbedder(), graph=_FakeGraph(["graph.py#z"]))
    pack = search_multi(retriever, "o/n", ["q0"], limits=CodebaseLimits(),
                        section_limits=CodeSectionLimits(max_files=1), branch="dev")
    assert [it.path for it in pack.items] == ["hyb.py"]


def test_section_budget_fits_selected_files_without_truncation():
    """Производный бюджет обязан вмещать то, что отобрано: среза строки нет."""
    body = "\n".join("y" * 80 for _ in range(60))
    store = _FakeStore({"q0": [_bm25(f"f{i}.py#s", text=body) for i in range(12)]})
    pack = search_multi(_Retriever(store, _FakeEmbedder()), "o/n", ["q0"],
                        limits=CodebaseLimits(), branch="dev")
    context = pack.as_context(line_numbers=True)
    assert "[...truncated]" not in context
    assert len({it.path for it in pack.items}) == 12


def test_section_budget_ignores_retriever_max_context_chars():
    """Бюджет секции отдельный: max_context_chars ретривера на неё не влияет."""
    store = _FakeStore({"q0": [_bm25("a.py#f")]})
    pack = search_multi(_Retriever(store, _FakeEmbedder(), max_context_chars=100),
                        "o/n", ["q0"], limits=CodebaseLimits(), branch="dev")
    assert pack.max_chars == CodeSectionLimits().max_chars
```

- [ ] **Step 2: Прогнать тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/retrieval/test_multiquery.py -q`
Expected: FAIL — `ImportError: cannot import name 'diversify_by_file'`

- [ ] **Step 3: Реализовать**

В `reviewer/retrieval/multiquery.py` удалить блок константы `MAX_BLOCK_CHARS` (строки 29-35 вместе с докстрингом) и сделать аргумент `cap_block` обязательным:

```python
def cap_block(item, max_chars: int):
    """Обрезать текст блока по границе строк, поправив end_line.

    Границу строк держим не ради красоты: as_context нумерует строки от
    start_line, а extract_context_paths требует в заголовке диапазон
    ':\\d+-\\d+'. Обрезка по середине строки сделала бы заголовок ложью, а
    усечение уже в as_context — вовсе съело бы заголовок и потеряло путь.

    Бюджет приходит из политики (CodeSectionLimits.chars_per_file), а не из
    модульной константы: доля на файл — часть файлового бюджета секции.
    """
```

Тело функции не меняется.

Добавить функцию диверсификации перед `search_multi`:

```python
def diversify_by_file(items: list, *, max_files: int, max_chunks_per_file: int) -> list:
    """Оставить не более max_chunks_per_file чанков на путь и не более max_files путей.

    Идёт по входному порядку и порядок выживших не меняет, поэтому приоритет
    «сначала hybrid, потом graph-only» и ранг RRF внутри файла сохраняются.

    Зовётся строго ПОСЛЕ _dedupe_overlapping: тот оставляет самый широкий чанк
    из вложенных, и обратный порядок удержал бы вложенный метод, выбросив
    охватывающий класс, — то есть ухудшил бы выдачу, а не улучшил.
    """
    per_file: dict[str, int] = {}
    kept: list = []
    for item in items:
        taken = per_file.get(item.path, 0)
        if taken >= max_chunks_per_file:
            continue
        if taken == 0 and len(per_file) >= max_files:
            continue
        per_file[item.path] = taken + 1
        kept.append(item)
    return kept
```

Переписать сигнатуру и хвост `search_multi`:

```python
def search_multi(retriever, repo: str, queries: list[str], *, limits=None,
                 section_limits=None, hops: int = 1, branch: str = "",
                 include_tests: bool = False) -> ContextPack:
    """Мультизапросный ретрив по base-индексу ветки: N прогонов, RRF, обрезка.

    Реранкера и cliff-отсечки здесь нет — финальный ранкер RRF (см. докстринг
    модуля). Порядок «сначала hybrid, потом graph-only» сохранён из search_base:
    hybrid приоритетен, граф добавляет разнообразие.

    Бюджет выдачи файловый (PRI-256): не более section_limits.max_files
    различных путей, символьный потолок производный — max_files × chars_per_file.
    """
    from reviewer.policy.context_limits import CodebaseLimits, CodeSectionLimits
    lim = limits or CodebaseLimits()
    sec = section_limits or CodeSectionLimits()
```

Остальное тело до `items = _dedupe_overlapping(items)` не меняется. Заменить последние три строки функции (строки 157-159 текущего файла):

```python
    items = diversify_by_file(_dedupe_overlapping(items),
                              max_files=sec.max_files,
                              max_chunks_per_file=sec.max_chunks_per_file)
    return ContextPack(items=[cap_block(item, sec.chars_per_file) for item in items],
                       max_chars=sec.max_chars)
```

Отсечка `[:lim.ceiling]` тем самым исчезает. Второе использование `lim.ceiling` — число сидов graph-expansion на строке `_graph_items(retriever, repo, merged, lim.ceiling, ...)` — **остаётся без изменений**: там это глубина расширения, а не бюджет выдачи.

- [ ] **Step 4: Прогнать тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/retrieval/ -q`
Expected: PASS. Существующие `tests/retrieval/test_search_base.py` обязаны пройти **без правок** — это guard того, что `search_base` не задет.

- [ ] **Step 5: Проверить, что нигде не осталось ссылок на удалённую константу**

Run: `grep -rn "MAX_BLOCK_CHARS" reviewer/ tests/ eval/`
Expected: пусто. Если что-то найдено — заменить на `CodeSectionLimits().chars_per_file` и дописать в этот же коммит.

- [ ] **Step 6: Коммит**

```bash
git add reviewer/retrieval/multiquery.py tests/retrieval/test_multiquery.py
git commit -m "feat(retrieval): файловая диверсификация и производный бюджет секции code"
```

---

### Task 3: Проброс лимитов секции из политики в MCP-слой

**Files:**
- Modify: `reviewer/mcp/service.py:1797-1819` (`_search_codebase_multi`)
- Test: `tests/mcp/test_context_limits_wiring.py:293-320`

**Interfaces:**
- Consumes: `ContextLimits.code_section` (Task 1), kwarg `section_limits` у `search_multi` (Task 2).
- Produces: ничего нового наружу — `_search_codebase_multi` остаётся приватным и возвращает строку.

- [ ] **Step 1: Написать падающий тест**

В `tests/mcp/test_context_limits_wiring.py` расширить импорт лимитов до `from reviewer.policy.context_limits import CodebaseLimits, CodeSectionLimits` (дописать имя к существующей строке импорта) и дописать тест после `test_search_codebase_multi_passes_limits_and_hops`:

```python
def test_search_codebase_multi_passes_code_section_limits(
    isolated_xdg_config_home,
) -> None:
    """_search_codebase_multi (PRI-256) пробрасывает в search_multi файловый
    бюджет секции, резолвленный из эффективной .review.yml-политики.

    Значения в домашнем слое НЕ совпадают с дефолтами (12/1/1300), иначе тест
    не отличил бы резолв политики от захардкоженных CodeSectionLimits().
    """
    path = isolated_xdg_config_home / "rag-reviewer/repos/o/r.yml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "context_limits: {code_section: {max_files: 5, chars_per_file: 400}}\n",
        encoding="utf-8")
    s = _settings()
    s.review_branches = "dev"
    components = MagicMock()
    vcs = MagicMock()
    vcs.get_file_at_ref.return_value = None
    svc = MCPReviewService(s, components, vcs_factory=lambda o, n: vcs)

    with patch("reviewer.retrieval.multiquery.search_multi") as sm:
        sm.return_value.as_context.return_value = "ok"
        svc._search_codebase_multi("o/r", ["q1"], branch="dev")

    call = sm.call_args
    assert isinstance(call.kwargs["section_limits"], CodeSectionLimits)
    assert call.kwargs["section_limits"].max_files == 5
    assert call.kwargs["section_limits"].chars_per_file == 400
    assert call.kwargs["section_limits"].max_chunks_per_file == 1   # дефолт сохранён
```

- [ ] **Step 2: Прогнать тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/mcp/test_context_limits_wiring.py::test_search_codebase_multi_passes_code_section_limits -q`
Expected: FAIL — `KeyError: 'section_limits'`

- [ ] **Step 3: Реализовать**

В `reviewer/mcp/service.py`, в `_search_codebase_multi`, дополнить вызов:

```python
            pack = search_multi(
                self.components.retriever, repo, queries,
                limits=cl.search_codebase, section_limits=cl.code_section,
                hops=cl.graph.hops, branch=resolved, include_tests=include_tests)
```

Резолв политики остаётся единственным (`cl = self._resolve_context_limits(repo, resolved)` уже есть выше) — параллельной ветки чтения `.review.yml` не заводить.

- [ ] **Step 4: Прогнать тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/mcp/ -q`
Expected: PASS, включая существующие `test_search_codebase_multi_delegates_to_search_multi`, `test_search_codebase_multi_passes_limits_and_hops`, `test_search_codebase_multi_empty_or_error_returns_note`.

- [ ] **Step 5: Прогнать весь unit-набор**

Run: `.venv/bin/pytest -q`
Expected: PASS. Красный тест вне `tests/retrieval`, `tests/policy`, `tests/mcp` означает незамеченного потребителя — чинить в этом же коммите, не расширяя скоуп.

- [ ] **Step 6: Коммит**

```bash
git add reviewer/mcp/service.py tests/mcp/test_context_limits_wiring.py
git commit -m "feat(mcp): проброс файлового бюджета секции code в search_multi"
```

---

### Task 4: Документация

**Files:**
- Modify: `.review.yml` (блок `context_limits` — сейчас в файле отсутствует, добавить закомментированным примером рядом с `summary_cluster_depth`)
- Modify: `CLAUDE.md` (раздел «Неочевидные факты», рядом с абзацем про PRI-255)
- Modify: `README.md`, `README.ru.md` (описание `context_limits`)

**Interfaces:**
- Consumes: имена ключей и дефолты из Task 1 (`context_limits.code_section`: `max_files=12`, `max_chunks_per_file=1`, `chars_per_file=1300`).
- Produces: ничего исполняемого.

- [ ] **Step 1: Добавить пример в `.review.yml`**

Дописать в конец файла:

```yaml
# --- Бюджет секции code контекста задачи (PRI-256) ---

# Единица бюджета секции code — ФАЙЛ, а не чанк: в бриф уходят строки
# «path:line — почему», поэтому 12 файлов там дешевле, чем 4 тела символов.
# Бюджет независим от context_limits.search_codebase (тот обслуживает /ask,
# грунтовку и ревью PR, где единица — чанк). Символьный потолок секции не
# задаётся отдельно: он производный, max_files × chars_per_file.
# Значения ниже — дефолты; блок можно не выписывать вовсе.
# context_limits:
#   code_section:
#     max_files: 12
#     max_chunks_per_file: 1
#     chars_per_file: 1300
```

- [ ] **Step 2: Добавить неочевидный факт в `CLAUDE.md`**

Вставить абзац сразу после существующего абзаца про PRI-255 (он начинается со слов «**Секция `code` контекста задачи идёт мультизапросом…**»):

```markdown
- **Бюджет секции `code` файловый, и символьный потолок у него производный (PRI-256).**
  До PRI-256 выдачу секции резала арифметика, а не ранжирование: `search_multi` отбирал
  `ceiling = 15` чанков, `cap_block` жал каждый блок до 2000 символов, а `ContextPack.as_context`
  срезал весь текст на `settings.max_tool_result_chars = 8000` — то есть до сборщика брифа
  физически доезжали 8000 ÷ 2000 = **4 блока**, остальные 11 отбрасывались молча. Отсюда и
  измеренная медиана «4 файла». Теперь единица бюджета — файл: `CodeSectionLimits`
  (`reviewer/policy/context_limits.py`, ключ `.review.yml` — `context_limits.code_section`) задаёт
  `max_files=12`, `max_chunks_per_file=1`, `chars_per_file=1300`, а `diversify_by_file`
  (`reviewer/retrieval/multiquery.py`) применяет их между `_dedupe_overlapping` и рендером.
  Три вещи здесь неочевидны. Во-первых, **символьный потолок секции — производный**
  (`max_files × chars_per_file`) и не выведен отдельным ключом: только так объём растёт линейно
  по числу файлов, а не кратно — третьего регулятора, который мог бы рассинхронизироваться с
  двумя другими, просто нет. Во-вторых, **порядок обязателен**: диверсификация идёт строго ПОСЛЕ
  `_dedupe_overlapping`, который оставляет самый широкий чанк из вложенных; обратный порядок
  удержал бы вложенный метод и выбросил охватывающий класс. В-третьих, бюджет **отдельный от
  `CodebaseLimits`**: тот обслуживает публичный `search_codebase`, `/ask` и грунтовку, где единица
  бюджета — чанк; `search_base` и публичный тул этой задачей не затронуты вовсе. Прежняя чанковая
  отсечка `[:lim.ceiling]` в `search_multi` снята, но `lim.ceiling` остался числом сидов
  graph-expansion — это глубина расширения, а не бюджет выдачи. Замер приёмки —
  `eval/replay_report.md`, раздел «Приёмка PRI-256».
```

- [ ] **Step 3: Синхронно обновить оба README**

Найти в `README.ru.md` описание `context_limits` (`grep -n "context_limits" README.ru.md`) и дописать в тот же блок подсекцию `code_section` с тремя ключами, дефолтами и одной фразой «бюджет секции `code` файловый; символьный потолок производный — `max_files × chars_per_file`». Затем внести эквивалентную правку в `README.md` (EN) в тот же по смыслу блок. Если блока `context_limits` в README нет — добавить его в раздел о `.review.yml`, перечислив все четыре подсекции (`search_codebase`, `search_tasks`, `graph`, `code_section`).

- [ ] **Step 4: Проверить синхронность**

Run: `grep -n "code_section" README.md README.ru.md .review.yml CLAUDE.md`
Expected: попадания во всех четырёх файлах; в README.md и README.ru.md — сопоставимое число строк.

- [ ] **Step 5: Коммит**

```bash
git add .review.yml CLAUDE.md README.md README.ru.md
git commit -m "docs: файловый бюджет секции code (context_limits.code_section)"
```

---

### Task 5: Замер приёмки и раздел отчёта

**Files:**
- Modify: `eval/replay_report.md` (новый раздел «Приёмка PRI-256»)
- Read-only: `eval/solve_task_metrics/replay.py`, `eval/solve_task_metrics/recall.py`, `eval/replay_report.md` (раздел «Приёмка PRI-255» — источник baseline)

**Interfaces:**
- Consumes: поведение, реализованное в Tasks 1-3.
- Produces: зафиксированные числа приёмки — медиана числа различных файлов секции `code`, медиана core-recall, bulk core-recall, precision.

**Внимание:** `eval/` входит в `paths.ignore` (`.review.yml`) — это не продакшн-код, в ревью-индекс он не попадает. Прогон тратит квоту Voyage (free tier 3 RPM / 10K TPM), поэтому запускается один раз, после того как Tasks 1-3 зелёные.

- [ ] **Step 1: Прочитать существующий раздел приёмки PRI-255**

Run: `grep -n "Приёмка PRI-255" -A 40 eval/replay_report.md`
Зафиксировать формат таблицы, набор метрик, значение `indexed_sha` и baseline-числа: медиана core-recall `0.225 → 0.3333`, bulk `0.1548 → 0.1825`, «предсказано файлов» `2 → 4`, precision `0.875 → 0.5`.

- [ ] **Step 2: Разобраться, как запускается replay**

Run: `.venv/bin/python -m eval.solve_task_metrics.replay --help`
Если модуль не имеет CLI — прочитать `eval/solve_task_metrics/replay.py` и найти точку входа, использованную для замера PRI-255 (она описана в разделе «процедура воспроизведения критерия 3» отчёта, коммит `44f5814`).

- [ ] **Step 3: Прогнать замер на том же `indexed_sha`**

Запустить replay ровно тем же способом и на том же `indexed_sha`, что и замер PRI-255 — иначе дельта будет посчитана другой линейкой и критерий приёмки №2 («дельта замерена отдельно от остальных рычагов») не будет закрыт. Сохранить сырой вывод.

Expected: медиана числа различных файлов заметно выше 4; core-recall и bulk core-recall не ниже значений PRI-255.

- [ ] **Step 4: Записать раздел отчёта**

Дописать в `eval/replay_report.md` раздел «Приёмка PRI-256» в формате существующего раздела PRI-255. Раздел обязан содержать:
- таблицу «до → после» по четырём метрикам, с `indexed_sha` и датой;
- явную оговорку про baseline: **источник истины — «4 файла» из раздела «Приёмка PRI-255»**, а не «5-6» из описания тикета; цифра «5-6» ни одним отчётом не подтверждается и, судя по всему, предшествует замеру PRI-255. Без этой оговорки приёмка сверялась бы с двумя разными линейками;
- дельту объёма секции в символах рядом с дельтой числа файлов — это и есть проверка критерия №3 (рост объёма обязан быть не кратным росту числа файлов);
- процедуру воспроизведения (точная команда), по образцу того, как это сделано для PRI-255.

- [ ] **Step 5: Коммит**

```bash
git add eval/replay_report.md
git commit -m "docs(eval): приёмка PRI-256 — файловая диверсификация секции code"
```

---

## Self-Review

**1. Покрытие спеки.**

| Требование спеки | Задача |
|---|---|
| `CodeSectionLimits` + парсинг `context_limits.code_section` | Task 1 |
| Производный символьный потолок `max_files × chars_per_file` | Task 1 (свойство `max_chars`), Task 2 (передача в `ContextPack`) |
| `diversify_by_file` после `_dedupe_overlapping` | Task 2 |
| Снятие `[:lim.ceiling]`, сохранение `lim.ceiling` как сидов графа | Task 2, Step 3 |
| Удаление `MAX_BLOCK_CHARS`, `cap_block` с явным аргументом | Task 2, Steps 3 и 5 |
| Применение к секциям `code` и `test_exemplars` | Task 3 (обе идут через `_search_codebase_multi`) |
| Неизменность `search_base` / `search_codebase` | Task 2, Step 4 (guard существующими тестами) |
| Тесты политики | Task 1 |
| Тесты диверсификации, бюджета, порядка, вырожденных значений | Task 2 |
| Замер приёмки, baseline = 4 | Task 5 |
| Документация (CLAUDE.md, README×2, .review.yml) | Task 4 |

Пробелов нет.

**2. Плейсхолдеры.** Не найдено: каждый шаг с кодом содержит код, каждая проверка — команду и ожидаемый результат. Task 4, Step 3 и Task 5, Steps 2-4 описаны процедурно, а не кодом, — сознательно: точное место правки в README и точная CLI-форма replay зависят от текущего содержимого файлов, которое исполнителю предписано прочитать первым шагом.

**3. Согласованность имён и типов.** `CodeSectionLimits` (поля `max_files`, `max_chunks_per_file`, `chars_per_file`, свойство `max_chars`) — одинаково в Tasks 1, 2, 3, 4. `diversify_by_file(items, *, max_files, max_chunks_per_file)` — одинаково в Task 2 (реализация и тесты). Kwarg `section_limits` — одинаково в Tasks 2 и 3. `cap_block(item, max_chars)` — обязательный аргумент во всех трёх обновлённых тестах и в реализации. `ContextLimits.code_section` — одинаково в Tasks 1 и 3.

**Найдено и учтено при написании плана:** удаление `[:lim.ceiling]` ломает существующий `test_ceiling_caps_merged_output`, а удаление `MAX_BLOCK_CHARS` ломает импорт на строке 4 `tests/retrieval/test_multiquery.py` и три теста `cap_block`. Все четыре правки выписаны явно в Task 2, Step 1 — иначе исполнитель Task 2 получил бы красный набор без объяснения причины.
