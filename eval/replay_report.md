# Replay-метрики ретрива solve-task

Прогон от 2026-08-17T10:05:12.326505+00:00, репозиторий `mimfort/rag_for_git`, ветка `dev`.

## Идентичность прогона

- **до**: вариант `baseline`, коммит `a87b468febc2eae5e6b313f2456658a4156e0945`, indexed_sha `a1b28c157c94d230db372484b9da01389dc87869`, корпус 56
- **после**: вариант `multiquery`, коммит `a87b468febc2eae5e6b313f2456658a4156e0945`, indexed_sha `a1b28c157c94d230db372484b9da01389dc87869`, корпус 56

## Агрегат

| Метрика | до | после | Δ |
|---|---|---|---|
| core-recall (медиана) | 0.225 | 0.3333 | +0.1083 |
| core-recall (среднее) | 0.2787 | 0.3738 | +0.09515 |
| core-recall bulk (ядро ≥ 10) | 0.1548 | 0.1825 | +0.02778 |
| bulk N | 4 | 4 | +0 |
| precision (медиана) | 0.875 | 0.5 | -0.375 |
| предсказано файлов (медиана) | 2 | 4 | +2 |
| задач измерено | 40 | 40 | +0 |
| без точки измерения | 0 | 0 | +0 |

## Статусы задач

| Статус | Задач |
|---|---|
| measured | 40 |
| empty_core_denominator | 10 |
| no_ground_truth | 6 |
| task_not_in_store | 0 |
| retrieval_failed | 0 |

## Дельта по задачам

| Ключ | Статус | до | после | Δ | приобретено | потеряно |
|---|---|---|---|---|---|---|
| PRI-222 | measured | 0 | 1 | +1 | `reviewer/compose_lifecycle.py`, `reviewer/config/settings.py`, `reviewer/entrypoints/cli.py`, `reviewer/install.py`, `reviewer/web/history.py` | `reviewer/entrypoints/mcp_server.py`, `reviewer/web/api.py` |
| PRI-179 | measured | 0 | 0.5 | +0.5 | `reviewer/graph/store.py`, `reviewer/mcp/service.py` | `reviewer/graph/inherit.py`, `reviewer/graph/scip.py` |
| PRI-212 | measured | 0.5 | 1 | +0.5 | `reviewer/mcp/session_store.py` | — |
| PRI-236 | measured | 0 | 0.5 | +0.5 | `reviewer/entrypoints/cli.py`, `reviewer/mcp/service.py`, `reviewer/services/review_service.py` | `reviewer/config/committed.py`, `reviewer/entrypoints/mcp_server.py` |
| PRI-250 | measured | 0.5 | 0 | -0.5 | `reviewer/entrypoints/mcp_server.py`, `reviewer/metrics/brief_quality/classify.py`, `reviewer/services/brief_quality.py`, `reviewer/tasks/boards/base.py`, `reviewer/web/api.py` | `plugin/hooks/brief_cost.py`, `reviewer/web/history.py` |
| PRI-205 | measured | 0.2 | 0.6 | +0.4 | `reviewer/tasks/boards/base.py`, `reviewer/tasks/boards/yougile.py` | — |
| PRI-239 | measured | 0.1667 | 0.5 | +0.3333 | `reviewer/config/settings.py`, `reviewer/entrypoints/mcp_server.py` | `reviewer/bugreport/environment.py`, `reviewer/bugreport/publish.py` |
| PRI-247 | measured | 0.1667 | 0.5 | +0.3333 | `plugin/hooks/brief_cost.py`, `reviewer/services/cost_sidecar.py`, `reviewer/web/history.py` | — |
| PRI-249 | measured | 0.3333 | 0 | -0.3333 | `reviewer/metrics/brief_quality/classify.py`, `reviewer/metrics/brief_quality/recall.py`, `reviewer/services/gc.py` | `reviewer/web/history.py` |
| PRI-248 | measured | 0.3333 | 0 | -0.3333 | `reviewer/bugreport/environment.py`, `reviewer/install.py`, `reviewer/metrics/brief_quality/classify.py`, `reviewer/tasks/service.py`, `scripts/update_codex_plugin_manifest.py` | `reviewer/mcp/service.py` |
| PRI-217 | measured | 0.25 | 0 | -0.25 | `reviewer/tasks/boards/clickup.py`, `reviewer/tasks/boards/kaiten.py`, `reviewer/tasks/boards/linear.py`, `reviewer/tasks/boards/trello.py` | `reviewer/tasks/boards/__init__.py`, `reviewer/tasks/boards/base.py`, `reviewer/tasks/boards/github.py`, `reviewer/tasks/boards/registry.py` |
| PRI-173 | measured | 0.5 | 0.75 | +0.25 | `reviewer/entrypoints/mcp_server.py` | — |
| PRI-219 | measured | 0.25 | 0.5 | +0.25 | `reviewer/entrypoints/cli.py`, `reviewer/metrics/brief_quality/classify.py` | `reviewer/mcp/service.py` |
| PRI-134 | measured | 0.25 | 0.5 | +0.25 | `reviewer/services/review_service.py` | — |
| PRI-234 | measured | 0.25 | 0.5 | +0.25 | `reviewer/entrypoints/cli.py` | — |
| PRI-221 | measured | 0 | 0.2222 | +0.2222 | `reviewer/config/branches.py`, `reviewer/entrypoints/cli.py`, `reviewer/services/review_service.py` | — |
| PRI-245 | measured | 0.3333 | 0.1111 | -0.2222 | `reviewer/install.py`, `reviewer/metrics/brief_quality/classify.py`, `reviewer/services/gc.py`, `reviewer/web/history.py` | `reviewer/entrypoints/mcp_server.py`, `reviewer/graph/summaries.py`, `reviewer/tools/code_tools.py` |
| PRI-207 | measured | 0.1667 | 0.3333 | +0.1667 | `reviewer/tasks/boards/base.py`, `reviewer/tasks/graph.py` | — |
| PRI-251 | measured | 0 | 0.1429 | +0.1429 | `reviewer/graph/backend.py`, `reviewer/tasks/boards/kaiten.py`, `reviewer/tasks/boards/restbase.py`, `reviewer/tasks/boards/trello.py` | `reviewer/metrics/brief_quality/recall.py`, `reviewer/retrieval/retriever.py`, `reviewer/services/brief_quality.py` |
| PRI-202 | measured | 0.1111 | 0.2222 | +0.1111 | `reviewer/config/settings.py`, `reviewer/entrypoints/mcp_server.py`, `reviewer/services/gc.py` | — |
| PRI-196 | measured | 0.2 | 0.3 | +0.1 | `reviewer/tasks/boards/yougile.py` | — |
| PRI-223 | measured | 0.04 | 0.12 | +0.08 | `reviewer/config/branches.py`, `reviewer/services/review_service.py` | — |
| PRI-225 | measured | 0.1667 | 0.2222 | +0.05556 | `reviewer/mcp/service.py`, `reviewer/tasks/boards/base.py`, `reviewer/tasks/sync_cursor.py` | `reviewer/entrypoints/mcp_server.py` |
| PRI-162 | empty_core_denominator | — | — | — | `reviewer/index/chunker.py`, `reviewer/metrics/brief_quality/classify.py`, `reviewer/services/brief_quality.py`, `reviewer/tasks/taskdoc.py`, `reviewer/web/api.py` | `reviewer/entrypoints/mcp_server.py`, `reviewer/retrieval/retriever.py` |
| PRI-164 | empty_core_denominator | — | — | — | `reviewer/mcp/service.py`, `reviewer/metrics/brief_quality/classify.py`, `reviewer/tasks/boards/base.py`, `reviewer/tasks/taskdoc.py` | — |
| PRI-176 | empty_core_denominator | — | — | — | `plugin/hooks/brief_cost.py`, `plugin/hooks/brief_guard.py`, `reviewer/metrics/brief_quality/classify.py` | — |
| PRI-203 | empty_core_denominator | — | — | — | `reviewer/mcp/service.py`, `reviewer/retrieval/retriever.py`, `reviewer/services/review_service.py` | — |
| PRI-206 | empty_core_denominator | — | — | — | `reviewer/mcp/service.py`, `reviewer/services/review_service.py` | `reviewer/entrypoints/mcp_server.py` |
| PRI-208 | empty_core_denominator | — | — | — | `reviewer/config/settings.py`, `reviewer/entrypoints/cli.py`, `reviewer/install.py`, `reviewer/metrics/brief_quality/classify.py`, `reviewer/policy/policy.py`, `reviewer/services/review_service.py` | `reviewer/mcp/service.py` |
| PRI-211 | measured | 0 | 0 | +0 | `reviewer/mcp/session_store.py`, `reviewer/retrieval/retriever.py` | — |
| PRI-172 | measured | 0 | 0 | +0 | `reviewer/entrypoints/mcp_server.py`, `reviewer/install_codex.py`, `reviewer/vcs/gitlab.py` | `reviewer/mcp/service.py` |
| PRI-213 | measured | 0.2857 | 0.2857 | +0 | `reviewer/tasks/boards/markup.py`, `reviewer/tasks/taskdoc.py` | — |
| PRI-215 | measured | 0.1429 | 0.1429 | +0 | `reviewer/tasks/boards/jira.py`, `reviewer/tasks/boards/linear.py`, `reviewer/tasks/boards/weeek.py`, `reviewer/tasks/boards/yougile.py`, `reviewer/tasks/boards/youtrack.py` | `reviewer/tasks/boards/__init__.py`, `reviewer/tasks/boards/base.py`, `reviewer/tasks/boards/registry.py` |
| PRI-216 | empty_core_denominator | — | — | — | `reviewer/mcp/service.py`, `reviewer/web/app.py`, `reviewer/web/history.py` | — |
| PRI-218 | measured | 1 | 1 | +0 | `reviewer/compose_lifecycle.py`, `reviewer/entrypoints/mcp_server.py`, `reviewer/install.py`, `reviewer/launcher/app.py` | — |
| PRI-177 | measured | 0 | 0 | +0 | `plugin/hooks/brief_cost.py`, `plugin/hooks/brief_guard.py`, `reviewer/metrics/brief_quality/classify.py`, `reviewer/policy/policy.py` | — |
| PRI-220 | empty_core_denominator | — | — | — | `reviewer/entrypoints/cli.py`, `reviewer/gitutil.py`, `reviewer/index/store.py`, `reviewer/services/brief_quality.py`, `reviewer/tasks/boards/asana.py`, `reviewer/tasks/boards/clickup.py` | `reviewer/tasks/boards/base.py` |
| PRI-178 | measured | 1 | 1 | +0 | `reviewer/entrypoints/cli.py`, `reviewer/index/store.py`, `reviewer/policy/context_limits.py`, `reviewer/retrieval/cliff.py`, `reviewer/tasks/service.py` | `reviewer/mcp/service.py` |
| PRI-227 | measured | 0 | 0 | +0 | `reviewer/entrypoints/cli.py` | `reviewer/install.py` |
| PRI-228 | measured | 0.5 | 0.5 | +0 | `reviewer/index/freshness.py`, `reviewer/policy/context_limits.py`, `reviewer/tasks/service.py` | — |
| PRI-235 | measured | 0.5 | 0.5 | +0 | `reviewer/index/store.py` | `reviewer/config/layers.py` |
| PRI-237 | measured | 0 | 0 | +0 | `reviewer/index/chunker.py`, `reviewer/mcp/schemas.py`, `reviewer/metrics/brief_quality/classify.py`, `reviewer/services/gc.py`, `reviewer/services/summary_fragments.py` | — |
| PRI-238 | measured | 0.3333 | 0.3333 | +0 | `reviewer/metrics/brief_quality/classify.py` | `reviewer/tasks/pr_backlink.py` |
| PRI-241 | measured | 1 | 1 | +0 | `reviewer/compose_lifecycle.py`, `reviewer/config/settings.py`, `reviewer/metrics/brief_quality/classify.py` | — |
| PRI-242 | measured | 0.5 | 0.5 | +0 | `reviewer/install.py`, `reviewer/metrics/brief_quality/classify.py` | `reviewer/update_lifecycle.py` |
| PRI-243 | measured | 0 | 0 | +0 | `reviewer/entrypoints/cli.py`, `reviewer/entrypoints/mcp_server.py`, `reviewer/tasks/boards/base.py` | `reviewer/tasks/service.py` |
| PRI-246 | empty_core_denominator | — | — | — | `reviewer/metrics/brief_quality/briefs.py`, `reviewer/metrics/brief_quality/classify.py`, `reviewer/metrics/brief_quality/recall.py` | `reviewer/web/history.py` |
| PRI-252 | measured | 0.6667 | 0.6667 | +0 | `reviewer/gitutil.py`, `reviewer/graph/metrics.py`, `reviewer/mcp/service.py` | — |
| PRI-254 | empty_core_denominator | — | — | — | `reviewer/entrypoints/mcp_server.py`, `reviewer/index/chunker.py`, `reviewer/metrics/brief_quality/classify.py`, `reviewer/metrics/brief_quality/recall.py`, `reviewer/tasks/boards/markup.py`, `reviewer/tasks/taskdoc.py` | `reviewer/retrieval/retriever.py` |
| _и ещё 7_ | без изменений | — | — | — | — | — |

## Оговорка

Линия `replay` и линия `snapshot` **несравнимы напрямую**: snapshot считает пути, которые отобрала LLM из выдачи ретрива, а replay — всю выдачу ретрива. Сравнивать можно только replay с replay.

## Приёмка PRI-255

Мультизапрос с RRF-слиянием: секция `code` контекста задачи ищется набором подзапросов,
извлечённых из структуры задачи, вместо одного запроса на весь её текст.

### Критерий 1 — распределение числа подзапросов производно от размера задачи

`python -m eval.solve_task_metrics subqueries --repo mimfort/rag_for_git --branch dev`,
корпус 56 задач:

| класс задачи | задач | медиана подзапросов | мин | макс |
|---|---|---|---|---|
| мелкая (≤10 строк) | 7 | 2 | 1 | 2 |
| средняя (11-30) | 19 | 11 | 2 | 18 |
| развёртка (>30) | 30 | 14 | 2 | 20 |

Не константа: 2 против 14 между крайними классами. Предохранитель `MAX_SUBQUERIES = 20`
срабатывает на самых крупных задачах (макс = 20 ровно).

### Критерий 2 — дельта bulk core-recall в replay

Обе стороны сняты в одном прогоне, на одном `indexed_sha` (`a1b28c1`) и одном коммите
(`a87b468`), корпус 56, измерено 40 задач:

| Метрика | до (`baseline`) | после (`multiquery`) | Δ |
|---|---|---|---|
| core-recall (медиана) | 0.225 | 0.3333 | **+0.1083** |
| core-recall bulk (ядро ≥ 10) | 0.1548 | 0.1825 | **+0.02778** |
| bulk N | 4 | 4 | — |
| precision (медиана) | 0.875 | 0.5 | −0.375 |
| предсказано файлов (медиана) | 2 | 4 | +2 |

Дельта положительна по обеим линиям recall. Три оговорки, без которых число читается неверно:

1. **Baseline здесь 0.1548, а не 0.127 из прошлого отчёта.** Прошлая цифра снята на другом
   корпусе (55 задач) и другом `indexed_sha`; сравнивать между отчётами нельзя, сравнивать
   можно только две стороны одного прогона — они и приведены.
2. **`bulk N = 4`.** Bulk-дельта опирается на четыре задачи, то есть её доверительный
   интервал широк. Медианная дельта (+0.1083, N=40) — куда более надёжный сигнал того, что
   рычаг работает.
3. **Precision упала с 0.875 до 0.5, число файлов выросло с 2 до 4.** Это ожидаемая цена, а
   не побочный дефект: механизм потери был именно в том, что выдача сжималась до двух файлов.
   Отбор нужного из четырёх — работа LLM-сборщика брифа, а вот отсутствующего в выдаче файла
   он не восстановит никак.

Не все задачи выиграли: `PRI-217` (11 адаптеров досок) просела на −0.25, `PRI-248`/`PRI-249`
на −0.3333. Разбор просадок в скоуп PRI-255 не входит — соседние рычаги той же программы
(ID-310 файловая диверсификация, ID-311 diff-пути похожих задач) остаются впереди.

### Критерий 3 — файлы, найденные только хвостовым подзапросом

Замер воспроизводится так (подкоманды у него нет — разовая проверка, живой Voyage, несколько
query-эмбеддингов):

```python
from eval.solve_task_metrics import live
from eval.solve_task_metrics.context_paths import extract_context_paths
from reviewer.mcp.subqueries import build_subqueries

provider, repo, branch = live.open_live("mimfort/rag_for_git", "dev")
with provider:
    for key in ("PRI-217", "PRI-222"):
        task = provider.task(key)
        queries = build_subqueries(task, provider.query(task, key))
        head = extract_context_paths(provider.code_multi(repo, branch, queries[:1], None))
        full = extract_context_paths(provider.code_multi(repo, branch, queries, None))
        print(key, len(queries), sorted(full - head))
```

Задачи-развёртки, сравнение выдачи по одному `q0` против полного набора подзапросов:

| Задача | подзапросов | только от `q0` | доехало только от хвостовых |
|---|---|---|---|
| PRI-217 | 20 | 4 файла | `reviewer/tasks/boards/clickup.py`, `reviewer/tasks/boards/kaiten.py` |
| PRI-222 | 12 | 5 файлов | `reviewer/compose_lifecycle.py`, `reviewer/entrypoints/cli.py`, `reviewer/web/app.py` |

Множество непустое на обеих задачах, и это именно те файлы, которых однозапросная выдача
не давала вовсе. У `PRI-222` два из трёх (`reviewer/entrypoints/cli.py`,
`reviewer/compose_lifecycle.py`) входят в её фактический diff — то есть хвостовой подзапрос
принёс не шум, а ядро.

### Критерий 4 — токены LLM не растут

Извлечение подзапросов детерминированное (`reviewer/mcp/subqueries.py`, только `re`), LLM в
нём не участвует. Эмбеддинги: один батч-вызов на сборку секции
(`tests/retrieval/test_multiquery.py::test_one_batched_embedding_call_per_assembly`).
Рост Voyage-эмбеддингов задачей разрешён явно, рост LLM-токенов — нет, и его нет.
