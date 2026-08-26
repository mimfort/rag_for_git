# PRI-268 — сигнал о недоступном хранилище вместо тихой деградации

Происхождение: `docs/superpowers/briefs/2026-08-25-PRI-268-infra-down-signal.md`

## Задача

При остановленных контейнерах `prepare_task_context` отвечает через десятки минут и отдаёт
обезличенный gap `{"section": "preflight", "reason": "статус индекса недоступен"}`. Из него не
следует ни причина (хранилище не отвечает), ни лекарство, и он неотличим от прочих сбоев секции.
Скилл `solve-task` обязан собрать бриф из того, что пришло, — и бриф на дырявом контексте внешне
неотличим от полного.

Двадцать три минуты дают `psycopg_pool.PoolTimeout`: пул создаётся с дефолтным `timeout=30.0`
(`reviewer/tasks/store.py:98-102`), а `index_batch` ходит в него **по одному разу на задачу**
(`reviewer/tasks/service.py:170`), не прерываясь на первом сбое. 47 изменившихся задач × 30 с.

## Решения, принятые на брейншторме

1. **Инфраструктуру поднимает пользователь, не сервер.** MCP-сервер контейнеры не трогает ни при
   каких условиях: он только классифицирует сбой и называет лекарство. Подъём выполняет скилл
   клиентским `reviewer start` — по спросу пользователя, ровно как шаг 0.1 уже делегирует
   переиндексацию в `rag-reviewer:sync-codebase`. Автоподъём отвергнут: `start_services`
   (`reviewer/compose_lifecycle.py:122`) ждёт до `WAIT_TIMEOUT_SECONDS = 300`, то есть превратил бы
   «висит 23 минуты» в «висит 5 минут» и нарушил критерий 1, а запуск контейнеров как побочный
   эффект чтения контекста — действие без ведома пользователя.
2. **Новых gap'ов на успешном пути не появляется.** Различие «хранилище не отвечает» ↔ «индекс не
   построен» выражается внутри exception-пути: сбой соединения даёт `cause="storage_unavailable"`,
   любой другой сбой секции — `cause="unknown"`. Состояние «индекса нет» остаётся тем, чем является
   сегодня: успешный `preflight` с `drift=null` и без записи в `gaps`. Это буквальное выполнение
   критерия 5 — при живой инфраструктуре payload побайтово тот же.

## Архитектура

### Компонент 1 — `reviewer/storage_health.py` (новый)

Модуль в **корне пакета**, а не в `mcp/` или `tasks/`. Прецедент — `reviewer/rrf.py` (PRI-267):
потребители лежат в `reviewer/mcp/`, `reviewer/tasks/` и `reviewer/entrypoints/`, общего подпакета
ниже них нет, а импорт `entrypoints.cli` из сервисного слоя развернул бы направление зависимости.

Поверхность:

- `CAUSE_STORAGE_UNAVAILABLE = "storage_unavailable"`, `CAUSE_UNKNOWN = "unknown"`,
  `REMEDY_START = "reviewer start"` — константы, чтобы тесты и скилл не сверялись со строковыми
  литералами по месту.
- `is_loopback_endpoint(value: str) -> bool` — перенос `_is_loopback_endpoint`
  (`reviewer/entrypoints/cli.py:816-829`) вместе с `_LOOPBACK_HOSTS` **дословно**, включая фолбэк на
  регэксп `host=([^\s]+)` для libpq keyword/value DSN. В `cli.py` остаётся приватный алиас на новую
  функцию, поэтому оба существующих теста (`tests/entrypoints/test_infra_commands.py:158,167`)
  остаются зелёными без правок.
- `is_storage_unavailable(exc: BaseException) -> bool` — `isinstance` по `psycopg.OperationalError`
  и по neo4j `ServiceUnavailable` / `SessionExpired`.
- `storage_remedy(*endpoints: str) -> str | None` — `REMEDY_START`, если хотя бы один эндпоинт
  loopback, иначе `None` (критерий 3: удалённому деплою локальный docker-стек ничего не чинит).

Два решения внутри модуля стоит зафиксировать, потому что оба неочевидны.

**Почему `isinstance`, а не сверка имён классов в MRO.** Соседний классификатор
`reviewer/config/fetch_errors.py:24` решает по именам в MRO намеренно: он судит исключения
**сменного** VCS-клиента (сегодня httpx, завтра другой), и модуль обязан остаться без зависимостей.
Здесь источник другой: `psycopg` и `neo4j` — жёстко закреплённые драйверы, без которых проект не
работает вовсе, и все потребители нового модуля их уже тянут. Точность важнее развязки:
`psycopg_pool.PoolTimeout` **является подклассом** `psycopg.OperationalError`, поэтому одна проверка
покрывает и таймаут пула, и обрыв соединения, а `psycopg.ProgrammingError` (настоящий баг SQL)
подклассом не является и под «хранилище лежит» не замаскируется. Сверка по именам этого различия
даром не даёт: её пришлось бы отдельно защищать от подстроки, а выигрыш — только развязка, которая
здесь не нужна.

**Почему решение принимается по типу, а не по тексту.** Текст `psycopg.OperationalError` может
содержать DSN с паролем. Ни `str(exc)`, ни `exc.args` в причину не попадают — в `gaps` уходит только
формулировка, собранная модулем. Это то же правило, которым живёт `fetch_errors.py`, и та же
дисциплина, что у `sanitize_provider_text` в `reviewer/tasks/sync.py`.

### Компонент 2 — форма `gaps` (аддитивно)

`reviewer/mcp/task_context.py::gap()` получает два новых ключа; `section` и `reason` сохраняются:

```
{"section": "preflight", "reason": "<текст>", "cause": "storage_unavailable", "remedy": "reviewer start"}
```

`cause` — `storage_unavailable` либо `unknown`. `remedy` — `"reviewer start"` только при
`storage_unavailable` **и** loopback-эндпоинте, иначе `None`. `_safe`
(`reviewer/mcp/task_context.py:32-40`) классифицирует пойманное исключение через
`is_storage_unavailable` и заполняет оба поля; `reason` при `storage_unavailable` заменяется на
формулировку модуля («хранилище не отвечает»), потому что константная строка call-сайта («статус
индекса недоступен») в этом случае называет симптом, а не причину.

Расширение аддитивно: существующие потребители читают `section`/`reason` и не ломаются. Скилл и
тесты ветвятся по `cause`, а не по прозе.

**Эндпоинты приходят через `deps`, а не из `Settings`.** Докстринг `task_context.py` гласит:
«модуль намеренно не знает про Settings и компоненты: источники секций приходят объектом-провайдером,
поэтому вся fail-open-таблица тестируется без Postgres, Neo4j и сети». Обращение к `Settings` из
`_safe` это свойство сломало бы. Поэтому `_TaskContextDeps` получает метод
`storage_endpoints() -> tuple[str, ...]` (возвращает `settings.pg_dsn`, `settings.neo4j_uri` — там
`Settings` доступен законно), `build_task_context` спрашивает его **один раз** в начале и передаёт
готовый `remedy` в `_safe`. Метод читается через `getattr(deps, "storage_endpoints", None)` — тем же
приёмом, что уже применён к `augment_gaps` и закреплён тестом
`test_deps_without_augment_gaps_attribute_still_work` (`tests/mcp/test_prepare_task_context.py:281`),
поэтому существующий `FakeDeps` без нового метода продолжает работать.

### Компонент 2b — короткое замыкание секций в `build_task_context`

Быстрого отказа внутри `index_batch` для критерия 1 **недостаточно**, и это главный вывод разбора.
`build_task_context` собирает девять секций подряд, и store-backed из них — `preflight`,
`warm_board`, `task`, `related.linked`, `related.similar`, `subsystems`, `code`, `test_exemplars`.
Каждая делает собственный заход в пул, каждый ждёт свои 30 с. Даже с идеально исправленным
`index_batch` суммарное время осталось бы порядка 3,5 минут — «десятки минут» превратились бы в
«минуты», а критерий 1 требует секунд.

Поэтому `build_task_context` получает локальный флаг: первая же секция, чьё исключение
классифицировано как `storage_unavailable`, взводит его, и все последующие секции **не вызываются
вовсе**. Каждая пропущенная секция получает свой `default` (как при обычном сбое — payload
по-прежнему содержит все девять ключей) и запись в `gaps` с тем же `cause="storage_unavailable"`,
тем же `remedy` и отдельным `reason` «пропущено: хранилище не отвечает». Итоговое время вызова —
один таймаут пула плюс сетевые операции, не зависящие от стора.

Инвариант `test_every_failure_still_returns_all_sections`
(`tests/mcp/test_prepare_task_context.py:161`) при этом сохраняется: пропуск заполняет секцию
дефолтом, а не удаляет ключ.

Замыкание срабатывает только на `storage_unavailable`. Сбой одной секции по любой другой причине
(`cause="unknown"`) остальные секции не отменяет — сегодняшнее fail-open поведение сохраняется
буквально, и тесты `test_neo4j_down_empties_linked_only`, `test_no_summaries_marks_gap`,
`test_board_unreachable_still_builds_payload` остаются зелёными без правок.

### Компонент 3 — быстрый отказ в `index_batch`

Флаг `storage_down` внутри `reviewer/tasks/service.py::index_batch` (строка 126). Первый сбой, на
котором `is_storage_unavailable(exc)` истинно, взводит флаг; все последующие обращения к стору
пропускаются без захода в пул. Под флагом стоят **все** store-фазы метода: шаг 2 (`existing_hash`,
строки 163-179), шаг 4 (`upsert_task`, 192-217), шаг 5 (`update_meta`, 219-232), снимок links
(235-249) и граф (251+). Флаг один на оба хранилища: при поднятом флаге Neo4j-фаза тоже
пропускается, иначе тот же 47 × таймаут повторился бы на графе.

Шаг 3 (единственный вызов Voyage) при взведённом флаге **не выполняется**: писать результат некуда,
а квота Voyage (3 RPM / 10K TPM) тратится безвозвратно.

Тот же флаг заводится в `TaskService.refresh_meta_batch` (`reviewer/tasks/service.py:285`): его
`update_meta_batch` — один вызов, но следом идёт **по-задачный** цикл `graph.upsert_task`, который на
лежащем Neo4j повторил бы ту же арифметику для задач ниже watermark (в измеренном прогоне их было
75 из 122). Флаг локальный для каждого метода: связывать их через возвращаемое значение значило бы
менять форму результата публичного тула `index_tasks_batch` ради того, что дешевле решается на месте.

**Длина результата сохраняется.** `index_batch` обязан вернуть по строке на каждую входную задачу:
`reviewer/mcp/service.py:981-983` (write-through после `create_subtasks`) проверяет
`len(results) != len(briefs)` и уходит в warning. Пропущенные задачи получают запись той же формы,
что и существующая ветка ошибки шага 2 — все семь ключей (`key`, `embedded`, `links_upserted`,
`links_stored`, `prs_linked`, `warnings`, `retry_required`), `retry_required: True`.

Отсюда следует, что инвариант продвижения курсора не меняется вовсе:
`reviewer/tasks/sync.py:289` гейтит watermark по `not retry_required`, а пропущенные задачи его
взводят — как взводили при 47 отдельных сбоях.

Место правки — `index_batch`, а не `sync.py`: у метода три call-сайта
(`reviewer/tasks/sync.py:244`, `reviewer/mcp/service.py:499` — публичный тул `index_tasks_batch`,
`reviewer/mcp/service.py:980` — write-through), и все три одинаково страдают от N × 30 с. Отдельный
предварительный ping в `sync.py` чинил бы один call-сайт и вводил бы окно между проверкой и
использованием.

### Компонент 4 — шаг скилла

`plugin/skills/solve-task/references/preflight.md` — новый шаг **0.0 «Инфраструктура»**, перед
проверкой свежести (0.1). Скилл читает `payload.gaps`; если хотя бы одна запись имеет
`cause == "storage_unavailable"`, бриф молча не собирается. Форма — трёхвариантный вопрос по образцу
шага 0.4 (сводки подсистем):

1. «Поднять сейчас» — предлагается **только** когда gap несёт `remedy`; скилл выполняет
   `reviewer start`, дожидается результата, один раз повторяет `prepare_task_context` и продолжает.
2. «Подниму сам» — пауза до подтверждения пользователя, затем повторный `prepare_task_context`
   и продолжение.
3. «Продолжить без контекста» — запись в **Constraints / open questions** брифа с `cause` и
   перечнем задетых секций; сборка продолжается.

При `remedy is None` (удалённый деплой) вариант 1 не показывается вовсе — там `reviewer start`
ничего не чинит. В режиме `full-auto` вопрос не задаётся: берётся вариант 1, а при отсутствии
`remedy` — вариант 3, и решение пишется в run-state.

Правка контента под `plugin/` меняет codex payload-digest, поэтому в план входит прогон
`scripts/update_codex_plugin_manifest.py`.

## Тестирование

Все тесты — unit, на фейках: unit-тестам запрещены внешние и localhost-сокеты
(`tests/infrastructure_policy.py`).

| Критерий | Проверка |
|---|---|
| 1 — отвечает за секунды, в gaps причина и команда | `build_task_context` с `preflight`, бросающим `psycopg.OperationalError`: gap несёт `cause="storage_unavailable"` и `remedy="reviewer start"`; фейковый `deps` подтверждает, что остальные store-секции **не вызывались** (`deps.calls`), при этом все девять ключей payload на месте; отдельно — тест числа обращений к стору (см. критерий 4) |
| 2 — «не отвечает» ≠ «не построен» | Два теста на одной секции `preflight`: `psycopg.OperationalError` → `cause="storage_unavailable"`; `RuntimeError` → `cause="unknown"`. Развитие существующей пары `test_postgres_down_empties_retrieval_sections:102` / `test_no_index_marks_gap_and_keeps_going:122` |
| 3 — совет не выдаётся удалённым | `storage_remedy` возвращает `None` для `postgresql://db.example.com/...` и `REMEDY_START` для `127.0.0.1`; существующие `test_infra_commands.py:158,167` остаются зелёными без правок |
| 4 — число попыток закреплено | Фейковый store (образец — `tests/tasks/test_service_batch.py:14`) считает вызовы `existing_hash` и бросает `psycopg_pool.PoolTimeout` на первом; при N задачах ассерт `calls == 1`, `len(results) == N`, у всех `retry_required is True` |
| 5 — живой путь не меняется | Существующие 30 тестов `tests/mcp/test_prepare_task_context.py` проходят без правок; `test_happy_path_has_no_gaps:58` — прямая проверка |

Дополнительно прогоняются: `tests/tasks/test_sync.py`, `tests/tasks/test_sync_cursor.py` (инвариант
watermark), `tests/skills/` (сборка промптов после правки `preflight.md`), `tests/entrypoints/`.

## Вне скоупа

- Автоподъём инфраструктуры сервером — отвергнут выше, не откладывается «на потом».
- Явный короткий `timeout=` у `ConnectionPool` в `reviewer/tasks/store.py` и
  `reviewer/index/store.py`. Он независимо ускорил бы отказ, но меняет поведение всех путей проекта,
  включая ревью PR, а критерий 4 закрывается ранним выходом. Отмечено как отдельная гипотеза.
- Прочие владельцы пулов (`reviewer/tasks/subtask_store.py`, `reviewer/web/history.py`,
  `reviewer/mcp/session_store.py`, `reviewer/index/summary_store.py`) — та же форма ленивого пула, но
  в измеренный сценарий не входят.
- Ретрофит трёх исходных адаптеров досок на общий транспорт — существующий долг, не задет.
