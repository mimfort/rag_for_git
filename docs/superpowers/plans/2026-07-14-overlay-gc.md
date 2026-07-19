# GC осиротевших overlay `pr:N` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Осиротевшие overlay `pr:N` (ревью прервано между `prepare_review` и `publish_review`) и просроченные строки `review_sessions` больше не накапливаются в Postgres: их собирает server-side GC.

**Architecture:** Одна GC-функция (`reviewer/services/gc.py`) с двумя вызывающими — оппортунистически из `MCPReviewService.prepare_review` (fail-soft) и явной CLI-командой `reviewer gc`. Критерий сироты: overlay `pr:N` репо `R` — сирота, если `(R, N)` нет ни среди непросроченных строк `review_sessions`, ни среди in-memory сессий процесса. Миграция схемы `chunks` не нужна: `review_sessions.created_at` уже играет роль реестра живых overlay.

**Tech Stack:** Python 3.11+, psycopg3 (+ `psycopg_pool`), Click, pytest. БД — ParadeDB (Postgres) на порту 5433.

**Спека:** `docs/superpowers/specs/2026-07-14-overlay-gc-design.md`
**Бриф:** `docs/superpowers/briefs/2026-07-14-overlay-pr-ref-leak.md`

## Global Constraints

- Язык проекта — русский: докстринги, комментарии, сообщения CLI. Новый код пишется в этом стиле.
- Коммиты — Conventional Commits на русском (`feat(mcp): …`, `fix(index): …`), **без self-attribution** (никаких `Co-Authored-By` / упоминаний Claude).
- Линт: `.venv/bin/ruff check .` — line-length 100, target py311. Repo-wide чистоты нет; следи только за файлами, которые трогаешь.
- Unit-тесты не ходят в Postgres/Neo4j/Voyage: внешние сервисы мокаются. Тесты, которым нужна живая БД, помечаются `@pytest.mark.integration` (по умолчанию `pytest` их исключает: `addopts = -m 'not integration'`).
- **Инвариант безопасности GC:** «не знаю, какие сессии живы» ≠ «живых сессий нет». Если список живых сессий получить не удалось (БД недоступна), GC не удаляет **ничего**. Нарушение этого инварианта снесёт overlay идущего прямо сейчас ревью.
- GC трогает только `ref LIKE 'pr:%'`. `base:<branch>` не затрагивается никогда.
- TTL берётся из существующей настройки `review_session_ttl_hours` (`reviewer/config/settings.py:56`, дефолт 24) — той же, по которой регидрируется сессия (`reviewer/mcp/service.py:201`).

## File Structure

| Файл | Ответственность |
|---|---|
| `reviewer/services/gc.py` (новый) | Чистая GC-логика: какие overlay — сироты, что удалить. Не знает про MCP и CLI. |
| `reviewer/index/store.py` (правка) | `ChunkStore.list_overlay_refs()` — глобальный список `(repo, ref)` overlay по всем репо. |
| `reviewer/mcp/session_store.py` (правка) | `SessionStore.live_keys()` / `delete_expired()` — кто жив и уборка просроченных строк. |
| `reviewer/mcp/service.py` (правка) | `_gc_overlays()` + вызов из `prepare_review` (fail-soft). |
| `reviewer/entrypoints/cli.py` (правка) | Команда `reviewer gc` — явная уборка с отчётом человеку. |
| `tests/services/test_gc.py` (новый) | Unit: критерий сироты, защита живых, `base:*` не тронут, инвариант безопасности. |
| `tests/mcp/test_gc_on_prepare.py` (новый) | Unit: ядро бага — «prepare без publish → overlay осиротел → следующий prepare его убрал». |
| `tests/index/test_store_overlay_refs.py` (новый) | Integration: `list_overlay_refs` на живом Postgres. |
| `tests/mcp/test_session_store.py` (правка) | Integration: `live_keys` / `delete_expired`. |
| `tests/entrypoints/test_cli_gc.py` (новый) | Unit: CLI печатает отчёт. |

---

### Task 1: `ChunkStore.list_overlay_refs` — глобальный список overlay

**Files:**
- Modify: `reviewer/index/store.py` (рядом с `list_refs`, строка 250)
- Test: `tests/index/test_store_overlay_refs.py` (создать)

**Interfaces:**
- Consumes: ничего (первая задача).
- Produces: `ChunkStore.list_overlay_refs() -> list[tuple[str, str]]` — отсортированный список пар `(repo, ref)` для всех `ref LIKE 'pr:%'` по **всем** репозиториям. Используется в Task 3.

Существующий `list_refs(repo)` скоупится одним репо — GC нужен обзор всей базы (осиротевший overlay может быть в любом репо деплоя).

- [ ] **Step 1: Написать падающий тест**

Создай `tests/index/test_store_overlay_refs.py`:

```python
"""Integration-тест ChunkStore.list_overlay_refs (нужен поднятый Postgres)."""
import pytest

from reviewer.config.settings import Settings
from reviewer.index.store import ChunkRow, ChunkStore


def _row(ref, path, fqn, vec, repo="a/x"):
    return ChunkRow(repo=repo, ref=ref, content_hash=fqn + ref + repo, path=path,
                    lang="python", symbol_fqn=fqn, kind="function", start_line=1,
                    end_line=2, text="def f(): pass", embedding=vec)


@pytest.mark.integration
def test_list_overlay_refs_returns_pr_refs_across_repos_and_skips_base():
    """Возвращает overlay всех репо; base:* не возвращает никогда."""
    s = Settings()
    store = ChunkStore(s.pg_dsn)
    store.init_schema()
    store.clear()
    vec = [0.0] * s.embedding_dim
    store.upsert([
        _row("base:main", "x.py", "f_x", vec, repo="a/x"),
        _row("pr:42", "x.py", "f_x", vec, repo="a/x"),
        _row("pr:7", "y.py", "f_y", vec, repo="b/y"),
    ])

    assert store.list_overlay_refs() == [("a/x", "pr:42"), ("b/y", "pr:7")]
    store.close()
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/index/test_store_overlay_refs.py -m integration -v`
Expected: FAIL — `AttributeError: 'ChunkStore' object has no attribute 'list_overlay_refs'`

(Нужен поднятый Postgres: `docker compose up -d`.)

- [ ] **Step 3: Реализовать**

В `reviewer/index/store.py` сразу после метода `list_refs` (строка 256) добавь:

```python
    def list_overlay_refs(self) -> list[tuple[str, str]]:
        """Все overlay-ref (repo, ref) по ВСЕМ репозиториям: ref вида 'pr:<N>'.

        Отличие от list_refs(repo): тот скоупится одним репо, а GC осиротевших
        overlay (reviewer/services/gc.py) должен видеть базу целиком — брошенный
        overlay может остаться в любом репо деплоя. base:<branch> не возвращается.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT repo, ref FROM chunks WHERE ref LIKE 'pr:%%' "
                "ORDER BY repo, ref"
            ).fetchall()
        return [(r[0], r[1]) for r in rows]
```

Обрати внимание на `'pr:%%'`: psycopg использует `%` как маркер параметра, поэтому литеральный процент в SQL экранируется удвоением.

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/index/test_store_overlay_refs.py -m integration -v`
Expected: PASS

- [ ] **Step 5: Линт и коммит**

```bash
.venv/bin/ruff check reviewer/index/store.py tests/index/test_store_overlay_refs.py
git add reviewer/index/store.py tests/index/test_store_overlay_refs.py
git commit -m "feat(index): list_overlay_refs — глобальный список overlay pr:N для GC"
```

---

### Task 2: `SessionStore.live_keys` / `delete_expired`

**Files:**
- Modify: `reviewer/mcp/session_store.py` (после `delete`, строка 115)
- Test: `tests/mcp/test_session_store.py` (дописать в конец)

**Interfaces:**
- Consumes: ничего.
- Produces:
  - `SessionStore.live_keys(ttl_hours: int) -> set[tuple[str, int]]` — ключи `(repo, pr_number)` непросроченных сессий. **НЕ fail-soft: при сбое БД пробрасывает исключение** (см. Global Constraints — инвариант безопасности).
  - `SessionStore.delete_expired(ttl_hours: int) -> int` — удаляет просроченные строки, возвращает их число. Fail-soft: при сбое логирует и возвращает `0`.

Асимметрия намеренна. `live_keys` — вход для решения «что удалять»: тихо вернуть пустое множество здесь означало бы «живых нет» и привело бы к удалению overlay идущего ревью. `delete_expired` — только уборка, её сбой безвреден.

- [ ] **Step 1: Написать падающие тесты**

Дописать в конец `tests/mcp/test_session_store.py`:

```python
def test_live_keys_raises_when_db_unavailable(monkeypatch) -> None:
    """live_keys НЕ fail-soft: «не знаю живых» ≠ «живых нет».

    Тихий пустой ответ при сбое БД заставил бы GC счесть сиротой overlay
    идущего прямо сейчас ревью и удалить его.
    """
    store = SessionStore("postgresql://invalid/none")

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "_connect", boom)
    with pytest.raises(RuntimeError):
        store.live_keys(24)


@pytest.mark.integration
def test_live_keys_and_delete_expired() -> None:
    """live_keys видит только непросроченные; delete_expired сносит просроченные."""
    pg_dsn = Settings().pg_dsn
    store = SessionStore(pg_dsn)
    store.init_schema()
    repo, pr = "owner/gc-test", 998
    store.delete(repo, pr)  # чистый старт
    store.save(repo, pr, {"repo": repo})

    assert (repo, pr) in store.live_keys(24)
    # TTL=0 → строка мгновенно просрочена
    assert (repo, pr) not in store.live_keys(0)

    assert store.delete_expired(0) >= 1
    assert store.load(repo, pr, 24) is None   # строка физически удалена
    store.close()
```

- [ ] **Step 2: Запустить тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/mcp/test_session_store.py -v`
Expected: FAIL — `AttributeError: 'SessionStore' object has no attribute 'live_keys'`
(Unit-тест `test_live_keys_raises_when_db_unavailable` падает без Postgres; integration-тест — запусти отдельно с `-m integration`.)

- [ ] **Step 3: Реализовать**

В `reviewer/mcp/session_store.py` после метода `delete` (строка 115) добавь:

```python
    def live_keys(self, ttl_hours: int) -> set[tuple[str, int]]:
        """Ключи (repo, pr_number) сессий, у которых не истёк TTL.

        НЕ fail-soft осознанно: сбой БД пробрасывается. Для GC осиротевших
        overlay (reviewer/services/gc.py) пустое множество означает «живых
        сессий нет» — то есть «все overlay сироты». Молча вернуть его при
        недоступной БД значило бы снести overlay идущего прямо сейчас ревью.
        Вызывающий обязан отличить «живых нет» от «прочитать не удалось».
        """
        sql = """
        SELECT repo, pr_number FROM review_sessions
        WHERE created_at > now() - make_interval(hours => %s)
        """
        with self._connect() as conn:
            rows = conn.execute(sql, (ttl_hours,)).fetchall()
        return {(r[0], r[1]) for r in rows}

    def delete_expired(self, ttl_hours: int) -> int:
        """Удалить строки сессий с истёкшим TTL; вернуть их число.

        До этого TTL применялся ТОЛЬКО как фильтр на чтении (см. load) —
        просроченная строка становилась невидимой, но жила в таблице вечно.
        Fail-soft: сбой только логируется (уборка не обязана ронять вызывающего).
        """
        sql = "DELETE FROM review_sessions WHERE created_at <= now() - make_interval(hours => %s)"
        try:
            with self._connect() as conn:
                deleted = conn.execute(sql, (ttl_hours,)).rowcount
                conn.commit()
            return deleted
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось удалить просроченные сессии: %s", exc)
            return 0
```

- [ ] **Step 4: Запустить тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/mcp/test_session_store.py -v` (unit)
Expected: PASS

Run: `.venv/bin/pytest tests/mcp/test_session_store.py -m integration -v` (нужен Postgres)
Expected: PASS

- [ ] **Step 5: Линт и коммит**

```bash
.venv/bin/ruff check reviewer/mcp/session_store.py tests/mcp/test_session_store.py
git add reviewer/mcp/session_store.py tests/mcp/test_session_store.py
git commit -m "feat(mcp): SessionStore.live_keys и delete_expired — реестр живых сессий и уборка просроченных"
```

---

### Task 3: GC-функция `purge_orphaned_overlays`

**Files:**
- Create: `reviewer/services/gc.py`
- Test: `tests/services/test_gc.py` (создать)

**Interfaces:**
- Consumes: `ChunkStore.list_overlay_refs()` (Task 1), `ChunkStore.delete_ref(repo, ref)` (существует, `reviewer/index/store.py:147`), `SessionStore.live_keys(ttl_hours)` и `SessionStore.delete_expired(ttl_hours)` (Task 2).
- Produces: `purge_orphaned_overlays(store, session_store, ttl_hours: int, active_keys: set[tuple[str, int]] = frozenset()) -> dict` — возвращает `{"purged": list[str], "kept": int, "sessions_deleted": int}`. Элемент `purged` — строка вида `"owner/name pr:42"`. Используется в Task 4 (MCP) и Task 5 (CLI).

Форма отчёта повторяет `TaskService.purge_orphaned_tasks` (`reviewer/tasks/service.py:341`) — тот же паттерн «explicit purge + dict-отчёт», уже принятый в кодовой базе.

- [ ] **Step 1: Написать падающие тесты**

Создай `tests/services/test_gc.py`:

```python
"""Unit-тесты GC осиротевших overlay (фейковые store/session_store, без БД)."""
from __future__ import annotations

import pytest

from reviewer.services.gc import purge_orphaned_overlays


class _FakeStore:
    def __init__(self, overlays: list[tuple[str, str]]) -> None:
        self._overlays = overlays
        self.deleted: list[tuple[str, str]] = []

    def list_overlay_refs(self) -> list[tuple[str, str]]:
        return list(self._overlays)

    def delete_ref(self, repo: str, ref: str) -> None:
        self.deleted.append((repo, ref))


class _FakeSessionStore:
    def __init__(self, live: set[tuple[str, int]], *, boom: bool = False) -> None:
        self._live = live
        self._boom = boom
        self.expired_deleted = 0

    def live_keys(self, ttl_hours: int) -> set[tuple[str, int]]:
        if self._boom:
            raise RuntimeError("db down")
        return set(self._live)

    def delete_expired(self, ttl_hours: int) -> int:
        self.expired_deleted += 1
        return 3


def test_purges_overlay_without_live_session():
    """Ядро бага: overlay без живой сессии — сирота, его удаляем."""
    store = _FakeStore([("a/x", "pr:94")])
    sessions = _FakeSessionStore(live=set())

    report = purge_orphaned_overlays(store, sessions, ttl_hours=24)

    assert store.deleted == [("a/x", "pr:94")]
    assert report["purged"] == ["a/x pr:94"]
    assert report["kept"] == 0
    assert report["sessions_deleted"] == 3


def test_keeps_overlay_with_live_session_row():
    """Ревью с непросроченной строкой сессии живо — его overlay неприкосновенен."""
    store = _FakeStore([("a/x", "pr:5")])
    sessions = _FakeSessionStore(live={("a/x", 5)})

    report = purge_orphaned_overlays(store, sessions, ttl_hours=24)

    assert store.deleted == []
    assert report["kept"] == 1


def test_keeps_overlay_of_active_in_memory_session():
    """Сессия только в памяти процесса (persist упал fail-soft) — тоже живая."""
    store = _FakeStore([("a/x", "pr:5")])
    sessions = _FakeSessionStore(live=set())   # в БД строки нет

    report = purge_orphaned_overlays(store, sessions, ttl_hours=24,
                                     active_keys={("a/x", 5)})

    assert store.deleted == []
    assert report["kept"] == 1


def test_never_deletes_anything_when_live_set_unavailable():
    """Инвариант безопасности: «не знаю живых» ≠ «живых нет» — не удаляем ничего."""
    store = _FakeStore([("a/x", "pr:94")])
    sessions = _FakeSessionStore(live=set(), boom=True)

    with pytest.raises(RuntimeError):
        purge_orphaned_overlays(store, sessions, ttl_hours=24)

    assert store.deleted == []
    assert sessions.expired_deleted == 0


def test_no_session_store_is_noop():
    """Персист сессий выключен → живость определить нечем → не удаляем ничего."""
    store = _FakeStore([("a/x", "pr:94")])

    report = purge_orphaned_overlays(store, None, ttl_hours=24)

    assert store.deleted == []
    assert report == {"purged": [], "kept": 0, "sessions_deleted": 0}


def test_ignores_unparsable_ref():
    """Мусорный ref вида 'pr:abc' не удаляем — только то, что уверенно распознали."""
    store = _FakeStore([("a/x", "pr:abc")])
    sessions = _FakeSessionStore(live=set())

    report = purge_orphaned_overlays(store, sessions, ttl_hours=24)

    assert store.deleted == []
    assert report["kept"] == 1
```

- [ ] **Step 2: Запустить тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/services/test_gc.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'reviewer.services.gc'`

- [ ] **Step 3: Реализовать**

Создай `reviewer/services/gc.py`:

```python
"""GC эфемерных артефактов ревью: осиротевшие overlay pr:N и просроченные сессии.

На «счастливом» пути overlay удаляет MCPReviewService._cleanup (после
publish_review). Но если ревью прервано между prepare_review и publish_review
(пользователь отменил, оркестрирующая LLM-сессия упала или упёрлась в таймаут),
publish_review не вызывается НИКОГДА — и overlay остаётся в Postgres навсегда:
self-healing в начале ReviewService.prepare чистит только тот же самый PR, а
смерженный PR больше никто не ревьюит. Этот модуль — страховка на такой случай.

Критерий сироты: overlay pr:N репо R — сирота, если (R, N) нет ни среди
непросроченных строк review_sessions, ни среди активных сессий процесса.
Отдельная таблица-реестр не нужна: строка сессии создаётся ровно там же, где
строится overlay, поэтому review_sessions.created_at и есть возраст overlay.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_OVERLAY_PREFIX = "pr:"


def _pr_number(ref: str) -> int | None:
    """Номер PR из overlay-ref 'pr:<N>'; None — если ref не распознан."""
    if not ref.startswith(_OVERLAY_PREFIX):
        return None
    try:
        return int(ref[len(_OVERLAY_PREFIX):])
    except ValueError:
        return None


def purge_orphaned_overlays(
    store,
    session_store,
    ttl_hours: int,
    active_keys: set[tuple[str, int]] = frozenset(),
) -> dict:
    """Удалить overlay без живой сессии и просроченные строки review_sessions.

    active_keys — ключи (repo, pr) сессий, живущих в памяти вызывающего процесса.
    Они считаются живыми независимо от БД: SessionStore.save fail-soft, и при
    сбое персиста сессия существует только в памяти — без этой страховки GC снёс
    бы overlay идущего ревью.

    Инвариант безопасности: если множество живых сессий получить НЕ удалось,
    исключение пробрасывается и не удаляется НИЧЕГО — «не знаю живых» ≠ «живых
    нет». Вызывающий решает, как реагировать (prepare_review — fail-soft, CLI —
    показывает ошибку).

    Возвращает {"purged": [...], "kept": int, "sessions_deleted": int}.
    """
    if session_store is None:
        # Персист сессий выключен → живость overlay определить нечем.
        return {"purged": [], "kept": 0, "sessions_deleted": 0}

    live = session_store.live_keys(ttl_hours) | set(active_keys)

    purged: list[str] = []
    kept = 0
    for repo, ref in store.list_overlay_refs():
        pr = _pr_number(ref)
        if pr is None or (repo, pr) in live:
            kept += 1
            continue
        store.delete_ref(repo, ref)
        purged.append(f"{repo} {ref}")

    sessions_deleted = session_store.delete_expired(ttl_hours)
    if purged:
        log.info("GC overlay: удалено %s, оставлено живых %s (%s)",
                 len(purged), kept, ", ".join(purged))
    return {"purged": purged, "kept": kept, "sessions_deleted": sessions_deleted}
```

- [ ] **Step 4: Запустить тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/services/test_gc.py -v`
Expected: PASS (6 тестов)

- [ ] **Step 5: Линт и коммит**

```bash
.venv/bin/ruff check reviewer/services/gc.py tests/services/test_gc.py
git add reviewer/services/gc.py tests/services/test_gc.py
git commit -m "feat(services): purge_orphaned_overlays — GC overlay без живой сессии"
```

---

### Task 4: Вызов GC из `prepare_review` (fail-soft) — закрытие ядра бага

**Files:**
- Modify: `reviewer/mcp/service.py` (`prepare_review`, строка 108; новый метод `_gc_overlays` рядом с `_cleanup`, строка 1178)
- Test: `tests/mcp/test_gc_on_prepare.py` (создать)

**Interfaces:**
- Consumes: `purge_orphaned_overlays` (Task 3).
- Produces: `MCPReviewService._gc_overlays() -> None` — оппортунистический GC, никогда не бросает.

Это и есть гарантия «больше никогда»: брошенный overlay живёт максимум до следующего `prepare_review` в этом деплое.

- [ ] **Step 1: Написать падающий тест (ядро бага)**

Создай `tests/mcp/test_gc_on_prepare.py`. Фейки повторяют образец `tests/mcp/test_publish.py:35-63`:

```python
"""Ядро бага: prepare_review без publish_review оставлял overlay навсегда.

Сценарий: ревью PR 7 подготовили, но публикация не состоялась (пользователь
отменил / оркестрирующая сессия упала) — _cleanup не вызвался. Следующий
prepare_review (ДРУГОГО PR) обязан подчистить осиротевший overlay pr:7.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService


def _settings() -> Settings:
    s = Settings()
    s.voyage_api_key = "test"
    s.github_token = "test"
    s.review_session_persist = False   # не трогаем реальный Postgres
    return s


def _components() -> MagicMock:
    c = MagicMock()
    c.store = MagicMock()
    c.store.deleted_refs = []
    c.store.delete_ref.side_effect = lambda repo, ref: c.store.deleted_refs.append(ref)
    c.store.list_overlay_refs.return_value = [("a/x", "pr:7")]
    return c


class _FakeSessionStore:
    """Строк сессий нет: сессия PR 7 умерла вместе с оркестрирующим клиентом.

    save() нужен потому, что prepare_review персистит новую сессию
    (reviewer/mcp/service.py:133-135) — без него тест упал бы на AttributeError.
    """

    def save(self, repo: str, pr: int, payload: dict) -> None:
        pass

    def live_keys(self, ttl_hours: int) -> set[tuple[str, int]]:
        return set()

    def delete_expired(self, ttl_hours: int) -> int:
        return 0


def _service(components: MagicMock) -> MCPReviewService:
    svc = MCPReviewService(_settings(), components)
    svc._session_store = _FakeSessionStore()
    # prepare() мокаем: нас интересует только GC, не подготовка ревью.
    svc._review_service = MagicMock()
    svc._prepared_payload = MagicMock(return_value={"status": "ok"})
    return svc


def test_prepare_purges_orphaned_overlay_of_abandoned_review():
    """Overlay брошенного ревью (pr:7) удаляется при следующем prepare_review."""
    c = _components()
    svc = _service(c)

    svc.prepare_review("a/x", 8)

    assert "pr:7" in c.store.deleted_refs


def test_prepare_keeps_overlay_of_active_in_memory_session():
    """Overlay ревью, живущего в памяти процесса, не трогаем (параллельное ревью)."""
    c = _components()
    svc = _service(c)
    svc._sessions[("a/x", 7)] = MagicMock()   # ревью PR 7 идёт прямо сейчас

    svc.prepare_review("a/x", 8)

    assert "pr:7" not in c.store.deleted_refs


def test_gc_failure_does_not_break_prepare():
    """Сбой GC не роняет подготовку ревью (fail-soft)."""
    c = _components()
    c.store.list_overlay_refs.side_effect = RuntimeError("db down")
    svc = _service(c)

    assert svc.prepare_review("a/x", 8) == {"status": "ok"}   # не бросил
```

- [ ] **Step 2: Запустить тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/mcp/test_gc_on_prepare.py -v`
Expected: FAIL — `test_prepare_purges_orphaned_overlay_of_abandoned_review`: `assert 'pr:7' in []` (GC не вызывается, overlay остаётся навсегда — это и есть баг).

- [ ] **Step 3: Реализовать**

В `reviewer/mcp/service.py` добавь импорт к остальным импортам из `reviewer.services`:

```python
from reviewer.services.gc import purge_orphaned_overlays
```

Добавь метод рядом с `_cleanup` (после строки 1197):

```python
    def _gc_overlays(self) -> None:
        """Оппортунистический GC осиротевших overlay (fail-soft, никогда не бросает).

        Вызывается на каждом prepare_review. _cleanup убирает overlay только когда
        publish_review реально состоялся; если ревью брошено между prepare и publish,
        убрать его больше некому — этот вызов и есть уборщик. Активные сессии процесса
        передаются как живые: их overlay неприкосновенен, даже если persist сессии
        упал fail-soft.

        Сбой GC не должен мешать ревью — любая ошибка уходит в лог.
        """
        try:
            purge_orphaned_overlays(
                self.components.store,
                self._ensure_session_store(),
                self.settings.review_session_ttl_hours,
                active_keys=set(self._sessions),
            )
        except Exception:
            log.warning("GC осиротевших overlay не удался", exc_info=True)
```

В `prepare_review` вызови его сразу после нормализации repo (после строки 118, `owner, name = repo.split("/", 1)`):

```python
        self._gc_overlays()
```

- [ ] **Step 4: Запустить тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/mcp/test_gc_on_prepare.py -v`
Expected: PASS (3 теста)

Прогони весь MCP-пакет — вызов GC не должен сломать существующие тесты (в них `components.store` — MagicMock, `list_overlay_refs` вернёт MagicMock, поэтому важно, что GC fail-soft):

Run: `.venv/bin/pytest tests/mcp -q`
Expected: PASS

- [ ] **Step 5: Линт и коммит**

```bash
.venv/bin/ruff check reviewer/mcp/service.py tests/mcp/test_gc_on_prepare.py
git add reviewer/mcp/service.py tests/mcp/test_gc_on_prepare.py
git commit -m "fix(mcp): GC осиротевших overlay при prepare_review — брошенное ревью больше не течёт"
```

---

### Task 5: CLI-команда `reviewer gc`

**Files:**
- Modify: `reviewer/entrypoints/cli.py` (новая команда после `status`, строка 531)
- Test: `tests/entrypoints/test_cli_gc.py` (создать)

**Interfaces:**
- Consumes: `purge_orphaned_overlays` (Task 3), `ChunkStore` и `SessionStore` (конструкторы как в команде `status`, строки 516-517).
- Produces: команда `reviewer gc` — явная уборка с отчётом человеку. Ею же вычищается уже осиротевший `pr:94`.

- [ ] **Step 1: Написать падающий тест**

Создай `tests/entrypoints/test_cli_gc.py`:

```python
"""Unit-тест CLI-команды `reviewer gc` (без Postgres: патчим стор и GC)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from reviewer.entrypoints.cli import cli


def test_gc_prints_report():
    """Команда печатает, что удалила и что оставила живым."""
    report = {"purged": ["a/x pr:94"], "kept": 2, "sessions_deleted": 3}
    with patch("reviewer.entrypoints.cli.ChunkStore", MagicMock()), \
         patch("reviewer.entrypoints.cli.SessionStore", MagicMock()), \
         patch("reviewer.entrypoints.cli.purge_orphaned_overlays",
               return_value=report) as gc_fn:
        result = CliRunner().invoke(cli, ["gc"])

    assert result.exit_code == 0
    assert "a/x pr:94" in result.output
    assert "оставлено живых 2" in result.output
    assert "3" in result.output
    assert gc_fn.called
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/entrypoints/test_cli_gc.py -v`
Expected: FAIL — `AttributeError: <module 'reviewer.entrypoints.cli'> does not have the attribute 'SessionStore'` (или ненулевой exit_code: команды `gc` ещё нет).

- [ ] **Step 3: Реализовать**

В `reviewer/entrypoints/cli.py` добавь импорты к существующим:

```python
from reviewer.mcp.session_store import SessionStore
from reviewer.services.gc import purge_orphaned_overlays
```

После команды `status` (строка 531) добавь:

```python
@cli.command()
def gc() -> None:
    """Вычистить осиротевшие overlay pr:N и просроченные сессии ревью.

    Overlay брошенного ревью (prepare_review без publish_review) не убирает никто:
    _cleanup срабатывает только после реальной публикации. Эта команда — явная
    уборка; та же логика оппортунистически работает при каждом prepare_review.
    """
    s = Settings()
    store = ChunkStore(s.pg_dsn, min_size=s.pg_pool_min_size, max_size=s.pg_pool_max_size)
    session_store = SessionStore(
        s.pg_dsn, min_size=s.pg_pool_min_size, max_size=s.pg_pool_max_size
    )
    try:
        report = purge_orphaned_overlays(store, session_store, s.review_session_ttl_hours)
    except psycopg.OperationalError as e:
        raise click.ClickException(f"Postgres недоступен: {e}")
    finally:
        store.close()
        session_store.close()
    for ref in report["purged"]:
        click.echo(f"удалён осиротевший overlay: {ref}")
    click.echo(
        f"Overlay: удалено {len(report['purged'])}, оставлено живых {report['kept']}; "
        f"просроченных сессий удалено: {report['sessions_deleted']}"
    )
```

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/entrypoints/test_cli_gc.py -v`
Expected: PASS

- [ ] **Step 5: Линт и коммит**

```bash
.venv/bin/ruff check reviewer/entrypoints/cli.py tests/entrypoints/test_cli_gc.py
git add reviewer/entrypoints/cli.py tests/entrypoints/test_cli_gc.py
git commit -m "feat(cli): команда reviewer gc — явная уборка осиротевших overlay"
```

---

### Task 6: Документация и вычистка накопленного мусора

**Files:**
- Modify: `CLAUDE.md` (буллет «Overlay удаляется автоматически» в разделе «Неочевидные факты»)
- Modify: `README.md` (EN) и `README.ru.md` (RU) — тот же инвариант + новая CLI-команда в списке команд

**Interfaces:**
- Consumes: `reviewer gc` (Task 5).
- Produces: ничего (документация + разовая операция).

Текущая формулировка в `CLAUDE.md` («Overlay удаляется автоматически — после `publish_review` эфемерный ref не остаётся в Postgres») неточна и ровно она ввела в заблуждение: она молчит про случай, когда `publish_review` не вызывается вовсе.

- [ ] **Step 1: Поправить `CLAUDE.md`**

Замени буллет про overlay в разделе «Неочевидные факты» на:

```markdown
- **Overlay удаляется автоматически** (`store.delete_ref("pr:N")`) — после `publish_review` эфемерный
  ref не остаётся в Postgres. При сбое prepare также чистится (fail-soft). Но если ревью **брошено**
  между `prepare_review` и `publish_review` (пользователь отменил, оркестрирующая LLM-сессия упала),
  публикация не вызывается вовсе — такой overlay собирает **GC** (`reviewer/services/gc.py`):
  оппортунистически при каждом `prepare_review` и по команде `reviewer gc`. Сирота = `pr:N` без
  непросроченной строки в `review_sessions` (TTL `review_session_ttl_hours`) и вне активных сессий
  процесса. GC никогда не трогает `base:<branch>`; при недоступной БД не удаляет ничего
  («не знаю живых» ≠ «живых нет»). Гарантию даёт только сервер: скилл `review-pr` — это промпт,
  а не `try/finally`.
```

- [ ] **Step 2: Поправить оба README**

Найди места, которые нужно тронуть (список CLI-команд и описание жизненного цикла overlay):

```bash
grep -n "reviewer status\|overlay\|pr:N" README.md README.ru.md
```

В **обоих** файлах нужны две правки. Первая — в списке CLI-команд, рядом со строкой про `reviewer status`:

`README.ru.md`:
```
reviewer gc                                       # вычистить осиротевшие overlay (брошенные ревью) и просроченные сессии
```

`README.md`:
```
reviewer gc                                       # purge orphaned overlays (abandoned reviews) and expired sessions
```

Вторая — там, где описан жизненный цикл overlay: дописать, что overlay брошенного ревью
(`prepare_review` без `publish_review`) собирает GC — при следующем `prepare_review` и по команде
`reviewer gc`.

`README.ru.md`:
```
Если ревью брошено между `prepare_review` и `publish_review` (пользователь отменил, оркестрирующая
LLM-сессия упала), публикация не вызывается — такой overlay собирает GC: оппортунистически при
следующем `prepare_review` и по команде `reviewer gc`.
```

`README.md`:
```
If a review is abandoned between `prepare_review` and `publish_review` (user cancelled, orchestrating
LLM session died), publish never runs — such an overlay is collected by GC: opportunistically on the
next `prepare_review`, and via the `reviewer gc` command.
```

Оба README держим синхронными: правка в одном без другого — регресс документации.

- [ ] **Step 3: Прогнать весь unit-набор и линт**

Run: `.venv/bin/pytest -q`
Expected: PASS (весь набор, integration исключены автоматически)

Run: `.venv/bin/ruff check reviewer/ tests/`
Expected: без новых замечаний в тронутых файлах (repo-wide чистоты нет — сравнивай с состоянием до правок)

- [ ] **Step 4: Вычистить накопленный мусор на живой БД**

Run: `.venv/bin/reviewer gc`
Expected: в выводе `удалён осиротевший overlay: mimfort/rag_for_git pr:94`

Проверь, что мусора не осталось:

Run: `.venv/bin/reviewer status . --branch dev --json`
Expected: в JSON пустой список `"overlays": []`

- [ ] **Step 5: Коммит**

```bash
git add CLAUDE.md README.md README.ru.md
git commit -m "docs: GC осиротевших overlay — уточнить инвариант жизненного цикла pr:N"
```
