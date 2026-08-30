# PRI-276 — отказ Neo4j как машиночитаемый сигнал, а не 162 с молчания

Происхождение: `docs/superpowers/briefs/2026-08-30-PRI-276-neo4j-down-silent-gap.md`

## Задача

При живом ParadeDB и остановленном Neo4j `prepare_task_context` отвечает 162.57 с (на живой
инфраструктуре — 8.5 с), `gaps` остаётся пустым списком, а `related.linked` содержит текстовую
заглушку `(task graph unavailable)`. Замер — приёмка PRI-269, дефект Д-5
(`eval/pri269_acceptance_report.md`, разделы «Критерий 9» и «Д-5»).

Это ровно тот класс дефекта, ради которого делался PRI-268: контекст обеднён, но машиночитаемо
неотличим от полного. Шаг 0a скилла `solve-task` ищет в `payload.gaps` запись с
`cause: storage_unavailable`; её здесь нет, поэтому вопрос пользователю не задаётся и бриф
собирается без связанных задач молча. Строка внутри значения секции сигналом не является.

Классифицировать этот отказ система умеет: `is_storage_unavailable`
(`reviewer/storage_health.py:54-66`) покрывает neo4j `ServiceUnavailable`/`SessionExpired`.
Дефект в том, что до классификатора исключение не доходит — `TaskService.get_task_context`
(`reviewer/tasks/service.py:415-426`) глотает его и возвращает ноту.

Цена молчания складывается из двух проглоченных заходов в мёртвый граф, а не из одного.
Первый — в preflight: `build_status_report` (`reviewer/services/status.py:63-67`) обёртывает
`graph.count_nodes` в `except Exception → graph_nodes = None`. Второй — `related.linked`.
Каждый заход платит дефолтные таймауты драйвера, который создаётся без единого явного значения
(`reviewer/graph/store.py:10-11`): 60 с на получение соединения, 30 с на ретраи транзакции.

## Критерии приёмки (из задачи)

1. Отказ Neo4j даёт запись в `gaps` с `cause: storage_unavailable` и уместным `remedy`.
2. Шаг 0a скилла `solve-task` срабатывает и спрашивает пользователя, а не собирает бриф молча.
3. Время ответа при мёртвом Neo4j сокращено с наблюдавшихся 162 с.
4. Состав потерянных секций не изменился: ровно `related.linked`.
5. Тест бросает настоящее neo4j-исключение.

## Принятые решения

| Развилка | Решение | Отвергнутая альтернатива и почему |
|---|---|---|
| Как развести замыкание, не нарушив критерий 4 | Разметка секции бэкендом + класс упавшего бэкенда по типу исключения | Общий флаг `down` при отказе Neo4j замкнул бы Postgres-секции и потерял бы не одну секцию, а все — прямое нарушение критерия 4. Замыкание только по типу исключения без разметки не замыкает ничего для Postgres — регресс PRI-268. |
| Кто решает, какой бэкенд упал | Тип исключения (`storage_backend(exc)`), а не разметка секции | Разметка отвечает на другой вопрос — «кого пропускать». Если бы класс упавшего брался из неё, Postgres-секция, упавшая neo4j-исключением, замкнула бы не тот бэкенд. Тот же принцип, что у `is_storage_unavailable`: решает `isinstance`, а не текст и не соседний параметр. |
| Как поднять факт недоступности графа из preflight | Поле `BranchStatus.graph_error` + ключ `graph_error` в словаре `deps.preflight` | Оставить preflight как есть — значит не выполнить критерий 3 вовсе: первый таймаут остаётся, а `related.linked` платит второй. Пробрасывать исключение наружу из `build_status_report` — потерять секцию preflight целиком (нарушение критерия 4) и сломать CLI `status`, которому `graph_nodes=None` штатно нужен. |
| Где живёт нота `(task graph unavailable)` | На границе публичного MCP-тула | Оставить ноту в `TaskService` — сохранить сам дефект: исключение так и не дойдёт до `_safe`. Пробросить её до тула — сломать публичный контракт, на который опираются скиллы `decompose-task`/`solve-task`, чего задача не требует. Отдельный метод `task_context_or_raise` даёт два пути к одному поведению, которые разойдутся. |
| Входят ли таймауты драйвера в скоуп | Входят, с ключами в `Settings` | Без них 162 с сокращаются лишь до ~80 с: замыкание срезает второй заход, но первый платит полную цену дефолтных ретраев. Жёсткие константы без env лишают деплой с удалённым Neo4j выхода при медленной сети. |
| Чем закрывать критерий 3 | Юнит на число обращений к графу + ручной замер в приёмке | Тест на время в CI флакует по построению. Один ручной замер без юнита оставляет механизм незакреплённым: регресс вернётся молча. |

## Архитектура

### 1. `reviewer/storage_health.py` — класс упавшего бэкенда

Новый публичный контракт рядом с существующей классификацией:

```python
BACKEND_GRAPH = "graph"
BACKEND_POSTGRES = "postgres"

def storage_backend(exc: BaseException) -> str | None: ...
```

Решение принимает `isinstance`, а не текст: neo4j `ServiceUnavailable`/`SessionExpired` →
`BACKEND_GRAPH`, `psycopg.OperationalError` (включая подкласс `psycopg_pool.PoolTimeout`) →
`BACKEND_POSTGRES`, всё прочее → `None`. Покрытие намеренно совпадает с
`is_storage_unavailable`: две функции отвечают на связанные вопросы об одном множестве
исключений — «лечится ли подъёмом контейнеров» и «какое из хранилищ молчит».

Мотив тот же, что у `classify_storage_failure` (PRI-277): в тексте `OperationalError` живёт DSN
с паролем, поэтому по тексту не решает ничего.

### 2. `reviewer/mcp/task_context.py` — замыкание per-store

`_StorageState` перестаёт быть булевым:

```python
class _StorageState:
    def __init__(self, endpoints: tuple[str, ...]) -> None:
        self.endpoints = endpoints
        self.down: set[str] = set()
        self.diagnoses: dict[str, StorageDiagnosis] = {}

    def mark(self, backend: str, exc: BaseException) -> StorageDiagnosis: ...
    def is_down(self, backend: str) -> bool: ...
```

Вердикт (`cause_detail`, `remedy`) хранится **по бэкенду**: при падении обоих хранилищ причины
различны — неверный пароль Postgres и остановленный Neo4j не должны получить один вердикт на
двоих. Как и прежде, вердикт считается лениво, при первом сбое своего бэкенда.

`_safe` получает параметр `backend: str = BACKEND_POSTGRES`:

```python
def _safe(payload, section, produce, default, reason, state,
          backend: str = BACKEND_POSTGRES):
    if state.is_down(backend):
        _storage_gap(payload, section, SKIPPED_REASON, state, backend)
        return default
    try:
        return produce()
    except Exception as exc:
        log.warning(...)
        if is_storage_unavailable(exc):
            failed = storage_backend(exc) or backend
            state.mark(failed, exc)
            _storage_gap(payload, section, STORAGE_REASON, state, failed)
        else:
            payload["gaps"].append(gap(section, reason))
        return default
```

Явную разметку `backend=BACKEND_GRAPH` получает ровно одна секция — `related.linked`. Остальные
остаются на дефолте: Postgres — их хранилище, и забытая разметка деградирует в сегодняшнее
поведение, а не в потерю секции.

`_storage_gap` получает бэкенд параметром и берёт вердикт из `state.diagnoses[backend]`, а не из
единственного поля. Третий её вызывающий — ветка `elif warm_board and not board` в
`build_task_context` — спрашивает `state.is_down(BACKEND_POSTGRES)`: доска читается из Postgres.

Форма записи `gap()` не меняется: пять ключей `section`/`reason`/`cause`/`cause_detail`/`remedy`,
`cause` остаётся `storage_unavailable` — шаг 0a скилла ищет равенство именно ему.

### 3. `reviewer/services/status.py` — сигнал графа, не теряя секцию

`BranchStatus` получает поле:

```python
graph_error: BaseException | None = None
```

`build_status_report` продолжает глотать отказ графа в `graph_nodes=None` — CLI `status`,
его текстовый рендер и `render_status_json` не меняются вовсе (JSON перечисляет поля явно,
поэтому исключение в него не утечёт; сериализовать его и не требуется). Дополнительно
исключение сохраняется в новом поле.

`_TaskContextDeps.preflight` протягивает его в возвращаемый словарь ключом `graph_error` —
это часть контракта провайдера секций, а не служебная деталь: фейк в юнит-тестах отдаёт его так же.

`build_task_context` извлекает ключ до записи в payload:

```python
preflight = _safe(payload, "preflight", ..., BACKEND_POSTGRES)
payload["preflight"] = _absorb_graph_error(preflight, state)
```

`_absorb_graph_error` делает `pop("graph_error", None)` и, если это storage-исключение, вызывает
`state.mark(BACKEND_GRAPH, exc)`. Наружу исключение не выходит: payload уходит в LLM, а объекту
исключения там делать нечего. Упавший preflight (`None` вместо словаря) и старый провайдер без
этого ключа проходят функцию без изменений — она возвращает вход как есть.

Дальше запись в `gaps` возникает сама и на правильной секции: `related.linked` видит взведённый
флаг графа, **не обращается к графу вовсе** и пишет `SKIPPED_REASON` с `cause: storage_unavailable`
и `remedy`. У preflight записи в `gaps` нет — секция собрана полностью. Это и есть критерий 4:
теряется ровно `related.linked`.

### 4. Граница ноты `(task graph unavailable)`

`TaskService.get_task_context` (`reviewer/tasks/service.py`):

```python
try:
    ctx = self._graph.task_context(key, project or "")
except Exception as exc:
    if is_storage_unavailable(exc):
        raise
    log.warning(...)
    return "(task graph unavailable)"
```

Отсутствие графа (`self._graph is None`) и прочие сбои обхода дают ноту как прежде — меняется
поведение ровно для недоступного хранилища.

`MCPReviewService.get_task_context` (публичный тул, `reviewer/mcp/service.py:506-508`) ловит
storage-исключение и возвращает ту же ноту: публичный контракт, на который опираются скиллы
`decompose-task` и `solve-task`, остаётся побайтово прежним.

`_TaskContextDeps.linked` зовёт `self._service.components.task_service.get_task_context(...)`
напрямую, минуя обёртку, — исключение доходит до `_safe`.

### 5. `reviewer/graph/store.py` + `reviewer/config/settings.py` — цена одного захода

`GraphStore.__init__` получает три необязательных аргумента с дефолтами и передаёт их драйверу:

| Аргумент | Дефолт | Дефолт драйвера |
|---|---|---|
| `connection_timeout` | 5 с | 30 с |
| `connection_acquisition_timeout` | 10 с | 60 с |
| `max_transaction_retry_time` | 5 с | 30 с |

Значения приходят из новых ключей `Settings` (`neo4j_connection_timeout`,
`neo4j_acquisition_timeout`, `neo4j_max_retry_time`) с env-переопределением — деплою с медленным
удалённым Neo4j нужен выход. Три существующих места создания `GraphStore` (`app.py:96`,
`cli.py:909`, `cli.py:1199`) передают значения из `Settings`; аргументы необязательны, поэтому
конструкции в тестах не ломаются.

Основной вклад в 162 с даёт `max_transaction_retry_time`: `execute_query` ретраит
`ServiceUnavailable` до его исчерпания на каждом заходе. Драйвер шарится с `TaskGraph`
(`GraphStore.driver`), поэтому один рычаг покрывает оба пути обращения к графу.

## Изменение закреплённого поведения

`tests/mcp/test_prepare_task_context.py:336-339` закрепляет `deps.calls == ["preflight"]` при
мёртвом Postgres. С per-store замыканием `related.linked` больше не замыкается Postgres-сбоем и
будет вызвана: граф — другое хранилище, и при живом Neo4j секция теперь собирается вместо того,
чтобы теряться зря. Тест обновляется осознанно, ожидание становится
`["preflight", "linked"]`. Худший случай — оба хранилища мертвы — стоит одного лишнего захода
в граф, а не восьми: остальные секции по-прежнему замкнуты.

## Тестирование

Юниты (`tests/mcp/test_prepare_task_context.py`):

- `test_neo4j_down_empties_linked_only` переписан на `neo4j.exceptions.ServiceUnavailable`
  вместо `RuntimeError`; проверяет `cause == "storage_unavailable"` и наличие `remedy`, а не
  только пустоту секции (критерий 5 и критерий 1).
- Отказ Neo4j не замыкает Postgres-секции: `code`, `test_exemplars`, `related.similar`,
  `subsystems` собраны полностью (критерий 4).
- Отказ Postgres по-прежнему замыкает свои секции (регресс PRI-268 не допущен).
- `graph_error` из preflight: `deps.linked` не вызывается **ни разу**, а запись в `gaps` есть —
  юнит-половина критерия 3, по числу обращений к графу.
- Вердикты не сливаются: при падении обоих хранилищ `remedy`/`cause_detail` берутся каждый из
  своего бэкенда.

Юниты остальных модулей:

- `tests/test_storage_health.py` — `storage_backend` на neo4j-, psycopg- и посторонних
  исключениях, включая `PoolTimeout`.
- `tests/tasks/test_service.py` — `get_task_context` пробрасывает `ServiceUnavailable`, но
  по-прежнему возвращает ноту при `graph is None` и при постороннем сбое обхода.
- `tests/mcp/test_service.py` — публичный `MCPReviewService.get_task_context` возвращает ноту
  при storage-исключении.
- `tests/services/test_status.py` — `graph_error` заполняется при сбое `count_nodes`,
  `graph_nodes` остаётся `None`, и поля нет в выводе `render_status_json`.
- `tests/graph/` — `GraphStore` передаёт три таймаута в драйвер и берёт их из `Settings`.

Ручной шаг приёмки (вторая половина критерия 3, и критерий 2): при остановленном
`rag-reviewer-neo4j-1` и живом ParadeDB замерить время `prepare_task_context`, зафиксировать
запись в `gaps` и убедиться, что шаг 0a скилла `solve-task` задаёт вопрос пользователю.

## Вне скоупа

- `TaskService.count_tasks` — тот же паттерн проглатывания графа, но вне пути
  `prepare_task_context`.
- Смежные открытые дефекты того же класса: ID-330 (`PoolTimeout` выдаётся за недоступность),
  ID-331 (два таймаута пула в preflight из-за `_clone_path`), ID-328 (эмбеддер классифицируется
  как `unknown`).
- Публичный контракт тула `get_task_context`: молчаливая нота для его вызывающих остаётся
  как есть.
