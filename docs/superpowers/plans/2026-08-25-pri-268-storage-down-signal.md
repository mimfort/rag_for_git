# PRI-268 — сигнал о недоступном хранилище: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `prepare_task_context` при лежащих хранилищах отвечает за один таймаут пула вместо десятков минут и называет в `gaps` причину и лекарство, а скилл `solve-task` больше не собирает бриф на дырявом контексте молча.

**Architecture:** Новый модуль `reviewer/storage_health.py` в корне пакета классифицирует исключение по типу (не по тексту) и решает, уместен ли совет `reviewer start`. `task_context.py` кладёт класс причины в `gaps` и замыкает остальные секции после первой недоступности хранилища. `TaskService.index_batch`/`refresh_meta_batch` прекращают ходить в пул после первого сбоя соединения, сохраняя длину результата. `preflight.md` получает шаг, который останавливает сборку и спрашивает пользователя.

**Tech Stack:** Python 3.13, psycopg / psycopg_pool, neo4j, pytest, click.

**Spec:** `docs/superpowers/specs/2026-08-25-pri-268-storage-down-signal-design.md`

## Global Constraints

- Ветка работы: `feat/pri-268-storage-down-signal` (создана от `dev`). Не переключаться на другие ветки.
- Язык кода, комментариев и докстрингов — **русский**. Так написан весь проект.
- Сообщения коммитов — Conventional Commits **на русском** (`feat(tasks): …`, `fix(mcp): …`). **Без self-attribution**: никаких `Co-Authored-By`, никаких упоминаний Claude.
- Unit-тестам **запрещены** внешние и localhost-сокеты (`tests/infrastructure_policy.py`). Всё на фейках. Любой тест с реальной сетью обязан иметь `@pytest.mark.integration` — в этом плане таких нет.
- Прогон тестов: `.venv/bin/pytest -q` (по умолчанию исключает integration через `addopts = -m 'not integration'` в `pyproject.toml`).
- Линт: `.venv/bin/ruff check reviewer tests`.
- **Voyage API сейчас отдаёт HTTP 403.** Ни один шаг верификации не должен звать `reviewer index`, `reviewer search`, `search_codebase` или что-либо ещё, требующее эмбеддингов. Все проверки этого плана — offline unit-тесты.
- Любая правка контента под `plugin/` обязывает прогнать `.venv/bin/python scripts/update_codex_plugin_manifest.py`, иначе install-тесты краснеют.
- Решение о недоступности принимается по **типу** исключения. Ни `str(exc)`, ни `exc.args` не попадают в `gaps`: в тексте `psycopg.OperationalError` живёт DSN с паролем.

---

### Task 1: Модуль `reviewer/storage_health.py` и вынос loopback-предиката из CLI

**Files:**
- Create: `reviewer/storage_health.py`
- Create: `tests/test_storage_health.py`
- Modify: `reviewer/entrypoints/cli.py:813-829` (удалить `_LOOPBACK_HOSTS` и `_is_loopback_endpoint`, заменить импортом-алиасом)

**Interfaces:**
- Consumes: ничего (первая задача).
- Produces: `reviewer.storage_health.is_storage_unavailable(exc: BaseException) -> bool`, `reviewer.storage_health.is_loopback_endpoint(value: str) -> bool`, `reviewer.storage_health.storage_remedy(*endpoints: str) -> str | None`, константы `CAUSE_STORAGE_UNAVAILABLE = "storage_unavailable"`, `CAUSE_UNKNOWN = "unknown"`, `REMEDY_START = "reviewer start"`. Задачи 2 и 3 импортируют отсюда.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_storage_health.py`:

```python
"""Unit-тесты классификатора недоступности хранилищ (PRI-268)."""
import psycopg
import psycopg_pool
import pytest
from neo4j.exceptions import AuthError, ServiceUnavailable, SessionExpired

from reviewer import storage_health as sh


def test_pool_timeout_is_storage_unavailable():
    """PoolTimeout — подкласс OperationalError, ради него всё и затевалось."""
    assert sh.is_storage_unavailable(
        psycopg_pool.PoolTimeout("couldn't get a connection after 30.00 sec"))


def test_operational_error_is_storage_unavailable():
    assert sh.is_storage_unavailable(psycopg.OperationalError("connection refused"))


def test_programming_error_is_not_storage_unavailable():
    """Настоящий баг SQL не должен маскироваться под «хранилище лежит»."""
    assert not sh.is_storage_unavailable(psycopg.ProgrammingError("syntax error at or near"))


@pytest.mark.parametrize("exc", [ServiceUnavailable("no routing servers"),
                                 SessionExpired("session expired")])
def test_neo4j_driver_errors_are_storage_unavailable(exc):
    assert sh.is_storage_unavailable(exc)


def test_neo4j_auth_error_is_not_storage_unavailable():
    """AuthError — это неверные креды, а не лежачее хранилище; лечится не reviewer start."""
    assert not sh.is_storage_unavailable(AuthError("unauthorized"))


def test_unrelated_exception_is_not_storage_unavailable():
    assert not sh.is_storage_unavailable(RuntimeError("boom"))


@pytest.mark.parametrize("endpoint", [
    "postgresql://u:p@127.0.0.1:5433/reviewer",
    "postgresql://u:p@localhost:5433/reviewer",
    "bolt://localhost:7687",
    "bolt://[::1]:7687",
    "host=127.0.0.1 port=5433 dbname=reviewer",
])
def test_loopback_endpoint_gets_remedy(endpoint):
    assert sh.storage_remedy(endpoint) == sh.REMEDY_START


@pytest.mark.parametrize("endpoint", [
    "postgresql://u:p@db.example.com:5432/reviewer",
    "bolt://neo4j.internal:7687",
    "host=db.example.com port=5432 dbname=reviewer",
])
def test_remote_endpoint_gets_no_remedy(endpoint):
    """Удалённому деплою локальный docker-стек ничего не чинит (критерий 3)."""
    assert sh.storage_remedy(endpoint) is None


def test_remedy_when_at_least_one_endpoint_is_local():
    assert sh.storage_remedy("postgresql://u:p@db.example.com:5432/reviewer",
                             "bolt://localhost:7687") == sh.REMEDY_START


def test_no_endpoints_means_no_remedy():
    assert sh.storage_remedy() is None
    assert sh.storage_remedy("") is None
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run: `.venv/bin/pytest tests/test_storage_health.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'reviewer.storage_health'`

- [ ] **Step 3: Написать модуль**

Создать `reviewer/storage_health.py`:

```python
"""Классификация недоступности хранилищ и совет по лечению (PRI-268).

Модуль лежит в корне пакета, а не в `mcp/` или `tasks/`: его потребители —
`reviewer/mcp/task_context.py`, `reviewer/tasks/service.py` и
`reviewer/entrypoints/cli.py`, общего подпакета ниже них нет, а импорт
`entrypoints.cli` из сервисного слоя развернул бы направление зависимости.

Решение принимается по ТИПУ исключения, а не по его тексту: в тексте
`psycopg.OperationalError` живёт DSN с паролем. Этим модуль отличается от
соседнего `reviewer/config/fetch_errors.py`, который судит по именам классов в
MRO: там исключения приходят от сменного VCS-клиента и модуль обязан остаться
без зависимостей, здесь — от жёстко закреплённых драйверов, без которых проект
не работает вовсе, и точность важнее развязки.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

import psycopg
from neo4j.exceptions import ServiceUnavailable, SessionExpired

CAUSE_STORAGE_UNAVAILABLE = "storage_unavailable"
CAUSE_UNKNOWN = "unknown"
REMEDY_START = "reviewer start"

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})


def is_loopback_endpoint(value: str) -> bool:
    """Адресован ли DSN/URI локальной машине.

    Нужен, чтобы совет `reviewer start` не показывался деплою с удалёнными
    хранилищами: там локальный docker-стек ничего не чинит.
    """
    try:
        host = urlsplit(value).hostname
    except ValueError:
        return False
    if host is None:
        match = re.search(r"host=([^\s]+)", value)
        host = match.group(1) if match else None
    return (host or "").lower() in _LOOPBACK_HOSTS


def is_storage_unavailable(exc: BaseException) -> bool:
    """Не отвечает ли хранилище — в отличие от прочих сбоев секции.

    `psycopg_pool.PoolTimeout` является подклассом `psycopg.OperationalError`,
    поэтому одна проверка покрывает и таймаут пула, и обрыв соединения.
    `psycopg.ProgrammingError` подклассом не является и под «хранилище лежит»
    не маскируется: настоящий баг SQL обязан остаться видимым. У neo4j по той же
    причине берутся только `ServiceUnavailable`/`SessionExpired` (ветка
    `DriverError`), но не `AuthError` — неверные креды лечатся не запуском
    контейнеров.
    """
    return isinstance(exc, (psycopg.OperationalError, ServiceUnavailable, SessionExpired))


def storage_remedy(*endpoints: str) -> str | None:
    """Команда-лекарство, если хоть один эндпоинт локальный, иначе None."""
    if any(is_loopback_endpoint(endpoint) for endpoint in endpoints if endpoint):
        return REMEDY_START
    return None
```

- [ ] **Step 4: Прогнать тест и убедиться, что он проходит**

Run: `.venv/bin/pytest tests/test_storage_health.py -q`
Expected: PASS (17 passed)

- [ ] **Step 5: Переключить `cli.py` на новый модуль**

В `reviewer/entrypoints/cli.py` удалить блок строк 813-829 целиком (константу `_LOOPBACK_HOSTS` и функцию `_is_loopback_endpoint` вместе с докстрингом) и добавить импорт в шапку модуля, рядом с прочими `from reviewer...`:

```python
from reviewer.storage_health import is_loopback_endpoint as _is_loopback_endpoint
```

Алиас под старым именем оставлен намеренно: два существующих теста
(`tests/entrypoints/test_infra_commands.py:158,167`) и два call-сайта в `reviewer check`
(строки 906 и 922) продолжают работать без правок.

- [ ] **Step 6: Убедиться, что CLI-тесты и линт зелёные**

Run: `.venv/bin/pytest tests/entrypoints/test_infra_commands.py tests/test_storage_health.py -q && .venv/bin/ruff check reviewer tests`
Expected: PASS, ruff без замечаний. Если ruff ругается на неиспользуемые `re`/`urlsplit` в `cli.py` — проверить, что они используются в других местах файла (строка 800 использует `re`), и убрать импорт только если он действительно осиротел.

- [ ] **Step 7: Коммит**

```bash
git add reviewer/storage_health.py tests/test_storage_health.py reviewer/entrypoints/cli.py
git commit -m "feat(storage): классификатор недоступности хранилищ и общий loopback-предикат"
```

---

### Task 2: Класс причины в `gaps` и короткое замыкание секций

**Files:**
- Modify: `reviewer/mcp/task_context.py:27-40` (`gap`, `_safe`) и `:71-119` (`build_task_context`)
- Modify: `reviewer/mcp/service.py:3520-3536` (добавить `_TaskContextDeps.storage_endpoints`)
- Test: `tests/mcp/test_prepare_task_context.py` (расширить `FakeDeps`, добавить тесты)

**Interfaces:**
- Consumes: `reviewer.storage_health.is_storage_unavailable`, `.storage_remedy`, `.CAUSE_STORAGE_UNAVAILABLE`, `.CAUSE_UNKNOWN` из Task 1.
- Produces: форма записи `gaps` — `{"section": str, "reason": str, "cause": str, "remedy": str | None}`. Task 4 (скилл) ветвится по `cause` и `remedy`.

- [ ] **Step 1: Написать падающие тесты**

В `tests/mcp/test_prepare_task_context.py` добавить импорт в шапку файла:

```python
import psycopg
```

Расширить `FakeDeps` (класс на строке 5) одним методом — дописать его после `preflight`:

```python
    def storage_endpoints(self):
        return ("postgresql://u:p@127.0.0.1:5433/reviewer", "bolt://localhost:7687")
```

Добавить в конец файла тесты:

```python
def test_storage_failure_names_cause_and_remedy():
    """Критерий 1: в gaps названы и причина, и команда лечения."""
    deps = FakeDeps(preflight=psycopg.OperationalError("connection refused"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-268", branch="dev", warm_board=False)
    entry = next(g for g in payload["gaps"] if g["section"] == "preflight")
    assert entry["cause"] == "storage_unavailable"
    assert entry["remedy"] == "reviewer start"


def test_other_failure_keeps_cause_unknown():
    """Критерий 2: «хранилище не отвечает» и прочий сбой — разные записи."""
    deps = FakeDeps(preflight=RuntimeError("no index"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-268", branch="dev", warm_board=False)
    entry = next(g for g in payload["gaps"] if g["section"] == "preflight")
    assert entry["cause"] == "unknown"
    assert entry["remedy"] is None


def test_storage_failure_short_circuits_remaining_sections():
    """Критерий 1: остальные store-секции не вызываются — иначе +30 с каждая."""
    deps = FakeDeps(preflight=psycopg.OperationalError("connection refused"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-268", branch="dev", warm_board=False)
    assert deps.calls == ["preflight"]
    assert set(payload) == set(task_context.SECTIONS)
    assert all(g["cause"] == "storage_unavailable" for g in payload["gaps"])


def test_short_circuit_does_not_fire_on_other_causes():
    """Сбой не-хранилища остальные секции не отменяет: fail-open как прежде."""
    deps = FakeDeps(preflight=RuntimeError("no index"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-268", branch="dev", warm_board=False)
    assert "code" in deps.calls
    assert payload["code"]


def test_remote_deploy_gets_cause_without_remedy():
    """Критерий 3: совет reviewer start не выдаётся удалённым эндпоинтам."""
    class RemoteDeps(FakeDeps):
        def storage_endpoints(self):
            return ("postgresql://u:p@db.example.com:5432/reviewer",
                    "bolt://neo4j.internal:7687")

    deps = RemoteDeps(preflight=psycopg.OperationalError("connection refused"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-268", branch="dev", warm_board=False)
    entry = next(g for g in payload["gaps"] if g["section"] == "preflight")
    assert entry["cause"] == "storage_unavailable"
    assert entry["remedy"] is None


def test_deps_without_storage_endpoints_still_work():
    """Провайдер без нового метода не ломает сборку — remedy просто пуст."""
    class OldDeps(FakeDeps):
        storage_endpoints = None

    deps = OldDeps(preflight=psycopg.OperationalError("connection refused"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-268", branch="dev", warm_board=False)
    entry = next(g for g in payload["gaps"] if g["section"] == "preflight")
    assert entry["cause"] == "storage_unavailable"
    assert entry["remedy"] is None


def test_existing_gaps_keep_section_and_reason():
    """Расширение аддитивно: прежние ключи записи на месте."""
    payload = task_context.build_task_context(
        FakeDeps(subsystems=RuntimeError("нет сводок")), repo="o/n",
        key="PRI-268", branch="dev", warm_board=False)
    entry = next(g for g in payload["gaps"] if g["section"] == "subsystems")
    assert entry["reason"] == "сводки подсистем недоступны"
    assert set(entry) == {"section", "reason", "cause", "remedy"}
```

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run: `.venv/bin/pytest tests/mcp/test_prepare_task_context.py -q`
Expected: FAIL — `KeyError: 'cause'` в новых тестах; старые 30 тестов проходят.

- [ ] **Step 3: Реализовать в `task_context.py`**

В шапку `reviewer/mcp/task_context.py` добавить импорт:

```python
from reviewer.storage_health import (
    CAUSE_STORAGE_UNAVAILABLE, CAUSE_UNKNOWN, is_storage_unavailable, storage_remedy,
)
```

Добавить после `SECTIONS` константы формулировок:

```python
STORAGE_REASON = "хранилище не отвечает"
SKIPPED_REASON = "пропущено: хранилище не отвечает"
```

Заменить `gap` (строка 27) на:

```python
def gap(section: str, reason: str, *, cause: str = CAUSE_UNKNOWN,
        remedy: str | None = None) -> dict:
    """Структурная запись о пробеле: секция, причина и её класс, без секретов.

    `cause` — машиночитаемый класс причины: скилл и тесты ветвятся по нему, а не
    по прозе `reason`. `remedy` — команда-лекарство, когда она есть и уместна.
    """
    return {"section": section, "reason": reason, "cause": cause, "remedy": remedy}
```

Добавить перед `_safe` состояние замыкания:

```python
class _StorageState:
    """Взведён ли флаг «хранилище не отвечает» и какое лекарство называть.

    Флаг живёт на один вызов `build_task_context`: первая же недоступность
    хранилища отменяет остальные store-секции, иначе каждая добавила бы к
    времени ответа собственный таймаут пула (30 с × 8 секций).
    """

    def __init__(self, remedy: str | None) -> None:
        self.remedy = remedy
        self.down = False
```

Заменить `_safe` (строки 32-40) на:

```python
def _safe(payload: dict, section: str, produce, default, reason: str,
          state: _StorageState):
    """Собрать секцию fail-open: сбой → default + запись в gaps.

    При взведённом `state.down` источник не вызывается вовсе — секция получает
    свой default и запись о пропуске, поэтому payload по-прежнему содержит все
    ключи `SECTIONS`.
    """
    if state.down:
        payload["gaps"].append(gap(section, SKIPPED_REASON,
                                   cause=CAUSE_STORAGE_UNAVAILABLE,
                                   remedy=state.remedy))
        return default
    try:
        return produce()
    except Exception as exc:  # noqa: BLE001 — источник секции недоступен, это штатный случай
        log.warning("prepare_task_context: секция %s недоступна", section, exc_info=True)
        if is_storage_unavailable(exc):
            state.down = True
            payload["gaps"].append(gap(section, STORAGE_REASON,
                                       cause=CAUSE_STORAGE_UNAVAILABLE,
                                       remedy=state.remedy))
        else:
            payload["gaps"].append(gap(section, reason))
        return default
```

Добавить функцию резолва эндпоинтов перед `build_task_context`:

```python
def _remedy(deps) -> str | None:
    """Лекарство по эндпоинтам хранилищ, если провайдер умеет их назвать.

    Читается через `getattr`, как `augment_gaps`: модуль намеренно не знает про
    Settings, а старый провайдер без этого метода обязан продолжать работать.
    """
    getter = getattr(deps, "storage_endpoints", None)
    if not callable(getter):
        return None
    try:
        return storage_remedy(*(getter() or ()))
    except Exception:  # noqa: BLE001 — источник эндпоинтов недоступен, это не повод падать
        log.warning("prepare_task_context: эндпоинты хранилищ недоступны", exc_info=True)
        return None
```

В `build_task_context` создать состояние сразу после инициализации `payload["warnings"]`:

```python
    state = _StorageState(_remedy(deps))
```

и добавить `state` последним позиционным аргументом **во все** вызовы `_safe` в этой функции
(их **девять**: `preflight`, `task_board`, `warm_board`, `task`, `related.linked`,
`related.similar`, `subsystems`, `code`, `test_exemplars` — и ни одного пропустить нельзя,
иначе замыкание протечёт).

- [ ] **Step 4: Прогнать тесты и убедиться, что они проходят**

Run: `.venv/bin/pytest tests/mcp/test_prepare_task_context.py -q`
Expected: PASS — все 37 тестов (30 существующих без правок + 7 новых).

- [ ] **Step 5: Добавить `storage_endpoints` в живой провайдер**

В `reviewer/mcp/service.py`, в класс `_TaskContextDeps` (строка 3520), дописать метод сразу после `preflight`:

```python
    def storage_endpoints(self) -> tuple[str, ...]:
        """Эндпоинты хранилищ для решения об уместности совета `reviewer start`.

        Settings доступен здесь законно: модуль сборки контекста про него не
        знает намеренно и получает уже готовое лекарство.
        """
        settings = self._service.settings
        return tuple(value for value in (settings.pg_dsn, settings.neo4j_uri) if value)
```

- [ ] **Step 6: Убедиться, что весь MCP-слой и линт зелёные**

Run: `.venv/bin/pytest tests/mcp -q && .venv/bin/ruff check reviewer tests`
Expected: PASS, ruff без замечаний. Атрибут `self.settings` у `MCPReviewService` существует (`reviewer/mcp/service.py:253`) — проверено при написании плана.

- [ ] **Step 7: Коммит**

```bash
git add reviewer/mcp/task_context.py reviewer/mcp/service.py tests/mcp/test_prepare_task_context.py
git commit -m "feat(mcp): класс причины в gaps и короткое замыкание секций при лежащем хранилище"
```

---

### Task 3: Быстрый отказ в `index_batch` и `refresh_meta_batch`

**Files:**
- Modify: `reviewer/tasks/service.py:126-283` (`index_batch`) и `:285-317` (`refresh_meta_batch`)
- Test: `tests/tasks/test_service_batch.py`

**Interfaces:**
- Consumes: `reviewer.storage_health.is_storage_unavailable` из Task 1.
- Produces: ничего для последующих задач. Форма результата `index_batch` не меняется — все семь ключей на месте, длина списка равна длине входа.

- [ ] **Step 1: Написать падающие тесты**

В `tests/tasks/test_service_batch.py` добавить импорт в шапку:

```python
import psycopg_pool
```

Добавить в конец файла:

```python
class _TimingOutStore(_FakeStore):
    """Стор, у которого пул не отдаёт соединение: каждый заход — 30 с в проде."""

    def __init__(self):
        super().__init__()
        self.existing_hash_calls = 0

    def existing_hash(self, key):
        self.existing_hash_calls += 1
        raise psycopg_pool.PoolTimeout("couldn't get a connection after 30.00 sec")


def test_first_pool_timeout_stops_further_store_calls():
    """Критерий 4: число попыток равно одной, а не числу задач."""
    store, graph, emb = _TimingOutStore(), _FakeGraph(), _FakeEmbedder()
    tasks = [_brief(f"ID-{n}", f"PRI-{n}", title=f"t{n}", description=f"d{n}", links=[])
             for n in range(1, 48)]
    results = TaskService(store, graph, emb).index_batch(tasks)

    assert store.existing_hash_calls == 1
    assert len(results) == len(tasks)
    assert all(r["retry_required"] is True for r in results)
    assert all(r["embedded"] is False for r in results)


def test_pool_timeout_skips_voyage_call_entirely():
    """Писать результат некуда — квоту Voyage (3 RPM / 10K TPM) не тратим."""
    store, graph, emb = _TimingOutStore(), _FakeGraph(), _FakeEmbedder()
    tasks = [_brief(f"ID-{n}", f"PRI-{n}", title=f"t{n}", description=f"d{n}", links=[])
             for n in range(1, 6)]
    TaskService(store, graph, emb).index_batch(tasks)

    assert emb.doc_calls == []


def test_pool_timeout_skips_graph_phase():
    """Флаг один на оба хранилища: иначе та же арифметика повторится на графе."""
    store, graph, emb = _TimingOutStore(), _FakeGraph(), _FakeEmbedder()
    tasks = [_brief(f"ID-{n}", f"PRI-{n}", title=f"t{n}", description=f"d{n}", links=[])
             for n in range(1, 6)]
    TaskService(store, graph, emb).index_batch(tasks)

    assert graph.tasks == []


def test_result_shape_survives_the_early_exit():
    """mcp/service.py:983 проверяет длину результата — форма обязана совпадать."""
    store, graph, emb = _TimingOutStore(), _FakeGraph(), _FakeEmbedder()
    tasks = [_brief("ID-1", "PRI-1"), _brief("ID-2", "PRI-2", title="t2",
                                              description="d2", links=[])]
    results = TaskService(store, graph, emb).index_batch(tasks)

    for result in results:
        assert set(result) == {"key", "embedded", "links_upserted", "links_stored",
                               "prs_linked", "warnings", "retry_required"}


def test_non_storage_error_still_processes_every_task():
    """Сбой не-хранилища прежнее пер-задачное поведение не меняет."""
    class _BrokenStore(_FakeStore):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def existing_hash(self, key):
            self.calls += 1
            raise RuntimeError("boom")

    store, graph, emb = _BrokenStore(), _FakeGraph(), _FakeEmbedder()
    tasks = [_brief(f"ID-{n}", f"PRI-{n}", title=f"t{n}", description=f"d{n}", links=[])
             for n in range(1, 6)]
    results = TaskService(store, graph, emb).index_batch(tasks)

    assert store.calls == 5
    assert len(results) == 5


def test_refresh_meta_batch_skips_graph_loop_when_store_is_down():
    """У refresh_meta_batch свой пер-задачный цикл по графу — он тоже гасится."""
    class _TimingOutMetaStore(_FakeStore):
        def update_meta_batch(self, metas):
            raise psycopg_pool.PoolTimeout("couldn't get a connection after 30.00 sec")

    store, graph, emb = _TimingOutMetaStore(), _FakeGraph(), _FakeEmbedder()
    metas = [{"key": f"ID-{n}", "title": f"t{n}", "status": "Open",
              "url": None, "aliases": [], "project": "PRI"} for n in range(1, 48)]
    result = TaskService(store, graph, emb).refresh_meta_batch(metas)

    assert graph.tasks == []
    assert result["warnings"]
```

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run: `.venv/bin/pytest tests/tasks/test_service_batch.py -q`
Expected: FAIL — `test_first_pool_timeout_stops_further_store_calls` даёт `existing_hash_calls == 47`, а не `1`.

- [ ] **Step 3: Реализовать в `service.py`**

В шапку `reviewer/tasks/service.py` добавить импорт:

```python
from reviewer.storage_health import is_storage_unavailable
```

Добавить на уровне модуля, рядом с прочими константами:

```python
_STORAGE_SKIPPED = "store unavailable: пропущено после первого сбоя соединения"


def _skipped_result(key: str) -> dict:
    """Строка результата для задачи, которую не тронули после отказа стора.

    Форма совпадает с веткой ошибки шага 2 — `index_batch` обязан вернуть по
    строке на каждую входную задачу: `reviewer/mcp/service.py:983` сверяет длину
    результата с длиной входа и уходит в warning при расхождении.
    """
    return {"key": key, "embedded": False, "links_upserted": 0,
            "links_stored": None, "prs_linked": 0,
            "warnings": [_STORAGE_SKIPPED], "retry_required": True}
```

В `index_batch` объявить флаг перед шагом 2 (рядом с `to_embed`/`meta_only`):

```python
        # Первый же сбой соединения гасит все дальнейшие заходы в пул: иначе
        # каждая задача добавляет собственный таймаут (47 × 30 с ≈ 23 минуты).
        storage_down = False
```

Шаг 2 — тело цикла начинается с проверки флага, а обработчик его взводит:

```python
        for i, p in enumerate(parsed):
            if p is None:
                continue
            if storage_down:
                results[i] = _skipped_result(p["key"])
                continue
            try:
                prev = self._store.existing_hash(p["key"])
            except Exception as e:
                if is_storage_unavailable(e):
                    storage_down = True
                log.warning("index_batch: existing_hash сбой для %s", p["key"], exc_info=True)
                results[i] = {"key": p["key"], "embedded": False, "links_upserted": 0,
                              "links_stored": None, "prs_linked": 0,
                              "warnings": [f"store: {type(e).__name__}: {e}"],
                              "retry_required": True}
                continue
            (meta_only if prev == p["chash"] else to_embed).append(i)
```

Шаг 3 — условие вызова Voyage дополняется флагом:

```python
        if to_embed and not storage_down:
```

Шаг 4 — в начале тела цикла:

```python
        for i in to_embed:
            p = parsed[i]
            if storage_down:
                results[i] = _skipped_result(p["key"])
                continue
```

и внутри существующего `except Exception as e:` этого шага перед `warnings.append` добавить:

```python
                    if is_storage_unavailable(e):
                        storage_down = True
```

Шаг 5 — то же самое: проверка флага в начале тела цикла и взведение внутри `except`:

```python
        for i in meta_only:
            p = parsed[i]
            if storage_down:
                results[i] = _skipped_result(p["key"])
                continue
```

```python
            except Exception as e:
                if is_storage_unavailable(e):
                    storage_down = True
                retry_required = True
```

Снимок links — цикл целиком под флагом, первой строкой тела:

```python
        for i, p in enumerate(parsed):
            if storage_down:
                break
```

Шаг 6 (граф) — то же, первой строкой тела цикла:

```python
        for i, p in enumerate(parsed):
            if storage_down:
                break
```

и батчевый MERGE:

```python
        if pr_pairs and self._graph is not None and not storage_down:
```

В `refresh_meta_batch` — тот же локальный флаг:

```python
        storage_down = False
        try:
            self._store.update_meta_batch(metas)
        except Exception as e:
            if is_storage_unavailable(e):
                storage_down = True
            log.warning("refresh_meta_batch: сбой update_meta_batch", exc_info=True)
            warnings.append(f"store: {type(e).__name__}: {e}")
        if storage_down:
            # Ниже — пер-задачный цикл по графу: на лежащем Neo4j он повторил бы
            # ту же арифметику для задач ниже watermark.
            warnings.append(_STORAGE_SKIPPED)
        elif self._graph is None:
            warnings.append("graph unavailable: task projects not refreshed in graph")
        else:
            for m in metas:
                # Тело цикла (try/except с graph.upsert_task) переносится как есть,
                # меняется только его отступ под новую ветку elif.
                ...
```

- [ ] **Step 4: Прогнать тесты и убедиться, что они проходят**

Run: `.venv/bin/pytest tests/tasks -q`
Expected: PASS — новые шесть тестов зелёные, все существующие тесты `tests/tasks/` тоже.

- [ ] **Step 5: Проверить инвариант watermark**

Run: `.venv/bin/pytest tests/tasks/test_sync.py tests/tasks/test_sync_cursor.py -q`
Expected: PASS. Пропущенные задачи взводят `retry_required`, поэтому гейт `reviewer/tasks/sync.py:289` (`elif not retry_required and (...)`) не двигает курсор — ровно как при 47 отдельных сбоях.

- [ ] **Step 6: Коммит**

```bash
git add reviewer/tasks/service.py tests/tasks/test_service_batch.py
git commit -m "fix(tasks): первый сбой соединения прекращает заходы в пул вместо N попыток по 30 с"
```

---

### Task 4: Шаг скилла «Инфраструктура» в `preflight.md`

**Files:**
- Modify: `plugin/skills/solve-task/references/preflight.md` (вставить новый шаг 0.0 перед существующим шагом 0.1 «Base-index freshness»)
- Modify: манифест codex (перегенерируется скриптом, руками не править)

**Interfaces:**
- Consumes: форма `gaps` из Task 2 — поля `cause` и `remedy`.
- Produces: ничего (последняя задача).

- [ ] **Step 1: Вставить новый шаг в `preflight.md`**

Вставить перед строкой `   1. **Base-index freshness.**` следующий блок (нумерация существующих шагов 1-4 не меняется — новый шаг намеренно нулевой, чтобы не переписывать все ссылки на «Step 0.1» и «Step 0.4» в `SKILL.md`):

```markdown
   0. **Storage reachability — check this before anything else.** Scan `payload.gaps` for an
      entry whose `cause` is `storage_unavailable`. Present it: **never build the brief on a
      gutted context silently.** The gaps list also carries `remedy` — the command that fixes
      it (`reviewer start`) or `null` when the deployment's storages are remote, where a local
      docker stack fixes nothing.

      Tell the user (in Russian) which sections were lost, then present **three options**:
      1. «Поднять сейчас» — offered **only** when the gap carries a `remedy`. Run that command
         (`reviewer start`), wait for it to finish, then re-run `prepare_task_context(...)` once
         and continue with the fresh payload.
      2. «Подниму сам» → **PAUSE HERE** and wait for the user to write «готово», «поднял»,
         «done» or any confirmation. Once confirmed, re-run `prepare_task_context(...)` and
         continue.
      3. «Продолжить без контекста» → note under **Constraints / open questions** in the brief:
         «хранилище не отвечает (`cause: storage_unavailable`); секции <перечислить> собраны не
         были», and continue.

      When `remedy` is `null`, option 1 is not shown at all — say plainly that the storages are
      remote and `reviewer start` does not apply here.

      **The server never starts containers.** It only classifies the failure and names the cure;
      bringing the infrastructure up is the user's call, made here. In `full-auto` do not ask:
      take option 1, or option 3 when there is no `remedy`, and record it in the run-state file's
      decisions section.

      If no gap carries `cause: storage_unavailable`, say nothing and go straight to Step 0.1.
```

- [ ] **Step 2: Перегенерировать манифест codex**

Run: `.venv/bin/python scripts/update_codex_plugin_manifest.py`
Expected: скрипт отчитывается об обновлении payload-digest. Без этого шага install-тесты покраснеют — правка контента под `plugin/` меняет digest.

- [ ] **Step 3: Прогнать guard-тесты скиллов и установки**

Run: `.venv/bin/pytest tests/skills tests/install -q`
Expected: PASS. Эти тесты проверяют сборку промптов скиллов и совпадение манифеста.

- [ ] **Step 4: Прогнать весь unit-набор и линт**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check reviewer tests`
Expected: PASS целиком. Базовая линия перед задачей — весь набор зелёный; любое падение здесь означает регрессию этого плана, а не «известное падение».

- [ ] **Step 5: Коммит**

```bash
git add plugin/skills/solve-task/references/preflight.md
git add .codex-plugin plugin/.codex-plugin plugin/.claude-plugin plugin/assets
git commit -m "feat(skills): solve-task не собирает бриф молча при недоступном хранилище"
```

---

## Проверка критериев приёмки

| Критерий | Где закрыт |
|---|---|
| 1 — отвечает за секунды; в gaps причина и команда | Task 2 (`test_storage_failure_names_cause_and_remedy`, `test_storage_failure_short_circuits_remaining_sections`) + Task 3 (`test_first_pool_timeout_stops_further_store_calls`) |
| 2 — «не отвечает» ≠ «не построен», закреплено тестом | Task 2 (`test_storage_failure_names_cause_and_remedy` против `test_other_failure_keeps_cause_unknown`) |
| 3 — совет не выдаётся удалённым эндпоинтам | Task 1 (`test_remote_endpoint_gets_no_remedy`) + Task 2 (`test_remote_deploy_gets_cause_without_remedy`) |
| 4 — число попыток закреплено тестом | Task 3 (`test_first_pool_timeout_stops_further_store_calls`: `existing_hash_calls == 1`) |
| 5 — при живой инфраструктуре payload и тайминги не меняются | Task 2, шаг 4: 30 существующих тестов `tests/mcp/test_prepare_task_context.py` проходят **без правок**; `test_happy_path_has_no_gaps` — прямая проверка |
