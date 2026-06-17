# PRI-99 — Персистентность сессии reviewer-mcp Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сессия ревью PR переживает рестарт/краш процесса `reviewer-mcp` между `prepare_review` и `publish_review` за счёт персиста `PreparedReview` в Postgres и ленивой регидрации.

**Architecture:** `MCPReviewService._sessions` остаётся горячим in-memory кэшем; `prepare_review` дополнительно сериализует `PreparedReview` (минус живой `vcs`) в таблицу `review_sessions`. При промахе кэша `_session()` лениво поднимает строку, пересоздаёт `vcs` через `_create_vcs_provider` и `ctx` через `_tool_context`, прогревает кэш. Все операции хранилища fail-soft. Overlay `pr:N` (эмбеддинги) уже персистится отдельно и переживает рестарт.

**Tech Stack:** Python 3.11–3.13, psycopg3 + psycopg_pool (Postgres/ParadeDB), FastMCP, pytest.

## Global Constraints

- Язык проекта — **русский**: комментарии, докстринги, сообщения. Сохранять стиль.
- Коммиты — Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By`/упоминаний Claude).
- Линт: `ruff check .`, line-length 100, target py311.
- Внешние сервисы изолированы за интерфейсами и мокаются в unit-тестах; реальные вызовы — только в тестах под маркером `integration`.
- `pytest` по умолчанию исключает `integration` (`addopts = -m 'not integration'`).
- Ветка работы: `feat/pri-99-session-persistence` (уже создана; на ней лежит спека).
- Спека-источник: `docs/superpowers/specs/2026-06-17-pri-99-session-persistence-design.md`.

## File Structure

- **Create** `reviewer/mcp/session_store.py` — класс `SessionStore` (Postgres-хранилище подложки сессий, по образцу `reviewer/web/history.py`). Единственная ответственность: save/load/delete/TTL + схема.
- **Create** `reviewer/mcp/session_store.sql` — DDL таблицы `review_sessions`.
- **Create** `reviewer/mcp/session_serde.py` — чистые функции `to_payload`/`from_payload` (сериализация `PreparedReview` ↔ JSON-dict). Отделены от хранилища: знают доменные типы, не знают про БД.
- **Modify** `reviewer/config/settings.py` — два поля: `review_session_persist`, `review_session_ttl_hours`.
- **Modify** `reviewer/mcp/service.py` — `_ensure_session_store()`, `_rehydrate_session()`, правки `__init__`/`prepare_review`/`_session`/`_cleanup` + импорты.
- **Modify** `pyproject.toml` — `package-data` для `reviewer.mcp` (`*.sql`).
- **Create** `tests/mcp/test_session_store.py` — unit (fail-soft) + integration (реальный Postgres) для `SessionStore`.
- **Create** `tests/mcp/test_session_serde.py` — round-trip `to_payload`/`from_payload`.
- **Create** `tests/mcp/test_session_persist.py` — регидрация после очистки `_sessions`, промах → `ValueError`+hint, save fail-soft.
- **Modify** `tests/mcp/test_publish.py`, `tests/mcp/test_service.py` — добавить `review_session_persist = False` в хелперы `_settings()` (герметичность: unit-тесты не трогают Postgres).

---

### Task 1: `SessionStore` + схема + упаковка

**Files:**
- Create: `reviewer/mcp/session_store.py`
- Create: `reviewer/mcp/session_store.sql`
- Modify: `pyproject.toml:54-56` (блок `[tool.setuptools.package-data]`)
- Test: `tests/mcp/test_session_store.py`

**Interfaces:**
- Consumes: ничего (нижний слой).
- Produces:
  - `class SessionStore(pg_dsn: str, *, min_size: int = 1, max_size: int = 4)`
  - `SessionStore.init_schema() -> None`
  - `SessionStore.save(repo: str, pr: int, payload: dict) -> None` (fail-soft)
  - `SessionStore.load(repo: str, pr: int, ttl_hours: int) -> dict | None` (fail-soft; TTL в `WHERE`)
  - `SessionStore.delete(repo: str, pr: int) -> None` (fail-soft)
  - `SessionStore.close() -> None`

- [ ] **Step 1: Создать DDL `reviewer/mcp/session_store.sql`**

```sql
-- Подложка in-memory MCPReviewService._sessions: переживает рестарт процесса
-- reviewer-mcp между prepare_review и publish_review. Применяется идемпотентно.
CREATE TABLE IF NOT EXISTS review_sessions (
    repo       TEXT        NOT NULL,
    pr_number  INTEGER     NOT NULL,
    payload    JSONB       NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (repo, pr_number)
);

CREATE INDEX IF NOT EXISTS review_sessions_created_idx ON review_sessions (created_at);
```

- [ ] **Step 2: Создать `reviewer/mcp/session_store.py`**

```python
"""Персистентное хранилище подготовленных сессий ревью (Postgres).

Подложка для in-memory ``MCPReviewService._sessions``: переживает рестарт/краш
процесса reviewer-mcp между ``prepare_review`` и ``publish_review`` одного PR.
Использует ту же БД (PG_DSN), что и ChunkStore/ReviewHistory, но отдельную
таблицу ``review_sessions``. Все операции fail-soft — персист никогда не должен
ронять основной путь ревью.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)

_SCHEMA = Path(__file__).with_name("session_store.sql").read_text()


class SessionStore:
    """Сохраняет/восстанавливает сериализованный PreparedReview по ключу (repo, pr).

    Пул соединений создаётся лениво; схема инициализируется идемпотентно при
    первом обращении. TTL применяется на чтении через условие ``WHERE``.
    """

    def __init__(self, pg_dsn: str, *, min_size: int = 1, max_size: int = 4) -> None:
        self.pg_dsn = pg_dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: ConnectionPool | None = None
        self._init_lock = threading.Lock()
        self._schema_ready = False

    def _ensure_pool(self) -> ConnectionPool:
        """Создать и открыть пул при первом обращении (thread-safe)."""
        if self._pool is None:
            with self._init_lock:
                if self._pool is None:
                    pool = ConnectionPool(
                        self.pg_dsn,
                        min_size=self._min_size,
                        max_size=self._max_size,
                        open=False,
                    )
                    pool.open()
                    self._pool = pool
        return self._pool

    def init_schema(self) -> None:
        """Создать таблицу review_sessions, если её нет (идемпотентно)."""
        with self._ensure_pool().connection() as conn:
            conn.execute(_SCHEMA)
            conn.commit()
        self._schema_ready = True

    def _connect(self):
        """Вернуть соединение из пула, гарантировав наличие схемы."""
        if not self._schema_ready:
            self.init_schema()
        return self._ensure_pool().connection()

    def close(self) -> None:
        """Закрыть пул соединений, если он был создан."""
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def save(self, repo: str, pr: int, payload: dict) -> None:
        """Upsert сериализованной сессии. Fail-soft: сбой только логируется."""
        sql = """
        INSERT INTO review_sessions (repo, pr_number, payload, created_at)
        VALUES (%s, %s, %s::jsonb, now())
        ON CONFLICT (repo, pr_number)
        DO UPDATE SET payload = EXCLUDED.payload, created_at = now()
        """
        try:
            with self._connect() as conn:
                conn.execute(sql, (repo, pr, json.dumps(payload, ensure_ascii=False)))
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось сохранить сессию %s#%s: %s", repo, pr, exc)

    def load(self, repo: str, pr: int, ttl_hours: int) -> dict | None:
        """Прочитать payload, если строка существует и не истёк TTL; иначе None.

        Fail-soft: при сбое БД возвращает None (вызывающий трактует как промах).
        """
        sql = """
        SELECT payload FROM review_sessions
        WHERE repo = %s AND pr_number = %s
          AND created_at > now() - make_interval(hours => %s)
        """
        try:
            with self._connect() as conn:
                row = conn.execute(sql, (repo, pr, ttl_hours)).fetchone()
            return row[0] if row else None
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось загрузить сессию %s#%s: %s", repo, pr, exc)
            return None

    def delete(self, repo: str, pr: int) -> None:
        """Удалить строку сессии. Fail-soft: сбой только логируется."""
        try:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM review_sessions WHERE repo = %s AND pr_number = %s",
                    (repo, pr),
                )
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось удалить сессию %s#%s: %s", repo, pr, exc)
```

- [ ] **Step 3: Включить `*.sql` для `reviewer.mcp` в `pyproject.toml`**

В блоке `[tool.setuptools.package-data]` (строки 54-56) добавить строку для `reviewer.mcp`, иначе `session_store.sql` не попадёт в wheel и импорт `session_store.py` упадёт в прод-деплое (`uvx --from rag-reviewer`):

```toml
[tool.setuptools.package-data]
"reviewer.web" = ["*.sql"]
"reviewer.index" = ["*.sql"]
"reviewer.mcp" = ["*.sql"]
```

- [ ] **Step 4: Написать unit-тест fail-soft (без БД) + integration-тест в `tests/mcp/test_session_store.py`**

```python
"""Тесты SessionStore.

Unit-тест fail-soft не требует инфраструктуры (мокаем _connect).
Integration-тесты (save/load/delete/TTL) помечены @pytest.mark.integration
и требуют поднятого Postgres (docker compose up -d).
"""
from __future__ import annotations

import pytest

from reviewer.config.settings import Settings
from reviewer.mcp.session_store import SessionStore


def test_session_store_failsoft_without_db(monkeypatch) -> None:
    """Сбой соединения не пробрасывается: save/delete молчат, load → None."""
    store = SessionStore("postgresql://invalid/none")

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "_connect", boom)
    store.save("o/r", 1, {"a": 1})            # не бросает
    assert store.load("o/r", 1, 24) is None   # fail-soft → None
    store.delete("o/r", 1)                     # не бросает


@pytest.mark.integration
def test_session_store_save_load_delete_ttl() -> None:
    pg_dsn = Settings().pg_dsn
    store = SessionStore(pg_dsn)
    store.init_schema()
    store.init_schema()  # идемпотентность
    repo, pr = "owner/sess-test", 999
    store.delete(repo, pr)  # чистый старт

    payload = {"repo": repo, "branch": "main", "items": [1, 2, 3]}
    store.save(repo, pr, payload)
    assert store.load(repo, pr, 24) == payload

    # upsert: повторный save перезаписывает payload
    store.save(repo, pr, {"repo": repo, "branch": "main", "items": []})
    assert store.load(repo, pr, 24)["items"] == []

    # TTL=0 → created_at > now() ложно → строка считается просроченной
    assert store.load(repo, pr, 0) is None

    store.delete(repo, pr)
    assert store.load(repo, pr, 24) is None
    store.close()
```

- [ ] **Step 5: Запустить тесты**

Run: `.venv/bin/pytest tests/mcp/test_session_store.py -v`
Expected: `test_session_store_failsoft_without_db` PASS; integration-тест **deselected** (без флага `-m integration`).

Опционально с поднятым Postgres: `.venv/bin/pytest tests/mcp/test_session_store.py -m integration -v` → оба PASS.

- [ ] **Step 6: Линт**

Run: `.venv/bin/ruff check reviewer/mcp/session_store.py tests/mcp/test_session_store.py`
Expected: без ошибок.

- [ ] **Step 7: Commit**

```bash
git add reviewer/mcp/session_store.py reviewer/mcp/session_store.sql pyproject.toml tests/mcp/test_session_store.py
git commit -m "feat(mcp): хранилище SessionStore для персиста сессий ревью (PRI-99)"
```

---

### Task 2: Сериализатор `PreparedReview` ↔ payload

**Files:**
- Create: `reviewer/mcp/session_serde.py`
- Test: `tests/mcp/test_session_serde.py`

**Interfaces:**
- Consumes: `PreparedReview` (`reviewer/services/review_service.py`), `PullRequest` (`reviewer/vcs/base.py`), `ReviewUnit` (`reviewer/agent/state.py`), `ReviewPolicy` (`reviewer/policy/policy.py`).
- Produces:
  - `to_payload(prepared: PreparedReview) -> dict` — JSON-дружелюбный dict без `vcs`.
  - `from_payload(d: dict, vcs) -> PreparedReview` — восстановление; `vcs` передаётся отдельно; бросает `KeyError`/`TypeError` на несовместимом payload.

- [ ] **Step 1: Написать падающий тест `tests/mcp/test_session_serde.py`**

```python
"""Round-trip сериализации PreparedReview через session_serde."""
from __future__ import annotations

import json

from reviewer.agent.state import ReviewUnit
from reviewer.mcp.session_serde import from_payload, to_payload
from reviewer.policy.policy import ReviewPolicy
from reviewer.services.review_service import PreparedReview
from reviewer.vcs.base import PullRequest


class _DummyVCS:
    """Маркерный объект вместо живого VCSProvider."""


def _prepared(vcs) -> PreparedReview:
    prq = PullRequest(
        number=7, base_sha="b1", head_sha="h2", base_ref="main",
        title="T", body="body", draft=False, head_ref="feature/x",
    )
    return PreparedReview(
        repo="o/r",
        branch="main",
        prq=prq,
        units=[ReviewUnit("a.py", ["a.py#foo"], "@@ -1 +1 @@\n x", new_source="x = 1\n")],
        policy=ReviewPolicy(min_confidence=0.5, max_comments=10, task_board={"type": "yougile"}),
        patches={"a.py": "@@ -1 +1 @@\n x", "b.py": None},
        sources={"a.py": "x = 1\n"},
        changed_paths=["a.py"],
        changed_node_ids=["a.py#foo"],
        skipped_paths=["c.py"],
        overlay_ref="pr:7",
        vcs=vcs,
        changed_status={"a.py": "modified", "b.py": "added"},
        task_board={"type": "yougile"},
        task_keys={"primary": "PRI-7", "others": []},
    )


def test_payload_roundtrip_preserves_fields() -> None:
    original = _prepared(_DummyVCS())
    # Имитируем хранение в JSONB: payload должен пережить json-сериализацию.
    payload = json.loads(json.dumps(to_payload(original)))

    new_vcs = _DummyVCS()
    restored = from_payload(payload, new_vcs)

    assert restored.repo == original.repo
    assert restored.branch == original.branch
    assert restored.prq == original.prq                 # dataclass __eq__
    assert restored.units == original.units             # list[ReviewUnit] __eq__
    assert restored.policy == original.policy            # ReviewPolicy __eq__
    assert restored.patches == original.patches
    assert restored.sources == original.sources
    assert restored.changed_paths == original.changed_paths
    assert restored.changed_node_ids == original.changed_node_ids
    assert restored.skipped_paths == original.skipped_paths
    assert restored.overlay_ref == original.overlay_ref
    assert restored.changed_status == original.changed_status
    assert restored.task_board == original.task_board
    assert restored.task_keys == original.task_keys
    assert restored.vcs is new_vcs                       # vcs не сериализуется, подставлен заново


def test_to_payload_excludes_vcs() -> None:
    payload = to_payload(_prepared(_DummyVCS()))
    assert "vcs" not in payload
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/mcp/test_session_serde.py -v`
Expected: FAIL с `ModuleNotFoundError: reviewer.mcp.session_serde`.

- [ ] **Step 3: Реализовать `reviewer/mcp/session_serde.py`**

```python
"""Сериализация PreparedReview в JSON-payload для SessionStore и обратно.

Живой ``vcs`` (VCSProvider с httpx-клиентом) НЕ сериализуется — он восстанавливается
вызывающим (MCPReviewService через _create_vcs_provider) и передаётся в from_payload.
``ctx`` (ToolContext) не сериализуется вовсе — он пересобирается из PreparedReview.
"""
from __future__ import annotations

from dataclasses import asdict

from reviewer.agent.state import ReviewUnit
from reviewer.policy.policy import ReviewPolicy
from reviewer.services.review_service import PreparedReview
from reviewer.vcs.base import PullRequest, VCSProvider


def to_payload(prepared: PreparedReview) -> dict:
    """Собрать JSON-дружелюбный dict из PreparedReview, исключая ``vcs``.

    Не используем ``dataclasses.asdict(prepared)`` целиком — он попытался бы
    глубоко скопировать живой ``vcs``. Сериализуем поля явно.
    """
    return {
        "repo": prepared.repo,
        "branch": prepared.branch,
        "prq": asdict(prepared.prq),
        "units": [asdict(u) for u in prepared.units],
        "policy": asdict(prepared.policy),
        "patches": prepared.patches,
        "sources": prepared.sources,
        "changed_paths": prepared.changed_paths,
        "changed_node_ids": prepared.changed_node_ids,
        "skipped_paths": prepared.skipped_paths,
        "overlay_ref": prepared.overlay_ref,
        "changed_status": prepared.changed_status,
        "task_board": prepared.task_board,
        "task_keys": prepared.task_keys,
    }


def from_payload(d: dict, vcs: VCSProvider) -> PreparedReview:
    """Восстановить PreparedReview из payload; ``vcs`` подставляется отдельно.

    Бросает KeyError/TypeError при несовместимом payload (например, схема
    dataclass изменилась между версиями) — вызывающий ловит и трактует как
    промах регидрации.
    """
    return PreparedReview(
        repo=d["repo"],
        branch=d["branch"],
        prq=PullRequest(**d["prq"]),
        units=[ReviewUnit(**u) for u in d["units"]],
        policy=ReviewPolicy(**d["policy"]),
        patches=d["patches"],
        sources=d["sources"],
        changed_paths=d["changed_paths"],
        changed_node_ids=d["changed_node_ids"],
        skipped_paths=d["skipped_paths"],
        overlay_ref=d["overlay_ref"],
        vcs=vcs,
        changed_status=d["changed_status"],
        task_board=d.get("task_board"),
        task_keys=d.get("task_keys"),
    )
```

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/mcp/test_session_serde.py -v`
Expected: оба теста PASS.

- [ ] **Step 5: Линт**

Run: `.venv/bin/ruff check reviewer/mcp/session_serde.py tests/mcp/test_session_serde.py`
Expected: без ошибок.

- [ ] **Step 6: Commit**

```bash
git add reviewer/mcp/session_serde.py tests/mcp/test_session_serde.py
git commit -m "feat(mcp): сериализация PreparedReview для персиста сессии (PRI-99)"
```

---

### Task 3: Настройки + проводка в `MCPReviewService` + регидрация

**Files:**
- Modify: `reviewer/config/settings.py:50` (рядом с `review_history`)
- Modify: `reviewer/mcp/service.py` (импорты; `__init__`; `prepare_review`; `_session`; `_cleanup`; новые `_ensure_session_store`/`_rehydrate_session`)
- Modify: `tests/mcp/test_publish.py:34-43` (хелпер `_settings`), `tests/mcp/test_service.py` (хелпер `_settings`)
- Test: `tests/mcp/test_session_persist.py`

**Interfaces:**
- Consumes: `SessionStore` (Task 1), `to_payload`/`from_payload` (Task 2), существующие `MCPReviewService._tool_context`, `ReviewService._create_vcs_provider`, `Settings.pg_dsn`/`pg_pool_min_size`/`pg_pool_max_size`.
- Produces:
  - `Settings.review_session_persist: bool` (дефолт `True`), `Settings.review_session_ttl_hours: int` (дефолт `24`).
  - `MCPReviewService._ensure_session_store() -> SessionStore | None`
  - `MCPReviewService._rehydrate_session(repo: str, pr: int) -> _Session | None`
  - Изменённый `_session()` с регидрацией; `prepare_review` пишет в store; `_cleanup` удаляет из store.

- [ ] **Step 1: Добавить поля в `Settings` (`reviewer/config/settings.py`)**

Найти строку `review_history: bool = True` (около строки 50) и добавить сразу после неё:

```python
    review_session_persist: bool = True           # персист сессии PR в Postgres (crash-recovery)
    review_session_ttl_hours: int = 24            # TTL персистнутой сессии до истечения
```

- [ ] **Step 2: Сделать существующие unit-тесты герметичными**

В `tests/mcp/test_publish.py` в функции `_settings()` (после `s.review_history = True`) добавить:

```python
    s.review_session_persist = False     # unit-тесты не трогают Postgres-таблицу сессий
```

В `tests/mcp/test_service.py` в функции `_settings()` (после `s.review_history = False`) добавить ту же строку:

```python
    s.review_session_persist = False     # unit-тесты не трогают Postgres-таблицу сессий
```

(Тесты с `vcs_factory=lambda...` персист и так не включают; строка защищает кейс `vcs_factory=None`, например `test_publish_closes_internal_vcs`.)

- [ ] **Step 3: Запустить существующие тесты — убедиться, что зелёные после правок settings**

Run: `.venv/bin/pytest tests/mcp/test_publish.py tests/mcp/test_service.py -q`
Expected: PASS (поведение не изменилось; персист выключен в хелперах).

- [ ] **Step 4: Добавить импорты и поле `_session_store` в `reviewer/mcp/service.py`**

После строки `from reviewer.mcp` импортов / рядом с существующими импортами модуля добавить:

```python
from reviewer.mcp.session_serde import from_payload, to_payload
from reviewer.mcp.session_store import SessionStore
```

В `MCPReviewService.__init__`, после строки `self._sessions: dict[tuple[str, int], _Session] = {}` добавить:

```python
        self._session_store: SessionStore | None = None
```

- [ ] **Step 5: Добавить `_ensure_session_store` и `_rehydrate_session`**

Вставить эти методы в `MCPReviewService` (рядом с `_session`, до или после него):

```python
    def _ensure_session_store(self) -> SessionStore | None:
        """Ленивое хранилище подложки сессий (по образцу _ensure_history).

        Возвращает None (персист выключен), если ``review_session_persist`` ложно
        ИЛИ задан ``_vcs_factory`` (test-only: после рестарта фабрика недоступна,
        регидрация подняла бы реальный GitHubProvider — неверно для снапшота).
        Уже внедрённый ``_session_store`` (в тестах) возвращается как есть.
        """
        if self._session_store is not None:
            return self._session_store
        if self.settings.review_session_persist and self._vcs_factory is None:
            self._session_store = SessionStore(
                self.settings.pg_dsn,
                min_size=self.settings.pg_pool_min_size,
                max_size=self.settings.pg_pool_max_size,
            )
        return self._session_store

    def _rehydrate_session(self, repo: str, pr: int) -> _Session | None:
        """Восстановить сессию из Postgres при промахе in-memory кэша.

        Возвращает None, если персист выключен, строки нет/истёк TTL, БД
        недоступна или payload несовместим (fail-soft → вызывающий бросит
        ValueError с recovery hint).
        """
        store = self._ensure_session_store()
        if store is None:
            return None
        payload = store.load(repo, pr, self.settings.review_session_ttl_hours)
        if not payload:
            return None
        try:
            owner, name = repo.split("/", 1)
            vcs = self._review_service._create_vcs_provider(owner, name)
            prepared = from_payload(payload, vcs)
        except Exception:
            log.warning("Регидрация сессии %s#%s не удалась", repo, pr, exc_info=True)
            return None
        ctx = self._tool_context(prepared)
        return _Session(prepared, ctx)
```

- [ ] **Step 6: Включить регидрацию в `_session()`**

Заменить тело `_session` (текущие строки 185-192):

```python
    def _session(self, repo: str, pr: int) -> _Session:
        """Получить сессию или бросить ValueError с понятным сообщением."""
        s = self._sessions.get((repo, pr))
        if s is None:
            raise ValueError(
                f"Сессия для {repo}#{pr} не найдена — сначала вызови prepare_review"
            )
        return s
```

на:

```python
    def _session(self, repo: str, pr: int) -> _Session:
        """Получить сессию из кэша или регидрировать из Postgres (crash-recovery).

        При промахе in-memory кэша пробуем поднять персистнутую сессию; успех
        прогревает кэш. Полный промах (нет строки / истёк TTL / БД недоступна) —
        ValueError с recovery hint.
        """
        s = self._sessions.get((repo, pr))
        if s is not None:
            return s
        rehydrated = self._rehydrate_session(repo, pr)
        if rehydrated is not None:
            self._sessions[(repo, pr)] = rehydrated
            return rehydrated
        raise ValueError(
            f"Сессия для {repo}#{pr} не найдена или истекла — вызови prepare_review заново"
        )
```

- [ ] **Step 7: Писать сессию в store в `prepare_review`**

В `prepare_review`, сразу после строки `self._sessions[(repo, pr)] = _Session(prepared, ctx)` добавить:

```python
        store = self._ensure_session_store()
        if store is not None:
            store.save(repo, pr, to_payload(prepared))
```

- [ ] **Step 8: Удалять строку сессии в `_cleanup`**

В `_cleanup`, после блока удаления overlay (`self.components.store.delete_ref(repo, f"pr:{pr}")` с его try/except) добавить:

```python
        store = self._ensure_session_store()
        if store is not None:
            store.delete(repo, pr)
```

- [ ] **Step 9: Написать тесты `tests/mcp/test_session_persist.py`**

```python
"""Персистентность/регидрация сессии MCPReviewService (PRI-99).

Эмулируем рестарт процесса очисткой _sessions между prepare_review и
publish_review; проверяем регидрацию из (фейкового) SessionStore, промах →
ValueError с recovery hint, и fail-soft записи.
"""
from __future__ import annotations

import json

import pytest
from unittest.mock import MagicMock, patch

from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService

SOURCE_A = "y = 0\nx = 1\n"
PATCH_A = "@@ -1,1 +1,2 @@\n y = 0\n+x = 1"
RAW = {
    "category": "correctness", "severity": "high", "file": "a.py", "line": 2,
    "code_quote": "x = 1", "message": "bug here", "suggestion": None,
    "fix": None, "confidence": 0.9,
}


class _FakeSessionStore:
    """In-memory подложка: эмулирует JSONB через json round-trip."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, int], dict] = {}

    def save(self, repo, pr, payload):
        self.rows[(repo, pr)] = json.loads(json.dumps(payload, ensure_ascii=False))

    def load(self, repo, pr, ttl_hours):
        return self.rows.get((repo, pr))

    def delete(self, repo, pr):
        self.rows.pop((repo, pr), None)


class _FakeChangedFile:
    def __init__(self, path, status, patch):
        self.path, self.status, self.patch = path, status, patch


class _FakeVCS:
    def __init__(self, number=7):
        self._number = number
        self.published = []
        self.close_calls = 0

    def get_pull_request(self, number):
        from reviewer.vcs.base import PullRequest
        return PullRequest(number=self._number, base_sha="base123", head_sha="head456",
                           base_ref="main", title="T", body="", draft=False)

    def get_changed_files(self, number):
        return [_FakeChangedFile("a.py", "modified", PATCH_A)]

    def get_file_at_ref(self, path, ref):
        return SOURCE_A if path == "a.py" else None

    def list_existing_fingerprints(self, number):
        return set()

    def publish_review(self, number, head_sha, summary, comments):
        self.published.append({"summary": summary, "comments": list(comments)})

    def compare_files(self, base_sha, head_sha):
        return []

    def close(self):
        self.close_calls += 1


def _settings() -> Settings:
    s = Settings()
    s.review_history = False
    s.review_skip_drafts = True
    s.review_max_files = 50
    s.review_session_persist = True           # включаем персист в этом тесте
    s.review_session_ttl_hours = 24
    s.voyage_api_key = "test"
    s.github_token = "test"
    return s


def _components() -> MagicMock:
    c = MagicMock()
    c.store = MagicMock()
    c.embedder = MagicMock()
    c.retriever = MagicMock()
    c.retriever.retrieve.return_value.as_context.return_value = "(результат)"
    c.graph = MagicMock()
    c.graph.expand.return_value = set()
    c.graph.callers.return_value = set()
    c.graph.find_symbol.return_value = []
    return c


class _FakeChunk:
    def __init__(self, node_id):
        self.node_id = node_id


def _fake_chunk(path, source):
    return [_FakeChunk(f"{path}#foo")]


def _make_service(store):
    """Сервис с vcs_factory=None (персист включён); _create_vcs_provider пропатчен."""
    svc = MCPReviewService(_settings(), _components(), vcs_factory=None)
    svc._session_store = store                 # внедряем фейковую подложку
    return svc


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_after_restart_rehydrates_session(_ov, _ch) -> None:
    store = _FakeSessionStore()
    svc = _make_service(store)
    vcs = _FakeVCS()
    with patch.object(svc._review_service, "_create_vcs_provider", return_value=vcs):
        svc.prepare_review("o/r", 7)
        assert ("o/r", 7) in store.rows               # сессия персистнута
        svc._sessions.clear()                          # эмуляция рестарта процесса
        report = svc.publish_review("o/r", 7, summary="s", findings=[RAW], dry_run=True)
    assert report["inline"][0]["line"] == 2            # регидрация дала рабочую сессию
    assert ("o/r", 7) not in store.rows                # cleanup удалил персист


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_tool_after_restart_rehydrates_session(_ov, _ch) -> None:
    store = _FakeSessionStore()
    svc = _make_service(store)
    vcs = _FakeVCS()
    with patch.object(svc._review_service, "_create_vcs_provider", return_value=vcs):
        svc.prepare_review("o/r", 7)
        svc._sessions.clear()
        out = svc.read_file("o/r", 7, "a.py", 1, 10)   # тул через _session → регидрация
    assert "x = 1" in out
    assert ("o/r", 7) in svc._sessions                 # кэш прогрет регидрацией


def test_publish_miss_raises_with_recovery_hint() -> None:
    store = _FakeSessionStore()                          # пусто: prepare не вызывался
    svc = _make_service(store)
    with pytest.raises(ValueError, match="prepare_review"):
        svc.publish_review("o/r", 7, summary="s", findings=[RAW], dry_run=True)


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_persist_disabled_no_rehydration(_ov, _ch) -> None:
    """review_session_persist=False: store не задействуется, после рестарта — ValueError."""
    s = _settings()
    s.review_session_persist = False
    svc = MCPReviewService(s, _components(), vcs_factory=None)
    vcs = _FakeVCS()
    with patch.object(svc._review_service, "_create_vcs_provider", return_value=vcs):
        svc.prepare_review("o/r", 7)                       # _ensure_session_store() → None, без save
        svc._sessions.clear()                              # эмуляция рестарта
        with pytest.raises(ValueError, match="prepare_review"):
            svc.publish_review("o/r", 7, summary="s", findings=[RAW], dry_run=True)
```

> Контракт fail-soft записи проверяется на уровне **самого `SessionStore`** (Task 1, `test_session_store_failsoft_without_db`): `save`/`load`/`delete` ловят исключения внутри и не пробрасывают. Поэтому прод-код (`prepare_review`/`_cleanup`) вызывает их напрямую без обёртки try, и отдельный тест fail-soft на уровне сервиса не нужен.

- [ ] **Step 10: Запустить новые тесты**

Run: `.venv/bin/pytest tests/mcp/test_session_persist.py -v`
Expected: все PASS (регидрация для publish и для тула, промах→ValueError с hint, save fail-soft).

- [ ] **Step 11: Прогнать весь MCP-набор + затронутые модули**

Run: `.venv/bin/pytest tests/mcp -q`
Expected: PASS (существующие + новые).

- [ ] **Step 12: Линт**

Run: `.venv/bin/ruff check reviewer/config/settings.py reviewer/mcp/service.py tests/mcp/`
Expected: без ошибок.

- [ ] **Step 13: Commit**

```bash
git add reviewer/config/settings.py reviewer/mcp/service.py tests/mcp/test_session_persist.py tests/mcp/test_publish.py tests/mcp/test_service.py
git commit -m "feat(mcp): регидрация сессии ревью после рестарта reviewer-mcp (PRI-99)"
```

---

## Финальная проверка (после всех задач)

- [ ] **Полный unit-прогон:** `.venv/bin/pytest -q` → зелёно.
- [ ] **Integration (с поднятым Postgres):** `.venv/bin/pytest tests/mcp/test_session_store.py -m integration -q` → зелёно.
- [ ] **Линт всего:** `.venv/bin/ruff check reviewer/mcp reviewer/config/settings.py tests/mcp`.
- [ ] **Док-связь:** убедиться, что поведение совпадает со спекой `docs/superpowers/specs/2026-06-17-pri-99-session-persistence-design.md`.

## Self-Review (выполнено при написании плана)

**1. Покрытие спеки:**
- Компонент `SessionStore` + схема `review_sessions` (JSONB, PK (repo,pr), created_at) → Task 1. ✓
- Сериализатор `to_payload`/`from_payload`, исключающий `vcs` → Task 2. ✓
- `_ensure_session_store` (гейт `review_session_persist` + `_vcs_factory is None`), `save` в `prepare_review`, ленивая регидрация в `_session`, `delete` в `_cleanup` → Task 3. ✓
- Настройки `review_session_persist=True`, `review_session_ttl_hours=24` → Task 3 Step 1. ✓
- Fail-soft всех операций store → Task 1 (реализация) + Task 1 Step 4 (тест на уровне SessionStore). ✓
- Гейт `review_session_persist` (выкл → нет регидрации) → Task 3 Step 9 (`test_persist_disabled_no_rehydration`). ✓
- Edge case `vcs_factory` (персист только при `_vcs_factory is None`) → Task 3 Step 5 + герметизация тестов Step 2. ✓
- Recovery-hint текст ошибки → Task 3 Step 6 + тест Step 9. ✓
- Тесты: round-trip, регидрация (publish + тул), промах→ошибка, fail-soft, integration → Tasks 1–3. ✓
- Упаковка `.sql` для прод-деплоя (`uvx`) → Task 1 Step 3. ✓

**2. Плейсхолдеры:** нет TBD/TODO; весь код приведён целиком.

**3. Согласованность типов/имён:** `SessionStore.save/load(ttl_hours)/delete/close/init_schema`, `to_payload`/`from_payload`, `_ensure_session_store`/`_rehydrate_session` используются одинаково во всех задачах и тестах. Ключ — нормализованный `repo` ("owner/name") + `pr:int`. ✓

**Вне объёма (из спеки):** future-proof headless/HTTP, fallback без grounding, фоновый sweep, gzip — не реализуются.
