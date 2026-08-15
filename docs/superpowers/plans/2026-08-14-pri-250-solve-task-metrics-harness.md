# Офлайн-харнесс метрик solve-task — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Превратить одноразовый спайк `eval/pri246_solve_task_cost.py` в переиспользуемый офлайн-харнесс метрик solve-task с историей срезов, взвешенной ценой, core-recall, полной ценой задачи «под ключ» и прогнозом с разбросом.

**Architecture:** Чистый пакет `eval/solve_task_metrics/` из модулей с одной ответственностью. Расчёт (`cost`, `classify`, `recall`, `history`, `forecast`) — чистые функции над данными; ввод-вывод (`briefs`, `ground_truth`, `endtoend`, `report`) изолирован. Git вызывается через инъектируемый callable, поэтому юнит-тесты идут без git-репозитория и без сети. CLI — `python -m eval.solve_task_metrics` с подкомандами `snapshot` / `compare` / `forecast`.

**Tech Stack:** Python 3 stdlib (`dataclasses`, `statistics`, `re`, `json`, `subprocess`, `argparse`), pytest. Никаких новых зависимостей.

**Spec:** `docs/superpowers/specs/2026-08-14-pri-250-solve-task-metrics-harness-design.md`

## Global Constraints

- Работа идёт в существующей ветке `feat/pri-250-solve-task-metrics-harness`. Ветку не создавать заново.
- Харнесс остаётся офлайн-инструментом в `eval/` и **не** импортируется из `reviewer/**`. Обратная зависимость (`eval/` → `reviewer/`) тоже запрещена: пакет использует только stdlib.
- Юнит-тесты не ходят в сеть, в Postgres, в Neo4j и не требуют git-репозитория: git-вызовы инъектируются как callable.
- Тесты лежат в `tests/eval/`, запускаются обычным `.venv/bin/pytest -q` (маркер `integration` не ставится).
- Язык кода проекта — русский: комментарии, докстринги, сообщения CLI на русском.
- Коммиты — Conventional Commits на русском, **без** self-attribution (никаких `Co-Authored-By`, упоминаний Claude).
- Множители взвешивания фиксированы: `fresh_in ×1.0`, `output ×5.0`, `cache_write ×1.25`, `cache_read ×0.1`.
- Бакеты токенов везде именуются одинаково: `fresh_in`, `output`, `cache_write`, `cache_read`.
- Инвариант ground truth: настоящий PR-мерж — только субъект, соответствующий `^Merge pull request #\d+ from `.
- Любая правка контента под `plugin/` требует прогона `python scripts/update_codex_plugin_manifest.py` в том же коммите, иначе install-тесты красные.
- `git push`, создание PR и любая запись в доску требуют явного подтверждения пользователя — сами по себе не выполняются.

---

### Task 1: Пакет-скелет и парсер корпуса брифов

**Files:**
- Create: `eval/solve_task_metrics/__init__.py`
- Create: `eval/solve_task_metrics/briefs.py`
- Create: `tests/eval/__init__.py`
- Test: `tests/eval/test_briefs.py`

**Interfaces:**
- Consumes: ничего (первая задача).
- Produces:
  - `TOKENS_HEADER: str`, `RELEVANT_HEADER: str`, `TEST_HEADER: str`, `SIDECHAIN_MARK: str`, `BUCKET_KEYS: tuple[str, ...]`
  - `parse_human_tokens(text: str) -> float`
  - `TokenBlock` (dataclass: `main_by_model: dict`, `sidechain_by_model: dict`; методы `main_total() -> float`, `sidechain_total() -> float`)
  - `parse_token_block(text: str) -> TokenBlock | None`
  - `extract_section_paths(text: str, header: str) -> set[str]`
  - `extract_task_key(filename: str) -> str | None`
  - `BriefRecord` (dataclass: `filename: str`, `task_key: str | None`, `token_block: TokenBlock | None`, `relevant_paths: set[str]`, `test_paths: set[str]`)
  - `load_briefs(briefs_dir: pathlib.Path) -> list[BriefRecord]`

- [ ] **Step 1: Написать падающие тесты парсера**

Создать `tests/eval/__init__.py` (пустой файл) и `tests/eval/test_briefs.py`:

```python
"""Unit-тесты парсера корпуса брифов офлайн-харнесса метрик solve-task."""
import pytest

from eval.solve_task_metrics import briefs

BRIEF_WITH_TOKENS = """# Brief — PRI-42 пример

## Relevant code
- `reviewer/mcp/service.py:944` — точка входа
- reviewer/policy/policy.py:12 — гейтинг
- `web/Dockerfile:26` — сборка фронта
- (dropped 3: не информируют реализацию)

## Test exemplars
- `tests/policy/test_policy.py:10` — образец

## Токены (этап solve-task)
Модель: claude-opus-4-8
fresh-in 9.9K · out 164K · cache-write 533K · cache-read 14.2M
Всего: 14.9M токенов

В т.ч. sidechain-сабагент:
Модель: claude-sonnet-4-5
fresh-in 500 · out 2K · cache-write 10K · cache-read 1.5M
Sidechain всего: 1.5M токенов
"""


def test_parse_human_tokens_units():
    assert briefs.parse_human_tokens("900") == 900.0
    assert briefs.parse_human_tokens("51.2K") == 51_200.0
    assert briefs.parse_human_tokens("3.3M") == 3_300_000.0


def test_parse_human_tokens_rejects_garbage():
    with pytest.raises(ValueError):
        briefs.parse_human_tokens("много")


def test_parse_token_block_main_and_sidechain():
    block = briefs.parse_token_block(BRIEF_WITH_TOKENS)
    assert block is not None
    assert block.main_by_model["claude-opus-4-8"] == {
        "fresh_in": 9_900.0,
        "output": 164_000.0,
        "cache_write": 533_000.0,
        "cache_read": 14_200_000.0,
    }
    assert block.sidechain_by_model["claude-sonnet-4-5"]["output"] == 2_000.0
    assert block.main_total() == pytest.approx(14_906_900.0)
    assert block.sidechain_total() == pytest.approx(1_512_500.0)


def test_parse_token_block_absent_returns_none():
    assert briefs.parse_token_block("# Brief — PRI-1\n\n## Task\nбез токенов\n") is None


def test_extract_section_paths_backticks_bare_and_dropped():
    paths = briefs.extract_section_paths(BRIEF_WITH_TOKENS, briefs.RELEVANT_HEADER)
    assert paths == {
        "reviewer/mcp/service.py",
        "reviewer/policy/policy.py",
        "web/Dockerfile",
    }


def test_extract_section_paths_test_section_is_separate():
    assert briefs.extract_section_paths(BRIEF_WITH_TOKENS, briefs.TEST_HEADER) == {
        "tests/policy/test_policy.py",
    }


def test_extract_section_paths_missing_header_is_empty():
    assert briefs.extract_section_paths("# Brief\n", briefs.RELEVANT_HEADER) == set()


def test_extract_task_key_from_filename():
    assert briefs.extract_task_key("2026-08-14-PRI-250-harness.md") == "PRI-250"
    assert briefs.extract_task_key("2026-08-14-pri-42-x.md") == "PRI-42"
    assert briefs.extract_task_key("2026-08-14-no-key.md") is None


def test_load_briefs_reads_corpus(tmp_path):
    (tmp_path / "2026-01-01-PRI-7-a.md").write_text(BRIEF_WITH_TOKENS, encoding="utf-8")
    (tmp_path / "2026-01-02-plain.md").write_text("# Brief\n", encoding="utf-8")

    records = briefs.load_briefs(tmp_path)

    assert [r.task_key for r in records] == ["PRI-7", None]
    assert records[0].token_block is not None
    assert records[1].token_block is None
    assert records[1].relevant_paths == set()
```

- [ ] **Step 2: Прогнать тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/eval/test_briefs.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'eval.solve_task_metrics'`

- [ ] **Step 3: Создать пакет и реализовать парсер**

Создать `eval/solve_task_metrics/__init__.py`:

```python
"""Офлайн-харнесс метрик этапа solve-task (PRI-250).

НЕ продакшн-путь reviewer: пакет живёт в eval/, использует только stdlib и
никогда не импортируется из reviewer/**. Расчётные модули (cost, classify,
recall, history, forecast) — чистые функции без ввода-вывода.

Когда за метрики возьмётся онлайн-версия (PRI-249), эти модули следует
ПЕРЕНЕСТИ в продакшн-слой и импортировать в обе стороны, а не переписать
второй раз: общий расчёт метрик не должен существовать в двух копиях.
"""
```

Создать `eval/solve_task_metrics/briefs.py`:

```python
"""Чтение корпуса брифов: блок токенов, пути секций, ключ задачи."""
from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass, field

TOKENS_HEADER = "## Токены (этап solve-task)"
RELEVANT_HEADER = "## Relevant code"
TEST_HEADER = "## Test exemplars"
SIDECHAIN_MARK = "В т.ч. sidechain-сабагент:"

BUCKET_KEYS = ("fresh_in", "output", "cache_write", "cache_read")

_NUM_RE = re.compile(r"^(\d+(?:\.\d+)?)([KM]?)$")
_MODEL_RE = re.compile(r"^Модель:\s*(.+)$")
_BUCKETS_RE = re.compile(
    r"fresh-in\s+(\S+)\s*·\s*out\s+(\S+)\s*·\s*cache-write\s+(\S+)\s*·\s*cache-read\s+(\S+)"
)
_KEY_RE = re.compile(r"(PRI-\d+)", re.IGNORECASE)

_BACKTICK_RE = re.compile(r"`([^`]+)`")
_PATH_LINE_RE = re.compile(r"^([\w./\-]+\.\w+):([\d,\-\s]+)$")
_BARE_PATH_RE = re.compile(r"^[\w./\-]+\.\w+$")
_LINE_SUFFIX_RE = re.compile(r"^(.+?):(\d[\d,\-\s]*)$")
_KNOWN_EXT_NO_DOT_NAMES = {"Dockerfile", "Makefile"}


def parse_human_tokens(text: str) -> float:
    """Обратное к human_tokens() хука brief_cost: '51.2K' -> 51200.0.

    Разбор лоссовый: исходное число уже округлено до одного знака после точки
    в K/M, поэтому восстановленное значение несёт погрешность округления.
    """
    match = _NUM_RE.match(text.strip())
    if not match:
        raise ValueError(f"не похоже на human_tokens число: {text!r}")
    value = float(match.group(1))
    unit = match.group(2)
    if unit == "K":
        return value * 1_000
    if unit == "M":
        return value * 1_000_000
    return value


@dataclass
class TokenBlock:
    """Разобранный блок токенов одного брифа."""

    main_by_model: dict = field(default_factory=dict)
    sidechain_by_model: dict = field(default_factory=dict)

    def main_total(self) -> float:
        return sum(sum(b.values()) for b in self.main_by_model.values())

    def sidechain_total(self) -> float:
        return sum(sum(b.values()) for b in self.sidechain_by_model.values())


def _section_body(text: str, header: str) -> list[str]:
    """Строки секции от её заголовка до следующего '## ' либо конца текста."""
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == header)
    except StopIteration:
        return []
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    return lines[start + 1 : end]


def parse_token_block(text: str) -> TokenBlock | None:
    """Найти и разобрать блок «## Токены (этап solve-task)»; None, если его нет."""
    body = _section_body(text, TOKENS_HEADER)
    if not body:
        return None
    block = TokenBlock()
    target = block.main_by_model
    current_model = None
    for raw in body:
        line = raw.strip()
        if not line:
            continue
        if line == SIDECHAIN_MARK:
            target = block.sidechain_by_model
            current_model = None
            continue
        model_match = _MODEL_RE.match(line)
        if model_match:
            current_model = model_match.group(1).strip()
            continue
        buckets_match = _BUCKETS_RE.search(line)
        if buckets_match and current_model is not None:
            values = [parse_human_tokens(g) for g in buckets_match.groups()]
            target[current_model] = dict(zip(BUCKET_KEYS, values))
        # Строки «Всего: …» не парсим: это производная от бакетов, считаем сами.
    if not block.main_by_model and not block.sidechain_by_model:
        return None
    return block


def _paths_from_backtick(fragment: str) -> list[str]:
    """Путь из backtick-фрагмента вида 'path.py:19,48'; [] для не-пути."""
    fragment = fragment.strip()
    match = _PATH_LINE_RE.match(fragment)
    if match:
        return [match.group(1)]
    if _BARE_PATH_RE.match(fragment):
        return [fragment]
    return []


def _leading_path(token: str) -> str | None:
    """Путь из первого токена bullet'а: не все пути в брифах обёрнуты в backticks."""
    token = token.strip("`,;()")
    match = _LINE_SUFFIX_RE.match(token)
    if match:
        token = match.group(1)
    if "/" in token or re.search(r"\.\w+$", token):
        return token
    if token in _KNOWN_EXT_NO_DOT_NAMES:
        return token
    return None


def extract_section_paths(text: str, header: str) -> set[str]:
    """Множество путей из bullet-секции брифа; строки '(dropped N: …)' пропускаются."""
    paths: set[str] = set()
    for raw in _section_body(text, header):
        line = raw.strip()
        if not line.startswith("-"):
            continue
        if re.match(r"^-\s*\(dropped\b", line):
            continue
        for fragment in _BACKTICK_RE.findall(line):
            paths.update(_paths_from_backtick(fragment))
        body = line[1:].strip()
        first_token = body.split()[0] if body.split() else ""
        leading = _leading_path(first_token)
        if leading:
            paths.add(leading)
    return paths


def extract_task_key(filename: str) -> str | None:
    """Ключ задачи из имени файла брифа ('…-PRI-250-…' -> 'PRI-250')."""
    match = _KEY_RE.search(filename)
    return match.group(1).upper() if match else None


@dataclass
class BriefRecord:
    """Один бриф корпуса: ключ, токены и предсказанные пути."""

    filename: str
    task_key: str | None
    token_block: TokenBlock | None
    relevant_paths: set
    test_paths: set


def load_briefs(briefs_dir: pathlib.Path) -> list[BriefRecord]:
    """Прочитать весь корпус брифов каталога (по возрастанию имени файла)."""
    records: list[BriefRecord] = []
    for path in sorted(briefs_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        records.append(
            BriefRecord(
                filename=path.name,
                task_key=extract_task_key(path.name),
                token_block=parse_token_block(text),
                relevant_paths=extract_section_paths(text, RELEVANT_HEADER),
                test_paths=extract_section_paths(text, TEST_HEADER),
            )
        )
    return records
```

- [ ] **Step 4: Прогнать тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/eval/test_briefs.py -q`
Expected: PASS (11 тестов)

- [ ] **Step 5: Прогнать линт**

Run: `.venv/bin/ruff check eval/solve_task_metrics tests/eval`
Expected: `All checks passed!`

- [ ] **Step 6: Коммит**

```bash
git add eval/solve_task_metrics/__init__.py eval/solve_task_metrics/briefs.py tests/eval/__init__.py tests/eval/test_briefs.py
git commit -m "feat(eval): парсер корпуса брифов для харнесса метрик solve-task"
```

---

### Task 2: Ground truth по настоящим PR-мержам

**Files:**
- Create: `eval/solve_task_metrics/ground_truth.py`
- Test: `tests/eval/test_ground_truth.py`

**Interfaces:**
- Consumes: ничего из Task 1.
- Produces:
  - `PR_MERGE_SUBJECT_RE: re.Pattern`
  - `filter_pr_merges(rows: list[tuple[str, str]]) -> tuple[list[str], int]` — чистая; возвращает `(shas настоящих PR-мержей, число отброшенных sync-мержей)`
  - `GitRunner = Callable[[list[str]], str]`
  - `git_runner(repo_root: pathlib.Path) -> GitRunner`
  - `merge_rows(task_key: str, run_git: GitRunner) -> list[tuple[str, str]]`
  - `changed_files(sha: str, run_git: GitRunner) -> set[str]`
  - `path_existed(parent_ref: str | None, path: str, run_git: GitRunner) -> bool`
  - `TaskGroundTruth` (dataclass: `task_key: str`, `merge_shas: list[str]`, `sync_merges_skipped: int`, `changed: set[str]`, `parent_ref: str | None`)
  - `collect(task_key: str, run_git: GitRunner) -> TaskGroundTruth`

- [ ] **Step 1: Написать падающие тесты (инвариант — критерий 6 спеки)**

Создать `tests/eval/test_ground_truth.py`:

```python
"""Unit-тесты ground truth: только настоящие PR-мержи считаются работой задачи."""
from eval.solve_task_metrics import ground_truth as gt

# Реальная форма граблей PRI-134: sync-мерж с тем же ключом в тексте тащит
# чужие файлы и раздувает знаменатель.
ROWS = [
    ("aaa111", "Merge pull request #148 from mimfort/feature/pri-134-x"),
    ("bbb222", "Merge remote-tracking branch 'origin/dev' into feature/pri-134-x"),
    ("ccc333", "merge: dev в feature/pri-134-x"),
    ("ddd444", "Merge pull request #149 from mimfort/fix/pri-134-followup"),
]


def test_filter_pr_merges_keeps_only_real_pr_merges():
    shas, skipped = gt.filter_pr_merges(ROWS)

    assert shas == ["aaa111", "ddd444"]
    assert skipped == 2


def test_filter_pr_merges_counts_sync_merges_instead_of_silently_dropping():
    _, skipped = gt.filter_pr_merges(ROWS[1:3])

    assert skipped == 2


def test_filter_pr_merges_empty_input():
    assert gt.filter_pr_merges([]) == ([], 0)


def test_merge_rows_parses_git_output():
    def fake_git(args):
        assert args[0] == "log"
        assert "--merges" in args
        return (
            "aaa111 Merge pull request #148 from mimfort/feature/pri-134-x\n"
            "bbb222 merge: dev в feature/pri-134-x\n"
            "\n"
        )

    assert gt.merge_rows("PRI-134", fake_git) == [
        ("aaa111", "Merge pull request #148 from mimfort/feature/pri-134-x"),
        ("bbb222", "merge: dev в feature/pri-134-x"),
    ]


def test_changed_files_splits_names():
    def fake_git(args):
        assert args[:2] == ["diff", "--name-only"]
        return "reviewer/a.py\ntests/test_a.py\n\n"

    assert gt.changed_files("aaa111", fake_git) == {"reviewer/a.py", "tests/test_a.py"}


def test_collect_uses_only_pr_merges_for_changed_files():
    calls = []

    def fake_git(args):
        calls.append(args)
        if args[0] == "log":
            return (
                "aaa111 Merge pull request #148 from mimfort/feature/pri-134-x\n"
                "bbb222 Merge remote-tracking branch 'origin/dev' into feature/pri-134-x\n"
            )
        if args[0] == "diff":
            assert args[2] == "aaa111^1"
            return "reviewer/a.py\n"
        raise AssertionError(f"неожиданный вызов git: {args}")

    result = gt.collect("PRI-134", fake_git)

    assert result.merge_shas == ["aaa111"]
    assert result.sync_merges_skipped == 1
    assert result.changed == {"reviewer/a.py"}
    assert result.parent_ref == "aaa111^1"
    assert not any(args[0] == "diff" and "bbb222" in args for args in calls)


def test_collect_without_merges_is_empty():
    def fake_git(args):
        return ""

    result = gt.collect("PRI-999", fake_git)

    assert result.merge_shas == []
    assert result.changed == set()
    assert result.parent_ref is None


def test_path_existed_true_when_git_exits_zero():
    def fake_git(args):
        assert args[:2] == ["cat-file", "-e"]
        return ""

    assert gt.path_existed("aaa111^1", "reviewer/a.py", fake_git) is True


def test_path_existed_false_when_git_raises():
    def fake_git(args):
        raise gt.GitError("нет такого объекта")

    assert gt.path_existed("aaa111^1", "reviewer/new.py", fake_git) is False


def test_path_existed_without_parent_assumes_existing():
    def fake_git(args):
        raise AssertionError("git не должен вызываться без parent_ref")

    assert gt.path_existed(None, "reviewer/a.py", fake_git) is True
```

- [ ] **Step 2: Прогнать тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/eval/test_ground_truth.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'eval.solve_task_metrics.ground_truth'`

- [ ] **Step 3: Реализовать модуль**

Создать `eval/solve_task_metrics/ground_truth.py`:

```python
"""Ground truth задачи: файлы, изменённые её НАСТОЯЩИМИ PR-мержами.

Git вызывается через инъектируемый callable (GitRunner), поэтому фильтрация и
разбор тестируются на чистых данных, без git-репозитория.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable

GitRunner = Callable[[list], str]


class GitError(RuntimeError):
    """Git-вызов завершился ненулевым кодом."""


# Настоящий PR-мерж: только «Merge pull request #N from <owner>/<ветка>».
# Синхронизационные мержи («Merge remote-tracking branch 'origin/dev' into
# feature/pri-N», «merge: dev в …») содержат тот же ключ задачи, но их diff
# первого родителя тащит ВСЕ файлы, попавшие в целевую ветку от чужих задач:
# у PRI-134 знаменатель раздувался с 17 реальных файлов PR #148 до 195, занижая
# core-recall с 50% до 8%. Считать их работой задачи нельзя.
PR_MERGE_SUBJECT_RE = re.compile(r"^Merge pull request #\d+ from ", re.IGNORECASE)


def filter_pr_merges(rows: list) -> tuple:
    """Оставить только настоящие PR-мержи.

    Args:
        rows: пары (sha, субъект коммита).

    Returns:
        (shas настоящих PR-мержей, число отброшенных синхронизационных мержей).
    """
    shas: list = []
    skipped = 0
    for sha, subject in rows:
        if PR_MERGE_SUBJECT_RE.match(subject.strip()):
            shas.append(sha)
        else:
            skipped += 1
    return shas, skipped


def git_runner(repo_root: pathlib.Path) -> GitRunner:
    """GitRunner поверх subprocess для реального прогона."""

    def run(args: list) -> str:
        result = subprocess.run(
            ["git", *args], cwd=repo_root, capture_output=True, text=True
        )
        if result.returncode != 0:
            raise GitError(result.stderr.strip() or f"git {' '.join(args)}")
        return result.stdout

    return run


def merge_rows(task_key: str, run_git: GitRunner) -> list:
    """Пары (sha, субъект) merge-коммитов, упоминающих ключ задачи."""
    out = run_git(
        ["log", "--merges", "--all", "--format=%H %s", "-i", f"--grep={task_key}"]
    )
    rows: list = []
    for line in out.splitlines():
        if not line.strip():
            continue
        sha, _, subject = line.partition(" ")
        rows.append((sha, subject))
    return rows


def changed_files(sha: str, run_git: GitRunner) -> set:
    """Файлы merge-коммита по diff первого родителя (реальный контент PR)."""
    try:
        out = run_git(["diff", "--name-only", f"{sha}^1", sha])
    except GitError:
        return set()
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def path_existed(parent_ref, path: str, run_git: GitRunner) -> bool:
    """Существовал ли путь в родителе merge-коммита (то есть не новый файл).

    Без parent_ref проверить нельзя — считаем «существовал», чтобы не завышать
    число исключённых из знаменателя файлов.
    """
    if not parent_ref:
        return True
    try:
        run_git(["cat-file", "-e", f"{parent_ref}:{path}"])
    except GitError:
        return False
    return True


@dataclass
class TaskGroundTruth:
    """Ground truth одной задачи."""

    task_key: str
    merge_shas: list = field(default_factory=list)
    sync_merges_skipped: int = 0
    changed: set = field(default_factory=set)
    parent_ref: str | None = None


def collect(task_key: str, run_git: GitRunner) -> TaskGroundTruth:
    """Собрать ground truth задачи: PR-мержи и объединение их изменённых файлов."""
    shas, skipped = filter_pr_merges(merge_rows(task_key, run_git))
    changed: set = set()
    for sha in shas:
        changed |= changed_files(sha, run_git)
    result = TaskGroundTruth(
        task_key=task_key,
        merge_shas=shas,
        sync_merges_skipped=skipped,
        changed=changed,
    )
    result.parent_ref = f"{shas[0]}^1" if shas else None
    return result
```

- [ ] **Step 4: Прогнать тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/eval/test_ground_truth.py -q`
Expected: PASS (10 тестов)

- [ ] **Step 5: Прогнать линт**

Run: `.venv/bin/ruff check eval/solve_task_metrics tests/eval`
Expected: `All checks passed!`

- [ ] **Step 6: Коммит**

```bash
git add eval/solve_task_metrics/ground_truth.py tests/eval/test_ground_truth.py
git commit -m "feat(eval): ground truth только из настоящих PR-мержей, инвариант под тестом"
```

---

### Task 3: Метрики — взвешенная цена, классификация путей, core-recall

**Files:**
- Create: `eval/solve_task_metrics/cost.py`
- Create: `eval/solve_task_metrics/classify.py`
- Create: `eval/solve_task_metrics/recall.py`
- Test: `tests/eval/test_cost.py`
- Test: `tests/eval/test_classify.py`
- Test: `tests/eval/test_recall.py`

**Interfaces:**
- Consumes: `briefs.BUCKET_KEYS` (Task 1).
- Produces:
  - `cost.WEIGHTS: dict[str, float]`, `cost.raw(buckets: dict) -> float`, `cost.weighted(buckets: dict) -> float`, `cost.inflation(raw_value: float, weighted_value: float) -> float | None`, `cost.sum_buckets(blocks: list[dict]) -> dict`
  - `classify.is_core_production_path(path: str) -> bool`, `classify.categorize_miss(path: str, existed_before: bool) -> str`, `classify.NEW_FILE_CATEGORY: str`
  - `recall.TaskQuality` (dataclass: `task_key: str`, `expected: int`, `expected_core: int`, `predicted: int`, `hit_core: int`, `core_recall: float | None`, `raw_recall: float | None`, `precision: float | None`)
  - `recall.evaluate_task(task_key: str, predicted: set, expected: set, expected_core: set) -> TaskQuality`
  - `recall.QualityAggregate` (dataclass: `n_measured: int`, `no_measurement: int`, `core_recall_median: float | None`, `core_recall_mean: float | None`, `raw_recall_median: float | None`, `denominator_median: float | None`)
  - `recall.aggregate(rows: list) -> QualityAggregate`

- [ ] **Step 1: Написать падающие тесты цены**

Создать `tests/eval/test_cost.py`:

```python
"""Unit-тесты взвешенной цены: сумма токенов не пропорциональна стоимости."""
import pytest

from eval.solve_task_metrics import cost

BUCKETS = {
    "fresh_in": 10_000.0,
    "output": 100_000.0,
    "cache_write": 200_000.0,
    "cache_read": 2_000_000.0,
}


def test_weights_are_the_documented_multipliers():
    assert cost.WEIGHTS == {
        "fresh_in": 1.0,
        "output": 5.0,
        "cache_write": 1.25,
        "cache_read": 0.1,
    }


def test_raw_is_plain_sum():
    assert cost.raw(BUCKETS) == pytest.approx(2_310_000.0)


def test_weighted_applies_multipliers():
    # 10_000*1 + 100_000*5 + 200_000*1.25 + 2_000_000*0.1 = 960_000
    assert cost.weighted(BUCKETS) == pytest.approx(960_000.0)


def test_weighted_is_lower_than_raw_when_cache_read_dominates():
    assert cost.weighted(BUCKETS) < cost.raw(BUCKETS)


def test_inflation_is_ratio_of_raw_to_weighted():
    assert cost.inflation(cost.raw(BUCKETS), cost.weighted(BUCKETS)) == pytest.approx(
        2.40625
    )


def test_inflation_none_when_weighted_is_zero():
    assert cost.inflation(0.0, 0.0) is None


def test_sum_buckets_merges_several_models():
    merged = cost.sum_buckets([BUCKETS, BUCKETS])

    assert merged["output"] == pytest.approx(200_000.0)
    assert merged["cache_read"] == pytest.approx(4_000_000.0)


def test_sum_buckets_of_nothing_is_zeroed():
    assert cost.sum_buckets([]) == {
        "fresh_in": 0.0,
        "output": 0.0,
        "cache_write": 0.0,
        "cache_read": 0.0,
    }
```

- [ ] **Step 2: Написать падающие тесты классификации**

Создать `tests/eval/test_classify.py`:

```python
"""Unit-тесты классификатора путей и таксономии промахов."""
import pytest

from eval.solve_task_metrics import classify


@pytest.mark.parametrize(
    "path",
    ["reviewer/mcp/service.py", "plugin/hooks/brief_cost.py", "sync_chunk.py"],
)
def test_core_production_paths(path):
    assert classify.is_core_production_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "tests/mcp/test_service.py",
        "docs/superpowers/plans/x.md",
        "plugin/skills/review-pr/SKILL.md",
        "README.md",
        ".review.yml",
        "eval/run_eval.py",
        "docker-compose.yml",
    ],
)
def test_non_core_paths(path):
    assert classify.is_core_production_path(path) is False


def test_categorize_miss_new_file_wins_over_directory():
    assert classify.categorize_miss("reviewer/new.py", existed_before=False) == (
        classify.NEW_FILE_CATEGORY
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/mcp/test_x.py", "tests/"),
        ("docs/x.md", "docs/"),
        (".review.yml", ".review.yml/конфиги"),
        ("plugin/skills/ask/SKILL.md", "plugin/skills/*.md"),
        ("plugin/hooks/x.py", "plugin/ (прочее)"),
        ("reviewer/index/store.py", "reviewer/index"),
        ("eval/run_eval.py", "eval/"),
        ("Makefile", "прочее"),
    ],
)
def test_categorize_miss_existing_paths(path, expected):
    assert classify.categorize_miss(path, existed_before=True) == expected
```

- [ ] **Step 3: Написать падающие тесты core-recall**

Создать `tests/eval/test_recall.py`:

```python
"""Unit-тесты core-recall и состояния «нет точки измерения»."""
import pytest

from eval.solve_task_metrics import recall


def test_evaluate_task_core_recall():
    row = recall.evaluate_task(
        "PRI-1",
        predicted={"reviewer/a.py", "reviewer/b.py", "docs/x.md"},
        expected={"reviewer/a.py", "reviewer/b.py", "reviewer/c.py", "tests/t.py"},
        expected_core={"reviewer/a.py", "reviewer/b.py", "reviewer/c.py"},
    )

    assert row.hit_core == 2
    assert row.core_recall == pytest.approx(2 / 3)
    assert row.raw_recall == pytest.approx(2 / 4)
    assert row.precision == pytest.approx(2 / 3)


def test_evaluate_task_empty_core_denominator_is_no_measurement():
    row = recall.evaluate_task(
        "PRI-2",
        predicted={"reviewer/a.py"},
        expected={"docs/x.md", "tests/t.py"},
        expected_core=set(),
    )

    assert row.core_recall is None
    assert row.expected_core == 0


def test_evaluate_task_without_predictions_has_no_precision():
    row = recall.evaluate_task(
        "PRI-3", predicted=set(), expected={"reviewer/a.py"},
        expected_core={"reviewer/a.py"},
    )

    assert row.precision is None
    assert row.core_recall == 0.0


def test_aggregate_excludes_no_measurement_from_medians():
    rows = [
        recall.evaluate_task(
            "PRI-1", {"reviewer/a.py"}, {"reviewer/a.py"}, {"reviewer/a.py"}
        ),
        recall.evaluate_task(
            "PRI-2", {"reviewer/a.py"}, {"reviewer/a.py", "reviewer/b.py"},
            {"reviewer/a.py", "reviewer/b.py"},
        ),
        recall.evaluate_task("PRI-3", {"reviewer/a.py"}, {"docs/x.md"}, set()),
    ]

    agg = recall.aggregate(rows)

    assert agg.n_measured == 2
    assert agg.no_measurement == 1
    # медиана из [1.0, 0.5] = 0.75; пустой знаменатель нулём НЕ считается
    assert agg.core_recall_median == pytest.approx(0.75)
    assert agg.denominator_median == pytest.approx(1.5)


def test_aggregate_of_only_no_measurement_rows():
    rows = [recall.evaluate_task("PRI-9", set(), {"docs/x.md"}, set())]

    agg = recall.aggregate(rows)

    assert agg.n_measured == 0
    assert agg.no_measurement == 1
    assert agg.core_recall_median is None
```

- [ ] **Step 4: Прогнать тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/eval/test_cost.py tests/eval/test_classify.py tests/eval/test_recall.py -q`
Expected: FAIL — `ModuleNotFoundError` на `cost`, `classify`, `recall`

- [ ] **Step 5: Реализовать `cost.py`**

```python
"""Взвешенная цена этапа: бакеты токенов неравноценны по стоимости."""
from __future__ import annotations

from .briefs import BUCKET_KEYS

# Множители относительно input-токена. Источник — тарифная структура,
# зафиксированная спайком PRI-246: сложение токенов завышает стоимость
# примерно в 4.1×, потому что 88% объёма приходится на дешёвый cache-read.
# Правка тарифа — правка этой константы, а не поиск по коду.
WEIGHTS = {
    "fresh_in": 1.0,
    "output": 5.0,
    "cache_write": 1.25,
    "cache_read": 0.1,
}


def raw(buckets: dict) -> float:
    """Сырая сумма токенов. НЕ пропорциональна стоимости — только справочно."""
    return float(sum(buckets.get(key, 0.0) for key in BUCKET_KEYS))


def weighted(buckets: dict) -> float:
    """Взвешенный input-эквивалент — основная метрика цены."""
    return float(sum(buckets.get(key, 0.0) * WEIGHTS[key] for key in BUCKET_KEYS))


def inflation(raw_value: float, weighted_value: float):
    """Во сколько раз сырая сумма завышает стоимость; None при нулевой цене."""
    if not weighted_value:
        return None
    return raw_value / weighted_value


def sum_buckets(blocks: list) -> dict:
    """Поэлементная сумма нескольких наборов бакетов (например, по моделям)."""
    total = {key: 0.0 for key in BUCKET_KEYS}
    for block in blocks:
        for key in BUCKET_KEYS:
            total[key] += float(block.get(key, 0.0))
    return total
```

- [ ] **Step 6: Реализовать `classify.py`**

```python
"""Классификация путей: что входит в ядро и как называется промах."""
from __future__ import annotations

NEW_FILE_CATEGORY = "новый файл (не существовал до PR)"


def is_core_production_path(path: str) -> bool:
    """Уже существовавший продакшн-код, по которому ретрив может и должен попадать.

    Ядро: reviewer/**/*.py, plugin/** кроме *.md, корневые *.py. Всё остальное
    (тесты, доки, конфиги, манифесты, eval/) — вне ядра: бриф структурно не
    обязан их предсказывать, и включение их в знаменатель делает recall
    метрикой размера diff'а, а не качества ретрива.
    """
    if path.startswith("eval/"):
        return False
    if path.startswith("reviewer/") and path.endswith(".py"):
        return True
    if path.startswith("plugin/") and not path.endswith(".md"):
        return True
    if "/" not in path and path.endswith(".py"):
        return True
    return False


def categorize_miss(path: str, existed_before: bool) -> str:
    """Категория непредсказанного файла. Новый файл — отдельная категория:
    бриф не мог сослаться на файл, которого ещё не существовало."""
    if not existed_before:
        return NEW_FILE_CATEGORY
    if path.startswith("tests/"):
        return "tests/"
    if path.startswith("docs/"):
        return "docs/"
    if path == ".review.yml" or path.endswith(".review.yml"):
        return ".review.yml/конфиги"
    if path.startswith("plugin/skills/") and path.endswith(".md"):
        return "plugin/skills/*.md"
    if path.startswith("plugin/"):
        return "plugin/ (прочее)"
    if path.startswith("reviewer/"):
        parts = path.split("/")
        module = parts[1] if len(parts) > 1 else ""
        return f"reviewer/{module}" if module else "reviewer/"
    if path.startswith("eval/"):
        return "eval/"
    return "прочее"
```

- [ ] **Step 7: Реализовать `recall.py`**

```python
"""core-recall: качество ретрива на суженном знаменателе.

Пустой знаменатель ядра — отдельное состояние «нет точки измерения» (None),
а НЕ нулевой recall: у задачи, чей diff состоит только из доков и тестов,
качество ретрива по ядру не определено, и подмешивать её нулём в медиану
значит систематически занижать метрику.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass


@dataclass
class TaskQuality:
    """Качество ретрива по одной задаче."""

    task_key: str
    expected: int
    expected_core: int
    predicted: int
    hit_core: int
    core_recall: float | None = None
    raw_recall: float | None = None
    precision: float | None = None


@dataclass
class QualityAggregate:
    """Агрегат качества по корпусу."""

    n_measured: int
    no_measurement: int
    core_recall_median: float | None = None
    core_recall_mean: float | None = None
    raw_recall_median: float | None = None
    denominator_median: float | None = None


def evaluate_task(task_key: str, predicted: set, expected: set, expected_core: set) -> TaskQuality:
    """Посчитать метрики одной задачи; core_recall=None при пустом ядре."""
    hit_core = predicted & expected_core
    hit_raw = predicted & expected
    row = TaskQuality(
        task_key=task_key,
        expected=len(expected),
        expected_core=len(expected_core),
        predicted=len(predicted),
        hit_core=len(hit_core),
    )
    row.core_recall = len(hit_core) / len(expected_core) if expected_core else None
    row.raw_recall = len(hit_raw) / len(expected) if expected else None
    row.precision = len(hit_raw) / len(predicted) if predicted else None
    return row


def aggregate(rows: list) -> QualityAggregate:
    """Свести задачи в агрегат; задачи без точки измерения считаются отдельно."""
    measured = [r for r in rows if r.core_recall is not None]
    agg = QualityAggregate(
        n_measured=len(measured),
        no_measurement=len(rows) - len(measured),
    )
    if measured:
        values = [r.core_recall for r in measured]
        agg.core_recall_median = statistics.median(values)
        agg.core_recall_mean = sum(values) / len(values)
        agg.denominator_median = statistics.median(
            [r.expected_core for r in measured]
        )
    raw_values = [r.raw_recall for r in rows if r.raw_recall is not None]
    if raw_values:
        agg.raw_recall_median = statistics.median(raw_values)
    return agg
```

- [ ] **Step 8: Прогнать тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/eval -q`
Expected: PASS (все тесты Task 1–3)

- [ ] **Step 9: Прогнать линт**

Run: `.venv/bin/ruff check eval/solve_task_metrics tests/eval`
Expected: `All checks passed!`

- [ ] **Step 10: Коммит**

```bash
git add eval/solve_task_metrics/cost.py eval/solve_task_metrics/classify.py eval/solve_task_metrics/recall.py tests/eval/test_cost.py tests/eval/test_classify.py tests/eval/test_recall.py
git commit -m "feat(eval): взвешенная цена, классификация путей и core-recall"
```

---

### Task 4: История срезов, сравнение и команда snapshot

**Files:**
- Create: `eval/solve_task_metrics/history.py`
- Create: `eval/solve_task_metrics/snapshot.py`
- Create: `eval/solve_task_metrics/report.py`
- Create: `eval/solve_task_metrics/__main__.py`
- Test: `tests/eval/test_history.py`
- Test: `tests/eval/test_snapshot.py`

**Interfaces:**
- Consumes: `briefs.load_briefs`, `briefs.BriefRecord` (Task 1); `ground_truth.collect`, `ground_truth.path_existed`, `ground_truth.GitRunner` (Task 2); `cost.*`, `classify.*`, `recall.*` (Task 3).
- Produces:
  - `history.SCHEMA: int`, `history.HISTORY_PATH_NAME: str`
  - `history.append_snapshot(path: pathlib.Path, snapshot: dict) -> None`
  - `history.load_snapshots(path: pathlib.Path) -> list[dict]`
  - `history.POLARITY: dict[str, str]` (значения `"lower_better"` / `"higher_better"` / `"neutral"`)
  - `history.Delta` (dataclass: `metric: str`, `old`, `new`, `delta`, `direction: str`)
  - `history.diff_snapshots(old: dict, new: dict) -> list[Delta]`
  - `snapshot.build_snapshot(briefs_dir, run_git, commit, taken_at, transcripts=None) -> tuple[dict, list]` — возвращает `(снимок, per-task строки)`
  - `report.render(snapshot: dict, rows: list) -> str`
  - `__main__.main(argv=None) -> int`

- [ ] **Step 1: Написать падающие тесты истории**

Создать `tests/eval/test_history.py`:

```python
"""Unit-тесты хранилища срезов и режима сравнения."""
import json

from eval.solve_task_metrics import history

OLD = {
    "schema": history.SCHEMA,
    "taken_at": "2026-08-01T00:00:00+00:00",
    "commit": "aaa",
    "window_mode": "legacy",
    "corpus": {"briefs": 50, "with_tokens": 28},
    "cost": {"weighted_median": 654_000.0, "raw_median": 2_810_000.0},
    "quality": {"core_recall_median": 0.61, "no_measurement": 10},
}
NEW = {
    "schema": history.SCHEMA,
    "taken_at": "2026-08-14T00:00:00+00:00",
    "commit": "bbb",
    "window_mode": "sealed",
    "corpus": {"briefs": 57, "with_tokens": 34},
    "cost": {"weighted_median": 600_000.0, "raw_median": 2_700_000.0},
    "quality": {"core_recall_median": 0.70, "no_measurement": 9},
    "endtoend": {"measured": 4, "weighted_median": 1_200_000.0},
}


def test_append_and_load_roundtrip(tmp_path):
    path = tmp_path / "history.jsonl"

    history.append_snapshot(path, OLD)
    history.append_snapshot(path, NEW)

    loaded = history.load_snapshots(path)
    assert [s["commit"] for s in loaded] == ["aaa", "bbb"]
    assert path.read_text(encoding="utf-8").count("\n") == 2


def test_load_snapshots_missing_file_is_empty(tmp_path):
    assert history.load_snapshots(tmp_path / "нет.jsonl") == []


def test_load_snapshots_skips_broken_line(tmp_path):
    path = tmp_path / "history.jsonl"
    path.write_text(json.dumps(OLD) + "\nне json\n", encoding="utf-8")

    assert len(history.load_snapshots(path)) == 1


def test_diff_marks_cost_drop_as_improvement():
    deltas = {d.metric: d for d in history.diff_snapshots(OLD, NEW)}

    assert deltas["cost.weighted_median"].delta == -54_000.0
    assert deltas["cost.weighted_median"].direction == "улучшение"


def test_diff_marks_recall_growth_as_improvement():
    deltas = {d.metric: d for d in history.diff_snapshots(OLD, NEW)}

    assert deltas["quality.core_recall_median"].direction == "улучшение"


def test_diff_marks_new_metric_as_new_not_growth_from_zero():
    deltas = {d.metric: d for d in history.diff_snapshots(OLD, NEW)}

    assert deltas["endtoend.weighted_median"].old is None
    assert deltas["endtoend.weighted_median"].direction == "новая"
    assert deltas["endtoend.weighted_median"].delta is None


def test_diff_marks_equal_values_as_unchanged():
    deltas = {d.metric: d for d in history.diff_snapshots(NEW, NEW)}

    assert deltas["cost.weighted_median"].direction == "без изменений"


def test_diff_flags_window_mode_change():
    deltas = {d.metric: d for d in history.diff_snapshots(OLD, NEW)}

    assert deltas["window_mode"].old == "legacy"
    assert deltas["window_mode"].new == "sealed"
    assert deltas["window_mode"].direction == "несопоставимо"
```

- [ ] **Step 2: Написать падающие тесты снимка**

Создать `tests/eval/test_snapshot.py`:

```python
"""Unit-тесты сборки среза по корпусу брифов (git инъектирован, сети нет)."""
from eval.solve_task_metrics import history, snapshot

BRIEF = """# Brief — PRI-7 пример

## Relevant code
- `reviewer/a.py:1` — трогаем
- `reviewer/z.py:2` — не трогаем

## Токены (этап solve-task)
Модель: m
fresh-in 10K · out 100K · cache-write 200K · cache-read 2M
Всего: 2.3M токенов
"""


def _fake_git(args):
    if args[0] == "log":
        return "aaa Merge pull request #1 from mimfort/feature/pri-7\n"
    if args[0] == "diff":
        return "reviewer/a.py\nreviewer/b.py\ntests/test_a.py\ndocs/x.md\n"
    if args[0] == "cat-file":
        # reviewer/b.py — новый файл, остальные существовали
        if args[2].endswith(":reviewer/b.py"):
            raise snapshot.ground_truth.GitError("нет объекта")
        return ""
    raise AssertionError(f"неожиданный git-вызов: {args}")


def test_build_snapshot_counts_corpus_and_metrics(tmp_path):
    (tmp_path / "2026-01-01-PRI-7-x.md").write_text(BRIEF, encoding="utf-8")

    snap, rows = snapshot.build_snapshot(
        briefs_dir=tmp_path,
        run_git=_fake_git,
        commit="deadbee",
        taken_at="2026-08-14T00:00:00+00:00",
    )

    assert snap["schema"] == history.SCHEMA
    assert snap["commit"] == "deadbee"
    assert snap["corpus"]["briefs"] == 1
    assert snap["corpus"]["with_tokens"] == 1
    # знаменатель ядра — только reviewer/a.py (b.py новый, tests/docs вне ядра)
    assert rows[0]["expected_core"] == 1
    assert rows[0]["core_recall"] == 1.0
    assert snap["quality"]["core_recall_median"] == 1.0


def test_build_snapshot_weighted_cost_is_below_raw(tmp_path):
    (tmp_path / "2026-01-01-PRI-7-x.md").write_text(BRIEF, encoding="utf-8")

    snap, _ = snapshot.build_snapshot(
        briefs_dir=tmp_path, run_git=_fake_git, commit="c",
        taken_at="2026-08-14T00:00:00+00:00",
    )

    assert snap["cost"]["weighted_median"] < snap["cost"]["raw_median"]
    assert snap["cost"]["inflation"] > 1.0


def test_build_snapshot_counts_new_file_miss(tmp_path):
    (tmp_path / "2026-01-01-PRI-7-x.md").write_text(BRIEF, encoding="utf-8")

    snap, _ = snapshot.build_snapshot(
        briefs_dir=tmp_path, run_git=_fake_git, commit="c",
        taken_at="2026-08-14T00:00:00+00:00",
    )

    assert snap["misses"]["новый файл (не существовал до PR)"] == 1
    assert snap["misses"]["tests/"] == 1


def test_build_snapshot_brief_without_key_not_in_quality(tmp_path):
    (tmp_path / "2026-01-01-noключа.md").write_text(BRIEF, encoding="utf-8")

    snap, rows = snapshot.build_snapshot(
        briefs_dir=tmp_path, run_git=_fake_git, commit="c",
        taken_at="2026-08-14T00:00:00+00:00",
    )

    assert rows == []
    assert snap["corpus"]["with_ground_truth"] == 0
```

- [ ] **Step 3: Прогнать тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/eval/test_history.py tests/eval/test_snapshot.py -q`
Expected: FAIL — `ModuleNotFoundError` на `history`, `snapshot`

- [ ] **Step 4: Реализовать `history.py`**

```python
"""Хранилище срезов метрик (JSONL, append-only) и режим сравнения."""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass

# Версия схемы среза: чтение старых срезов не должно падать при добавлении
# новых метрик, поэтому diff работает по фактически присутствующим ключам.
SCHEMA = 1

HISTORY_PATH_NAME = "solve_task_metrics_history.jsonl"

# Полярность метрики: как трактовать рост значения.
POLARITY = {
    "cost.weighted_median": "lower_better",
    "cost.raw_median": "neutral",
    "cost.inflation": "neutral",
    "quality.core_recall_median": "higher_better",
    "quality.core_recall_mean": "higher_better",
    "quality.raw_recall_median": "neutral",
    "quality.denominator_median": "neutral",
    "quality.no_measurement": "lower_better",
    "corpus.briefs": "neutral",
    "corpus.with_tokens": "neutral",
    "corpus.with_ground_truth": "neutral",
    "endtoend.measured": "higher_better",
    "endtoend.weighted_median": "lower_better",
}


@dataclass
class Delta:
    """Изменение одной метрики между срезами."""

    metric: str
    old: object = None
    new: object = None
    delta: float | None = None
    direction: str = "без изменений"


def append_snapshot(path: pathlib.Path, snapshot: dict) -> None:
    """Дописать срез строкой в JSONL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(snapshot, ensure_ascii=False, sort_keys=True) + "\n")


def load_snapshots(path: pathlib.Path) -> list:
    """Прочитать все срезы; битые строки пропускаются, а не роняют чтение."""
    snapshots: list = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            snapshots.append(json.loads(line))
        except ValueError:
            continue
    return snapshots


def _flatten(snapshot: dict) -> dict:
    """Плоское представление 'секция.метрика' -> значение (только числа)."""
    flat: dict = {}
    for section, value in snapshot.items():
        if not isinstance(value, dict):
            continue
        for name, item in value.items():
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                flat[f"{section}.{name}"] = item
    return flat


def _direction(metric: str, delta: float) -> str:
    if delta == 0:
        return "без изменений"
    polarity = POLARITY.get(metric, "neutral")
    if polarity == "neutral":
        return "рост" if delta > 0 else "падение"
    improved = delta < 0 if polarity == "lower_better" else delta > 0
    return "улучшение" if improved else "ухудшение"


def diff_snapshots(old: dict, new: dict) -> list:
    """Дельты метрик нового среза против старого.

    Метрика, которой не было в старом срезе, помечается «новая» — показывать её
    как рост с нуля значит выдумывать историю.
    """
    old_flat = _flatten(old)
    new_flat = _flatten(new)
    deltas: list = []
    for metric in sorted(set(old_flat) | set(new_flat)):
        old_value = old_flat.get(metric)
        new_value = new_flat.get(metric)
        if old_value is None or new_value is None:
            deltas.append(
                Delta(metric=metric, old=old_value, new=new_value, direction="новая")
            )
            continue
        delta = new_value - old_value
        deltas.append(
            Delta(
                metric=metric,
                old=old_value,
                new=new_value,
                delta=delta,
                direction=_direction(metric, delta),
            )
        )
    old_mode = old.get("window_mode")
    new_mode = new.get("window_mode")
    if old_mode != new_mode:
        deltas.append(
            Delta(
                metric="window_mode",
                old=old_mode,
                new=new_mode,
                direction="несопоставимо",
            )
        )
    return deltas
```

- [ ] **Step 5: Реализовать `snapshot.py`**

```python
"""Сборка среза метрик по всему корпусу брифов."""
from __future__ import annotations

import pathlib
import statistics
from collections import Counter

from . import briefs, classify, cost, ground_truth, history, recall

# Режим окна замера цены этапа. 'sealed' — брифы, размеченные после починки
# повторного срабатывания brief_cost (окно = до первой записи брифа);
# 'legacy' — до неё. Смешивать значения в сравнении нельзя.
WINDOW_MODE = "sealed"


def _median(values: list):
    return statistics.median(values) if values else None


def build_snapshot(
    briefs_dir: pathlib.Path,
    run_git,
    commit: str,
    taken_at: str,
    transcripts=None,
) -> tuple:
    """Посчитать срез по корпусу брифов.

    Args:
        briefs_dir: каталог брифов.
        run_git: GitRunner (инъектируется, чтобы тесты шли без git-репозитория).
        commit: sha HEAD на момент прогона.
        taken_at: ISO-8601 метка времени прогона.
        transcripts: результат endtoend.scan_transcripts() или None.

    Returns:
        (срез, per-task строки для отчёта).
    """
    records = briefs.load_briefs(briefs_dir)
    with_tokens = [r for r in records if r.token_block]
    with_key = [r for r in records if r.task_key]

    weighted_values: list = []
    raw_values: list = []
    for record in with_tokens:
        block = record.token_block
        buckets = cost.sum_buckets(
            list(block.main_by_model.values()) + list(block.sidechain_by_model.values())
        )
        weighted_values.append(cost.weighted(buckets))
        raw_values.append(cost.raw(buckets))

    quality_rows: list = []
    report_rows: list = []
    misses: Counter = Counter()
    sync_skipped = 0

    for record in with_key:
        truth = ground_truth.collect(record.task_key, run_git)
        sync_skipped += truth.sync_merges_skipped
        if not truth.changed:
            continue
        expected_core = {
            path
            for path in truth.changed
            if classify.is_core_production_path(path)
            and ground_truth.path_existed(truth.parent_ref, path, run_git)
        }
        row = recall.evaluate_task(
            record.task_key, record.relevant_paths, truth.changed, expected_core
        )
        quality_rows.append(row)
        for missed in truth.changed - record.relevant_paths:
            existed = ground_truth.path_existed(truth.parent_ref, missed, run_git)
            misses[classify.categorize_miss(missed, existed)] += 1
        report_rows.append(
            {
                "key": row.task_key,
                "file": record.filename,
                "expected": row.expected,
                "expected_core": row.expected_core,
                "predicted": row.predicted,
                "hit_core": row.hit_core,
                "core_recall": row.core_recall,
                "raw_recall": row.raw_recall,
                "precision": row.precision,
            }
        )

    aggregate = recall.aggregate(quality_rows)
    weighted_median = _median(weighted_values)
    raw_median = _median(raw_values)

    snapshot = {
        "schema": history.SCHEMA,
        "taken_at": taken_at,
        "commit": commit,
        "window_mode": WINDOW_MODE,
        "corpus": {
            "briefs": len(records),
            "with_tokens": len(with_tokens),
            "with_key": len(with_key),
            "with_ground_truth": len(quality_rows),
            "sync_merges_skipped": sync_skipped,
        },
        "cost": {
            "weighted_median": weighted_median,
            "raw_median": raw_median,
            "inflation": cost.inflation(raw_median or 0.0, weighted_median or 0.0),
        },
        "quality": {
            "core_recall_median": aggregate.core_recall_median,
            "core_recall_mean": aggregate.core_recall_mean,
            "raw_recall_median": aggregate.raw_recall_median,
            "denominator_median": aggregate.denominator_median,
            "n_measured": aggregate.n_measured,
            "no_measurement": aggregate.no_measurement,
        },
        "misses": dict(misses),
    }
    if transcripts is not None:
        measured = [v["weighted"] for v in transcripts.values() if v.get("weighted")]
        snapshot["endtoend"] = {
            "measured": len(measured),
            "weighted_median": _median(measured),
        }
    return snapshot, report_rows
```

- [ ] **Step 6: Реализовать `report.py`**

```python
"""Markdown-отчёт по срезу метрик."""
from __future__ import annotations


def _pct(value) -> str:
    return "—" if value is None else f"{value:.0%}"


def _num(value) -> str:
    if value is None:
        return "—"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:.0f}"


def render(snapshot: dict, rows: list) -> str:
    """Отчёт по срезу: охват, цена, качество, промахи, per-task таблица."""
    corpus = snapshot["corpus"]
    cost_block = snapshot["cost"]
    quality = snapshot["quality"]
    inflation = cost_block.get("inflation")
    if inflation:
        raw_line = (
            "- Сырая сумма токенов — **не пропорциональна стоимости**, справочно: "
            f"{_num(cost_block['raw_median'])} (медиана); завышает в {inflation:.1f}×"
        )
    else:
        raw_line = "- Сырая сумма токенов: нет данных"
    lines = [
        "# Метрики этапа solve-task",
        "",
        f"Срез от {snapshot['taken_at']}, коммит `{snapshot['commit']}`, "
        f"режим окна замера цены: `{snapshot['window_mode']}`.",
        "",
        "## Охват",
        "",
        "| Метрика | Значение |",
        "|---|---|",
        f"| Брифов в корпусе | {corpus['briefs']} |",
        f"| С блоком токенов | {corpus['with_tokens']} |",
        f"| С ключом задачи | {corpus['with_key']} |",
        f"| С ground truth (PR-мерж найден) | {corpus['with_ground_truth']} |",
        f"| Отброшено sync-мержей | {corpus['sync_merges_skipped']} |",
        "",
        "## Цена этапа",
        "",
        f"- Взвешенный input-эквивалент (основная метрика): "
        f"**{_num(cost_block['weighted_median'])}** (медиана)",
        raw_line,
        "",
        "## Качество ретрива",
        "",
        f"- core-recall: медиана {_pct(quality['core_recall_median'])}, "
        f"среднее {_pct(quality['core_recall_mean'])}, N={quality['n_measured']}",
        f"- Задач без точки измерения (пустой знаменатель ядра): "
        f"{quality['no_measurement']} — не считаются нулевым recall",
        f"- Медианный размер знаменателя ядра: "
        f"{_num(quality['denominator_median'])}",
        f"- Сырой recall (справочно, измеряет выбор знаменателя, не качество "
        f"ретрива): медиана {_pct(quality['raw_recall_median'])}",
        "",
    ]
    if snapshot.get("endtoend"):
        end = snapshot["endtoend"]
        lines += [
            "## Полная цена задачи «под ключ»",
            "",
            f"- Измерено задач: {end['measured']} (остальные — транскрипт "
            "недоступен локально, это не ноль, а отсутствие замера)",
            f"- Взвешенный input-эквивалент: медиана {_num(end['weighted_median'])}",
            "",
        ]
    lines += ["## Промахи по категориям", "", "| Категория | Промахов |", "|---|---|"]
    for category, count in sorted(
        snapshot["misses"].items(), key=lambda kv: -kv[1]
    ):
        lines.append(f"| {category} | {count} |")
    lines += [
        "",
        "## Per-task",
        "",
        "| Ключ | Бриф | expected | core | predicted | hit | core-recall |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['key']} | {row['file']} | {row['expected']} | "
            f"{row['expected_core']} | {row['predicted']} | {row['hit_core']} | "
            f"{_pct(row['core_recall'])} |"
        )
    return "\n".join(lines) + "\n"
```

- [ ] **Step 7: Реализовать `__main__.py` с подкомандами `snapshot` и `compare`**

```python
"""CLI офлайн-харнесса метрик solve-task.

Запуск: python -m eval.solve_task_metrics {snapshot|compare|forecast}
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys

from . import ground_truth, history, report, snapshot as snapshot_mod

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
BRIEFS_DIR = REPO_ROOT / "docs" / "superpowers" / "briefs"
EVAL_DIR = REPO_ROOT / "eval"
HISTORY_PATH = EVAL_DIR / history.HISTORY_PATH_NAME
REPORT_PATH = EVAL_DIR / "solve_task_metrics_report.md"


def _head_commit(run_git) -> str:
    try:
        return run_git(["rev-parse", "HEAD"]).strip()
    except ground_truth.GitError:
        return "unknown"


def cmd_snapshot(_args) -> int:
    run_git = ground_truth.git_runner(REPO_ROOT)
    taken_at = dt.datetime.now(dt.timezone.utc).isoformat()
    snap, rows = snapshot_mod.build_snapshot(
        briefs_dir=BRIEFS_DIR,
        run_git=run_git,
        commit=_head_commit(run_git),
        taken_at=taken_at,
    )
    history.append_snapshot(HISTORY_PATH, snap)
    REPORT_PATH.write_text(report.render(snap, rows), encoding="utf-8")
    print(f"Срез сохранён: {HISTORY_PATH}")
    print(f"Отчёт записан: {REPORT_PATH}")
    return 0


def cmd_compare(_args) -> int:
    snapshots = history.load_snapshots(HISTORY_PATH)
    if len(snapshots) < 2:
        print("Нужно минимум два среза; сначала прогоните snapshot.")
        return 1
    deltas = history.diff_snapshots(snapshots[-2], snapshots[-1])
    print(f"Сравнение: {snapshots[-2]['taken_at']} → {snapshots[-1]['taken_at']}")
    for delta in deltas:
        old = "—" if delta.old is None else delta.old
        new = "—" if delta.new is None else delta.new
        change = "—" if delta.delta is None else f"{delta.delta:+.4g}"
        print(f"  {delta.metric}: {old} → {new} ({change}) — {delta.direction}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m eval.solve_task_metrics",
        description="Офлайн-метрики этапа solve-task: цена, качество ретрива, тренд.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("snapshot", help="пересчитать метрики и сохранить срез")
    subparsers.add_parser("compare", help="дельты последнего среза против предыдущего")
    args = parser.parse_args(argv)
    if args.command == "snapshot":
        return cmd_snapshot(args)
    return cmd_compare(args)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 8: Прогнать тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/eval -q`
Expected: PASS

- [ ] **Step 9: Прогнать команду на реальном корпусе**

Run: `.venv/bin/python -m eval.solve_task_metrics snapshot`
Expected: печатает пути среза и отчёта; `eval/solve_task_metrics_history.jsonl` и `eval/solve_task_metrics_report.md` созданы, отчёт содержит непустую таблицу per-task.

- [ ] **Step 10: Прогнать линт**

Run: `.venv/bin/ruff check eval/solve_task_metrics tests/eval`
Expected: `All checks passed!`

- [ ] **Step 11: Коммит**

```bash
git add eval/solve_task_metrics/history.py eval/solve_task_metrics/snapshot.py eval/solve_task_metrics/report.py eval/solve_task_metrics/__main__.py eval/solve_task_metrics_history.jsonl eval/solve_task_metrics_report.md tests/eval/test_history.py tests/eval/test_snapshot.py
git commit -m "feat(eval): история срезов, режим сравнения и команда snapshot"
```

---

### Task 5: Полная цена задачи «под ключ» из транскриптов

**Files:**
- Create: `eval/solve_task_metrics/endtoend.py`
- Modify: `eval/solve_task_metrics/__main__.py` (подключить сканирование транскриптов к `snapshot`)
- Test: `tests/eval/test_endtoend.py`

**Interfaces:**
- Consumes: `briefs.BUCKET_KEYS` (Task 1), `cost.weighted`, `cost.sum_buckets` (Task 3), `snapshot.build_snapshot(..., transcripts=...)` (Task 4).
- Produces:
  - `endtoend.SKILL_MARKER: str`, `endtoend.BASE_DIR_MARKER: str`
  - `endtoend.window_start(lines: list) -> tuple[str | None, int]` — `(ключ задачи, индекс начала)`; `(None, -1)` если маркеров нет
  - `endtoend.aggregate_after(lines: list, start_idx: int) -> dict` — суммарные бакеты
  - `endtoend.scan_transcripts(root: pathlib.Path) -> dict` — `{task_key: {"weighted": float, "buckets": dict, "sessions": int}}`

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/eval/test_endtoend.py`:

```python
"""Unit-тесты полной цены задачи «под ключ» по транскриптам сессий."""
import json

from eval.solve_task_metrics import endtoend


def _user(text):
    return {"type": "user", "message": {"content": text}}


def _assistant(model, out):
    return {
        "type": "assistant",
        "message": {
            "model": model,
            "usage": {
                "input_tokens": 100,
                "output_tokens": out,
                "cache_creation_input_tokens": 1_000,
                "cache_read_input_tokens": 10_000,
            },
        },
    }


SKILL_CALL = (
    "Base directory for this skill: /x/skills/solve-task\n"
    "# Solve Task\n\n`PRI-250` is either:\n"
)


def test_window_start_finds_marker_and_key():
    lines = [_assistant("m", 1), _user(SKILL_CALL), _assistant("m", 2)]

    key, idx = endtoend.window_start(lines)

    assert key == "PRI-250"
    assert idx == 1


def test_window_start_without_markers():
    assert endtoend.window_start([_user("привет")]) == (None, -1)


def test_window_start_without_key_is_unusable():
    lines = [_user("Base directory for this skill: /x/skills/solve-task\nбез ключа")]

    key, idx = endtoend.window_start(lines)

    assert key is None


def test_window_does_not_depend_on_brief_writes():
    """Окно тянется до конца транскрипта и не закрывается записью брифа."""
    lines = [
        _user(SKILL_CALL),
        _assistant("m", 10),
        _user("Записал бриф в docs/superpowers/briefs/x.md"),
        _assistant("m", 20),
    ]

    _, idx = endtoend.window_start(lines)
    buckets = endtoend.aggregate_after(lines, idx)

    assert buckets["output"] == 30


def test_aggregate_after_ignores_turns_before_window():
    lines = [_assistant("m", 999), _user(SKILL_CALL), _assistant("m", 5)]

    _, idx = endtoend.window_start(lines)

    assert endtoend.aggregate_after(lines, idx)["output"] == 5


def test_scan_transcripts_sums_sessions_of_one_task(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    for name in ("a.jsonl", "b.jsonl"):
        rows = [_user(SKILL_CALL), _assistant("m", 10)]
        (project / name).write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
        )

    result = endtoend.scan_transcripts(tmp_path)

    assert result["PRI-250"]["sessions"] == 2
    assert result["PRI-250"]["buckets"]["output"] == 20
    assert result["PRI-250"]["weighted"] > 0


def test_scan_transcripts_skips_sessions_without_key(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    rows = [_user("обычная сессия"), _assistant("m", 10)]
    (project / "a.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
    )

    assert endtoend.scan_transcripts(tmp_path) == {}


def test_scan_transcripts_missing_root_is_empty(tmp_path):
    assert endtoend.scan_transcripts(tmp_path / "нет") == {}
```

- [ ] **Step 2: Прогнать тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/eval/test_endtoend.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'eval.solve_task_metrics.endtoend'`

- [ ] **Step 3: Реализовать `endtoend.py`**

```python
"""Полная цена задачи «под ключ»: ретроспективно по транскриптам сессий.

Осознанное решение: без нового рантайм-хука. Харнесс остаётся офлайн-инструментом,
не имеет рантайм-риска и работает на уже закрытых задачах.

Окно замера — от user-сообщения с маркерами вызова скилла solve-task до конца
транскрипта. Запись файла брифа в определении окна НЕ участвует вовсе: именно
эта связь и была дефектом старого замера.
"""
from __future__ import annotations

import json
import pathlib
import re

from . import cost
from .briefs import BUCKET_KEYS

SKILL_MARKER = "skills/solve-task"
BASE_DIR_MARKER = "Base directory for this skill:"

_KEY_RE = re.compile(r"(PRI-\d+)", re.IGNORECASE)


def _message_text(line: dict) -> str:
    """Текст сообщения: content строкой или списком блоков {text}."""
    content = (line.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block["text"]
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    return ""


def window_start(lines: list) -> tuple:
    """Первый вызов скилла solve-task с распознанным ключом задачи.

    Returns:
        (ключ задачи или None, индекс начала окна или -1).
    """
    for index, line in enumerate(lines):
        if line.get("type") != "user":
            continue
        text = _message_text(line)
        if BASE_DIR_MARKER not in text or SKILL_MARKER not in text:
            continue
        match = _KEY_RE.search(text)
        return (match.group(1).upper() if match else None, index)
    return (None, -1)


def aggregate_after(lines: list, start_idx: int) -> dict:
    """Сумма бакетов токенов по assistant-ходам после начала окна."""
    buckets = {key: 0.0 for key in BUCKET_KEYS}
    if start_idx < 0:
        return buckets
    for line in lines[start_idx + 1 :]:
        if line.get("type") != "assistant":
            continue
        usage = (line.get("message") or {}).get("usage") or {}
        buckets["fresh_in"] += float(usage.get("input_tokens") or 0)
        buckets["output"] += float(usage.get("output_tokens") or 0)
        buckets["cache_write"] += float(usage.get("cache_creation_input_tokens") or 0)
        buckets["cache_read"] += float(usage.get("cache_read_input_tokens") or 0)
    return buckets


def _read_jsonl(path: pathlib.Path) -> list:
    rows: list = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def scan_transcripts(root: pathlib.Path) -> dict:
    """Полная цена по задачам из транскриптов под root.

    Сессия без распознанного ключа задачи в измерение не входит. Несколько
    сессий одной задачи суммируются, их число сохраняется.
    """
    result: dict = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*.jsonl")):
        lines = _read_jsonl(path)
        if not lines:
            continue
        key, start = window_start(lines)
        if not key or start < 0:
            continue
        buckets = aggregate_after(lines, start)
        entry = result.setdefault(
            key,
            {"buckets": {b: 0.0 for b in BUCKET_KEYS}, "sessions": 0, "weighted": 0.0},
        )
        entry["buckets"] = cost.sum_buckets([entry["buckets"], buckets])
        entry["sessions"] += 1
        entry["weighted"] = cost.weighted(entry["buckets"])
    return result
```

- [ ] **Step 4: Подключить транскрипты к `snapshot` в `__main__.py`**

В `eval/solve_task_metrics/__main__.py` заменить импорт и тело `cmd_snapshot`:

```python
from . import endtoend, ground_truth, history, report, snapshot as snapshot_mod

TRANSCRIPTS_ROOT = pathlib.Path.home() / ".claude" / "projects"
```

```python
def cmd_snapshot(_args) -> int:
    run_git = ground_truth.git_runner(REPO_ROOT)
    taken_at = dt.datetime.now(dt.timezone.utc).isoformat()
    transcripts = endtoend.scan_transcripts(TRANSCRIPTS_ROOT)
    snap, rows = snapshot_mod.build_snapshot(
        briefs_dir=BRIEFS_DIR,
        run_git=run_git,
        commit=_head_commit(run_git),
        taken_at=taken_at,
        transcripts=transcripts,
    )
    history.append_snapshot(HISTORY_PATH, snap)
    REPORT_PATH.write_text(report.render(snap, rows), encoding="utf-8")
    print(f"Срез сохранён: {HISTORY_PATH}")
    print(f"Отчёт записан: {REPORT_PATH}")
    print(
        f"Полная цена «под ключ» измерена для {snap['endtoend']['measured']} задач "
        "(остальные — транскрипт локально недоступен)"
    )
    return 0
```

- [ ] **Step 5: Прогнать тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/eval -q`
Expected: PASS

- [ ] **Step 6: Прогнать команду и убедиться, что покрытие печатается**

Run: `.venv/bin/python -m eval.solve_task_metrics snapshot`
Expected: в выводе строка «Полная цена «под ключ» измерена для N задач»; в отчёте появился раздел «Полная цена задачи «под ключ»».

- [ ] **Step 7: Прогнать линт**

Run: `.venv/bin/ruff check eval/solve_task_metrics tests/eval`
Expected: `All checks passed!`

- [ ] **Step 8: Коммит**

```bash
git add eval/solve_task_metrics/endtoend.py eval/solve_task_metrics/__main__.py eval/solve_task_metrics_history.jsonl eval/solve_task_metrics_report.md tests/eval/test_endtoend.py
git commit -m "feat(eval): полная цена задачи «под ключ» из транскриптов, окно не зависит от правок брифа"
```

---

### Task 6: Прогноз с разбросом

**Files:**
- Create: `eval/solve_task_metrics/forecast.py`
- Modify: `eval/solve_task_metrics/__main__.py` (подкоманда `forecast`)
- Test: `tests/eval/test_forecast.py`

**Interfaces:**
- Consumes: per-task строки из `snapshot.build_snapshot` (Task 4) — словари с ключами `expected_core`, `core_recall`.
- Produces:
  - `forecast.BUCKETS: tuple`, `forecast.MIN_SAMPLE: int`
  - `forecast.bucket_label(core_size: int) -> str`
  - `forecast.BucketForecast` (dataclass: `label: str`, `n: int`, `enough_data: bool`, `recall_median`, `recall_p25`, `recall_p75`)
  - `forecast.build(rows: list) -> list[BucketForecast]`
  - `forecast.describe(item: BucketForecast) -> str`

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/eval/test_forecast.py`:

```python
"""Unit-тесты прогноза: интервал вместо точечного числа."""
import pytest

from eval.solve_task_metrics import forecast


def _row(core, recall):
    return {"expected_core": core, "core_recall": recall}


def test_bucket_labels_by_core_size():
    assert forecast.bucket_label(1) == "1–3"
    assert forecast.bucket_label(3) == "1–3"
    assert forecast.bucket_label(4) == "4–9"
    assert forecast.bucket_label(9) == "4–9"
    assert forecast.bucket_label(10) == "10+"
    assert forecast.bucket_label(25) == "10+"


def test_build_returns_interval_for_large_enough_bucket():
    rows = [_row(2, value) for value in (0.2, 0.4, 0.5, 0.6, 0.9)]

    items = {item.label: item for item in forecast.build(rows)}

    small = items["1–3"]
    assert small.n == 5
    assert small.enough_data is True
    assert small.recall_median == pytest.approx(0.5)
    assert small.recall_p25 < small.recall_median < small.recall_p75


def test_build_marks_small_bucket_as_insufficient():
    rows = [_row(12, 0.3), _row(15, 0.4)]

    items = {item.label: item for item in forecast.build(rows)}

    assert items["10+"].enough_data is False
    assert items["10+"].recall_median is None


def test_build_skips_rows_without_measurement():
    rows = [_row(0, None), _row(2, 0.5)]

    items = {item.label: item for item in forecast.build(rows)}

    assert items["1–3"].n == 1
    assert "0" not in items


def test_describe_never_gives_a_bare_point_estimate():
    rows = [_row(2, value) for value in (0.2, 0.4, 0.5, 0.6, 0.9)]
    item = {i.label: i for i in forecast.build(rows)}["1–3"]

    text = forecast.describe(item)

    assert "–" in text  # интервал, а не одно число
    assert "N=5" in text


def test_describe_of_insufficient_bucket_says_so():
    item = {i.label: i for i in forecast.build([_row(12, 0.3)])}["10+"]

    assert "недостаточно данных" in forecast.describe(item)
```

- [ ] **Step 2: Прогнать тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/eval/test_forecast.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'eval.solve_task_metrics.forecast'`

- [ ] **Step 3: Реализовать `forecast.py`**

```python
"""Прогноз core-recall по размеру знаменателя ядра — интервалом, не числом.

Точечная оценка на выборке в десятки задач с разбросом от 0 до 100% —
это выдача шума за достоверность. Поэтому прогноз всегда интервальный
(медиана + межквартильный размах) и честно объявляет размер выборки.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

# Границы бакетов по размеру знаменателя ядра. Выбраны по baseline-распределению
# спайка PRI-246: медиана 4 файла, максимум 25.
BUCKETS = (("1–3", 1, 3), ("4–9", 4, 9), ("10+", 10, None))

# Бакет меньше этого размера не даёт интервала: сообщаем «недостаточно данных».
MIN_SAMPLE = 5


@dataclass
class BucketForecast:
    """Прогноз по одному бакету размера."""

    label: str
    n: int
    enough_data: bool
    recall_median: float | None = None
    recall_p25: float | None = None
    recall_p75: float | None = None


def bucket_label(core_size: int) -> str:
    """Метка бакета для заданного размера знаменателя ядра."""
    for label, low, high in BUCKETS:
        if core_size >= low and (high is None or core_size <= high):
            return label
    return BUCKETS[0][0]


def _quantile(values: list, q: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def build(rows: list) -> list:
    """Прогноз по всем бакетам. Задачи без точки измерения не участвуют."""
    grouped: dict = {label: [] for label, _, _ in BUCKETS}
    for row in rows:
        recall = row.get("core_recall")
        core = row.get("expected_core") or 0
        if recall is None or core < 1:
            continue
        grouped[bucket_label(core)].append(recall)
    items: list = []
    for label, _, _ in BUCKETS:
        values = grouped[label]
        item = BucketForecast(
            label=label, n=len(values), enough_data=len(values) >= MIN_SAMPLE
        )
        if item.enough_data:
            item.recall_median = statistics.median(values)
            item.recall_p25 = _quantile(values, 0.25)
            item.recall_p75 = _quantile(values, 0.75)
        items.append(item)
    return items


def describe(item: BucketForecast) -> str:
    """Человекочитаемый прогноз бакета — всегда с разбросом и размером выборки."""
    if not item.enough_data:
        return (
            f"{item.label} core-файлов: недостаточно данных для прогноза "
            f"(N={item.n}, нужно ≥{MIN_SAMPLE})"
        )
    return (
        f"{item.label} core-файлов: ожидаемый core-recall "
        f"{item.recall_p25:.0%}–{item.recall_p75:.0%} "
        f"(медиана {item.recall_median:.0%}, N={item.n})"
    )
```

- [ ] **Step 4: Добавить подкоманду `forecast` в `__main__.py`**

Заменить строку импорта пакета на:

```python
from . import endtoend, forecast, ground_truth, history, report, snapshot as snapshot_mod
```

Добавить команду:

```python
def cmd_forecast(args) -> int:
    run_git = ground_truth.git_runner(REPO_ROOT)
    _, rows = snapshot_mod.build_snapshot(
        briefs_dir=BRIEFS_DIR,
        run_git=run_git,
        commit=_head_commit(run_git),
        taken_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    )
    items = forecast.build(rows)
    if args.core_files is None:
        print("Прогноз core-recall по размеру знаменателя ядра:")
        for item in items:
            print(f"  {forecast.describe(item)}")
        return 0
    label = forecast.bucket_label(args.core_files)
    for item in items:
        if item.label == label:
            print(forecast.describe(item))
    return 0
```

В `main()` — зарегистрировать парсер и ветку:

```python
    forecast_parser = subparsers.add_parser(
        "forecast", help="прогноз core-recall с разбросом"
    )
    forecast_parser.add_argument(
        "--core-files",
        type=int,
        default=None,
        help="предполагаемое число файлов ядра у задачи",
    )
```

```python
    if args.command == "snapshot":
        return cmd_snapshot(args)
    if args.command == "forecast":
        return cmd_forecast(args)
    return cmd_compare(args)
```

- [ ] **Step 5: Прогнать тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/eval -q`
Expected: PASS

- [ ] **Step 6: Прогнать команду**

Run: `.venv/bin/python -m eval.solve_task_metrics forecast`
Expected: три строки прогноза по бакетам; ни одна не даёт голого точечного числа.

Run: `.venv/bin/python -m eval.solve_task_metrics forecast --core-files 12`
Expected: строка прогноза бакета `10+`.

- [ ] **Step 7: Прогнать линт**

Run: `.venv/bin/ruff check eval/solve_task_metrics tests/eval`
Expected: `All checks passed!`

- [ ] **Step 8: Коммит**

```bash
git add eval/solve_task_metrics/forecast.py eval/solve_task_metrics/__main__.py tests/eval/test_forecast.py
git commit -m "feat(eval): прогноз core-recall интервалом по размеру знаменателя ядра"
```

---

### Task 7: Печать brief_cost — повторная правка брифа не раздувает окно

**Files:**
- Modify: `plugin/hooks/brief_cost.py:253-282` (функция `run`)
- Modify: `tests/hooks/test_brief_cost.py` (дописать тесты, существующие не трогать)
- Modify: манифест codex (генерируется скриптом, не руками)

**Interfaces:**
- Consumes: ничего из предыдущих задач (независимая правка хука).
- Produces: `brief_cost.has_block(text: str) -> bool` — публичная для тестов проверка наличия блока токенов.

- [ ] **Step 1: Написать падающие тесты**

Дописать в конец `tests/hooks/test_brief_cost.py`:

```python
def _transcript(tmp_path, assistant_turns):
    """Транскрипт с вызовом скилла solve-task и N assistant-ходов."""
    rows = [
        {
            "type": "user",
            "message": {
                "content": (
                    "Base directory for this skill: /x/skills/solve-task\n# Solve Task"
                )
            },
        }
    ]
    for _ in range(assistant_turns):
        rows.append(
            {
                "type": "assistant",
                "message": {
                    "model": "m",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 200,
                        "cache_creation_input_tokens": 300,
                        "cache_read_input_tokens": 400,
                    },
                },
            }
        )
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
    )
    return str(path)


def _brief_setup(tmp_path):
    """Каталог брифов с .review.yml, где флаг brief_token_cost включён."""
    repo = tmp_path / "repo"
    briefs = repo / "docs" / "superpowers" / "briefs"
    briefs.mkdir(parents=True)
    (repo / ".review.yml").write_text(
        "solve_task:\n  brief_token_cost: true\n", encoding="utf-8"
    )
    brief = briefs / "2026-01-01-PRI-1-x.md"
    brief.write_text("# Brief — PRI-1\n\n## Task\nдетали\n", encoding="utf-8")
    return brief


def test_has_block_detects_token_header():
    assert bc.has_block("# Brief\n\n## Токены (этап solve-task)\nМодель: m\n") is True
    assert bc.has_block("# Brief\n\n## Task\n") is False


def test_run_writes_block_on_first_write(tmp_path):
    brief = _brief_setup(tmp_path)
    payload = {
        "tool_input": {"file_path": str(brief)},
        "transcript_path": _transcript(tmp_path, 1),
    }

    assert bc.run(payload) == 0
    assert bc.HEADER in brief.read_text(encoding="utf-8")


def test_run_is_noop_when_block_already_present(tmp_path):
    """Печать: повторная правка брифа не расширяет окно и не меняет цифру."""
    brief = _brief_setup(tmp_path)
    first = {
        "tool_input": {"file_path": str(brief)},
        "transcript_path": _transcript(tmp_path, 1),
    }
    bc.run(first)
    sealed = brief.read_text(encoding="utf-8")

    # В транскрипте прибавилось ходов — старое поведение раздуло бы число.
    second = {
        "tool_input": {"file_path": str(brief)},
        "transcript_path": _transcript(tmp_path, 5),
    }
    assert bc.run(second) == 0

    assert brief.read_text(encoding="utf-8") == sealed
```

- [ ] **Step 2: Прогнать тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/hooks/test_brief_cost.py -q`
Expected: FAIL — `AttributeError: module 'brief_cost' has no attribute 'has_block'`, а `test_run_is_noop_when_block_already_present` падает на неравенстве текста.

- [ ] **Step 3: Реализовать печать в `plugin/hooks/brief_cost.py`**

Добавить функцию рядом с `upsert_block`:

```python
def has_block(text: str) -> bool:
    """True, если бриф уже содержит блок токенов (замер запечатан)."""
    return any(line.strip() == HEADER for line in text.splitlines())
```

В `run()` перенести чтение брифа выше агрегации и добавить печать — заменить блок между path-guard и записью:

```python
        lines = _read_jsonl(payload.get("transcript_path") or "")
        if not lines:
            return 0
        brief = _read_text(file_path)
        if brief is None:
            return 0
        # Печать: блок описывает завершённый этап сборки контекста, то есть окно
        # ДО первой записи брифа. Пересчёт при повторной правке файла втянул бы
        # в число всё, что произошло после (брейншторм, план, реализация).
        if has_block(brief):
            return 0
        start = find_window_start(lines)
        if start < 0:
            return 0
        by_model, sidechain = aggregate_usage(lines, start)
        if not by_model and not sidechain:
            return 0
        _write_text(file_path, upsert_block(brief, render_block(by_model, sidechain)))
```

- [ ] **Step 4: Прогнать тесты хука — убедиться, что проходят**

Run: `.venv/bin/pytest tests/hooks/ -q`
Expected: PASS (включая существующие тесты `upsert_block`, которые не менялись)

- [ ] **Step 5: Пересобрать манифест codex-плагина**

Run: `.venv/bin/python scripts/update_codex_plugin_manifest.py`
Expected: манифест обновлён (payload-digest пересчитан)

- [ ] **Step 6: Прогнать install-тесты и линт**

Run: `.venv/bin/pytest tests/install tests/hooks -q && .venv/bin/ruff check plugin/hooks tests/hooks`
Expected: PASS, `All checks passed!`

- [ ] **Step 7: Коммит**

```bash
git add plugin/hooks/brief_cost.py tests/hooks/test_brief_cost.py
git add -A plugin
git commit -m "fix(hooks): запечатать окно замера brief_cost — повторная правка брифа не раздувает цифру"
```

---

### Task 8: Удаление спайка и документация запуска

**Files:**
- Delete: `eval/pri246_solve_task_cost.py`
- Modify: `README.md`
- Modify: `README.ru.md`
- Test: `tests/eval/test_docs.py`

**Interfaces:**
- Consumes: CLI `python -m eval.solve_task_metrics {snapshot|compare|forecast}` (Tasks 4–6).
- Produces: ничего для последующих задач (финальная).

- [ ] **Step 1: Написать падающий тест документации**

Создать `tests/eval/test_docs.py`:

```python
"""Guard: обе версии README документируют команду харнесса метрик."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
COMMAND = "python -m eval.solve_task_metrics"


def test_spike_script_removed():
    assert not (ROOT / "eval" / "pri246_solve_task_cost.py").exists()


def test_spike_report_kept_as_historical_artifact():
    assert (ROOT / "eval" / "pri246_report.md").exists()


def test_both_readmes_document_the_command():
    for name in ("README.md", "README.ru.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert COMMAND in text, f"{name} не документирует команду харнесса"
        for subcommand in ("snapshot", "compare", "forecast"):
            assert f"{COMMAND} {subcommand}" in text, f"{name}: нет {subcommand}"
```

- [ ] **Step 2: Прогнать тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/eval/test_docs.py -q`
Expected: FAIL — спайк-скрипт ещё на месте, README не содержат команду

- [ ] **Step 3: Удалить спайк**

```bash
git rm eval/pri246_solve_task_cost.py
```

`eval/pri246_report.md` остаётся как замороженный исторический артефакт — не удалять.

- [ ] **Step 4: Задокументировать команду в `README.ru.md`**

Найти раздел с командами разработки (рядом с описанием тестов/линта) и добавить блок:

```markdown
### Метрики этапа solve-task (офлайн)

Офлайн-харнесс считает цену этапа и качество ретрива по накопленному корпусу
брифов (`docs/superpowers/briefs/`), хранит историю срезов и умеет сравнивать
прогоны. Не требует Postgres, Neo4j и сети — только локальный git.

```bash
python -m eval.solve_task_metrics snapshot   # пересчитать метрики, сохранить срез, обновить отчёт
python -m eval.solve_task_metrics compare    # дельты последнего среза против предыдущего
python -m eval.solve_task_metrics forecast   # прогноз core-recall с разбросом
```

Цена считается во взвешенных input-эквивалентах (`output ×5`, `cache-write ×1.25`,
`cache-read ×0.1`); сырая сумма токенов показывается только справочно — она не
пропорциональна стоимости. Качество — core-recall на суженном знаменателе;
задачи без файлов ядра в diff'е учитываются как «нет точки измерения», а не как
нулевой recall. Срезы лежат в `eval/solve_task_metrics_history.jsonl`, отчёт —
в `eval/solve_task_metrics_report.md`.
```

- [ ] **Step 5: Задокументировать команду в `README.md` (английская версия)**

Добавить в соответствующий раздел:

```markdown
### solve-task metrics (offline)

An offline harness measures the cost of the solve-task stage and retrieval
quality over the accumulated brief corpus (`docs/superpowers/briefs/`), stores a
history of snapshots and compares runs. No Postgres, Neo4j or network needed —
local git only.

```bash
python -m eval.solve_task_metrics snapshot   # recompute metrics, store a snapshot, refresh the report
python -m eval.solve_task_metrics compare    # deltas of the latest snapshot against the previous one
python -m eval.solve_task_metrics forecast   # core-recall forecast with a spread
```

Cost is measured in weighted input-equivalents (`output ×5`, `cache-write ×1.25`,
`cache-read ×0.1`); the raw token sum is shown for reference only — it is not
proportional to cost. Quality is core-recall over a narrowed denominator; tasks
whose diff contains no core files count as "no measurement point", not as zero
recall. Snapshots live in `eval/solve_task_metrics_history.jsonl`, the report in
`eval/solve_task_metrics_report.md`.
```

- [ ] **Step 6: Прогнать тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/eval/test_docs.py -q`
Expected: PASS

- [ ] **Step 7: Прогнать весь unit-набор и линт**

Run: `.venv/bin/pytest -q`
Expected: PASS (весь набор без integration)

Run: `.venv/bin/ruff check eval tests/eval`
Expected: `All checks passed!`

- [ ] **Step 8: Коммит**

```bash
git add -A eval README.md README.ru.md tests/eval/test_docs.py
git commit -m "docs(eval): одна команда харнесса метрик в обоих README, удалён спайк PRI-246"
```

---

## Финальная верификация

- [ ] `.venv/bin/pytest -q` — весь unit-набор зелёный
- [ ] `.venv/bin/ruff check eval plugin/hooks tests/eval tests/hooks` — чисто
- [ ] `.venv/bin/python -m eval.solve_task_metrics snapshot` — срез и отчёт обновляются
- [ ] `.venv/bin/python -m eval.solve_task_metrics compare` — печатает дельты (после второго прогона)
- [ ] `.venv/bin/python -m eval.solve_task_metrics forecast` — интервалы, ни одного голого точечного числа
- [ ] `grep -rn "from reviewer" eval/solve_task_metrics/` — пусто (харнесс не в продакшн-пути)
- [ ] `grep -rn "eval.solve_task_metrics" reviewer/` — пусто (продакшн не зависит от харнесса)
- [ ] Манифест codex пересобран после правки `plugin/hooks/brief_cost.py`
