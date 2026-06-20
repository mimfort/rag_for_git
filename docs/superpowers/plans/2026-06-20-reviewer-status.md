# reviewer status — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить CLI-команду `reviewer status [PATH] [--repo] [--branch]`, показывающую по веткам здоровье/свежесть base-индекса (last-indexed SHA, дрейф vs git, чанки, узлы графа, бэкенд, overlay), не тратя Voyage.

**Architecture:** Тонкий чистый билдер `build_status_report` в `reviewer/services/status.py` собирает данные из `ChunkStore` (Postgres) + `GraphStore` (Neo4j) + git (`gitutil`) в датакласс `RepoStatus`; чистый форматтер `render_status` печатает его; команда в `cli.py` создаёт сторы напрямую (как `check`, без эмбеддера → без Voyage) и рендерит. Все новые методы сторов — аддитивные и read-only.

**Tech Stack:** Python 3.11, Click (CLI), psycopg/psycopg_pool (Postgres), neo4j-драйвер, pytest (unit + `integration`-маркер), ruff.

## Global Constraints

- Язык проекта — **русский**: комментарии, докстринги, сообщения CLI.
- Внешние сервисы изолированы за сторами; unit-тесты — на фейках, реальные вызовы только в `integration`.
- `pytest` по умолчанию исключает `integration` (`addopts = -m 'not integration'`); тесты, требующие живых Postgres/Neo4j, помечать `@pytest.mark.integration`.
- ruff: line-length 100, target py311.
- Коммиты: Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By`/упоминаний Claude).
- Команда **не должна тратить Voyage**: не создавать эмбеддер / `build_components`; сторы строить напрямую.
- Существующий `get_index_meta` и логику `prepare_review` НЕ трогать.

---

## File Structure

- **Create** `reviewer/services/status.py` — датаклассы `BranchStatus`/`OverlayStatus`/`RepoStatus`, билдер `build_status_report`, форматтер `render_status`.
- **Modify** `reviewer/gitutil.py` — добавить `commits_behind`.
- **Modify** `reviewer/index/store.py` — добавить `get_index_meta_row`, `count_chunks`, `list_refs`.
- **Modify** `reviewer/graph/store.py` — добавить `count_nodes`.
- **Modify** `reviewer/entrypoints/cli.py` — добавить команду `status` + импорт билдера/форматтера.
- **Create** `tests/services/test_status.py` — unit-тесты билдера, форматтера, smoke CLI.
- **Modify** `tests/test_gitutil.py` — тест `commits_behind`.
- **Create** `tests/index/test_status_meta.py` — integration-тест `count_chunks`/`list_refs`/`get_index_meta_row`.
- **Create** `tests/graph/test_count_nodes.py` — integration-тест `count_nodes`.
- **Modify** `README.md`, `CLAUDE.md` — упомянуть `reviewer status` в списке CLI.

---

### Task 1: `gitutil.commits_behind` — дрейф индекса vs git

**Files:**
- Modify: `reviewer/gitutil.py` (добавить функцию после `rev_parse`, ~строка 28)
- Test: `tests/test_gitutil.py`

**Interfaces:**
- Consumes: существующий `_git(repo, *args)` (запускает `git -C repo …`, `check=True`).
- Produces: `commits_behind(repo: str, sha: str, ref: str) -> int | None` — число коммитов в `ref`, отсутствующих в `sha` (`git rev-list --count <sha>..<ref>`); `None` при любом сбое git (не репо / недостижимый sha|ref).

- [ ] **Step 1: Написать падающий тест**

В `tests/test_gitutil.py` добавить импорт `commits_behind` в существующую строку импорта и тест:

```python
def test_commits_behind(tmp_path):
    r = tmp_path
    _run("git", "init", "-q", cwd=r)
    _run("git", "config", "user.email", "t@t", "--local", cwd=r)
    _run("git", "config", "user.name", "t", "--local", cwd=r)
    (r / "a.py").write_text("x=1\n")
    _run("git", "add", "-A", cwd=r); _run("git", "commit", "-qm", "c1", cwd=r)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=r,
                         capture_output=True, text=True).stdout.strip()
    assert commits_behind(str(r), sha, "HEAD") == 0
    (r / "a.py").write_text("x=2\n"); _run("git", "add", "-A", cwd=r)
    _run("git", "commit", "-qm", "c2", cwd=r)
    (r / "a.py").write_text("x=3\n"); _run("git", "add", "-A", cwd=r)
    _run("git", "commit", "-qm", "c3", cwd=r)
    assert commits_behind(str(r), sha, "HEAD") == 2
    assert commits_behind(str(r), "0" * 40, "HEAD") is None        # мусорный sha
    assert commits_behind(str(tmp_path / "nope"), sha, "HEAD") is None  # не git-репо
```

Обновить строку импорта вверху файла на:
```python
from reviewer.gitutil import changed_files, file_at_ref, remote_url, commits_behind
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/test_gitutil.py::test_commits_behind -q`
Expected: FAIL — `ImportError: cannot import name 'commits_behind'`.

- [ ] **Step 3: Реализовать `commits_behind`**

В `reviewer/gitutil.py` после `rev_parse` (после строки 27) добавить:

```python
def commits_behind(repo: str, sha: str, ref: str) -> int | None:
    """На сколько коммитов ``ref`` опережает ``sha`` (`git rev-list --count <sha>..<ref>`).

    Возвращает None, если ``repo`` не git-репо либо ``sha``/``ref`` недостижимы —
    дрейф просто считается «неизвестным» (fail-soft, без падения вызывающего)."""
    try:
        out = _git(repo, "rev-list", "--count", f"{sha}..{ref}")
    except subprocess.CalledProcessError:
        return None
    return int(out.strip())
```

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/test_gitutil.py::test_commits_behind -q`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add reviewer/gitutil.py tests/test_gitutil.py
git commit -m "feat(gitutil): commits_behind — дрейф SHA vs git-ref (fail-soft)"
```

---

### Task 2: read-only методы `ChunkStore`

**Files:**
- Modify: `reviewer/index/store.py` (добавить методы после `set_index_meta`, ~строка 175)
- Test: `tests/index/test_status_meta.py` (create)

**Interfaces:**
- Consumes: существующий `self._connect()` (контекст-менеджер pooled-соединения); таблицы `chunks(repo, ref, …)`, `index_meta(repo, ref, sha, updated_at)`.
- Produces:
  - `get_index_meta_row(repo: str, ref: str) -> tuple[str, datetime] | None` — `(sha, updated_at)` или `None` (нет записи / нет таблицы).
  - `count_chunks(repo: str, ref: str) -> int` — число чанков в `(repo, ref)`.
  - `list_refs(repo: str) -> list[str]` — отсортированный список distinct `ref` репо.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/index/test_status_meta.py`:

```python
import pytest
from reviewer.config.settings import Settings
from reviewer.index.store import ChunkStore, ChunkRow


def _row(ref, path, fqn):
    return ChunkRow(repo="a/x", ref=ref, content_hash=fqn, path=path, lang="python",
                    symbol_fqn=fqn, kind="function", start_line=1, end_line=2,
                    text="code", embedding=[0.0] * 1024)


@pytest.mark.integration
def test_count_chunks_list_refs_meta_row():
    s = Settings()
    store = ChunkStore(s.pg_dsn)
    store.init_schema()
    store.clear("a/x")
    store.upsert([_row("base:main", "a.py", "f"),
                  _row("base:main", "b.py", "g"),
                  _row("pr:7", "a.py", "f")])
    store.set_index_meta("a/x", "base:main", "cafe1234")
    try:
        assert store.count_chunks("a/x", "base:main") == 2
        assert store.count_chunks("a/x", "pr:7") == 1
        assert store.count_chunks("a/x", "absent") == 0
        assert set(store.list_refs("a/x")) == {"base:main", "pr:7"}
        row = store.get_index_meta_row("a/x", "base:main")
        assert row is not None and row[0] == "cafe1234"
        assert store.get_index_meta_row("a/x", "base:absent") is None
    finally:
        store.clear("a/x")
        store.close()
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest -m integration tests/index/test_status_meta.py -q`
Expected: FAIL — `AttributeError: 'ChunkStore' object has no attribute 'count_chunks'`.
(Требуется поднятый Postgres: `docker compose up -d`.)

- [ ] **Step 3: Реализовать методы**

В `reviewer/index/store.py` после `set_index_meta` (после строки 175) добавить:

```python
    def get_index_meta_row(self, repo: str, ref: str) -> tuple[str, datetime] | None:
        """SHA и время последней индексации для ref, или None.

        Как get_index_meta, но возвращает ещё updated_at. Отсутствие таблицы
        (старый индекс без init_schema) равнозначно отсутствию записи."""
        import psycopg.errors
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT sha, updated_at FROM index_meta WHERE repo=%s AND ref=%s",
                    (repo, ref),
                ).fetchone()
        except psycopg.errors.UndefinedTable:
            return None
        return (row[0], row[1]) if row else None

    def count_chunks(self, repo: str, ref: str) -> int:
        """Число чанков в (repo, ref)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT count(*) FROM chunks WHERE repo=%s AND ref=%s", (repo, ref)
            ).fetchone()
        return row[0] if row else 0

    def list_refs(self, repo: str) -> list[str]:
        """Отсортированный список distinct ref репо (для поиска overlay)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT ref FROM chunks WHERE repo=%s ORDER BY ref", (repo,)
            ).fetchall()
        return [r[0] for r in rows]
```

Примечание: `store.py` начинается с `from __future__ import annotations`, поэтому аннотацию `datetime` в сигнатуре вычислять не нужно — дополнительный импорт `datetime` не требуется.

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `.venv/bin/pytest -m integration tests/index/test_status_meta.py -q`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add reviewer/index/store.py tests/index/test_status_meta.py
git commit -m "feat(index): read-only count_chunks/list_refs/get_index_meta_row для status"
```

---

### Task 3: `GraphStore.count_nodes`

**Files:**
- Modify: `reviewer/graph/store.py` (добавить метод в конец класса, после `delete_outgoing_calls`, ~строка 137)
- Test: `tests/graph/test_count_nodes.py` (create)

**Interfaces:**
- Consumes: существующий `self._driver.execute_query(cypher, **params) -> (records, _, _)`; узлы `:Symbol {repo, branch, id}`.
- Produces: `count_nodes(repo: str, branch: str = "") -> int` — число `:Symbol` в `(repo, branch)`.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/graph/test_count_nodes.py`:

```python
import pytest
from reviewer.config.settings import Settings
from reviewer.graph.store import GraphStore


@pytest.mark.integration
def test_count_nodes_by_branch():
    s = Settings()
    g = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    g.init_schema()
    g.clear("a/x", branch="main")
    g.upsert_nodes("a/x", ["m.py#a", "m.py#b"], branch="main")
    try:
        assert g.count_nodes("a/x", "main") == 2
        assert g.count_nodes("a/x", "absent") == 0
    finally:
        g.clear("a/x", branch="main")
        g.close()
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest -m integration tests/graph/test_count_nodes.py -q`
Expected: FAIL — `AttributeError: 'GraphStore' object has no attribute 'count_nodes'`.
(Требуется поднятый Neo4j: `docker compose up -d`.)

- [ ] **Step 3: Реализовать `count_nodes`**

В `reviewer/graph/store.py` в конец класса `GraphStore` (после строки 137) добавить:

```python
    def count_nodes(self, repo: str, branch: str = "") -> int:
        """Число :Symbol-узлов в (repo, branch)."""
        records, _, _ = self._driver.execute_query(
            "MATCH (s:Symbol {repo: $repo, branch: $branch}) RETURN count(s) AS n",
            repo=repo, branch=branch)
        return records[0]["n"]
```

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `.venv/bin/pytest -m integration tests/graph/test_count_nodes.py -q`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add reviewer/graph/store.py tests/graph/test_count_nodes.py
git commit -m "feat(graph): count_nodes — число :Symbol по (repo, branch)"
```

---

### Task 4: билдер `build_status_report` + датаклассы

**Files:**
- Create: `reviewer/services/status.py`
- Test: `tests/services/test_status.py` (create)

**Interfaces:**
- Consumes (из Tasks 1–3): `store.get_index_meta_row(repo, ref) -> tuple[str, datetime] | None`, `store.count_chunks(repo, ref) -> int`, `store.list_refs(repo) -> list[str]`, `graph.count_nodes(repo, branch) -> int`, `gitutil.commits_behind(repo, sha, ref) -> int | None`; `reviewer.index.refs.base_ref(branch) -> str`.
- Produces: датаклассы `BranchStatus`, `OverlayStatus`, `RepoStatus`; `build_status_report(store, graph, repo: str, branches: list[str], repo_path: str) -> RepoStatus`. Поля:
  - `BranchStatus(branch, ref, indexed_sha: str|None, updated_at: datetime|None, chunks: int, graph_nodes: int|None, drift: int|None)`
  - `OverlayStatus(ref: str, chunks: int)`
  - `RepoStatus(repo: str, branches: list[BranchStatus], overlays: list[OverlayStatus])`

- [ ] **Step 1: Написать падающий тест**

Создать `tests/services/test_status.py`:

```python
from datetime import datetime

import reviewer.services.status as status_mod
from reviewer.services.status import build_status_report, OverlayStatus


class FakeStore:
    def __init__(self, meta, chunks, refs):
        self._meta, self._chunks, self._refs = meta, chunks, refs

    def get_index_meta_row(self, repo, ref):
        return self._meta.get(ref)

    def count_chunks(self, repo, ref):
        return self._chunks.get(ref, 0)

    def list_refs(self, repo):
        return list(self._refs)


class FakeGraph:
    def __init__(self, nodes, fail=False):
        self._nodes, self._fail = nodes, fail

    def count_nodes(self, repo, branch):
        if self._fail:
            raise RuntimeError("neo4j down")
        return self._nodes.get(branch, 0)


def test_build_status_report_fresh_and_behind(monkeypatch):
    dt = datetime(2026, 6, 18, 14, 2)
    store = FakeStore(
        meta={"base:main": ("abc1234", dt), "base:dev": ("def5678", dt)},
        chunks={"base:main": 1843, "base:dev": 1850, "pr:24": 18},
        refs=["base:main", "base:dev", "pr:24"])
    graph = FakeGraph(nodes={"main": 1207, "dev": 1190})
    drifts = {"main": 0, "dev": 12}
    monkeypatch.setattr(status_mod, "commits_behind",
                        lambda path, sha, ref: drifts.get(ref))
    rep = build_status_report(store, graph, "a/x", ["main", "dev"], "/tmp/repo")
    assert rep.branches[0].drift == 0 and rep.branches[0].graph_nodes == 1207
    assert rep.branches[0].indexed_sha == "abc1234"
    assert rep.branches[1].drift == 12
    assert rep.overlays == [OverlayStatus(ref="pr:24", chunks=18)]


def test_build_status_report_not_indexed_and_neo4j_down(monkeypatch):
    store = FakeStore(meta={}, chunks={"base:main": 0}, refs=["base:main"])
    graph = FakeGraph(nodes={}, fail=True)
    monkeypatch.setattr(status_mod, "commits_behind", lambda *a: None)
    rep = build_status_report(store, graph, "a/x", ["main"], "/tmp/repo")
    b = rep.branches[0]
    assert b.indexed_sha is None and b.drift is None and b.graph_nodes is None
    assert rep.overlays == []  # base:main исключён из overlay
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/services/test_status.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'reviewer.services.status'`.

- [ ] **Step 3: Реализовать модуль**

Создать `reviewer/services/status.py`:

```python
"""Сбор и рендер статуса base-индекса (команда `reviewer status`).

Чистый слой без эмбеддера/Settings: данные берутся только из стора чанков,
графа и git — поэтому команда не тратит Voyage и легко тестируется на фейках.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime

from reviewer.gitutil import commits_behind
from reviewer.index.refs import base_ref


@dataclass
class BranchStatus:
    branch: str
    ref: str
    indexed_sha: str | None
    updated_at: datetime | None
    chunks: int
    graph_nodes: int | None
    drift: int | None


@dataclass
class OverlayStatus:
    ref: str
    chunks: int


@dataclass
class RepoStatus:
    repo: str
    branches: list[BranchStatus]
    overlays: list[OverlayStatus]


def _drift(repo_path: str, sha: str, branch: str) -> int | None:
    """Дрейф ветки: пробуем локальный ref, затем origin/<branch>; иначе None."""
    for cand in (branch, f"origin/{branch}"):
        n = commits_behind(repo_path, sha, cand)
        if n is not None:
            return n
    return None


def build_status_report(store, graph, repo: str, branches: list[str],
                        repo_path: str) -> RepoStatus:
    """Собрать RepoStatus по веткам. Neo4j fail-soft (graph_nodes=None при сбое)."""
    branch_statuses: list[BranchStatus] = []
    for branch in branches:
        ref = base_ref(branch)
        row = store.get_index_meta_row(repo, ref)
        sha = row[0] if row else None
        updated_at = row[1] if row else None
        chunks = store.count_chunks(repo, ref)
        try:
            graph_nodes = graph.count_nodes(repo, branch)
        except Exception:  # noqa: BLE001 — Neo4j недоступен, дрейф/счётчики прочего печатаем
            graph_nodes = None
        drift = _drift(repo_path, sha, branch) if sha else None
        branch_statuses.append(BranchStatus(
            branch=branch, ref=ref, indexed_sha=sha, updated_at=updated_at,
            chunks=chunks, graph_nodes=graph_nodes, drift=drift))
    overlays = [
        OverlayStatus(ref=r, chunks=store.count_chunks(repo, r))
        for r in store.list_refs(repo)
        if not r.startswith("base:")
    ]
    return RepoStatus(repo=repo, branches=branch_statuses, overlays=overlays)
```

Также создать пустой `tests/services/__init__.py`, если его нет (в каталоге `tests/services/` уже есть `__init__.py` — проверить; модуль `reviewer/services/` уже пакет).

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/services/test_status.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Коммит**

```bash
git add reviewer/services/status.py tests/services/test_status.py
git commit -m "feat(services): build_status_report — сбор статуса индекса по веткам"
```

---

### Task 5: форматтер `render_status`

**Files:**
- Modify: `reviewer/services/status.py` (добавить `render_status` в конец)
- Test: `tests/services/test_status.py` (дополнить)

**Interfaces:**
- Consumes: `RepoStatus` из Task 4.
- Produces: `render_status(report: RepoStatus, backend: str) -> str` — человекочитаемый многострочный текст (заканчивается `\n`).

- [ ] **Step 1: Написать падающий тест**

Дополнить `tests/services/test_status.py`:

```python
from reviewer.services.status import (
    render_status, RepoStatus, BranchStatus, OverlayStatus)


def test_render_status_shapes_output():
    dt = datetime(2026, 6, 18, 14, 2)
    rep = RepoStatus(
        repo="a/x",
        branches=[
            BranchStatus("main", "base:main", "abc1234567", dt, 1843, 1207, 0),
            BranchStatus("dev", "base:dev", "def5678901", dt, 1850, None, 12),
            BranchStatus("old", "base:old", None, None, 0, None, None),
        ],
        overlays=[OverlayStatus("pr:24", 18)])
    out = render_status(rep, "tree-sitter (fallback)")
    assert "Репозиторий: a/x" in out
    assert "✓ свежо" in out
    assert "отстаёт на 12 коммитов" in out
    assert "Neo4j недоступен" in out         # dev: graph_nodes=None
    assert "не проиндексирована" in out       # old: indexed_sha=None
    assert "pr:24   18 чанков" in out
    assert "abc1234" in out                    # короткий SHA (7 символов)
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/services/test_status.py::test_render_status_shapes_output -q`
Expected: FAIL — `ImportError: cannot import name 'render_status'`.

- [ ] **Step 3: Реализовать `render_status`**

В конец `reviewer/services/status.py` добавить:

```python
def _fmt_dt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "—"


def render_status(report: RepoStatus, backend: str) -> str:
    """Человекочитаемый отчёт по RepoStatus (для click.echo)."""
    lines = [
        f"Репозиторий: {report.repo}",
        f"Граф (бэкенд для индексации): {backend}",
        "",
    ]
    for b in report.branches:
        lines.append(f"Ветка {b.branch}   [{b.ref}]")
        if b.indexed_sha is None:
            lines.append("  SHA:    — (не проиндексирована)")
            lines.append("")
            continue
        lines.append(
            f"  SHA:    {b.indexed_sha[:7]}  (проиндексировано {_fmt_dt(b.updated_at)})")
        if b.drift is None:
            lines.append("  Статус: дрейф неизвестен (нет git-клона)")
        elif b.drift == 0:
            lines.append("  Статус: ✓ свежо")
        else:
            lines.append(f"  Статус: ↗ отстаёт на {b.drift} коммитов")
        nodes = "—  (Neo4j недоступен)" if b.graph_nodes is None else str(b.graph_nodes)
        lines.append(f"  Чанки:  {b.chunks}   Узлы графа: {nodes}")
        lines.append("")
    if report.overlays:
        lines.append("Overlay:")
        for o in report.overlays:
            lines.append(f"  {o.ref}   {o.chunks} чанков")
    return "\n".join(lines).rstrip() + "\n"
```

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/services/test_status.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Коммит**

```bash
git add reviewer/services/status.py tests/services/test_status.py
git commit -m "feat(services): render_status — человекочитаемый отчёт статуса индекса"
```

---

### Task 6: CLI-команда `status` + документация

**Files:**
- Modify: `reviewer/entrypoints/cli.py` (импорт вверху + новая команда после `search`, ~строка 228)
- Modify: `README.md`, `CLAUDE.md`
- Test: `tests/services/test_status.py` (дополнить smoke-тестом CLI)

**Interfaces:**
- Consumes: `build_status_report`, `render_status` (Tasks 4–5); существующие `Settings`, `_resolve_repo`, `ChunkStore`, `GraphStore`, `_shutil` (уже импортирован как `shutil as _shutil`).
- Produces: команда `reviewer status [PATH] [--repo] [--branch]`.

- [ ] **Step 1: Написать падающий smoke-тест**

Дополнить `tests/services/test_status.py`:

```python
from click.testing import CliRunner
import reviewer.entrypoints.cli as cli_mod


def test_status_command_smoke(monkeypatch):
    dt = datetime(2026, 6, 18, 14, 2)
    rep = RepoStatus(
        repo="a/x",
        branches=[BranchStatus("main", "base:main", "abc1234", dt, 5, 3, 0)],
        overlays=[])
    monkeypatch.setattr(cli_mod, "build_status_report", lambda *a, **k: rep)
    res = CliRunner().invoke(cli_mod.cli, ["status", ".", "--repo", "a/x"])
    assert res.exit_code == 0, res.output
    assert "Ветка main" in res.output
    assert "✓ свежо" in res.output
```

(Реальных подключений нет: `build_status_report` замокан, `ChunkStore` ленив — пул не открывается, `GraphStore` создаёт драйвер без I/O, `.close()` обоих безопасен оффлайн.)

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/services/test_status.py::test_status_command_smoke -q`
Expected: FAIL — у `cli_mod` нет атрибута `build_status_report` (или нет команды `status`).

- [ ] **Step 3: Реализовать команду**

В `reviewer/entrypoints/cli.py` вверху, после строки `from reviewer.index.store import ChunkStore` (строка 16), добавить импорты:

```python
import psycopg
from reviewer.services.status import build_status_report, render_status
```

После команды `search` (после строки 228, перед `@cli.command()` для `serve`) добавить:

```python
@cli.command()
@click.argument("path", default=".")
@click.option("--repo", "repo_tag", default=None,
              help="owner/name тег индекса; по умолчанию из git remote origin")
@click.option("--branch", "branch_opt", default=None,
              help="одна ветка; по умолчанию все из REVIEW_BRANCHES")
def status(path: str, repo_tag: str | None, branch_opt: str | None) -> None:
    """Показать здоровье/свежесть base-индекса по веткам (не тратит Voyage)."""
    s = Settings()
    repo = _resolve_repo(repo_tag, path, s)
    branches = [branch_opt] if branch_opt else s.review_branches_list()
    store = ChunkStore(s.pg_dsn, min_size=s.pg_pool_min_size, max_size=s.pg_pool_max_size)
    graph = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    try:
        report = build_status_report(store, graph, repo, branches, path)
    except psycopg.OperationalError as e:
        raise click.ClickException(f"Postgres недоступен: {e}")
    finally:
        store.close()
        graph.close()
    backend = ("scip-python (точный)" if _shutil.which("scip-python")
               else "tree-sitter (fallback, scip-python не найден)")
    click.echo(render_status(report, backend))
```

- [ ] **Step 4: Запустить smoke-тест и весь unit-набор**

Run: `.venv/bin/pytest tests/services/test_status.py -q`
Expected: PASS (4 passed).

Run: `.venv/bin/pytest -q`
Expected: весь unit-набор зелёный (integration исключены по умолчанию).

- [ ] **Step 5: Обновить документацию**

В `README.md` и `CLAUDE.md` в блок со списком CLI-команд `reviewer` (рядом с `reviewer search`) добавить строку:

```
reviewer status                                   # здоровье/свежесть индекса по веткам (не тратит Voyage)
reviewer status /path/to/repo --branch dev        # статус конкретной ветки (дрейф vs git HEAD клона)
```

- [ ] **Step 6: Линт и финальный прогон**

Run: `.venv/bin/ruff check reviewer/services/status.py reviewer/entrypoints/cli.py reviewer/index/store.py reviewer/graph/store.py reviewer/gitutil.py`
Expected: чисто по затронутым файлам.

Run: `.venv/bin/pytest -q`
Expected: зелёно.

- [ ] **Step 7: Коммит**

```bash
git add reviewer/entrypoints/cli.py tests/services/test_status.py README.md CLAUDE.md
git commit -m "feat(cli): команда reviewer status — здоровье/свежесть индекса (PRI-125)"
```

---

## Self-Review (выполнено при написании плана)

**Spec coverage:**
- last-indexed SHA + updated_at → Task 2 (`get_index_meta_row`) + Task 5 (рендер).
- дрейф «свежо / отстаёт на N» → Task 1 (`commits_behind`) + Task 4 (`_drift`) + Task 5 (рендер). ✓ критерий приёмки.
- кол-во чанков → Task 2 (`count_chunks`).
- бэкенд графа (which scip-python) → Task 6 (CLI).
- узлы графа + fail-soft Neo4j → Task 3 (`count_nodes`) + Task 4 (try/except).
- overlay (`pr:*`/`local`/legacy `base`) → Task 4 (фильтр `list_refs`) + Task 5 (рендер).
- «не тратит Voyage» → Task 4 (чистый билдер) + Task 6 (сторы напрямую, без `build_components`).
- сигнатура `status [PATH] [--repo] [--branch]`, PATH=cwd → Task 6.
- Postgres down → exit 1 (Task 6 `ClickException`); прочие fail-soft (Tasks 1, 4).

**Placeholder scan:** плейсхолдеров нет — весь код приведён полностью.

**Type consistency:** имена/сигнатуры согласованы между задачами: `get_index_meta_row` (Task 2 → Task 4), `count_chunks`/`list_refs` (Task 2 → Task 4), `count_nodes` (Task 3 → Task 4), `commits_behind` (Task 1 → Task 4), `build_status_report`/`render_status` (Tasks 4–5 → Task 6), датаклассы `BranchStatus`/`OverlayStatus`/`RepoStatus` единообразны.
