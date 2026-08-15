# `prepare_task_context` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Свернуть детерминированный preflight и сбор контекста скилла solve-task в один session-less MCP-тул `prepare_task_context`, разгрузив промпт скилла и доказав эффект замером во взвешенных единицах.

**Architecture:** Сначала офлайн-анализатор транскриптов даёт разбивку взвешенной цены по под-шагам solve-task и baseline «до». Затем чистый модуль сборки payload (`reviewer/mcp/task_context.py`) агрегирует уже существующие session-less операции с посекционным fail-open, тонкий метод `MCPReviewService` и регистрация тула отдают его наружу. Наконец `SKILL.md` ужимается до ядра с выносом деталей в `references/`, а guard-тесты переводятся с грепа сырого файла на собранный `assemble()` текст.

**Tech Stack:** Python 3.11+, FastMCP, pytest, pgvector/ParadeDB, Neo4j, stdlib-only офлайн-анализатор.

**Spec:** `docs/superpowers/specs/2026-08-15-pri-248-prepare-task-context-design.md`

## Global Constraints

- Язык проекта — русский: комментарии, докстринги, сообщения CLI. Докстринги MCP-тулов в `mcp_server.py` — английские (это API-контракт для LLM-клиентов), как у соседних тулов.
- Коммиты: Conventional Commits на русском, без self-attribution (`feat(mcp): …`, `fix(skills): …`).
- Unit-тестам запрещены внешние и localhost-сокеты. Любой тест с реальной сетью обязан иметь `@pytest.mark.integration`.
- Прогон unit: `.venv/bin/pytest -q`. Прогон одного файла: `.venv/bin/pytest -q tests/path/test_x.py`.
- Веса бакетов — единственный тариф проекта, копировать значения нельзя, только импортировать: `WEIGHTS = {"fresh_in": 1.0, "output": 5.0, "cache_write": 1.25, "cache_read": 0.1}` из `eval/solve_task_metrics/cost.py`.
- Существующие session-less тулы (`get_task`, `search_codebase`, `get_subsystem_summaries`, `get_task_context`, `search_tasks`, графовые, `get_pr_diff`) НЕ удаляются и НЕ меняют сигнатуры: `prepare_task_context` — агрегатор поверх них.
- Любая правка содержимого под `plugin/` требует пересборки codex-манифестов (Task 6), иначе install-тесты краснеют.
- `reviewer/mcp/service.py` уже 3452 строки — новая логика идёт в отдельный модуль, в `service.py` остаётся только тонкий делегирующий метод.

---

### Task 1: Офлайн-анализатор расхода по под-шагам + baseline «до»

Фаза 1 спеки. Отвечает на вопрос PRI-246 «какая доля взвешенной цены приходится на преflight и сбор контекста» ретроспективно, по уже накопленным транскриптам, до единой строки реализации тула.

**Files:**
- Create: `eval/solve_task_metrics/steps.py`
- Modify: `eval/solve_task_metrics/__main__.py` (добавить подкоманду `steps`)
- Test: `tests/eval/test_solve_task_steps.py`

**Interfaces:**
- Consumes: `eval.solve_task_metrics.cost.WEIGHTS`, `cost.weighted`; `eval.solve_task_metrics.endtoend.find_windows`, `endtoend._read_jsonl`, `endtoend.BUCKET_KEYS`.
- Produces:
  - `STEP_TOOLS: dict[str, str]` — отображение имени тула на под-шаг.
  - `classify_turn(line: dict) -> str` — под-шаг одного assistant-хода (`"preflight" | "gather" | "brief" | "other"`).
  - `attribute_window(lines: list, start: int, end: int) -> dict[str, dict[str, float]]` — бакеты по под-шагам внутри окна.
  - `weighted_shares(by_step: dict) -> dict[str, float]` — доли взвешенной цены по под-шагам, сумма ≈ 1.0.
  - `scan_steps(root: pathlib.Path) -> dict[str, dict]` — по ключу задачи агрегированные бакеты по под-шагам.

- [ ] **Step 1: Написать падающий тест классификации хода**

Создать `tests/eval/test_solve_task_steps.py`:

```python
"""Офлайн-атрибуция расхода solve-task по под-шагам (PRI-248, фаза 1)."""
from eval.solve_task_metrics import steps


def _assistant(tool_names, usage):
    content = [{"type": "tool_use", "name": name, "input": {}} for name in tool_names]
    return {
        "type": "assistant",
        "message": {"model": "claude-sonnet-5", "content": content, "usage": usage},
    }


def test_classify_preflight_tools():
    line = _assistant(["Bash"], {"output_tokens": 1})
    line["message"]["content"][0]["input"] = {"command": "uvx --from rag-reviewer reviewer status . --json"}
    assert steps.classify_turn(line) == "preflight"


def test_classify_gather_tools():
    assert steps.classify_turn(_assistant(["mcp__reviewer__search_codebase"], {})) == "gather"
    assert steps.classify_turn(_assistant(["mcp__reviewer__get_subsystem_summaries"], {})) == "gather"


def test_classify_brief_write():
    line = _assistant(["Write"], {})
    line["message"]["content"][0]["input"] = {
        "file_path": "/repo/docs/superpowers/briefs/2026-08-15-PRI-248-x.md"
    }
    assert steps.classify_turn(line) == "brief"


def test_classify_unknown_is_other():
    assert steps.classify_turn(_assistant(["Glob"], {})) == "other"


def test_text_only_turn_is_other():
    line = {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}], "usage": {}}}
    assert steps.classify_turn(line) == "other"
```

- [ ] **Step 2: Прогнать тест, убедиться что падает**

Run: `.venv/bin/pytest -q tests/eval/test_solve_task_steps.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'eval.solve_task_metrics.steps'`

- [ ] **Step 3: Реализовать классификацию**

Создать `eval/solve_task_metrics/steps.py`:

```python
"""Атрибуция взвешенной цены solve-task по под-шагам (офлайн, PRI-248).

Спайк PRI-246 измерил этап целиком, но не разбил его на шаги, поэтому не мог
доказать, где именно сидит расход. Атрибуция — чистая функция от транскрипта:
assistant-ход относится к под-шагу по тому, какой инструмент он вызвал.
Считается ретроспективно по уже накопленным транскриптам — ждать новых
прогонов не нужно.
"""
from __future__ import annotations

import pathlib

from . import endtoend
from .briefs import BUCKET_KEYS
from .cost import weighted

# Тул → под-шаг. Ключ — имя как оно приходит в транскрипте; MCP-тулы приходят
# с префиксом сервера, поэтому сопоставление идёт по суффиксу после '__'.
STEP_TOOLS = {
    "sync_board": "preflight",
    "get_board_config": "preflight",
    "get_board_targets": "preflight",
    "get_task": "gather",
    "get_task_context": "gather",
    "search_tasks": "gather",
    "search_codebase": "gather",
    "get_subsystem_summaries": "gather",
    "related_symbols": "gather",
    "callers": "gather",
    "definition": "gather",
    "implementations": "gather",
    "get_pr_diff": "gather",
}

BRIEFS_MARKER = "docs/superpowers/briefs/"
STATUS_MARKER = "reviewer status"
STEPS = ("preflight", "gather", "brief", "other")


def _tool_calls(line: dict) -> list:
    content = (line.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]


def _short_name(name: str) -> str:
    return name.rsplit("__", 1)[-1] if name else ""


def classify_turn(line: dict) -> str:
    """Под-шаг assistant-хода по инструментам, которые он вызвал.

    Ход без tool_use относится к "other": текстовый ход не принадлежит
    ни одному под-шагу однозначно. Первый распознанный вызов решает.
    """
    for block in _tool_calls(line):
        name = block.get("name") or ""
        payload = block.get("input") or {}
        short = _short_name(name)
        if short in STEP_TOOLS:
            return STEP_TOOLS[short]
        if name == "Bash" and STATUS_MARKER in str(payload.get("command") or ""):
            return "preflight"
        if name in ("Write", "Edit") and BRIEFS_MARKER in str(payload.get("file_path") or ""):
            return "brief"
    return "other"


def attribute_window(lines: list, start: int, end: int) -> dict:
    """Бакеты токенов по под-шагам для assistant-ходов окна (start, end)."""
    by_step = {step: {key: 0.0 for key in BUCKET_KEYS} for step in STEPS}
    for line in lines[start + 1 : end]:
        if line.get("type") != "assistant":
            continue
        usage = (line.get("message") or {}).get("usage") or {}
        bucket = by_step[classify_turn(line)]
        bucket["fresh_in"] += float(usage.get("input_tokens") or 0)
        bucket["output"] += float(usage.get("output_tokens") or 0)
        bucket["cache_write"] += float(usage.get("cache_creation_input_tokens") or 0)
        bucket["cache_read"] += float(usage.get("cache_read_input_tokens") or 0)
    return by_step


def weighted_shares(by_step: dict) -> dict:
    """Доли взвешенной цены по под-шагам. Нулевая цена → нулевые доли."""
    values = {step: weighted(buckets) for step, buckets in by_step.items()}
    total = sum(values.values())
    if not total:
        return {step: 0.0 for step in by_step}
    return {step: value / total for step, value in values.items()}


def scan_steps(root: pathlib.Path) -> dict:
    """Разбивка по под-шагам для каждой задачи из транскриптов под root."""
    result: dict = {}
    if not root.exists():
        return result
    for path in sorted(root.glob("*/*.jsonl")):
        lines = endtoend._read_jsonl(path)
        if not lines:
            continue
        for key, start, end in endtoend.find_windows(lines):
            if not key:
                continue
            slot = result.setdefault(
                key, {step: {b: 0.0 for b in BUCKET_KEYS} for step in STEPS})
            for step, buckets in attribute_window(lines, start, end).items():
                for bucket_key in BUCKET_KEYS:
                    slot[step][bucket_key] += buckets[bucket_key]
    return result
```

- [ ] **Step 4: Прогнать тест классификации, убедиться что проходит**

Run: `.venv/bin/pytest -q tests/eval/test_solve_task_steps.py`
Expected: PASS (5 passed)

- [ ] **Step 5: Написать падающий тест атрибуции и долей**

Дописать в `tests/eval/test_solve_task_steps.py`:

```python
def test_attribute_window_splits_buckets_by_step():
    lines = [
        {"type": "user", "message": {"content": "Base directory for this skill: skills/solve-task PRI-1"}},
        _assistant(["mcp__reviewer__sync_board"], {"cache_creation_input_tokens": 100}),
        _assistant(["mcp__reviewer__search_codebase"], {"cache_creation_input_tokens": 300}),
    ]
    by_step = steps.attribute_window(lines, 0, len(lines))
    assert by_step["preflight"]["cache_write"] == 100.0
    assert by_step["gather"]["cache_write"] == 300.0
    assert by_step["brief"]["cache_write"] == 0.0


def test_weighted_shares_sum_to_one():
    by_step = steps.attribute_window(
        [
            {"type": "user", "message": {"content": ""}},
            _assistant(["mcp__reviewer__sync_board"], {"output_tokens": 10}),
            _assistant(["mcp__reviewer__search_tasks"], {"output_tokens": 30}),
        ],
        0,
        3,
    )
    shares = steps.weighted_shares(by_step)
    assert abs(sum(shares.values()) - 1.0) < 1e-9
    assert abs(shares["preflight"] - 0.25) < 1e-9


def test_weighted_shares_zero_cost_is_zero_not_crash():
    empty = {s: {"fresh_in": 0.0, "output": 0.0, "cache_write": 0.0, "cache_read": 0.0}
             for s in steps.STEPS}
    assert steps.weighted_shares(empty) == {s: 0.0 for s in steps.STEPS}


def test_scan_steps_missing_root_is_empty(tmp_path):
    assert steps.scan_steps(tmp_path / "nope") == {}
```

- [ ] **Step 6: Прогнать, убедиться что проходит**

Run: `.venv/bin/pytest -q tests/eval/test_solve_task_steps.py`
Expected: PASS (9 passed)

- [ ] **Step 7: Добавить подкоманду `steps` в CLI харнесса**

В `eval/solve_task_metrics/__main__.py` добавить импорт `steps` в строку `from . import endtoend, forecast, ground_truth, history, report, snapshot as snapshot_mod` (получится `from . import endtoend, forecast, ground_truth, history, report, snapshot as snapshot_mod, steps`) и функцию-команду:

```python
def cmd_steps(_args) -> int:
    """Разбивка взвешенной цены solve-task по под-шагам (baseline PRI-248)."""
    per_task = steps.scan_steps(TRANSCRIPTS_ROOT)
    if not per_task:
        print("Транскриптов solve-task не найдено")
        return 1
    totals = {s: {k: 0.0 for k in steps.BUCKET_KEYS} for s in steps.STEPS}
    for by_step in per_task.values():
        for step, buckets in by_step.items():
            for key in steps.BUCKET_KEYS:
                totals[step][key] += buckets[key]
    shares = steps.weighted_shares(totals)
    print(f"Задач измерено: {len(per_task)}")
    for step in steps.STEPS:
        print(f"  {step:<10} {shares[step] * 100:5.1f}%  "
              f"(cache_write {totals[step]['cache_write']:.0f})")
    consolidated = shares["preflight"] + shares["gather"]
    print(f"Доля преflight+gather: {consolidated * 100:.1f}%")
    return 0
```

Зарегистрировать её рядом с существующими подкомандами в парсере: найти блок `subparsers.add_parser(...)` и добавить по тому же образцу парсер `"steps"` с `set_defaults(func=cmd_steps)`.

- [ ] **Step 8: Прогнать анализатор на реальном корпусе и записать baseline**

Run: `.venv/bin/python -m eval.solve_task_metrics steps`
Expected: печатает число измеренных задач и доли по под-шагам.

Записать полученные числа в конец `eval/pri246_report.md` новой секцией `## Baseline по под-шагам (PRI-248)`: число задач, доли `preflight` / `gather` / `brief` / `other`, суммарная доля `preflight+gather`.

- [ ] **Step 9: Точка принятия решения по порогу спеки**

Спека фиксирует порог: если суммарная доля `preflight + gather` во взвешенной цене **< 15 %**, консолидация не окупается в заявленном объёме.

- Доля ≥ 15 % → продолжать план с Task 2 без изменений.
- Доля < 15 % → ОСТАНОВИТЬСЯ, записать факт и числа в `eval/pri246_report.md`, сообщить пользователю и дождаться его решения о сокращении объёма. Не начинать Task 2 самостоятельно.

- [ ] **Step 10: Коммит**

```bash
git add eval/solve_task_metrics/steps.py eval/solve_task_metrics/__main__.py tests/eval/test_solve_task_steps.py eval/pri246_report.md
git commit -m "feat(eval): атрибуция расхода solve-task по под-шагам и baseline (PRI-248)"
```

---

### Task 2: Модуль сборки payload `prepare_task_context`

Вся логика агрегации — в отдельном модуле на чистых зависимостях, чтобы `service.py` (3452 строки) не рос и чтобы fail-open-таблица тестировалась на фейках без Postgres/Neo4j/сети.

**Files:**
- Create: `reviewer/mcp/task_context.py`
- Test: `tests/mcp/test_prepare_task_context.py`

**Interfaces:**
- Consumes: ничего из Task 1.
- Produces:
  - `SECTIONS: tuple[str, ...]` — имена секций payload.
  - `Gap` — namedtuple-подобный dict-конструктор `gap(section: str, reason: str) -> dict`.
  - `build_task_context(deps, *, repo, key, branch, warm_board) -> dict` — где `deps` — объект с методами-провайдерами (см. код), что и позволяет тестировать на фейке.

- [ ] **Step 1: Написать падающие тесты happy path и формы payload**

Создать `tests/mcp/test_prepare_task_context.py`:

```python
"""prepare_task_context: форма payload и посекционный fail-open (PRI-248)."""
import pytest

from reviewer.mcp import task_context


class FakeDeps:
    """Фейковые провайдеры секций. Любое поле-исключение имитирует сбой источника."""

    def __init__(self, **overrides):
        self.calls = []
        self._overrides = overrides

    def _result(self, name, default):
        value = self._overrides.get(name, default)
        self.calls.append(name)
        if isinstance(value, Exception):
            raise value
        return value

    def preflight(self, repo, branch):
        return self._result("preflight", {
            "branch": branch, "indexed_sha": "abc", "drift": 0,
            "summaries": 40, "chunks": 7110, "graph_nodes": 7362})

    def task_board(self, repo, branch):
        return self._result("task_board", {
            "type": "yougile", "project": "PRI", "key_pattern": r"PRI-\d+",
            "create_target": None, "done_target": "Готово", "options": {}})

    def warm_board(self, repo, branch):
        return self._result("warm_board", {"enumerated": 109, "changed": 0})

    def task(self, key, project):
        return self._result("task", {"key": "ID-302", "title": "T", "description": "D"})

    def linked(self, key, project):
        return self._result("linked", "Task PRI-248\n  Linked tasks: ...")

    def similar(self, query, project):
        return self._result("similar", "1. ID-300 ...")

    def subsystems(self, repo, branch, query):
        return self._result("subsystems", {"summaries": [{"cluster_key": "reviewer/mcp"}]})

    def code(self, repo, branch, query):
        return self._result("code", "reviewer/mcp/service.py#X (service.py:1-10)")

    def test_exemplars(self, repo, branch, query):
        return self._result("test_exemplars", "tests/mcp/test_x.py#y")


def test_payload_has_all_sections():
    payload = task_context.build_task_context(
        FakeDeps(), repo="o/n", key="PRI-248", branch="dev", warm_board=True)
    for section in task_context.SECTIONS:
        assert section in payload, section


def test_happy_path_has_no_gaps():
    payload = task_context.build_task_context(
        FakeDeps(), repo="o/n", key="PRI-248", branch="dev", warm_board=True)
    assert payload["gaps"] == []
    assert payload["preflight"]["drift"] == 0
    assert payload["task"]["key"] == "ID-302"
    assert payload["related"]["linked"].startswith("Task PRI-248")
    assert payload["related"]["similar"].startswith("1. ID-300")


def test_warm_board_false_skips_sync():
    deps = FakeDeps()
    task_context.build_task_context(
        deps, repo="o/n", key="PRI-248", branch="dev", warm_board=False)
    assert "warm_board" not in deps.calls


def test_related_is_not_deduped_by_the_tool():
    """Дедуп linked ∪ similar — суждение LLM, тул отдаёт обе выдачи как есть."""
    payload = task_context.build_task_context(
        FakeDeps(), repo="o/n", key="PRI-248", branch="dev", warm_board=True)
    assert set(payload["related"]) == {"linked", "similar"}
```

- [ ] **Step 2: Прогнать, убедиться что падает**

Run: `.venv/bin/pytest -q tests/mcp/test_prepare_task_context.py`
Expected: FAIL — `ImportError: cannot import name 'task_context' from 'reviewer.mcp'`

- [ ] **Step 3: Реализовать модуль**

Создать `reviewer/mcp/task_context.py`:

```python
"""Сборка единого контекста задачи для скилла solve-task (PRI-248).

Свёртка детерминированной части скилла: преflight (свежесть индекса, теплота
сводок, разрешённая доска) и сбор контекста (задача, связанные и похожие
задачи, подсистемы, релевантный код, тест-образцы) — за один вызов вместо
8-12 тул-раундов. За LLM остаётся relevance-фильтр и сборка брифа.

Модуль намеренно не знает про Settings и компоненты: источники секций
приходят объектом-провайдером, поэтому вся fail-open-таблица тестируется
без Postgres, Neo4j и сети.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

SECTIONS = (
    "preflight", "task_board", "task", "related", "subsystems",
    "code", "test_exemplars", "gaps", "warnings",
)


def gap(section: str, reason: str) -> dict:
    """Структурная запись о пробеле: секция и причина, без секретов и трейсбека."""
    return {"section": section, "reason": reason}


def _safe(payload: dict, section: str, produce, default, reason: str):
    """Собрать секцию fail-open: сбой → default + запись в gaps."""
    try:
        return produce()
    except Exception:  # noqa: BLE001 — источник секции недоступен, это штатный случай
        log.warning("prepare_task_context: секция %s недоступна", section, exc_info=True)
        payload["gaps"].append(gap(section, reason))
        return default


def _query(task: dict | None, key: str) -> str:
    """Запрос ретрива: заголовок и начало описания задачи либо сам ключ.

    Board-less вход (свободный текст вместо ключа) остаётся рабочим: без
    задачи в сторе запросом становится сама формулировка пользователя.
    """
    if not task:
        return key
    title = str(task.get("title") or "")
    description = str(task.get("description") or "")
    head = "\n".join(description.splitlines()[:8])
    return f"{title}. {head}".strip(". ").strip() or key


def _test_query(task: dict | None, key: str) -> str:
    """Целевой запрос про тесты области, а не код-запрос с флагом."""
    return f"как тестируется: {_query(task, key)}"


def build_task_context(deps, *, repo: str, key: str, branch: str,
                       warm_board: bool = True) -> dict:
    """Единый payload контекста задачи. Ни один сбой секции не прерывает сборку."""
    payload: dict = {section: None for section in SECTIONS}
    payload["gaps"] = []
    payload["warnings"] = []

    payload["preflight"] = _safe(
        payload, "preflight", lambda: deps.preflight(repo, branch), None,
        "статус индекса недоступен")
    board = _safe(
        payload, "task_board", lambda: deps.task_board(repo, branch), None,
        "конфиг доски не разрешён")
    payload["task_board"] = board
    project = (board or {}).get("project")

    if warm_board and board:
        result = _safe(
            payload, "warm_board", lambda: deps.warm_board(repo, branch), None,
            "прогрев доски не выполнен")
        if result is not None:
            payload["warnings"].append({"warm_board": result})
    elif warm_board and not board:
        payload["gaps"].append(gap("warm_board", "доска не настроена"))

    task = _safe(payload, "task", lambda: deps.task(key, project), None,
                 "задача не прочитана из стора")
    payload["task"] = task
    if task is None and not any(g["section"] == "task" for g in payload["gaps"]):
        payload["gaps"].append(gap("task", "задачи нет в сторе"))

    query = _query(task, key)
    payload["related"] = {
        "linked": _safe(payload, "related.linked",
                        lambda: deps.linked(key, project), "", "граф задач недоступен"),
        "similar": _safe(payload, "related.similar",
                         lambda: deps.similar(query, project), "", "корпус задач недоступен"),
    }
    payload["subsystems"] = _safe(
        payload, "subsystems", lambda: deps.subsystems(repo, branch, query), None,
        "сводки подсистем недоступны")
    payload["code"] = _safe(
        payload, "code", lambda: deps.code(repo, branch, query), "",
        "поиск по коду недоступен")
    payload["test_exemplars"] = _safe(
        payload, "test_exemplars",
        lambda: deps.test_exemplars(repo, branch, _test_query(task, key)), "",
        "поиск по тестам недоступен")
    return payload
```

- [ ] **Step 4: Прогнать тесты, убедиться что проходят**

Run: `.venv/bin/pytest -q tests/mcp/test_prepare_task_context.py`
Expected: PASS (4 passed)

- [ ] **Step 5: Написать падающие тесты на каждую строку fail-open-таблицы спеки**

Дописать в `tests/mcp/test_prepare_task_context.py`:

```python
def _gap_sections(payload):
    return {g["section"] for g in payload["gaps"]}


def test_board_disabled_keeps_task_from_store():
    payload = task_context.build_task_context(
        FakeDeps(task_board=None), repo="o/n", key="PRI-248", branch="dev", warm_board=True)
    assert payload["task_board"] is None
    assert payload["task"]["key"] == "ID-302"
    assert "warm_board" in _gap_sections(payload)


def test_board_unreachable_still_builds_payload():
    payload = task_context.build_task_context(
        FakeDeps(warm_board=RuntimeError("401")), repo="o/n", key="PRI-248",
        branch="dev", warm_board=True)
    assert "warm_board" in _gap_sections(payload)
    assert payload["code"]


def test_postgres_down_empties_retrieval_sections():
    deps = FakeDeps(code=RuntimeError("no pg"), test_exemplars=RuntimeError("no pg"),
                    similar=RuntimeError("no pg"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-248", branch="dev", warm_board=False)
    assert payload["code"] == ""
    assert payload["test_exemplars"] == ""
    assert payload["related"]["similar"] == ""
    assert {"code", "test_exemplars", "related.similar"} <= _gap_sections(payload)


def test_neo4j_down_empties_linked_only():
    payload = task_context.build_task_context(
        FakeDeps(linked=RuntimeError("no neo4j")), repo="o/n", key="PRI-248",
        branch="dev", warm_board=False)
    assert payload["related"]["linked"] == ""
    assert payload["related"]["similar"]
    assert "related.linked" in _gap_sections(payload)


def test_no_index_marks_gap_and_keeps_going():
    payload = task_context.build_task_context(
        FakeDeps(preflight=RuntimeError("no index")), repo="o/n", key="PRI-248",
        branch="dev", warm_board=False)
    assert payload["preflight"] is None
    assert "preflight" in _gap_sections(payload)
    assert payload["code"]


def test_no_summaries_marks_gap():
    payload = task_context.build_task_context(
        FakeDeps(subsystems=RuntimeError("нет сводок")), repo="o/n", key="PRI-248",
        branch="dev", warm_board=False)
    assert payload["subsystems"] is None
    assert "subsystems" in _gap_sections(payload)


def test_task_missing_is_a_gap_not_an_error():
    payload = task_context.build_task_context(
        FakeDeps(task=None), repo="свободный текст задачи", key="свободный текст задачи",
        branch="dev", warm_board=False)
    assert payload["task"] is None
    assert "task" in _gap_sections(payload)


def test_board_less_query_falls_back_to_user_formulation():
    captured = {}

    class CapturingDeps(FakeDeps):
        def code(self, repo, branch, query):
            captured["query"] = query
            return "snippet"

    task_context.build_task_context(
        CapturingDeps(task=None), repo="o/n", key="добавить logout endpoint",
        branch="dev", warm_board=False)
    assert captured["query"] == "добавить logout endpoint"


def test_every_failure_still_returns_all_sections():
    deps = FakeDeps(preflight=RuntimeError(), task_board=RuntimeError(),
                    task=RuntimeError(), linked=RuntimeError(), similar=RuntimeError(),
                    subsystems=RuntimeError(), code=RuntimeError(),
                    test_exemplars=RuntimeError())
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-248", branch="dev", warm_board=True)
    for section in task_context.SECTIONS:
        assert section in payload
    assert len(payload["gaps"]) >= 8
```

- [ ] **Step 6: Прогнать, убедиться что проходят**

Run: `.venv/bin/pytest -q tests/mcp/test_prepare_task_context.py`
Expected: PASS (13 passed)

- [ ] **Step 7: Коммит**

```bash
git add reviewer/mcp/task_context.py tests/mcp/test_prepare_task_context.py
git commit -m "feat(mcp): сборка единого контекста задачи с посекционным fail-open (PRI-248)"
```

---

### Task 3: Сервисный метод и регистрация MCP-тула

Подключение модуля Task 2 к реальным компонентам и выставление тула наружу.

**Files:**
- Modify: `reviewer/mcp/service.py` (добавить метод `prepare_task_context` рядом с прочими session-less тулами, после `search_codebase` на строке 1768)
- Modify: `reviewer/entrypoints/mcp_server.py` (регистрация рядом с `search_codebase`, строка ~336; обновить число тулов в докстринге `create_server`)
- Test: `tests/mcp/test_prepare_task_context.py` (дописать), `tests/mcp/test_schemas.py` (если там пинится список тулов — обновить)

**Interfaces:**
- Consumes: `reviewer.mcp.task_context.build_task_context` из Task 2.
- Produces: `MCPReviewService.prepare_task_context(repo, key, branch=None, path=None, warm_board=True) -> dict` — публичный контракт тула.

- [ ] **Step 1: Написать падающий тест сервисного метода**

Дописать в `tests/mcp/test_prepare_task_context.py`:

```python
def test_service_method_resolves_branch_and_delegates(monkeypatch):
    """Метод сервиса резолвит (repo, branch) и отдаёт payload модуля."""
    from reviewer.mcp import service as service_mod

    captured = {}

    def fake_build(deps, *, repo, key, branch, warm_board):
        captured.update(repo=repo, key=key, branch=branch, warm_board=warm_board)
        return {"preflight": {"branch": branch}, "gaps": []}

    monkeypatch.setattr(service_mod.task_context, "build_task_context", fake_build)

    svc = service_mod.MCPReviewService.__new__(service_mod.MCPReviewService)
    monkeypatch.setattr(
        service_mod.MCPReviewService, "_resolve_repo_branch",
        lambda self, repo, branch: ("owner/name", "dev"))
    payload = svc.prepare_task_context("owner/name", "PRI-248", branch="dev")
    assert captured["repo"] == "owner/name"
    assert captured["branch"] == "dev"
    assert payload["preflight"]["branch"] == "dev"


def test_service_method_returns_gap_on_bad_repo(monkeypatch):
    """Нерезолвящийся repo/branch — не исключение, а payload с пробелом."""
    from reviewer.mcp import service as service_mod

    svc = service_mod.MCPReviewService.__new__(service_mod.MCPReviewService)
    monkeypatch.setattr(
        service_mod.MCPReviewService, "_resolve_repo_branch",
        lambda self, repo, branch: "(repo не задан: передайте repo или задайте DEFAULT_REPO)")
    payload = svc.prepare_task_context("", "PRI-248")
    assert payload["task_board"] is None
    assert any(g["section"] == "repo" for g in payload["gaps"])
```

- [ ] **Step 2: Прогнать, убедиться что падает**

Run: `.venv/bin/pytest -q tests/mcp/test_prepare_task_context.py -k service`
Expected: FAIL — `AttributeError: 'MCPReviewService' object has no attribute 'prepare_task_context'`

- [ ] **Step 3: Реализовать метод сервиса**

В `reviewer/mcp/service.py` добавить импорт рядом с существующим `from reviewer.config.task_board import ...` (строка 24):

```python
from reviewer.mcp import task_context
```

и метод сразу после `search_codebase` (после строки 1794):

```python
    def prepare_task_context(self, repo: str, key: str, branch: str | None = None,
                             path: str | None = None,
                             warm_board: bool = True) -> dict:
        """Единый контекст задачи для solve-task: преflight + сбор, один вызов.

        Свёртка 8-12 детерминированных тул-раундов скилла. Ни один сбой
        источника не прерывает сборку: недоступная доска, лежачий Neo4j,
        отсутствующий индекс дают частичный payload с записями в `gaps`.
        path — необязательный override пути к клону; по умолчанию клон
        резолвится из таблицы `repo_clone` (PRI-235), поэтому клиенту не нужно
        запускать `reviewer status` отдельным процессом.
        """
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            payload = {section: None for section in task_context.SECTIONS}
            payload["gaps"] = [task_context.gap("repo", rb.strip("()"))]
            payload["warnings"] = []
            return payload
        normalized_repo, resolved = rb
        deps = _TaskContextDeps(self, path)
        return task_context.build_task_context(
            deps, repo=normalized_repo, key=key, branch=resolved,
            warm_board=warm_board)
```

и класс-провайдер в конце файла:

```python
class _TaskContextDeps:
    """Источники секций prepare_task_context поверх живых компонентов сервиса.

    Отдельный класс, а не замыкания: он и есть та поверхность, которую
    подменяет фейк в юнит-тестах модуля сборки.
    """

    def __init__(self, service: "MCPReviewService", path: str | None):
        self._service = service
        self._path = path

    def _clone_path(self, repo: str) -> str:
        return self._path or self._service._repo_clone_path(repo) or ""

    def preflight(self, repo: str, branch: str) -> dict:
        from reviewer.services.status import build_status_report
        components = self._service.components
        report = build_status_report(
            components.store, components.graph, repo, [branch],
            self._clone_path(repo),
            summary_store=getattr(components, "summary_store", None))
        status = report.branches[0]
        return {
            "branch": status.branch,
            "indexed_sha": status.indexed_sha,
            "drift": status.drift,
            "summaries": status.summaries,
            "chunks": status.chunks,
            "graph_nodes": status.graph_nodes,
        }

    def task_board(self, repo: str, branch: str) -> dict | None:
        policy, _meta = self._service._resolve_policy(repo, branch)
        board = getattr(policy, "task_board", None)
        if not board:
            return None
        return dict(board)

    def warm_board(self, repo: str, branch: str) -> dict:
        return self._service.sync_board(repo=repo, branch=branch, purge_orphaned=False)

    def task(self, key: str, project: str | None) -> dict | None:
        return self._service.get_task(key, project=project)

    def linked(self, key: str, project: str | None) -> str:
        return self._service.get_task_context(key, project=project)

    def similar(self, query: str, project: str | None) -> str:
        return self._service.search_tasks(query, project=project)

    def subsystems(self, repo: str, branch: str, query: str) -> dict:
        return self._service.get_subsystem_summaries(repo, branch, None, query, None)

    def code(self, repo: str, branch: str, query: str) -> str:
        return self._service.search_codebase(repo, query, None, branch, False)

    def test_exemplars(self, repo: str, branch: str, query: str) -> str:
        return self._service.search_codebase(repo, query, None, branch, True)
```

- [ ] **Step 4: Прогнать тесты сервиса, убедиться что проходят**

Run: `.venv/bin/pytest -q tests/mcp/test_prepare_task_context.py`
Expected: PASS (15 passed)

- [ ] **Step 5: Зарегистрировать тул в MCP-сервере**

В `reviewer/entrypoints/mcp_server.py` после регистрации `search_codebase` (после строки 348) добавить:

```python
    @mcp.tool()
    def prepare_task_context(repo: str, key: str, branch: str | None = None,
                             path: str | None = None,
                             warm_board: bool = True) -> dict:
        """Everything /solve-task needs to write a brief, in one call (no PR session).
        Returns {preflight, task_board, task, related{linked,similar}, subsystems,
        code, test_exemplars, gaps, warnings}. repo is "owner/name"; key is a board
        task key or a free-text description (board-less). branch defaults to the
        primary tracked branch. warm_board=True runs an incremental board sync first.
        Never raises on a missing source: an unreachable board, a down Neo4j or a
        missing index yield a partial payload with entries in `gaps`. Relevance
        filtering and brief assembly stay with the caller."""
        return service.prepare_task_context(repo, key, branch, path, warm_board)
```

Обновить докстринг `create_server` (строка 19): `с 40 тулами` → `с 41 тулом`.

- [ ] **Step 6: Прогнать тесты сервера и схем**

Run: `.venv/bin/pytest -q tests/mcp/`
Expected: PASS. Если тест пинит число или список тулов — обновить его ожидание на 41 тул с добавлением `prepare_task_context` в список.

- [ ] **Step 7: Прогнать полный unit-прогон**

Run: `.venv/bin/pytest -q`
Expected: PASS (кроме заранее известных красных, если такие есть — зафиксировать их до правки и сравнить)

- [ ] **Step 8: Коммит**

```bash
git add reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp/
git commit -m "feat(mcp): тул prepare_task_context для solve-task (PRI-248)"
```

---

### Task 4: Расширить `assemble()` и перевести guard-тесты на собранный промпт

Без этого Task 5 сломает восемь файлов молча. Делается ДО реструктуризации: тесты должны быть зелёными и на текущем `SKILL.md`, и на новом.

**Files:**
- Modify: `tests/skills/test_assembled_prompts.py:5-25` (регексп и резолвер)
- Modify: `tests/skills/test_solve_task_brief.py`, `tests/skills/test_preflight_guardrail.py`, `tests/skills/test_solve_task_modes.py`, `tests/skills/test_ask_uses_summaries.py`, `tests/skills/test_create_task_skill.py`, `tests/skills/test_finish_task_skill.py`, `tests/skills/test_readme_grounding_block.py`, `tests/skills/test_review_pr_store_first.py`

**Interfaces:**
- Consumes: ничего из Task 1-3.
- Produces: `tests.skills.test_assembled_prompts.assemble(rel_path: str) -> str` — рекурсивный резолвер любого пути относительно `plugin/skills/`; импортируется остальными guard-файлами как `from .test_assembled_prompts import assemble`.

- [ ] **Step 1: Написать падающий тест рекурсивного резолвера**

Дописать в `tests/skills/test_assembled_prompts.py`:

```python
def test_assemble_resolves_nested_reference_includes(tmp_path, monkeypatch):
    """Маркер может указывать на references/*.md, который сам включает _common/*.md."""
    import tests.skills.test_assembled_prompts as mod

    root = tmp_path / "skills"
    (root / "_common").mkdir(parents=True)
    (root / "demo" / "references").mkdir(parents=True)
    (root / "_common" / "leaf.md").write_text("ЛИСТ", encoding="utf-8")
    (root / "demo" / "references" / "mid.md").write_text(
        "СЕРЕДИНА\n<!-- include: _common/leaf.md -->\n", encoding="utf-8")
    (root / "demo" / "SKILL.md").write_text(
        "КОРЕНЬ\n<!-- include: demo/references/mid.md -->\n", encoding="utf-8")

    monkeypatch.setattr(mod, "SKILLS_DIR", root)
    out = mod.assemble("demo/SKILL.md")
    assert "КОРЕНЬ" in out and "СЕРЕДИНА" in out and "ЛИСТ" in out


def test_assemble_detects_include_cycle(tmp_path, monkeypatch):
    import pytest

    import tests.skills.test_assembled_prompts as mod

    root = tmp_path / "skills"
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "a.md").write_text("<!-- include: demo/b.md -->", encoding="utf-8")
    (root / "demo" / "b.md").write_text("<!-- include: demo/a.md -->", encoding="utf-8")
    monkeypatch.setattr(mod, "SKILLS_DIR", root)
    with pytest.raises(AssertionError, match="цикл"):
        mod.assemble("demo/a.md")
```

- [ ] **Step 2: Прогнать, убедиться что падает**

Run: `.venv/bin/pytest -q tests/skills/test_assembled_prompts.py -k nested`
Expected: FAIL — маркер `demo/references/mid.md` не совпадает с регекспом, остаётся неразрешённым, срабатывает `assert not _INCLUDE.search(out)`

- [ ] **Step 3: Расширить резолвер**

Заменить строки 5 и 12-25 в `tests/skills/test_assembled_prompts.py`:

```python
_INCLUDE = re.compile(r"<!-- include: ([A-Za-z0-9_\-/]+\.md) -->")


def assemble(rel_path: str, _stack: tuple = ()) -> str:
    """Собрать промпт как оркестратор: подставить содержимое include-маркеров.

    Путь в маркере — относительно plugin/skills/. Резолв рекурсивный:
    SKILL.md включает references/*.md, те в свою очередь включают _common/*.md.
    Цикл включений — ошибка сборки, а не бесконечная рекурсия.
    """
    assert rel_path not in _stack, f"цикл включений: {' -> '.join((*_stack, rel_path))}"
    text = (SKILLS_DIR / rel_path).read_text(encoding="utf-8")

    def repl(m: re.Match) -> str:
        return assemble(m.group(1), (*_stack, rel_path))

    out = _INCLUDE.sub(repl, text)
    assert not _INCLUDE.search(out), f"неразрешённый include в {rel_path}"
    return out
```

- [ ] **Step 4: Прогнать, убедиться что проходит**

Run: `.venv/bin/pytest -q tests/skills/test_assembled_prompts.py`
Expected: PASS

- [ ] **Step 5: Перевести восемь guard-файлов на `assemble`**

В каждом из восьми файлов заменить чтение сырого файла на собранный текст. Образец правки (`tests/skills/test_preflight_guardrail.py`): было

```python
SOLVE = ROOT / "plugin" / "skills" / "solve-task" / "SKILL.md"


def test_solve_task_has_preflight():
    text = SOLVE.read_text(encoding="utf-8")
```

стало

```python
from .test_assembled_prompts import assemble


def _solve() -> str:
    return assemble("solve-task/SKILL.md")


def test_solve_task_has_preflight():
    text = _solve()
```

Применить ту же замену в: `test_solve_task_brief.py`, `test_solve_task_modes.py` (только для чтения `SKILL`; чтение `PROFILE` не трогать — профиль не включается маркером), `test_ask_uses_summaries.py`, `test_create_task_skill.py`, `test_finish_task_skill.py`, `test_readme_grounding_block.py` (там чтение через `_read("plugin/skills/solve-task/SKILL.md")` — заменить на `assemble("solve-task/SKILL.md")`), `test_review_pr_store_first.py`.

- [ ] **Step 6: Прогнать все guard-тесты скиллов**

Run: `.venv/bin/pytest -q tests/skills/`
Expected: PASS — тесты зелёные на ТЕКУЩЕМ `SKILL.md` (реструктуризации ещё не было), это и есть доказательство эквивалентности перевода.

- [ ] **Step 7: Коммит**

```bash
git add tests/skills/
git commit -m "test(skills): guard-тесты solve-task проверяют собранный промпт, а не сырой SKILL.md (PRI-248)"
```

---

### Task 5: Реструктуризация `SKILL.md`: ядро + `references/`

**Files:**
- Modify: `plugin/skills/solve-task/SKILL.md` (430 строк → ~120)
- Create: `plugin/skills/solve-task/references/preflight.md`
- Create: `plugin/skills/solve-task/references/brief-format.md`
- Create: `plugin/skills/solve-task/references/modes.md`
- Create: `plugin/skills/solve-task/references/context-gathering.md`
- Test: `tests/skills/` (уже переведены Task 4 — должны остаться зелёными без правок)

**Interfaces:**
- Consumes: `prepare_task_context` из Task 3 (скилл вызывает его вместо цепочки раундов); `assemble()` из Task 4.
- Produces: ядро `SKILL.md` с маркерами `<!-- include: solve-task/references/<file>.md -->`.

- [ ] **Step 1: Зафиксировать эталон собранного текста ДО правки**

Run:
```bash
.venv/bin/python -c "import sys; sys.path.insert(0,'.'); from tests.skills.test_assembled_prompts import assemble; open('/tmp/solve_before.md','w').write(assemble('solve-task/SKILL.md'))"
wc -l /tmp/solve_before.md
```
Expected: файл записан, число строк совпадает с текущим `SKILL.md` (430) — включений пока нет.

- [ ] **Step 2: Вынести четыре блока в `references/`**

Перенести **дословно**, без переписывания формулировок (guard-тесты пинят конкретные якоря):

- `references/preflight.md` — из текущего Шага 0: подшаги 0.1-0.4 целиком (свежесть индекса, отчёт о проблемах, прогрев корпуса, теплота сводок) и абзац «Decisions:». Переписать вводную часть подшагов так, чтобы источником чисел был payload `prepare_task_context` (`preflight.drift`, `preflight.summaries`, `gaps`), а не `reviewer status --json`; ветки решений (спросить/реиндексировать/пропустить) сохранить дословно.
- `references/brief-format.md` — из Шага 4: «Relevance filter», «Brief skeleton», «Persist the brief».
- `references/modes.md` — из Шага 0.0 стартовый опрос и из Шага 5 всё про режимы, стратегии, run-state и рубрику резолва `auto`.
- `references/context-gathering.md` — из Шага 3 остаточный ручной добор: графовые тулы, `implementations` для OO/registry, lazy expansion, `get_pr_diff`, правила `stale`-сводок, «Relevance signals → Step 4 filter».

- [ ] **Step 3: Ужать ядро `SKILL.md`**

Ядро оставляет: заголовок и Inputs; один вызов `prepare_task_context(repo, key, branch, warm_board=True)` с описанием, какие секции payload куда идут; ссылку на разбор `gaps`; относящиеся к суждению шаги (Steps 2-4 в сокращённом виде); хендофф (Step 5) в сокращённом виде; четыре маркера включения:

```markdown
<!-- include: solve-task/references/modes.md -->
<!-- include: solve-task/references/preflight.md -->
<!-- include: solve-task/references/context-gathering.md -->
<!-- include: solve-task/references/brief-format.md -->
```

Существующие маркеры `<!-- include: _common/tool-usage.md -->`, `<!-- include: _common/branch-selection.md -->`, `<!-- include: _common/bug-reporting.md -->` сохранить (они могут переехать внутрь references — рекурсивный резолвер Task 4 это выдерживает).

- [ ] **Step 4: Проверить эквивалентность собранного текста**

Run:
```bash
.venv/bin/python -c "import sys; sys.path.insert(0,'.'); from tests.skills.test_assembled_prompts import assemble; open('/tmp/solve_after.md','w').write(assemble('solve-task/SKILL.md'))"
wc -l plugin/skills/solve-task/SKILL.md /tmp/solve_after.md
```
Expected: `SKILL.md` ≤ 130 строк; собранный текст сопоставим по объёму с `/tmp/solve_before.md` за вычетом сокращённых раундов.

- [ ] **Step 5: Прогнать guard-тесты**

Run: `.venv/bin/pytest -q tests/skills/`
Expected: PASS без правок тестов. Красный тест здесь означает, что при переносе потерялся закреплённый якорь — вернуть формулировку дословно, а не ослаблять тест.

- [ ] **Step 6: Добавить guard-тест на сам факт разгрузки ядра**

Дописать в `tests/skills/test_preflight_guardrail.py`:

```python
def test_solve_task_core_is_small_and_uses_references():
    """Ядро скилла разгружено: детали живут в references/, ядро их включает."""
    raw = SOLVE.read_text(encoding="utf-8")
    assert len(raw.splitlines()) <= 130, "ядро SKILL.md снова разрослось"
    assert "<!-- include: solve-task/references/" in raw


def test_solve_task_calls_prepare_task_context():
    text = _solve()
    assert "prepare_task_context(" in text
```

В этом файле `SOLVE` — путь к сырому `SKILL.md`; его чтение здесь сохраняется намеренно: тест про размер ядра обязан смотреть на ядро, а не на собранный текст.

- [ ] **Step 7: Прогнать, убедиться что проходит**

Run: `.venv/bin/pytest -q tests/skills/`
Expected: PASS

- [ ] **Step 8: Коммит**

```bash
git add plugin/skills/solve-task/ tests/skills/test_preflight_guardrail.py
git commit -m "refactor(skills): ядро solve-task + references, один вызов prepare_task_context (PRI-248)"
```

---

### Task 6: Манифесты, документация, финальная проверка

**Files:**
- Modify: `plugin/.codex/*` (генерируется скриптом, руками не править)
- Modify: `README.md`
- Modify: `README.ru.md`
- Test: `tests/install/` (install-тесты проверяют payload-digest)

**Interfaces:**
- Consumes: результат Task 5 (изменённое содержимое `plugin/`).
- Produces: ничего для последующих задач — это финал.

- [ ] **Step 1: Найти скрипт пересборки манифестов**

Run: `ls scripts/ | grep -i codex`
Expected: `update_codex_plugin_manifest.py`

- [ ] **Step 2: Пересобрать манифесты**

Run: `.venv/bin/python scripts/update_codex_plugin_manifest.py`
Expected: скрипт отработал, `git status` показывает изменённые файлы манифестов.

- [ ] **Step 3: Прогнать install-тесты**

Run: `.venv/bin/pytest -q tests/install/`
Expected: PASS. Красный digest означает, что манифесты не пересобраны — вернуться к Step 2.

- [ ] **Step 4: Обновить оба README**

Найти описание потока solve-task: `grep -n "solve-task" README.md README.ru.md`.
Заменить описание преflight и сбора контекста на один вызов `prepare_task_context`: указать, что скилл делает один серверный вызов вместо цепочки `reviewer status` → `sync_board` → `get_task` → `search_*`, что fail-open семантика сохранена и выражена секцией `gaps`, и что графовые расширения и `get_pr_diff` остаются отдельными вызовами по суждению LLM. Правки в `README.md` (EN) и `README.ru.md` (RU) должны быть содержательно одинаковыми.

- [ ] **Step 5: Замер «после» и закрытие критерия приёмки 6**

Прогнать solve-task на любой задаче с новым тулом, затем:

Run: `.venv/bin/python -m eval.solve_task_metrics steps`
Expected: печатает доли по под-шагам. Сравнить долю `preflight + gather` с baseline из Task 1 Step 8 и дописать сравнение «до/после» во взвешенных единицах в `eval/pri246_report.md`.

- [ ] **Step 6: Полный unit-прогон**

Run: `.venv/bin/pytest -q`
Expected: PASS

- [ ] **Step 7: Прогон integration**

Run:
```bash
docker compose --profile test up -d --wait paradedb-test neo4j-test
.venv/bin/pytest -q -m integration
docker compose --profile test rm -sfv paradedb-test neo4j-test
```
Expected: PASS. Никогда не использовать `docker compose --profile test down -v` — команда удалит контейнеры разработки.

- [ ] **Step 8: Линт**

Run: `.venv/bin/ruff check reviewer/ tests/ eval/`
Expected: чисто по изменённым файлам. Repo-wide чистоты добиваться не нужно — на `dev` ruff чист не полностью; сравнить с состоянием до правок.

- [ ] **Step 9: Коммит**

```bash
git add README.md README.ru.md plugin/ eval/pri246_report.md
git commit -m "docs: поток solve-task через prepare_task_context, манифесты и замер эффекта (PRI-248)"
```

---

## Self-Review

**1. Spec coverage:**

| Требование спеки | Задача |
|---|---|
| Офлайн-анализатор `steps.py`, baseline «до», порог 15 % | Task 1 |
| Контракт `prepare_task_context` и форма payload | Task 2 (форма), Task 3 (контракт наружу) |
| `drift` считает сервер, `path` — override, клон из `repo_clone` | Task 3, Step 3 (`_TaskContextDeps.preflight`/`_clone_path`) |
| `warm_board=True` втягивает `sync_board` | Task 2 Step 3, Task 3 Step 3 (`_TaskContextDeps.warm_board`) |
| Секции — строки существующих форматтеров | Task 3 Step 3 (делегирование в `search_codebase`/`get_subsystem_summaries`) |
| Дедуп `linked ∪ similar` остаётся у LLM | Task 2 Step 1 (`test_related_is_not_deduped_by_the_tool`) |
| Шесть строк fail-open-таблицы | Task 2 Step 5 (по тесту на строку) |
| Старые тулы сохраняются | Global Constraints + Task 3 (только добавление) |
| Ядро `SKILL.md` + четыре `references/` | Task 5 |
| Восемь guard-файлов на `assemble()` | Task 4 Step 5 |
| Рекурсивный `assemble()` с любым путём | Task 4 Steps 1-3 |
| Юниты без живых сервисов, сеть под `integration` | Task 2 (фейки), Task 6 Step 7 |
| Пересборка codex-манифестов | Task 6 Steps 2-3 |
| Оба README | Task 6 Step 4 |
| Замер «после» во взвешенных единицах | Task 6 Step 5 |

Пробелов не осталось.

**2. Placeholder scan:** «TBD»/«TODO»/«обработать ошибки»/«написать тесты для вышеописанного» — не встречаются; все шаги с кодом содержат код.

**3. Type consistency:** `build_task_context(deps, *, repo, key, branch, warm_board)` — одна сигнатура в Task 2 Step 3, Task 2 тестах и Task 3 Step 1/3. `gap(section, reason)` — один конструктор, используется в модуле и в `service.py`. `SECTIONS` — один кортеж, используется в модуле, тестах и ветке ошибки резолва repo. Провайдерские методы `_TaskContextDeps` (`preflight`, `task_board`, `warm_board`, `task`, `linked`, `similar`, `subsystems`, `code`, `test_exemplars`) поимённо совпадают с методами `FakeDeps` в тестах. `assemble(rel_path, _stack=())` — один резолвер, импортируется восемью файлами.
