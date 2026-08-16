# Replay-режим офлайн-харнесса метрик solve-task — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить в офлайн-харнесс `eval/solve_task_metrics` режим `replay`, который заново прогоняет продакшн-ретрив по корпусу задач против ground truth и сравнивает два варианта конфигурации в одном A/B-отчёте.

**Architecture:** Шесть новых stdlib-модулей в `eval/solve_task_metrics/` плюс один адаптер (`live.py`), который единственный импортирует живой `reviewer`. Логика прогона получает источник ретрива инъекцией — ровно так же, как `build_snapshot` получает `run_git`, — поэтому тестируется без Postgres, Neo4j и Voyage. Расчётное ядро метрики не дублируется: используются ре-экспорты `reviewer/metrics/brief_quality/`.

**Tech Stack:** Python 3, stdlib (`argparse`, `dataclasses`, `json`, `re`, `statistics`, `pathlib`), pytest. Живые зависимости только в `live.py`: `reviewer.config.settings.Settings`, `reviewer.app.build_components`, `reviewer.mcp.service.MCPReviewService`.

**Spec:** `docs/superpowers/specs/2026-08-17-replay-offline-harness-design.md`

## Global Constraints

- Язык проекта — **русский**: комментарии, докстринги, сообщения CLI. Новый код пишется в том же стиле.
- Коммиты: Conventional Commits на русском (`feat(eval): …`), **без self-attribution** (никаких `Co-Authored-By`, упоминаний Claude).
- Ветка уже создана: `feat/pri-254-replay-harness`. Не переключаться и не мержить.
- Тесты: `.venv/bin/pytest -q` (unit, без инфраструктуры). Integration-тесты обязаны нести `@pytest.mark.integration`.
- **Юнит-тестам запрещены внешние и localhost-сокеты.** Любой тест, кроме помеченного `integration`, обязан работать на фейках.
- `eval/**` — не продакшн-путь. Инвариант `reviewer/**` не импортирует `eval/**` (guard `tests/metrics/test_reexport_guard.py:29`) остаётся нетронутым.
- Расчётные формулы метрики (`core_recall`, `precision`, `is_core_production_path`, `categorize_miss`) берутся **только** из `reviewer/metrics/brief_quality/` через существующие ре-экспорты `eval/solve_task_metrics/{recall,classify,briefs}.py`. Вторая копия запрещена (guard PRI-249, критерий приёмки 3).
- Подкоманды `snapshot`, `stats`, `compare`, `forecast` **не меняются** ни в поведении, ни в наборе зависимостей: они обязаны продолжать работать без Postgres/Neo4j/Voyage.
- `predicted` в replay — пути **только** из секции `code`. `test_exemplars` и `subsystems` не собираются.
- Baseline харнесса для сверки: `core_recall_median` 0.5556, `bulk_core_recall_median` 0.373, `bulk_n_measured` 4.

---

## File Structure

| Файл | Ответственность |
|---|---|
| `eval/solve_task_metrics/context_paths.py` | Парсер путей из отрендеренного вывода ретрива |
| `eval/solve_task_metrics/variants.py` | Типы `TaskInput`/`ReplayTarget`, реестр вариантов, парсер `--set` |
| `eval/solve_task_metrics/replay.py` | Оркестрация прогона корпуса → снимок |
| `eval/solve_task_metrics/replay_history.py` | Схема, запись и чтение снимков replay |
| `eval/solve_task_metrics/replay_report.py` | Markdown-отчёт A/B |
| `eval/solve_task_metrics/live.py` | Живой провайдер ретрива поверх `reviewer` |
| `eval/solve_task_metrics/__main__.py` | Подкоманда `replay` (модифицируется) |
| `eval/solve_task_metrics/__init__.py` | Докстринг: уточнение stdlib-инварианта (модифицируется) |

---

### Task 1: Парсер путей из вывода ретрива

Вывод `ContextPack.as_context(line_numbers=True)` — это блоки `// <node_id> (<path>:<start>-<end>)` плюс пронумерованные строки кода, плюс хвостовые заметки. Нужны только пути из заголовков.

**Files:**
- Create: `eval/solve_task_metrics/context_paths.py`
- Test: `tests/eval/test_context_paths.py`

**Interfaces:**
- Consumes: ничего
- Produces: `extract_context_paths(text: str) -> set[str]`

- [ ] **Step 1: Написать падающий тест**

Создать `tests/eval/test_context_paths.py`:

```python
"""Парсер путей из отрендеренного вывода ретрива (PRI-254)."""
from __future__ import annotations

from eval.solve_task_metrics.context_paths import extract_context_paths

SAMPLE = """// reviewer/services/brief_quality.py#measure (reviewer/services/brief_quality.py:87-168)
   87 | def measure(
   88 |     *,
   89 |     task_key: str | None,

// reviewer/web/history.py#ReviewHistory (reviewer/web/history.py:21-601)
   21 | class ReviewHistory:
   22 |     \"\"\"Персистирует историю прогонов ревью.\"\"\"

— контекст обрезан по cliff: 15 из 58 (скор 0.51→0.37, обрыв на 0.37). За обрезом ещё 17 релевантных: reviewer (0.37).
"""


def test_extracts_paths_from_headers():
    assert extract_context_paths(SAMPLE) == {
        "reviewer/services/brief_quality.py",
        "reviewer/web/history.py",
    }


def test_empty_result_marker_yields_no_paths():
    assert extract_context_paths("(ничего не найдено)") == set()


def test_blank_input_yields_no_paths():
    assert extract_context_paths("") == set()


def test_code_line_mentioning_a_path_is_not_a_candidate():
    """Путь в теле кода — не кандидат ретрива: заголовок начинается с '// '."""
    text = (
        "// reviewer/a.py#f (reviewer/a.py:1-3)\n"
        "    1 |     from reviewer.b import thing  # reviewer/b.py\n"
        "    2 |     path = \"tests/test_zzz.py\"\n"
    )
    assert extract_context_paths(text) == {"reviewer/a.py"}


def test_truncated_header_is_dropped_not_guessed():
    """Обрыв по max_context_chars может разрезать заголовок — половину не берём."""
    text = (
        "// reviewer/a.py#f (reviewer/a.py:1-3)\n"
        "    1 | x = 1\n\n"
        "// reviewer/b.py#g (reviewer/b.py:10-\n"
        "[...truncated]"
    )
    assert extract_context_paths(text) == {"reviewer/a.py"}


def test_degraded_note_is_not_a_path():
    text = "(ничего не найдено)\n\n(реранкер недоступен: выдача деградировала)"
    assert extract_context_paths(text) == set()
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run: `.venv/bin/pytest tests/eval/test_context_paths.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'eval.solve_task_metrics.context_paths'`

- [ ] **Step 3: Написать минимальную реализацию**

Создать `eval/solve_task_metrics/context_paths.py`:

```python
"""Пути-кандидаты из отрендеренного вывода ретрива (PRI-254).

Парсится ИМЕННО отрендеренный текст, а не объект ContextPack: as_context
обрезает вывод по max_context_chars, и часть найденных путей до сборщика
брифа не доезжает. Чтение items напрямую приписало бы ретриву кандидатов,
которых сборщик брифа не видел, и завысило бы метрику.
"""
from __future__ import annotations

import re

# Заголовок блока: '// <node_id> (<path>:<start>-<end>)'. Диапазон строк
# обязателен: обрезанный по max_context_chars заголовок его не имеет, и
# такой блок отбрасывается, а не достраивается догадкой.
_HEADER_RE = re.compile(r"^//\s+\S+\s+\(([^()]+):\d+-\d+\)\s*$")


def extract_context_paths(text: str) -> set[str]:
    """Множество путей из заголовков блоков вывода ретрива."""
    paths: set[str] = set()
    for line in (text or "").splitlines():
        match = _HEADER_RE.match(line.strip())
        if match:
            paths.add(match.group(1).strip())
    return paths
```

- [ ] **Step 4: Прогнать тест и убедиться, что он проходит**

Run: `.venv/bin/pytest tests/eval/test_context_paths.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Закоммитить**

```bash
git add eval/solve_task_metrics/context_paths.py tests/eval/test_context_paths.py
git commit -m "feat(eval): парсер путей-кандидатов из вывода ретрива"
```

---

### Task 2: Типы прогона и реестр вариантов

**Files:**
- Create: `eval/solve_task_metrics/variants.py`
- Test: `tests/eval/test_variants.py`

**Interfaces:**
- Consumes: `context_paths.extract_context_paths` (Task 1)
- Produces:
  - `TaskInput(key: str, task: dict | None, query: str)` — frozen dataclass
  - `ReplayTarget(repo: str, branch: str, limits: dict | None)` — frozen dataclass
  - `parse_overrides(pairs: list[str]) -> dict` — `["search_codebase.ceiling=25"] → {"search_codebase": {"ceiling": 25}}`
  - `get_variant(name: str) -> Callable[[object, TaskInput, ReplayTarget], set[str]]`
  - `VARIANT_NAMES: tuple[str, ...]`
  - `UnknownVariant(ValueError)`, `BadOverride(ValueError)`

- [ ] **Step 1: Написать падающий тест**

Создать `tests/eval/test_variants.py`:

```python
"""Реестр вариантов ретрива и парсер оверрайдов лимитов (PRI-254)."""
from __future__ import annotations

import pytest

from eval.solve_task_metrics import variants


class FakeProvider:
    """Провайдер ретрива: запоминает, с какими лимитами его звали."""

    def __init__(self, text: str = ""):
        self.text = text
        self.calls: list = []

    def code(self, repo: str, branch: str, query: str, limits):
        self.calls.append((repo, branch, query, limits))
        return self.text


HEADER = "// reviewer/a.py#f (reviewer/a.py:1-3)\n    1 | x = 1\n"


def _inputs():
    return (
        variants.TaskInput(key="PRI-1", task={"title": "t"}, query="q"),
        variants.ReplayTarget(repo="o/n", branch="dev", limits=None),
    )


def test_baseline_returns_paths_and_passes_no_limits():
    provider = FakeProvider(HEADER)
    task, target = _inputs()
    assert variants.get_variant("baseline")(provider, task, target) == {"reviewer/a.py"}
    assert provider.calls == [("o/n", "dev", "q", None)]


def test_limits_variant_forwards_overrides():
    provider = FakeProvider(HEADER)
    task, _ = _inputs()
    target = variants.ReplayTarget(
        repo="o/n", branch="dev", limits={"search_codebase": {"ceiling": 25}}
    )
    assert variants.get_variant("limits")(provider, task, target) == {"reviewer/a.py"}
    assert provider.calls[0][3] == {"search_codebase": {"ceiling": 25}}


def test_unknown_variant_lists_available_names():
    with pytest.raises(variants.UnknownVariant) as error:
        variants.get_variant("нет-такого")
    for name in variants.VARIANT_NAMES:
        assert name in str(error.value)


def test_parse_overrides_builds_nested_dict_with_typed_values():
    assert variants.parse_overrides(
        ["search_codebase.ceiling=25", "search_codebase.ratio=0.4", "graph.hops=2"]
    ) == {
        "search_codebase": {"ceiling": 25, "ratio": 0.4},
        "graph": {"hops": 2},
    }


def test_parse_overrides_empty_is_none():
    assert variants.parse_overrides([]) is None
    assert variants.parse_overrides(None) is None


@pytest.mark.parametrize("bad", ["ceiling=25", "search_codebase.ceiling", "a.b.c=1", "=5"])
def test_parse_overrides_rejects_malformed(bad):
    with pytest.raises(variants.BadOverride):
        variants.parse_overrides([bad])
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run: `.venv/bin/pytest tests/eval/test_variants.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'eval.solve_task_metrics.variants'`

- [ ] **Step 3: Написать минимальную реализацию**

Создать `eval/solve_task_metrics/variants.py`:

```python
"""Варианты конфигурации ретрива для replay (PRI-254).

Вариант — именованная стратегия сборки множества путей-кандидатов по задаче.
Реестр живёт в eval/, а не в reviewer/: продакшн-путь про эвал знать не должен.
Рычаги ID-311/312 добавят свои стратегии одной строкой этого реестра.
"""
from __future__ import annotations

from dataclasses import dataclass

from .context_paths import extract_context_paths

# Разделы блока context_limits, которые разрешено оверрайдить: форма совпадает
# с .review.yml, поэтому свёртка идёт существующим ContextLimits.from_review_yaml
# и второго парсера лимитов в проекте не появляется.
OVERRIDE_SECTIONS = ("search_codebase", "search_tasks", "graph")


class UnknownVariant(ValueError):
    """Запрошен незарегистрированный вариант."""


class BadOverride(ValueError):
    """Оверрайд лимитов не разбирается."""


@dataclass(frozen=True)
class TaskInput:
    """Вход одной задачи корпуса: ключ, задача из стора и запрос ретрива."""

    key: str
    task: dict | None
    query: str


@dataclass(frozen=True)
class ReplayTarget:
    """Цель прогона: репозиторий, ветка и оверрайды лимитов варианта."""

    repo: str
    branch: str
    limits: dict | None = None


def _paths(provider, task: TaskInput, target: ReplayTarget, limits) -> set[str]:
    text = provider.code(target.repo, target.branch, task.query, limits)
    return extract_context_paths(text)


def _baseline(provider, task: TaskInput, target: ReplayTarget) -> set[str]:
    """Продакшн-путь без изменений: лимиты репозитория, никаких оверрайдов."""
    return _paths(provider, task, target, None)


def _limits(provider, task: TaskInput, target: ReplayTarget) -> set[str]:
    """Тот же путь с оверрайдами лимитов из --set."""
    return _paths(provider, task, target, target.limits)


_REGISTRY = {
    "baseline": _baseline,
    "limits": _limits,
}

VARIANT_NAMES = tuple(_REGISTRY)


def get_variant(name: str):
    """Стратегия по имени; неизвестное имя — ошибка, а не тихий фолбэк."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise UnknownVariant(
            f"неизвестный вариант {name!r}; доступны: {', '.join(VARIANT_NAMES)}"
        ) from None


def _value(raw: str):
    """Привести значение оверрайда к int/float; иначе оставить строкой."""
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def parse_overrides(pairs) -> dict | None:
    """Разобрать пары '<раздел>.<ключ>=<значение>' в блок context_limits."""
    if not pairs:
        return None
    out: dict = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not raw:
            raise BadOverride(f"оверрайд {pair!r}: ожидается ключ=значение")
        section, dot, field = key.partition(".")
        if not dot or not field or "." in field:
            raise BadOverride(
                f"оверрайд {pair!r}: ожидается <раздел>.<ключ>, "
                f"раздел из {', '.join(OVERRIDE_SECTIONS)}"
            )
        if section not in OVERRIDE_SECTIONS:
            raise BadOverride(
                f"оверрайд {pair!r}: неизвестный раздел {section!r}; "
                f"доступны: {', '.join(OVERRIDE_SECTIONS)}"
            )
        out.setdefault(section, {})[field] = _value(raw)
    return out
```

- [ ] **Step 4: Прогнать тест и убедиться, что он проходит**

Run: `.venv/bin/pytest tests/eval/test_variants.py -q`
Expected: PASS (9 passed)

- [ ] **Step 5: Закоммитить**

```bash
git add eval/solve_task_metrics/variants.py tests/eval/test_variants.py
git commit -m "feat(eval): реестр вариантов ретрива и парсер оверрайдов лимитов"
```

---

### Task 3: Оркестрация прогона корпуса

Сердце режима. Провайдер ретрива и `run_git` инъектируются, поэтому модуль тестируется без инфраструктуры.

**Files:**
- Create: `eval/solve_task_metrics/replay.py`
- Test: `tests/eval/test_replay.py`

**Interfaces:**
- Consumes: `variants.TaskInput`, `variants.ReplayTarget`, `variants.get_variant` (Task 2); `ground_truth.collect`, `classify.is_core_production_path`, `recall.evaluate_task`, `recall.aggregate`, `briefs.load_briefs` (существующие)
- Produces:
  - `STATUS_MEASURED`, `STATUS_EMPTY_CORE`, `STATUS_NO_GROUND_TRUTH`, `STATUS_NO_TASK`, `STATUS_RETRIEVAL_FAILED` — строковые константы
  - `corpus_keys(briefs_dir) -> list[str]`
  - `run_replay(*, provider, run_git, briefs_dir, target, variant_name, commit, taken_at, limit=None) -> dict` — снимок replay

- [ ] **Step 1: Написать падающий тест**

Создать `tests/eval/test_replay.py`:

```python
"""Оркестрация replay-прогона корпуса (PRI-254)."""
from __future__ import annotations

import pathlib

from eval.solve_task_metrics import replay

BRIEF = """# Brief — {key}

## Relevant code
- `reviewer/whatever.py:1` — неважно: replay не читает эту секцию.
"""


def _corpus(tmp_path: pathlib.Path, keys) -> pathlib.Path:
    briefs = tmp_path / "briefs"
    briefs.mkdir()
    for index, key in enumerate(keys):
        (briefs / f"2026-01-{index + 1:02d}-{key}-x.md").write_text(
            BRIEF.format(key=key), encoding="utf-8"
        )
    return briefs


class FakeGit:
    """git-раннер с заранее заданными мержами и составом diff'а."""

    def __init__(self, changed_by_key, missing=()):
        self.changed_by_key = changed_by_key
        self.missing = set(missing)

    def __call__(self, args):
        if args[0] == "log":
            key = args[-1].removeprefix("--grep=")
            if key in self.missing or key not in self.changed_by_key:
                return ""
            return f"sha{key} Merge pull request #1 from owner/branch\n"
        if args[0] == "diff":
            key = args[-1].removeprefix("sha")
            return "\n".join(self.changed_by_key[key])
        if args[0] == "cat-file":
            return ""
        raise AssertionError(f"неожиданный git-вызов: {args}")


class FakeProvider:
    def __init__(self, tasks, paths_by_key, fail=()):
        self.tasks = tasks
        self.paths_by_key = paths_by_key
        self.fail = set(fail)
        self.queries: list = []

    def preflight(self, repo, branch):
        return {"branch": branch, "indexed_sha": "abc123", "drift": 0,
                "summaries": 1, "chunks": 2, "graph_nodes": 3}

    def task(self, key):
        return self.tasks.get(key)

    def query(self, task, key):
        return f"{key}|{(task or {}).get('title', '')}"

    def code(self, repo, branch, query, limits):
        self.queries.append(query)
        key = query.split("|")[0]
        if key in self.fail:
            raise RuntimeError("ретрив недоступен")
        return "\n".join(
            f"// {path}#f ({path}:1-2)\n    1 | x = 1"
            for path in self.paths_by_key.get(key, [])
        )


def _target():
    return replay.variants.ReplayTarget(repo="o/n", branch="dev", limits=None)


def _run(tmp_path, keys, *, tasks, changed, predicted, fail=(), missing=(), limit=None):
    provider = FakeProvider(tasks, predicted, fail=fail)
    return replay.run_replay(
        provider=provider,
        run_git=FakeGit(changed, missing=missing),
        briefs_dir=_corpus(tmp_path, keys),
        target=_target(),
        variant_name="baseline",
        commit="deadbee",
        taken_at="2026-08-17T00:00:00+00:00",
        limit=limit,
    )


def test_measured_task_scores_core_recall(tmp_path):
    snap = _run(
        tmp_path, ["PRI-1"],
        tasks={"PRI-1": {"title": "PRI-1", "description": ""}},
        changed={"PRI-1": ["reviewer/a.py", "reviewer/b.py", "docs/x.md"]},
        predicted={"PRI-1": ["reviewer/a.py"]},
    )
    row = snap["tasks"][0]
    assert row["status"] == replay.STATUS_MEASURED
    assert row["expected_core"] == 2 and row["hit_core"] == 1
    assert row["core_recall"] == 0.5
    assert row["predicted_paths"] == ["reviewer/a.py"]
    assert snap["aggregate"]["core_recall_median"] == 0.5


def test_empty_core_denominator_is_not_zero_recall(tmp_path):
    snap = _run(
        tmp_path, ["PRI-2"],
        tasks={"PRI-2": {"title": "t", "description": ""}},
        changed={"PRI-2": ["docs/x.md", "tests/test_y.py"]},
        predicted={"PRI-2": []},
    )
    assert snap["tasks"][0]["status"] == replay.STATUS_EMPTY_CORE
    assert snap["tasks"][0]["core_recall"] is None
    assert snap["aggregate"]["n_measured"] == 0


def test_missing_ground_truth_is_named_not_dropped(tmp_path):
    snap = _run(
        tmp_path, ["PRI-3"],
        tasks={"PRI-3": {"title": "t", "description": ""}},
        changed={}, predicted={}, missing=["PRI-3"],
    )
    assert snap["tasks"][0]["status"] == replay.STATUS_NO_GROUND_TRUTH
    assert snap["statuses"][replay.STATUS_NO_GROUND_TRUTH] == 1


def test_task_absent_from_store_is_named(tmp_path):
    snap = _run(
        tmp_path, ["PRI-4"],
        tasks={},
        changed={"PRI-4": ["reviewer/a.py"]},
        predicted={"PRI-4": ["reviewer/a.py"]},
    )
    assert snap["tasks"][0]["status"] == replay.STATUS_NO_TASK


def test_retrieval_failure_does_not_abort_the_run(tmp_path):
    snap = _run(
        tmp_path, ["PRI-5", "PRI-6"],
        tasks={"PRI-5": {"title": "PRI-5", "description": ""},
               "PRI-6": {"title": "PRI-6", "description": ""}},
        changed={"PRI-5": ["reviewer/a.py"], "PRI-6": ["reviewer/b.py"]},
        predicted={"PRI-6": ["reviewer/b.py"]},
        fail=["PRI-5"],
    )
    by_key = {row["key"]: row["status"] for row in snap["tasks"]}
    assert by_key["PRI-5"] == replay.STATUS_RETRIEVAL_FAILED
    assert by_key["PRI-6"] == replay.STATUS_MEASURED


def test_duplicate_keys_counted_once(tmp_path):
    briefs = tmp_path / "briefs"
    briefs.mkdir()
    for name in ("2026-01-01-PRI-7-a.md", "2026-01-02-PRI-7-b.md"):
        (briefs / name).write_text(BRIEF.format(key="PRI-7"), encoding="utf-8")
    assert replay.corpus_keys(briefs) == ["PRI-7"]


def test_limit_truncates_corpus_and_marks_snapshot_partial(tmp_path):
    snap = _run(
        tmp_path, ["PRI-8", "PRI-9"],
        tasks={"PRI-8": {"title": "PRI-8", "description": ""}},
        changed={"PRI-8": ["reviewer/a.py"]},
        predicted={"PRI-8": ["reviewer/a.py"]},
        limit=1,
    )
    assert snap["partial"] is True
    assert len(snap["tasks"]) == 1


def test_full_run_is_not_partial_and_records_index_identity(tmp_path):
    snap = _run(
        tmp_path, ["PRI-10"],
        tasks={"PRI-10": {"title": "PRI-10", "description": ""}},
        changed={"PRI-10": ["reviewer/a.py"]},
        predicted={"PRI-10": ["reviewer/a.py"]},
    )
    assert snap["partial"] is False
    assert snap["indexed_sha"] == "abc123"
    assert snap["commit"] == "deadbee"
    assert snap["variant"] == "baseline"
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run: `.venv/bin/pytest tests/eval/test_replay.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'eval.solve_task_metrics.replay'`

- [ ] **Step 3: Написать минимальную реализацию**

Создать `eval/solve_task_metrics/replay.py`:

```python
"""Прогон ретрива по корпусу задач против ground truth (PRI-254).

Модуль намеренно не знает про живые компоненты reviewer: источник ретрива
приходит объектом-провайдером, git — инъектируемым раннером, ровно как в
build_snapshot. Поэтому весь прогон тестируется без Postgres, Neo4j и Voyage.

Замеряется СЫРОЙ ретрив, а не бриф: baseline существующего снимка построен на
путях, которые отобрала LLM, и линии replay/snapshot несравнимы напрямую.
"""
from __future__ import annotations

import pathlib
import statistics

from . import briefs, classify, ground_truth, recall, variants

SCHEMA = 1
"""Версия схемы снимка replay. Растёт при несовместимом изменении формата."""

STATUS_MEASURED = "measured"
STATUS_EMPTY_CORE = "empty_core_denominator"
STATUS_NO_GROUND_TRUTH = "no_ground_truth"
STATUS_NO_TASK = "task_not_in_store"
STATUS_RETRIEVAL_FAILED = "retrieval_failed"

STATUSES = (
    STATUS_MEASURED, STATUS_EMPTY_CORE, STATUS_NO_GROUND_TRUTH,
    STATUS_NO_TASK, STATUS_RETRIEVAL_FAILED,
)


def _median(values: list):
    return statistics.median(values) if values else None


def corpus_keys(briefs_dir: pathlib.Path) -> list[str]:
    """Ключи задач корпуса в порядке брифов, по одному на ключ.

    Дедуп обязателен: два брифа одного ключа (переписанный бриф, вторая
    итерация) иначе дали бы задаче двойной вес в агрегате.
    """
    seen: set = set()
    keys: list = []
    for record in briefs.load_briefs(briefs_dir):
        if not record.task_key or record.task_key in seen:
            continue
        seen.add(record.task_key)
        keys.append(record.task_key)
    return keys


def _task_row(key: str, status: str, **fields) -> dict:
    row = {
        "key": key, "status": status, "expected": 0, "expected_core": 0,
        "predicted": 0, "hit_core": 0, "core_recall": None, "raw_recall": None,
        "precision": None, "predicted_paths": [], "expected_core_paths": [],
    }
    row.update(fields)
    return row


def _evaluate(key: str, predicted: set, truth, run_git) -> dict:
    """Посчитать одну задачу той же линейкой, что build_snapshot."""
    existed_cache: dict = {}

    def existed(path: str) -> bool:
        if path not in existed_cache:
            existed_cache[path] = ground_truth.path_existed(
                truth.parent_ref, path, run_git
            )
        return existed_cache[path]

    expected_core = {
        path for path in truth.changed
        if classify.is_core_production_path(path) and existed(path)
    }
    row = recall.evaluate_task(key, predicted, truth.changed, expected_core)
    status = STATUS_MEASURED if expected_core else STATUS_EMPTY_CORE
    return _task_row(
        key, status,
        expected=row.expected, expected_core=row.expected_core,
        predicted=row.predicted, hit_core=row.hit_core,
        core_recall=row.core_recall, raw_recall=row.raw_recall,
        precision=row.precision,
        predicted_paths=sorted(predicted),
        expected_core_paths=sorted(expected_core),
    )


def run_replay(*, provider, run_git, briefs_dir: pathlib.Path,
               target: variants.ReplayTarget, variant_name: str,
               commit: str, taken_at: str, limit: int | None = None) -> dict:
    """Прогнать корпус одним вариантом и вернуть снимок replay.

    Ни один отказ не прерывает прогон и не исчезает молча: у каждой задачи
    корпуса есть именованный статус (см. STATUSES).
    """
    strategy = variants.get_variant(variant_name)
    keys = corpus_keys(briefs_dir)
    partial = bool(limit) and limit < len(keys)
    if limit:
        keys = keys[:limit]

    try:
        preflight = provider.preflight(target.repo, target.branch)
    except Exception:  # noqa: BLE001 — статус индекса не обязателен для прогона
        preflight = {}

    rows: list = []
    for key in keys:
        truth = ground_truth.collect(key, run_git)
        if not truth.changed:
            rows.append(_task_row(key, STATUS_NO_GROUND_TRUTH))
            continue
        task = provider.task(key)
        # Формула запроса — продакшн-функция; она живёт в провайдере вместе с
        # остальными живыми зависимостями, чтобы этот модуль не тянул reviewer.
        task_input = variants.TaskInput(
            key=key, task=task, query=provider.query(task, key)
        )
        try:
            predicted = strategy(provider, task_input, target)
        except Exception:  # noqa: BLE001 — сбой ретрива на задаче не роняет прогон
            rows.append(_task_row(key, STATUS_RETRIEVAL_FAILED))
            continue
        row = _evaluate(key, predicted, truth, run_git)
        if task is None:
            # Задача есть в корпусе брифов, но не в сторе: запрос выродился в
            # ключ, поэтому измерение непредставительно и считаться не должно.
            row["status"] = STATUS_NO_TASK
            row["core_recall"] = None
        rows.append(row)

    measured = [r for r in rows if r["status"] == STATUS_MEASURED]
    quality = [
        recall.TaskQuality(
            task_key=r["key"], expected=r["expected"],
            expected_core=r["expected_core"], predicted=r["predicted"],
            hit_core=r["hit_core"], core_recall=r["core_recall"],
            raw_recall=r["raw_recall"], precision=r["precision"],
        )
        for r in measured
    ]
    agg = recall.aggregate(quality)

    return {
        "schema": SCHEMA,
        "taken_at": taken_at,
        "commit": commit,
        "variant": variant_name,
        "variant_params": target.limits,
        "repo": target.repo,
        "branch": preflight.get("branch", target.branch),
        "indexed_sha": preflight.get("indexed_sha"),
        "chunks": preflight.get("chunks"),
        "graph_nodes": preflight.get("graph_nodes"),
        "partial": partial,
        "corpus": len(rows),
        "statuses": {status: sum(1 for r in rows if r["status"] == status)
                     for status in STATUSES},
        "aggregate": {
            "core_recall_median": agg.core_recall_median,
            "core_recall_mean": agg.core_recall_mean,
            "raw_recall_median": agg.raw_recall_median,
            "denominator_median": agg.denominator_median,
            "bulk_core_recall_median": agg.bulk_core_recall_median,
            "bulk_n_measured": agg.bulk_n_measured,
            "n_measured": agg.n_measured,
            "no_measurement": agg.no_measurement,
            # Медианы уже посчитанных полей строк — не формула метрики, а сводка
            # по ним; расчётное ядро остаётся единственной копией в reviewer/.
            "precision_median": _median([r["precision"] for r in measured
                                         if r["precision"] is not None]),
            "predicted_median": _median([r["predicted"] for r in measured]),
        },
        "tasks": rows,
    }
```

- [ ] **Step 4: Прогнать тест и убедиться, что он проходит**

Run: `.venv/bin/pytest tests/eval/test_replay.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Закоммитить**

```bash
git add eval/solve_task_metrics/replay.py tests/eval/test_replay.py
git commit -m "feat(eval): прогон корпуса ретривом против ground truth"
```

---

### Task 4: История снимков replay

Формат `snapshot` агрегатный, а критерий приёмки 2 требует дельту по задачам — поэтому у replay своя схема и свой файл.

**Files:**
- Create: `eval/solve_task_metrics/replay_history.py`
- Test: `tests/eval/test_replay_history.py`

**Interfaces:**
- Consumes: ничего
- Produces:
  - `HISTORY_PATH_NAME = "replay_history.jsonl"`
  - `append(path: pathlib.Path, snapshot: dict) -> None`
  - `load(path: pathlib.Path) -> list[dict]`
  - `select(snapshots: list[dict], ref: str) -> dict` — `ref` = `"last"`, `"-N"` или имя варианта
  - `PartialSnapshotRejected(ValueError)`, `SnapshotNotFound(ValueError)`
  - `comparability_warnings(old: dict, new: dict) -> list[str]`

- [ ] **Step 1: Написать падающий тест**

Создать `tests/eval/test_replay_history.py`:

```python
"""Хранилище снимков replay и проверка сравнимости (PRI-254)."""
from __future__ import annotations

import pytest

from eval.solve_task_metrics import replay_history as rh


def _snap(**over):
    snap = {
        "schema": 1, "taken_at": "2026-08-17T00:00:00+00:00", "commit": "aaaaaaa",
        "variant": "baseline", "variant_params": None, "repo": "o/n", "branch": "dev",
        "indexed_sha": "sha1", "chunks": 10, "graph_nodes": 20, "partial": False,
        "corpus": 1, "statuses": {}, "aggregate": {}, "tasks": [],
    }
    snap.update(over)
    return snap


def test_append_and_load_roundtrip(tmp_path):
    path = tmp_path / rh.HISTORY_PATH_NAME
    rh.append(path, _snap(commit="one"))
    rh.append(path, _snap(commit="two"))
    loaded = rh.load(path)
    assert [s["commit"] for s in loaded] == ["one", "two"]


def test_load_missing_file_is_empty(tmp_path):
    assert rh.load(tmp_path / "нет.jsonl") == []


def test_select_last_and_offset(tmp_path):
    snaps = [_snap(commit="one"), _snap(commit="two"), _snap(commit="three")]
    assert rh.select(snaps, "last")["commit"] == "three"
    assert rh.select(snaps, "-1")["commit"] == "two"


def test_select_by_variant_takes_most_recent(tmp_path):
    snaps = [_snap(variant="baseline", commit="one"),
             _snap(variant="limits", commit="two"),
             _snap(variant="baseline", commit="three")]
    assert rh.select(snaps, "baseline")["commit"] == "three"


def test_select_rejects_partial_snapshot():
    with pytest.raises(rh.PartialSnapshotRejected):
        rh.select([_snap(partial=True)], "last")


def test_select_on_empty_history_raises():
    with pytest.raises(rh.SnapshotNotFound):
        rh.select([], "last")


def test_comparability_warns_on_index_drift():
    warnings = rh.comparability_warnings(
        _snap(indexed_sha="sha1"), _snap(indexed_sha="sha2")
    )
    assert any("indexed_sha" in w for w in warnings)


def test_comparability_warns_on_commit_mismatch():
    warnings = rh.comparability_warnings(_snap(commit="a"), _snap(commit="b"))
    assert any("коммит" in w for w in warnings)


def test_comparability_silent_when_identical():
    assert rh.comparability_warnings(_snap(), _snap()) == []
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run: `.venv/bin/pytest tests/eval/test_replay_history.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'eval.solve_task_metrics.replay_history'`

- [ ] **Step 3: Написать минимальную реализацию**

Создать `eval/solve_task_metrics/replay_history.py`:

```python
"""Хранилище снимков replay (PRI-254).

Своя схема и свой файл: формат snapshot агрегатный, а отчёт A/B обязан
показывать дельту ПО ЗАДАЧАМ, поэтому смешивать истории нельзя.
"""
from __future__ import annotations

import json
import pathlib

HISTORY_PATH_NAME = "replay_history.jsonl"


class SnapshotNotFound(ValueError):
    """В истории нет снимка, подходящего под ссылку."""


class PartialSnapshotRejected(ValueError):
    """Частичный снимок (--limit) нельзя использовать как сторону сравнения."""


def append(path: pathlib.Path, snapshot: dict) -> None:
    """Дописать снимок в jsonl-историю."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(snapshot, ensure_ascii=False) + "\n")


def load(path: pathlib.Path) -> list:
    """Прочитать историю; отсутствующий файл — пустая история."""
    if not path.exists():
        return []
    out: list = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def select(snapshots: list, ref: str) -> dict:
    """Выбрать снимок: 'last', отступ '-N' или имя варианта (последний такой).

    Частичный снимок отвергается явно: сравнение усечённого корпуса с полным
    даёт разницу корпуса, а не эффект варианта. Тот же fail-closed, что у
    sync_board, где --limit отключает продвижение курсора.
    """
    if not snapshots:
        raise SnapshotNotFound("история replay пуста; сначала прогоните replay")
    if ref == "last":
        chosen = snapshots[-1]
    elif ref.startswith("-") and ref[1:].isdigit():
        offset = int(ref[1:])
        if offset >= len(snapshots):
            raise SnapshotNotFound(
                f"в истории {len(snapshots)} снимк(ов) — отступ {ref} недоступен"
            )
        chosen = snapshots[-1 - offset]
    else:
        matching = [s for s in snapshots if s.get("variant") == ref]
        if not matching:
            raise SnapshotNotFound(f"снимков варианта {ref!r} в истории нет")
        chosen = matching[-1]
    if chosen.get("partial"):
        raise PartialSnapshotRejected(
            "снимок помечен partial (снят с --limit) и не годится как сторона "
            "сравнения; прогоните полный replay"
        )
    return chosen


def comparability_warnings(old: dict, new: dict) -> list:
    """Чем стороны сравнения различаются помимо варианта.

    Молча склеивать снимки с разных состояний индекса нельзя: дрейф индекса
    выглядел бы как эффект варианта.
    """
    warnings: list = []
    if old.get("indexed_sha") != new.get("indexed_sha"):
        warnings.append(
            f"indexed_sha сторон различается ({old.get('indexed_sha')} против "
            f"{new.get('indexed_sha')}): дельта включает дрейф base-индекса"
        )
    if old.get("commit") != new.get("commit"):
        warnings.append(
            f"коммит сторон различается ({old.get('commit')} против "
            f"{new.get('commit')}): ground truth мог измениться"
        )
    if old.get("corpus") != new.get("corpus"):
        warnings.append(
            f"размер корпуса различается ({old.get('corpus')} против "
            f"{new.get('corpus')})"
        )
    return warnings
```

- [ ] **Step 4: Прогнать тест и убедиться, что он проходит**

Run: `.venv/bin/pytest tests/eval/test_replay_history.py -q`
Expected: PASS (9 passed)

- [ ] **Step 5: Закоммитить**

```bash
git add eval/solve_task_metrics/replay_history.py tests/eval/test_replay_history.py
git commit -m "feat(eval): история снимков replay с проверкой сравнимости"
```

---

### Task 5: Отчёт A/B

**Files:**
- Create: `eval/solve_task_metrics/replay_report.py`
- Test: `tests/eval/test_replay_report.py`

**Interfaces:**
- Consumes: `replay_history.comparability_warnings` (Task 4), `replay.STATUS_MEASURED` (Task 3)
- Produces: `render(new: dict, old: dict | None = None) -> str`

- [ ] **Step 1: Написать падающий тест**

Создать `tests/eval/test_replay_report.py`:

```python
"""Markdown-отчёт replay и A/B-дельта (PRI-254)."""
from __future__ import annotations

from eval.solve_task_metrics import replay_report


def _task(key, core_recall, predicted_paths, status="measured"):
    return {
        "key": key, "status": status, "expected": 3, "expected_core": 2,
        "predicted": len(predicted_paths), "hit_core": 1,
        "core_recall": core_recall, "raw_recall": 0.33, "precision": 0.5,
        "predicted_paths": sorted(predicted_paths),
        "expected_core_paths": ["reviewer/a.py", "reviewer/b.py"],
    }


def _snap(variant, tasks, median, **over):
    snap = {
        "schema": 1, "taken_at": "2026-08-17T00:00:00+00:00", "commit": "aaaaaaa",
        "variant": variant, "variant_params": None, "repo": "o/n", "branch": "dev",
        "indexed_sha": "sha1", "chunks": 10, "graph_nodes": 20, "partial": False,
        "corpus": len(tasks),
        "statuses": {"measured": len(tasks)},
        "aggregate": {
            "core_recall_median": median, "core_recall_mean": median,
            "raw_recall_median": 0.33, "denominator_median": 2.0,
            "bulk_core_recall_median": None, "bulk_n_measured": 0,
            "n_measured": len(tasks), "no_measurement": 0,
            "precision_median": 0.5, "predicted_median": 1.0,
        },
        "tasks": tasks,
    }
    snap.update(over)
    return snap


def test_single_run_report_has_no_delta_columns():
    text = replay_report.render(_snap("baseline", [_task("PRI-1", 0.5, ["a.py"])], 0.5))
    assert "core-recall" in text
    assert "Δ" not in text


def test_ab_report_shows_aggregate_delta():
    old = _snap("baseline", [_task("PRI-1", 0.5, ["reviewer/a.py"])], 0.5)
    new = _snap("limits", [_task("PRI-1", 1.0, ["reviewer/a.py", "reviewer/b.py"])], 1.0)
    text = replay_report.render(new, old)
    assert "Δ" in text
    assert "+0.5" in text


def test_ab_report_shows_per_task_delta_with_path_diff():
    old = _snap("baseline", [_task("PRI-1", 0.5, ["reviewer/a.py"])], 0.5)
    new = _snap("limits", [_task("PRI-1", 1.0, ["reviewer/b.py"])], 1.0)
    text = replay_report.render(new, old)
    assert "PRI-1" in text
    assert "reviewer/b.py" in text     # приобретённый путь
    assert "reviewer/a.py" in text     # потерянный путь


def test_unchanged_tasks_are_collapsed_into_a_counter():
    tasks = [_task(f"PRI-{i}", 0.5, ["reviewer/a.py"]) for i in range(5)]
    old = _snap("baseline", tasks, 0.5)
    new = _snap("limits", tasks, 0.5)
    text = replay_report.render(new, old)
    assert "без изменений" in text
    assert text.count("PRI-0") <= 1


def test_index_drift_warning_is_surfaced():
    old = _snap("baseline", [_task("PRI-1", 0.5, ["a.py"])], 0.5, indexed_sha="sha1")
    new = _snap("limits", [_task("PRI-1", 0.5, ["a.py"])], 0.5, indexed_sha="sha2")
    assert "indexed_sha" in replay_report.render(new, old)


def test_incomparability_disclaimer_always_present():
    text = replay_report.render(_snap("baseline", [_task("PRI-1", 0.5, ["a.py"])], 0.5))
    assert "snapshot" in text and "несравним" in text
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run: `.venv/bin/pytest tests/eval/test_replay_report.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'eval.solve_task_metrics.replay_report'`

- [ ] **Step 3: Написать минимальную реализацию**

Создать `eval/solve_task_metrics/replay_report.py`:

```python
"""Markdown-отчёт replay: агрегат, A/B-дельта и дельта по задачам (PRI-254)."""
from __future__ import annotations

from . import replay_history

AGGREGATE_ROWS = (
    ("core_recall_median", "core-recall (медиана)"),
    ("core_recall_mean", "core-recall (среднее)"),
    ("bulk_core_recall_median", "core-recall bulk (ядро ≥ 10)"),
    ("bulk_n_measured", "bulk N"),
    ("precision_median", "precision (медиана)"),
    ("predicted_median", "предсказано файлов (медиана)"),
    ("n_measured", "задач измерено"),
    ("no_measurement", "без точки измерения"),
)

DISCLAIMER = (
    "Линия `replay` и линия `snapshot` **несравнимы напрямую**: snapshot считает "
    "пути, которые отобрала LLM из выдачи ретрива, а replay — всю выдачу ретрива. "
    "Сравнивать можно только replay с replay."
)


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def _delta(old, new) -> str:
    if old is None or new is None:
        return "—"
    return f"{new - old:+.4g}"


def _identity(snapshot: dict, label: str) -> list:
    params = snapshot.get("variant_params")
    return [
        f"- **{label}**: вариант `{snapshot['variant']}`"
        + (f", параметры `{params}`" if params else "")
        + f", коммит `{snapshot.get('commit')}`"
        + f", indexed_sha `{snapshot.get('indexed_sha')}`"
        + f", корпус {snapshot.get('corpus')}"
        + (", **частичный (--limit)**" if snapshot.get("partial") else "")
    ]


def _task_delta_rows(new: dict, old: dict) -> tuple:
    """Строки дельты по задачам и число задач без изменений."""
    old_by_key = {row["key"]: row for row in old["tasks"]}
    rows: list = []
    unchanged = 0
    for row in new["tasks"]:
        before = old_by_key.get(row["key"])
        if before is None:
            continue
        old_recall = before.get("core_recall")
        new_recall = row.get("core_recall")
        gained = sorted(set(row["predicted_paths"]) - set(before["predicted_paths"]))
        lost = sorted(set(before["predicted_paths"]) - set(row["predicted_paths"]))
        if old_recall == new_recall and not gained and not lost:
            unchanged += 1
            continue
        magnitude = abs((new_recall or 0) - (old_recall or 0))
        rows.append((magnitude, [
            row["key"], row["status"], _fmt(old_recall), _fmt(new_recall),
            _delta(old_recall, new_recall),
            ", ".join(f"`{p}`" for p in gained) or "—",
            ", ".join(f"`{p}`" for p in lost) or "—",
        ]))
    rows.sort(key=lambda item: -item[0])
    return [cells for _, cells in rows], unchanged


def render(new: dict, old: dict | None = None) -> str:
    """Отчёт по прогону; при наличии второй стороны — с дельтами."""
    lines = [
        "# Replay-метрики ретрива solve-task",
        "",
        f"Прогон от {new['taken_at']}, репозиторий `{new.get('repo')}`, "
        f"ветка `{new.get('branch')}`.",
        "",
        "## Идентичность прогона",
        "",
    ]
    if old is not None:
        lines += _identity(old, "до")
    lines += _identity(new, "после" if old is not None else "прогон")
    lines.append("")

    if old is not None:
        warnings = replay_history.comparability_warnings(old, new)
        if warnings:
            lines += ["> **Стороны различаются не только вариантом:**", ""]
            lines += [f"> - {warning}" for warning in warnings]
            lines.append("")

    lines += ["## Агрегат", ""]
    if old is None:
        lines += ["| Метрика | Значение |", "|---|---|"]
        for key, label in AGGREGATE_ROWS:
            lines.append(f"| {label} | {_fmt(new['aggregate'].get(key))} |")
    else:
        lines += ["| Метрика | до | после | Δ |", "|---|---|---|---|"]
        for key, label in AGGREGATE_ROWS:
            before = old["aggregate"].get(key)
            after = new["aggregate"].get(key)
            lines.append(
                f"| {label} | {_fmt(before)} | {_fmt(after)} | {_delta(before, after)} |"
            )
    lines.append("")

    lines += ["## Статусы задач", "", "| Статус | Задач |", "|---|---|"]
    for status, count in new.get("statuses", {}).items():
        lines.append(f"| {status} | {count} |")
    lines.append("")

    if old is not None:
        rows, unchanged = _task_delta_rows(new, old)
        lines += [
            "## Дельта по задачам",
            "",
            "| Ключ | Статус | до | после | Δ | приобретено | потеряно |",
            "|---|---|---|---|---|---|---|",
        ]
        for cells in rows:
            lines.append("| " + " | ".join(cells) + " |")
        if unchanged:
            lines.append(f"| _и ещё {unchanged}_ | без изменений | — | — | — | — | — |")
        lines.append("")
    else:
        lines += [
            "## Задачи",
            "",
            "| Ключ | Статус | core-recall | предсказано | попало |",
            "|---|---|---|---|---|",
        ]
        for row in new["tasks"]:
            lines.append(
                f"| {row['key']} | {row['status']} | {_fmt(row.get('core_recall'))} "
                f"| {row['predicted']} | {row['hit_core']} |"
            )
        lines.append("")

    lines += ["## Оговорка", "", DISCLAIMER, ""]
    return "\n".join(lines)
```

- [ ] **Step 4: Прогнать тест и убедиться, что он проходит**

Run: `.venv/bin/pytest tests/eval/test_replay_report.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Закоммитить**

```bash
git add eval/solve_task_metrics/replay_report.py tests/eval/test_replay_report.py
git commit -m "feat(eval): отчёт A/B с дельтой по задачам и агрегату"
```

---

### Task 6: Живой провайдер ретрива

Единственный модуль харнесса, импортирующий `reviewer`. Плюс guard, что остальные его не тянут.

**Files:**
- Create: `eval/solve_task_metrics/live.py`
- Modify: `eval/solve_task_metrics/__init__.py` (докстринг)
- Test: `tests/eval/test_live_boundary.py`

**Interfaces:**
- Consumes: `variants.OVERRIDE_SECTIONS` (Task 2)
- Produces:
  - `LiveRetrieval` — контекст-менеджер; методы `preflight(repo, branch)`, `task(key)`, `query(task, key)`, `code(repo, branch, query, limits)`
  - `open_live(repo: str | None, branch: str | None) -> LiveRetrieval`
  - `limits_to_yaml(limits) -> dict` — сериализация `ContextLimits` в блок `context_limits`

- [ ] **Step 1: Написать падающий тест**

Создать `tests/eval/test_live_boundary.py`:

```python
"""Граница живых зависимостей харнесса (PRI-254).

reviewer импортирует ТОЛЬКО live.py: остальные модули обязаны оставаться
импортируемыми и тестируемыми без Postgres, Neo4j и Voyage.
"""
from __future__ import annotations

import pathlib
import re

MODULE_DIR = pathlib.Path(__file__).resolve().parents[2] / "eval" / "solve_task_metrics"

# Импорт reviewer по форме оператора, а не по подстроке: слово 'reviewer'
# встречается в прозе докстрингов и в путях-примерах.
IMPORT_RE = re.compile(r"^\s*(?:from\s+reviewer[\s.]|import\s+reviewer[\s.,]|import\s+reviewer$)", re.M)

# Ре-экспорты расчётного ядра (PRI-249) — не живые зависимости: чистые функции
# без ввода-вывода. Плюс live.py, который и есть объявленное исключение.
ALLOWED = {"briefs.py", "classify.py", "recall.py", "live.py"}


def test_only_live_module_imports_reviewer_at_module_level():
    offenders = [
        path.name
        for path in MODULE_DIR.glob("*.py")
        if path.name not in ALLOWED and IMPORT_RE.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], (
        f"живой импорт reviewer вне live.py: {offenders}; "
        "snapshot|stats|compare|forecast обязаны работать без инфраструктуры"
    )


def test_limits_to_yaml_roundtrips_through_production_parser():
    """Сериализация лимитов совместима с ContextLimits.from_review_yaml."""
    from reviewer.policy.context_limits import ContextLimits

    from eval.solve_task_metrics.live import limits_to_yaml

    original = ContextLimits()
    block = limits_to_yaml(original)
    assert ContextLimits.from_review_yaml({"context_limits": block}) == original


def test_limits_to_yaml_preserves_non_default_values():
    from reviewer.policy.context_limits import CodebaseLimits, ContextLimits

    from eval.solve_task_metrics.live import limits_to_yaml

    original = ContextLimits(search_codebase=CodebaseLimits(ceiling=25, ratio=0.4))
    block = limits_to_yaml(original)
    assert block["search_codebase"]["ceiling"] == 25
    assert block["search_codebase"]["ratio"] == 0.4
    assert ContextLimits.from_review_yaml({"context_limits": block}) == original
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run: `.venv/bin/pytest tests/eval/test_live_boundary.py -q`
Expected: FAIL — `ImportError: cannot import name 'limits_to_yaml'` (модуля `live` нет)

- [ ] **Step 3: Написать минимальную реализацию**

Создать `eval/solve_task_metrics/live.py`:

```python
"""Живой провайдер ретрива поверх компонентов reviewer (PRI-254).

ЕДИНСТВЕННЫЙ модуль харнесса с живыми зависимостями: Postgres, Neo4j, Voyage.
Остальные модули (в том числе replay.py) о нём не знают и тестируются на
фейках — источник ретрива приходит инъекцией, как run_git в build_snapshot.

Вызываются продакшн-методы MCPReviewService — те же, что дёргает
_TaskContextDeps при сборке контекста задачи. Своей копии пути ретрива здесь
не заводится, иначе replay мерил бы не то, что работает в проде.
"""
from __future__ import annotations

from dataclasses import asdict

from reviewer.app import build_components
from reviewer.config.branches import resolve_repo_branches
from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService
from reviewer.policy.context_limits import ContextLimits
from reviewer.services.status import build_status_report


def limits_to_yaml(limits: ContextLimits) -> dict:
    """Сериализовать ContextLimits в блок context_limits формата .review.yml.

    Нужна, чтобы оверрайд одного ключа не обнулял остальные: from_review_yaml
    добирает недостающие ключи из КЛАССОВЫХ дефолтов, а не из лимитов репо.
    """
    return {
        "search_codebase": asdict(limits.search_codebase),
        "search_tasks": asdict(limits.search_tasks),
        "graph": asdict(limits.graph),
    }


def _merge(base: dict, overrides: dict | None) -> dict:
    """Наложить оверрайды на блок лимитов посекционно."""
    if not overrides:
        return base
    merged = {section: dict(values) for section, values in base.items()}
    for section, values in overrides.items():
        merged.setdefault(section, {}).update(values)
    return merged


class LiveRetrieval:
    """Провайдер секций replay поверх живого MCPReviewService."""

    def __init__(self, settings: Settings, components, service: MCPReviewService):
        self._settings = settings
        self._components = components
        self._service = service

    # -- жизненный цикл ---------------------------------------------------

    def __enter__(self) -> "LiveRetrieval":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        """Закрыть пул Postgres и драйвер Neo4j."""
        self._components.close()

    # -- секции -----------------------------------------------------------

    def preflight(self, repo: str, branch: str) -> dict:
        """Состояние base-индекса ветки: часть идентичности снимка."""
        report = build_status_report(
            self._components.store, self._components.graph, repo, [branch],
            self._service._repo_clone_path(repo) or "",
            summary_store=getattr(self._components, "summary_store", None),
        )
        status = report.branches[0]
        return {
            "branch": status.branch,
            "indexed_sha": status.indexed_sha,
            "drift": status.drift,
            "summaries": status.summaries,
            "chunks": status.chunks,
            "graph_nodes": status.graph_nodes,
        }

    def task(self, key: str) -> dict | None:
        """Нормализованная задача из стора reviewer (не из брифа)."""
        return self._service.get_task(key)

    def query(self, task: dict | None, key: str) -> str:
        """Запрос ретрива продакшн-формулой, без своей копии.

        Копия формулы запроса — тот же класс дефекта, что PRI-249 запрещает
        для формул метрики: replay мерил бы не тот вход, что видит прод.
        """
        from reviewer.mcp.task_context import _query as production_query

        return production_query(task, key)

    def code(self, repo: str, branch: str, query: str, limits: dict | None) -> str:
        """Выдача ретрива по коду в том же виде, в каком её получает сборщик брифа.

        Без оверрайдов зовётся продакшн-метод search_codebase дословно. С
        оверрайдами приходится идти на уровень ниже (search_codebase не
        принимает лимиты параметром) — рендер при этом тот же as_context.
        """
        if not limits:
            return self._service.search_codebase(repo, query, None, branch, False)
        base = limits_to_yaml(self._service._resolve_context_limits(repo, branch))
        effective = ContextLimits.from_review_yaml(
            {"context_limits": _merge(base, limits)}
        )
        pack = self._components.retriever.search_base(
            repo, query, limits=effective.search_codebase,
            hops=effective.graph.hops, ceiling_override=None,
            branch=branch, include_tests=False,
        )
        return pack.as_context(line_numbers=True) or "(ничего не найдено)"


def open_live(repo: str | None = None, branch: str | None = None) -> tuple:
    """Собрать живой провайдер и вернуть (provider, repo, branch).

    repo по умолчанию — DEFAULT_REPO, ветка — первичная отслеживаемая.
    """
    settings = Settings()
    resolved_repo = repo or settings.default_repo
    if not resolved_repo:
        raise SystemExit(
            "не задан репозиторий: укажите --repo owner/name или DEFAULT_REPO"
        )
    if branch:
        resolved_branch = branch
    else:
        resolved_branch = resolve_repo_branches(
            resolved_repo, settings=settings
        ).primary
    components = build_components(settings)
    try:
        service = MCPReviewService(settings, components)
    except Exception:
        components.close()
        raise
    return LiveRetrieval(settings, components, service), resolved_repo, resolved_branch
```

- [ ] **Step 4: Обновить докстринг пакета**

В `eval/solve_task_metrics/__init__.py` заменить фразу про stdlib. Было:

```python
НЕ продакшн-путь reviewer: пакет живёт в eval/, использует только stdlib и
никогда не импортируется из reviewer/**. Расчётные модули (cost, classify,
recall, history, forecast) — чистые функции без ввода-вывода.
```

Стало:

```python
НЕ продакшн-путь reviewer: пакет живёт в eval/ и никогда не импортируется из
reviewer/**. Расчётные модули (cost, classify, recall, history, forecast) —
чистые функции без ввода-вывода и на stdlib.

Единственное исключение из stdlib-инварианта — live.py (PRI-254): режиму
replay нужен живой ретрив (Postgres, Neo4j, Voyage). Он импортируется лениво,
внутри тела команды, поэтому snapshot|stats|compare|forecast продолжают
работать без инфраструктуры. Границу стережёт tests/eval/test_live_boundary.py.
```

- [ ] **Step 5: Прогнать тест и убедиться, что он проходит**

Run: `.venv/bin/pytest tests/eval/test_live_boundary.py -q`
Expected: PASS (3 passed)

- [ ] **Step 6: Закоммитить**

```bash
git add eval/solve_task_metrics/live.py eval/solve_task_metrics/__init__.py tests/eval/test_live_boundary.py
git commit -m "feat(eval): живой провайдер ретрива и граница зависимостей харнесса"
```

---

### Task 7: Подкоманда CLI, документация и интеграционный прогон

**Files:**
- Modify: `eval/solve_task_metrics/__main__.py`
- Modify: `tests/eval/test_docs.py:16-22`
- Modify: `README.md`, `README.ru.md`
- Modify: `CLAUDE.md`
- Test: `tests/eval/test_replay_cli.py`

**Interfaces:**
- Consumes: `replay.run_replay`, `replay_history.{append,load,select}`, `replay_report.render`, `variants.{parse_overrides,get_variant,UnknownVariant,BadOverride}`, `live.open_live`
- Produces: подкоманда `replay`; функция `cmd_replay(args) -> int`

- [ ] **Step 1: Написать падающий тест**

Создать `tests/eval/test_replay_cli.py`:

```python
"""Подкоманда replay: парсинг аргументов и отказы (PRI-254)."""
from __future__ import annotations

import pytest

from eval.solve_task_metrics import __main__ as cli


def test_replay_parses_variant_and_overrides():
    parser = cli.build_parser()
    args = parser.parse_args(
        ["replay", "--variant", "limits", "--set", "search_codebase.ceiling=25",
         "--limit", "3", "--repo", "o/n", "--branch", "dev"]
    )
    assert args.command == "replay"
    assert args.variant == "limits"
    assert args.set == ["search_codebase.ceiling=25"]
    assert args.limit == 3 and args.repo == "o/n" and args.branch == "dev"


def test_replay_defaults_to_baseline_variant():
    args = cli.build_parser().parse_args(["replay"])
    assert args.variant == "baseline"
    assert args.set == [] and args.limit is None and args.baseline is None


@pytest.mark.parametrize("command", ["snapshot", "stats", "compare", "forecast", "steps"])
def test_existing_subcommands_still_parse(command):
    """Критерий 4: существующие команды не тронуты."""
    assert cli.build_parser().parse_args([command]).command == command


def test_unknown_variant_is_reported_without_touching_infrastructure(capsys):
    args = cli.build_parser().parse_args(["replay", "--variant", "нет-такого"])
    assert cli.cmd_replay(args) == 1
    assert "нет-такого" in capsys.readouterr().out


def test_malformed_override_is_reported_without_touching_infrastructure(capsys):
    args = cli.build_parser().parse_args(["replay", "--variant", "limits", "--set", "ceiling=25"])
    assert cli.cmd_replay(args) == 1
    assert "ceiling=25" in capsys.readouterr().out
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run: `.venv/bin/pytest tests/eval/test_replay_cli.py -q`
Expected: FAIL — `AttributeError: module 'eval.solve_task_metrics.__main__' has no attribute 'build_parser'`

- [ ] **Step 3: Выделить построение парсера и добавить подкоманду**

В `eval/solve_task_metrics/__main__.py`:

1. В шапке добавить пути и импорты (рядом с существующими константами):

```python
from . import (
    endtoend, forecast, ground_truth, history, replay as replay_mod,
    replay_history, replay_report, report, snapshot as snapshot_mod, steps,
    variants,
)

REPLAY_HISTORY_PATH = EVAL_DIR / replay_history.HISTORY_PATH_NAME
REPLAY_REPORT_PATH = EVAL_DIR / "replay_report.md"
```

(строка `from . import endtoend, forecast, ...` на 12-й строке заменяется целиком)

2. Перед `def main(...)` добавить команду:

```python
def _replay_side(provider, args, run_git, commit, taken_at, repo, branch,
                 variant_name, limits) -> dict:
    """Прогнать одну сторону A/B и сохранить снимок."""
    target = variants.ReplayTarget(repo=repo, branch=branch, limits=limits)
    snap = replay_mod.run_replay(
        provider=provider, run_git=run_git, briefs_dir=BRIEFS_DIR,
        target=target, variant_name=variant_name, commit=commit,
        taken_at=taken_at, limit=args.limit,
    )
    replay_history.append(REPLAY_HISTORY_PATH, snap)
    return snap


def cmd_replay(args) -> int:
    """Прогнать ретрив по корпусу и сравнить варианты (PRI-254)."""
    try:
        variants.get_variant(args.variant)
        limits = variants.parse_overrides(args.set)
    except (variants.UnknownVariant, variants.BadOverride) as error:
        print(str(error))
        return 1
    if args.variant == "limits" and not limits:
        print("вариант 'limits' требует хотя бы один --set <раздел>.<ключ>=<значение>")
        return 1

    baseline_snap = None
    if args.baseline:
        try:
            baseline_snap = replay_history.select(
                replay_history.load(REPLAY_HISTORY_PATH), args.baseline
            )
        except (replay_history.SnapshotNotFound,
                replay_history.PartialSnapshotRejected) as error:
            print(str(error))
            return 1

    from . import live  # ленивый импорт: живые зависимости только здесь

    run_git = ground_truth.git_runner(REPO_ROOT)
    commit = _head_commit(run_git)
    taken_at = dt.datetime.now(dt.timezone.utc).isoformat()
    provider, repo, branch = live.open_live(args.repo, args.branch)
    try:
        if baseline_snap is None and args.variant != "baseline":
            print("Прогон стороны «до» (baseline)…")
            baseline_snap = _replay_side(
                provider, args, run_git, commit, taken_at, repo, branch,
                "baseline", None,
            )
        print(f"Прогон варианта «{args.variant}»…")
        snap = _replay_side(
            provider, args, run_git, commit, taken_at, repo, branch,
            args.variant, limits,
        )
    finally:
        provider.close()

    REPLAY_REPORT_PATH.write_text(
        replay_report.render(snap, baseline_snap), encoding="utf-8"
    )
    print(f"Снимок сохранён: {REPLAY_HISTORY_PATH}")
    print(f"Отчёт записан: {REPLAY_REPORT_PATH}")
    aggregate = snap["aggregate"]
    print(
        f"core-recall медиана: {aggregate['core_recall_median']}, "
        f"N={aggregate['n_measured']}, "
        f"bulk={aggregate['bulk_core_recall_median']} (N={aggregate['bulk_n_measured']})"
    )
    if snap["partial"]:
        print("Снимок частичный (--limit): как сторона сравнения не годится.")
    return 0
```

3. Разделить `main` на `build_parser` и `main`. Тело `main` от `parser = argparse.ArgumentParser(` до `forecast_parser.add_argument(...)` включительно переносится в новую функцию, возвращающую `parser`; в неё же добавляется блок `replay`:

```python
def build_parser() -> argparse.ArgumentParser:
    """Собрать парсер CLI (выделено, чтобы разбор аргументов тестировался)."""
    parser = argparse.ArgumentParser(
        prog="python -m eval.solve_task_metrics",
        description="Офлайн-метрики этапа solve-task: цена, качество ретрива, тренд.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    # … существующие подпарсеры snapshot/stats/compare/steps/forecast без изменений …
    replay_parser = subparsers.add_parser(
        "replay",
        help="прогнать ретрив по корпусу заново и сравнить варианты (A/B)",
    )
    replay_parser.add_argument(
        "--variant", default="baseline",
        help=f"вариант ретрива: {', '.join(variants.VARIANT_NAMES)}",
    )
    replay_parser.add_argument(
        "--set", action="append", default=[], metavar="РАЗДЕЛ.КЛЮЧ=ЗНАЧЕНИЕ",
        help="оверрайд лимитов для варианта limits (повторяемый)",
    )
    replay_parser.add_argument(
        "--baseline", default=None, metavar="ССЫЛКА",
        help="переиспользовать сохранённый снимок как сторону «до»: "
             "last, -N или имя варианта",
    )
    replay_parser.add_argument(
        "--limit", type=int, default=None,
        help="усечь корпус (снимок помечается частичным)",
    )
    replay_parser.add_argument("--repo", default=None, help="owner/name; по умолчанию DEFAULT_REPO")
    replay_parser.add_argument("--branch", default=None, help="ветка; по умолчанию первичная")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "snapshot":
        return cmd_snapshot(args)
    if args.command == "stats":
        return cmd_stats(args)
    if args.command == "forecast":
        return cmd_forecast(args)
    if args.command == "steps":
        return cmd_steps(args)
    if args.command == "replay":
        return cmd_replay(args)
    return cmd_compare(args)
```

- [ ] **Step 4: Прогнать тест и убедиться, что он проходит**

Run: `.venv/bin/pytest tests/eval/test_replay_cli.py -q`
Expected: PASS (9 passed)

- [ ] **Step 5: Обновить guard документации**

В `tests/eval/test_docs.py` расширить кортеж подкоманд:

```python
        for subcommand in ("snapshot", "stats", "compare", "forecast", "replay"):
```

- [ ] **Step 6: Задокументировать `replay` в обоих README**

Найти в `README.md` и `README.ru.md` блок с `python -m eval.solve_task_metrics` и добавить строку. В `README.ru.md`:

```
python -m eval.solve_task_metrics replay                                    # прогнать ретрив по корпусу заново (baseline)
python -m eval.solve_task_metrics replay --variant limits --set search_codebase.ceiling=25 --baseline last   # A/B против сохранённого снимка
```

В `README.md` — те же строки с английскими комментариями:

```
python -m eval.solve_task_metrics replay                                    # re-run retrieval over the corpus (baseline)
python -m eval.solve_task_metrics replay --variant limits --set search_codebase.ceiling=25 --baseline last   # A/B against a stored snapshot
```

- [ ] **Step 7: Исправить число в CLAUDE.md**

В `CLAUDE.md`, в абзаце про онлайн-метрику качества брифа (PRI-249), заменить

```
сырой recall на том же корпусе давал медиану 15 % против 67 %
```

на

```
сырой recall на том же корпусе давал медиану 18 % против 61 %
```

и дописать в конец того же предложения-скобки: `(числа спайка eval/pri246_report.md)`.

Обоснование: спайк даёт 18 % сырого и 61 % core; ни один артефакт репозитория не даёт 67 %.

- [ ] **Step 8: Прогнать весь unit-набор**

Run: `.venv/bin/pytest -q`
Expected: PASS, без падений. Особенно `tests/metrics/test_reexport_guard.py` (критерий 3) и `tests/eval/test_docs.py`.

- [ ] **Step 9: Добавить integration-тест живого прогона**

Дописать в `tests/eval/test_live_boundary.py`:

```python
@pytest.mark.integration
def test_live_replay_smoke():
    """Живой прогон трёх задач: компоненты собираются, ретрив отдаёт пути."""
    import datetime as dt

    from eval.solve_task_metrics import ground_truth, replay, variants
    from eval.solve_task_metrics.live import open_live
    from eval.solve_task_metrics.__main__ import BRIEFS_DIR, REPO_ROOT

    provider, repo, branch = open_live(None, None)
    try:
        snap = replay.run_replay(
            provider=provider,
            run_git=ground_truth.git_runner(REPO_ROOT),
            briefs_dir=BRIEFS_DIR,
            target=variants.ReplayTarget(repo=repo, branch=branch, limits=None),
            variant_name="baseline",
            commit="test",
            taken_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            limit=3,
        )
    finally:
        provider.close()

    assert snap["partial"] is True
    assert snap["corpus"] == 3
    assert snap["indexed_sha"]
    assert any(row["predicted_paths"] for row in snap["tasks"])
```

Не забыть `import pytest` в шапке файла.

- [ ] **Step 10: Прогнать линт и закоммитить**

```bash
.venv/bin/ruff check eval/solve_task_metrics tests/eval
git add eval/solve_task_metrics/__main__.py tests/eval/test_replay_cli.py \
        tests/eval/test_docs.py tests/eval/test_live_boundary.py \
        README.md README.ru.md CLAUDE.md
git commit -m "feat(eval): подкоманда replay, документация и живой smoke-прогон"
```

---

## Финальная приёмка

- [ ] **Полный unit-набор зелёный:** `.venv/bin/pytest -q`
- [ ] **Guard PRI-249 зелёный** (критерий приёмки 3): `.venv/bin/pytest tests/metrics/test_reexport_guard.py -q`
- [ ] **Существующие команды не тронуты** (критерий 4): `python -m eval.solve_task_metrics stats --last 3` работает без Postgres
- [ ] **Живой прогон baseline** на полном корпусе: `python -m eval.solve_task_metrics replay`; сверить порядок величин с baseline харнесса (0.5556 / 0.373 / 4) — точного совпадения **не ожидается**, replay меряет сырую выдачу ретрива, а не отобранные LLM пути; расхождение фиксируется в отчёте
- [ ] **Живой A/B:** `python -m eval.solve_task_metrics replay --variant limits --set search_codebase.ceiling=25 --baseline last` — отчёт содержит дельту по задачам и по агрегату (критерий 2)
- [ ] **Воспроизводимость** (критерий 4): два подряд прогона `replay` на одном коммите и одном `indexed_sha` дают одинаковый агрегат
