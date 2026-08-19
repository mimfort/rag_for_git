# PRI-257 — Подмешивание diff-путей похожих задач и git-со-изменяемости: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** подмешать в секцию `code` контекста задачи два путевых сигнала — фактические diff-пути похожих задач и git-со-изменяемость — с жёсткой квотой, видимой нотой и fail-soft деградацией.

**Architecture:** источники входят третьим приоритетом в существующий пайплайн `search_multi` (`hybrid → graph-only → augmented`), дальше без изменений `_dedupe_overlapping → diversify_by_file → cap_block`. Подсчёт со-изменяемости — чистая функция над уже прочитанными множествами файлов коммитов; git и Postgres остаются на краю.

**Tech Stack:** Python 3.11+, psycopg (Postgres/ParadeDB), pytest (unit — без сети, БД и localhost), git CLI через `subprocess`.

**Spec:** `docs/superpowers/specs/2026-08-17-pri-257-augmented-candidates-design.md`

## Global Constraints

- Язык кода проекта — русский: комментарии, докстринги, сообщения. Сохранять стиль.
- Unit-тестам запрещены сеть, localhost-сокеты, Postgres и Neo4j. Любой тест с реальным I/O обязан иметь `@pytest.mark.integration`.
- Коммиты — Conventional Commits на русском, без self-attribution (никаких `Co-Authored-By`, упоминаний Claude).
- Запуск тестов: `.venv/bin/pytest -q` (по умолчанию исключает integration).
- Линт: `.venv/bin/ruff check <files>` по тронутым файлам (repo-wide чистоты не требуется).
- Схема БД НЕ меняется: новых колонок и миграций в этой задаче нет.
- Публичные `search_codebase` / `search_base` не трогаются; формат `payload.related.similar` не меняется.
- Квота по умолчанию: `CodeSectionLimits.max_augmented_files = 3` при `max_files = 12`.
- Глубина git-истории co-change — модульная константа `COCHANGE_COMMITS = 300` в `reviewer/retrieval/augment.py`, НЕ ключ `.review.yml`.
- Порог со-изменяемости по умолчанию: `MIN_COCHANGE = 2` (файл должен встретиться с seed'ом минимум в двух коммитах).
- Ветка работы: `feat/pri-257-augmented-candidates`. `git push`, создание PR и запись в доску требуют явного подтверждения пользователя.

---

### Task 1: Чистый слой подсчёта и git-примитивы

**Files:**
- Create: `reviewer/retrieval/augment.py`
- Modify: `reviewer/gitutil.py` (дописать в конец файла)
- Test: `tests/retrieval/test_augment.py` (создать)

**Interfaces:**
- Consumes: ничего (первая задача).
- Produces:
  - `reviewer.retrieval.augment.AugmentResult(paths: list[str], by_source: dict[str, int], gaps: list[str])` — frozen dataclass.
  - `reviewer.retrieval.augment.rank_cochanged(commit_files: list[set[str]], seeds: set[str], *, min_count: int = MIN_COCHANGE, limit: int) -> list[str]`
  - `reviewer.retrieval.augment.COCHANGE_COMMITS: int = 300`, `MIN_COCHANGE: int = 2`
  - `reviewer.gitutil.commit_file_sets(repo: str, *, limit: int) -> list[set[str]]`
  - `reviewer.gitutil.paths_touched_by_grep(repo: str, pattern: str, *, limit: int) -> list[str]`

- [ ] **Step 1: Написать падающие тесты чистого подсчёта**

Создать `tests/retrieval/test_augment.py`:

```python
"""Подсчёт co-change и сборка путей-кандидатов (PRI-257)."""
from reviewer.retrieval.augment import AugmentResult, rank_cochanged


def test_cochanged_ranks_by_cooccurrence_count():
    commits = [
        {"reviewer/a.py", "reviewer/b.py"},
        {"reviewer/a.py", "reviewer/b.py"},
        {"reviewer/a.py", "reviewer/c.py"},
    ]
    assert rank_cochanged(commits, {"reviewer/a.py"}, min_count=2, limit=10) == [
        "reviewer/b.py"
    ]


def test_cochanged_excludes_seeds_themselves():
    commits = [{"reviewer/a.py", "reviewer/b.py"}] * 3
    ranked = rank_cochanged(commits, {"reviewer/a.py"}, min_count=2, limit=10)
    assert "reviewer/a.py" not in ranked


def test_cochanged_respects_min_count_and_limit():
    commits = [
        {"reviewer/a.py", "reviewer/b.py"},
        {"reviewer/a.py", "reviewer/c.py"},
        {"reviewer/a.py", "reviewer/c.py"},
        {"reviewer/a.py", "reviewer/d.py"},
        {"reviewer/a.py", "reviewer/d.py"},
    ]
    assert rank_cochanged(commits, {"reviewer/a.py"}, min_count=2, limit=1) == [
        "reviewer/c.py"
    ], "порядок при равном счёте — по пути, лимит режет хвост"


def test_cochanged_without_seeds_or_commits_is_empty():
    assert rank_cochanged([], {"reviewer/a.py"}, min_count=2, limit=5) == []
    assert rank_cochanged([{"reviewer/a.py"}], set(), min_count=2, limit=5) == []


def test_augment_result_is_immutable_value():
    result = AugmentResult(paths=["reviewer/a.py"], by_source={"cochange": 1}, gaps=[])
    assert result.paths == ["reviewer/a.py"]
    assert result.by_source["cochange"] == 1
    assert result.gaps == []
```

- [ ] **Step 2: Запустить тесты, убедиться что падают**

Run: `.venv/bin/pytest tests/retrieval/test_augment.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'reviewer.retrieval.augment'`

- [ ] **Step 3: Написать модуль `reviewer/retrieval/augment.py`**

```python
"""Путевые сигналы-кандидаты секции code: похожие задачи и co-change (PRI-257).

Модуль намеренно без I/O: и множества файлов коммитов, и строки истории
прогонов приходят параметрами. Подсчёт со-появления — чистая функция, поэтому
тестируется без git и без временного репозитория.
"""
from __future__ import annotations

from dataclasses import dataclass, field

COCHANGE_COMMITS = 300
"""Глубина истории для co-change. Модульная константа, а не ключ .review.yml:
третий регулятор рядом с max_files/max_augmented_files рассинхронизировался бы
с ними, а крутить его оператору незачем."""

MIN_COCHANGE = 2
"""Порог со-появления: один общий коммит — совпадение, два — уже сигнал."""


@dataclass(frozen=True)
class AugmentResult:
    """Пути-кандидаты, их происхождение и пробелы сбора."""

    paths: list[str] = field(default_factory=list)
    by_source: dict[str, int] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)


def rank_cochanged(commit_files: list[set[str]], seeds: set[str], *,
                   min_count: int = MIN_COCHANGE, limit: int) -> list[str]:
    """Файлы, чаще прочих менявшиеся в одних коммитах с seeds.

    Порядок — по убыванию числа со-появлений, тай-брейк по пути, поэтому
    результат детерминирован и не зависит от порядка коммитов на входе.
    """
    if not seeds or not commit_files or limit <= 0:
        return []
    counts: dict[str, int] = {}
    for files in commit_files:
        if not files & seeds:
            continue
        for path in files - seeds:
            counts[path] = counts.get(path, 0) + 1
    ranked = sorted(
        (path for path, count in counts.items() if count >= min_count),
        key=lambda path: (-counts[path], path))
    return ranked[:limit]
```

- [ ] **Step 4: Запустить тесты чистого слоя**

Run: `.venv/bin/pytest tests/retrieval/test_augment.py -q`
Expected: PASS (5 тестов)

- [ ] **Step 5: Дописать git-примитивы в `reviewer/gitutil.py`**

Добавить в конец файла (рядом с прочими fail-soft обёртками вроде `commits_behind`):

```python
def commit_file_sets(repo: str, *, limit: int) -> list[set[str]]:
    """Последние ``limit`` коммитов как список множеств затронутых файлов.

    Один процесс git на весь запрос. Pathspec намеренно НЕ передаётся: с ним
    `--name-only` отдал бы только совпавшие пути, а со-изменяемость требует
    полного состава коммита. Fail-soft: не git-репо или сбой — пустой список.
    """
    try:
        out = _git(repo, "log", f"-n{limit}", "--name-only", "--no-merges",
                   "--pretty=format:%x00")
    except (OSError, subprocess.CalledProcessError):
        return []
    sets: list[set[str]] = []
    current: set[str] = set()
    for line in out.splitlines():
        if line.startswith("\0"):
            if current:
                sets.append(current)
            current = set()
            continue
        if line:
            current.add(line)
    if current:
        sets.append(current)
    return sets


def paths_touched_by_grep(repo: str, pattern: str, *, limit: int) -> list[str]:
    """Пути коммитов, чьё сообщение содержит ``pattern`` (ключ задачи).

    Фолбэк к истории прогонов: ключ задачи присутствует в именах веток
    (feat/pri-256-…), сообщениях merge-коммитов и телах PR. Fail-soft.
    """
    try:
        out = _git(repo, "log", f"-n{limit}", "--name-only", "--pretty=format:",
                   "-i", f"--grep={pattern}")
    except (OSError, subprocess.CalledProcessError):
        return []
    seen: dict[str, None] = {}
    for line in out.splitlines():
        if line:
            seen.setdefault(line, None)
    return list(seen)
```

- [ ] **Step 6: Написать integration-тест git-примитивов**

Создать `tests/services/test_gitutil_cochange.py`:

```python
"""git-примитивы co-change на настоящем репозитории (PRI-257)."""
import subprocess

import pytest

from reviewer.gitutil import commit_file_sets, paths_touched_by_grep


def _run(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    _run(tmp_path, "init", "-q", "-b", "main")
    _run(tmp_path, "config", "user.email", "t@example.com")
    _run(tmp_path, "config", "user.name", "t")
    return tmp_path


def _commit(repo, message, files):
    for name, body in files.items():
        (repo / name).write_text(body, encoding="utf-8")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", message)


@pytest.mark.integration
def test_commit_file_sets_groups_files_per_commit(repo):
    _commit(repo, "первый", {"a.py": "1", "b.py": "1"})
    _commit(repo, "второй", {"c.py": "1"})
    sets = commit_file_sets(str(repo), limit=10)
    assert {"c.py"} in sets
    assert {"a.py", "b.py"} in sets


@pytest.mark.integration
def test_paths_touched_by_grep_matches_task_key(repo):
    _commit(repo, "feat(x): PRI-999 сделано", {"a.py": "1"})
    _commit(repo, "чужой коммит", {"b.py": "1"})
    assert paths_touched_by_grep(str(repo), "PRI-999", limit=50) == ["a.py"]


@pytest.mark.integration
def test_non_git_path_is_fail_soft(tmp_path):
    assert commit_file_sets(str(tmp_path / "нет"), limit=10) == []
    assert paths_touched_by_grep(str(tmp_path / "нет"), "PRI-1", limit=10) == []
```

- [ ] **Step 7: Прогнать оба набора и линт**

Run: `.venv/bin/pytest tests/retrieval/test_augment.py -q && .venv/bin/pytest tests/services/test_gitutil_cochange.py -q -m integration && .venv/bin/ruff check reviewer/retrieval/augment.py reviewer/gitutil.py tests/retrieval/test_augment.py tests/services/test_gitutil_cochange.py`
Expected: PASS, PASS (3 теста), линт чист

- [ ] **Step 8: Коммит**

```bash
git add reviewer/retrieval/augment.py reviewer/gitutil.py tests/retrieval/test_augment.py tests/services/test_gitutil_cochange.py
git commit -m "feat(retrieval): чистый подсчёт co-change и git-примитивы истории"
```

---

### Task 2: Источник diff-путей похожих задач

**Files:**
- Modify: `reviewer/web/history.py` (новый метод рядом с `brief_quality_trend`, строка 583)
- Modify: `reviewer/tasks/store.py:71-76` (`TaskHit`), `reviewer/tasks/store.py:252-283` (`search`)
- Modify: `reviewer/tasks/service.py:318-345` (`search_tasks` → рендер поверх `search_hits`)
- Modify: `reviewer/retrieval/augment.py` (функция сборки)
- Test: `tests/retrieval/test_augment.py` (дописать), `tests/tasks/test_search_hits.py` (создать)

**Interfaces:**
- Consumes: `AugmentResult` из Task 1.
- Produces:
  - `reviewer.web.history.History.diff_paths_for_tasks(keys: list[str], repo: str | None = None) -> dict[str, list[str]]`
  - `reviewer.tasks.store.TaskHit(key, title, status, score, aliases: list[str])`
  - `reviewer.tasks.service.TaskService.search_hits(query: str, top_k: int | None = None, project: str | None = None) -> list[TaskHit] | None` (`None` = источник недоступен, `[]` = ничего не найдено)
  - `reviewer.tasks.service.TaskService.render_hits(hits: list[TaskHit] | None, top_k: int | None = None) -> str`
  - `reviewer.retrieval.augment.collect_similar_task_paths(*, keys, aliases_by_key, history, clone_path, limit) -> AugmentResult`

- [ ] **Step 1: Написать падающий тест сборки путей похожих задач**

Дописать в `tests/retrieval/test_augment.py`:

```python
from reviewer.retrieval.augment import collect_similar_task_paths


class _FakeHistory:
    def __init__(self, by_key=None, fail=False):
        self._by_key = by_key or {}
        self._fail = fail

    def diff_paths_for_tasks(self, keys, repo=None):
        if self._fail:
            raise RuntimeError("Postgres недоступен")
        return {k: v for k, v in self._by_key.items() if k in keys}


def test_similar_paths_match_by_key_and_alias():
    history = _FakeHistory({"PRI-257": ["reviewer/a.py"]})
    result = collect_similar_task_paths(
        keys=["ID-311"], aliases_by_key={"ID-311": ["PRI-257"]},
        history=history, clone_path="", limit=10)
    assert result.paths == ["reviewer/a.py"]
    assert result.by_source["similar_diffs"] == 1


def test_similar_paths_survive_history_failure_with_gap():
    result = collect_similar_task_paths(
        keys=["ID-311"], aliases_by_key={}, history=_FakeHistory(fail=True),
        clone_path="", limit=10)
    assert result.paths == []
    assert any("история прогонов" in gap for gap in result.gaps)


def test_similar_paths_without_history_fall_back_to_git(monkeypatch):
    calls: list = []

    def fake_grep(repo, pattern, *, limit):
        calls.append(pattern)
        return ["reviewer/b.py"]

    monkeypatch.setattr("reviewer.retrieval.augment.gitutil.paths_touched_by_grep",
                        fake_grep)
    result = collect_similar_task_paths(
        keys=["ID-311"], aliases_by_key={"ID-311": ["PRI-257"]},
        history=None, clone_path="/repo", limit=10)
    assert result.paths == ["reviewer/b.py"]
    assert "PRI-257" in calls, "grep идёт по человеческому ключу, не по ID-N"


def test_similar_paths_respect_limit():
    history = _FakeHistory({"PRI-1": [f"reviewer/f{i}.py" for i in range(10)]})
    result = collect_similar_task_paths(
        keys=["PRI-1"], aliases_by_key={}, history=history, clone_path="", limit=3)
    assert len(result.paths) == 3
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `.venv/bin/pytest tests/retrieval/test_augment.py -q`
Expected: FAIL — `ImportError: cannot import name 'collect_similar_task_paths'`

- [ ] **Step 3: Реализовать `collect_similar_task_paths` в `reviewer/retrieval/augment.py`**

Дописать импорт `from reviewer import gitutil` в шапку модуля и функцию:

```python
GIT_GREP_COMMITS = 200
"""Сколько последних коммитов просматривает фолбэк по ключу задачи."""


def _human_keys(key: str, aliases_by_key: dict[str, list[str]]) -> list[str]:
    """Ключ и его алиасы: стор ключует ID-N, доска и git знают PRI-N."""
    return [key, *(aliases_by_key.get(key) or [])]


def collect_similar_task_paths(*, keys, aliases_by_key, history, clone_path,
                               limit: int) -> AugmentResult:
    """Фактические diff-пути похожих задач: история прогонов, фолбэк — git.

    Табличный источник точнее (пути уже классифицированы как core), но
    появляется только у задачи с опубликованным ревью и брифом. Фолбэк по
    сообщениям коммитов даёт покрытие на репозитории без истории прогонов.
    """
    if not keys or limit <= 0:
        return AugmentResult()
    lookup: list[str] = []
    for key in keys:
        lookup.extend(_human_keys(key, aliases_by_key))
    gaps: list[str] = []
    ordered: dict[str, None] = {}
    if history is not None:
        try:
            by_key = history.diff_paths_for_tasks(lookup)
            for key in lookup:
                for path in by_key.get(key) or []:
                    ordered.setdefault(path, None)
        except Exception as exc:  # noqa: BLE001 — источник недоступен, это штатный случай
            gaps.append(f"история прогонов недоступна: {type(exc).__name__}")
    if not ordered and clone_path:
        for key in lookup:
            try:
                for path in gitutil.paths_touched_by_grep(
                        clone_path, key, limit=GIT_GREP_COMMITS):
                    ordered.setdefault(path, None)
            except Exception as exc:  # noqa: BLE001
                gaps.append(f"git-история недоступна: {type(exc).__name__}")
                break
    paths = list(ordered)[:limit]
    return AugmentResult(paths=paths,
                         by_source={"similar_diffs": len(paths)} if paths else {},
                         gaps=gaps)
```

- [ ] **Step 4: Прогнать тесты слоя**

Run: `.venv/bin/pytest tests/retrieval/test_augment.py -q`
Expected: PASS (9 тестов)

- [ ] **Step 5: Добавить `diff_paths_for_tasks` в `reviewer/web/history.py`**

Вставить метод сразу после `brief_quality_trend` (после строки 697, перед `_row_to_dict`), по образцу его же `_connect`/fail-soft:

```python
    def diff_paths_for_tasks(self, keys: list[str],
                             repo: str | None = None) -> dict[str, list[str]]:
        """Фактические core-пути диффов задач: task_key → пути (PRI-257).

        Union по всем строкам задачи, как в brief_quality_trend: у задачи может
        быть несколько PR, и одна строка — это один PR, а не вся задача.
        Fail-soft: недоступная БД — пустая карта, вызывающий продолжает сборку.
        """
        if not keys:
            return {}
        sql = """
        SELECT task_key, expected_core_paths
        FROM brief_quality
        WHERE status = 'measured' AND task_key = ANY(%(keys)s)
          AND (%(repo)s::text IS NULL OR repo = %(repo)s::text)
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, {"keys": list(keys), "repo": repo}).fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось получить diff-пути задач: %s", exc)
            return {}
        out: dict[str, set] = {}
        for task_key, paths in rows:
            out.setdefault(task_key, set()).update(paths or [])
        return {key: sorted(paths) for key, paths in out.items()}
```

- [ ] **Step 6: Добавить `aliases` в `TaskHit` и `search`, а `search_hits` — в `TaskService`**

В `reviewer/tasks/store.py` дополнить dataclass (строки 71-76):

```python
@dataclass(frozen=True)
class TaskHit:
    key: str
    title: str
    status: str | None
    score: float
    aliases: list[str] = field(default_factory=list)
```

(если `field` ещё не импортирован — добавить в существующий `from dataclasses import ...`).

В SQL метода `search` заменить строку SELECT и сборку результата:

```python
        SELECT t.key, t.title, t.status, t.aliases, SUM(r.s) AS score
        FROM rrf r JOIN tasks t USING (id)
        GROUP BY t.id, t.key, t.title, t.status, t.aliases
        ORDER BY score DESC LIMIT %(k)s
```

и в конструкции хитов передать `aliases=list(row_aliases or [])` (распаковка строки получает на одно поле больше — поправить распаковку кортежа под новую форму).

В `reviewer/tasks/service.py` разделить поиск и рендер:

```python
    def search_hits(self, query: str, top_k: int | None = None,
                    project: str | None = None) -> list | None:
        """Структурные хиты похожих задач (PRI-257).

        Отдельный метод, потому что подмешивание diff-путей ключуется по
        hit.key и hit.aliases: парсить их regex'ом из человекочитаемого
        рендера search_tasks значило бы завязаться на формат сообщения.

        None и [] различаются намеренно: прежний search_tasks отдавал разные
        ноты на «источник недоступен» и «ничего не найдено», и схлопывание их
        в пустой список было бы регрессией контракта. None — сбой, [] — пусто.
        """
        from reviewer.policy.context_limits import TasksLimits
        ceiling = top_k or TasksLimits.ceiling
        try:
            vec = self._embedder.embed_query(query)
            return self._store.search(query, vec, top_k=max(ceiling * 3, 30),
                                      project=project)
        except Exception:
            log.warning("search_hits: сбой поиска по запросу %r", query, exc_info=True)
            return None

    def render_hits(self, hits: list | None, top_k: int | None = None) -> str:
        """Рендер хитов в формат search_tasks. Формат не менялся с PRI-202."""
        from reviewer.policy.context_limits import TasksLimits
        ceiling = top_k or TasksLimits.ceiling
        if hits is None:
            return "(task search unavailable)"
        if not hits:
            return "(no similar tasks found)"
        total = len(hits)
        shown = hits[:ceiling]
        # Ранг-ординал — стабильный, query-независимый сигнал для relevance-фильтра
        # (solve-task/review-pr прунят по порядку). Score даём с 4 знаками: RRF лежит
        # в ≈0.016–0.033, и грубая точность схлопнула бы близкие задачи в одно число.
        lines = [f"{i}. {h.key} [{h.status or '—'}] {h.title} (score {h.score:.4f})"
                 for i, h in enumerate(shown, 1)]
        if total > ceiling:
            lines.append(f"— показано {ceiling} из {total} (рельса ceiling). "
                         f"Перевызови с большим ceiling для остальных.")
        return "\n".join(lines)
```

и переписать `search_tasks` как композицию двух методов — формат вывода обязан остаться байт-в-байт прежним:

```python
    def search_tasks(self, query: str, top_k: int | None = None,
                     project: str | None = None) -> str:
        """Похожие задачи (RRF, без реранкера) с рельсой ceiling (PRI-202).

        Композиция search_hits + render_hits: изменился только способ
        получения, не формат (PRI-257).
        """
        return self.render_hits(self.search_hits(query, top_k, project=project), top_k)
```

- [ ] **Step 7: Написать тест сохранения контракта рендера**

Создать `tests/tasks/test_search_hits.py`:

```python
"""search_tasks остаётся рендером поверх структурных хитов (PRI-257)."""
from reviewer.tasks.store import TaskHit


from reviewer.tasks.service import TaskService


class _Svc:
    """Носитель методов TaskService без его зависимостей: рендер их не требует."""

    render_hits = TaskService.render_hits
    search_tasks = TaskService.search_tasks

    def __init__(self, hits):
        self._hits = hits

    def search_hits(self, query, top_k=None, project=None):
        return self._hits


def test_ceiling_rail_note_kept():
    hits = [TaskHit(key=f"ID-{i}", title="T", status=None, score=0.03, aliases=[])
            for i in range(12)]
    assert "показано 8 из 12 (рельса ceiling)" in _Svc(hits).search_tasks("q")


def test_render_format_unchanged():
    svc = _Svc([TaskHit(key="ID-1", title="Заголовок", status="done", score=0.0321,
                        aliases=["PRI-1"])])
    assert svc.search_tasks("q") == "1. ID-1 [done] Заголовок (score 0.0321)"


def test_empty_hits_and_unavailable_source_differ():
    assert _Svc([]).search_tasks("q") == "(no similar tasks found)"
    assert _Svc(None).search_tasks("q") == "(task search unavailable)"
```

- [ ] **Step 8: Прогнать тесты и линт**

Run: `.venv/bin/pytest tests/retrieval/test_augment.py tests/tasks -q && .venv/bin/ruff check reviewer/retrieval/augment.py reviewer/web/history.py reviewer/tasks/store.py reviewer/tasks/service.py tests/tasks/test_search_hits.py`
Expected: PASS, линт чист

- [ ] **Step 9: Прогнать весь unit-набор (регрессия рендера задач)**

Run: `.venv/bin/pytest -q`
Expected: PASS

- [ ] **Step 10: Коммит**

```bash
git add reviewer/retrieval/augment.py reviewer/web/history.py reviewer/tasks/store.py reviewer/tasks/service.py tests/retrieval/test_augment.py tests/tasks/test_search_hits.py
git commit -m "feat(tasks): структурные хиты похожих задач и чтение их diff-путей из истории"
```

---

### Task 3: Третий источник кандидатов в пайплайне ретрива

**Files:**
- Modify: `reviewer/index/store.py` (новый метод рядом с `fetch_nodes`, строка 519)
- Modify: `reviewer/policy/context_limits.py:30-56` (`CodeSectionLimits`), `:92-98` (`from_review_yaml`)
- Modify: `reviewer/retrieval/retriever.py:87-119` (`ContextPack`)
- Modify: `reviewer/retrieval/multiquery.py:145-185` (`search_multi`)
- Test: `tests/retrieval/test_multiquery.py` (дописать), `tests/policy/test_context_limits.py` (дописать)

**Interfaces:**
- Consumes: `AugmentResult`, `rank_cochanged`, `COCHANGE_COMMITS` из Task 1.
- Produces:
  - `IndexStore.fetch_retrieved_at_paths(repo, paths, *, base_ref, limit_per_path=1) -> list[Retrieved]`
  - `CodeSectionLimits.max_augmented_files: int = 3`
  - `ContextPack.augment_note: str | None = None`
  - `search_multi(retriever, repo, queries, *, limits=None, section_limits=None, hops=1, branch="", include_tests=False, augment_paths=None, cochange=None)`
    где `cochange: Callable[[list[str]], list[str]] | None`.

- [ ] **Step 1: Написать падающие тесты квоты, приоритета и ноты**

Дописать в `tests/retrieval/test_multiquery.py` (фикстуры `_hit`, `_bm25`, `_FakeStore`, `_FakeEmbedder`, `_Retriever` уже есть в файле; `_FakeStore` дополнить методом ниже в Step 3):

```python
def test_augmented_paths_appended_after_hybrid_and_capped_by_quota():
    store = _FakeStore(
        {"q0": [_bm25("a.py#f")]},
        nodes_by_path={
            "x.py": _hit("x.py#s"), "y.py": _hit("y.py#s"), "z.py": _hit("z.py#s"),
        },
    )
    pack = search_multi(
        _Retriever(store, _FakeEmbedder()), "o/n", ["q0"], limits=CodebaseLimits(),
        section_limits=CodeSectionLimits(max_augmented_files=2), branch="dev",
        augment_paths=["x.py", "y.py", "z.py"])
    paths = [it.path for it in pack.items]
    assert paths[0] == "a.py", "гибрид остаётся первым"
    assert paths[1:] == ["x.py", "y.py"], "квота режет третий подмешанный файл"


def test_augmented_do_not_displace_hybrid_when_budget_is_full():
    hits = [_bm25(f"f{i}.py#s") for i in range(12)]
    store = _FakeStore({"q0": hits}, nodes_by_path={"x.py": _hit("x.py#s")})
    pack = search_multi(
        _Retriever(store, _FakeEmbedder()), "o/n", ["q0"], limits=CodebaseLimits(),
        section_limits=CodeSectionLimits(), branch="dev", augment_paths=["x.py"])
    assert "x.py" not in {it.path for it in pack.items}, \
        "при полном бюджете max_files подмешанные вытесняются гибридом"


def test_augment_note_reports_sources_and_quota():
    store = _FakeStore({"q0": [_bm25("a.py#f")]},
                       nodes_by_path={"x.py": _hit("x.py#s"), "c.py": _hit("c.py#s")})
    pack = search_multi(
        _Retriever(store, _FakeEmbedder()), "o/n", ["q0"], limits=CodebaseLimits(),
        section_limits=CodeSectionLimits(max_augmented_files=3), branch="dev",
        augment_paths=["x.py"], cochange=lambda seeds: ["c.py"])
    context = pack.as_context()
    assert "подмешано 2" in context
    assert "similar-diffs 1" in context and "co-change 1" in context


def test_cochange_receives_hybrid_paths_as_seeds():
    seen: list = []
    store = _FakeStore({"q0": [_bm25("a.py#f")]}, nodes_by_path={"c.py": _hit("c.py#s")})
    search_multi(_Retriever(store, _FakeEmbedder()), "o/n", ["q0"],
                 limits=CodebaseLimits(), branch="dev",
                 cochange=lambda seeds: seen.append(list(seeds)) or ["c.py"])
    assert seen == [["a.py"]], "seeds co-change — пути гибридной выдачи"


def test_augment_failure_is_fail_soft():
    def boom(seeds):
        raise RuntimeError("git недоступен")

    store = _FakeStore({"q0": [_bm25("a.py#f")]})
    pack = search_multi(_Retriever(store, _FakeEmbedder()), "o/n", ["q0"],
                        limits=CodebaseLimits(), branch="dev", cochange=boom)
    assert [it.path for it in pack.items] == ["a.py"]
    assert pack.augment_note is None


def test_no_augmentation_leaves_note_absent():
    store = _FakeStore({"q0": [_bm25("a.py#f")]})
    pack = search_multi(_Retriever(store, _FakeEmbedder()), "o/n", ["q0"],
                        limits=CodebaseLimits(), branch="dev")
    assert pack.augment_note is None
    assert "подмешано" not in pack.as_context()
```

Дописать в `tests/policy/test_context_limits.py`:

```python
def test_code_section_augmented_quota_default_and_override():
    from reviewer.policy.context_limits import CodeSectionLimits, ContextLimits
    assert CodeSectionLimits().max_augmented_files == 3
    limits = ContextLimits.from_review_yaml(
        {"context_limits": {"code_section": {"max_augmented_files": 5}}})
    assert limits.code_section.max_augmented_files == 5
    assert limits.code_section.max_files == 12, "прочие ключи остаются дефолтными"
```

- [ ] **Step 2: Запустить, убедиться что падают**

Run: `.venv/bin/pytest tests/retrieval/test_multiquery.py tests/policy/test_context_limits.py -q`
Expected: FAIL — `TypeError: search_multi() got an unexpected keyword argument 'augment_paths'` и `TypeError: CodeSectionLimits() got an unexpected keyword argument 'max_augmented_files'`

- [ ] **Step 3: Дополнить `_FakeStore` в тестах методом выборки по путям**

В классе `_FakeStore` (файл `tests/retrieval/test_multiquery.py`) добавить приём `nodes_by_path` в `__init__` и метод:

```python
    def fetch_retrieved_at_paths(self, repo, paths, *, base_ref, limit_per_path=1):
        return [self._nodes_by_path[p] for p in paths if p in self._nodes_by_path]
```

- [ ] **Step 4: Добавить квоту в политику**

`reviewer/policy/context_limits.py` — поле в `CodeSectionLimits` (после `chars_per_file`):

```python
    max_augmented_files: int = 3  # сколько файлов секции может занять подмешанный сигнал (PRI-257)
```

и в `from_review_yaml`, в конструктор `CodeSectionLimits`:

```python
                max_augmented_files=int(
                    cs.get("max_augmented_files", CodeSectionLimits.max_augmented_files)),
```

- [ ] **Step 5: Добавить выборку чанков по путям в стор**

`reviewer/index/store.py`, сразу после `fetch_nodes` (строка 538):

```python
    def fetch_retrieved_at_paths(self, repo, paths, *, base_ref="base",
                                 limit_per_path: int = 1):
        """Чанки base-индекса по ПУТЯМ (а не по node_id) — вход подмешанных путей.

        Симметричен fetch_nodes, но ключ — путь: сигнал PRI-257 знает файл, а не
        символ. На путь отдаётся limit_per_path самых широких чанков: ниже по
        потоку _dedupe_overlapping оставляет охватывающий символ, и узкий метод
        вместо класса обеднил бы выдачу ещё до дедупа.
        """
        if not paths:
            return []
        sql = """
        SELECT path, symbol_fqn, kind, start_line, end_line, text FROM (
            SELECT c.*, ROW_NUMBER() OVER (
                PARTITION BY c.path ORDER BY (c.end_line - c.start_line) DESC, c.start_line
            ) AS rn
            FROM chunks c
            WHERE c.repo=%(repo)s AND c.ref=%(base)s AND c.path = ANY(%(paths)s)
        ) ranked
        WHERE rn <= %(per_path)s
        """
        params = {"repo": repo, "base": base_ref, "paths": list(paths),
                  "per_path": limit_per_path}
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Retrieved(node_id=f"{p}#{f}", path=p, symbol_fqn=f, kind=k,
                          start_line=sl, end_line=el, text=t, score=0.0)
                for (p, f, k, sl, el, t) in rows]
```

- [ ] **Step 6: Добавить ноту в `ContextPack`**

`reviewer/retrieval/retriever.py` — поле dataclass после `degraded_reason` (строка 92):

```python
    augment_note: str | None = None   # PRI-257: сколько файлов подмешано и откуда
```

и печать в `as_context`, сразу после блока `degraded_note` (строка 118):

```python
        if self.augment_note:
            text = f"{text}\n\n{self.augment_note}" if text else self.augment_note
```

- [ ] **Step 7: Провести третий источник через `search_multi`**

`reviewer/retrieval/multiquery.py` — новая приватная функция перед `search_multi`:

```python
def _augment_items(retriever, repo: str, merged: list, *, augment_paths, cochange,
                   quota: int, bref: str, known_paths: set) -> tuple[list, str | None]:
    """Подмешанные кандидаты: пути похожих задач и co-change. Fail-soft.

    Квота общая на оба источника, приоритет — similar-diffs: путь, который
    задача уже реально правила, сильнее статистики со-изменяемости.
    """
    if quota <= 0:
        return [], None
    counts = {"similar-diffs": 0, "co-change": 0}
    ordered: dict[str, str] = {}
    for path in augment_paths or []:
        if path not in known_paths and len(ordered) < quota:
            ordered.setdefault(path, "similar-diffs")
    if cochange is not None and len(ordered) < quota:
        try:
            seeds = list(dict.fromkeys(item.path for item in merged))
            for path in cochange(seeds):
                if path not in known_paths and path not in ordered and len(ordered) < quota:
                    ordered[path] = "co-change"
        except Exception:  # noqa: BLE001 — git или история недоступны, это штатный случай
            log.warning("multiquery: co-change недоступен", exc_info=True)
    if not ordered:
        return [], None
    try:
        fetched = {item.path: item for item in retriever.store.fetch_retrieved_at_paths(
            repo, list(ordered), base_ref=bref)}
    except Exception:  # noqa: BLE001
        log.warning("multiquery: выборка подмешанных путей недоступна", exc_info=True)
        return [], None
    items = []
    for path, source in ordered.items():
        if path in fetched:
            items.append(fetched[path])
            counts[source] += 1
    if not items:
        return [], None
    note = (f"— подмешано {len(items)} файлов: "
            f"similar-diffs {counts['similar-diffs']}, "
            f"co-change {counts['co-change']} (квота {quota})")
    return items, note
```

и изменения в самом `search_multi`: расширить сигнатуру двумя kwargs (`augment_paths=None, cochange=None`), после сборки `items` из hybrid+graph вставить:

```python
    augmented, note = _augment_items(
        retriever, repo, merged, augment_paths=augment_paths, cochange=cochange,
        quota=sec.max_augmented_files, bref=bref,
        known_paths={item.path for item in items})
    items = [*items, *augmented]
```

(строка должна стоять ДО фильтра тестов и `_dedupe_overlapping`), а в конструктор результата добавить `augment_note=note`.

Дополнить докстринг `search_multi` абзацем:

```
    Третий источник (PRI-257) — подмешанные пути: фактические диффы похожих
    задач и git-со-изменяемость. Идёт последним, поэтому при полном файловом
    бюджете вытесняется гибридом естественно; квота max_augmented_files
    страхует обратный случай — бедную гибридную выдачу.
```

- [ ] **Step 8: Прогнать тесты пайплайна**

Run: `.venv/bin/pytest tests/retrieval tests/policy -q`
Expected: PASS (включая существующие тесты порядка dedupe→diversify)

- [ ] **Step 9: Прогнать весь unit-набор и линт**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check reviewer/retrieval/multiquery.py reviewer/retrieval/retriever.py reviewer/index/store.py reviewer/policy/context_limits.py tests/retrieval/test_multiquery.py tests/policy/test_context_limits.py`
Expected: PASS, линт чист

- [ ] **Step 10: Коммит**

```bash
git add reviewer/retrieval/multiquery.py reviewer/retrieval/retriever.py reviewer/index/store.py reviewer/policy/context_limits.py tests/retrieval/test_multiquery.py tests/policy/test_context_limits.py
git commit -m "feat(retrieval): третий источник кандидатов секции code с квотой и нотой"
```

---

### Task 4: Проводка сигналов в prepare_task_context

**Files:**
- Modify: `reviewer/mcp/service.py:1797-1819` (`_search_codebase_multi`), `:3504-3560` (`_TaskContextDeps`)
- Modify: `reviewer/mcp/task_context.py:100-112` (порядок и gaps)
- Test: `tests/mcp/test_prepare_task_context.py` (дописать), `tests/mcp/test_context_limits_wiring.py` (дописать)

**Interfaces:**
- Consumes: `collect_similar_task_paths`, `rank_cochanged`, `COCHANGE_COMMITS` (Task 1-2); `search_multi(..., augment_paths, cochange)` (Task 3); `TaskService.search_hits` (Task 2).
- Produces: `_TaskContextDeps.code(repo, branch, queries)` подмешивает пути; payload получает `gap("code.augment", …)` при сбое источника.

- [ ] **Step 1: Написать падающие тесты проводки**

Дописать в `tests/mcp/test_prepare_task_context.py` (стиль фейкового `deps` уже задан в файле):

Существующий `FakeDeps` (строки 5-47 файла) уже пишет каждый вызов в `self.calls` — этого хватает, новой фикстуры не нужно:

```python
def test_similar_runs_before_code_so_hits_are_available():
    """Порядок вызовов — контракт: ключи секции code берутся из хитов similar."""
    deps = FakeDeps()
    task_context.build_task_context(deps, repo="o/n", key="ID-311", branch="dev",
                                    warm_board=False)
    assert deps.calls.index("similar") < deps.calls.index("code")


def test_augment_gaps_are_copied_into_payload():
    deps = FakeDeps()
    deps.augment_gaps = ["git-история недоступна: CalledProcessError"]
    payload = task_context.build_task_context(deps, repo="o/n", key="ID-311",
                                              branch="dev", warm_board=False)
    assert payload["code"], "сбой подмешивания не обнуляет секцию"
    assert {"section": "code.augment",
            "reason": "git-история недоступна: CalledProcessError"} in payload["gaps"]


def test_deps_without_augment_gaps_attribute_still_work():
    """Старый провайдер секций (без нового поля) не должен падать."""
    payload = task_context.build_task_context(FakeDeps(), repo="o/n", key="ID-311",
                                              branch="dev", warm_board=False)
    assert payload["gaps"] == []
```

- [ ] **Step 2: Запустить, убедиться что падают**

Run: `.venv/bin/pytest tests/mcp/test_prepare_task_context.py -q`
Expected: FAIL — фикстуры/атрибутов нет

- [ ] **Step 3: Расширить `_search_codebase_multi`**

`reviewer/mcp/service.py` — прокинуть новые параметры насквозь:

```python
    def _search_codebase_multi(self, repo: str, queries: list[str],
                               branch: str | None = None,
                               include_tests: bool = False,
                               augment_paths: list[str] | None = None,
                               cochange=None) -> str:
```

и в вызове `search_multi(...)` добавить `augment_paths=augment_paths, cochange=cochange`.

- [ ] **Step 4: Собрать вход сигналов в `_TaskContextDeps`**

`reviewer/mcp/service.py`, класс `_TaskContextDeps`: в `__init__` добавить `self._similar_hits: list = []` и `self.augment_gaps: list[str] = []`; переписать `similar` и `code`:

```python
    def similar(self, query: str, project: str | None) -> str:
        """Похожие задачи. Побочно запоминает структурные хиты для секции code.

        Хиты берутся из ОДНОГО поиска: второй вызов означал бы второй
        embed_query и лишний расход квоты Voyage (3 RPM / 10K TPM). Порядок
        вызовов (similar до code) закреплён тестом build_task_context.
        """
        service = self._service.components.task_service
        hits = service.search_hits(query, project=project)
        self._similar_hits = list(hits or [])
        return service.render_hits(hits)

    def _augment_paths(self, repo: str) -> list[str]:
        """Фактические diff-пути похожих задач. Пробелы копятся в augment_gaps."""
        from reviewer.retrieval.augment import collect_similar_task_paths
        if not self._similar_hits:
            return []
        keys = [hit.key for hit in self._similar_hits]
        aliases = {hit.key: list(getattr(hit, "aliases", []) or [])
                   for hit in self._similar_hits}
        history = None
        try:
            history = self._service._review_service._ensure_history()
        except Exception as exc:  # noqa: BLE001
            self.augment_gaps.append(f"история прогонов недоступна: {type(exc).__name__}")
        result = collect_similar_task_paths(
            keys=keys, aliases_by_key=aliases, history=history,
            clone_path=self._clone_path(repo), limit=AUGMENT_LOOKUP_LIMIT)
        self.augment_gaps.extend(result.gaps)
        return result.paths

    def _cochange(self, repo: str):
        """Callable seeds → co-change пути; None, если клона нет."""
        from reviewer.retrieval.augment import (
            COCHANGE_COMMITS, MIN_COCHANGE, rank_cochanged,
        )
        from reviewer import gitutil
        clone = self._clone_path(repo)
        if not clone:
            return None

        def _fn(seeds: list[str]) -> list[str]:
            commits = gitutil.commit_file_sets(clone, limit=COCHANGE_COMMITS)
            return rank_cochanged(commits, set(seeds), min_count=MIN_COCHANGE,
                                  limit=AUGMENT_LOOKUP_LIMIT)

        return _fn

    def code(self, repo: str, branch: str, queries: list) -> str:
        return self._service._search_codebase_multi(
            repo, queries, branch, False,
            augment_paths=self._augment_paths(repo), cochange=self._cochange(repo))
```

Модульная константа рядом с классом:

```python
AUGMENT_LOOKUP_LIMIT = 20
"""Сколько путей источник отдаёт ДО квоты: квота режет позже, в search_multi."""
```

`self._service.components.task_service` — тот же объект, через который ходит `MCPReviewService.search_tasks` (`service.py:504`); нового способа доступа не заводится. Требование неизменности: `payload.related.similar` обязан остаться байт-в-байт тем же текстом, что и до задачи — это проверяется тестом Step 7.

Секция `test_exemplars` подмешивания НЕ получает: её `deps.test_exemplars` вызывает `_search_codebase_multi` без новых параметров (сигнал про diff-пути кода, тестовые образцы он бы засорял).

- [ ] **Step 5: Перенести пробелы в payload**

`reviewer/mcp/task_context.py` — после сборки секции `code` (после строки 112) добавить:

```python
    for reason in getattr(deps, "augment_gaps", []) or []:
        payload["gaps"].append(gap("code.augment", reason))
```

- [ ] **Step 6: Прогнать тесты MCP**

Run: `.venv/bin/pytest tests/mcp -q`
Expected: PASS

- [ ] **Step 7: Тест неизменности рендера `related.similar`**

Дописать в `tests/mcp/test_prepare_task_context.py`:

```python
def test_similar_section_text_is_unchanged_by_augmentation(fake_deps_factory):
    deps = fake_deps_factory()
    payload = build_task_context(deps, repo="o/n", key="ID-311", branch="dev",
                                 warm_board=False)
    assert payload["related"]["similar"] == deps.expected_similar_text
```

Run: `.venv/bin/pytest tests/mcp/test_prepare_task_context.py -q`
Expected: PASS

- [ ] **Step 8: Полный unit-набор и линт**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check reviewer/mcp/service.py reviewer/mcp/task_context.py tests/mcp/test_prepare_task_context.py`
Expected: PASS, линт чист

- [ ] **Step 9: Коммит**

```bash
git add reviewer/mcp/service.py reviewer/mcp/task_context.py tests/mcp/test_prepare_task_context.py tests/mcp/test_context_limits_wiring.py
git commit -m "feat(mcp): подмешивание diff-путей похожих задач и co-change в секцию code"
```

---

### Task 5: Варианты replay и замер дельты

**Files:**
- Modify: `eval/solve_task_metrics/variants.py:60-75` (реестр)
- Modify: `eval/solve_task_metrics/live.py:126-145` (`code_multi`)
- Modify: `eval/replay_report.md` (раздел приёмки)
- Test: `tests/eval/test_variants.py` (дописать), `tests/eval/test_live_boundary.py` (дописать)

**Interfaces:**
- Consumes: всё из Task 1-4.
- Produces: имена вариантов `similar_paths`, `cochange`, `augmented` в `VARIANT_NAMES`.

- [ ] **Step 1: Написать падающий тест реестра**

Дописать в `tests/eval/test_variants.py`:

```python
def test_augment_variants_registered():
    from eval.solve_task_metrics.variants import VARIANT_NAMES, get_variant
    assert {"similar_paths", "cochange", "augmented"} <= set(VARIANT_NAMES)
    for name in ("similar_paths", "cochange", "augmented"):
        assert callable(get_variant(name))
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `.venv/bin/pytest tests/eval/test_variants.py -q`
Expected: FAIL — `UnknownVariant`

- [ ] **Step 3: Добавить варианты в реестр**

`eval/solve_task_metrics/variants.py`, после `_multiquery`:

```python
def _augmented(provider, task: TaskInput, target: ReplayTarget, *,
               similar: bool, cochange: bool) -> set:
    """Мультизапрос плюс подмешанные путевые сигналы (PRI-257).

    Рычаги включаются раздельно, потому что критерий приёмки требует мерить
    дельту каждого: включённые вместе они дали бы одно число на два решения.
    """
    queries = build_subqueries(task.task, task.query)
    text = provider.code_multi(target.repo, target.branch, queries, target.limits,
                               similar_paths=similar, cochange=cochange)
    return extract_context_paths(text)


def _similar_paths(provider, task: TaskInput, target: ReplayTarget) -> set:
    """Только diff-пути похожих задач."""
    return _augmented(provider, task, target, similar=True, cochange=False)


def _cochange(provider, task: TaskInput, target: ReplayTarget) -> set:
    """Только git-со-изменяемость."""
    return _augmented(provider, task, target, similar=False, cochange=True)


def _both(provider, task: TaskInput, target: ReplayTarget) -> set:
    """Оба сигнала вместе — верхняя оценка совокупного эффекта."""
    return _augmented(provider, task, target, similar=True, cochange=True)
```

и реестр:

```python
_REGISTRY = {
    "baseline": _baseline,
    "limits": _limits,
    "multiquery": _multiquery,
    "similar_paths": _similar_paths,
    "cochange": _cochange,
    "augmented": _both,
}
```

- [ ] **Step 4: Прокинуть флаги в `live.py`**

`eval/solve_task_metrics/live.py`, метод `code_multi` — расширить сигнатуру и собрать вход тем же способом, что прод:

```python
    def code_multi(self, repo: str, branch: str, queries: list, limits: dict | None,
                   *, similar_paths: bool = False, cochange: bool = False) -> str:
        """Мультизапросная выдача тем же продакшн-путём, что видит сборщик брифа.

        Подмешанные сигналы (PRI-257) собираются продакшн-объектом
        _TaskContextDeps: своей копии сборки путей здесь не заводится, иначе
        replay мерил бы не тот вход, что видит прод.
        """
```

Тело метода — существующая ветка «без оверрайдов / с оверрайдами» плюс сбор сигналов продакшн-объектом:

```python
        augment, co = None, None
        if similar_paths or cochange:
            from reviewer.mcp.service import _TaskContextDeps
            deps = _TaskContextDeps(self._service, None)
            if similar_paths:
                # Хиты похожих задач наполняются вызовом similar; первый подзапрос —
                # это и есть продакшн-запрос задачи целиком (см. _queries).
                deps.similar(queries[0], None)
                augment = deps._augment_paths(repo)
            if cochange:
                co = deps._cochange(repo)
        if not limits:
            return self._service._search_codebase_multi(
                repo, list(queries), branch, False,
                augment_paths=augment, cochange=co)
```

и в ветке с оверрайдами передать те же `augment_paths=augment, cochange=co` в прямой вызов `search_multi(...)` рядом с `section_limits=effective.code_section`.

- [ ] **Step 5: Дописать тест границы live**

В `tests/eval/test_live_boundary.py` — добавить проверку, что `code_multi` без флагов зовёт ровно продакшн-метод (существующий тест границы расширить новыми аргументами по умолчанию).

- [ ] **Step 6: Прогнать eval-тесты, полный набор и линт**

Run: `.venv/bin/pytest tests/eval -q && .venv/bin/pytest -q && .venv/bin/ruff check eval/solve_task_metrics/variants.py eval/solve_task_metrics/live.py tests/eval/test_variants.py tests/eval/test_live_boundary.py`
Expected: PASS, линт чист

- [ ] **Step 7: Коммит**

```bash
git add eval/solve_task_metrics/variants.py eval/solve_task_metrics/live.py tests/eval/test_variants.py tests/eval/test_live_boundary.py
git commit -m "feat(eval): варианты replay для рычагов similar-paths и co-change"
```

- [ ] **Step 8: Замерить дельту (требует живых Postgres/Neo4j и квоты Voyage)**

Прогнать replay на одном `indexed_sha` для baseline (`multiquery`) и трёх новых вариантов, дописать раздел «Приёмка PRI-257» в `eval/replay_report.md` с медианой core-recall, bulk core-recall, precision и числом файлов до/после — по образцу разделов «Приёмка PRI-255» и «Приёмка PRI-256» в том же файле.

**Гейт критерия 1:** рычаг, чья дельта bulk core-recall не покрывает шум, в PR не входит — его запись реестра остаётся, а включение в `_TaskContextDeps.code` снимается. Решение по каждому рычагу записать в раздел отчёта явно.

- [ ] **Step 9: Коммит отчёта**

```bash
git add eval/replay_report.md eval/replay_history.jsonl
git commit -m "docs(eval): замер дельты рычагов PRI-257"
```

---

### Task 6: Документация

**Files:**
- Modify: `CLAUDE.md` (раздел «Неочевидные факты»)
- Modify: `README.md`, `README.ru.md`
- Modify: `plugin/skills/configure-review/SKILL.md` (профиль retrieval-лимитов)
- Test: `tests/skills/test_configure_review_skill.py` (дописать)

**Interfaces:**
- Consumes: финальное поведение из Task 1-5 (включая решение гейта Task 5, Step 8).
- Produces: документированный ключ `context_limits.code_section.max_augmented_files`.

- [ ] **Step 1: Дописать факт в `CLAUDE.md`**

Добавить абзац в «Неочевидные факты» после абзаца PRI-256, зафиксировав: два источника и их асимметрию (таблица точнее, git-фолбэк покрывает пустую историю); почему квота общая и почему приоритет у similar-diffs; что глубина истории — модульная константа, а не ключ политики; что `test_exemplars` подмешивания не получает; что нота — единственный способ увидеть долю подмешанных.

- [ ] **Step 2: Обновить оба README**

В `README.md` и `README.ru.md` — синхронно, в разделе про контекст задачи / лимиты: упомянуть новый ключ `context_limits.code_section.max_augmented_files` и два источника кандидатов.

- [ ] **Step 3: Дописать ключ в профиль скилла configure-review**

`plugin/skills/configure-review/SKILL.md` — в профиль retrieval-лимитов добавить `max_augmented_files` рядом с `max_files`/`max_chunks_per_file`/`chars_per_file` (там же, где их добавил PRI-256).

- [ ] **Step 4: Дописать guard-тест**

`tests/skills/test_configure_review_skill.py` — добавить `max_augmented_files` в проверку присутствия ключей профиля (по образцу существующих ассертов).

- [ ] **Step 5: Пересобрать манифесты плагина**

Правка контента под `plugin/` меняет payload-digest — иначе install-тесты покраснеют.

Run: `.venv/bin/python scripts/update_codex_plugin_manifest.py`
Затем: `.venv/bin/pytest -q`
Expected: PASS

- [ ] **Step 6: Коммит**

```bash
git add CLAUDE.md README.md README.ru.md plugin/skills/configure-review/SKILL.md tests/skills/test_configure_review_skill.py .codex-plugin/plugin.json plugin/.codex-plugin/plugin.json
git commit -m "docs(pri-257): подмешанные путевые сигналы секции code"
```

---

## Финал

- [ ] Полный прогон: `.venv/bin/pytest -q`
- [ ] Integration (требует поднятой тестовой инфраструктуры): `docker compose --profile test up -d --wait paradedb-test neo4j-test && .venv/bin/pytest -q -m integration`, затем `docker compose --profile test rm -sfv paradedb-test neo4j-test`
- [ ] `git push` — **только после явного подтверждения пользователя**
- [ ] Создание PR в `dev` — **только после явного подтверждения пользователя**
- [ ] Закрытие задачи на доске (`rag-reviewer:finish-task`) — **только после явного подтверждения пользователя**
