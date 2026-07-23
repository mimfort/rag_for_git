# PRI-212 Keepalive сессии ревью — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ревью, активно работающее дольше `review_session_ttl_hours`, не теряет свой overlay: живость сессии продлевается активностью (`last_seen_at` + `SessionStore.touch`), единый предикат `COALESCE(last_seen_at, created_at)` в `load`/`live_keys`/`delete_expired`.

**Architecture:** Аддитивная миграция колонки `last_seen_at` в `review_sessions`; fail-soft метод `SessionStore.touch()`; точка продления — `MCPReviewService._session()` (единственная воронка всех обращений к сессии) с in-memory бампом всегда и DB-touch не чаще 60 с; `_gc_overlays` фильтрует активные in-memory сессии по `last_seen_at` вместо `started_at`. `reviewer/services/gc.py` НЕ меняется.

**Tech Stack:** Python 3.11–3.13, psycopg/psycopg_pool (Postgres/ParadeDB), pytest, ruff.

**Спека:** `docs/superpowers/specs/2026-07-20-pri-212-session-keepalive-design.md`
**Бриф:** `docs/superpowers/briefs/2026-07-20-PRI-212-session-keepalive.md`

## Global Constraints

- Работа на ветке `feat/pri-212-session-keepalive` от `dev` (dev защищён PR-required).
- Язык кода, комментариев, докстрок, сообщений коммитов — **русский**. Conventional Commits (`feat(mcp): …`), **без Co-Authored-By / упоминаний Claude**.
- **НЕ трогать** `reviewer/services/gc.py` и `tests/services/test_gc.py` — алгоритм GC и его инварианты (порядок чтений T1→T2, «не знаю живых» ≠ «живых нет») нетронуты по спеке.
- Никаких новых ключей Settings: интервал троттлинга — модульная константа `_TOUCH_INTERVAL_S = 60` в `reviewer/mcp/service.py`.
- Unit-тесты — без Postgres/сети (`.venv/bin/pytest -q` по умолчанию исключает integration). Тесты с реальной БД — `@pytest.mark.integration`; они используют `Settings().pg_dsn` (dev ParadeDB на :5433, поднят `docker compose up -d paradedb`) по образцу существующих тестов в `tests/mcp/test_session_store.py`.
- `ruff check reviewer/mcp tests/mcp` — чисто (line-length 100; repo-wide чистоту не гнать — на main есть свои огрехи).
- Контракты ошибок не смешивать: `touch`/`save`/`delete_expired` — fail-soft; `live_keys` — осознанно НЕ fail-soft.

---

### Task 1: Схема + SessionStore — `last_seen_at`, `touch()`, единый предикат живости

**Files:**
- Modify: `reviewer/mcp/session_store.sql`
- Modify: `reviewer/mcp/session_store.py`
- Test: `tests/mcp/test_session_store.py`

**Interfaces:**
- Consumes: существующие `SessionStore._connect()`, `log` (модульный логгер), схему `review_sessions`.
- Produces: `SessionStore.touch(repo: str, pr: int) -> None` — fail-soft UPDATE `last_seen_at = now()`, НЕ создаёт строку. Семантика TTL всех читающих методов: живость = `COALESCE(last_seen_at, created_at)` внутри окна `ttl_hours`. Task 2 вызывает `touch()` из сервисного слоя.

- [ ] **Step 1: Создать ветку**

```bash
git checkout dev && git checkout -b feat/pri-212-session-keepalive
```

- [ ] **Step 2: Написать красные тесты**

В `tests/mcp/test_session_store.py` добавить в конец файла:

```python
def test_touch_failsoft_without_db(monkeypatch) -> None:
    """PRI-212: touch fail-soft — сбой БД не роняет tool call вызывающего."""
    store = SessionStore("postgresql://invalid/none")

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "_connect", boom)
    store.touch("o/r", 1)  # не бросает


@pytest.mark.integration
def test_touch_extends_liveness_unified_predicate() -> None:
    """PRI-212: touch продлевает живость для ВСЕХ трёх предикатов сразу.

    Строка с состаренным created_at и NULL last_seen_at (legacy-семантика)
    невидима для live_keys/load и удаляема delete_expired; после touch —
    жива везде. Back-date'им ТОЛЬКО свою строку и зовём delete_expired(24)
    с прод-семантикой (см. предостережение в test_live_keys_and_delete_expired).
    """
    pg_dsn = Settings().pg_dsn
    store = SessionStore(pg_dsn)
    store.init_schema()
    repo, pr = "owner/keepalive-test", 997
    store.delete(repo, pr)
    try:
        store.save(repo, pr, {"repo": repo})
        with store._connect() as conn:
            conn.execute(
                "UPDATE review_sessions SET created_at = now() - interval '48 hours', "
                "last_seen_at = NULL WHERE repo=%s AND pr_number=%s",
                (repo, pr),
            )
            conn.commit()
        # legacy-строка (NULL last_seen_at): живость по created_at — просрочена
        assert (repo, pr) not in store.live_keys(24)
        assert store.load(repo, pr, 24) is None

        store.touch(repo, pr)  # активность → строка снова жива везде
        assert (repo, pr) in store.live_keys(24)
        assert store.load(repo, pr, 24) is not None
        store.delete_expired(24)  # не удаляет строку со свежим last_seen_at
        assert store.load(repo, pr, 24) is not None
    finally:
        store.delete(repo, pr)
        store.close()


@pytest.mark.integration
def test_touch_does_not_create_row() -> None:
    """PRI-212: touch по несуществующему ключу — no-op, строка не создаётся.

    Если персист упал ещё на save(), сессию страхует in-memory-путь
    (active_keys в _gc_overlays) — touch не должен рожать строку-огрызок.
    """
    pg_dsn = Settings().pg_dsn
    store = SessionStore(pg_dsn)
    store.init_schema()
    repo, pr = "owner/keepalive-test", 996
    store.delete(repo, pr)
    try:
        store.touch(repo, pr)
        assert store.load(repo, pr, 24) is None
        assert (repo, pr) not in store.live_keys(24)
    finally:
        store.delete(repo, pr)
        store.close()
```

- [ ] **Step 3: Убедиться, что тесты красные**

```bash
.venv/bin/pytest tests/mcp/test_session_store.py::test_touch_failsoft_without_db -q
```
Ожидание: FAIL, `AttributeError: 'SessionStore' object has no attribute 'touch'`.

- [ ] **Step 4: Миграция схемы**

`reviewer/mcp/session_store.sql` — целиком заменить содержимое на:

```sql
-- Подложка in-memory MCPReviewService._sessions: переживает рестарт процесса
-- reviewer-mcp между prepare_review и publish_review. Применяется идемпотентно.
CREATE TABLE IF NOT EXISTS review_sessions (
    repo       TEXT        NOT NULL,
    pr_number  INTEGER     NOT NULL,
    payload    JSONB       NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- PRI-212: последняя активность сессии (keepalive). NULL у legacy-строк —
    -- для них живость считается по created_at (COALESCE в предикатах чтения).
    last_seen_at TIMESTAMPTZ,
    PRIMARY KEY (repo, pr_number)
);

CREATE INDEX IF NOT EXISTS review_sessions_created_idx ON review_sessions (created_at);

-- PRI-212: аддитивная идемпотентная миграция уже существующих таблиц
-- (по прецеденту миграций review_findings).
ALTER TABLE review_sessions ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ;
```

- [ ] **Step 5: SessionStore — touch + единый предикат**

В `reviewer/mcp/session_store.py`:

5a. В `save()` заменить SQL (метод остаётся fail-soft, тело без изменений кроме строки SQL):

```python
        sql = """
        INSERT INTO review_sessions (repo, pr_number, payload, created_at, last_seen_at)
        VALUES (%s, %s, %s::jsonb, now(), now())
        ON CONFLICT (repo, pr_number)
        DO UPDATE SET payload = EXCLUDED.payload, created_at = now(), last_seen_at = now()
        """
```

5b. В `load()` заменить условие TTL в SQL:

```python
        sql = """
        SELECT payload FROM review_sessions
        WHERE repo = %s AND pr_number = %s
          AND COALESCE(last_seen_at, created_at) > now() - make_interval(hours => %s)
        """
```

5c. В `live_keys()` заменить условие TTL в SQL (докстроку про НЕ fail-soft не трогать):

```python
        sql = """
        SELECT repo, pr_number FROM review_sessions
        WHERE COALESCE(last_seen_at, created_at) > now() - make_interval(hours => %s)
        """
```

5d. В `delete_expired()` заменить SQL:

```python
        sql = (
            "DELETE FROM review_sessions "
            "WHERE COALESCE(last_seen_at, created_at) <= now() - make_interval(hours => %s)"
        )
```

5e. Добавить метод `touch()` после `load()` (перед `delete()`):

```python
    def touch(self, repo: str, pr: int) -> None:
        """Продлить живость сессии активностью (keepalive, PRI-212). Fail-soft.

        Не создаёт строку: если персист упал ещё на save(), сессия существует
        только в памяти процесса — её страхует in-memory-путь (active_keys
        в MCPReviewService._gc_overlays), а не строка-огрызок без payload.
        """
        sql = "UPDATE review_sessions SET last_seen_at = now() WHERE repo = %s AND pr_number = %s"
        try:
            with self._connect() as conn:
                conn.execute(sql, (repo, pr))
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось продлить сессию %s#%s: %s", repo, pr, exc)
```

5f. В докстроке класса `SessionStore` заменить последнее предложение
«TTL применяется на чтении через условие ``WHERE``.» на:

```
    TTL применяется на чтении через условие ``WHERE`` и считается от последней
    активности: живость = ``COALESCE(last_seen_at, created_at)`` внутри окна
    (PRI-212, keepalive) — активное ревью продлевает себя, брошенное истекает.
```

- [ ] **Step 6: Прогнать unit-тесты**

```bash
.venv/bin/pytest tests/mcp/test_session_store.py -q
```
Ожидание: PASS (integration-тесты пропущены по умолчанию).

- [ ] **Step 7: Прогнать integration-тесты (dev ParadeDB должен быть поднят)**

```bash
docker compose up -d paradedb
.venv/bin/pytest tests/mcp/test_session_store.py -m integration -q
```
Ожидание: PASS все 4 (2 старых + 2 новых).

- [ ] **Step 8: Commit**

```bash
git add reviewer/mcp/session_store.sql reviewer/mcp/session_store.py tests/mcp/test_session_store.py
git commit -m "feat(mcp): SessionStore.touch и живость по COALESCE(last_seen_at, created_at) (PRI-212)"
```

---

### Task 2: Сервисный слой — продление живости на каждом обращении к сессии

**Files:**
- Modify: `reviewer/mcp/service.py` (dataclass `_Session` ~строки 53-74; `_session` ~247-263; `_gc_overlays` ~1248-1278; константы ~48-50)
- Modify: `tests/mcp/test_gc_on_prepare.py`
- Create: `tests/mcp/test_session_keepalive.py`

**Interfaces:**
- Consumes: `SessionStore.touch(repo, pr)` из Task 1; существующие `_ensure_session_store()`, `_rehydrate_session()`.
- Produces: поля `_Session.last_seen_at: datetime` (default_factory now, UTC) и `_Session.db_touched_at: datetime | None`; константа `_TOUCH_INTERVAL_S = 60`; приватный `MCPReviewService._touch_session(repo, pr, s) -> None`. `_gc_overlays` фильтрует active по `s.last_seen_at`. Никаких изменений публичного API MCP.

- [ ] **Step 1: Красный тест сценария PRI-212 + адаптация существующих GC-тестов**

В `tests/mcp/test_gc_on_prepare.py`:

1a. В `_FakeSessionStore` добавить метод (после `delete()`):

```python
    def touch(self, repo: str, pr: int) -> None:
        self.calls.append(("touch", repo, pr))
```

1b. В `test_prepare_keeps_overlay_of_active_in_memory_session` после строки
`live_session.started_at = datetime.now(timezone.utc)` добавить:

```python
    live_session.last_seen_at = datetime.now(timezone.utc)  # и активность свежая
```

1c. В `test_prepare_purges_overlay_of_stale_in_memory_session_past_ttl` после блока
`stale_session.started_at = (...)` добавить (MagicMock иначе отдаст truthy-заглушку
на сравнение `last_seen_at > cutoff`, и тест молча сломается):

```python
    stale_session.last_seen_at = stale_session.started_at  # активности не было
```

1d. Добавить новый тест в конец файла:

```python
@_PATCH_TO_PAYLOAD
def test_prepare_keeps_overlay_of_long_running_active_session(_to_payload):
    """PRI-212 (ядро задачи): активное ревью дольше TTL не теряет свой overlay.

    started_at старше TTL (ревью началось давно), но last_seen_at свежий
    (обращения к тулам продолжаются) — GC обязан считать сессию живой и не
    трогать pr:7. До keepalive фильтр шёл по started_at и сносил overlay
    прямо из-под работающего анализа.
    """
    c = _components()
    svc = _service(c)
    long_session = MagicMock()
    long_session.started_at = (
        datetime.now(timezone.utc)
        - timedelta(hours=svc.settings.review_session_ttl_hours + 1)
    )
    long_session.last_seen_at = datetime.now(timezone.utc)  # активность прямо сейчас
    svc._sessions[("a/x", 7)] = long_session

    svc.prepare_review("a/x", 8)

    assert "pr:7" not in c.store.deleted_refs
```

- [ ] **Step 2: Красные тесты keepalive-механики**

Создать `tests/mcp/test_session_keepalive.py`:

```python
"""PRI-212: keepalive сессии — обращения продлевают живость.

_session() бампает last_seen_at in-memory на каждом обращении и продлевает
строку в Postgres (SessionStore.touch) не чаще _TOUCH_INTERVAL_S. Сбой БД
не роняет обращение (touch fail-soft на стороне SessionStore); при
выключенном персисте продление остаётся чисто in-memory.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from reviewer.config.settings import Settings
from reviewer.mcp.service import _TOUCH_INTERVAL_S, MCPReviewService, _Session


class _TouchLog:
    """Фейковый стор: журналирует только touch (другие методы не нужны тесту)."""

    def __init__(self) -> None:
        self.touched: list[tuple[str, int]] = []

    def touch(self, repo: str, pr: int) -> None:
        self.touched.append((repo, pr))


def _svc_with_session() -> tuple[MCPReviewService, _Session, _TouchLog]:
    s = Settings()
    s.voyage_api_key = "test"
    s.github_token = "test"
    s.review_session_persist = True
    svc = MCPReviewService(s, MagicMock())
    store = _TouchLog()
    svc._session_store = store
    sess = _Session(prepared=MagicMock(), ctx=MagicMock())
    svc._sessions[("a/x", 7)] = sess
    return svc, sess, store


def test_access_bumps_last_seen_and_touches_db():
    """Обращение к сессии бампает in-memory last_seen_at и делает DB-touch."""
    svc, sess, store = _svc_with_session()
    before = sess.last_seen_at

    svc._session("a/x", 7)

    assert sess.last_seen_at >= before
    assert store.touched == [("a/x", 7)]


def test_db_touch_throttled_within_interval():
    """Два обращения подряд → один DB-touch (троттлинг _TOUCH_INTERVAL_S)."""
    svc, sess, store = _svc_with_session()

    svc._session("a/x", 7)
    svc._session("a/x", 7)

    assert store.touched == [("a/x", 7)]


def test_db_touch_repeats_after_interval():
    """Интервал истёк → следующий DB-touch проходит."""
    svc, sess, store = _svc_with_session()

    svc._session("a/x", 7)
    sess.db_touched_at = datetime.now(timezone.utc) - timedelta(
        seconds=_TOUCH_INTERVAL_S + 1
    )
    svc._session("a/x", 7)

    assert store.touched == [("a/x", 7), ("a/x", 7)]


def test_in_memory_bump_without_store():
    """Персист выключен → продление чисто in-memory, без ошибок."""
    svc, sess, _ = _svc_with_session()
    svc._session_store = None
    svc.settings.review_session_persist = False
    before = sess.last_seen_at

    svc._session("a/x", 7)  # не бросает

    assert sess.last_seen_at >= before
```

- [ ] **Step 3: Убедиться, что тесты красные**

```bash
.venv/bin/pytest tests/mcp/test_session_keepalive.py tests/mcp/test_gc_on_prepare.py -q
```
Ожидание: FAIL — `test_session_keepalive.py` падает на импорте
(`ImportError: cannot import name '_TOUCH_INTERVAL_S'`),
`test_prepare_keeps_overlay_of_long_running_active_session` — на
`assert "pr:7" not in ...` (фильтр всё ещё по `started_at`).

- [ ] **Step 4: Реализация в `reviewer/mcp/service.py`**

4a. После константы `_MAX_SESSION_STEPS = 1000` добавить:

```python
# PRI-212: DB-touch живости сессии (SessionStore.touch) — не чаще раза в
# _TOUCH_INTERVAL_S. Тулы зовутся LLM-темпом; при TTL в часах минутная
# гранулярность ничего не теряет и убирает бессмысленно частые UPDATE.
_TOUCH_INTERVAL_S = 60
```

4b. В dataclass `_Session` после поля `started_at` (перед `_seq: int = 0`) добавить:

```python
    # PRI-212: последняя активность сессии (keepalive) — бампается на каждом
    # обращении через _session(); по ней _gc_overlays фильтрует active_keys.
    # started_at остаётся чистым «моментом создания» (duration_ms в истории).
    last_seen_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # PRI-212: момент последнего DB-touch (троттлинг _TOUCH_INTERVAL_S).
    db_touched_at: datetime | None = None
```

4c. Заменить метод `_session` целиком (сообщение ValueError сохранить дословно) и
добавить `_touch_session` сразу после него:

```python
    def _session(self, repo: str, pr: int) -> _Session:
        """Получить сессию из кэша или регидрировать из Postgres (crash-recovery).

        При промахе in-memory кэша пробуем поднять персистнутую сессию; успех
        прогревает кэш. Полный промах (нет строки / истёк TTL / БД недоступна) —
        ValueError с recovery hint.

        PRI-212 (keepalive): каждое обращение продлевает живость сессии —
        in-memory всегда (last_seen_at), в Postgres не чаще _TOUCH_INTERVAL_S
        (SessionStore.touch fail-soft: сбой БД не роняет обращение).
        """
        s = self._sessions.get((repo, pr))
        if s is None:
            s = self._rehydrate_session(repo, pr)
            if s is None:
                raise ValueError(
                    f"Сессия для {repo}#{pr} не найдена или истекла — вызови prepare_review заново"
                )
            self._sessions[(repo, pr)] = s
        self._touch_session(repo, pr, s)
        return s

    def _touch_session(self, repo: str, pr: int, s: _Session) -> None:
        """Продлить живость сессии активностью (PRI-212)."""
        now = datetime.now(timezone.utc)
        s.last_seen_at = now
        if (
            s.db_touched_at is not None
            and (now - s.db_touched_at).total_seconds() < _TOUCH_INTERVAL_S
        ):
            return
        store = self._ensure_session_store()
        if store is not None:
            store.touch(repo, pr)  # fail-soft внутри SessionStore
        s.db_touched_at = now
```

4d. В `_gc_overlays` заменить строку

```python
            active = {k for k, s in self._sessions.items() if s.started_at > cutoff}
```

на

```python
            active = {k for k, s in self._sessions.items() if s.last_seen_at > cutoff}
```

и в его докстроке заменить фрагмент «active_keys ограничены тем же TTL, что и
персистнутые сессии (по _Session.started_at), а не всем содержимым self._sessions.»
на «active_keys ограничены тем же TTL, что и персистнутые сессии (по
_Session.last_seen_at — последней активности, PRI-212), а не всем содержимым
self._sessions.»

- [ ] **Step 5: Прогнать целевые тесты**

```bash
.venv/bin/pytest tests/mcp/test_session_keepalive.py tests/mcp/test_gc_on_prepare.py -q
```
Ожидание: PASS все (включая C4-якорь `test_prepare_purges_overlay_of_stale_in_memory_session_past_ttl`).

- [ ] **Step 6: Прогнать полный unit-набор (регрессии tools/submit/publish, которые ходят через `_session`)**

```bash
.venv/bin/pytest -q
```
Ожидание: PASS. Если какой-то mcp-тест падает на `AttributeError: ... touch` у
самодельного фейк-стора — добавить в этот фейк no-op метод `def touch(self, repo, pr): ...`
по образцу шага 1a (это единственный допустимый тип правки чужих тестов здесь).

- [ ] **Step 7: Commit**

```bash
git add reviewer/mcp/service.py tests/mcp/test_gc_on_prepare.py tests/mcp/test_session_keepalive.py
git commit -m "feat(mcp): keepalive сессии — обращения продлевают живость overlay (PRI-212)"
```

---

### Task 3: Документация + финальная проверка

**Files:**
- Modify: `CLAUDE.md` (абзац «Overlay удаляется автоматически», ~строки 129-137)
- Modify: `README.ru.md` (абзац про GC в разделе потока ревью, ~строки 900-903)
- Modify: `README.md` (абзац «If a review is abandoned…», ~строки 127-129)

**Interfaces:**
- Consumes: терминологию Task 1–2 (`last_seen_at`, `SessionStore.touch`, idle-семантика TTL).
- Produces: только документация; кода не меняет.

- [ ] **Step 1: CLAUDE.md**

В абзаце `- **Overlay удаляется автоматически**` заменить предложение
«Сирота = `pr:N` без непросроченной строки в `review_sessions` (TTL `review_session_ttl_hours`) и вне активных сессий процесса.» на:

```
Сирота = `pr:N` без живой строки в `review_sessions` и вне активных сессий процесса.
Живость — по последней активности (PRI-212, keepalive): обращения к сессии бампают
`last_seen_at` (in-memory всегда; в Postgres — `SessionStore.touch`, не чаще 60 с),
предикат везде `COALESCE(last_seen_at, created_at)` внутри TTL `review_session_ttl_hours`
(idle-таймаут, единый для GC, регидрации и `delete_expired`) — активное ревью дольше TTL
не теряет overlay, брошенное собирается как прежде.
```

- [ ] **Step 2: README.ru.md**

После предложения «…такой overlay собирает GC: оппортунистически при следующем `prepare_review` и по команде `reviewer gc`.» добавить:

```
Живость сессии продлевается активностью (keepalive): обращения к тулам ревью обновляют
`last_seen_at`, поэтому ревью, работающее дольше `review_session_ttl_hours`, не теряет свой
overlay; ревью без активности по-прежнему собирается GC по истечении TTL.
```

- [ ] **Step 3: README.md**

После предложения «…such an overlay is collected by GC: opportunistically on the next `prepare_review`, and via the `reviewer gc` command.» добавить:

```
Session liveness is extended by activity (keepalive): review tool calls bump
`last_seen_at`, so a review running longer than `review_session_ttl_hours` keeps its
overlay; an idle review is still collected once the TTL elapses.
```

- [ ] **Step 4: Линт и полный прогон**

```bash
.venv/bin/ruff check reviewer/mcp tests/mcp
.venv/bin/pytest -q
```
Ожидание: ruff чисто; тесты PASS.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md README.md README.ru.md
git commit -m "docs: keepalive сессии ревью в CLAUDE.md и обоих README (PRI-212)"
```
