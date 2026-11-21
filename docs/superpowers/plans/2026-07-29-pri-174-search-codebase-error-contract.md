# PRI-174 Search Codebase Error Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Научить `search_codebase` безопасно отличать корректный пустой результат от недоступности embeddings, storage и неизвестного внутреннего сбоя, чтобы глобальный плагин переходил к локальному поиску без повторного Voyage-вызова.

**Architecture:** `Retriever.search_base` типизирует отказы двух обязательных этапов через `SearchUnavailableError` и сохраняет исходную причину exception chaining. `MCPReviewService.search_codebase` переводит только известные категории в стабильные публичные сообщения, а все остальные исключения — в безопасную внутреннюю ошибку; `reviewer_ask` распознаёт общий unavailable-prefix и использует существующий lexical fallback.

**Tech Stack:** Python 3.11–3.13, pytest, unittest.mock, FastMCP service layer, Markdown skills/guard-тесты.

## Global Constraints

- Инфраструктурная ошибка никогда не возвращается как `(ничего не найдено)`.
- Подлинно пустой результат сохраняет прежнее сообщение `(ничего не найдено)`.
- Публичный ответ содержит только категории `embeddings`, `storage` или `внутренняя ошибка`; исходный текст исключения, DSN, URL и секреты не выводятся.
- Graph expansion и reranker остаются fail-soft и возвращают доступные результаты hybrid search.
- После unavailable-note skill не повторяет semantic search и не делает новый Voyage-вызов.
- Алгоритм ранжирования, relevance scoring и cliff-отсечка не меняются.
- Локальный lexical fallback остаётся на уровне skill; MCP-сервис не реализует локальный поиск.
- Новые зависимости и сетевые вызовы не добавляются.
- Комментарии, докстринги и сообщения кода остаются на русском; line length — 100.

## File Map

- `reviewer/retrieval/retriever.py` — объявляет доменную ошибку и устанавливает границы обязательных этапов `search_base`.
- `tests/retrieval/test_search_base.py` — доказывает категории, exception chaining и сохранение fail-soft поведения.
- `reviewer/mcp/service.py` — отображает внутренние категории в безопасный стабильный MCP-контракт.
- `tests/mcp/test_service.py` — фиксирует публичные сообщения, отсутствие утечки и обратную совместимость успешной/пустой выдачи.
- `plugin/skills/ask/SKILL.md` — распознаёт unavailable-prefix и запрещает повторный semantic search до lexical fallback.
- `tests/skills/test_ask_uses_summaries.py` — guard-тест публичного поведения глобального skill.

---

### Task 1: Типизированные отказы обязательных этапов Retriever

**Files:**
- Modify: `reviewer/retrieval/retriever.py:1-8,111-159`
- Modify: `tests/retrieval/test_search_base.py:1-77,104-116,188-206`

**Interfaces:**
- Consumes: `embedder.embed_query(query)`, `store.hybrid_search(...)`, существующий `ContextPack`.
- Produces: `SearchUnavailableError(component: str)` с публичным атрибутом `component`; `search_base(...) -> ContextPack` либо `raise SearchUnavailableError("embeddings" | "storage")`.

- [ ] **Step 1: Расширить тестовые doubles управляемыми отказами**

В `tests/retrieval/test_search_base.py` импортировать `pytest` и новый тип:

```python
import pytest

from reviewer.policy.context_limits import CodebaseLimits
from reviewer.retrieval.retriever import (
    ContextPack,
    Retriever,
    SearchUnavailableError,
)
```

Изменить `_FakeStore` и `_FakeEmbedder`, не затрагивая их успешное поведение:

```python
class _FakeStore:
    def __init__(self, hits, related=None, error: Exception | None = None):
        self._hits = hits
        self._related = related or []
        self._error = error
        self.search_calls = []
        self.fetch_calls = []

    def hybrid_search(self, repo, *, query_text, query_embedding, overlay_ref,
                      changed_paths, top_k, candidates, base_ref="base"):
        self.search_calls.append({
            "repo": repo, "overlay_ref": overlay_ref, "changed_paths": changed_paths,
            "top_k": top_k, "candidates": candidates,
        })
        if self._error is not None:
            raise self._error
        return self._hits
```

```python
class _FakeEmbedder:
    def __init__(self, error: Exception | None = None):
        self._error = error
        self.queries = []

    def embed_query(self, text):
        self.queries.append(text)
        if self._error is not None:
            raise self._error
        return [0.1] * 8
```

- [ ] **Step 2: Написать падающие тесты категорий и chaining**

Добавить рядом с `test_search_base_empty_returns_empty_pack`:

```python
def test_search_base_wraps_embedding_failure():
    cause = RuntimeError("voyage transport detail")
    retriever = Retriever(
        _FakeStore([]),
        graph=None,
        embedder=_FakeEmbedder(error=cause),
        reranker=None,
    )

    with pytest.raises(SearchUnavailableError) as caught:
        retriever.search_base("a/x", "nothing", limits=_cb())

    assert caught.value.component == "embeddings"
    assert caught.value.__cause__ is cause


def test_search_base_wraps_storage_failure():
    cause = RuntimeError("postgres transport detail")
    retriever = Retriever(
        _FakeStore([], error=cause),
        graph=None,
        embedder=_FakeEmbedder(),
        reranker=None,
    )

    with pytest.raises(SearchUnavailableError) as caught:
        retriever.search_base("a/x", "nothing", limits=_cb())

    assert caught.value.component == "storage"
    assert caught.value.__cause__ is cause
```

Существующие `test_search_base_empty_returns_empty_pack`,
`test_search_base_graph_down_falls_back_to_hybrid` и
`test_search_base_reranker_failure_falls_back_to_rrf` остаются регрессиями для трёх
неизменённых путей.

- [ ] **Step 3: Запустить новые тесты и подтвердить красную фазу**

Run:

```bash
.venv/bin/pytest -q \
  tests/retrieval/test_search_base.py::test_search_base_wraps_embedding_failure \
  tests/retrieval/test_search_base.py::test_search_base_wraps_storage_failure
```

Expected: FAIL при импорте `SearchUnavailableError`, потому что тип ещё не объявлен.

- [ ] **Step 4: Реализовать минимальную доменную ошибку и две границы**

В `reviewer/retrieval/retriever.py` после `log` добавить:

```python
class SearchUnavailableError(RuntimeError):
    """Обязательный компонент base-поиска временно недоступен."""

    def __init__(self, component: str) -> None:
        self.component = component
        super().__init__(f"search unavailable: {component}")
```

В `Retriever.search_base` заменить два обязательных вызова:

```python
try:
    qvec = self.embedder.embed_query(query)
except Exception as error:
    raise SearchUnavailableError("embeddings") from error

try:
    hits = self.store.hybrid_search(
        repo,
        query_text=query,
        query_embedding=qvec,
        overlay_ref="__none__",
        changed_paths=[],
        top_k=lim.candidate_pool,
        candidates=lim.candidate_pool,
        base_ref=bref,
    )
except Exception as error:
    raise SearchUnavailableError("storage") from error
```

Не оборачивать `_dedupe_overlapping`, graph expansion, `fetch_nodes`, reranker и
`select_by_cliff`: graph/reranker уже имеют собственные fail-soft ветки, а неожиданная ошибка
после обязательных шагов должна дойти до generic boundary MCP-сервиса.

- [ ] **Step 5: Запустить весь Retriever test file**

Run:

```bash
.venv/bin/pytest -q tests/retrieval/test_search_base.py
```

Expected: PASS; новые категории и существующие empty/graph/reranker сценарии зелёные.

- [ ] **Step 6: Проверить стиль и закоммитить Task 1**

Run:

```bash
.venv/bin/ruff check reviewer/retrieval/retriever.py tests/retrieval/test_search_base.py
git diff --check
git add reviewer/retrieval/retriever.py tests/retrieval/test_search_base.py
git commit -m "feat(retrieval): типизировать недоступность base-поиска"
```

Expected: ruff и `git diff --check` завершаются с exit 0; коммит содержит только Retriever и
его unit-тесты.

---

### Task 2: Безопасный публичный контракт MCP

**Files:**
- Modify: `reviewer/mcp/service.py:25-29,824-849`
- Modify: `tests/mcp/test_service.py:1-15,613-637`

**Interfaces:**
- Consumes: `SearchUnavailableError.component` из Task 1 и `ContextPack.as_context(line_numbers=True)`.
- Produces: `MCPReviewService.search_codebase(...) -> str` с четырьмя стабильными исходами: успешный line-numbered текст, `(ничего не найдено)`, `(поиск недоступен — embeddings|storage)`, `(поиск недоступен — внутренняя ошибка)`.

- [ ] **Step 1: Написать падающие тесты публичных сообщений**

В `tests/mcp/test_service.py` добавить:

```python
from reviewer.retrieval.retriever import SearchUnavailableError
```

Разделить старый `test_search_codebase_empty_or_error_returns_note` на пустой результат и
параметризованные ошибки:

```python
def test_search_codebase_empty_returns_not_found_note() -> None:
    svc = _make_mcp_service()
    svc.components.retriever.search_base.return_value.as_context.return_value = ""

    assert svc.search_codebase("a/b", "x") == "(ничего не найдено)"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            SearchUnavailableError("embeddings"),
            "(поиск недоступен — embeddings)",
        ),
        (
            SearchUnavailableError("storage"),
            "(поиск недоступен — storage)",
        ),
        (
            SearchUnavailableError("future-component"),
            "(поиск недоступен — внутренняя ошибка)",
        ),
        (
            RuntimeError("postgresql://user:secret@host/db"),
            "(поиск недоступен — внутренняя ошибка)",
        ),
    ],
)
def test_search_codebase_maps_failures_to_safe_notes(
    error: Exception,
    expected: str,
) -> None:
    svc = _make_mcp_service()
    svc.components.retriever.search_base.side_effect = error

    out = svc.search_codebase("a/b", "x")

    assert out == expected
    assert "secret" not in out
```

- [ ] **Step 2: Запустить MCP-тесты и подтвердить красную фазу**

Run:

```bash
.venv/bin/pytest -q \
  tests/mcp/test_service.py::test_search_codebase_empty_returns_not_found_note \
  tests/mcp/test_service.py::test_search_codebase_maps_failures_to_safe_notes
```

Expected: empty-case PASS, error cases FAIL — текущий сервис возвращает
`(ничего не найдено)` для каждого исключения.

- [ ] **Step 3: Реализовать whitelist отображения ошибок**

В `reviewer/mcp/service.py` расширить импорт:

```python
from reviewer.retrieval.retriever import ContextPack, SearchUnavailableError
```

Заменить catch в `search_codebase`:

```python
try:
    pack = self.components.retriever.search_base(
        repo,
        query,
        limits=cl.search_codebase,
        hops=cl.graph.hops,
        ceiling_override=top_k,
        branch=resolved,
        include_tests=include_tests,
    )
except SearchUnavailableError as error:
    log.warning("search_codebase: обязательный компонент недоступен", exc_info=True)
    messages = {
        "embeddings": "(поиск недоступен — embeddings)",
        "storage": "(поиск недоступен — storage)",
    }
    return messages.get(error.component, "(поиск недоступен — внутренняя ошибка)")
except Exception:
    log.warning("search_codebase: внутренний сбой поиска", exc_info=True)
    return "(поиск недоступен — внутренняя ошибка)"
return pack.as_context(line_numbers=True) or "(ничего не найдено)"
```

Whitelist обязателен: не интерполировать `error.component`, `str(error)` или `__cause__` в
MCP-ответ. Словарь создаётся только на error path и не влияет на успешный поиск.

- [ ] **Step 4: Запустить весь MCP service test file**

Run:

```bash
.venv/bin/pytest -q tests/mcp/test_service.py
```

Expected: PASS, включая делегирование, line-numbered rendering, empty result, whitelist,
unknown category и generic exception.

- [ ] **Step 5: Проверить стиль и закоммитить Task 2**

Run:

```bash
.venv/bin/ruff check reviewer/mcp/service.py tests/mcp/test_service.py
git diff --check
git add reviewer/mcp/service.py tests/mcp/test_service.py
git commit -m "fix(mcp): различать пустой и недоступный поиск"
```

Expected: ruff и `git diff --check` завершаются с exit 0; коммит не содержит skill-файлы.

---

### Task 3: Fallback глобального `reviewer_ask`

**Files:**
- Modify: `plugin/skills/ask/SKILL.md:55-62,92-99`
- Modify: `tests/skills/test_ask_uses_summaries.py:53-57`

**Interfaces:**
- Consumes: публичный prefix `(поиск недоступен` из Task 2.
- Produces: детерминированное правило skill — unavailable-note немедленно запускает локальный `Grep`/`Glob`/`Read` и запрещает повторный `search_codebase`.

- [ ] **Step 1: Написать падающий guard-тест skill-контракта**

Добавить в `tests/skills/test_ask_uses_summaries.py`:

```python
def test_ask_falls_back_without_retry_when_search_unavailable():
    text = ASK.read_text(encoding="utf-8")

    assert "starts with `(поиск недоступен`" in text
    assert "Do not re-call `search_codebase`" in text
    assert "`Grep`/`Glob`/`Read`" in text
```

- [ ] **Step 2: Запустить guard-тест и подтвердить красную фазу**

Run:

```bash
.venv/bin/pytest -q \
  tests/skills/test_ask_uses_summaries.py::test_ask_falls_back_without_retry_when_search_unavailable
```

Expected: FAIL на первых двух assertions — текущий skill перечисляет только
`(ничего не найдено)` и `(граф недоступен)`.

- [ ] **Step 3: Обновить Search и Fallback инструкции**

В шаге Search `plugin/skills/ask/SKILL.md` заменить одноусловную фразу на:

```markdown
If the result is `(ничего не найдено)` or starts with `(поиск недоступен`, go to
Fallback. For `(поиск недоступен...)`, do not re-call `search_codebase`: the
failure is infrastructural, not a relevance signal.
```

В секции Fallback расширить перечень:

```markdown
If the reviewer MCP server is unreachable or returns `(ничего не найдено)` /
`(поиск недоступен...)` / `(граф недоступен)` (Postgres/embeddings/Neo4j/index
down), degrade gracefully:
- Do not re-call `search_codebase` after an unavailable response.
- Use the harness `Grep`/`Glob`/`Read` over the local clone to locate and confirm code.
- Tell the user (in Russian) that semantic/graph search was unavailable and the answer comes from
  a lexical search, so it may be less complete.
```

Не менять `solve-task`: его существующий общий контракт для unavailable-note уже покрывает новые
сообщения и не требует перечисления категорий.

- [ ] **Step 4: Запустить skill guard-тесты**

Run:

```bash
.venv/bin/pytest -q tests/skills/test_ask_uses_summaries.py tests/skills/test_solve_task_brief.py
```

Expected: PASS; `reviewer_ask` фиксирует unavailable fallback, а контракт `solve-task`
остаётся без регрессии.

- [ ] **Step 5: Запустить объединённую целевую проверку PRI-174**

Run:

```bash
.venv/bin/pytest -q \
  tests/retrieval/test_search_base.py \
  tests/mcp/test_service.py \
  tests/skills/test_ask_uses_summaries.py \
  tests/skills/test_solve_task_brief.py
```

Expected: PASS без skipped/failures в unit-наборе PRI-174.

- [ ] **Step 6: Запустить полную unit-проверку и lint**

Run:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
git diff --check
```

Expected: все unit-тесты проходят; ruff и whitespace check завершаются с exit 0. Integration
тесты не запускаются, потому что обычный pytest исключает marker `integration` по конфигурации
проекта.

- [ ] **Step 7: Закоммитить Task 3**

Run:

```bash
git add plugin/skills/ask/SKILL.md tests/skills/test_ask_uses_summaries.py
git commit -m "fix(skills): включить fallback при недоступном поиске"
```

Expected: коммит содержит только `reviewer_ask` и его guard-тест. После коммита `git status
--short` может показывать только пользовательские изменения, существовавшие до реализации; не
добавлять их в коммиты PRI-174.

## Final Verification Checklist

- [ ] `git log -5 --oneline` показывает design/plan-коммиты и три независимых implementation-коммита PRI-174.
- [ ] `git diff HEAD~3..HEAD -- reviewer/retrieval/retriever.py reviewer/mcp/service.py plugin/skills/ask/SKILL.md` не содержит изменений ranking/cliff/graph fallback.
- [ ] В публичных ответах отсутствуют `str(error)`, `error.__cause__` и динамическая интерполяция неизвестной категории.
- [ ] `.venv/bin/pytest -q` завершился с нулём failures.
- [ ] `.venv/bin/ruff check .` завершился с exit 0.
- [ ] После создания PR использовать `rag-reviewer:reviewer_finish-task` для ссылки PR↔PRI-174 и перевода задачи в done target.
