# Дизайн — поле `summaries` в `reviewer status` (PRI-219)

Задача: https://ru.yougile.com/team/686c049c8af8/#PRI-219
Бриф: `docs/superpowers/briefs/2026-07-28-PRI-219-status-json-summaries-field.md`

## 1. Проблема

Скилл `solve-task` в preflight (Step 0.4 «Summary warmth») вызывает
`get_subsystem_summaries(repo, branch)` **без `query`** ради единственного факта — построены ли
сводки подсистем. В этом режиме сервер (`reviewer/mcp/service.py:1064`) возвращает
`store.get_summaries(...)`, то есть все сводки целиком, с полными текстами.

Замер на этом репозитории (прогон solve-task от 2026-07-28, ветка `dev`): ответ — **68 882
символа, 226 строк**, порядка 8k токенов. Он не поместился в контекст и был выгружен в файл. Эти
данные садятся в контекст оркестратора **до** диспатча brief-субагента, то есть в ту самую
сессию, которая дальше ведёт brainstorming, и на результат не влияют: нужен только счётчик.

При этом Step 0.1 уже вызывает `reviewer status --json`. Его per-branch payload
(`reviewer/services/status.py:82-97`) отдаёт `chunks`, `graph_nodes`, `drift`, но не число
сводок — хотя это такой же факт о здоровье индекса ветки, а `SummaryStore.count_summaries`
(`reviewer/index/summary_store.py:145`) уже существует и fail-soft по `UndefinedTable`.

Порог `summary_topk_threshold: 20` в `.review.yml` означает, что рабочий вызов сводок в Step 3
уже идёт по ANN-пути и возвращает top-8. Раздут исключительно preflight.

## 2. Решение

Число сводок становится частью отчёта `reviewer status` — там же, где `chunks` и `graph_nodes`.
Потребитель (скилл) читает его из payload, который и так получает, и перестаёт звать
`get_subsystem_summaries` в preflight.

Серверный `get_subsystem_summaries` **не меняется**, включая полный дамп без `query`: экономия
достигается тем, что потребитель перестаёт его звать. Это сохраняет поведение для `ask` и
`pr-walkthrough`, которые ходят с `query`.

### 2.1 Ключевое свойство: один `null`-путь у потребителя

Отсутствие ключа в JSON (деплой старше этой версии) и сбой стора схлопываются у потребителя в
одно значение — `null`. Поэтому скиллу не нужно ловить исключения ради обратной совместимости:
обе ситуации ведут в одну фолбэк-ветку.

Именно поэтому отклонены альтернативы (см. §7).

## 3. Изменения в `reviewer/services/status.py`

### 3.1 `BranchStatus`

Новое поле **`summaries: int | None = None` — последним и с дефолтом**. Причина не
стилистическая: dataclass конструируется позиционно в четырёх местах
`tests/services/test_status.py` (:68-71, :89, :105-108, :129); поле без дефолта или в середине
списка сломает их на арности. В проде единственный конструктор (`status.py:64-66`) передаёт
значение по имени.

### 3.2 `build_status_report`

Сигнатура получает keyword-only параметр с дефолтом:

```python
def build_status_report(store, graph, repo: str, branches: list[str],
                        repo_path: str, *, summary_store=None) -> RepoStatus:
```

Внутри цикла по веткам — зеркало fail-soft блока для `graph_nodes` (`status.py:59-62`):

```python
try:
    summaries = summary_store.count_summaries(repo, branch) if summary_store else None
except Exception:  # noqa: BLE001 — стор сводок недоступен
    summaries = None
```

Дефолт `None` сохраняет оба существующих вызова (`cli.py:608` и monkeypatch'и в тестах) без
правок — это критерий приёмки №2.

Семантика значений:

| Значение | Смысл |
|---|---|
| `int > 0` | сводки построены |
| `0` | сводок нет (в т. ч. таблицы нет — `count_summaries` ловит `UndefinedTable → 0`) |
| `None` | неизвестно: стор не передан или упал |

### 3.3 Рендеры

`render_status_json` — ключ `"summaries": b.summaries` в per-branch объекте, рядом с
`graph_nodes`; `None` сериализуется в `null`.

`render_status` — расширяется существующая строка счётчиков (`status.py:127`):

```
  Чанки:  1843   Узлы графа: 1207   Сводки: 26
```

При `summaries is None` → `Сводки: —`.

Для непроиндексированной ветки функция делает `continue` раньше этой строки
(`status.py:114-117`), поэтому сводки там не печатаются — ровно как сейчас не печатаются чанки и
узлы графа. Существующее поведение не меняется.

## 4. Изменения в `reviewer/entrypoints/cli.py`

Команда `status` (:590-613) получает третий стор рядом с `ChunkStore`/`GraphStore`:

```python
from reviewer.index.summary_store import SummaryStore
...
summary_store = SummaryStore(s.pg_dsn, min_size=s.pg_pool_min_size, max_size=s.pg_pool_max_size)
try:
    report = build_status_report(store, graph, repo, branches, path,
                                 summary_store=summary_store)
except psycopg.OperationalError as e:
    raise click.ClickException(f"Postgres недоступен: {e}")
finally:
    store.close()
    graph.close()
    summary_store.close()
```

Конструирование безопасно: `SummaryStore.__init__` (`summary_store.py:18-23`) только сохраняет
параметры, пул открывается лениво в `_ensure_pool`. Если Postgres лежит, команда падает на
`count_chunks` раньше, чем дойдёт до сводок, — существующая обработка
`psycopg.OperationalError` остаётся валидной.

## 5. Изменения в `plugin/skills/solve-task/SKILL.md` (Step 0.4)

Проверка перестаёт быть отдельным вызовом и становится чтением поля из payload, полученного в
Step 0.1. Инструкция явно запрещает вызывать `get_subsystem_summaries` в основном пути — без
этого запрета правка бессмысленна.

| Состояние | Поведение |
|---|---|
| `drift == null` | проверка пропускается (как сейчас — индекса нет, сводок быть не может) |
| `summaries > 0` | молча продолжить |
| `summaries == 0` | те же три опции: «Прогреть сейчас» / «Прогрею сам» / «Пропустить» |
| `summaries == null` **или ключа нет** | фолбэк: вызвать `get_subsystem_summaries(repo, branch)` и использовать его счётчик, с теми же тремя опциями и тем же fail-open |

Ветка «Прогрею сам» перепроверяет результат **повторным `reviewer status --branch <branch>
--json`**, а не вызовом MCP-тула. В фолбэк-ветке перепроверка остаётся прежней.

## 6. Тестирование

### 6.1 `tests/services/test_status.py`

Добавляется `FakeSummaryStore(counts, fail=False)` по образцу `FakeGraph` (:25-32), и проверки:

1. **happy path** — счётчики попадают в `BranchStatus.summaries` per-branch;
2. **без `summary_store`** — `summaries is None` (критерий приёмки №2);
3. **стор бросает исключение** — `summaries is None` (по образцу теста «Neo4j down», :53-60);
4. `render_status_json` — `summaries` в per-branch объекте: число и `null`;
5. `render_status` — присутствуют «Сводки: 26» и «Сводки: —».

В обоих CLI-тестах (`test_status_command_smoke` :85, `test_status_command_json` :125) добавляется
`monkeypatch.setattr(cli_mod, "SummaryStore", MagicMock())` рядом с уже мокнутыми
`ChunkStore`/`GraphStore`.

### 6.2 `tests/skills/test_preflight_guardrail.py`

Guard на Step 0.4 — закрывает критерий приёмки №4, иначе непроверяемый. Секция вырезается из
`SKILL.md` по границам (от `4. **Summary warmth.**` до `Decisions:`) и проверяется:

- в ней встречается `summaries` — счётчик читается из статуса;
- присутствует фолбэк-ветка для старого деплоя;
- `get_subsystem_summaries` встречается в секции **ровно один раз** — только в фолбэке.

Последний ассерт — главный: сейчас упоминаний три (основной вызов, опция «Прогрею сам»,
fail-open), и счётчик надёжнее строкового поиска ловит регресс «вернули дамп обратно». Он же
задаёт требование к формулировке §5: новый текст Step 0.4 должен называть
`get_subsystem_summaries` ровно единожды — в фолбэк-ветке; правило fail-open в ней
формулируется без повторного упоминания имени тула.

Существующие guard-тесты (`test_solve_task_has_preflight` и др.) от правки не ломаются: они
проверяют наличие `reviewer status`, `drift`, `sync_board(`, `reviewer_sync-codebase` — всё
остаётся на месте.

## 7. Отклонённые альтернативы

- **`count_only=True` у `get_subsystem_summaries`** — оставляет лишний вызов в preflight и на
  старом деплое падает ошибкой валидации параметра вместо мягкого `null`.
- **Отдельный тул `count_subsystem_summaries`** — описание каждого MCP-тула есть постоянный
  налог на контекст всех сессий; разовая экономия одного прогона его не окупает.
- **CLI считает сводки сам и передаёт `dict[branch, int | None]`** — уводит fail-soft логику в
  CLI и ломает симметрию с `graph_nodes`; команда `status` намеренно тонкая.
- **Одно поле на репо, а не на ветку** — противоречит критерию приёмки №1 и мульти-бранч модели
  индекса: сводки скоупятся `(repo, branch)`.
- **Отсутствие ключа трактовать как `0`** — на старом CLI скилл ложно предлагал бы прогрев уже
  построенных сводок.
- **Path B (инлайн-сборка брифа без override модели, где весь ретрив идёт в главный контекст)** —
  вне скоупа задачи.

## 8. Документация и релиз

- `README.md` и `README.ru.md` правятся **синхронно**: поток preflight у `solve-task`
  (`README.md:779`, `README.ru.md:698`) и блок диагностических команд (`README.md:653`,
  `README.ru.md:573`).
- Версия `0.4.1 → 0.4.2` в `pyproject.toml` и `plugin/.claude-plugin/plugin.json`, затем
  `python scripts/update_codex_plugin_manifest.py`: правка `plugin/` меняет codex payload-digest,
  без пересборки install-тесты краснеют.
- Ветка `feature/pri-219` от `origin/dev`. Локальный `dev` на момент проектирования отставал на 5
  коммитов (`b9e1c8e` против `5ae7193`) — перед ветвлением подтянуть.
- Конфликт с незамерженной `feature/pri-177`: обе ветки правят `plugin/skills/solve-task/SKILL.md`
  (разные разделы — Step 0.4 против Step 5) и digest-строку в `plugin/.codex-plugin/plugin.json`.
  Git смержит SKILL.md сам; конфликт digest при мерже второго PR разрешается повторным прогоном
  `update_codex_plugin_manifest.py`.

## 9. Критерии приёмки

1. `reviewer status --json` содержит `summaries` в каждом branch-объекте: число при доступном
   сторе, `null` при сбое.
2. `build_status_report` без `summary_store` возвращает `summaries=None` — существующие вызовы и
   тесты не ломаются.
3. Текстовый `reviewer status` показывает число сводок; «—», когда неизвестно.
4. `solve-task` в preflight не вызывает `get_subsystem_summaries`, когда статус вернул
   `summaries`; при `null` или отсутствии ключа поведение и три опции идентичны сегодняшним.
5. Unit-тесты покрывают happy path, `None` и исключение стора, оба рендера, а также Step 0.4
   guard'ом.
6. `.venv/bin/pytest -q` зелёный; `ruff check` чист по изменённым файлам.
