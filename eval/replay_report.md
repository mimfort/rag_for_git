# Replay-метрики ретрива solve-task

Прогон от 2026-08-17T11:34:44.755800+00:00, репозиторий `mimfort/rag_for_git`, ветка `dev`.

## Идентичность прогона

- **до**: вариант `multiquery`, параметры `{'code_section': {'max_files': 4, 'max_chunks_per_file': 1, 'chars_per_file': 2000}}`, коммит `25a7aa93d6061602e976e60000c57c11b09c1eee`, indexed_sha `7d66a08baa462be1a2edde216dfbdc66a27fa68f`, корпус 57
- **после**: вариант `multiquery`, коммит `25a7aa93d6061602e976e60000c57c11b09c1eee`, indexed_sha `7d66a08baa462be1a2edde216dfbdc66a27fa68f`, корпус 57

## Агрегат

| Метрика | до | после | Δ |
|---|---|---|---|
| core-recall (медиана) | 0.2857 | 0.5 | +0.2143 |
| core-recall (среднее) | 0.3355 | 0.5242 | +0.1887 |
| core-recall bulk (ядро ≥ 10) | 0.1911 | 0.3544 | +0.1633 |
| bulk N | 4 | 4 | +0 |
| precision (медиана) | 0.5 | 0.25 | -0.25 |
| предсказано файлов (медиана) | 4 | 12 | +8 |
| задач измерено | 41 | 41 | +0 |
| без точки измерения | 0 | 0 | +0 |

## Статусы задач

| Статус | Задач |
|---|---|
| measured | 41 |
| empty_core_denominator | 10 |
| no_ground_truth | 6 |
| task_not_in_store | 0 |
| retrieval_failed | 0 |

## Дельта по задачам

| Ключ | Статус | до | после | Δ | приобретено | потеряно |
|---|---|---|---|---|---|---|
| PRI-218 | measured | 0 | 1 | +1 | `reviewer/compose_lifecycle.py`, `reviewer/entrypoints/cli.py`, `reviewer/launcher/command.py`, `reviewer/launcher/controller.py`, `reviewer/tasks/taskdoc.py`, `reviewer/versioning.py`, `reviewer/web/history.py`, `scripts/verify_launcher_distribution.py` | — |
| PRI-252 | measured | 0 | 0.6667 | +0.6667 | `reviewer/config/committed.py`, `reviewer/entrypoints/cli.py`, `reviewer/graph/scip.py`, `reviewer/graph/store.py`, `reviewer/install.py`, `reviewer/services/brief_quality.py`, `reviewer/tasks/boards/youtrack.py`, `reviewer/tasks/service.py` | — |
| PRI-207 | measured | 0.5 | 1 | +0.5 | `reviewer/entrypoints/mcp_server.py`, `reviewer/mcp/service.py`, `reviewer/mcp/task_context.py`, `reviewer/policy/policy.py`, `reviewer/tasks/boards/weeek.py`, `reviewer/tasks/boards/yougile.py`, `reviewer/tasks/boards/youtrack.py`, `reviewer/tasks/store.py` | — |
| PRI-210 | measured | 0.5 | 1 | +0.5 | `reviewer/config/settings.py`, `reviewer/graph/store.py`, `reviewer/index/struct_diff.py`, `reviewer/install.py`, `reviewer/install_claude.py`, `reviewer/mcp/service.py`, `scripts/verify_launcher_distribution.py` | — |
| PRI-236 | measured | 0.5 | 1 | +0.5 | `reviewer/config/settings.py`, `reviewer/entrypoints/mcp_server.py`, `reviewer/graph/family.py`, `reviewer/graph/store.py`, `reviewer/index/embeddings.py`, `reviewer/index/store.py`, `reviewer/install.py`, `reviewer/tasks/boards/kaiten.py` | — |
| PRI-250 | measured | 0 | 0.5 | +0.5 | `plugin/hooks/_transcript.py`, `plugin/hooks/brief_cost.py`, `reviewer/agent/assemble.py`, `reviewer/entrypoints/mcp_server.py`, `reviewer/mcp/service.py`, `reviewer/metrics/brief_quality/briefs.py`, `reviewer/metrics/brief_quality/recall.py`, `reviewer/web/history.py` | — |
| PRI-213 | measured | 0.2857 | 0.7143 | +0.4286 | `reviewer/config/settings.py`, `reviewer/config/task_board.py`, `reviewer/entrypoints/mcp_server.py`, `reviewer/mcp/service.py`, `reviewer/tasks/boards/asana.py`, `reviewer/tasks/boards/kaiten.py`, `reviewer/tasks/boards/yandex_tracker.py`, `reviewer/tasks/boards/youtrack.py` | — |
| PRI-205 | measured | 0.6 | 1 | +0.4 | `reviewer/config/task_board.py`, `reviewer/entrypoints/mcp_server.py`, `reviewer/mcp/service.py`, `reviewer/policy/policy.py`, `reviewer/tasks/boards/kaiten.py`, `reviewer/tasks/boards/weeek.py`, `reviewer/tasks/boards/yandex_tracker.py` | — |
| PRI-221 | measured | 0.2222 | 0.5556 | +0.3333 | `reviewer/config/committed.py`, `reviewer/config/settings.py`, `reviewer/graph/scip.py`, `reviewer/install.py`, `reviewer/mcp/session_serde.py`, `reviewer/metrics/brief_quality/classify.py`, `reviewer/policy/policy.py`, `reviewer/services/repo_id.py` | — |
| PRI-249 | measured | 0 | 0.3333 | +0.3333 | `reviewer/agent/assemble.py`, `reviewer/entrypoints/cli.py`, `reviewer/entrypoints/mcp_server.py`, `reviewer/index/store.py`, `reviewer/metrics/brief_quality/briefs.py`, `reviewer/services/review_service.py`, `reviewer/web/app.py`, `reviewer/web/history.py` | — |
| PRI-255 | measured | 0.3333 | 0.6667 | +0.3333 | `reviewer/graph/metrics.py`, `reviewer/graph/store.py`, `reviewer/index/embeddings.py`, `reviewer/index/store.py`, `reviewer/retrieval/multiquery.py`, `reviewer/retrieval/retriever.py`, `reviewer/tasks/boards/attachments.py`, `reviewer/web/history.py` | — |
| PRI-179 | measured | 0.5 | 0.75 | +0.25 | `reviewer/app.py`, `reviewer/entrypoints/cli.py`, `reviewer/entrypoints/mcp_server.py`, `reviewer/gitutil.py`, `reviewer/graph/builder.py`, `reviewer/graph/family.py`, `reviewer/graph/inherit.py`, `reviewer/graph/scip.py` | — |
| PRI-134 | measured | 0.5 | 0.75 | +0.25 | `reviewer/agent/state.py`, `reviewer/config/layers.py`, `reviewer/graph/family.py`, `reviewer/index/embeddings.py`, `reviewer/mcp/session_serde.py`, `reviewer/services/gc.py`, `reviewer/tasks/boards/attachments.py`, `reviewer/tasks/boards/clickup.py` | — |
| PRI-234 | measured | 0.75 | 1 | +0.25 | `reviewer/compose_lifecycle.py`, `reviewer/config/branches.py`, `reviewer/config/settings.py`, `reviewer/mcp/service.py`, `reviewer/mcp/session_store.py`, `reviewer/metrics/brief_quality/classify.py`, `reviewer/policy/policy.py`, `reviewer/services/brief_quality.py` | — |
| PRI-235 | measured | 0.75 | 1 | +0.25 | `reviewer/config/onboarding.py`, `reviewer/gitutil.py`, `reviewer/graph/family.py`, `reviewer/install.py`, `reviewer/mcp/service.py`, `reviewer/mcp/task_context.py`, `reviewer/metrics/brief_quality/classify.py`, `reviewer/services/review_service.py` | — |
| PRI-202 | measured | 0.2222 | 0.4444 | +0.2222 | `reviewer/app.py`, `reviewer/config/layers.py`, `reviewer/graph/family.py`, `reviewer/index/embeddings.py`, `reviewer/mcp/service.py`, `reviewer/policy/policy.py`, `reviewer/services/gc.py`, `reviewer/services/review_service.py` | — |
| PRI-215 | measured | 0.07143 | 0.2857 | +0.2143 | `reviewer/mcp/service.py`, `reviewer/tasks/boards/asana.py`, `reviewer/tasks/boards/base.py`, `reviewer/tasks/boards/clickup.py`, `reviewer/tasks/boards/kaiten.py`, `reviewer/tasks/boards/registry.py`, `reviewer/tasks/boards/trello.py`, `reviewer/tasks/boards/yougile.py` | — |
| PRI-196 | measured | 0.3 | 0.5 | +0.2 | `reviewer/mcp/service.py`, `reviewer/tasks/boards/asana.py`, `reviewer/tasks/boards/jira.py`, `reviewer/tasks/boards/linear.py`, `reviewer/tasks/boards/trello.py`, `reviewer/tasks/boards/weeek.py`, `reviewer/tasks/service.py`, `reviewer/tasks/store.py` | — |
| PRI-225 | measured | 0.2222 | 0.3889 | +0.1667 | `reviewer/config/settings.py`, `reviewer/entrypoints/mcp_server.py`, `reviewer/tasks/boards/github.py`, `reviewer/tasks/graph.py`, `reviewer/tasks/service.py`, `reviewer/tasks/store.py`, `reviewer/tasks/sync_cursor.py`, `reviewer/tasks/sync_filter.py` | — |
| PRI-247 | measured | 0.3333 | 0.5 | +0.1667 | `plugin/hooks/_transcript.py`, `plugin/hooks/brief_cost.py`, `plugin/hooks/review_cost.py`, `reviewer/entrypoints/mcp_server.py`, `reviewer/graph/builder.py`, `reviewer/services/gc.py`, `reviewer/tasks/boards/base.py`, `scripts/update_codex_plugin_manifest.py` | — |
| PRI-223 | measured | 0.16 | 0.32 | +0.16 | `reviewer/config/layers.py`, `reviewer/config/onboarding.py`, `reviewer/config/provider_access.py`, `reviewer/config/settings.py`, `reviewer/entrypoints/mcp_server.py`, `reviewer/services/repo_id.py`, `reviewer/tasks/boards/adf.py`, `reviewer/tasks/boards/github.py` | — |
| PRI-245 | measured | 0.1111 | 0.2222 | +0.1111 | `reviewer/compose_lifecycle.py`, `reviewer/config/committed.py`, `reviewer/entrypoints/cli.py`, `reviewer/graph/backend.py`, `reviewer/graph/summaries.py`, `reviewer/index/summary_store.py`, `reviewer/install.py`, `reviewer/tasks/boards/base.py` | — |
| PRI-162 | empty_core_denominator | — | — | — | `reviewer/entrypoints/mcp_server.py`, `reviewer/index/chunker.py`, `reviewer/install.py`, `reviewer/install_codex.py`, `reviewer/metrics/brief_quality/recall.py`, `reviewer/retrieval/retriever.py`, `reviewer/services/gc.py`, `reviewer/tasks/taskdoc.py` | — |
| PRI-164 | empty_core_denominator | — | — | — | `reviewer/app.py`, `reviewer/entrypoints/mcp_server.py`, `reviewer/install.py`, `reviewer/services/brief_quality.py`, `reviewer/tasks/boards/asana.py`, `reviewer/tasks/boards/base.py`, `reviewer/tasks/store.py`, `reviewer/tasks/subtasks.py` | — |
| PRI-176 | empty_core_denominator | — | — | — | `reviewer/tasks/store.py` | — |
| PRI-203 | empty_core_denominator | — | — | — | `reviewer/app.py`, `reviewer/config/settings.py`, `reviewer/entrypoints/cli.py`, `reviewer/graph/inherit.py`, `reviewer/install.py`, `reviewer/policy/context_limits.py`, `reviewer/services/gc.py`, `reviewer/tasks/service.py` | — |
| PRI-206 | empty_core_denominator | — | — | — | `plugin/hooks/reviewer_defect.py`, `reviewer/entrypoints/cli.py`, `reviewer/entrypoints/mcp_server.py`, `reviewer/install.py`, `reviewer/mcp/task_context.py`, `reviewer/retrieval/multiquery.py`, `reviewer/retrieval/retriever.py`, `reviewer/vcs/github.py` | — |
| PRI-208 | empty_core_denominator | — | — | — | `reviewer/config/committed.py`, `reviewer/config/layers.py`, `reviewer/entrypoints/mcp_server.py`, `reviewer/install_claude.py`, `reviewer/mcp/service.py`, `reviewer/policy/policy.py`, `reviewer/services/review_service.py`, `reviewer/tasks/boards/attachments.py` | — |
| PRI-211 | measured | 0 | 0 | +0 | `reviewer/entrypoints/mcp_server.py`, `reviewer/mcp/service.py`, `reviewer/metrics/brief_quality/briefs.py`, `reviewer/metrics/brief_quality/classify.py`, `reviewer/services/graph_sync.py`, `reviewer/web/api.py`, `reviewer/web/app.py`, `reviewer/web/history.py` | — |
| PRI-212 | measured | 1 | 1 | +0 | `plugin/hooks/reviewer_defect.py`, `reviewer/gitutil.py`, `reviewer/index/embeddings.py`, `reviewer/index/reranker.py`, `reviewer/services/review_service.py`, `reviewer/tasks/boards/kaiten.py`, `reviewer/tasks/boards/weeek.py`, `reviewer/web/history.py` | — |
| PRI-172 | measured | 0 | 0 | +0 | `reviewer/bugreport/sanitize.py`, `reviewer/install_codex.py`, `reviewer/vcs/gitlab.py` | — |
| PRI-216 | empty_core_denominator | — | — | — | `reviewer/graph/backend.py`, `reviewer/graph/family.py`, `reviewer/launcher/app.py`, `reviewer/launcher/controller.py`, `reviewer/mcp/session_store.py`, `reviewer/tasks/boards/base.py`, `reviewer/tasks/subtask_store.py`, `reviewer/web/history.py` | — |
| PRI-217 | measured | 0 | 0 | +0 | `reviewer/tasks/boards/asana.py`, `reviewer/tasks/boards/base.py`, `reviewer/tasks/boards/github.py`, `reviewer/tasks/boards/jira.py`, `reviewer/tasks/boards/weeek.py`, `reviewer/tasks/boards/yandex_tracker.py`, `reviewer/tasks/boards/yougile.py`, `reviewer/tasks/boards/youtrack.py` | — |
| PRI-177 | measured | 0 | 0 | +0 | `reviewer/mcp/service.py`, `reviewer/metrics/brief_quality/classify.py`, `reviewer/policy/policy.py`, `reviewer/tasks/boards/youtrack.py` | — |
| PRI-219 | measured | 0.5 | 0.5 | +0 | `reviewer/agent/assemble.py`, `reviewer/entrypoints/mcp_server.py`, `reviewer/index/summary_store.py`, `reviewer/install_codex.py`, `reviewer/services/gc.py`, `reviewer/tasks/boards/base.py`, `reviewer/tools/code_tools.py`, `scripts/update_codex_plugin_manifest.py` | — |
| PRI-220 | empty_core_denominator | — | — | — | `plugin/hooks/reviewer_defect.py`, `reviewer/config/task_board.py`, `reviewer/mcp/task_context.py`, `reviewer/tasks/boards/base.py`, `reviewer/tasks/boards/clickup.py`, `reviewer/tasks/boards/github.py`, `reviewer/tasks/boards/linear.py`, `reviewer/tasks/boards/yougile.py` | — |
| PRI-178 | measured | 1 | 1 | +0 | `reviewer/entrypoints/cli.py`, `reviewer/entrypoints/mcp_server.py`, `reviewer/index/store.py`, `reviewer/install.py`, `reviewer/mcp/service.py`, `reviewer/retrieval/cliff.py`, `reviewer/tasks/boards/adf.py`, `reviewer/tools/code_tools.py` | — |
| PRI-227 | measured | 0 | 0 | +0 | `plugin/hooks/brief_post_write.py`, `reviewer/compose_lifecycle.py`, `reviewer/config/task_board.py`, `reviewer/install.py`, `reviewer/launcher/controller.py`, `reviewer/metrics/brief_quality/briefs.py`, `reviewer/tasks/boards/github.py`, `reviewer/tasks/subtasks.py` | — |
| PRI-222 | measured | 1 | 1 | +0 | `reviewer/bugreport/triage.py`, `reviewer/compose_lifecycle.py`, `reviewer/config/settings.py`, `reviewer/entrypoints/mcp_server.py`, `reviewer/graph/scip.py`, `reviewer/services/repo_id.py`, `reviewer/tasks/boards/github.py`, `reviewer/web/history.py` | — |
| PRI-228 | measured | 0.5 | 0.5 | +0 | `reviewer/entrypoints/mcp_server.py`, `reviewer/graph/family.py`, `reviewer/index/store.py`, `reviewer/mcp/service.py`, `reviewer/metrics/brief_quality/briefs.py`, `reviewer/retrieval/multiquery.py`, `reviewer/tasks/service.py`, `reviewer/web/history.py` | — |
| PRI-237 | measured | 0 | 0 | +0 | `plugin/hooks/reviewer_defect.py`, `reviewer/entrypoints/mcp_server.py`, `reviewer/graph/summaries.py`, `reviewer/index/chunker.py`, `reviewer/services/brief_quality.py`, `reviewer/services/summary_fragments.py`, `reviewer/tasks/boards/base.py`, `reviewer/tools/code_tools.py` | — |
| PRI-238 | measured | 0 | 0 | +0 | `reviewer/graph/scip.py`, `reviewer/install.py`, `reviewer/tasks/boards/linear.py`, `reviewer/tasks/boards/yandex_tracker.py`, `reviewer/tasks/boards/yougile.py`, `reviewer/tasks/boards/youtrack.py`, `reviewer/tasks/pr_backlink.py`, `scripts/update_codex_plugin_manifest.py` | — |
| PRI-239 | measured | 0.5 | 0.5 | +0 | `plugin/hooks/reviewer_defect.py`, `reviewer/app.py`, `reviewer/bugreport/environment.py`, `reviewer/bugreport/triage.py`, `reviewer/entrypoints/cli.py`, `reviewer/services/gc.py`, `reviewer/tasks/boards/github.py`, `reviewer/web/history.py` | — |
| PRI-241 | measured | 1 | 1 | +0 | `reviewer/config/settings.py`, `reviewer/graph/family.py`, `reviewer/graph/scip.py`, `reviewer/launcher/app.py`, `reviewer/metrics/brief_quality/briefs.py`, `reviewer/policy/policy.py`, `reviewer/tasks/boards/yougile.py`, `reviewer/web/serve.py` | — |
| PRI-242 | measured | 0.5 | 0.5 | +0 | `reviewer/config/settings.py`, `reviewer/entrypoints/launcher.py`, `reviewer/entrypoints/mcp_server.py`, `reviewer/graph/store.py`, `reviewer/tasks/boards/base.py`, `reviewer/tasks/boards/linear.py`, `reviewer/update_lifecycle.py`, `scripts/changelog_section.py` | — |
| PRI-243 | measured | 0 | 0 | +0 | `reviewer/config/committed.py`, `reviewer/graph/backend.py`, `reviewer/index/store.py`, `reviewer/install.py`, `reviewer/mcp/task_context.py`, `reviewer/services/brief_quality.py`, `reviewer/services/gc.py`, `reviewer/services/repo_id.py` | — |
| PRI-246 | empty_core_denominator | — | — | — | `plugin/hooks/brief_guard.py`, `reviewer/compose_lifecycle.py`, `reviewer/entrypoints/cli.py`, `reviewer/mcp/task_context.py`, `reviewer/metrics/brief_quality/briefs.py`, `reviewer/policy/policy.py`, `reviewer/tasks/boards/base.py`, `reviewer/tasks/boards/youtrack.py` | — |
| PRI-251 | measured | 0.1429 | 0.1429 | +0 | `reviewer/metrics/brief_quality/classify.py`, `reviewer/metrics/brief_quality/recall.py`, `reviewer/services/brief_quality.py`, `reviewer/tasks/boards/asana.py`, `reviewer/tasks/boards/clickup.py`, `reviewer/tasks/boards/registry.py`, `reviewer/tasks/boards/weeek.py`, `reviewer/web/api.py` | — |
| PRI-248 | measured | 0 | 0 | +0 | `reviewer/bugreport/environment.py`, `reviewer/install.py`, `reviewer/install_codex.py`, `reviewer/launcher/app.py`, `reviewer/metrics/brief_quality/briefs.py`, `reviewer/retrieval/retriever.py`, `reviewer/tasks/boards/base.py`, `reviewer/tools/code_tools.py` | — |
| PRI-254 | empty_core_denominator | — | — | — | `reviewer/bugreport/triage.py`, `reviewer/compose_lifecycle.py`, `reviewer/entrypoints/mcp_server.py`, `reviewer/index/chunker.py`, `reviewer/mcp/service.py`, `reviewer/retrieval/retriever.py`, `reviewer/tasks/boards/base.py`, `reviewer/tasks/taskdoc.py` | — |
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

## Приёмка PRI-256

Файловый бюджет секции `code`: выдача ограничивается числом РАЗЛИЧНЫХ файлов
(`context_limits.code_section`, дефолт `max_files=12`, `max_chunks_per_file=1`,
`chars_per_file=1300`), а не арифметическим следствием символьного потолка.

### Дельта в replay

Обе стороны сняты в одном прогоне, на одном `indexed_sha` (`7d66a08`), одном коммите
(`25a7aa9`) и одном корпусе (57 задач, измерено 41), 2026-08-17:

| Метрика | до (бюджет 4×1×2000) | после (12×1×1300) | Δ |
|---|---|---|---|
| различных файлов секции (медиана) | 4 | 12 | **+8** |
| объём секции, символов (медиана) | 6412 | 13660 | **+7248 (×2.13)** |
| core-recall (медиана) | 0.2857 | 0.5 | **+0.2143** |
| core-recall bulk (ядро ≥ 10) | 0.1911 | 0.3544 | **+0.1633** |
| bulk N | 4 | 4 | — |
| precision (медиана) | 0.5 | 0.25 | −0.25 |

Обе линии recall выросли: медианная (N=41) и bulk (N=4). Precision упала ровно так, как
ожидалось от расширения выдачи, — отбор нужного из двенадцати остаётся работой сборщика
брифа, а отсутствующего в выдаче файла он не восстановит.

### Критерий «рост объёма не кратен росту числа файлов»

Число файлов выросло втрое (4 → 12), объём секции — в 2.13 раза (6412 → 13660 символов;
среднее 6479 → 13408, те же 2.07). Кратного роста нет: доля символов на файл сокращена с
2000 до 1300, поэтому втрое больше файлов стоят вдвое больше символов. Замер объёма —
`.superpowers/sdd/2026-08-17-pri-256-file-diversification/pri256-volume.json`, все 57 задач
корпуса, обе стороны в одном процессе.

### Три оговорки, без которых числа читаются неверно

1. **Baseline — «4 файла» из раздела «Приёмка PRI-255», а не «5-6» из описания тикета.**
   Цифра «5-6» ни одним отчётом этого репозитория не подтверждается и, судя по всему,
   предшествует замеру PRI-255. Сверять приёмку с ней значило бы мерить две стороны
   разными линейками.
2. **Сторона «до» эмулирована оверрайдом `code_section` 4×1×2000, а не старым кодом.**
   Это воспроизводит прежнюю арифметическую стену `max_tool_result_chars 8000 ÷
   MAX_BLOCK_CHARS 2000 = 4 блока`. Эмуляция великодушна к baseline: старый путь мог
   потратить слоты на несколько чанков ОДНОГО файла и дать меньше четырёх различных
   файлов. Измеренная дельта, таким образом, — нижняя оценка.
3. **`indexed_sha` не тот, что у PRI-255.** Тот замер снят на `a1b28c1`, индекс с тех пор
   перестроен на `7d66a08`, а корпус вырос с 56 задач до 57. Поэтому числа PRI-256 не
   сравниваются с числами PRI-255 напрямую: сравниваются только две стороны ЭТОГО прогона
   (у обеих `indexed_sha`, коммит и корпус совпадают — предупреждений о несопоставимости
   в отчёте нет).

### Процедура воспроизведения

Сторона «до» и сторона «после», два прогона подряд (второй берёт первый как baseline):

```bash
.venv/bin/python -m eval.solve_task_metrics replay --variant multiquery \
  --repo mimfort/rag_for_git --branch dev --baseline multiquery \
  --set code_section.max_files=4 --set code_section.max_chunks_per_file=1 \
  --set code_section.chars_per_file=2000
.venv/bin/python -m eval.solve_task_metrics replay --variant multiquery \
  --repo mimfort/rag_for_git --branch dev --baseline last
```

Объём секции в символах в снимок replay не входит (снимок хранит пути, не тексты), поэтому
он считается отдельно — обе стороны в одном процессе, чтобы LRU эмбеддера не удваивал
расход Voyage:

```python
from eval.solve_task_metrics import live, replay as replay_mod
from reviewer.mcp.subqueries import build_subqueries

BEFORE = {"code_section": {"max_files": 4, "max_chunks_per_file": 1,
                           "chars_per_file": 2000}}
provider, repo, branch = live.open_live("mimfort/rag_for_git", "dev")
with provider:
    for key in replay_mod.corpus_keys(pathlib.Path("docs/superpowers/briefs")):
        task = provider.task(key)
        queries = build_subqueries(task, provider.query(task, key))
        before = provider.code_multi(repo, branch, queries, BEFORE)
        after = provider.code_multi(repo, branch, queries, None)
        print(key, len(before), len(after))
```

Оверрайд `code_section` через `--set` появился вместе с этим замером (PRI-256): без него
раздел не сериализовался в `limits_to_yaml` и молча терялся в `LiveRetrieval.code_multi`,
то есть сторону «до» нечем было бы выразить.
