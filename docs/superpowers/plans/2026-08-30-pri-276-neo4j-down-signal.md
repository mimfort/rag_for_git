# PRI-276 — отказ Neo4j как машиночитаемый сигнал: план реализации

> **Для агентов-исполнителей:** ОБЯЗАТЕЛЬНЫЙ СУБ-СКИЛЛ: используйте
> superpowers:subagent-driven-development (рекомендуется) или superpowers:executing-plans, чтобы
> выполнять план задача за задачей. Шаги размечены чекбоксами (`- [ ]`) для отслеживания.

**Цель:** при недоступном Neo4j `prepare_task_context` обязан вернуть запись в `gaps` с
`cause: storage_unavailable`, потерять ровно секцию `related.linked` и ответить за единицы секунд
вместо 162.

**Архитектура:** исключение недоступности графа перестаёт подменяться текстовой нотой раньше
классификатора; замыкание секций становится per-store (граф отдельно от Postgres), а факт
недоступности графа поднимается из preflight полем `BranchStatus.graph_error`. Цену одного захода
в мёртвый граф снижают явные таймауты драйвера.

**Стек:** Python 3.12, pytest, psycopg, neo4j-driver, pydantic-settings.

**Спека:** `docs/superpowers/specs/2026-08-30-pri-276-neo4j-down-signal-design.md`

## Глобальные ограничения

- Язык проекта — русский: комментарии, докстринги, сообщения CLI. Новый код пишется в этом стиле.
- Коммиты — Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By`,
  упоминаний Claude).
- Unit-тесты запрещено пускать в сеть и на localhost-сокеты; любой тест с реальной сетью обязан
  иметь `@pytest.mark.integration`. Все тесты этого плана — unit.
- Рабочая ветка: `feat/pri-276-neo4j-down-signal` (уже создана, спека и бриф в ней закоммичены).
- Форма записи `gap()` не меняется: пять ключей `section`/`reason`/`cause`/`cause_detail`/`remedy`,
  и `cause` при недоступности хранилища остаётся строкой `"storage_unavailable"` — шаг 0a скилла
  `solve-task` ищет равенство именно ей. Плагин (`plugin/`) в этом плане не правится, поэтому
  пересборка манифестов не требуется.
- Прогон тестов: `.venv/bin/pytest -q` (integration исключаются автоматически).
- Линт: `.venv/bin/ruff check reviewer tests`.

---

### Task 1: `storage_backend` — какое хранилище молчит

Публичный контракт классификации: отвечает на вопрос «какое из хранилищ недоступно», в пару к
существующему `is_storage_unavailable` («лечится ли это подъёмом контейнеров»). Решает `isinstance`,
а не текст: в тексте `OperationalError` живёт DSN с паролем.

**Файлы:**
- Modify: `reviewer/storage_health.py` (рядом с `is_storage_unavailable`, строки 26-66)
- Test: `tests/test_storage_health.py`

**Интерфейсы:**
- Потребляет: ничего из предыдущих задач.
- Производит: `reviewer.storage_health.BACKEND_GRAPH = "graph"`,
  `reviewer.storage_health.BACKEND_POSTGRES = "postgres"`,
  `storage_backend(exc: BaseException) -> str | None`.

- [ ] **Шаг 1: Написать падающие тесты**

В конец `tests/test_storage_health.py`:

```python
# ---------------------------------------------------------------------------
# Тесты PRI-276: какое из хранилищ молчит
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exc", [ServiceUnavailable("no routing servers"),
                                 SessionExpired("session expired")])
def test_storage_backend_neo4j_errors_are_graph(exc):
    assert sh.storage_backend(exc) == sh.BACKEND_GRAPH


def test_storage_backend_operational_error_is_postgres():
    assert sh.storage_backend(psycopg.OperationalError("connection refused")) == sh.BACKEND_POSTGRES


def test_storage_backend_pool_timeout_is_postgres():
    """PoolTimeout — подкласс OperationalError, покрытие обязано совпадать с is_storage_unavailable."""
    exc = psycopg_pool.PoolTimeout("couldn't get a connection after 30.00 sec")
    assert sh.storage_backend(exc) == sh.BACKEND_POSTGRES


@pytest.mark.parametrize("exc", [RuntimeError("no index"),
                                 psycopg.ProgrammingError("syntax error at or near"),
                                 AuthError("unauthorized")])
def test_storage_backend_is_none_for_non_storage_failures(exc):
    """Покрытие совпадает с is_storage_unavailable: что не «хранилище лежит» — то None."""
    assert sh.storage_backend(exc) is None
```

- [ ] **Шаг 2: Прогнать тесты и убедиться, что они падают**

Run: `.venv/bin/pytest tests/test_storage_health.py -q -k storage_backend`
Ожидание: FAIL с `AttributeError: module 'reviewer.storage_health' has no attribute 'storage_backend'`

- [ ] **Шаг 3: Реализовать**

В `reviewer/storage_health.py` после константы `REMEDY_START = "reviewer start"` (строка 28):

```python
BACKEND_GRAPH = "graph"
BACKEND_POSTGRES = "postgres"
```

После функции `is_storage_unavailable`:

```python
def storage_backend(exc: BaseException) -> str | None:
    """Какое из хранилищ молчит, если молчит вообще.

    Пара к `is_storage_unavailable`: та отвечает «лечится ли подъёмом
    контейнеров», эта — «кого именно поднимать». Покрытие у них общее, поэтому
    None здесь означает ровно то же, что False там.

    Решает тип исключения, а не текст: в тексте `OperationalError` живёт DSN с
    паролем (тот же мотив, что у `classify_storage_failure`).
    """
    if isinstance(exc, (ServiceUnavailable, SessionExpired)):
        return BACKEND_GRAPH
    if isinstance(exc, psycopg.OperationalError):
        return BACKEND_POSTGRES
    return None
```

- [ ] **Шаг 4: Прогнать тесты и убедиться, что они проходят**

Run: `.venv/bin/pytest tests/test_storage_health.py -q`
Ожидание: PASS, все тесты файла зелёные.

- [ ] **Шаг 5: Коммит**

```bash
git add reviewer/storage_health.py tests/test_storage_health.py
git commit -m "feat(storage-health): класс недоступного хранилища по типу исключения"
```

---

### Task 2: замыкание секций per-store

Сейчас `_StorageState.down` — один флаг на все хранилища: взведённый отказом Neo4j, он отменит и
Postgres-секции, а критерий 4 требует терять ровно `related.linked`. Флаг становится множеством
бэкендов, вердикт — словарём по бэкендам (у неверного пароля Postgres и остановленного Neo4j
причины разные).

**Файлы:**
- Modify: `reviewer/mcp/task_context.py:17-21` (импорт), `:53-128` (`_StorageState`, `_storage_gap`,
  `_safe`), `:198-218` (ветка `warm_board` и секция `related.linked`)
- Test: `tests/mcp/test_prepare_task_context.py`

**Интерфейсы:**
- Потребляет: `BACKEND_GRAPH`, `BACKEND_POSTGRES`, `storage_backend` из Task 1.
- Производит: `_StorageState.down: set[str]`, `_StorageState.diagnoses: dict[str, StorageDiagnosis]`,
  `_StorageState.mark(backend, exc) -> StorageDiagnosis`, `_StorageState.is_down(backend) -> bool`,
  `_safe(payload, section, produce, default, reason, state, backend=BACKEND_POSTGRES)`,
  `_storage_gap(payload, section, reason, state, backend)`.

- [ ] **Шаг 1: Написать падающие тесты**

В `tests/mcp/test_prepare_task_context.py` заменить существующий
`test_neo4j_down_empties_linked_only` (строки 118-124) на блок ниже и добавить остальные тесты
в конец файла:

```python
def test_neo4j_down_empties_linked_only():
    """Критерий 5: настоящее neo4j-исключение, а не RuntimeError."""
    payload = task_context.build_task_context(
        FakeDeps(linked=ServiceUnavailable("no routing servers")), repo="o/n",
        key="PRI-276", branch="dev", warm_board=False)
    assert payload["related"]["linked"] == ""
    assert payload["related"]["similar"]
    entry = next(g for g in payload["gaps"] if g["section"] == "related.linked")
    assert entry["cause"] == "storage_unavailable"
    assert entry["remedy"] == "reviewer start"


def test_neo4j_down_keeps_postgres_sections():
    """Критерий 4: теряется ровно related.linked, Postgres-секции собраны полностью."""
    deps = FakeDeps(linked=ServiceUnavailable("no routing servers"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-276", branch="dev", warm_board=False)
    assert payload["code"] and payload["test_exemplars"]
    assert payload["subsystems"] and payload["related"]["similar"]
    assert [g["section"] for g in payload["gaps"]] == ["related.linked"]


def test_both_stores_down_keep_separate_diagnoses():
    """Вердикты не сливаются: причина Postgres не приписывается графу."""
    deps = FakeDeps(
        preflight=psycopg.OperationalError(
            'connection failed: FATAL:  password authentication failed for user "reviewer"'),
        linked=ServiceUnavailable("no routing servers"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-276", branch="dev", warm_board=False)
    pg_entry = next(g for g in payload["gaps"] if g["section"] == "preflight")
    graph_entry = next(g for g in payload["gaps"] if g["section"] == "related.linked")
    assert pg_entry["cause_detail"] == "auth_failed"
    assert graph_entry["cause_detail"] is None
```

Импорт в шапке файла дополнить: `from neo4j.exceptions import ServiceUnavailable`.

Существующий `test_storage_failure_short_circuits_remaining_sections` (строки 336-343) обновить —
граф теперь другое хранилище и Postgres-сбоем не замыкается:

```python
def test_storage_failure_short_circuits_remaining_sections():
    """Критерий 1 PRI-268: Postgres-секции не вызываются — иначе +30 с каждая.

    related.linked живёт в другом хранилище и Postgres-сбоем не замыкается
    (PRI-276): при живом Neo4j секция собирается, вместо того чтобы теряться зря.
    """
    deps = FakeDeps(preflight=psycopg.OperationalError("connection refused"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-268", branch="dev", warm_board=False)
    assert deps.calls == ["preflight", "linked"]
    assert set(payload) == set(task_context.SECTIONS)
    assert all(g["cause"] == "storage_unavailable" for g in payload["gaps"])
```

- [ ] **Шаг 2: Прогнать тесты и убедиться, что они падают**

Run: `.venv/bin/pytest tests/mcp/test_prepare_task_context.py -q`
Ожидание: FAIL — `test_neo4j_down_empties_linked_only` не находит записи с `remedy`,
`test_neo4j_down_keeps_postgres_sections` видит замкнутые Postgres-секции,
`test_storage_failure_short_circuits_remaining_sections` видит `deps.calls == ["preflight"]`.

- [ ] **Шаг 3: Реализовать**

В `reviewer/mcp/task_context.py` расширить импорт (строки 17-21):

```python
from reviewer.storage_health import (
    BACKEND_GRAPH, BACKEND_POSTGRES, CAUSE_STORAGE_UNAVAILABLE, CAUSE_UNKNOWN,
    DETAIL_AUTH_FAILED, DETAIL_MISSING_DATABASE, StorageDiagnosis,
    classify_storage_failure, is_storage_unavailable, storage_backend,
)
```

Заменить `_StorageState` (строки 53-74):

```python
class _StorageState:
    """Какие хранилища не отвечают и каков вердикт по первому сбою каждого.

    Флаги живут на один вызов `build_task_context`: первая же недоступность
    хранилища отменяет остальные секции ЭТОГО хранилища, иначе каждая добавила
    бы к времени ответа собственный таймаут пула.

    Множество, а не один флаг (PRI-276): у графа и Postgres разные секции, и
    отказ Neo4j не должен отменять поиск по коду. Вердикт хранится по бэкенду —
    у неверного пароля Postgres и остановленного Neo4j причины разные, и один
    вердикт на двоих приписал бы одному чужое лекарство.
    """

    def __init__(self, endpoints: tuple[str, ...]) -> None:
        self.endpoints = endpoints
        self.down: set[str] = set()
        self.diagnoses: dict[str, StorageDiagnosis] = {}

    def mark(self, backend: str, exc: BaseException) -> StorageDiagnosis:
        """Взвести флаг бэкенда; вердикт считается по его первому сбою."""
        self.down.add(backend)
        if backend not in self.diagnoses:
            self.diagnoses[backend] = classify_storage_failure(exc, *self.endpoints)
        return self.diagnoses[backend]

    def is_down(self, backend: str) -> bool:
        return backend in self.down
```

Заменить `_storage_gap` (строки 77-88):

```python
def _storage_gap(payload: dict, section: str, reason: str, state: _StorageState,
                 backend: str) -> None:
    """Записать в gaps пробел, вызванный недоступностью хранилища.

    Общая точка для трёх мест, различающихся только `reason`: skip- и except-
    ветки `_safe`, а также `elif warm_board and not board` в build_task_context.
    Вердикт берётся по бэкенду секции, а не общий на вызов.
    """
    diagnosis = state.diagnoses.get(backend)
    detail = diagnosis.detail if diagnosis is not None else None
    remedy = diagnosis.remedy if diagnosis is not None else None
    payload["gaps"].append(gap(section, _reason_with_detail(reason, diagnosis),
                               cause=CAUSE_STORAGE_UNAVAILABLE,
                               cause_detail=detail, remedy=remedy))
```

Заменить `_safe` (строки 107-128):

```python
def _safe(payload: dict, section: str, produce, default, reason: str,
          state: _StorageState, backend: str = BACKEND_POSTGRES):
    """Собрать секцию fail-open: сбой → default + запись в gaps.

    `backend` — хранилище секции; дефолт Postgres, потому что своё хранилище он
    у всех секций, кроме `related.linked`. При взведённом флаге ЭТОГО бэкенда
    источник не вызывается вовсе — секция получает свой default и запись
    о пропуске, поэтому payload по-прежнему содержит все ключи `SECTIONS`.

    Какой бэкенд упал, решает тип исключения, а не разметка секции: она отвечает
    на другой вопрос — кого пропускать.
    """
    if state.is_down(backend):
        _storage_gap(payload, section, SKIPPED_REASON, state, backend)
        return default
    try:
        return produce()
    except Exception as exc:  # noqa: BLE001 — источник секции недоступен, это штатный случай
        log.warning("prepare_task_context: секция %s недоступна", section, exc_info=True)
        if is_storage_unavailable(exc):
            failed = storage_backend(exc) or backend
            state.mark(failed, exc)
            _storage_gap(payload, section, STORAGE_REASON, state, failed)
        else:
            payload["gaps"].append(gap(section, reason))
        return default
```

В `build_task_context` заменить ветку `warm_board` (строки 198-202):

```python
    elif warm_board and not board:
        if state.is_down(BACKEND_POSTGRES):
            _storage_gap(payload, "warm_board", SKIPPED_REASON, state, BACKEND_POSTGRES)
        else:
            payload["gaps"].append(gap("warm_board", "доска не настроена"))
```

И разметить секцию графа (строки 212-214):

```python
        "linked": _safe(payload, "related.linked",
                        lambda: deps.linked(key, project), "", "граф задач недоступен",
                        state, BACKEND_GRAPH),
```

- [ ] **Шаг 4: Прогнать тесты и убедиться, что они проходят**

Run: `.venv/bin/pytest tests/mcp/test_prepare_task_context.py -q`
Ожидание: PASS, все тесты файла зелёные.

- [ ] **Шаг 5: Коммит**

```bash
git add reviewer/mcp/task_context.py tests/mcp/test_prepare_task_context.py
git commit -m "feat(mcp): замыкание секций контекста задачи раздельно по хранилищам"
```

---

### Task 3: нота `(task graph unavailable)` уезжает на границу MCP-тула

Корень дефекта: `TaskService.get_task_context` глотает любое исключение и возвращает ноту, поэтому
классификатор в `_safe` не вызывается вовсе. Проброс делается только для недоступности хранилища;
публичный тул сохраняет прежний ответ, ловя исключение у себя.

**Файлы:**
- Modify: `reviewer/tasks/service.py:415-426` (`get_task_context`)
- Modify: `reviewer/mcp/service.py:506-508` (публичный тул), `:3576-3577` (`_TaskContextDeps.linked`)
- Test: `tests/tasks/test_service.py`, `tests/mcp/test_service.py`

**Интерфейсы:**
- Потребляет: `is_storage_unavailable` (существующий).
- Производит: `TaskService.get_task_context` пробрасывает storage-исключения;
  `MCPReviewService.get_task_context` по-прежнему возвращает `str`;
  `_TaskContextDeps.linked` вызывает `components.task_service.get_task_context` напрямую.

- [ ] **Шаг 1: Написать падающие тесты**

В `tests/tasks/test_service.py` рядом с существующими тестами `get_task_context` (строки 313-330):

```python
def test_get_task_context_reraises_storage_failure():
    """PRI-276: недоступность графа обязана дойти до классификатора, а не стать нотой."""
    from neo4j.exceptions import ServiceUnavailable

    class DownGraph(_FakeGraph):
        def task_context(self, key, project=""):
            raise ServiceUnavailable("no routing servers")

    svc = TaskService(_FakeStore(), DownGraph(context={}), _FakeEmbedder())
    with pytest.raises(ServiceUnavailable):
        svc.get_task_context("ID-1")


def test_get_task_context_still_swallows_other_failures():
    """Прочий сбой обхода — по-прежнему нота: меняется поведение только для хранилища."""
    svc = TaskService(_FakeStore(), _FakeGraph(context={}, raise_on={"task_context"}),
                      _FakeEmbedder())
    assert svc.get_task_context("ID-1") == "(task graph unavailable)"
```

Существующий `_FakeGraph` (строки 56-92) уже умеет `raise_on={"task_context"}` и бросает оттуда
`RuntimeError`, поэтому второму тесту свой класс не нужен; первому нужен, потому что тип исключения
здесь и есть предмет проверки.

В `tests/mcp/test_service.py` рядом с тестами делегирования (строки 631-641); сервис собирается
фабрикой `_make_mcp_service()`, которой пользуются соседние тесты:

```python
def test_public_get_task_context_keeps_note_on_storage_failure():
    """PRI-276: публичный контракт тула цел — нота, а не исключение."""
    from neo4j.exceptions import ServiceUnavailable

    svc = _make_mcp_service()
    svc.components.task_service.get_task_context.side_effect = ServiceUnavailable("down")
    assert svc.get_task_context("ID-1") == "(task graph unavailable)"


def test_task_context_deps_linked_lets_storage_failure_through():
    """А провайдер секции отдаёт исключение в _safe, минуя обёртку с нотой."""
    from neo4j.exceptions import ServiceUnavailable
    from reviewer.mcp.service import _TaskContextDeps

    svc = _make_mcp_service()
    svc.components.task_service.get_task_context.side_effect = ServiceUnavailable("down")
    with pytest.raises(ServiceUnavailable):
        _TaskContextDeps(svc, None).linked("ID-1", "PRI")
```

- [ ] **Шаг 2: Прогнать тесты и убедиться, что они падают**

Run: `.venv/bin/pytest tests/tasks/test_service.py tests/mcp/test_service.py -q -k "task_context"`
Ожидание: FAIL — `get_task_context` возвращает ноту вместо проброса, `linked` тоже.

- [ ] **Шаг 3: Реализовать**

В `reviewer/tasks/service.py` дополнить импорты модуля:

```python
from reviewer.storage_health import is_storage_unavailable
```

и заменить тело `get_task_context` (строки 415-426):

```python
    def get_task_context(self, key: str, project: str | None = None) -> str:
        """Граф-контекст задачи: связанные задачи → их PR → код. Деградация → нота.

        Недоступность хранилища — исключение, а не нота (PRI-276): подмена
        строкой здесь означала бы, что классификатор `_safe` не вызывается
        вовсе, а клиент не получает машиночитаемого сигнала. Ноту для своих
        вызывающих ставит граница MCP-тула.
        """
        if self._graph is None:
            return "(task graph unavailable)"
        try:
            ctx = self._graph.task_context(key, project or "")
        except Exception as exc:
            if is_storage_unavailable(exc):
                raise
            log.warning("get_task_context: сбой обхода графа для %s", key, exc_info=True)
            return "(task graph unavailable)"
        if not ctx:
            return f"(no task '{key}' in task graph)"
        return _format_task_context(ctx, self._max_chars)
```

В `reviewer/mcp/service.py` заменить публичный тул (строки 506-508):

```python
    def get_task_context(self, key: str, project: str | None = None) -> str:
        """Граф-контекст задачи. При project — соседи только этого проекта.

        Нота при недоступном хранилище ставится здесь, а не в TaskService
        (PRI-276): контракт тула — строка, а провайдеру секции нужно исключение.
        """
        try:
            return self.components.task_service.get_task_context(key, project=project)
        except Exception as exc:  # noqa: BLE001 — недоступное хранилище графа, штатный случай
            if not is_storage_unavailable(exc):
                raise
            log.warning("get_task_context: хранилище графа недоступно", exc_info=True)
            return "(task graph unavailable)"
```

Проверить, что `is_storage_unavailable` уже импортирован в `reviewer/mcp/service.py`; если нет —
добавить в существующий импорт из `reviewer.storage_health`.

И заменить провайдер секции (строки 3576-3577):

```python
    def linked(self, key: str, project: str | None) -> str:
        """Граф-контекст задачи без ноты: исключение обязано дойти до `_safe`.

        Зовём task_service напрямую, минуя обёртку сервиса: та ставит ноту ради
        публичного контракта тула, и здесь она обнулила бы сигнал (PRI-276).
        """
        return self._service.components.task_service.get_task_context(key, project=project)
```

- [ ] **Шаг 4: Прогнать тесты и убедиться, что они проходят**

Run: `.venv/bin/pytest tests/tasks/ tests/mcp/ -q`
Ожидание: PASS. Особое внимание — `tests/tasks/test_service.py:313` (`graph is None` → нота) и
`tests/mcp/test_service.py:632` (делегирование) остаются зелёными.

- [ ] **Шаг 5: Коммит**

```bash
git add reviewer/tasks/service.py reviewer/mcp/service.py tests/tasks/test_service.py tests/mcp/test_service.py
git commit -m "fix(tasks): недоступность графа доходит до классификатора, нота — на границе тула"
```

---

### Task 4: `graph_error` — preflight взводит флаг графа, не теряя себя

Preflight идёт первым и трогает граф (`count_nodes`), но глотает отказ в `graph_nodes=None`. Без
сигнала оттуда `related.linked` заплатит второй таймаут. Поле в отчёте сохраняет секцию целой и
даёт `build_task_context` повод замкнуть граф ещё до обращения к нему.

**Файлы:**
- Modify: `reviewer/services/status.py:16-24` (`BranchStatus`), `:53-75` (`build_status_report`)
- Modify: `reviewer/mcp/service.py:3536-3551` (`_TaskContextDeps.preflight`)
- Modify: `reviewer/mcp/task_context.py` (новая `_absorb_graph_error`, вызов в `build_task_context`)
- Test: `tests/services/test_status.py`, `tests/mcp/test_prepare_task_context.py`

**Интерфейсы:**
- Потребляет: `_StorageState.mark`, `BACKEND_GRAPH` из Task 2.
- Производит: `BranchStatus.graph_error: BaseException | None = None`; ключ `graph_error` в словаре
  `_TaskContextDeps.preflight`; `_absorb_graph_error(preflight, state)` в `task_context.py`.

- [ ] **Шаг 1: Написать падающие тесты**

В `tests/services/test_status.py`:

```python
def test_build_status_report_carries_graph_error(monkeypatch):
    """PRI-276: секция цела (graph_nodes=None), но причина названа полем."""
    store = FakeStore(meta={}, chunks={"base:main": 0}, refs=["base:main"])
    graph = FakeGraph(nodes={}, fail=True)
    monkeypatch.setattr(status_mod, "commits_behind", lambda *a: None)
    rep = build_status_report(store, graph, "a/x", ["main"], "/tmp/repo")
    b = rep.branches[0]
    assert b.graph_nodes is None
    assert isinstance(b.graph_error, Exception)


def test_build_status_report_graph_error_is_none_when_graph_alive(monkeypatch):
    store = FakeStore(meta={}, chunks={"base:main": 0}, refs=["base:main"])
    graph = FakeGraph(nodes={("a/x", "main"): 7})
    monkeypatch.setattr(status_mod, "commits_behind", lambda *a: None)
    rep = build_status_report(store, graph, "a/x", ["main"], "/tmp/repo")
    assert rep.branches[0].graph_error is None


def test_render_status_json_omits_graph_error():
    """Исключение — не для машиночитаемого вывода: контракт status --json не меняется."""
    rep = RepoStatus(repo="a/x", branches=[
        BranchStatus("main", "base:main", "abc1234567", None, 10, None, 0,
                     graph_error=RuntimeError("neo4j down"))], overlays=[])
    payload = json.loads(render_status_json(rep))
    assert "graph_error" not in payload["branches"][0]
```

Импорт `json` в шапке файла добавить, если его там нет. Конструкцию `FakeGraph(nodes=...)` брать
ровно ту, что используют соседние тесты файла (строки 60-84).

В `tests/mcp/test_prepare_task_context.py` — поддержка ключа в фейке и тест экономии захода.
Метод `FakeDeps.preflight` (строки 21-24) заменить на:

```python
    def preflight(self, repo, branch):
        payload = self._result("preflight", {
            "branch": branch, "indexed_sha": "abc", "drift": 0,
            "summaries": 40, "chunks": 7110, "graph_nodes": 7362})
        if isinstance(payload, dict) and "graph_error" in self._overrides:
            payload = {**payload, "graph_error": self._overrides["graph_error"]}
        return payload
```

И новые тесты в конец файла:

```python
def test_graph_error_from_preflight_skips_linked_without_calling_graph():
    """Критерий 3: второй заход в мёртвый граф не делается вовсе."""
    deps = FakeDeps(graph_error=ServiceUnavailable("no routing servers"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-276", branch="dev", warm_board=False)
    assert "linked" not in deps.calls
    entry = next(g for g in payload["gaps"] if g["section"] == "related.linked")
    assert entry["cause"] == "storage_unavailable"
    assert payload["related"]["linked"] == ""
    assert payload["code"] and payload["test_exemplars"]


def test_graph_error_does_not_leak_into_payload():
    """Объекту исключения в payload делать нечего: его читает LLM."""
    deps = FakeDeps(graph_error=ServiceUnavailable("no routing servers"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-276", branch="dev", warm_board=False)
    assert "graph_error" not in payload["preflight"]
    assert payload["preflight"]["drift"] == 0


def test_non_storage_graph_error_is_ignored():
    """Не всякая ошибка графа — недоступность: замыкать нечего, секция вызывается."""
    deps = FakeDeps(graph_error=RuntimeError("cypher blew up"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-276", branch="dev", warm_board=False)
    assert "linked" in deps.calls
    assert payload["related"]["linked"]
```

- [ ] **Шаг 2: Прогнать тесты и убедиться, что они падают**

Run: `.venv/bin/pytest tests/services/test_status.py tests/mcp/test_prepare_task_context.py -q`
Ожидание: FAIL — у `BranchStatus` нет поля `graph_error`; `deps.calls` содержит `linked`.

- [ ] **Шаг 3: Реализовать**

В `reviewer/services/status.py` дополнить `BranchStatus` полем в конце (после `summaries`):

```python
    summaries: int | None = None
    # Исключение, проглоченное при чтении графа (PRI-276): секция отчёта остаётся
    # целой (graph_nodes=None), но потребитель может узнать причину и не платить
    # второй таймаут. В render_status_json намеренно не входит — не сериализуется
    # и машиночитаемому контракту не принадлежит.
    graph_error: BaseException | None = None
```

и заполнить его в `build_status_report`:

```python
        graph_error: BaseException | None = None
        try:
            graph_nodes = graph.count_nodes(repo, branch)
        except Exception as exc:  # noqa: BLE001 — Neo4j недоступен, граф недоступен
            graph_nodes = None
            graph_error = exc
```

В конструкторе `BranchStatus(...)` добавить `graph_error=graph_error`.

В `reviewer/mcp/service.py` в `_TaskContextDeps.preflight` добавить ключ в возвращаемый словарь:

```python
            "graph_nodes": status.graph_nodes,
            # Причина пропуска графа: build_task_context извлечёт ключ и замкнёт
            # граф, не платя второй таймаут (PRI-276). В payload не попадает.
            "graph_error": status.graph_error,
```

В `reviewer/mcp/task_context.py` добавить функцию после `_safe`:

```python
def _absorb_graph_error(preflight, state: _StorageState):
    """Взвести флаг графа по ошибке, которую preflight проглотил внутри себя.

    Preflight обязан остаться собранной секцией (`graph_nodes=None` — валидная
    деградация), поэтому исключение приходит не броском, а ключом словаря.
    Ключ извлекается: payload читает LLM, объекту исключения там места нет.

    Упавший preflight (None вместо словаря) и провайдер без этого ключа проходят
    функцию без изменений.
    """
    if not isinstance(preflight, dict):
        return preflight
    error = preflight.pop("graph_error", None)
    if error is not None and is_storage_unavailable(error):
        state.mark(BACKEND_GRAPH, error)
    return preflight
```

и обернуть вызов в `build_task_context` (строки 183-185):

```python
    payload["preflight"] = _absorb_graph_error(
        _safe(payload, "preflight", lambda: deps.preflight(repo, branch), None,
              "статус индекса недоступен", state),
        state)
```

- [ ] **Шаг 4: Прогнать тесты и убедиться, что они проходят**

Run: `.venv/bin/pytest tests/services/test_status.py tests/mcp/ -q`
Ожидание: PASS.

- [ ] **Шаг 5: Коммит**

```bash
git add reviewer/services/status.py reviewer/mcp/service.py reviewer/mcp/task_context.py tests/services/test_status.py tests/mcp/test_prepare_task_context.py
git commit -m "feat(mcp): preflight поднимает отказ графа, не теряя секцию"
```

---

### Task 5: явные таймауты драйвера Neo4j

Драйвер создаётся без единого явного таймаута, поэтому каждый заход в мёртвый граф платит дефолты
neo4j: 30 с на соединение, 60 с на получение из пула, 30 с на ретраи транзакции. Именно ретраи дают
основной вклад в наблюдавшиеся 162 с. Драйвер шарится с `TaskGraph`, поэтому один рычаг покрывает
оба пути; попутно ускоряется `reviewer check`, который делает `verify_connectivity`.

**Файлы:**
- Modify: `reviewer/graph/store.py:5-11` (`GraphStore.__init__`)
- Modify: `reviewer/config/settings.py:79-81` (ключи рядом с `neo4j_password`)
- Modify: `reviewer/app.py:96`, `reviewer/entrypoints/cli.py:909`, `reviewer/entrypoints/cli.py:1199`
- Create: `tests/graph/test_store_timeouts.py`

**Интерфейсы:**
- Потребляет: ничего из предыдущих задач.
- Производит: `GraphStore(uri, user, password, *, connection_timeout=5.0,
  acquisition_timeout=10.0, max_retry_time=5.0)`; ключи `Settings.neo4j_connection_timeout`,
  `Settings.neo4j_acquisition_timeout`, `Settings.neo4j_max_retry_time` (все `float`).

- [ ] **Шаг 1: Написать падающие тесты**

Создать `tests/graph/test_store_timeouts.py`:

```python
"""Таймауты драйвера Neo4j: цена одного захода в мёртвый граф (PRI-276)."""
from reviewer.config.settings import Settings
from reviewer.graph import store as store_mod


class _FakeDriver:
    def close(self):
        pass


def _capture(monkeypatch) -> dict:
    """Подменить GraphDatabase фейком и вернуть словарь с kwargs вызова."""
    captured: dict = {}

    class _FakeGraphDatabase:
        @staticmethod
        def driver(uri, **kwargs):
            captured["uri"] = uri
            captured.update(kwargs)
            return _FakeDriver()

    monkeypatch.setattr(store_mod, "GraphDatabase", _FakeGraphDatabase)
    return captured


def test_driver_gets_explicit_timeouts(monkeypatch):
    """Дефолты драйвера (30/60/30 с) заменены единицами секунд."""
    captured = _capture(monkeypatch)
    store_mod.GraphStore("neo4j://localhost:7687", "neo4j", "pass")
    assert captured["connection_timeout"] == 5.0
    assert captured["connection_acquisition_timeout"] == 10.0
    assert captured["max_transaction_retry_time"] == 5.0


def test_driver_timeouts_are_overridable(monkeypatch):
    """Деплою с медленным удалённым Neo4j нужен выход."""
    captured = _capture(monkeypatch)
    store_mod.GraphStore("neo4j://remote:7687", "neo4j", "pass",
                         connection_timeout=20.0, acquisition_timeout=40.0,
                         max_retry_time=30.0)
    assert captured["connection_timeout"] == 20.0
    assert captured["connection_acquisition_timeout"] == 40.0
    assert captured["max_transaction_retry_time"] == 30.0


def test_settings_expose_timeout_keys():
    """Значения приходят из Settings, а не из констант в конструкторе."""
    s = Settings()
    assert s.neo4j_connection_timeout == 5.0
    assert s.neo4j_acquisition_timeout == 10.0
    assert s.neo4j_max_retry_time == 5.0


def test_notifications_setting_survives(monkeypatch):
    """Прежний аргумент не потерян: notification-спам драйвера по-прежнему заглушен."""
    captured = _capture(monkeypatch)
    store_mod.GraphStore("neo4j://localhost:7687", "neo4j", "pass")
    assert captured["notifications_min_severity"] == "OFF"
```

- [ ] **Шаг 2: Прогнать тесты и убедиться, что они падают**

Run: `.venv/bin/pytest tests/graph/test_store_timeouts.py -q`
Ожидание: FAIL — `KeyError: 'connection_timeout'` и `AttributeError` на ключах `Settings`.

- [ ] **Шаг 3: Реализовать**

В `reviewer/config/settings.py` после `neo4j_password` (строка 81):

```python
    # Таймауты драйвера Neo4j (PRI-276). Дефолты драйвера — 30/60/30 с, и на
    # мёртвом хранилище каждый заход платит их полностью: именно ретраи
    # транзакции дали основную часть наблюдавшихся 162 с. Единиц секунд хватает
    # локальному и типичному удалённому Neo4j; env — выход для медленной сети.
    neo4j_connection_timeout: float = 5.0
    neo4j_acquisition_timeout: float = 10.0
    neo4j_max_retry_time: float = 5.0
```

В `reviewer/graph/store.py` заменить конструктор:

```python
class GraphStore:
    def __init__(self, uri: str, user: str, password: str, *,
                 connection_timeout: float = 5.0,
                 acquisition_timeout: float = 10.0,
                 max_retry_time: float = 5.0):
        # notifications_min_severity="OFF" глушит notification-спам драйвера
        # (напр. «relationship type IMPLEMENTS does not exist», когда граф наполнен
        # только частью типов рёбер) — на выполнение запросов это не влияет.
        # Таймауты заданы явно (PRI-276): с дефолтами драйвера каждый заход в
        # недоступный граф стоит десятки секунд, и молчание выходит дороже отказа.
        self._driver = GraphDatabase.driver(
            uri, auth=(user, password), notifications_min_severity="OFF",
            connection_timeout=connection_timeout,
            connection_acquisition_timeout=acquisition_timeout,
            max_transaction_retry_time=max_retry_time)
```

В трёх местах создания передать значения из `Settings`.

`reviewer/app.py:96`:

```python
        graph = GraphStore(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password,
                           connection_timeout=settings.neo4j_connection_timeout,
                           acquisition_timeout=settings.neo4j_acquisition_timeout,
                           max_retry_time=settings.neo4j_max_retry_time) \
            if connect else None
```

`reviewer/entrypoints/cli.py:909` и `:1199` — тем же набором именованных аргументов, читая их из
локальной переменной настроек (`s`):

```python
        graph = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password,
                           connection_timeout=s.neo4j_connection_timeout,
                           acquisition_timeout=s.neo4j_acquisition_timeout,
                           max_retry_time=s.neo4j_max_retry_time)
```

- [ ] **Шаг 4: Прогнать тесты и убедиться, что они проходят**

Run: `.venv/bin/pytest tests/graph/ tests/entrypoints/ -q`
Ожидание: PASS.

- [ ] **Шаг 5: Коммит**

```bash
git add reviewer/graph/store.py reviewer/config/settings.py reviewer/app.py reviewer/entrypoints/cli.py tests/graph/test_store_timeouts.py
git commit -m "feat(graph): явные таймауты драйвера Neo4j вместо дефолтных десятков секунд"
```

---

### Task 6: документация неочевидного и полная верификация

Проект держит неочевидные факты в `CLAUDE.md`; правка добавляет два таких: замыкание стало
per-store, а отказ графа поднимается из preflight полем, а не броском. Здесь же — полный прогон и
ручной замер, закрывающий вторую половину критерия 3.

**Файлы:**
- Modify: `CLAUDE.md` (блок «Неочевидные факты», абзац про `storage_health.py` / PRI-268 / PRI-277)

**Интерфейсы:**
- Потребляет: всё поведение задач 1-5.
- Производит: документацию; кода не добавляет.

- [ ] **Шаг 1: Прогнать весь набор тестов и линт**

Run: `.venv/bin/pytest -q`
Ожидание: PASS без падений (baseline проекта — все unit-тесты зелёные; падение = регрессия).

Run: `.venv/bin/ruff check reviewer tests`
Ожидание: `All checks passed!`

- [ ] **Шаг 2: Дописать неочевидный факт в `CLAUDE.md`**

В конец абзаца «**Недоступность хранилища — классифицированный сигнал…**» добавить:

```markdown
  **Замыкание раздельно по хранилищам, а отказ графа приходит из preflight полем (PRI-276).**
  `_StorageState.down` — множество бэкендов (`storage_health.BACKEND_GRAPH` /
  `BACKEND_POSTGRES`), а вердикты — словарь по ним: один флаг на двоих при мёртвом
  Neo4j отменял бы и поиск по коду, теряя не одну секцию, а все. Какой бэкенд упал,
  решает тип исключения (`storage_backend`), а параметр `backend` у `_safe` отвечает
  на другой вопрос — кого пропускать; дефолт у него Postgres, и явная разметка есть
  ровно у `related.linked`. Три вещи здесь неочевидны. Во-первых, **нота
  `(task graph unavailable)` живёт на границе MCP-тула, а не в `TaskService`**: пока
  её ставил сервис, исключение не доходило до классификатора вовсе — это и был
  дефект (пустой `gaps` при обеднённом контексте), а публичный контракт тула при
  этом обязан остаться строкой. Во-вторых, **preflight сообщает об отказе графа
  полем `BranchStatus.graph_error`, а не броском**: бросок потерял бы секцию целиком
  (`graph_nodes=None` — валидная деградация, на ней стоит CLI `status`), а без
  сигнала оттуда `related.linked` платит второй таймаут; `build_task_context`
  извлекает ключ `graph_error` из словаря preflight и наружу его не отдаёт — payload
  читает LLM. В-третьих, **при мёртвом Postgres `related.linked` теперь вызывается**:
  граф — другое хранилище, и при живом Neo4j секция собирается вместо того, чтобы
  теряться зря; ценой одного лишнего захода, когда мертвы оба.
```

- [ ] **Шаг 3: Ручной замер (вторая половина критерия 3 и критерий 2)**

При живом ParadeDB остановить контейнер графа и замерить ответ:

```bash
docker compose stop neo4j
```

Затем вызвать `prepare_task_context` для любой задачи (например, через сам скилл
`/rag-reviewer:solve-task PRI-276`) и зафиксировать три вещи: время ответа (было 162.57 с),
наличие в `gaps` записи `section: related.linked` с `cause: storage_unavailable` и то, что шаг 0a
скилла задаёт вопрос пользователю, а не собирает бриф молча. Вернуть инфраструктуру:

```bash
docker compose start neo4j
```

Числа замера записать в тело PR.

- [ ] **Шаг 4: Коммит**

```bash
git add CLAUDE.md
git commit -m "docs(pri-276): замыкание по хранилищам и сигнал отказа графа из preflight"
```

---

## Соответствие критериям приёмки

| Критерий | Где закрывается |
|---|---|
| 1. Запись в `gaps` с `cause: storage_unavailable` и уместным `remedy` | Task 3 (исключение доходит) + Task 2 (`test_neo4j_down_empties_linked_only`) |
| 2. Шаг 0a скилла срабатывает | Следствие критерия 1: скилл ищет равенство `storage_unavailable`; проверка — Task 6, шаг 3 |
| 3. Время ответа сокращено | Task 4 (второй заход не делается: `test_graph_error_from_preflight_skips_linked_without_calling_graph`) + Task 5 (цена первого захода) + ручной замер в Task 6 |
| 4. Теряется ровно `related.linked` | Task 2 (`test_neo4j_down_keeps_postgres_sections`) + Task 4 (preflight остаётся собранной секцией) |
| 5. Тест бросает настоящее neo4j-исключение | Task 2, шаг 1 (`ServiceUnavailable` вместо `RuntimeError`) |
