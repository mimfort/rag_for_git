# Онлайн-метрика качества брифа solve-task (PRI-249) — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** по факту публикации ревью автоматически сопоставлять пути секции `## Relevant code` брифа задачи с реально изменёнными файлами PR, считать core-recall/precision с таксономией промахов, сохранять в Postgres и показывать динамику в веб-админке.

**Architecture:** расчётное ядро переносится из офлайн-харнесса `eval/solve_task_metrics/` в `reviewer/metrics/brief_quality/` (одна копия формул на офлайн и онлайн, `eval/` становится ре-экспортом). Сервис-адаптер `reviewer/services/brief_quality.py` читает бриф из локального клона (канал `repo_clone`, PRI-235) и строит измерение из `PreparedReview.changed_paths` / `changed_status`. `publish_review` вызывает его fail-soft после записи истории, `ReviewHistory` пишет строку в новую таблицу `brief_quality` и отдаёт агрегаты в `/api/quality`, страница `Quality.tsx` рисует динамику.

**Tech Stack:** Python 3.11+, psycopg 3 / psycopg_pool, FastAPI, pytest; фронт — React + TypeScript + recharts + react-router-dom.

**Spec:** `docs/superpowers/specs/2026-08-14-pri-249-brief-quality-metric-design.md`

## Global Constraints

- Язык проекта — русский: комментарии, докстринги, сообщения. Новый код пишется в этом же стиле.
- Коммиты — Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By` и упоминаний Claude).
- `reviewer/**` НИКОГДА не импортирует `eval/**`. После этого плана направление зависимости — только `eval/ → reviewer/`.
- Unit-тесты запрещают внешние и localhost-сокеты. Любой тест с реальной БД обязан иметь `@pytest.mark.integration`.
- Прогон unit-тестов: `.venv/bin/pytest -q`. Прогон integration: `.venv/bin/pytest -q -m integration` (нужны `docker compose --profile test up -d --wait paradedb-test neo4j-test`).
- Вся новая логика fail-soft: ни один сбой метрики не меняет результат `publish_review` и не роняет ревью.
- Формула знаменателя ядра обязана совпадать с офлайн-снимком `eval/solve_task_metrics/snapshot.py:84-88` — иначе baseline PRI-251 (`bulk_core_recall_median ≈ 0.373`, `bulk_n_measured = 4`) несопоставим с онлайн-числами.
- Порог bulk-подвыборки — `BULK_CORE_THRESHOLD = 10`, значение берётся из общего модуля, не дублируется числом.
- Ветка работы: `feat/pri-249-brief-quality-metric` (уже создана, спека закоммичена в `48f158a`).

---

## Структура файлов

**Создаются:**

| Файл | Ответственность |
|---|---|
| `reviewer/metrics/__init__.py` | пакет метрик (пустой докстринг-модуль) |
| `reviewer/metrics/brief_quality/__init__.py` | докстринг подсистемы; ничего не реэкспортирует |
| `reviewer/metrics/brief_quality/classify.py` | `is_core_production_path`, `categorize_miss` (перенос из `eval/`) |
| `reviewer/metrics/brief_quality/recall.py` | `TaskQuality`, `QualityAggregate`, `evaluate_task`, `aggregate`, `BULK_CORE_THRESHOLD` (перенос) |
| `reviewer/metrics/brief_quality/briefs.py` | парсер брифа (перенос) + новая `has_section` |
| `reviewer/services/brief_quality.py` | адаптер: найти бриф → построить множества → посчитать → `BriefQualityMeasurement` |
| `web/frontend/src/pages/Quality.tsx` | страница динамики метрики |
| `tests/metrics/__init__.py`, `tests/metrics/test_classify.py`, `test_recall.py`, `test_briefs.py`, `test_bulk_subsample.py` | переезжают из `tests/eval/` |
| `tests/metrics/test_reexport_guard.py` | guard: `eval/` реэкспортирует продакшн-модуль, а не свою копию |
| `tests/services/test_brief_quality.py` | тесты адаптера |

**Модифицируются:**

| Файл | Что меняется |
|---|---|
| `eval/solve_task_metrics/classify.py`, `recall.py`, `briefs.py` | тело заменяется на ре-экспорт из `reviewer.metrics.brief_quality.*` |
| `eval/solve_task_metrics/__init__.py` | докстринг: перенос выполнен, здесь ре-экспорт |
| `reviewer/web/schema.sql` | новая таблица `brief_quality` + индексы |
| `reviewer/web/history.py` | `record_brief_quality`, `brief_quality_trend` |
| `reviewer/web/api.py` | `GET /api/quality` |
| `reviewer/mcp/service.py` | вызов метрики в `publish_review` + приватный `_record_brief_quality` |
| `web/frontend/src/api.ts` | типы `QualityPoint`/`QualityStats` + `fetchQuality` |
| `web/frontend/src/App.tsx` | роут и пункт навигации «Качество» |
| `tests/web/test_history.py`, `tests/web/test_api.py`, `tests/mcp/test_publish.py` | новые тесты |
| `CLAUDE.md`, `README.md`, `README.ru.md` | документация |

---

## Task 1: Общее расчётное ядро в `reviewer/metrics/brief_quality/`

Перенести чистые расчётные модули из офлайн-харнесса в продакшн-слой и оставить в `eval/` ре-экспорт, чтобы формула метрики существовала в одной копии. Это предписано докстрингом `eval/solve_task_metrics/__init__.py`.

**Files:**
- Create: `reviewer/metrics/__init__.py`, `reviewer/metrics/brief_quality/__init__.py`, `reviewer/metrics/brief_quality/classify.py`, `reviewer/metrics/brief_quality/recall.py`, `reviewer/metrics/brief_quality/briefs.py`
- Modify: `eval/solve_task_metrics/classify.py`, `eval/solve_task_metrics/recall.py`, `eval/solve_task_metrics/briefs.py`, `eval/solve_task_metrics/__init__.py`
- Test: `tests/metrics/__init__.py`, `tests/metrics/test_classify.py`, `tests/metrics/test_recall.py`, `tests/metrics/test_briefs.py`, `tests/metrics/test_bulk_subsample.py` (переезд из `tests/eval/`), `tests/metrics/test_reexport_guard.py` (новый)

**Interfaces:**
- Consumes: ничего (первая задача).
- Produces:
  - `reviewer.metrics.brief_quality.classify.is_core_production_path(path: str) -> bool`
  - `reviewer.metrics.brief_quality.classify.categorize_miss(path: str, existed_before: bool) -> str`
  - `reviewer.metrics.brief_quality.classify.NEW_FILE_CATEGORY: str`
  - `reviewer.metrics.brief_quality.recall.BULK_CORE_THRESHOLD: int` (= 10)
  - `reviewer.metrics.brief_quality.recall.TaskQuality` (dataclass: `task_key`, `expected`, `expected_core`, `predicted`, `hit_core`, `core_recall`, `raw_recall`, `precision`)
  - `reviewer.metrics.brief_quality.recall.QualityAggregate` (dataclass: `n_measured`, `no_measurement`, `core_recall_median`, `core_recall_mean`, `raw_recall_median`, `denominator_median`, `bulk_core_recall_median`, `bulk_n_measured`)
  - `reviewer.metrics.brief_quality.recall.evaluate_task(task_key: str, predicted: set, expected: set, expected_core: set) -> TaskQuality`
  - `reviewer.metrics.brief_quality.recall.aggregate(rows: list) -> QualityAggregate`
  - `reviewer.metrics.brief_quality.briefs.extract_section_paths(text: str, header: str) -> set[str]`
  - `reviewer.metrics.brief_quality.briefs.extract_task_key(filename: str) -> str | None`
  - `reviewer.metrics.brief_quality.briefs.has_section(text: str, header: str) -> bool` (новая)
  - `reviewer.metrics.brief_quality.briefs.RELEVANT_HEADER: str` (= `"## Relevant code"`), `TEST_HEADER`, `TOKENS_HEADER`

- [ ] **Step 1: Написать падающий guard-тест на ре-экспорт**

Создать `tests/metrics/__init__.py` (пустой) и `tests/metrics/test_reexport_guard.py`:

```python
"""Guard: расчётное ядро метрики живёт в одной копии (PRI-249).

Офлайн-харнесс eval/solve_task_metrics обязан РЕЭКСПОРТИРОВАТЬ продакшн-модуль,
а не держать вторую реализацию формул: иначе онлайн- и офлайн-числа разъедутся
незаметно, и сравнение «до/после» (критерий 4 PRI-251) перестанет быть валидным.
"""
from __future__ import annotations

from eval.solve_task_metrics import briefs as eval_briefs
from eval.solve_task_metrics import classify as eval_classify
from eval.solve_task_metrics import recall as eval_recall
from reviewer.metrics.brief_quality import briefs as prod_briefs
from reviewer.metrics.brief_quality import classify as prod_classify
from reviewer.metrics.brief_quality import recall as prod_recall


def test_eval_reexports_production_objects():
    """Объекты офлайн-харнесса — те же самые объекты, что в reviewer/."""
    assert eval_classify.is_core_production_path is prod_classify.is_core_production_path
    assert eval_classify.categorize_miss is prod_classify.categorize_miss
    assert eval_recall.evaluate_task is prod_recall.evaluate_task
    assert eval_recall.aggregate is prod_recall.aggregate
    assert eval_recall.TaskQuality is prod_recall.TaskQuality
    assert eval_recall.BULK_CORE_THRESHOLD == prod_recall.BULK_CORE_THRESHOLD
    assert eval_briefs.extract_section_paths is prod_briefs.extract_section_paths
    assert eval_briefs.extract_task_key is prod_briefs.extract_task_key


def test_production_core_does_not_import_eval():
    """reviewer/** не тянет eval/** — инвариант направления зависимости.

    Проверяется по форме оператора импорта, а не по подстроке: 'eval' встречается
    в прозе докстрингов и в literal_eval, и подстрочная проверка красила бы тест
    на ровном месте.
    """
    import pathlib
    import re

    import_re = re.compile(r"^\s*(?:from\s+eval[\s.]|import\s+eval[\s.,]|import\s+eval$)", re.M)
    root = pathlib.Path(prod_classify.__file__).resolve().parents[3]
    offenders = [
        str(path.relative_to(root))
        for path in (root / "reviewer").rglob("*.py")
        if import_re.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run: `.venv/bin/pytest -q tests/metrics/test_reexport_guard.py`
Expected: FAIL с `ModuleNotFoundError: No module named 'reviewer.metrics'`

- [ ] **Step 3: Перенести модули в продакшн-слой (git mv, содержимое без правок)**

```bash
mkdir -p reviewer/metrics/brief_quality
git mv eval/solve_task_metrics/classify.py reviewer/metrics/brief_quality/classify.py
git mv eval/solve_task_metrics/recall.py   reviewer/metrics/brief_quality/recall.py
git mv eval/solve_task_metrics/briefs.py   reviewer/metrics/brief_quality/briefs.py
```

Создать `reviewer/metrics/__init__.py`:

```python
"""Метрики качества самого reviewer (не метрики ревьюируемого кода)."""
```

Создать `reviewer/metrics/brief_quality/__init__.py`:

```python
"""Качество ретрива под бриф solve-task: core-recall, precision, таксономия промахов.

Расчётное ядро общее для двух потребителей: онлайн-съёма на publish_review
(PRI-249) и офлайн-харнесса eval/solve_task_metrics (PRI-250). Модули чистые —
без ввода-вывода, без git, без БД: это условие того, что офлайн и онлайн меряют
ОДНОЙ линейкой и числа «до/после» сравнимы.
"""
```

- [ ] **Step 4: Добавить `has_section` в перенесённый парсер**

В `reviewer/metrics/brief_quality/briefs.py` рядом с `extract_section_paths` добавить:

```python
def has_section(text: str, header: str) -> bool:
    """Есть ли в тексте секция с таким заголовком.

    Нужна, чтобы отличить «бриф без секции ## Relevant code» (бриф непригоден
    как источник предсказаний) от «секция есть, но пуста» (валидное измерение
    с predicted=0). extract_section_paths оба случая отдаёт пустым множеством.
    """
    return any(line.strip() == header for line in text.splitlines())
```

- [ ] **Step 5: Заменить модули в `eval/` на ре-экспорт**

`eval/solve_task_metrics/classify.py`:

```python
"""Ре-экспорт расчётного ядра из reviewer/ (перенос по PRI-249).

Формулы живут в reviewer.metrics.brief_quality.classify: их делят офлайн-харнесс
и онлайн-съём метрики, и второй копии у них быть не должно.
"""
from reviewer.metrics.brief_quality.classify import (  # noqa: F401
    NEW_FILE_CATEGORY,
    categorize_miss,
    is_core_production_path,
)
```

`eval/solve_task_metrics/recall.py`:

```python
"""Ре-экспорт расчётного ядра из reviewer/ (перенос по PRI-249)."""
from reviewer.metrics.brief_quality.recall import (  # noqa: F401
    BULK_CORE_THRESHOLD,
    QualityAggregate,
    TaskQuality,
    aggregate,
    evaluate_task,
)
```

`eval/solve_task_metrics/briefs.py`:

```python
"""Ре-экспорт парсера брифов из reviewer/ (перенос по PRI-249)."""
from reviewer.metrics.brief_quality.briefs import (  # noqa: F401
    BUCKET_KEYS,
    RELEVANT_HEADER,
    SIDECHAIN_MARK,
    TEST_HEADER,
    TOKENS_HEADER,
    BriefRecord,
    TokenBlock,
    extract_section_paths,
    extract_task_key,
    has_section,
    load_briefs,
    parse_human_tokens,
    parse_token_block,
)
```

В `eval/solve_task_metrics/__init__.py` заменить последний абзац докстринга (про «следует ПЕРЕНЕСТИ») на:

```
Расчётные модули (classify, recall, briefs) ПЕРЕНЕСЕНЫ в reviewer/metrics/
brief_quality (PRI-249) и здесь только реэкспортируются: онлайн-съём метрики и
этот харнесс обязаны мерить одной линейкой. Остальные модули (cost, ground_truth,
history, snapshot, report, forecast, endtoend) офлайн-специфичны и живут здесь.
```

- [ ] **Step 6: Перенести тесты расчётного ядра**

```bash
git mv tests/eval/test_classify.py       tests/metrics/test_classify.py
git mv tests/eval/test_recall.py         tests/metrics/test_recall.py
git mv tests/eval/test_briefs.py         tests/metrics/test_briefs.py
git mv tests/eval/test_bulk_subsample.py tests/metrics/test_bulk_subsample.py
```

В каждом из перенесённых файлов заменить импорты `from eval.solve_task_metrics import X` на `from reviewer.metrics.brief_quality import X` (и соответствующие `eval.solve_task_metrics.X` → `reviewer.metrics.brief_quality.X`). Ничего кроме импортов не трогать: тела тестов должны остаться дословными, иначе теряется доказательство, что перенос ничего не изменил.

- [ ] **Step 7: Прогнать тесты**

Run: `.venv/bin/pytest -q tests/metrics tests/eval`
Expected: PASS, все тесты (перенесённые, guard и оставшиеся офлайн-тесты) зелёные.

- [ ] **Step 8: Убедиться, что офлайн-харнесс по-прежнему работает**

Run: `.venv/bin/ruff check reviewer/metrics eval tests/metrics`
Expected: без ошибок.

- [ ] **Step 9: Коммит**

```bash
git add reviewer/metrics eval/solve_task_metrics tests/metrics tests/eval
git commit -m "refactor(metrics): перенести расчётное ядро метрики брифа в reviewer/ (PRI-249)"
```

---

## Task 2: Сервис-адаптер `reviewer/services/brief_quality.py`

Единственный модуль метрики с вводом-выводом: находит файл брифа в локальном клоне и превращает `(changed_paths, changed_status)` в измерение. Не знает ни про Postgres, ни про MCP.

**Files:**
- Create: `reviewer/services/brief_quality.py`
- Test: `tests/services/test_brief_quality.py`

**Interfaces:**
- Consumes: `reviewer.metrics.brief_quality.{classify,recall,briefs}` из Task 1.
- Produces:
  - `reviewer.services.brief_quality.BRIEFS_DIR: str` (= `"docs/superpowers/briefs"`)
  - `reviewer.services.brief_quality.BriefQualityMeasurement` — frozen dataclass с полями `status: str`, `task_key: str | None`, `brief_path: str | None`, `expected: int`, `expected_core: int`, `predicted: int`, `hit_core: int`, `core_recall: float | None`, `raw_recall: float | None`, `precision: float | None`, `misses: dict[str, int]`, `predicted_paths: tuple[str, ...]`, `expected_core_paths: tuple[str, ...]`, `hit_core_paths: tuple[str, ...]`
  - `reviewer.services.brief_quality.find_brief(clone_path: str | None, task_key: str) -> pathlib.Path | None`
  - `reviewer.services.brief_quality.measure(*, task_key: str | None, clone_path: str | None, changed_paths: list[str], changed_status: dict[str, str]) -> BriefQualityMeasurement`

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/services/test_brief_quality.py`:

```python
"""Тесты адаптера онлайн-метрики качества брифа (PRI-249).

Файловая система — только tmp_path; ни БД, ни git, ни сети.
"""
from __future__ import annotations

import pytest

from reviewer.services.brief_quality import BRIEFS_DIR, find_brief, measure

_BRIEF = """# Brief — PRI-999 Тестовая задача

## Relevant code
- `reviewer/mcp/service.py:100` — точка съёма
- `reviewer/web/history.py:94` — запись истории
- `docs/superpowers/specs/old.md` — не ядро
(dropped 3: не информируют реализацию)

## Test exemplars
- `tests/web/test_history.py:84` — образец
"""


def _clone(tmp_path, name="2026-08-14-PRI-999-test.md", text=_BRIEF):
    briefs = tmp_path / BRIEFS_DIR
    briefs.mkdir(parents=True, exist_ok=True)
    (briefs / name).write_text(text, encoding="utf-8")
    return str(tmp_path)


def test_measured_matches_offline_formula(tmp_path):
    """core-recall считается по знаменателю «ядро И существовал до PR»."""
    clone = _clone(tmp_path)
    result = measure(
        task_key="PRI-999",
        clone_path=clone,
        changed_paths=[
            "reviewer/mcp/service.py",      # ядро, существовал, предсказан
            "reviewer/web/history.py",      # ядро, существовал, предсказан
            "reviewer/web/api.py",          # ядро, существовал, НЕ предсказан
            "reviewer/metrics/new.py",      # ядро, но новый файл → вне знаменателя
            "tests/web/test_api.py",        # не ядро
            "README.md",                    # не ядро
        ],
        changed_status={
            "reviewer/mcp/service.py": "modified",
            "reviewer/web/history.py": "modified",
            "reviewer/web/api.py": "modified",
            "reviewer/metrics/new.py": "added",
            "tests/web/test_api.py": "added",
            "README.md": "modified",
        },
    )
    assert result.status == "measured"
    assert result.task_key == "PRI-999"
    assert result.brief_path == f"{BRIEFS_DIR}/2026-08-14-PRI-999-test.md"
    assert result.expected == 6
    assert result.expected_core == 3
    assert result.hit_core == 2
    assert result.core_recall == pytest.approx(2 / 3)
    assert set(result.expected_core_paths) == {
        "reviewer/mcp/service.py",
        "reviewer/web/history.py",
        "reviewer/web/api.py",
    }
    assert set(result.hit_core_paths) == {
        "reviewer/mcp/service.py",
        "reviewer/web/history.py",
    }


def test_misses_are_categorized(tmp_path):
    """Каждый непредсказанный файл попадает в именованную категорию."""
    clone = _clone(tmp_path)
    result = measure(
        task_key="PRI-999",
        clone_path=clone,
        changed_paths=["reviewer/web/api.py", "tests/web/test_api.py", "reviewer/metrics/new.py"],
        changed_status={
            "reviewer/web/api.py": "modified",
            "tests/web/test_api.py": "modified",
            "reviewer/metrics/new.py": "added",
        },
    )
    assert result.misses["reviewer/web"] == 1
    assert result.misses["tests/"] == 1
    assert result.misses["новый файл (не существовал до PR)"] == 1


def test_dropped_line_is_not_a_path(tmp_path):
    """Служебная строка '(dropped N: …)' не попадает в predicted."""
    clone = _clone(tmp_path)
    result = measure(
        task_key="PRI-999",
        clone_path=clone,
        changed_paths=["reviewer/web/api.py"],
        changed_status={"reviewer/web/api.py": "modified"},
    )
    assert all("dropped" not in path for path in result.predicted_paths)


def test_empty_core_denominator_is_not_zero_recall(tmp_path):
    """Diff только из тестов и доков — «нет точки измерения», а не ноль."""
    clone = _clone(tmp_path)
    result = measure(
        task_key="PRI-999",
        clone_path=clone,
        changed_paths=["tests/web/test_api.py", "README.md"],
        changed_status={"tests/web/test_api.py": "modified", "README.md": "modified"},
    )
    assert result.status == "empty_core_denominator"
    assert result.core_recall is None


def test_no_task_key(tmp_path):
    result = measure(task_key=None, clone_path=_clone(tmp_path), changed_paths=[], changed_status={})
    assert result.status == "no_task_key"


def test_no_brief_without_clone():
    result = measure(
        task_key="PRI-999", clone_path=None,
        changed_paths=["reviewer/web/api.py"], changed_status={"reviewer/web/api.py": "modified"},
    )
    assert result.status == "no_brief"


def test_no_brief_when_key_not_found(tmp_path):
    result = measure(
        task_key="PRI-111", clone_path=_clone(tmp_path),
        changed_paths=["reviewer/web/api.py"], changed_status={"reviewer/web/api.py": "modified"},
    )
    assert result.status == "no_brief"


def test_brief_without_relevant_section_is_unreadable(tmp_path):
    clone = _clone(tmp_path, text="# Brief — PRI-999\n\n## Task\nбез секции кода\n")
    result = measure(
        task_key="PRI-999", clone_path=clone,
        changed_paths=["reviewer/web/api.py"], changed_status={"reviewer/web/api.py": "modified"},
    )
    assert result.status == "brief_unreadable"


def test_latest_brief_wins_on_duplicate_keys(tmp_path):
    """Несколько брифов на один ключ → берётся лексикографически последний."""
    clone = _clone(tmp_path, name="2026-08-10-PRI-999-old.md")
    _clone(tmp_path, name="2026-08-14-PRI-999-new.md")
    found = find_brief(clone, "PRI-999")
    assert found is not None and found.name == "2026-08-14-PRI-999-new.md"


def test_key_match_is_case_insensitive(tmp_path):
    clone = _clone(tmp_path, name="2026-08-14-pri-999-lower.md")
    assert find_brief(clone, "PRI-999") is not None
```

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run: `.venv/bin/pytest -q tests/services/test_brief_quality.py`
Expected: FAIL с `ModuleNotFoundError: No module named 'reviewer.services.brief_quality'`

- [ ] **Step 3: Реализовать адаптер**

Создать `reviewer/services/brief_quality.py`:

```python
"""Онлайн-съём качества ретрива под бриф solve-task (PRI-249).

Единственный модуль метрики с вводом-выводом: читает файл брифа из локального
клона репозитория. Формулы не свои — берутся из reviewer.metrics.brief_quality,
общего с офлайн-харнессом eval/solve_task_metrics (PRI-250): только тождество
кода делает числа «до/после» сравнимыми.

Ground truth не требует git: PreparedReview.changed_status уже несёт статус
файла (added/modified/removed), а «existed_before» — это ровно `status != added`.
"""
from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass, field

from reviewer.metrics.brief_quality import briefs, classify, recall

log = logging.getLogger(__name__)

BRIEFS_DIR = "docs/superpowers/briefs"

STATUS_MEASURED = "measured"
STATUS_NO_TASK_KEY = "no_task_key"
STATUS_NO_BRIEF = "no_brief"
STATUS_BRIEF_UNREADABLE = "brief_unreadable"
STATUS_EMPTY_CORE = "empty_core_denominator"


@dataclass(frozen=True)
class BriefQualityMeasurement:
    """Одно измерение качества брифа. status != measured — точки измерения нет."""

    status: str
    task_key: str | None = None
    brief_path: str | None = None
    expected: int = 0
    expected_core: int = 0
    predicted: int = 0
    hit_core: int = 0
    core_recall: float | None = None
    raw_recall: float | None = None
    precision: float | None = None
    misses: dict = field(default_factory=dict)
    predicted_paths: tuple = ()
    expected_core_paths: tuple = ()
    hit_core_paths: tuple = ()


def find_brief(clone_path: str | None, task_key: str) -> pathlib.Path | None:
    """Файл брифа задачи в клоне или None.

    Совпадение по ключу регистронезависимо: имена брифов пишутся и как PRI-249,
    и как pri-249. При нескольких файлах берётся лексикографически последний —
    имя начинается с даты, поэтому это самый свежий бриф задачи.
    """
    if not clone_path or not task_key:
        return None
    directory = pathlib.Path(clone_path) / BRIEFS_DIR
    if not directory.is_dir():
        return None
    needle = task_key.lower()
    matches = sorted(
        path for path in directory.glob("*.md") if needle in path.name.lower()
    )
    return matches[-1] if matches else None


def measure(
    *,
    task_key: str | None,
    clone_path: str | None,
    changed_paths: list,
    changed_status: dict,
) -> BriefQualityMeasurement:
    """Посчитать качество брифа задачи против фактического diff'а PR.

    Никогда не бросает: каждый отказ — именованный status, потому что молчание
    неотличимо от «метрика сломалась».
    """
    if not task_key:
        return BriefQualityMeasurement(status=STATUS_NO_TASK_KEY)

    brief = find_brief(clone_path, task_key)
    if brief is None:
        return BriefQualityMeasurement(status=STATUS_NO_BRIEF, task_key=task_key)

    relative = f"{BRIEFS_DIR}/{brief.name}"
    try:
        text = brief.read_text(encoding="utf-8")
    except OSError:
        log.warning("Не удалось прочитать бриф %s", relative, exc_info=True)
        return BriefQualityMeasurement(
            status=STATUS_BRIEF_UNREADABLE, task_key=task_key, brief_path=relative
        )
    if not briefs.has_section(text, briefs.RELEVANT_HEADER):
        return BriefQualityMeasurement(
            status=STATUS_BRIEF_UNREADABLE, task_key=task_key, brief_path=relative
        )

    predicted = briefs.extract_section_paths(text, briefs.RELEVANT_HEADER)
    expected = set(changed_paths)

    def existed_before(path: str) -> bool:
        return changed_status.get(path) != "added"

    expected_core = {
        path
        for path in expected
        if classify.is_core_production_path(path) and existed_before(path)
    }
    row = recall.evaluate_task(task_key, predicted, expected, expected_core)

    misses: dict = {}
    for missed in expected - predicted:
        category = classify.categorize_miss(missed, existed_before(missed))
        misses[category] = misses.get(category, 0) + 1

    status = STATUS_MEASURED if expected_core else STATUS_EMPTY_CORE
    return BriefQualityMeasurement(
        status=status,
        task_key=task_key,
        brief_path=relative,
        expected=row.expected,
        expected_core=row.expected_core,
        predicted=row.predicted,
        hit_core=row.hit_core,
        core_recall=row.core_recall,
        raw_recall=row.raw_recall,
        precision=row.precision,
        misses=misses,
        predicted_paths=tuple(sorted(predicted)),
        expected_core_paths=tuple(sorted(expected_core)),
        hit_core_paths=tuple(sorted(predicted & expected_core)),
    )
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/pytest -q tests/services/test_brief_quality.py`
Expected: PASS (10 тестов)

- [ ] **Step 5: Линт и коммит**

```bash
.venv/bin/ruff check reviewer/services/brief_quality.py tests/services/test_brief_quality.py
git add reviewer/services/brief_quality.py tests/services/test_brief_quality.py
git commit -m "feat(metrics): адаптер онлайн-съёма качества брифа solve-task (PRI-249)"
```

---

## Task 3: Хранение — таблица `brief_quality`, запись и чтение в `ReviewHistory`

Схема, идемпотентная миграция, запись строки и агрегирующее чтение с объединением нескольких PR одной задачи (без него task-level число несопоставимо с офлайн-baseline).

**Files:**
- Modify: `reviewer/web/schema.sql`, `reviewer/web/history.py`
- Test: `tests/web/test_history.py`

**Interfaces:**
- Consumes: `BriefQualityMeasurement` из Task 2, `recall.aggregate`/`recall.TaskQuality`/`BULK_CORE_THRESHOLD` из Task 1.
- Produces:
  - `ReviewHistory.record_brief_quality(run_id: int, repo: str, pr_number: int, head_sha: str | None, measurement: BriefQualityMeasurement) -> int | None` — id строки или `None` (fail-soft).
  - `ReviewHistory.brief_quality_trend(days: int = 90, repo: str | None = None) -> dict` — ключи `trend`, `aggregate`, `bulk`, `misses`, `bulk_threshold`, `no_measurement_by_status`.

- [ ] **Step 1: Написать падающие тесты**

Добавить в конец `tests/web/test_history.py`:

```python
# ---------------------------------------------------------------------------
# PRI-249: качество брифа solve-task
# ---------------------------------------------------------------------------


def test_record_brief_quality_fail_soft_without_db():
    """Недоступная БД не роняет запись метрики — возвращается None."""
    from reviewer.services.brief_quality import BriefQualityMeasurement
    from reviewer.web.history import ReviewHistory

    history = ReviewHistory("postgresql://nobody@127.0.0.1:1/none")
    assert history.record_brief_quality(
        1, "owner/repo", 42, "deadbeef",
        BriefQualityMeasurement(status="measured", task_key="PRI-999"),
    ) is None


def test_brief_quality_trend_fail_soft_without_db():
    """Недоступная БД отдаёт пустой, но валидный по форме ответ."""
    from reviewer.web.history import ReviewHistory

    data = ReviewHistory("postgresql://nobody@127.0.0.1:1/none").brief_quality_trend(days=30)
    assert data["trend"] == []
    assert data["aggregate"]["n_measured"] == 0
    assert data["misses"] == []


@pytest.mark.integration
def test_brief_quality_roundtrip_and_task_union():
    """Два PR одной задачи объединяются в одну точку — как в офлайн-харнессе.

    repo уникален на прогон: таблица истории живёт между запусками, и фильтр по
    репозиторию — единственный способ изолировать выборку теста от чужих строк.
    """
    import uuid

    from reviewer.metrics.brief_quality.recall import BULK_CORE_THRESHOLD
    from reviewer.services.brief_quality import BriefQualityMeasurement

    repo = f"pri249/union-{uuid.uuid4().hex[:8]}"
    history = ReviewHistory(Settings().pg_dsn)
    history.init_schema()
    run_a = history.record_run({**_sample_run(), "pr_number": 900001}, [])
    run_b = history.record_run({**_sample_run(), "pr_number": 900002}, [])

    history.record_brief_quality(
        run_a, repo, 1, "sha1",
        BriefQualityMeasurement(
            status="measured", task_key="PRI-999", brief_path="docs/superpowers/briefs/b.md",
            expected=2, expected_core=2, predicted=2, hit_core=1,
            core_recall=0.5, raw_recall=0.5, precision=0.5,
            misses={"tests/": 1},
            predicted_paths=("reviewer/a.py", "reviewer/b.py"),
            expected_core_paths=("reviewer/a.py", "reviewer/c.py"),
            hit_core_paths=("reviewer/a.py",),
        ),
    )
    history.record_brief_quality(
        run_b, repo, 2, "sha2",
        BriefQualityMeasurement(
            status="measured", task_key="PRI-999", brief_path="docs/superpowers/briefs/b.md",
            expected=1, expected_core=1, predicted=2, hit_core=1,
            core_recall=1.0, raw_recall=1.0, precision=0.5,
            misses={"docs/": 2},
            predicted_paths=("reviewer/a.py", "reviewer/b.py"),
            expected_core_paths=("reviewer/b.py",),
            hit_core_paths=("reviewer/b.py",),
        ),
    )

    data = history.brief_quality_trend(days=30, repo=repo)
    assert len(data["trend"]) == 1                      # обе строки — одна задача
    point = data["trend"][0]
    assert point["task_key"] == "PRI-999"
    assert point["expected_core"] == 3                  # union {a, c} ∪ {b}
    assert point["hit_core"] == 2                       # union {a} ∪ {b}
    assert point["core_recall"] == pytest.approx(2 / 3)
    assert data["aggregate"]["n_measured"] == 1
    assert data["bulk_threshold"] == BULK_CORE_THRESHOLD
    assert {m["category"]: m["count"] for m in data["misses"]} == {"tests/": 1, "docs/": 2}


@pytest.mark.integration
def test_brief_quality_no_measurement_is_separate():
    """status != measured считается отдельно и не мешается в медиану."""
    import uuid

    from reviewer.services.brief_quality import BriefQualityMeasurement

    repo = f"pri249/nomeasure-{uuid.uuid4().hex[:8]}"
    history = ReviewHistory(Settings().pg_dsn)
    history.init_schema()
    run = history.record_run({**_sample_run(), "pr_number": 900003}, [])
    history.record_brief_quality(
        run, repo, 3, "sha3",
        BriefQualityMeasurement(status="no_brief", task_key="PRI-777"),
    )
    data = history.brief_quality_trend(days=30, repo=repo)
    assert data["trend"] == []
    assert data["aggregate"]["n_measured"] == 0
    assert data["no_measurement_by_status"] == {"no_brief": 1}
```

Хелперы `_sample_run()` и `Settings` в файле уже есть (строки 15 и 23) — новые фикстуры не нужны.
Тесты изолируются уникальным `repo` в строках метрики, а не очисткой таблицы: таблица истории живёт
между прогонами, и удалять из неё чужие строки тест не вправе.

- [ ] **Step 2: Прогнать unit-часть и убедиться, что падает**

Run: `.venv/bin/pytest -q tests/web/test_history.py -k brief_quality`
Expected: FAIL с `AttributeError: 'ReviewHistory' object has no attribute 'record_brief_quality'`

- [ ] **Step 3: Добавить таблицу в схему**

В конец `reviewer/web/schema.sql`:

```sql
-- Качество ретрива под бриф solve-task (PRI-249): одна строка на прогон ревью.
-- status отделяет «нет точки измерения» от нулевого recall: у задачи, чей diff
-- состоит только из тестов и доков, качество ретрива по ядру не определено, и
-- подмешивать её нулём в медиану значит систематически занижать метрику.
-- Множества путей хранятся, потому что офлайн-baseline посчитан по ЗАДАЧЕ
-- (объединение всех её PR), а онлайн видит по одному PR: без них task-level
-- число было бы посчитано другой линейкой и несравнимо с «до».
CREATE TABLE IF NOT EXISTS brief_quality (
    id                  BIGSERIAL   PRIMARY KEY,
    run_id              BIGINT      NOT NULL
                            REFERENCES review_runs (id) ON DELETE CASCADE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    repo                TEXT        NOT NULL,
    pr_number           INT         NOT NULL,
    task_key            TEXT,
    head_sha            TEXT,
    status              TEXT        NOT NULL,   -- measured | no_task_key | no_brief
                                                -- | brief_unreadable | empty_core_denominator
    brief_path          TEXT,
    expected            INT         NOT NULL DEFAULT 0,
    expected_core       INT         NOT NULL DEFAULT 0,
    predicted           INT         NOT NULL DEFAULT 0,
    hit_core            INT         NOT NULL DEFAULT 0,
    core_recall         REAL,                   -- NULL = нет точки измерения
    raw_recall          REAL,
    precision           REAL,
    misses              JSONB       NOT NULL DEFAULT '{}'::jsonb,
    predicted_paths     JSONB       NOT NULL DEFAULT '[]'::jsonb,
    expected_core_paths JSONB       NOT NULL DEFAULT '[]'::jsonb,
    hit_core_paths      JSONB       NOT NULL DEFAULT '[]'::jsonb
);

CREATE INDEX IF NOT EXISTS brief_quality_repo_created_at
    ON brief_quality (repo, created_at DESC);
CREATE INDEX IF NOT EXISTS brief_quality_task_key ON brief_quality (task_key);
```

- [ ] **Step 4: Реализовать запись и чтение**

В `reviewer/web/history.py` добавить импорт `import json` (если его нет) и два метода в конец класса `ReviewHistory`, перед `_row_to_dict`:

```python
    # ------------------------------------------------------------------
    # Качество брифа solve-task (PRI-249)
    # ------------------------------------------------------------------

    def record_brief_quality(
        self,
        run_id: int,
        repo: str,
        pr_number: int,
        head_sha: str | None,
        measurement,
    ) -> int | None:
        """Записать измерение качества брифа (fail-soft, как record_run).

        Строки со status != 'measured' пишутся намеренно: «точки измерения не
        было и вот почему» — диагностический сигнал, а молчание неотличимо от
        сломанной метрики.
        """
        sql = """
        INSERT INTO brief_quality (
            run_id, repo, pr_number, task_key, head_sha, status, brief_path,
            expected, expected_core, predicted, hit_core,
            core_recall, raw_recall, precision,
            misses, predicted_paths, expected_core_paths, hit_core_paths
        ) VALUES (
            %(run_id)s, %(repo)s, %(pr_number)s, %(task_key)s, %(head_sha)s,
            %(status)s, %(brief_path)s,
            %(expected)s, %(expected_core)s, %(predicted)s, %(hit_core)s,
            %(core_recall)s, %(raw_recall)s, %(precision)s,
            %(misses)s, %(predicted_paths)s, %(expected_core_paths)s, %(hit_core_paths)s
        ) RETURNING id
        """
        try:
            params = {
                "run_id": run_id,
                "repo": repo,
                "pr_number": pr_number,
                "task_key": measurement.task_key,
                "head_sha": head_sha,
                "status": measurement.status,
                "brief_path": measurement.brief_path,
                "expected": measurement.expected,
                "expected_core": measurement.expected_core,
                "predicted": measurement.predicted,
                "hit_core": measurement.hit_core,
                "core_recall": measurement.core_recall,
                "raw_recall": measurement.raw_recall,
                "precision": measurement.precision,
                "misses": json.dumps(measurement.misses, ensure_ascii=False),
                "predicted_paths": json.dumps(list(measurement.predicted_paths)),
                "expected_core_paths": json.dumps(list(measurement.expected_core_paths)),
                "hit_core_paths": json.dumps(list(measurement.hit_core_paths)),
            }
            with self._connect() as conn:
                row = conn.execute(sql, params).fetchone()
                conn.commit()
            return int(row[0]) if row else None
        except Exception as exc:  # noqa: BLE001 — метрика не смеет ронять ревью
            log.warning("Не удалось записать качество брифа для прогона %s: %s", run_id, exc)
            return None

    def brief_quality_trend(self, days: int = 90, repo: str | None = None) -> dict:
        """Динамика качества брифа за окно, агрегированная ПО ЗАДАЧЕ.

        Несколько PR одной задачи объединяются в одну точку (union множеств) —
        ровно так считает офлайн-харнесс, чей baseline служит точкой «до» для
        критерия 4 PRI-251. Считать по PR значило бы мерить другой линейкой.
        """
        from reviewer.metrics.brief_quality.recall import (
            BULK_CORE_THRESHOLD,
            TaskQuality,
            aggregate,
        )

        empty = {
            "trend": [],
            "aggregate": {"n_measured": 0, "no_measurement": 0},
            "bulk": {"n_measured": 0, "core_recall_median": None},
            "misses": [],
            "bulk_threshold": BULK_CORE_THRESHOLD,
            "no_measurement_by_status": {},
        }
        sql = """
        SELECT created_at, task_key, pr_number, status,
               predicted_paths, expected_core_paths, hit_core_paths, misses
        FROM brief_quality
        WHERE created_at >= now() - %(days)s * INTERVAL '1 day'
          AND (%(repo)s IS NULL OR repo = %(repo)s)
        ORDER BY created_at
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, {"days": days, "repo": repo}).fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось получить динамику качества брифа: %s", exc)
            return empty

        by_task: dict = {}
        no_measurement: dict = {}
        misses_total: dict = {}
        for created_at, task_key, pr_number, status, predicted, core, hits, misses in rows:
            for category, count in (misses or {}).items():
                misses_total[category] = misses_total.get(category, 0) + int(count)
            if status != "measured" or not task_key:
                key = status or "unknown"
                no_measurement[key] = no_measurement.get(key, 0) + 1
                continue
            entry = by_task.setdefault(
                task_key,
                {"created_at": created_at, "prs": [], "predicted": set(), "core": set(), "hits": set()},
            )
            entry["created_at"] = max(entry["created_at"], created_at)
            entry["prs"].append(int(pr_number))
            entry["predicted"].update(predicted or [])
            entry["core"].update(core or [])
            entry["hits"].update(hits or [])

        trend: list = []
        quality_rows: list = []
        for task_key, entry in by_task.items():
            core_recall = len(entry["hits"]) / len(entry["core"]) if entry["core"] else None
            trend.append({
                "date": entry["created_at"].isoformat(),
                "task_key": task_key,
                "prs": sorted(entry["prs"]),
                "expected_core": len(entry["core"]),
                "predicted": len(entry["predicted"]),
                "hit_core": len(entry["hits"]),
                "core_recall": core_recall,
                "precision": (
                    len(entry["hits"]) / len(entry["predicted"]) if entry["predicted"] else None
                ),
            })
            quality_rows.append(TaskQuality(
                task_key=task_key,
                expected=len(entry["core"]),
                expected_core=len(entry["core"]),
                predicted=len(entry["predicted"]),
                hit_core=len(entry["hits"]),
                core_recall=core_recall,
            ))
        trend.sort(key=lambda point: point["date"])
        agg = aggregate(quality_rows)
        return {
            "trend": trend,
            "aggregate": {
                "n_measured": agg.n_measured,
                "no_measurement": sum(no_measurement.values()),
                "core_recall_median": agg.core_recall_median,
                "core_recall_mean": agg.core_recall_mean,
                "denominator_median": agg.denominator_median,
            },
            "bulk": {
                "n_measured": agg.bulk_n_measured,
                "core_recall_median": agg.bulk_core_recall_median,
            },
            "misses": [
                {"category": category, "count": count}
                for category, count in sorted(
                    misses_total.items(), key=lambda item: item[1], reverse=True
                )
            ],
            "bulk_threshold": BULK_CORE_THRESHOLD,
            "no_measurement_by_status": no_measurement,
        }
```

Важно: `precision` в `trend` считается на объединённых множествах задачи, поэтому у него тот же знаменатель, что у офлайн-метода.

- [ ] **Step 5: Прогнать unit-тесты**

Run: `.venv/bin/pytest -q tests/web/test_history.py`
Expected: PASS (integration-тесты пропущены по маркеру)

- [ ] **Step 6: Прогнать integration-тесты**

```bash
docker compose --profile test up -d --wait paradedb-test neo4j-test
.venv/bin/pytest -q -m integration tests/web/test_history.py
```
Expected: PASS. Убирать сервисы, если нужно, только адресно: `docker compose --profile test rm -sfv paradedb-test neo4j-test`.

- [ ] **Step 7: Коммит**

```bash
git add reviewer/web/schema.sql reviewer/web/history.py tests/web/test_history.py
git commit -m "feat(web): таблица brief_quality и агрегирующее чтение метрики (PRI-249)"
```

---

## Task 4: Точка съёма в `publish_review`

Подключить метрику к реальному событию: успешная публикация ревью. Полностью fail-soft, отчёт тула не меняется.

**Files:**
- Modify: `reviewer/mcp/service.py` (в `publish_review` после `_record_history`, ~строка 3044; новый приватный метод рядом с `_record_history`)
- Test: `tests/mcp/test_publish.py`

**Interfaces:**
- Consumes: `measure(...)` из Task 2, `ReviewHistory.record_brief_quality(...)` из Task 3, существующий `MCPReviewService._repo_clone_path(repo)`.
- Produces: `MCPReviewService._record_brief_quality(repo, pr, p, run_id, task_key) -> None` (приватный, ничего не возвращает).

- [ ] **Step 1: Написать падающие тесты**

Тесты используют уже существующие в файле хелперы: `_make_mcp_service_with_publish()` (возвращает
`(svc, vcs, history)`; репо `"o/r"`, PR `7`), `_submit_then_publish(...)`, `_FakeHistory` и константу
`RAW`. Фейковый VCS отдаёт один изменённый файл `a.py` со статусом `modified` — это корневой `*.py`,
то есть ядро, и знаменатель непуст.

Сначала расширить `_FakeHistory` (класс, строка 130) методом сбора измерений:

```python
    def record_brief_quality(self, run_id, repo, pr_number, head_sha, measurement):
        self.brief_quality.append((run_id, repo, pr_number, head_sha, measurement))
        return len(self.brief_quality)
```

и добавить `self.brief_quality: list = []` в его `__init__`.

Затем добавить тесты в конец файла:

```python
def _write_brief(tmp_path, body: str = "- `a.py:1` — точка правки") -> None:
    """Положить бриф задачи PRI-999 в клон, как это делает solve-task."""
    briefs = tmp_path / "docs" / "superpowers" / "briefs"
    briefs.mkdir(parents=True, exist_ok=True)
    (briefs / "2026-08-14-PRI-999-x.md").write_text(
        f"# Brief — PRI-999\n\n## Relevant code\n{body}\n", encoding="utf-8"
    )


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_records_brief_quality(_ov, _ch, monkeypatch, tmp_path) -> None:
    """Успешная публикация пишет измерение качества брифа рядом с историей."""
    _write_brief(tmp_path)
    svc, _vcs, history = _make_mcp_service_with_publish()
    monkeypatch.setattr(svc, "_repo_clone_path", lambda repo: str(tmp_path))
    svc.prepare_review("o/r", 7)

    _submit_then_publish(svc, "o/r", 7, [RAW], task_key="PRI-999")

    assert len(history.brief_quality) == 1
    run_id, repo, pr_number, _head, measurement = history.brief_quality[0]
    assert (repo, pr_number) == ("o/r", 7)
    assert run_id == 1                          # id прогона из _FakeHistory.record_run
    assert measurement.status == "measured"
    assert measurement.task_key == "PRI-999"
    assert measurement.expected_core == 1       # a.py — корневой .py, existed_before
    assert measurement.hit_core == 1            # бриф его предсказал
    assert measurement.core_recall == 1.0


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_brief_quality_missing_brief_is_recorded(_ov, _ch, monkeypatch, tmp_path) -> None:
    """Брифа нет — пишется строка no_brief: «точки измерения не было и вот почему»."""
    svc, _vcs, history = _make_mcp_service_with_publish()
    monkeypatch.setattr(svc, "_repo_clone_path", lambda repo: str(tmp_path))
    svc.prepare_review("o/r", 7)

    _submit_then_publish(svc, "o/r", 7, [RAW], task_key="PRI-999")

    assert history.brief_quality[0][4].status == "no_brief"


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_survives_brief_quality_failure(_ov, _ch, monkeypatch, tmp_path) -> None:
    """Сбой метрики не меняет ни отчёт publish_review, ни факт публикации."""
    def _boom(**kwargs):
        raise RuntimeError("метрика сломалась")

    monkeypatch.setattr("reviewer.services.brief_quality.measure", _boom)
    svc, vcs, _history = _make_mcp_service_with_publish()
    monkeypatch.setattr(svc, "_repo_clone_path", lambda repo: str(tmp_path))
    svc.prepare_review("o/r", 7)

    report = _submit_then_publish(svc, "o/r", 7, [RAW], task_key="PRI-999")

    assert report["posted"] is True
    assert vcs.published                       # ревью опубликовано
    assert "brief_quality" not in report       # отчёт тула не расширяется


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_dry_run_does_not_measure(_ov, _ch, monkeypatch, tmp_path) -> None:
    """dry_run точкой измерения не является: ревью не опубликовано."""
    _write_brief(tmp_path)
    svc, _vcs, history = _make_mcp_service_with_publish()
    monkeypatch.setattr(svc, "_repo_clone_path", lambda repo: str(tmp_path))
    svc.prepare_review("o/r", 7)

    _submit_then_publish(svc, "o/r", 7, [RAW], dry_run=True, task_key="PRI-999")

    assert history.brief_quality == []
```

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run: `.venv/bin/pytest -q tests/mcp/test_publish.py -k brief_quality`
Expected: FAIL — `assert len(history.brief_quality) == 1` падает на `0`, потому что `publish_review`
метрику ещё не вызывает.

- [ ] **Step 3: Реализовать вызов**

В `reviewer/mcp/service.py`, в `publish_review` сразу после присваивания `run_id = self._record_history(...)` и ДО `self._cleanup(repo, pr)` вставить:

```python
        # 6b) Качество ретрива под бриф solve-task (PRI-249) — только по факту
        # реальной публикации: dry_run и сбой постинга точкой измерения не являются.
        if not dry_run and posted and run_id is not None:
            self._record_brief_quality(repo, pr, p, run_id, task_key)
```

И добавить приватный метод рядом с `_record_history`:

```python
    def _record_brief_quality(
        self,
        repo: str,
        pr: int,
        p: PreparedReview,
        run_id: int,
        task_key: str | None,
    ) -> None:
        """Посчитать и сохранить качество брифа solve-task (PRI-249).

        Полностью fail-soft: метрика — наблюдение за самим reviewer, и ни один
        её сбой не смеет повлиять на результат ревью. Ключ задачи берётся из
        аргумента publish_review, иначе из уже отрезолвленного task_keys.primary.
        """
        try:
            from reviewer.services import brief_quality

            key = task_key
            if not key and p.task_keys:
                key = p.task_keys.get("primary")
            measurement = brief_quality.measure(
                task_key=key,
                clone_path=self._repo_clone_path(repo),
                changed_paths=p.changed_paths,
                changed_status=p.changed_status,
            )
            history = self._review_service._ensure_history()
            if history is None:
                return
            history.record_brief_quality(
                run_id, repo, pr, p.prq.head_sha, measurement
            )
        except Exception:  # noqa: BLE001 — наблюдаемость не роняет ревью
            log.warning("Не удалось посчитать качество брифа для %s pr:%s", repo, pr, exc_info=True)
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/pytest -q tests/mcp/test_publish.py`
Expected: PASS

- [ ] **Step 5: Прогнать весь unit-набор — точка съёма в горячем пути**

Run: `.venv/bin/pytest -q`
Expected: PASS

- [ ] **Step 6: Коммит**

```bash
git add reviewer/mcp/service.py tests/mcp/test_publish.py
git commit -m "feat(mcp): снимать качество брифа при публикации ревью (PRI-249)"
```

---

## Task 5: API `/api/quality` и страница админки

**Files:**
- Modify: `reviewer/web/api.py`, `web/frontend/src/api.ts`, `web/frontend/src/App.tsx`
- Create: `web/frontend/src/pages/Quality.tsx`
- Test: `tests/web/test_api.py`

**Interfaces:**
- Consumes: `ReviewHistory.brief_quality_trend(days, repo)` из Task 3.
- Produces: `GET /api/quality?days=<1..365>&repo=<owner/name>` → JSON того же вида, что возвращает `brief_quality_trend`.

- [ ] **Step 1: Написать падающий тест API**

Добавить в `tests/web/test_api.py` (в фейковый стор — метод, в тесты — проверку):

```python
_FAKE_QUALITY = {
    "trend": [
        {"date": _NOW, "task_key": "PRI-999", "prs": [1], "expected_core": 12,
         "predicted": 6, "hit_core": 5, "core_recall": 0.4167, "precision": 0.83},
    ],
    "aggregate": {"n_measured": 1, "no_measurement": 2, "core_recall_median": 0.4167,
                  "core_recall_mean": 0.4167, "denominator_median": 12},
    "bulk": {"n_measured": 1, "core_recall_median": 0.4167},
    "misses": [{"category": "tests/", "count": 3}],
    "bulk_threshold": 10,
    "no_measurement_by_status": {"no_brief": 2},
}


def test_quality_endpoint_returns_trend(client):
    """GET /api/quality отдаёт динамику метрики качества брифа."""
    response = client.get("/api/quality?days=90")
    assert response.status_code == 200
    body = response.json()
    assert body["trend"][0]["task_key"] == "PRI-999"
    assert body["bulk_threshold"] == 10


def test_quality_endpoint_validates_days(client):
    """days вне диапазона 1..365 отклоняется валидацией FastAPI."""
    assert client.get("/api/quality?days=0").status_code == 422
```

В `FakeHistory` этого файла добавить:

```python
    def brief_quality_trend(self, days: int = 90, repo: str | None = None) -> dict:
        return _FAKE_QUALITY
```

(Имя фикстуры клиента взять то, что уже используется соседними тестами файла.)

- [ ] **Step 2: Прогнать и убедиться, что падает**

Run: `.venv/bin/pytest -q tests/web/test_api.py -k quality`
Expected: FAIL со статусом 404

- [ ] **Step 3: Добавить эндпоинт**

В `reviewer/web/api.py` после `get_stats` и перед `return router`:

```python
    @router.get("/api/quality")
    def get_brief_quality(
        days: int = Query(default=90, ge=1, le=365, description="Окно в днях"),
        repo: str | None = Query(default=None, description="Фильтр по репозиторию"),
    ) -> JSONResponse:
        """Динамика качества ретрива под бриф solve-task (PRI-249).

        Точки агрегированы по задаче, а не по PR: так число совпадает с
        методикой офлайн-харнесса, чей baseline служит точкой «до».
        """
        try:
            data = history.brief_quality_trend(days=days, repo=repo)
            return JSONResponse(data)
        except Exception as exc:
            log.error("Ошибка при получении качества брифов: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера") from exc
```

В докстринге `make_router` добавить строку в блок `Routes:`:

```
        GET /api/quality         — динамика качества брифа solve-task.
```

- [ ] **Step 4: Прогнать тесты API**

Run: `.venv/bin/pytest -q tests/web/test_api.py`
Expected: PASS

- [ ] **Step 5: Добавить типы и клиент во фронт**

В конец `web/frontend/src/api.ts`:

```ts
// ─── Качество брифа solve-task (PRI-249) ──────────────────────────────────────

export interface QualityPoint {
  date: string
  task_key: string
  prs: number[]
  expected_core: number
  predicted: number
  hit_core: number
  core_recall: number | null
  precision: number | null
}

export interface QualityAggregate {
  n_measured: number
  no_measurement: number
  core_recall_median: number | null
  core_recall_mean: number | null
  denominator_median: number | null
}

export interface MissPoint {
  category: string
  count: number
}

export interface Quality {
  trend: QualityPoint[]
  aggregate: QualityAggregate
  bulk: { n_measured: number; core_recall_median: number | null }
  misses: MissPoint[]
  bulk_threshold: number
  no_measurement_by_status: Record<string, number>
}

export async function fetchQuality(days: number): Promise<Quality> {
  return apiFetch<Quality>(`/api/quality?days=${days}`)
}
```

- [ ] **Step 6: Создать страницу**

Создать `web/frontend/src/pages/Quality.tsx`:

```tsx
import { useState, useEffect } from 'react'
import {
  LineChart, Line, BarChart, Bar, ReferenceLine,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer,
} from 'recharts'
import { fetchQuality, type Quality } from '../api'
import { fmtPct } from '../utils'

const PERIODS = [
  { label: '30 дн', value: 30 },
  { label: '90 дн', value: 90 },
  { label: '365 дн', value: 365 },
]

// База «до» из офлайн-харнесса PRI-250 на bulk-подвыборке (коммит d474e02):
// bulk_core_recall_median ≈ 0.373 при 4 измеренных задачах. Горизонталь на
// графике — точка отсчёта для отложенного критерия 4 PRI-251.
const BULK_BASELINE = 0.373

const tooltipStyle = {
  background: 'rgba(14,16,24,0.95)',
  border: '1px solid rgba(255,255,255,0.07)',
  borderRadius: 8,
  fontFamily: 'JetBrains Mono, monospace',
  fontSize: 12,
}

export default function Quality() {
  const [days, setDays] = useState(90)
  const [data, setData] = useState<Quality | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setLoading(true)
    setError(null)
    fetchQuality(days)
      .then(setData)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false))
  }, [days])

  if (loading) return <div className="page-state">Загрузка…</div>
  if (error) return <div className="page-state error">{error}</div>
  if (!data) return null

  const bulk = data.trend.filter((p) => p.expected_core >= data.bulk_threshold)
  const points = data.trend.map((p) => ({
    date: p.date.slice(0, 10),
    task_key: p.task_key,
    core_recall: p.core_recall,
    precision: p.precision,
    bulk_recall: p.expected_core >= data.bulk_threshold ? p.core_recall : null,
  }))

  return (
    <div className="page">
      <div className="page-head">
        <h1>Качество брифа solve-task</h1>
        <div className="period-switch">
          {PERIODS.map((period) => (
            <button
              key={period.value}
              className={period.value === days ? 'active' : ''}
              onClick={() => setDays(period.value)}
            >
              {period.label}
            </button>
          ))}
        </div>
      </div>

      <div className="kpi-row">
        <div className="kpi-card">
          <div className="kpi-label">core-recall, медиана</div>
          <div className="kpi-value">{fmtPct(data.aggregate.core_recall_median ?? 0)}</div>
          <div className="kpi-sub">измерено задач: {data.aggregate.n_measured}</div>
        </div>
        <div className="kpi-card">
          <div className="kpi-label">bulk-подвыборка (ядро ≥ {data.bulk_threshold})</div>
          <div className="kpi-value">{fmtPct(data.bulk.core_recall_median ?? 0)}</div>
          <div className="kpi-sub">
            задач: {data.bulk.n_measured} · база до family: {fmtPct(BULK_BASELINE)}
          </div>
        </div>
        <div className="kpi-card">
          <div className="kpi-label">без точки измерения</div>
          <div className="kpi-value">{data.aggregate.no_measurement}</div>
          <div className="kpi-sub">
            {Object.entries(data.no_measurement_by_status)
              .map(([status, count]) => `${status}: ${count}`)
              .join(' · ') || '—'}
          </div>
        </div>
      </div>

      <section className="chart-card">
        <h2>Динамика</h2>
        <ResponsiveContainer width="100%" height={280}>
          <LineChart data={points}>
            <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.06)" />
            <XAxis dataKey="date" stroke="#8b90a5" fontSize={12} />
            <YAxis domain={[0, 1]} stroke="#8b90a5" fontSize={12} />
            <Tooltip contentStyle={tooltipStyle} />
            <Legend />
            <ReferenceLine
              y={BULK_BASELINE}
              stroke="#ff8c42"
              strokeDasharray="4 4"
              label={{ value: 'база bulk до family', fill: '#ff8c42', fontSize: 11 }}
            />
            <Line name="core-recall" dataKey="core_recall" stroke="#2dd4bf" dot />
            <Line name="precision" dataKey="precision" stroke="#7c5cff" dot />
            <Line name="bulk core-recall" dataKey="bulk_recall" stroke="#ff4d6d" dot connectNulls />
          </LineChart>
        </ResponsiveContainer>
        {bulk.length === 0 && (
          <p className="chart-note">
            В окне нет задач с ядром ≥ {data.bulk_threshold}: bulk-линия пуста.
          </p>
        )}
      </section>

      <section className="chart-card">
        <h2>Промахи по категориям</h2>
        <ResponsiveContainer width="100%" height={280}>
          <BarChart data={data.misses}>
            <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.06)" />
            <XAxis dataKey="category" stroke="#8b90a5" fontSize={11} interval={0} angle={-20} height={70} textAnchor="end" />
            <YAxis stroke="#8b90a5" fontSize={12} />
            <Tooltip contentStyle={tooltipStyle} />
            <Bar dataKey="count" fill="#5ac8fa" />
          </BarChart>
        </ResponsiveContainer>
      </section>
    </div>
  )
}
```

Если `fmtPct` в `web/frontend/src/utils.ts` ожидает долю 0..1 — использовать как выше; если проценты 0..100, домножить аргументы на 100. Проверить сигнатуру перед сборкой.

- [ ] **Step 7: Зарегистрировать роут**

В `web/frontend/src/App.tsx`: добавить импорт `import Quality from './pages/Quality'`, пункт навигации после «Прогоны»

```tsx
            <NavLink to="/quality" className={({ isActive }) => isActive ? 'nav-link active' : 'nav-link'}>
              Качество
            </NavLink>
```

и маршрут в `<Routes>`:

```tsx
            <Route path="/quality" element={<Quality />} />
```

- [ ] **Step 8: Собрать фронт**

```bash
cd web/frontend && npm install && npm run build && cd ../..
```
Expected: сборка без ошибок TypeScript.

- [ ] **Step 9: Прогнать тесты и закоммитить**

```bash
.venv/bin/pytest -q tests/web
git add reviewer/web/api.py tests/web/test_api.py web/frontend/src
git commit -m "feat(web): страница и API динамики качества брифа (PRI-249)"
```

Собранный `web/frontend/dist` коммитить только если он уже отслеживается git (проверить `git status`); если он в `.gitignore` — не добавлять.

---

## Task 6: Документация

**Files:**
- Modify: `CLAUDE.md`, `README.md`, `README.ru.md`

**Interfaces:**
- Consumes: всё выше.
- Produces: ничего исполняемого.

- [ ] **Step 1: Добавить неочевидный факт в `CLAUDE.md`**

В раздел «Неочевидные факты (не выводятся из кода)» добавить пункт:

```markdown
- **Онлайн-метрика качества брифа solve-task (PRI-249).** По факту реальной публикации ревью
  (`publish_review`, только `posted and not dry_run`) пути секции `## Relevant code` брифа задачи
  сопоставляются с фактическим diff'ом PR и пишутся в таблицу `brief_quality` рядом с историей
  прогонов. Три вещи в этом неочевидны. Во-первых, **знаменатель — не весь diff**: считается
  core-recall по ядру (`reviewer/**/*.py`, `plugin/**` не-`.md`, корневые `*.py`) И только по
  файлам, существовавшим до PR; сырой recall на том же корпусе давал медиану 15 % против 67 %
  у core (спайк PRI-246), то есть был метрикой размера diff'а, а не качества ретрива.
  «Существовал до PR» берётся из `PreparedReview.changed_status`, git при съёме не вызывается.
  Во-вторых, **пустое ядро — это `status='empty_core_denominator'` и `core_recall IS NULL`, а не
  ноль**: у задачи, чей diff состоит из тестов и доков, качество ретрива по ядру не определено
  (в спайке таких 10 из 45). В-третьих, **строка хранит множества путей, а не только счётчики**:
  офлайн-baseline посчитан по задаче (объединение всех её PR), онлайн видит по одному PR, и без
  union'а на чтении task-level число было бы посчитано другой линейкой, чем точка «до»
  (`bulk_core_recall_median ≈ 0.373`, `bulk_n_measured = 4`) — то есть отложенный критерий
  PRI-251 остался бы незакрытым. Расчётное ядро одно на офлайн и онлайн:
  `reviewer/metrics/brief_quality/`, а `eval/solve_task_metrics/{classify,recall,briefs}.py` —
  ре-экспорт (guard-тест ловит возврат второй копии). Гейт — общий `REVIEW_HISTORY`; своего
  ключа у метрики нет. Мержа PR метрика не видит: вебхука в системе нет, и правки после ревью
  в неё не попадают — сознательное сужение.
```

- [ ] **Step 2: Обновить оба README**

В `README.ru.md` — в раздел про веб-админку добавить абзац о странице «Качество»: что показывает (медиана core-recall и precision по задачам, bulk-подвыборка с горизонталью базы, разбивка промахов), откуда берутся данные (`brief_quality`, пишется при `publish_review`), и что при недоступном брифе точка просто не появляется. В `README.md` — тот же абзац по-английски. Оба README правятся синхронно.

- [ ] **Step 3: Проверить, что дока не разошлась с кодом**

Run: `.venv/bin/pytest -q tests/eval/test_docs.py`
Expected: PASS (тест сверяет документацию офлайн-харнесса; убедиться, что перенос модулей его не сломал).

- [ ] **Step 4: Коммит**

```bash
git add CLAUDE.md README.md README.ru.md
git commit -m "docs: описать онлайн-метрику качества брифа solve-task (PRI-249)"
```

---

## Финальная проверка

- [ ] `.venv/bin/pytest -q` — весь unit-набор зелёный
- [ ] `docker compose --profile test up -d --wait paradedb-test neo4j-test && .venv/bin/pytest -q -m integration`
- [ ] `.venv/bin/ruff check reviewer eval tests`
- [ ] `cd web/frontend && npm run build`
- [ ] Проверка на живой БД разработки, что миграция применяется идемпотентно:
      `.venv/bin/python -c "from reviewer.web.history import ReviewHistory; from reviewer.config.settings import Settings; h=ReviewHistory(Settings().pg_dsn); h.init_schema(); h.init_schema(); print('ok')"`
- [ ] `git push` и создание PR — **только после явного подтверждения пользователя** (в режиме full-auto эти два действия подтверждение сохраняют)
