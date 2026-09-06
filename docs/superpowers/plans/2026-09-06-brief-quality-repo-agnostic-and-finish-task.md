# Репо-агностичное ядро метрики брифа + съём без ревью PR — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать ядро метрики `brief_quality` репо-агностичным (один конфиг вместо трёх хардкодов) и снимать метрику не только на редком `publish_review`, но и на `finish_task` и новой команде `reviewer measure-briefs`.

**Architecture:** Новый чистый `BriefQualityConfig` (ядро-глобы, паттерн ключа задачи, каталог брифов) читается из `.review.yml` и обязательным параметром проходит во все функции ядра. Съём выносится в сервисную функцию `measure_and_record`, которую зовут три точки: `publish_review` (без изменения поведения), `finish_task` и CLI. Идентичность строки измерения — `(repo, pr_number, COALESCE(task_key,''))`, поэтому повторный съём обновляет строку, а не плодит.

**Tech Stack:** Python 3.11+, psycopg 3 (Postgres/ParadeDB), Click (CLI), FastMCP (MCP-слой), pytest.

**Spec:** `docs/superpowers/specs/2026-09-06-brief-quality-repo-agnostic-and-finish-task-design.md`

## Global Constraints

- Язык проекта — русский: комментарии, докстринги, сообщения CLI. Новый код пишется в этом стиле.
- `reviewer/**` НЕ импортирует `eval/**` — инвариант направления зависимости, его стережёт `tests/metrics/test_reexport_guard.py::test_production_core_does_not_import_eval`.
- `eval/solve_task_metrics/{briefs,classify,context_core,recall,ground_truth}.py` — только РЕЭКСПОРТ продакшн-объектов, без второй реализации формул.
- `publish_review` поведения не меняет: гейт `if not dry_run and posted and run_id is not None` (`reviewer/mcp/service.py:3199`) остаётся дословно прежним.
- Fail-soft контракт: ни один сбой метрики не смеет уронить ревью, `finish_task` или CLI. Каждый отказ — именованный `status`.
- Дефолтный конфиг обязан быть побитово эквивалентен нынешнему предикату ядра: `reviewer/**/*.py`, `plugin/**` кроме `*.md`, корневые `*.py`, `eval/` вне ядра.
- Тесты: unit-тестам запрещены внешние и localhost-сокеты; всё, что требует Postgres, помечается `@pytest.mark.integration`.
- Прогон unit-набора: `.venv/bin/pytest -q`. Baseline перед началом работы: 4497 passed, ruff чист. Падение = регрессия.
- Коммиты: Conventional Commits на русском, без self-attribution (никаких `Co-Authored-By`, упоминаний Claude).
- Ветка: `feat/pri-271-pri-270-brief-quality-repo-agnostic` (создана, спека и бриф в ней закоммичены).

---

## File Structure

**Создаются:**
- `reviewer/metrics/brief_quality/config.py` — `BriefQualityConfig`, дефолты, glob-матчер путей.
- `reviewer/metrics/brief_quality/ground_truth.py` — переезд из `eval/`; PR-мержи задачи и их дифф.
- `tests/metrics/test_brief_quality_config.py` — конфиг и матчер.
- `tests/metrics/test_ground_truth.py` — перенесённые + новые тесты номера PR.
- `tests/policy/test_policy_brief_quality.py` — чтение ключа `.review.yml` (guard, мутационно проверяемый).
- `tests/entrypoints/test_measure_briefs.py` — CLI.

**Модифицируются:**
- `reviewer/metrics/brief_quality/classify.py` — `config` обязателен, `categorize_miss` производный.
- `reviewer/metrics/brief_quality/briefs.py` — `extract_task_key`/`load_briefs` берут паттерн из конфига.
- `reviewer/metrics/brief_quality/context_core.py` — прокид `config`.
- `reviewer/policy/policy.py` — поле `brief_quality` в `ReviewPolicy`.
- `reviewer/services/brief_quality.py` — `config` в `measure`/`find_brief`, новый статус, `measure_and_record`.
- `reviewer/web/schema.sql`, `reviewer/web/history.py` — nullable `run_id`, уникальность, `ON CONFLICT`.
- `reviewer/mcp/service.py` — `_record_brief_quality` через сервис, съём в `finish_task`, `_pr_session`.
- `reviewer/entrypoints/cli.py` — команда `measure-briefs`.
- `eval/solve_task_metrics/{classify,briefs,ground_truth}.py` — реэкспорты.
- `eval/solve_task_metrics/{replay,snapshot,context_seeds,__main__}.py` — прокид конфига и флаги путей.
- `CLAUDE.md`, `README.md`, `README.ru.md` — документация.

---

### Task 1: Конфиг метрики и glob-матчер

**Files:**
- Create: `reviewer/metrics/brief_quality/config.py`
- Modify: `reviewer/policy/policy.py` (поле `brief_quality`, чтение ключа в `from_yaml`/`load_data`)
- Test: `tests/metrics/test_brief_quality_config.py`, `tests/policy/test_policy_brief_quality.py`

**Interfaces:**
- Consumes: `DEFAULT_KEY_PATTERN` из `reviewer/services/task_keys.py:16` (`[A-Z]+-\d+`).
- Produces: `BriefQualityConfig(core_paths, key_pattern, briefs_dir, configured)` с методом
  `matches_core(path) -> bool` и классметодом `from_review_yaml(data: Mapping) -> BriefQualityConfig`;
  константы `DEFAULT_CORE_PATHS`, `DEFAULT_BRIEFS_DIR`, `DEFAULT` (готовый конфиг rag_for_git);
  поле `ReviewPolicy.brief_quality: BriefQualityConfig`.

- [ ] **Step 1: Написать падающий тест матчера и дефолта**

```python
# tests/metrics/test_brief_quality_config.py
import pytest

from reviewer.metrics.brief_quality.config import DEFAULT, BriefQualityConfig


@pytest.mark.parametrize("path", [
    "reviewer/app.py",                 # один сегмент — "**/" обязан матчить пустой путь
    "reviewer/index/store.py",
    "plugin/hooks/review_cost.py",
    "sync_chunk.py",                   # корневой *.py
])
def test_default_core_matches_production_paths(path):
    assert DEFAULT.matches_core(path) is True


@pytest.mark.parametrize("path", [
    "tests/metrics/test_classify.py",  # '*' не пересекает '/', значит tests/ вне ядра
    "docs/superpowers/plans/x.md",
    "plugin/skills/solve-task/SKILL.md",  # исключение "!plugin/**/*.md"
    "eval/solve_task_metrics/replay.py",
    "README.md",
])
def test_default_core_rejects_non_production_paths(path):
    assert DEFAULT.matches_core(path) is False


def test_foreign_repo_core_paths():
    config = BriefQualityConfig.from_review_yaml(
        {"metrics": {"brief_quality": {"core_paths": ["app/**/*.py", "frontend/src/**"]}}}
    )
    assert config.configured is True
    assert config.matches_core("app/api/routes.py") is True
    assert config.matches_core("frontend/src/pages/Login.tsx") is True
    assert config.matches_core("reviewer/app.py") is False


def test_absent_key_gives_default_and_unconfigured():
    config = BriefQualityConfig.from_review_yaml({})
    assert config.core_paths == DEFAULT.core_paths
    assert config.configured is False


def test_explicit_empty_list_is_configured():
    """Явный пустой список — высказывание «ядра нет», а не молчание."""
    config = BriefQualityConfig.from_review_yaml(
        {"metrics": {"brief_quality": {"core_paths": []}}}
    )
    assert config.core_paths == ()
    assert config.configured is True


def test_null_value_falls_back_to_default():
    """`core_paths:` без значения — YAML None, а не явный пустой список."""
    config = BriefQualityConfig.from_review_yaml(
        {"metrics": {"brief_quality": {"core_paths": None}}}
    )
    assert config.core_paths == DEFAULT.core_paths
    assert config.configured is False


def test_key_pattern_comes_from_task_board():
    config = BriefQualityConfig.from_review_yaml({"task_board": {"key_pattern": r"RON-\d+"}})
    assert config.key_pattern == r"RON-\d+"


def test_key_pattern_defaults_when_board_absent():
    assert BriefQualityConfig.from_review_yaml({}).key_pattern == r"[A-Z]+-\d+"
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `.venv/bin/pytest tests/metrics/test_brief_quality_config.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'reviewer.metrics.brief_quality.config'`

- [ ] **Step 3: Реализовать конфиг**

```python
# reviewer/metrics/brief_quality/config.py
"""Конфигурация метрики качества брифа: ядро репозитория, ключ задачи, каталог брифов.

Модуль ЧИСТЫЙ, как и весь пакет: на вход — уже разобранный mapping `.review.yml`,
ни файлов, ни git, ни БД. Три хардкода, которые он заменяет (regex ключа, предикат
ядра, каталог брифов), были тремя независимыми каналами настройки; одного объекта
достаточно, и рассинхронизировать их между собой больше нечем.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Mapping

from reviewer.services.task_keys import DEFAULT_KEY_PATTERN

# Дефолт воспроизводит прежний предикат rag_for_git один в один: ядро — это
# reviewer/**/*.py, plugin/** кроме *.md и корневые *.py; eval/ вне ядра.
DEFAULT_CORE_PATHS: tuple[str, ...] = (
    "reviewer/**/*.py",
    "plugin/**",
    "!plugin/**/*.md",
    "!eval/**",
    "*.py",
)
DEFAULT_BRIEFS_DIR = "docs/superpowers/briefs"


def _glob_to_regex(pattern: str) -> str:
    """glob → regex, где `**` пересекает `/`, а `*` и `?` — нет.

    fnmatch здесь непригоден: он не знает про `/`, поэтому `fnmatch(
    "reviewer/x.py", "*.py")` истинно, и правило «только корневые *.py»
    на нём невыразимо. Последовательность `**/` переводится в `(?:.*/)?`,
    иначе `reviewer/**/*.py` не совпал бы с `reviewer/app.py`.
    """
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return f"^{''.join(out)}$"


@lru_cache(maxsize=64)
def _compiled(patterns: tuple[str, ...]) -> tuple[tuple[re.Pattern, ...], tuple[re.Pattern, ...]]:
    """(позитивные, исключающие) скомпилированные паттерны набора."""
    positive: list[re.Pattern] = []
    negative: list[re.Pattern] = []
    for raw in patterns:
        if not raw:
            continue
        target = negative if raw.startswith("!") else positive
        target.append(re.compile(_glob_to_regex(raw.lstrip("!"))))
    return tuple(positive), tuple(negative)


@dataclass(frozen=True)
class BriefQualityConfig:
    """Настройка метрики для конкретного репозитория.

    `configured` — был ли `core_paths` задан явно. Без него «у репозитория
    в диффе нет файлов ядра» неотличимо от «репозиторий не настроен, и ядро
    посчитано чужой линейкой»: именно на этом различии стоит статус
    `unconfigured_core_denominator`.
    """

    core_paths: tuple[str, ...] = DEFAULT_CORE_PATHS
    key_pattern: str = DEFAULT_KEY_PATTERN
    briefs_dir: str = DEFAULT_BRIEFS_DIR
    configured: bool = False

    def matches_core(self, path: str) -> bool:
        """Путь принадлежит ядру: совпал с позитивным паттерном и ни с одним `!`."""
        positive, negative = _compiled(self.core_paths)
        if any(rx.match(path) for rx in negative):
            return False
        return any(rx.match(path) for rx in positive)

    @classmethod
    def from_review_yaml(
        cls, data: Mapping[str, object] | None, *, briefs_dir: str | None = None
    ) -> "BriefQualityConfig":
        """Собрать конфиг из данных `.review.yml` (уже разобранных в mapping)."""
        data = data or {}
        raw = data.get("metrics")
        section = raw.get("brief_quality") if isinstance(raw, Mapping) else None
        core_raw = section.get("core_paths") if isinstance(section, Mapping) else None
        # Tri-state как у ReviewPolicy._summary_paths_ignore: ключа нет или
        # значение None → дефолт; явный список, в том числе пустой → настроено.
        if core_raw is None:
            core_paths, configured = DEFAULT_CORE_PATHS, False
        else:
            core_paths, configured = tuple(str(item) for item in core_raw), True

        board = data.get("task_board")
        pattern = board.get("key_pattern") if isinstance(board, Mapping) else None
        return cls(
            core_paths=core_paths,
            key_pattern=str(pattern) if pattern else DEFAULT_KEY_PATTERN,
            briefs_dir=briefs_dir or DEFAULT_BRIEFS_DIR,
            configured=configured,
        )


DEFAULT = BriefQualityConfig()
```

- [ ] **Step 4: Прогнать тест конфига**

Run: `.venv/bin/pytest tests/metrics/test_brief_quality_config.py -q`
Expected: PASS (9 тестов)

- [ ] **Step 5: Написать падающий тест чтения ключа политикой**

```python
# tests/policy/test_policy_brief_quality.py
"""Guard: ядро метрики читается из .review.yml, а не из константы модуля.

Мутационная проверка (критерий 5 PRI-271): если снять чтение ключа из
политики на копии модуля вне рабочего дерева, эти тесты обязаны покраснеть.
"""
from reviewer.metrics.brief_quality.config import DEFAULT_CORE_PATHS
from reviewer.policy.policy import ReviewPolicy

FOREIGN = """
task_board:
  type: yougile
  key_pattern: 'RON-\\d+'
metrics:
  brief_quality:
    core_paths:
      - 'app/**/*.py'
      - 'frontend/src/**'
"""


def test_policy_reads_core_paths_from_review_yml():
    policy = ReviewPolicy.from_yaml(FOREIGN)
    assert policy.brief_quality.core_paths == ("app/**/*.py", "frontend/src/**")
    assert policy.brief_quality.configured is True
    assert policy.brief_quality.key_pattern == r"RON-\d+"
    assert policy.brief_quality.matches_core("app/api/routes.py") is True
    assert policy.brief_quality.matches_core("reviewer/app.py") is False


def test_policy_without_key_keeps_default_and_unconfigured():
    policy = ReviewPolicy.from_yaml("severity_threshold: low\n")
    assert policy.brief_quality.core_paths == DEFAULT_CORE_PATHS
    assert policy.brief_quality.configured is False


def test_load_data_reads_core_paths(monkeypatch):
    """load_data — второй путь чтения политики (MCP), он тоже обязан видеть ключ."""
    from reviewer.config.settings import Settings

    policy = ReviewPolicy.load_data(
        Settings(_env_file=None),
        {"metrics": {"brief_quality": {"core_paths": ["src/**/*.py"]}}},
    )
    assert policy.brief_quality.core_paths == ("src/**/*.py",)
    assert policy.brief_quality.configured is True
```

- [ ] **Step 6: Запустить и убедиться, что падает**

Run: `.venv/bin/pytest tests/policy/test_policy_brief_quality.py -q`
Expected: FAIL — `AttributeError: 'ReviewPolicy' object has no attribute 'brief_quality'`

- [ ] **Step 7: Добавить поле в ReviewPolicy**

В `reviewer/policy/policy.py`:
1. импорт `from reviewer.metrics.brief_quality.config import BriefQualityConfig` (направление
   `policy → metrics` — одностороннее, `metrics` про политику не знает);
2. поле рядом с `context_limits` (строка ~37):

```python
    brief_quality: BriefQualityConfig = field(
        default_factory=BriefQualityConfig)   # ядро метрики брифа (PRI-271), только из .review.yml
```

3. в `from_yaml` (после `context_limits=ContextLimits.from_review_yaml(data),`):

```python
            brief_quality=BriefQualityConfig.from_review_yaml(data),
```

4. в `load_data` (рядом с блоком `if "context_limits" in data:`):

```python
        if "metrics" in data or "task_board" in data:
            policy.brief_quality = BriefQualityConfig.from_review_yaml(data)
```

- [ ] **Step 8: Прогнать оба теста и весь набор политики**

Run: `.venv/bin/pytest tests/policy tests/metrics -q`
Expected: PASS, регрессий нет

- [ ] **Step 9: Коммит**

```bash
git add reviewer/metrics/brief_quality/config.py reviewer/policy/policy.py \
        tests/metrics/test_brief_quality_config.py tests/policy/test_policy_brief_quality.py
git commit -m "feat(metrics): ядро метрики брифа настраивается через .review.yml"
```

---

### Task 2: Функции ядра становятся производными от конфига

**Files:**
- Modify: `reviewer/metrics/brief_quality/classify.py`, `reviewer/metrics/brief_quality/briefs.py:20,159-191`, `reviewer/metrics/brief_quality/context_core.py:17,67`
- Modify (вызывающие): `reviewer/services/brief_quality.py:143,149`, `eval/solve_task_metrics/replay.py:76,153,239`, `eval/solve_task_metrics/snapshot.py:87,94`, `eval/solve_task_metrics/context_seeds.py:35,363,401`
- Modify (реэкспорт): `eval/solve_task_metrics/classify.py`
- Test: `tests/metrics/test_classify.py`, `tests/metrics/test_briefs.py`, `tests/metrics/test_context_core.py`, `tests/metrics/test_reexport_guard.py`

**Interfaces:**
- Consumes: `BriefQualityConfig`, `DEFAULT` из Task 1.
- Produces: `is_core_production_path(path: str, config: BriefQualityConfig) -> bool`;
  `categorize_miss(path: str, existed_before: bool, config: BriefQualityConfig) -> str`;
  `extract_task_key(filename: str, config: BriefQualityConfig) -> str | None`;
  `load_briefs(briefs_dir: pathlib.Path, config: BriefQualityConfig) -> list[BriefRecord]`;
  `derive_context_core(seed_ids, changed_core, traverse, config, allowed_names=None) -> set`.

- [ ] **Step 1: Переписать тесты классификации под обязательный config**

```python
# tests/metrics/test_classify.py — заменить существующие вызовы
import pytest

from reviewer.metrics.brief_quality import classify
from reviewer.metrics.brief_quality.config import DEFAULT, BriefQualityConfig


@pytest.mark.parametrize("path", ["reviewer/app.py", "plugin/hooks/x.py", "sync_chunk.py"])
def test_core_paths_with_default_config(path):
    assert classify.is_core_production_path(path, DEFAULT) is True


def test_foreign_config_moves_the_core():
    config = BriefQualityConfig.from_review_yaml(
        {"metrics": {"brief_quality": {"core_paths": ["app/**/*.py"]}}}
    )
    assert classify.is_core_production_path("app/api/routes.py", config) is True
    assert classify.is_core_production_path("reviewer/app.py", config) is False


def test_categorize_miss_new_file_wins_over_directory():
    assert classify.categorize_miss("reviewer/new.py", existed_before=False, config=DEFAULT) == (
        classify.NEW_FILE_CATEGORY
    )


@pytest.mark.parametrize("path,expected", [
    ("tests/metrics/test_x.py", "tests/"),
    ("docs/superpowers/plans/x.md", "docs/"),
    ("reviewer/index/store.py", "reviewer/index"),   # файл ядра → верхний сегмент + модуль
    ("reviewer/app.py", "reviewer/"),                # ядро без модуля
    ("plugin/skills/x.md", "plugin/"),               # не ядро (исключение) → верхний сегмент
    (".review.yml", "корень"),
])
def test_categorize_miss_is_derived_from_core_paths(path, expected):
    assert classify.categorize_miss(path, existed_before=True, config=DEFAULT) == expected


def test_categorize_miss_follows_foreign_core():
    config = BriefQualityConfig.from_review_yaml(
        {"metrics": {"brief_quality": {"core_paths": ["app/**/*.py"]}}}
    )
    assert classify.categorize_miss("app/api/routes.py", True, config) == "app/api"
    assert classify.categorize_miss("reviewer/index/store.py", True, config) == "reviewer/"
```

- [ ] **Step 2: Запустить и убедиться в падении**

Run: `.venv/bin/pytest tests/metrics/test_classify.py -q`
Expected: FAIL — `TypeError: is_core_production_path() takes 1 positional argument but 2 were given`

- [ ] **Step 3: Переписать classify.py**

```python
"""Классификация путей: что входит в ядро и как называется промах."""
from __future__ import annotations

from reviewer.metrics.brief_quality.config import BriefQualityConfig

NEW_FILE_CATEGORY = "новый файл (не существовал до PR)"
ROOT_CATEGORY = "корень"


def is_core_production_path(path: str, config: BriefQualityConfig) -> bool:
    """Уже существовавший продакшн-код, по которому ретрив может и должен попадать.

    Ядро задаёт `config.core_paths` (по умолчанию — ядро rag_for_git). Всё
    остальное — тесты, доки, конфиги, манифесты — вне ядра: бриф структурно не
    обязан их предсказывать, и включение их в знаменатель делает recall метрикой
    размера diff'а, а не качества ретрива.

    `config` обязателен намеренно: значение по умолчанию вернуло бы ровно тот
    тихий провал, ради которого задача и делалась — чужой репозиторий молча
    считался бы по ядру rag_for_git.
    """
    return config.matches_core(path)


def categorize_miss(path: str, existed_before: bool, config: BriefQualityConfig) -> str:
    """Категория непредсказанного файла.

    Категории выводятся из тех же `core_paths`, что и само ядро: файл ядра
    называется «верхний сегмент + модуль», прочий — верхним сегментом. Прежний
    захардкоженный список ярлыков (`.review.yml/конфиги`, `plugin/skills/*.md`)
    на чужом репозитории врал, а разъехаться с определением ядра теперь нечему.
    Новый файл — отдельная категория: бриф не мог сослаться на файл, которого
    ещё не существовало.
    """
    if not existed_before:
        return NEW_FILE_CATEGORY
    parts = path.split("/")
    if len(parts) == 1:
        return ROOT_CATEGORY
    if config.matches_core(path) and len(parts) > 2:
        return f"{parts[0]}/{parts[1]}"
    return f"{parts[0]}/"
```

- [ ] **Step 4: Прокинуть config в briefs.py**

`_KEY_RE` (строка 20) удаляется. `extract_task_key` и `load_briefs`:

```python
def extract_task_key(filename: str, config: BriefQualityConfig) -> str | None:
    """Ключ задачи из имени файла брифа ('…-PRI-250-…' -> 'PRI-250').

    Паттерн — из конфига репозитория (`task_board.key_pattern`), иначе общий
    `[A-Z]+-\\d+`. Невалидный паттерн, как и в `task_keys.extract_task_keys`,
    даёт предупреждение и пустой результат, а не падение.
    """
    try:
        rx = re.compile(f"({config.key_pattern})", re.IGNORECASE)
    except re.error:
        log.warning("Невалидный key_pattern %r — ключ брифа не извлекается", config.key_pattern)
        return None
    match = rx.search(filename)
    return match.group(1).upper() if match else None


def load_briefs(briefs_dir: pathlib.Path, config: BriefQualityConfig) -> list[BriefRecord]:
    ...
                task_key=extract_task_key(path.name, config),
```

В модуле появляются `import logging` и `log = logging.getLogger(__name__)`, если их ещё нет.

- [ ] **Step 5: Прокинуть config в context_core.py**

```python
def derive_context_core(
    seed_ids: Iterable[str],
    changed_core: Iterable[str],
    traverse: Traversal,
    config: BriefQualityConfig,
    allowed_names: set | None = None,
) -> set:
    ...
    paths = {p for p in node_paths(neighbours) if is_core_production_path(p, config)}
```

`config` ставится ПЕРЕД `allowed_names`, потому что `allowed_names` имеет дефолт; все вызовы
(`eval/solve_task_metrics/context_seeds.py`, `eval/solve_task_metrics/replay.py`) правятся в шаге 6.

- [ ] **Step 6: Прокинуть config во все 8 вызовов**

Найти вызовы: `grep -rn "is_core_production_path\|categorize_miss\|extract_task_key\|load_briefs\|derive_context_core" --include='*.py' reviewer eval`

Правки (конфиг у офлайн-путей берётся из `.review.yml` целевого клона — параметр приходит сверху,
из Task 9; до неё вызывающие в `eval/` принимают `config` аргументом функции и передают дальше):
- `reviewer/services/brief_quality.py:143,149` — `config` уже есть в области видимости (Task 3).
- `eval/solve_task_metrics/replay.py:76,153,239` — функции получают параметр `config` и передают его.
- `eval/solve_task_metrics/snapshot.py:87,94` — `build_snapshot(..., config)`.
- `eval/solve_task_metrics/context_seeds.py:363,401` — функции получают `config`.

- [ ] **Step 7: Расширить guard-тест реэкспорта на config**

```python
# tests/metrics/test_reexport_guard.py — добавить в test_eval_reexports_production_objects
    from eval.solve_task_metrics import config as eval_config
    from reviewer.metrics.brief_quality import config as prod_config

    assert eval_config.BriefQualityConfig is prod_config.BriefQualityConfig
    assert eval_config.DEFAULT is prod_config.DEFAULT
```

и создать реэкспорт `eval/solve_task_metrics/config.py`:

```python
"""Ре-экспорт конфигурации метрики из reviewer/ (PRI-271)."""
from reviewer.metrics.brief_quality.config import (  # noqa: F401
    DEFAULT,
    DEFAULT_BRIEFS_DIR,
    DEFAULT_CORE_PATHS,
    BriefQualityConfig,
)
```

- [ ] **Step 8: Прогнать весь набор**

Run: `.venv/bin/pytest -q`
Expected: PASS. Падения в `tests/eval/*` означают непрокинутый `config` — исправить их, а не ослаблять сигнатуру.

- [ ] **Step 9: Коммит**

```bash
git add reviewer/metrics eval/solve_task_metrics tests/metrics
git commit -m "refactor(metrics): предикат ядра и ключ задачи берут конфиг репозитория"
```

---

### Task 3: Статус «ядро не сконфигурировано» и каталог брифов из конфига

**Files:**
- Modify: `reviewer/services/brief_quality.py:25,54-84,87-168`
- Test: `tests/services/test_brief_quality.py`

**Interfaces:**
- Consumes: `BriefQualityConfig` (Task 1), `classify.*` с обязательным `config` (Task 2).
- Produces: `STATUS_UNCONFIGURED_CORE = "unconfigured_core_denominator"`;
  `find_brief(clone_path, task_key, config) -> pathlib.Path | None`;
  `measure(*, task_key, clone_path, changed_paths, changed_status, config) -> BriefQualityMeasurement`.

- [ ] **Step 1: Написать падающие тесты статуса и чужого ядра**

```python
# tests/services/test_brief_quality.py — добавить
from reviewer.metrics.brief_quality.config import DEFAULT, BriefQualityConfig
from reviewer.services import brief_quality


def _write_brief(tmp_path, name, relevant):
    directory = tmp_path / "docs" / "superpowers" / "briefs"
    directory.mkdir(parents=True, exist_ok=True)
    lines = "\n".join(f"- `{path}` — зачем" for path in relevant)
    (directory / name).write_text(f"# Brief\n\n## Relevant code\n{lines}\n", encoding="utf-8")


def test_empty_core_without_config_is_unconfigured(tmp_path):
    """Чужой репозиторий без ключа: знаменатель пуст, но это не «пустое ядро»."""
    _write_brief(tmp_path, "2026-08-26-RON-55-x.md", ["app/api/routes.py"])
    out = brief_quality.measure(
        task_key="RON-55",
        clone_path=str(tmp_path),
        changed_paths=["app/api/routes.py"],
        changed_status={"app/api/routes.py": "modified"},
        config=BriefQualityConfig.from_review_yaml({"task_board": {"key_pattern": r"RON-\d+"}}),
    )
    assert out.status == brief_quality.STATUS_UNCONFIGURED_CORE
    assert out.core_recall is None


def test_empty_core_with_config_stays_empty_core(tmp_path):
    """Настроенный репозиторий с диффом из одних доков — честное «ядро пусто»."""
    _write_brief(tmp_path, "2026-08-26-RON-55-x.md", ["app/api/routes.py"])
    out = brief_quality.measure(
        task_key="RON-55",
        clone_path=str(tmp_path),
        changed_paths=["docs/readme.md"],
        changed_status={"docs/readme.md": "modified"},
        config=BriefQualityConfig.from_review_yaml(
            {"metrics": {"brief_quality": {"core_paths": ["app/**/*.py"]}}}
        ),
    )
    assert out.status == brief_quality.STATUS_EMPTY_CORE


def test_foreign_repo_core_recall_11_of_13(tmp_path):
    """Критерий 1 PRI-271 на управляемых данных: ядро app/** + frontend/src/**.

    13 файлов ядра существовали до PR, бриф предсказал 11 — core-recall 11/13.
    """
    expected_core = [f"app/mod{i}.py" for i in range(9)] + [
        f"frontend/src/comp{i}.tsx" for i in range(4)
    ]
    predicted = expected_core[:11]
    _write_brief(tmp_path, "2026-08-26-RON-55-x.md", predicted)
    changed_status = {path: "modified" for path in expected_core}
    changed_status["tests/test_x.py"] = "modified"          # вне ядра
    changed_status["app/new.py"] = "added"                  # ядро, но новый файл
    out = brief_quality.measure(
        task_key="RON-55",
        clone_path=str(tmp_path),
        changed_paths=list(changed_status),
        changed_status=changed_status,
        config=BriefQualityConfig.from_review_yaml({
            "task_board": {"key_pattern": r"RON-\d+"},
            "metrics": {"brief_quality": {"core_paths": ["app/**/*.py", "frontend/src/**"]}},
        }),
    )
    assert out.status == brief_quality.STATUS_MEASURED
    assert (out.hit_core, out.expected_core) == (11, 13)
    assert round(out.core_recall, 4) == round(11 / 13, 4)
```

- [ ] **Step 2: Запустить и убедиться в падении**

Run: `.venv/bin/pytest tests/services/test_brief_quality.py -q`
Expected: FAIL — `measure() got an unexpected keyword argument 'config'`

- [ ] **Step 3: Реализовать**

В `reviewer/services/brief_quality.py`:
1. удалить модульную константу `BRIEFS_DIR` (строка 25) — её роль занимает `config.briefs_dir`;
2. `find_brief(clone_path, task_key, config)` использует `config.briefs_dir`;
3. `measure(..., config)` прокидывает `config` в `classify.*` (строки 143, 149) и выбирает статус:

```python
STATUS_UNCONFIGURED_CORE = "unconfigured_core_denominator"
...
    if expected_core:
        status = STATUS_MEASURED
    elif config.configured:
        status = STATUS_EMPTY_CORE
    else:
        # Ядро пусто И ключа в .review.yml нет: скорее всего репозиторий просто
        # не настроен, и молчаливый ноль здесь неотличим от честного «в диффе
        # только тесты и доки» (критерий 3 PRI-271).
        status = STATUS_UNCONFIGURED_CORE
```

- [ ] **Step 4: Прогнать тесты сервиса**

Run: `.venv/bin/pytest tests/services/test_brief_quality.py -q`
Expected: PASS

- [ ] **Step 5: Обновить комментарий статусов в схеме**

В `reviewer/web/schema.sql` строка-комментарий у колонки `status` дополняется
`| unconfigured_core_denominator`.

- [ ] **Step 6: Коммит**

```bash
git add reviewer/services/brief_quality.py reviewer/web/schema.sql tests/services/test_brief_quality.py
git commit -m "feat(metrics): отдельный статус для ненастроенного ядра репозитория"
```

---

### Task 4: `ground_truth` переезжает в ядро и отдаёт номер PR

**Files:**
- Create: `reviewer/metrics/brief_quality/ground_truth.py` (перенос содержимого `eval/solve_task_metrics/ground_truth.py`)
- Modify: `eval/solve_task_metrics/ground_truth.py` (становится реэкспортом), `tests/metrics/test_reexport_guard.py`
- Modify (вызывающие `filter_pr_merges`/`collect`): найти `grep -rn "filter_pr_merges\|ground_truth.collect" --include='*.py' eval tests`
- Test: `tests/metrics/test_ground_truth.py` (перенос из `tests/eval/`, если есть, плюс новые)

**Interfaces:**
- Produces: `filter_pr_merges(rows) -> tuple[list[PRMerge], int]`, где
  `PRMerge = NamedTuple("PRMerge", [("sha", str), ("number", int)])`;
  `TaskGroundTruth.merges: list[PRMerge]` (поле `merge_shas` сохраняется как свойство для
  совместимости чтения); `collect(task_key, run_git) -> TaskGroundTruth`;
  `changed_status(sha, run_git) -> dict[str, str]` — статусы файлов PR-мержа.

- [ ] **Step 1: Тест номера PR и статусов файлов**

```python
# tests/metrics/test_ground_truth.py
from reviewer.metrics.brief_quality import ground_truth


def test_filter_pr_merges_extracts_number():
    rows = [
        ("abc", "Merge pull request #232 from mimfort/feat/pri-275"),
        ("def", "Merge remote-tracking branch 'origin/dev' into feat/pri-275"),
    ]
    merges, skipped = ground_truth.filter_pr_merges(rows)
    assert [(m.sha, m.number) for m in merges] == [("abc", 232)]
    assert skipped == 1


def test_changed_status_parses_name_status():
    def run_git(args):
        assert args[:2] == ["diff", "--name-status"]
        return "M\treviewer/app.py\nA\treviewer/new.py\nR100\told.py\tnew.py\n"

    assert ground_truth.changed_status("abc", run_git) == {
        "reviewer/app.py": "modified",
        "reviewer/new.py": "added",
        "new.py": "renamed",
    }
```

- [ ] **Step 2: Запустить, убедиться в падении**

Run: `.venv/bin/pytest tests/metrics/test_ground_truth.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'reviewer.metrics.brief_quality.ground_truth'`

- [ ] **Step 3: Перенести модуль и добавить две функции**

`git mv eval/solve_task_metrics/ground_truth.py reviewer/metrics/brief_quality/ground_truth.py`, затем:

```python
class PRMerge(NamedTuple):
    """Настоящий PR-мерж: sha коммита и номер PR из его субъекта."""

    sha: str
    number: int


PR_MERGE_SUBJECT_RE = re.compile(r"^Merge pull request #(\d+) from ", re.IGNORECASE)


def filter_pr_merges(rows: list) -> tuple:
    """Оставить только настоящие PR-мержи, вытащив номер PR из субъекта.

    Номер нужен строке измерения: её идентичность — (repo, pr_number, task_key).
    """
    merges: list[PRMerge] = []
    skipped = 0
    for sha, subject in rows:
        match = PR_MERGE_SUBJECT_RE.match(subject.strip())
        if match:
            merges.append(PRMerge(sha, int(match.group(1))))
        else:
            skipped += 1
    return merges, skipped


def changed_status(sha: str, run_git: GitRunner) -> dict:
    """Статусы файлов PR-мержа: путь → added|modified|removed|renamed|copied.

    Источник — `git diff --name-status <sha>^1 <sha>`; для переименования и
    копирования берётся НОВЫЙ путь, как и в `vcs.get_changed_files`, чтобы
    офлайн и онлайн считали одним множеством.
    """
    letters = {"A": "added", "M": "modified", "D": "removed",
               "R": "renamed", "C": "copied", "T": "modified"}
    try:
        out = run_git(["diff", "--name-status", f"{sha}^1", sha])
    except GitError:
        return {}
    result: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = letters.get(parts[0][0], "modified")
        result[parts[-1]] = status
    return result
```

`TaskGroundTruth` получает поле `merges: list[PRMerge]`, а `merge_shas` остаётся свойством:

```python
    @property
    def merge_shas(self) -> list:
        """Только sha — для кода, которому номер PR не нужен."""
        return [m.sha for m in self.merges]
```

- [ ] **Step 4: Сделать eval-модуль реэкспортом**

```python
# eval/solve_task_metrics/ground_truth.py
"""Ре-экспорт ground truth из reviewer/ (перенос по PRI-271).

Логика «какой мерж считать настоящим PR» одна на офлайн-харнесс и на команду
`reviewer measure-briefs`: две копии разъехались бы ровно так, как страхует
tests/metrics/test_reexport_guard.py.
"""
from reviewer.metrics.brief_quality.ground_truth import (  # noqa: F401
    PR_MERGE_SUBJECT_RE,
    GitError,
    GitRunner,
    PRMerge,
    TaskGroundTruth,
    changed_files,
    changed_status,
    collect,
    filter_pr_merges,
    git_runner,
    merge_rows,
    path_existed,
)
```

и добавить в guard-тест:

```python
    assert eval_ground_truth.collect is prod_ground_truth.collect
    assert eval_ground_truth.filter_pr_merges is prod_ground_truth.filter_pr_merges
```

- [ ] **Step 5: Прогнать весь набор**

Run: `.venv/bin/pytest -q`
Expected: PASS. Вызывающие `filter_pr_merges` получают теперь `PRMerge`, а не строку — поправить их.

- [ ] **Step 6: Коммит**

```bash
git add reviewer/metrics/brief_quality/ground_truth.py eval/solve_task_metrics/ground_truth.py \
        tests/metrics/test_ground_truth.py tests/metrics/test_reexport_guard.py
git commit -m "refactor(metrics): ground truth задачи переезжает в ядро и отдаёт номер PR"
```

---

### Task 5: Схема `brief_quality` — nullable `run_id` и идемпотентность

**Files:**
- Modify: `reviewer/web/schema.sql:99-126`, `reviewer/web/history.py:526-581`
- Test: `tests/web/test_history.py`

**Interfaces:**
- Produces: `ReviewHistory.record_brief_quality(run_id: int | None, repo, pr_number, head_sha, measurement) -> int | None`
  — идемпотентен по `(repo, pr_number, COALESCE(task_key,''))`.

- [ ] **Step 1: Тест формы SQL (без БД)**

```python
# tests/web/test_history.py — добавить
def test_record_brief_quality_sql_is_idempotent_and_nullable(monkeypatch):
    """Запись обязана быть UPSERT'ом по идентичности строки, а run_id — необязательным.

    Проверяется фактический SQL, отданный драйверу: без ON CONFLICT повторный
    finish_task создаст вторую строку, а без COALESCE уникальность не покроет
    строки с task_key IS NULL (в SQL NULL ≠ NULL).
    """
    captured = {}

    class _Cur:
        def fetchone(self):
            return (1,)

    class _Conn:
        def execute(self, sql, params):
            captured["sql"] = sql
            captured["params"] = params
            return _Cur()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    history = ReviewHistory("postgresql://unused")
    monkeypatch.setattr(history, "_connect", lambda: _Conn())
    measurement = SimpleNamespace(
        task_key="PRI-1", status="measured", brief_path="b.md", expected=1,
        expected_core=1, predicted=1, hit_core=1, core_recall=1.0, raw_recall=1.0,
        precision=1.0, misses={}, predicted_paths=(), expected_core_paths=(),
        hit_core_paths=(),
    )
    assert history.record_brief_quality(None, "o/r", 7, "sha", measurement) == 1
    assert "ON CONFLICT" in captured["sql"]
    assert "COALESCE(task_key, '')" in captured["sql"]
    assert "COALESCE(EXCLUDED.run_id, brief_quality.run_id)" in captured["sql"]
    assert captured["params"]["run_id"] is None
```

- [ ] **Step 2: Запустить, убедиться в падении**

Run: `.venv/bin/pytest tests/web/test_history.py -q -k idempotent`
Expected: FAIL — `assert "ON CONFLICT" in ...`

- [ ] **Step 3: Миграция схемы**

В конец блока `brief_quality` в `reviewer/web/schema.sql` (после существующих индексов):

```sql
-- PRI-270: съём метрики переехал из publish_review в finish_task и CLI, где
-- прогона ревью нет вовсе, поэтому run_id перестаёт быть обязательным. FK с
-- ON DELETE CASCADE при этом не трогаем: NULL ему не подчиняется.
ALTER TABLE brief_quality ALTER COLUMN run_id DROP NOT NULL;

-- Схлопывание дублей перед уникальным индексом: до PRI-270 идентичности у
-- строки не было, и на деплое с историей их может оказаться несколько.
-- Выживает последняя (максимальный id) — она же самая свежая.
DELETE FROM brief_quality a
    USING brief_quality b
    WHERE a.repo = b.repo
      AND a.pr_number = b.pr_number
      AND COALESCE(a.task_key, '') = COALESCE(b.task_key, '')
      AND a.id < b.id;

-- COALESCE обязателен: в SQL NULL ≠ NULL, и обычный UNIQUE не покрыл бы
-- строки без task_key — а именно они пишутся при съёме без ключа задачи.
CREATE UNIQUE INDEX IF NOT EXISTS brief_quality_identity
    ON brief_quality (repo, pr_number, (COALESCE(task_key, '')));
```

- [ ] **Step 4: UPSERT в `record_brief_quality`**

Сигнатура `run_id: int | None`; SQL получает хвост:

```sql
        ) ON CONFLICT (repo, pr_number, (COALESCE(task_key, ''))) DO UPDATE SET
            created_at          = now(),
            head_sha            = EXCLUDED.head_sha,
            status              = EXCLUDED.status,
            brief_path          = EXCLUDED.brief_path,
            expected            = EXCLUDED.expected,
            expected_core       = EXCLUDED.expected_core,
            predicted           = EXCLUDED.predicted,
            hit_core            = EXCLUDED.hit_core,
            core_recall         = EXCLUDED.core_recall,
            raw_recall          = EXCLUDED.raw_recall,
            precision           = EXCLUDED.precision,
            misses              = EXCLUDED.misses,
            predicted_paths     = EXCLUDED.predicted_paths,
            expected_core_paths = EXCLUDED.expected_core_paths,
            hit_core_paths      = EXCLUDED.hit_core_paths,
            run_id              = COALESCE(EXCLUDED.run_id, brief_quality.run_id)
        RETURNING id
```

Докстринг дополняется абзацем: `finish_task` пишет `run_id=NULL` и не затирает уже
проставленный `run_id`, а `publish_review` дописывает его в строку, созданную ранее.

- [ ] **Step 5: Integration-тест идемпотентности**

```python
# tests/web/test_history_integration.py (или существующий integration-модуль истории)
@pytest.mark.integration
def test_second_write_updates_row_and_keeps_run_id(test_history):
    """Повтор съёма не плодит строку, а run_id дописывается позже (крит. 2 и 5 PRI-270)."""
    measurement = _measurement(status="measured", core_recall=0.5)
    test_history.record_brief_quality(None, "o/r", 7, "sha1", measurement)
    test_history.record_brief_quality(None, "o/r", 7, "sha1", measurement)
    rows = _select_all(test_history, repo="o/r", pr_number=7)
    assert len(rows) == 1 and rows[0]["run_id"] is None

    run_id = test_history.record_run(...)   # реальный прогон ревью
    test_history.record_brief_quality(run_id, "o/r", 7, "sha2", measurement)
    rows = _select_all(test_history, repo="o/r", pr_number=7)
    assert len(rows) == 1 and rows[0]["run_id"] == run_id
```

- [ ] **Step 6: Прогнать unit и integration**

Run: `.venv/bin/pytest tests/web -q`
Run: `docker compose --profile test up -d --wait paradedb-test neo4j-test && .venv/bin/pytest -q -m integration -k brief_quality`
Expected: PASS

- [ ] **Step 7: Коммит**

```bash
git add reviewer/web/schema.sql reviewer/web/history.py tests/web
git commit -m "feat(web): строка качества брифа идемпотентна и живёт без прогона ревью"
```

---

### Task 6: Сервисный путь съёма `measure_and_record`

**Files:**
- Modify: `reviewer/services/brief_quality.py` (новая функция), `reviewer/mcp/service.py:3240-3273`
- Test: `tests/services/test_brief_quality.py`, `tests/mcp/test_publish.py:638-654`

**Interfaces:**
- Consumes: `measure(...)` (Task 3), `ReviewHistory.record_brief_quality` (Task 5).
- Produces:
  `measure_and_record(*, task_key, repo, pr_number, head_sha, changed_paths, changed_status, clone_path, config, history, run_id=None) -> str | None`
  — возвращает `status` измерения либо `None`, если запись не состоялась; никогда не бросает.

- [ ] **Step 1: Тест сервисной функции**

```python
def test_measure_and_record_writes_row_and_returns_status(tmp_path):
    _write_brief(tmp_path, "2026-09-01-PRI-1-x.md", ["reviewer/app.py"])
    written = {}

    class _History:
        def record_brief_quality(self, run_id, repo, pr_number, head_sha, measurement):
            written.update(run_id=run_id, repo=repo, pr=pr_number, status=measurement.status)
            return 1

    status = brief_quality.measure_and_record(
        task_key="PRI-1", repo="o/r", pr_number=7, head_sha="sha",
        changed_paths=["reviewer/app.py"], changed_status={"reviewer/app.py": "modified"},
        clone_path=str(tmp_path), config=DEFAULT, history=_History(),
    )
    assert status == brief_quality.STATUS_MEASURED
    assert written == {"run_id": None, "repo": "o/r", "pr": 7,
                       "status": brief_quality.STATUS_MEASURED}


def test_measure_and_record_is_fail_soft_on_history_error(tmp_path):
    """Сбой записи не смеет прорваться наружу: метрика наблюдает, а не управляет."""
    _write_brief(tmp_path, "2026-09-01-PRI-1-x.md", ["reviewer/app.py"])

    class _Boom:
        def record_brief_quality(self, *a, **kw):
            raise RuntimeError("БД недоступна")

    assert brief_quality.measure_and_record(
        task_key="PRI-1", repo="o/r", pr_number=7, head_sha=None,
        changed_paths=[], changed_status={"reviewer/app.py": "modified"},
        clone_path=str(tmp_path), config=DEFAULT, history=_Boom(),
    ) is None


def test_measure_and_record_without_history_returns_none(tmp_path):
    assert brief_quality.measure_and_record(
        task_key="PRI-1", repo="o/r", pr_number=7, head_sha=None,
        changed_paths=[], changed_status={}, clone_path=str(tmp_path),
        config=DEFAULT, history=None,
    ) is None
```

- [ ] **Step 2: Запустить, убедиться в падении**

Run: `.venv/bin/pytest tests/services/test_brief_quality.py -q -k measure_and_record`
Expected: FAIL — `AttributeError: module has no attribute 'measure_and_record'`

- [ ] **Step 3: Реализовать**

```python
def measure_and_record(
    *,
    task_key: str | None,
    repo: str,
    pr_number: int,
    head_sha: str | None,
    changed_paths: list,
    changed_status: dict,
    clone_path: str | None,
    config,
    history,
    run_id: int | None = None,
) -> str | None:
    """Посчитать качество брифа и записать строку. Никогда не бросает.

    Общий путь трёх точек съёма: publish_review (с run_id), finish_task и CLI
    measure-briefs (без него). Возвращает status измерения — его показывает
    вызывающий; None означает, что записи не было (нет истории или сбой).
    """
    if history is None:
        return None
    try:
        measurement = measure(
            task_key=task_key,
            clone_path=clone_path,
            changed_paths=changed_paths,
            changed_status=changed_status,
            config=config,
        )
        history.record_brief_quality(run_id, repo, pr_number, head_sha, measurement)
        return measurement.status
    except Exception:  # noqa: BLE001 — наблюдаемость не роняет вызывающий поток
        log.warning("Не удалось снять качество брифа для %s pr:%s", repo, pr_number,
                    exc_info=True)
        return None
```

- [ ] **Step 4: Перевести `_record_brief_quality` на сервис**

`reviewer/mcp/service.py:3240-3273` — тело сводится к сборке аргументов:

```python
    def _record_brief_quality(
        self, repo: str, pr: int, p: PreparedReview, run_id: int, task_key: str | None,
    ) -> None:
        """Съём качества брифа на публикации ревью (PRI-249).

        Гейт вызова и его семантика не меняются; сам расчёт и запись живут в
        reviewer.services.brief_quality.measure_and_record, общем с finish_task.
        """
        from reviewer.services import brief_quality

        key = task_key
        if not key and p.task_keys:
            key = p.task_keys.get("primary")
        policy, _meta = self._resolve_policy(repo, p.prq.base_ref)
        brief_quality.measure_and_record(
            task_key=key, repo=repo, pr_number=pr, head_sha=p.prq.head_sha,
            changed_paths=p.changed_paths, changed_status=p.changed_status,
            clone_path=self._repo_clone_path(repo), config=policy.brief_quality,
            history=self._review_service._ensure_history(), run_id=run_id,
        )
```

Гейт на строке 3199 не трогается.

- [ ] **Step 5: Прогнать тесты publish и сервиса**

Run: `.venv/bin/pytest tests/services/test_brief_quality.py tests/mcp/test_publish.py -q`
Expected: PASS — существующий `test_publish_records_brief_quality` остаётся зелёным без правок теста

- [ ] **Step 6: Коммит**

```bash
git add reviewer/services/brief_quality.py reviewer/mcp/service.py tests
git commit -m "refactor(mcp): съём качества брифа вынесен в сервисный путь"
```

---

### Task 7: Съём в `finish_task`

**Files:**
- Modify: `reviewer/mcp/service.py:1097-1136` (`_backlink_pr` → `_pr_session` + `_apply_backlink`), `:1395-1452` (`finish_task`)
- Test: `tests/mcp/test_finish_task.py`

**Interfaces:**
- Consumes: `measure_and_record` (Task 6), `parse_pr_url`/`PRTarget` (`reviewer/tasks/pr_backlink.py:26-52`),
  `VCSProvider.get_changed_files(number) -> list[ChangedFile]` (`reviewer/vcs/base.py:89-98`).
- Produces: ответ `finish_task` дополняется ключом `brief_quality_status: str | None`.

- [ ] **Step 1: Тесты съёма и его fail-soft**

```python
def test_finish_task_records_brief_quality(monkeypatch, tmp_path):
    """Съём метрики на закрытии задачи: строка пишется, run_id остаётся пустым."""
    _write_brief(tmp_path, "2026-09-01-PRI-1-x.md", ["reviewer/app.py"])
    recorded = {}
    svc = _service_with_board(monkeypatch, tmp_path, recorded)   # хелпер модуля
    out = svc.finish_task("PRI-1", "https://github.com/o/r/pull/7")
    assert out["status"] == "ok"
    assert out["brief_quality_status"] == "measured"
    assert recorded["run_id"] is None and recorded["pr_number"] == 7


def test_finish_task_survives_metric_failure(monkeypatch, tmp_path):
    """Полный отказ съёма не меняет прежний результат finish_task (крит. 4 PRI-270)."""
    svc = _service_with_board(monkeypatch, tmp_path, {}, vcs_raises=True)
    out = svc.finish_task("PRI-1", "https://github.com/o/r/pull/7")
    assert out["status"] == "ok"
    assert out["task_link_status"] in {"added", "already_present", "failed"}
    assert out["brief_quality_status"] is None


def test_finish_task_opens_vcs_once(monkeypatch, tmp_path):
    """Бэклинк и съём делят одно соединение: PR-ссылка резолвится один раз."""
    opened = []
    svc = _service_with_board(monkeypatch, tmp_path, {}, on_vcs_open=opened.append)
    svc.finish_task("PRI-1", "https://github.com/o/r/pull/7")
    assert len(opened) == 1
```

- [ ] **Step 2: Запустить, убедиться в падении**

Run: `.venv/bin/pytest tests/mcp/test_finish_task.py -q -k brief_quality`
Expected: FAIL — `KeyError: 'brief_quality_status'`

- [ ] **Step 3: Ввести общий контекст PR-сессии**

```python
    @contextmanager
    def _pr_session(self, pr_url: str):
        """Открыть VCS по ссылке на PR один раз на все операции finish_task.

        Никогда не бросает: отдаёт (target, vcs, error). Ни бэклинк, ни съём
        метрики не смеют уронить finish_task — доска к этому моменту уже
        записана, и её успех не отменяется ничем из последующего.
        """
        from reviewer.tasks.pr_backlink import parse_pr_url

        target = parse_pr_url(pr_url)
        if target is None:
            yield None, None, f"{pr_url!r} не распознан как ссылка на PR/MR"
            return
        vcs = None
        try:
            vcs = (self._vcs_factory(target.owner, target.repo) if self._vcs_factory
                   else self._review_service._create_vcs_provider(
                       target.owner, target.repo,
                       platform=target.platform, base_url=target.base_url))
            yield target, vcs, None
        except Exception as exc:  # noqa: BLE001
            log.warning("не удалось открыть VCS для %s", pr_url, exc_info=True)
            yield target, None, str(exc)
        finally:
            if vcs is not None and self._vcs_factory is None:
                try:
                    vcs.close()
                except Exception:
                    log.warning("не удалось закрыть VCS после finish_task", exc_info=True)
```

`_backlink_pr(pr_url, key, task_url)` превращается в `_apply_backlink(target, vcs, key, task_url)`
с прежней логикой трёх исходов (`added` / `already_present` / `failed`) и прежними текстами
предупреждений; открытие и закрытие VCS уходят в `_pr_session`.

- [ ] **Step 4: Снять метрику в `finish_task`**

```python
                brief = self._write_through(resolved.provider, key)
                task_url = (brief or {}).get("url") or ""
                metric_status = None
                with self._pr_session(pr_url) as (target, vcs, error):
                    if vcs is None:
                        link_status, link_warnings = "failed", [
                            f"ссылка на задачу не добавлена в PR: {error}"]
                    else:
                        link_status, link_warnings = self._apply_backlink(
                            target, vcs, key, task_url)
                        metric_status = self._record_finish_task_metric(target, vcs, key)
```

и приватный метод:

```python
    def _record_finish_task_metric(self, target, vcs, key: str) -> str | None:
        """Снять качество брифа по факту закрытия задачи (PRI-270).

        Знаменатель — ПОЛНЫЙ дифф PR, как и на публикации ревью: иначе числа
        двух точек съёма посчитаны разными линейками. Полностью fail-soft.
        """
        from reviewer.services import brief_quality

        repo = f"{target.owner}/{target.repo}"
        try:
            changed = vcs.get_changed_files(target.number)
            status_map = {item.path: item.status for item in changed}
            head_sha = vcs.get_pull_request(target.number).head_sha
            policy, _meta = self._resolve_policy(repo, self._bug_branches(repo)[0])
            return brief_quality.measure_and_record(
                task_key=key, repo=repo, pr_number=target.number, head_sha=head_sha,
                changed_paths=list(status_map), changed_status=status_map,
                clone_path=self._repo_clone_path(repo), config=policy.brief_quality,
                history=self._review_service._ensure_history(), run_id=None,
            )
        except Exception:  # noqa: BLE001 — наблюдаемость не роняет закрытие задачи
            log.warning("Не удалось снять качество брифа на finish_task для %s", repo,
                        exc_info=True)
            return None
```

Ответ дополняется `"brief_quality_status": metric_status` рядом с `"task_link_status"`.

- [ ] **Step 5: Прогнать тесты MCP**

Run: `.venv/bin/pytest tests/mcp -q`
Expected: PASS, включая прежние тесты бэклинка

- [ ] **Step 6: Коммит**

```bash
git add reviewer/mcp/service.py tests/mcp/test_finish_task.py
git commit -m "feat(mcp): finish_task снимает качество брифа без прогона ревью"
```

---

### Task 8: CLI `reviewer measure-briefs`

**Files:**
- Modify: `reviewer/entrypoints/cli.py` (новая команда рядом со `status`, `:1181`)
- Test: `tests/entrypoints/test_measure_briefs.py`

**Interfaces:**
- Consumes: `ground_truth.collect`/`changed_status` (Task 4), `measure_and_record` (Task 6),
  `BriefQualityConfig` (Task 1), `ReviewHistory` (`reviewer/web/history.py`).
- Produces: команда `reviewer measure-briefs [PATH] [--repo] [--briefs-dir] [--json]`;
  чистая функция `measure_corpus(clone_path, repo, config, run_git, history) -> dict` в
  `reviewer/metrics/brief_quality/corpus.py` — считает и возвращает сводку по статусам.

- [ ] **Step 1: Тест пересчёта корпуса на фейковом git**

```python
# tests/entrypoints/test_measure_briefs.py
from reviewer.metrics.brief_quality.corpus import measure_corpus
from reviewer.metrics.brief_quality.config import DEFAULT


def test_measure_corpus_writes_row_per_pr(tmp_path):
    """Строка на PR-мерж, а не на задачу: идентичность строки — (repo, pr, task_key)."""
    briefs = tmp_path / "docs" / "superpowers" / "briefs"
    briefs.mkdir(parents=True)
    (briefs / "2026-09-01-PRI-1-x.md").write_text(
        "# Brief\n\n## Relevant code\n- `reviewer/app.py` — зачем\n", encoding="utf-8")

    def run_git(args):
        if args[0] == "log":
            return "abc Merge pull request #7 from o/feat/pri-1\n"
        if args[:2] == ["diff", "--name-status"]:
            return "M\treviewer/app.py\n"
        raise AssertionError(args)

    rows = []

    class _History:
        def record_brief_quality(self, run_id, repo, pr_number, head_sha, measurement):
            rows.append((run_id, repo, pr_number, measurement.status))
            return len(rows)

    summary = measure_corpus(str(tmp_path), "o/r", DEFAULT, run_git, _History())
    assert rows == [(None, "o/r", 7, "measured")]
    assert summary["measured"] == 1 and summary["briefs"] == 1


def test_measure_corpus_skips_brief_without_key(tmp_path):
    briefs = tmp_path / "docs" / "superpowers" / "briefs"
    briefs.mkdir(parents=True)
    (briefs / "2026-09-01-no-key.md").write_text("# Brief\n", encoding="utf-8")
    summary = measure_corpus(str(tmp_path), "o/r", DEFAULT, lambda args: "", None)
    assert summary["skipped_no_key"] == 1
```

- [ ] **Step 2: Запустить, убедиться в падении**

Run: `.venv/bin/pytest tests/entrypoints/test_measure_briefs.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'reviewer.metrics.brief_quality.corpus'`

- [ ] **Step 3: Реализовать `corpus.py`**

```python
"""Пересчёт качества брифов по PR-мержам клона (PRI-270).

Git приходит инъекцией (GitRunner), история — объектом с record_brief_quality:
модуль тестируется без git-репозитория и без Postgres. Voyage не задействован
вовсе — метрика ничего не эмбеддит.
"""

def measure_corpus(clone_path, repo, config, run_git, history) -> dict:
    """Посчитать корпус брифов клона и записать строку на каждый PR-мерж.

    Строка на PR, а не на задачу: идентичность строки измерения —
    (repo, pr_number, task_key), а task-level число собирается union'ом на
    чтении (ReviewHistory.brief_quality_trend) — той же линейкой, которой
    считает офлайн-baseline.
    """
    from reviewer.metrics.brief_quality import briefs as briefs_mod
    from reviewer.metrics.brief_quality import ground_truth
    from reviewer.services import brief_quality

    directory = pathlib.Path(clone_path) / config.briefs_dir
    summary = {"briefs": 0, "skipped_no_key": 0, "skipped_no_merges": 0, "rows": 0}
    for record in briefs_mod.load_briefs(directory, config):
        summary["briefs"] += 1
        if not record.task_key:
            summary["skipped_no_key"] += 1
            continue
        truth = ground_truth.collect(record.task_key, run_git)
        if not truth.merges:
            summary["skipped_no_merges"] += 1
            continue
        for merge in truth.merges:
            status_map = ground_truth.changed_status(merge.sha, run_git)
            status = brief_quality.measure_and_record(
                task_key=record.task_key, repo=repo, pr_number=merge.number,
                head_sha=merge.sha, changed_paths=list(status_map),
                changed_status=status_map, clone_path=clone_path, config=config,
                history=history, run_id=None,
            )
            summary["rows"] += 1
            summary[status or "not_recorded"] = summary.get(status or "not_recorded", 0) + 1
    return summary
```

- [ ] **Step 4: Добавить команду CLI**

Новые импорты в `reviewer/entrypoints/cli.py` (остальные уже есть — `psycopg`, `yaml`,
`json`, `Path`, `_resolve_repo`):

```python
from reviewer.metrics.brief_quality import ground_truth
from reviewer.metrics.brief_quality.config import BriefQualityConfig
from reviewer.metrics.brief_quality.corpus import measure_corpus
from reviewer.web.history import ReviewHistory
```

```python
@cli.command("measure-briefs")
@click.argument("path", default=".")
@click.option("--repo", "repo_tag", default=None,
              help="owner/name тег строк метрики; по умолчанию из git remote origin")
@click.option("--briefs-dir", "briefs_dir", default=None,
              help="каталог брифов относительно клона; по умолчанию docs/superpowers/briefs")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="машиночитаемый JSON вместо текста")
def measure_briefs(path, repo_tag, briefs_dir, as_json):
    """Пересчитать качество брифов по PR-мержам клона (не тратит Voyage)."""
    s = Settings()
    resolution = _resolve_repo(repo_tag, path, s)
    yaml_text = pathlib.Path(path, ".review.yml")
    data = yaml.safe_load(yaml_text.read_text(encoding="utf-8")) if yaml_text.exists() else {}
    config = BriefQualityConfig.from_review_yaml(data or {}, briefs_dir=briefs_dir)
    history = ReviewHistory(s.pg_dsn, min_size=s.pg_pool_min_size, max_size=s.pg_pool_max_size)
    try:
        summary = measure_corpus(path, resolution.repo, config,
                                 ground_truth.git_runner(pathlib.Path(path)), history)
    except psycopg.OperationalError as e:
        raise click.ClickException(f"Postgres недоступен: {e}")
    finally:
        history.close()
    click.echo(json.dumps(summary, ensure_ascii=False) if as_json else _render_measure(summary))
```

`_render_measure` печатает по-русски: сколько брифов, сколько строк записано, разбивка по
статусам и сколько брифов пропущено (без ключа, без PR-мержей).

- [ ] **Step 5: Прогнать тесты**

Run: `.venv/bin/pytest tests/entrypoints -q`
Expected: PASS

- [ ] **Step 6: Дополнить README (обе версии) разделом о команде**

В `README.md` и `README.ru.md` — строка в списке команд CLI:
`reviewer measure-briefs` — пересчёт качества брифов по PR-мержам клона, без обращений к Voyage.

- [ ] **Step 7: Коммит**

```bash
git add reviewer/metrics/brief_quality/corpus.py reviewer/entrypoints/cli.py \
        tests/entrypoints/test_measure_briefs.py README.md README.ru.md
git commit -m "feat(cli): команда measure-briefs пересчитывает качество брифов из git"
```

---

### Task 9: Офлайн-харнесс считает чужой клон

**Files:**
- Modify: `eval/solve_task_metrics/__main__.py:28-29` и команды `cmd_snapshot` (:45), `cmd_forecast` (:141), `cmd_replay` (:232), `cmd_subqueries` (:324)
- Modify: `eval/solve_task_metrics/{snapshot,replay,context_seeds}.py` — прокид `config` из аргументов команды
- Test: `tests/eval/test_snapshot.py`, `tests/eval/test_replay.py`

**Interfaces:**
- Consumes: `BriefQualityConfig.from_review_yaml` (Task 1), функции ядра с обязательным `config` (Task 2).
- Produces: у всех четырёх команд флаги `--repo-path` (дефолт — корень этого репозитория) и
  `--briefs-dir` (дефолт — `<repo-path>/docs/superpowers/briefs`); история и отчёт пишутся в
  `<repo-path>/eval/`.

- [ ] **Step 1: Тест путей и конфига чужого клона**

```python
# tests/eval/test_snapshot.py — добавить
def test_snapshot_uses_foreign_repo_config(tmp_path):
    """--repo-path чужого клона: ядро и ключ берутся из ЕГО .review.yml."""
    (tmp_path / ".review.yml").write_text(
        "task_board:\n  key_pattern: 'RON-\\d+'\n"
        "metrics:\n  brief_quality:\n    core_paths: ['app/**/*.py']\n",
        encoding="utf-8")
    config = resolve_config(tmp_path, briefs_dir=None)      # хелпер __main__
    assert config.key_pattern == r"RON-\d+"
    assert config.matches_core("app/api/routes.py") is True


def test_history_path_follows_repo_path(tmp_path):
    """Ряды чужого репозитория не смешиваются с нашими замерами приёмок."""
    paths = resolve_paths(tmp_path, briefs_dir=None)
    assert paths.history == tmp_path / "eval" / history.HISTORY_PATH_NAME
    assert paths.briefs == tmp_path / "docs" / "superpowers" / "briefs"
```

- [ ] **Step 2: Запустить, убедиться в падении**

Run: `.venv/bin/pytest tests/eval/test_snapshot.py -q -k foreign`
Expected: FAIL — `ImportError: cannot import name 'resolve_config'`

- [ ] **Step 3: Ввести резолв путей и конфига**

В `eval/solve_task_metrics/__main__.py`:

```python
DEFAULT_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class HarnessPaths:
    """Пути прогона: всё привязано к целевому клону, а не к этому репозиторию."""

    repo: pathlib.Path
    briefs: pathlib.Path
    eval_dir: pathlib.Path
    history: pathlib.Path
    report: pathlib.Path
    replay_history: pathlib.Path
    replay_report: pathlib.Path


def resolve_paths(repo_path, briefs_dir) -> HarnessPaths: ...


def resolve_config(repo_path, briefs_dir):
    """Конфиг метрики из .review.yml ЦЕЛЕВОГО клона; без файла — дефолт."""
    review = pathlib.Path(repo_path) / ".review.yml"
    data = yaml.safe_load(review.read_text(encoding="utf-8")) if review.exists() else {}
    relative = str(pathlib.Path(briefs_dir)) if briefs_dir else None
    return BriefQualityConfig.from_review_yaml(data or {}, briefs_dir=relative)
```

и общий декоратор опций для четырёх команд:

```python
def _add_repo_options(parser) -> None:
    parser.add_argument("--repo-path", default=str(DEFAULT_REPO_ROOT),
                        help="клон, по которому считать (по умолчанию — этот репозиторий)")
    parser.add_argument("--briefs-dir", default=None,
                        help="каталог брифов относительно клона")
```

- [ ] **Step 4: Прокинуть в команды**

`cmd_snapshot`, `cmd_forecast`, `cmd_replay`, `cmd_subqueries` берут `paths = resolve_paths(...)`
и `config = resolve_config(...)`, передавая их вместо модульных констант в
`snapshot_mod.build_snapshot`, `ground_truth.git_runner`, `report_merge.ensure_mergeable` и далее.

- [ ] **Step 5: Прогнать весь набор**

Run: `.venv/bin/pytest -q`
Expected: PASS

- [ ] **Step 6: Коммит**

```bash
git add eval/solve_task_metrics tests/eval
git commit -m "feat(eval): офлайн-харнесс считает чужой клон через --repo-path"
```

---

### Task 10: Документация и приёмка

**Files:**
- Modify: `CLAUDE.md` (раздел «Неочевидные факты»), `README.md`, `README.ru.md`
- Verify: прогон `snapshot`, мутационная проверка guard-теста

- [ ] **Step 1: Записать неочевидные факты в CLAUDE.md**

Абзац после факта про `brief_quality` (PRI-249), в том же стиле — что именно неочевидно:
статус `unconfigured_core_denominator` требует ДВУХ условий сразу (пустой знаменатель И
отсутствие ключа), иначе критерии 2 и 3 PRI-271 противоречат друг другу; матчер путей
собственный, потому что `fnmatch` не знает про `/` и правило «только корневые `*.py`» на нём
невыразимо; уникальность строки измерения требует `COALESCE(task_key,'')`, так как в SQL
`NULL ≠ NULL`; съём теперь трёхточечный (`publish_review`, `finish_task`, `measure-briefs`), и
идентичность строки — `(repo, pr_number, task_key)`, а не прогон ревью.

- [ ] **Step 2: Синхронно дополнить оба README**

Ключ `metrics.brief_quality.core_paths` в разделе про `.review.yml` и команда
`reviewer measure-briefs` в списке CLI — в `README.md` (EN) и `README.ru.md` (RU).

- [ ] **Step 3: Регрессия чисел дефолтного конфига**

Run: `.venv/bin/python -m eval.solve_task_metrics snapshot`
Expected: `core_recall_median 0.5714`, `bulk_core_recall_median 0.3571`.
Сверять медианы измеренных задач; рост корпуса 75 → 76 брифов ожидаем (бриф этой задачи ещё
не имеет PR-мержей и в измеренные не попадает).

- [ ] **Step 4: Мутационная проверка guard-теста (критерий 5 PRI-271)**

```bash
cp reviewer/policy/policy.py /tmp/policy_backup.py
# заменить чтение ключа на константу: brief_quality=BriefQualityConfig()
.venv/bin/pytest tests/policy/test_policy_brief_quality.py -q   # ОБЯЗАН упасть
cp /tmp/policy_backup.py reviewer/policy/policy.py
.venv/bin/pytest tests/policy/test_policy_brief_quality.py -q   # снова зелёный
```

Если тест остаётся зелёным при снятом чтении конфига — он ничего не проверяет, и его надо
переписать до того, как задача считается сделанной.

- [ ] **Step 5: Полный прогон и линт**

Run: `.venv/bin/pytest -q` (baseline 4497 passed + новые тесты)
Run: `.venv/bin/ruff check reviewer eval tests`
Expected: PASS, ruff чист

- [ ] **Step 6: Коммит**

```bash
git add CLAUDE.md README.md README.ru.md
git commit -m "docs(metrics): репо-агностичное ядро метрики и трёхточечный съём"
```

---

## Self-Review

**Покрытие спеки:** конфиг и ключ `.review.yml` → Task 1; производные функции ядра и 8 вызовов →
Task 2; статус `unconfigured_core_denominator`, `briefs_dir` и фикстура 11/13 → Task 3;
`ground_truth` в ядре и номер PR → Task 4; nullable `run_id`, дедуп, уникальный индекс и UPSERT →
Task 5; `measure_and_record` и неизменность `publish_review` → Task 6; съём в `finish_task` и
аддитивное поле ответа → Task 7; CLI → Task 8; флаги харнесса и история рядом с клоном → Task 9;
документация и приёмка (регрессия чисел, мутационный guard) → Task 10. Разделов спеки без задачи
не осталось.

**Плейсхолдеры:** не найдены — каждый шаг несёт либо код, либо точную команду с ожидаемым
результатом.

**Согласованность имён:** `BriefQualityConfig`, `matches_core`, `from_review_yaml`, `DEFAULT`,
`STATUS_UNCONFIGURED_CORE`, `measure_and_record`, `PRMerge`, `changed_status`, `measure_corpus`,
`_pr_session`, `_apply_backlink`, `brief_quality_status` — используются в одной форме во всех
задачах.
